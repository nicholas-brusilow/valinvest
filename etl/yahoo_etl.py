"""Yahoo Finance (yfinance) -> PostgreSQL ETL.

Fetches daily OHLCV + dividends + splits for the EDGAR-derived ticker universe
and loads price_daily / dividend / stock_split / price_quarterly via COPY.

Usage:
    python etl/yahoo_etl.py [--tickers AAPL,MSFT] [--workers 8] [--rate 5]
                            [--chunk 300] [--reset]
                            [--log-file logs/yahoo_etl.log]
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import json
import math
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import yfinance as yf

from common import (
    TICKER_MAP_PATH,
    TICKER_MAP_URL,
    chunked,
    copy_csv,
    download,
    env,
    excluded_tickers,
    get_conn,
    quarter_label,
    setup_logging,
)

PRICE_COLS = ["ticker_yahoo", "date", "open", "high", "low", "close", "adj_close", "volume"]
DIV_COLS = ["ticker_yahoo", "ex_date", "amount", "amount_as_reported", "quarter", "currency"]
SPLIT_COLS = ["ticker_yahoo", "split_date", "ratio"]
PQUARTER_COLS = ["ticker_yahoo", "quarter", "close_raw", "close_adj", "close_as_traded",
                 "currency", "price_date"]

YAHOO_DIR = "data/csv_yahoo"


# --------------------------------------------------------------------------- #
# Value formatting
# --------------------------------------------------------------------------- #
def _f(x) -> str:
    """Format a float for a NUMERIC(18,6) column.

    Out-of-range/huge outliers (bad vendor data, e.g. warrants) are dropped to
    NULL so a single bad value cannot abort a COPY.
    """
    try:
        if x is None:
            return ""
        fx = float(x)
        if math.isnan(fx) or math.isinf(fx):
            return ""
        if not (-1e12 < fx < 1e12):
            return ""
        return repr(fx)
    except Exception:
        return ""


def _i(x) -> str:
    """Format an integer for a BIGINT column (drop absurd outliers)."""
    try:
        if x is None:
            return ""
        fx = float(x)
        if math.isnan(fx) or math.isinf(fx):
            return ""
        if abs(fx) >= 9.2e18:
            return ""
        return str(int(round(fx)))
    except Exception:
        return ""


def _norm_ticker(t: str) -> str:
    return re.sub(r"[./]", "-", (t or "").strip().upper())


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Global token limiter: enforces a minimum interval between request starts."""

    def __init__(self, rate: float):
        self.interval = 1.0 / max(float(rate), 0.001)
        self.lock = threading.Lock()
        self.next_time = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if now < self.next_time:
                time.sleep(self.next_time - now)
                now = time.monotonic()
            self.next_time = now + self.interval


# --------------------------------------------------------------------------- #
# Fetch + normalize
# --------------------------------------------------------------------------- #
def _currency_of(ticker) -> str | None:
    try:
        meta = getattr(ticker, "history_metadata", None) or {}
        if isinstance(meta, dict) and meta.get("currency"):
            return meta["currency"]
    except Exception:
        pass
    try:
        return ticker.fast_info.currency
    except Exception:
        return None


def _build(sym: str, df, currency):
    """Normalize a history frame; return dict of CSV rows + status."""
    result = {"ticker": sym, "status": "empty", "error": None,
              "price": [], "div": [], "split": [], "pquarter": [], "currency": currency,
              "pq_skipped": 0, "div_skipped": 0}

    if df is None or len(df) == 0:
        return result
    if "Close" not in df.columns:
        return result

    # normalize index to tz-naive
    try:
        idx = df.index
        if getattr(idx, "tz", None) is not None:
            df.index = idx.tz_localize(None)
    except Exception:
        pass

    try:
        df = df[~df.index.duplicated(keep="last")]
    except Exception:
        pass
    df = df[df["Close"].notna()]
    if len(df) == 0:
        return result

    has_adj = "Adj Close" in df.columns

    # --- split factors ------------------------------------------------------ #
    # yfinance Close is split-adjusted to the present.  To recover the
    # as-traded (historical) price: as_traded(d) = close_raw(d) * product of
    # every split ratio with split_date > d.  Reverse splits have ratio < 1 and
    # are multiplied in as well.
    split_list: list = []
    if "Stock Splits" in df.columns:
        seen = set()
        for ts, row in df.iterrows():
            ratio = row.get("Stock Splits")
            try:
                v = float(ratio)
            except Exception:
                continue
            if math.isnan(v) or math.isinf(v) or v <= 0:
                continue
            d = ts.date() if hasattr(ts, "date") else ts
            if d in seen:
                continue
            seen.add(d)
            ratio_s = _f(v)
            if not ratio_s:
                continue
            split_list.append((d, v))
            result["split"].append([sym, d.isoformat(), ratio_s])
    split_list.sort(key=lambda x: x[0])
    split_dates = [d for d, _r in split_list]
    total_ratio = 1.0
    prefix: list = []
    acc = 1.0
    for _d, r in split_list:
        total_ratio *= r
        acc *= r
        prefix.append(acc)

    def _factor(d) -> float:
        """Product of split ratios strictly after ``d`` (1.0 if none)."""
        if not split_dates:
            return 1.0
        i = bisect.bisect_right(split_dates, d) - 1
        if i < 0:
            return total_ratio
        return total_ratio / prefix[i]

    quarter_last: dict = {}
    as_traded_map: dict = {}  # date -> as-traded close (only valid values)

    for ts, row in df.iterrows():
        try:
            d = ts.date() if hasattr(ts, "date") else ts
        except Exception:
            continue
        close = row.get("Close")
        result["price"].append([
            sym, d.isoformat(), _f(row.get("Open")), _f(row.get("High")),
            _f(row.get("Low")), _f(close), _f(row.get("Adj Close")) if has_adj else "",
            _i(row.get("Volume")),
        ])
        # as-traded close (valid only when the sanitized close and product are sane)
        close_s = _f(close)
        at = None
        if close_s:
            try:
                cv = float(close_s)
            except ValueError:
                cv = None
            if cv is not None and cv > 0:
                cand = cv * _factor(d)
                if 0 < cand < 1e12:
                    at = cand
                    as_traded_map[d] = cand
        # quarterly last observation wins, but only if the as-traded price is
        # valid (1e-6 <= at <= 1e7).  Invalid/NULL closes, ultra-tiny corrupt
        # reverse-split artifacts and implausibly large values clear the quarter
        # so no bad price_quarterly row is written.
        q = quarter_label(d)
        qrow = None
        if close_s and at is not None and 1e-6 <= at <= 1e7:
            qrow = [sym, q, close_s,
                    _f(row.get("Adj Close")) if has_adj else "", _f(at), currency, d.isoformat()]
        quarter_last[q] = qrow

    # dividends (exclude Capital Gains -- only the Dividends column is used)
    # Yahoo dividends are split-adjusted like Close; amount_as_reported restores
    # the as-paid per-share cash amount using the same split factors.
    if "Dividends" in df.columns:
        div_map: dict = {}
        for ts, row in df.iterrows():
            amt = row.get("Dividends")
            try:
                v = float(amt)
            except Exception:
                result["div_skipped"] += 1
                continue
            if math.isnan(v) or math.isinf(v):
                result["div_skipped"] += 1
                continue
            if v == 0.0:
                continue  # normal "no dividend" marker, not a glitch
            if v < 0:
                result["div_skipped"] += 1
                continue
            d = ts.date() if hasattr(ts, "date") else ts
            ar = v * _factor(d)
            if not math.isfinite(ar) or ar <= 0 or ar > 100000:
                result["div_skipped"] += 1
                continue
            at = as_traded_map.get(d)
            if at is not None and ar > at:
                # as-paid dividend exceeds the as-traded share price -> vendor glitch
                result["div_skipped"] += 1
                continue
            amt_s = _f(v)
            ar_s = _f(ar)
            if not amt_s or not ar_s:
                result["div_skipped"] += 1
                continue
            div_map[d] = [sym, d.isoformat(), amt_s, ar_s, quarter_label(d), currency]
        result["div"] = list(div_map.values())

    result["pquarter"] = [r for r in quarter_last.values() if r is not None]
    result["pq_skipped"] = sum(1 for r in quarter_last.values() if r is None)
    result["status"] = "ok" if result["price"] else "empty"
    return result


def fetch_one(sym: str, limiter: RateLimiter, retries: int = 4) -> dict:
    last_exc = None
    for attempt in range(retries + 1):
        limiter.wait()
        try:
            t = yf.Ticker(sym)
            df = t.history(
                period="max", interval="1d", auto_adjust=False,
                actions=True, repair=False, keepna=False,
            )
            return _build(sym, df, _currency_of(t))
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= retries:
                break
            time.sleep(2 ** (attempt + 1) + random.uniform(0, 1))
    return {"ticker": sym, "status": "error", "error": str(last_exc),
            "price": [], "div": [], "split": [], "pquarter": [], "currency": None}


# --------------------------------------------------------------------------- #
# Universe / resume
# --------------------------------------------------------------------------- #
def get_universe(tickers_arg: str, conn, logger) -> list:
    if tickers_arg:
        tickers = [_norm_ticker(t) for t in tickers_arg.split(",") if _norm_ticker(t)]
        logger.info("universe from --tickers: %d tickers", len(tickers))
    else:
        tickers = None
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT ticker_yahoo FROM ticker_map WHERE ticker_yahoo IS NOT NULL")
                rows = [r[0] for r in cur.fetchall()]
            if rows:
                tickers = rows
                logger.info("universe from ticker_map: %d tickers", len(tickers))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ticker_map unavailable (%s); falling back to JSON", exc)
            try:
                conn.rollback()
            except Exception:
                pass

        if tickers is None:
            if not os.path.exists(TICKER_MAP_PATH) or os.path.getsize(TICKER_MAP_PATH) == 0:
                download(TICKER_MAP_URL, TICKER_MAP_PATH, logger=logger)
            with open(TICKER_MAP_PATH, "r", encoding="utf-8") as fh:
                obj = json.load(fh)
            fields = obj.get("fields") or []
            data = obj.get("data") or []
            ti = fields.index("ticker") if "ticker" in fields else 2
            tickers = []
            for rec in data:
                if isinstance(rec, (list, tuple)) and len(rec) > ti:
                    tickers.append(_norm_ticker(rec[ti]))
                elif isinstance(rec, dict):
                    tickers.append(_norm_ticker(rec.get("ticker", "")))
            logger.info("universe from JSON: %d tickers", len(tickers))

    # Drop NONE- and excluded tickers (corrupt vendor price series; see
    # etl/excluded_tickers.txt) so they are never fetched again.
    candidates = {t for t in tickers if t and t != "NONE-"}
    dropped = sorted(candidates & excluded_tickers())
    if dropped:
        logger.warning(
            "universe: dropping %d excluded ticker(s) with corrupt price history: %s",
            len(dropped), ", ".join(dropped),
        )
    return sorted(candidates - excluded_tickers())


def get_done(conn, logger) -> set:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker_yahoo FROM yahoo_fetch_log WHERE status IN ('ok','empty')")
            return {r[0] for r in cur.fetchall()}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return set()


# --------------------------------------------------------------------------- #
# Chunk processing
# --------------------------------------------------------------------------- #
def _open_writer(path, cols):
    fh = open(path, "w", newline="", encoding="utf-8")
    w = csv.writer(fh)
    w.writerow(cols)
    return fh, w


def process_chunk(tickers, results_iter, conn, logger, failures_fh):
    """Write one chunk's results to CSVs and COPY them into PostgreSQL."""
    os.makedirs(YAHOO_DIR, exist_ok=True)
    price_p = os.path.join(YAHOO_DIR, "price_daily.csv")
    div_p = os.path.join(YAHOO_DIR, "dividend.csv")
    split_p = os.path.join(YAHOO_DIR, "stock_split.csv")
    pq_p = os.path.join(YAHOO_DIR, "price_quarterly.csv")

    n_price = n_div = n_split = n_pq = 0
    n_pq_skipped = n_div_skipped = 0
    stats = {"ok": 0, "empty": 0, "error": 0}
    div_tickers = set()
    log_rows = []

    fh_price, w_price = _open_writer(price_p, PRICE_COLS)
    fh_div, w_div = _open_writer(div_p, DIV_COLS)
    fh_split, w_split = _open_writer(split_p, SPLIT_COLS)
    fh_pq, w_pq = _open_writer(pq_p, PQUARTER_COLS)
    try:
        for res in results_iter:
            sym = res.get("ticker")
            status = res.get("status", "error")
            stats[status] = stats.get(status, 0) + 1
            for r in res.get("price", []):
                w_price.writerow(r)
                n_price += 1
            for r in res.get("div", []):
                w_div.writerow(r)
                n_div += 1
                div_tickers.add(sym)
            for r in res.get("split", []):
                w_split.writerow(r)
                n_split += 1
            for r in res.get("pquarter", []):
                w_pq.writerow(r)
                n_pq += 1
            n_pq_skipped += res.get("pq_skipped", 0)
            n_div_skipped += res.get("div_skipped", 0)
            log_rows.append([sym, status, len(res.get("price", [])), res.get("error")])
            if status == "error":
                failures_fh.writerow([sym, res.get("error") or ""])
                failures_fh.flush()
    finally:
        for fh in (fh_price, fh_div, fh_split, fh_pq):
            fh.close()

    # row counts per ticker for the fetch log: recompute from price list sizes
    # (we stored size per result inline below)
    copy_csv(conn, "price_daily", PRICE_COLS, price_p)
    copy_csv(conn, "dividend", DIV_COLS, div_p)
    copy_csv(conn, "stock_split", SPLIT_COLS, split_p)
    copy_csv(conn, "price_quarterly", PQUARTER_COLS, pq_p)

    with conn.cursor() as cur:
        for r in log_rows:
            cur.execute(
                "INSERT INTO yahoo_fetch_log (ticker_yahoo, status, rows, error) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (ticker_yahoo) DO UPDATE "
                "SET status = EXCLUDED.status, rows = EXCLUDED.rows, "
                "error = EXCLUDED.error, fetched_at = now()",
                (r[0], r[1], r[2], r[3]),
            )
    conn.commit()

    for p in (price_p, div_p, split_p, pq_p):
        try:
            os.remove(p)
        except OSError:
            pass

    return {"price": n_price, "div": n_div, "split": n_split, "pq": n_pq,
            "pq_skipped": n_pq_skipped, "div_skipped": n_div_skipped,
            "stats": stats, "div_tickers": div_tickers}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Yahoo Finance -> PostgreSQL ETL")
    p.add_argument("--tickers", default="", help="comma separated ticker override")
    p.add_argument("--workers", type=int, default=int(env("YF_WORKERS", "8")))
    p.add_argument("--rate", type=float, default=float(env("YF_RATE", "5")))
    p.add_argument("--chunk", type=int, default=300)
    p.add_argument("--reset", action="store_true")
    p.add_argument("--replace", action="store_true",
                   help="with --tickers: delete those tickers' rows and re-import them")
    p.add_argument("--log-file", default="logs/yahoo_etl.log")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logger = setup_logging("yahoo_etl", args.log_file)
    t0 = time.time()
    os.makedirs(YAHOO_DIR, exist_ok=True)

    conn = get_conn()
    if args.reset:
        logger.info("truncating yahoo tables (--reset)")
        with conn.cursor() as cur:
            cur.execute("TRUNCATE price_daily, dividend, stock_split, price_quarterly, yahoo_fetch_log")
        conn.commit()

    universe = get_universe(args.tickers, conn, logger)
    done = get_done(conn, logger)
    if args.replace and args.tickers:
        logger.info("--replace: deleting existing rows for %d tickers", len(universe))
        with conn.cursor() as cur:
            for table in ("price_daily", "dividend", "stock_split", "price_quarterly",
                          "yahoo_fetch_log"):
                cur.execute("DELETE FROM %s WHERE ticker_yahoo = ANY(%%s)" % table, (universe,))
        conn.commit()
        # bypass resume for the replaced tickers (re-fetch even if previously ok)
        done = done - set(universe)
    todo = [t for t in universe if t not in done]
    logger.info("universe=%d already-done=%d todo=%d", len(universe), len(done), len(todo))

    limiter = RateLimiter(args.rate)
    failures_path = "data/yahoo_failures.csv"
    failures_missing = not os.path.exists(failures_path)
    failures_fh = open(failures_path, "a", newline="", encoding="utf-8")
    if failures_missing:
        csv.writer(failures_fh).writerow(["ticker_yahoo", "error"])
        failures_fh.flush()

    totals = {"price": 0, "div": 0, "split": 0, "pq": 0, "pq_skipped": 0, "div_skipped": 0}
    gstats = {"ok": 0, "empty": 0, "error": 0}
    all_div_tickers = set()
    processed = 0
    n_total = len(todo)

    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            for batch in chunked(todo, max(1, args.chunk)):
                futures = {pool.submit(fetch_one, sym, limiter): sym for sym in batch}
                results = []
                for fut in as_completed(futures):
                    try:
                        results.append(fut.result())
                    except Exception as exc:  # noqa: BLE001
                        results.append({"ticker": futures[fut], "status": "error",
                                        "error": str(exc), "price": [], "div": [],
                                        "split": [], "pquarter": [], "currency": None,
                                        "pq_skipped": 0, "div_skipped": 0})
                summary = None
                try:
                    summary = process_chunk(batch, results, conn, logger, failures_fh)
                except Exception as exc:  # noqa: BLE001 - a bad chunk must not kill the run
                    logger.exception("chunk failed (%d tickers); will be retried on resume: %s",
                                     len(batch), exc)
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    continue
                for k in totals:
                    totals[k] += summary[k]
                for k, v in summary["stats"].items():
                    gstats[k] = gstats.get(k, 0) + v
                all_div_tickers |= summary["div_tickers"]
                processed += len(batch)
                logger.info(
                    "progress %d/%d tickers | ok=%d empty=%d error=%d | rows: price=%d div=%d split=%d pq=%d | skipped: pq=%d div=%d | elapsed=%.1fs",
                    processed, n_total, gstats["ok"], gstats["empty"], gstats["error"],
                    totals["price"], totals["div"], totals["split"], totals["pq"],
                    totals["pq_skipped"], totals["div_skipped"],
                    time.time() - t0,
                )
    finally:
        failures_fh.close()
        conn.close()

    logger.info("=" * 70)
    logger.info("Yahoo ETL summary (%.1fs)", time.time() - t0)
    logger.info("  attempted: %d  ok=%d empty=%d error=%d",
                processed, gstats["ok"], gstats["empty"], gstats["error"])
    logger.info("  rows: price_daily=%d dividend=%d stock_split=%d price_quarterly=%d",
                totals["price"], totals["div"], totals["split"], totals["pq"])
    logger.info("  skipped: price_quarterly=%d (invalid/reverse-split artifacts), dividends=%d (non-finite/<=0/as-paid>1e5/as-paid>price)",
                totals["pq_skipped"], totals["div_skipped"])
    logger.info("  tickers with dividends: %d", len(all_div_tickers))
    logger.info("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
