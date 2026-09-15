"""One-time precompute of the ``stock_risk_quarter`` table.

Streams ``price_daily`` (split-adjusted ``close``) ticker by ticker, computes
trailing 252-trading-day risk metrics, samples the last observation of every
calendar quarter and COPYs the result into ``stock_risk_quarter``.

Usage:
    python dashboard/prepare.py [--force]

Without ``--force`` the script skips when the table already contains rows.
Exit code is non-zero on any failure.
"""
from __future__ import annotations

import argparse
import datetime
import math
import os
import sys
import time

import numpy as np
import pandas as pd
import psycopg

DEFAULT_DB_URL = "postgresql://valinvest:valinvest@db:5432/valinvest"

# price_daily has ~86k corrupt ~1e12 closes; treat these as missing.
MIN_CLOSE = 1e-6
MAX_CLOSE = 1e7

BATCH_TICKERS = 1000


def _sql_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "sql", "dashboard.sql")


def _quarter_label(ts: pd.Timestamp) -> str:
    return f"{ts.year}Q{ts.quarter}"


def _rows_for_ticker(ticker: str, dates: list, closes: list):
    """Return a list of COPY rows for one ticker's buffered daily closes."""
    s = pd.Series(np.asarray(closes, dtype="float64"), index=pd.to_datetime(dates))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    invalid = (s <= MIN_CLOSE) | (s >= MAX_CLOSE) | s.isna()
    s = s.mask(invalid)
    if s.notna().sum() == 0:
        return []

    r = s.pct_change()
    vol = r.rolling(252, min_periods=120).std() * np.sqrt(252)
    ret = s / s.shift(252) - 1
    off_high = s / s.rolling(252, min_periods=60).max() - 1

    df = pd.DataFrame({"vol": vol, "ret": ret, "off": off_high})
    df["_year"] = df.index.year
    df["_quarter"] = df.index.quarter
    last = df.groupby(["_year", "_quarter"], sort=True).tail(1)

    rows = []
    for ts, row in last.iterrows():
        rows.append(
            (
                ticker,
                _quarter_label(ts),
                ts.date(),
                None if pd.isna(row["vol"]) else float(row["vol"]),
                None if pd.isna(row["ret"]) else float(row["ret"]),
                None if pd.isna(row["off"]) else float(row["off"]),
            )
        )
    return rows


def _to_db(value, integer: bool = False):
    """Convert a pandas/numpy cell into something psycopg's COPY accepts."""
    if value is None:
        return None
    if integer:
        # e.g. cik is a nullable BIGINT but lands in pandas as float64 because of
        # the missing values; COPY would reject the "1750.0" text form.
        if isinstance(value, float) and math.isnan(value):
            return None
        return int(value)
    if isinstance(value, float):  # np.float64 is a float subclass
        return None if math.isnan(value) else value
    if isinstance(value, np.floating):
        v = float(value)
        return None if math.isnan(v) else v
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime.datetime)):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if value is pd.NaT or value is pd.NA:
        return None
    return value


# Columns stored as integer types in ``value_panel`` but potentially float in
# the DataFrame (because of nullable values).
_INT_COLUMNS = frozenset({"qidx", "cik"})


def _panel_rows(panel: pd.DataFrame, columns: tuple[str, ...]):
    """Yield COPY-ready tuples for ``panel`` in ``columns`` order."""
    ordered = panel[list(columns)]
    int_flags = tuple(c in _INT_COLUMNS for c in columns)
    for row in ordered.itertuples(index=False, name=None):
        yield tuple(_to_db(v, integer=i) for v, i in zip(row, int_flags))


def _build_risk(conn, db_url: str, truncate: bool) -> float:
    """(Re)build ``stock_risk_quarter``; returns the elapsed seconds."""
    with conn.cursor() as cur:
        if truncate:
            cur.execute("TRUNCATE stock_risk_quarter")
        cur.execute("SELECT count(DISTINCT ticker_yahoo) FROM price_daily")
        total_tickers = cur.fetchone()[0] or 0
    conn.commit()

    start = time.time()
    tickers_done = 0
    rows_written = 0
    skipped = 0

    write_conn = psycopg.connect(db_url)
    copy_sql = (
        "COPY stock_risk_quarter "
        "(ticker_yahoo, quarter, price_date, vol_252d, ret_252d, off_high_252d) "
        "FROM STDIN"
    )
    stream_conn = psycopg.connect(db_url)
    try:
        with write_conn.cursor() as wcur, wcur.copy(copy_sql) as cp:
            with stream_conn.cursor(name="risk_stream") as scur:
                scur.itersize = 100000
                scur.execute(
                    "SELECT ticker_yahoo, date, close FROM price_daily "
                    "WHERE close IS NOT NULL "
                    "ORDER BY ticker_yahoo, date"
                )
                cur_ticker = None
                buf_dates: list = []
                buf_closes: list = []

                def flush(ticker, dates, closes):
                    nonlocal rows_written, tickers_done, skipped
                    out = _rows_for_ticker(ticker, dates, closes)
                    for row in out:
                        cp.write_row(row)
                    rows_written += len(out)
                    tickers_done += 1
                    if not out:
                        skipped += 1
                    if tickers_done % BATCH_TICKERS == 0:
                        elapsed = time.time() - start
                        rate = tickers_done / elapsed if elapsed > 0 else 0.0
                        remaining = max(total_tickers - tickers_done, 0)
                        eta = remaining / rate if rate > 0 else float("nan")
                        print(
                            f"  {tickers_done}/{total_tickers} tickers, "
                            f"{rows_written} rows, {elapsed:.0f}s elapsed, "
                            f"ETA {eta:.0f}s",
                            flush=True,
                        )

                for ticker, date, close in scur:
                    if ticker != cur_ticker:
                        if cur_ticker is not None:
                            flush(cur_ticker, buf_dates, buf_closes)
                        cur_ticker = ticker
                        buf_dates = []
                        buf_closes = []
                    buf_dates.append(date)
                    buf_closes.append(float(close))

                if cur_ticker is not None:
                    flush(cur_ticker, buf_dates, buf_closes)
        write_conn.commit()
    finally:
        write_conn.close()
        stream_conn.close()

    elapsed = time.time() - start
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(DISTINCT ticker_yahoo) FROM stock_risk_quarter"
        )
        final_rows, final_tickers = cur.fetchone()

    print(
        f"done: {final_tickers} tickers, {final_rows} rows, "
        f"{skipped} skipped (no valid closes), {elapsed:.1f}s"
    )
    return elapsed


def _build_value_panel(conn, db_url: str, truncate: bool) -> tuple[int, float]:
    """Materialize ``build_features()`` output into ``value_panel``.

    Returns ``(rows_written, elapsed_seconds)``.  TRUNCATE and COPY share one
    transaction, committed at the end.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import backtest  # noqa: E402 - local module in this directory

    if truncate:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE value_panel")

    start = time.time()
    panel = backtest.compute_panel(db_url)

    columns = backtest.VALUE_PANEL_COLUMNS
    col_list = ", ".join(f'"{c}"' for c in columns)
    copy_sql = f"COPY value_panel ({col_list}) FROM STDIN"

    rows_written = 0
    with conn.cursor() as cur, cur.copy(copy_sql) as cp:
        for row in _panel_rows(panel, columns):
            cp.write_row(row)
            rows_written += 1
    conn.commit()

    return rows_written, time.time() - start


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Precompute stock_risk_quarter and value_panel"
    )
    parser.add_argument("--force", action="store_true", help="TRUNCATE and rebuild")
    args = parser.parse_args(argv)

    db_url = os.environ.get("DATABASE_URL", DEFAULT_DB_URL)

    conn = None
    try:
        conn = psycopg.connect(db_url)
        with conn.cursor() as cur:
            with open(_sql_path(), "r", encoding="utf-8") as fh:
                cur.execute(fh.read())
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM stock_risk_quarter")
            risk_existing = cur.fetchone()[0] or 0
            cur.execute("SELECT to_regclass('value_panel')")
            panel_exists = cur.fetchone()[0] is not None
            panel_existing = 0
            if panel_exists:
                cur.execute("SELECT count(*) FROM value_panel")
                panel_existing = cur.fetchone()[0] or 0

        if risk_existing and panel_existing and not args.force:
            print(
                f"stock_risk_quarter already has {risk_existing} rows and "
                f"value_panel already has {panel_existing} rows; "
                "pass --force to rebuild. Nothing to do."
            )
            return 0

        total_start = time.time()
        risk_seconds = 0.0
        panel_seconds = 0.0
        panel_rows: int | None = panel_existing

        if risk_existing and not args.force:
            print(
                f"stock_risk_quarter already has {risk_existing} rows; "
                "skipping rebuild (pass --force to rebuild)."
            )
        else:
            risk_seconds = _build_risk(conn, db_url, truncate=bool(risk_existing))

        if panel_existing and not args.force:
            print(
                f"value_panel already has {panel_existing} rows; "
                "skipping rebuild (pass --force to rebuild)."
            )
        else:
            panel_rows, panel_seconds = _build_value_panel(
                conn, db_url, truncate=bool(panel_existing)
            )

        total_seconds = time.time() - total_start
        print(
            f"timing: risk build {risk_seconds:.1f}s, "
            f"panel build {panel_seconds:.1f}s, "
            f"value_panel rows {panel_rows}, total {total_seconds:.1f}s"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - report and fail loudly
        print(f"ERROR: prepare failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
