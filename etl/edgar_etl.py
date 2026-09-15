"""EDGAR companyfacts -> PostgreSQL ETL.

Parses the SEC ``companyfacts.zip`` archive into fact tables, derives quarterly
EPS (including Q4 derivation from 10-K annual figures) and quarterly shares
outstanding, and loads everything via COPY.

Usage:
    python etl/edgar_etl.py [--zip PATH] [--ticker-map PATH] [--workers N]
                            [--ciks 320193,789019] [--reset]
                            [--log-file logs/edgar_etl.log]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import functools
import glob
import json
import multiprocessing as mp
import os
import re
import time
import zipfile

from common import (
    SEC_USER_AGENT,
    TICKER_MAP_PATH,
    TICKER_MAP_URL,
    chunked,
    copy_csv,
    download,
    env,
    get_conn,
    quarter_end,
    setup_logging,
    snap_quarter,
    upsert_csv,
)

# --------------------------------------------------------------------------- #
# Concept registry
# --------------------------------------------------------------------------- #
EPS_CONCEPTS = {
    "us-gaap:EarningsPerShareBasic": "USD/shares",
    "us-gaap:EarningsPerShareDiluted": "USD/shares",
}
# concept -> required unit
SHARE_CONCEPTS = {
    "dei:EntityCommonStockSharesOutstanding": "shares",
    "us-gaap:CommonStockSharesOutstanding": "shares",
    "us-gaap:CommonStockSharesIssued": "shares",
    "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic": "shares",
    "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding": "shares",
}
FLOAT_CONCEPTS = {"dei:EntityPublicFloat": "USD"}

EPS_COLS = [
    "cik", "concept", "unit", "period_start", "period_end", "duration_days",
    "is_instant", "value", "accn", "fy", "fp", "form", "filed", "frame",
]
SHARES_COLS = list(EPS_COLS)
FLOAT_COLS = [
    "cik", "concept", "period_start", "period_end", "is_instant", "value",
    "accn", "fy", "fp", "form", "filed",
]
COMPANY_COLS = ["cik", "entity_name"]
TICKER_COLS = ["cik", "ticker_edgar", "ticker_yahoo", "exchange"]

# worker shard key -> (filename stem, table, columns)
SHARD_SPECS = [
    ("eps", "eps_facts", "eps_facts", EPS_COLS),
    ("shares", "shares_facts", "shares_facts", SHARES_COLS),
    ("float", "public_float_facts", "public_float_facts", FLOAT_COLS),
    ("company", "company_names", "company", COMPANY_COLS),
]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _s(x) -> str:
    """CSV-safe string (None -> empty)."""
    if x is None:
        return ""
    return str(x)


def _num(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _norm_ticker(t: str) -> str:
    return re.sub(r"[./]", "-", (t or "").strip().upper())


def _entry_name(cik: int) -> str:
    return "CIK%010d.json" % int(cik)


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #
_W: dict = {}


def _init_worker(zip_path: str, shard_dir: str) -> None:
    _W["zip"] = zipfile.ZipFile(zip_path, "r")
    idx = mp.current_process()._identity[0]
    _W["idx"] = idx
    _W["min_pe"] = dt.date(1990, 1, 1)
    _W["max_pe"] = dt.date.today() + dt.timedelta(days=1)
    _W["files"] = {}
    for key, stem, _table, cols in SHARD_SPECS:
        path = os.path.join(shard_dir, "%s.part%02d.csv" % (stem, idx))
        fh = open(path, "w", newline="", encoding="utf-8")
        w = csv.writer(fh)
        w.writerow(cols)
        _W["files"][key] = (fh, w)


def _close_worker() -> None:
    for fh, _w in _W.get("files", {}).values():
        try:
            fh.flush()
            fh.close()
        except Exception:
            pass


# Sentinel that sorts after any real ISO date / accession string in the
# earliest-filed dedup, so missing filed/accn never win.
_RANK_MAX = "\uffff"


def _dedup_store(store: dict, key, rank, row) -> bool:
    """Keep the row with the smallest ``rank`` for ``key``.

    Dedup is by **earliest filed (as-originally-reported) wins**: later filings
    often restate prior periods on a split-adjusted basis, and mixing those bases
    would be inconsistent with the as-traded price / as-paid dividends.  Missing
    ``filed``/``accn`` are encoded as ``_RANK_MAX`` (+inf) so real values win.
    Returns True if the row was stored.
    """
    prev = store.get(key)
    if prev is None or rank < prev[0]:
        store[key] = (rank, row)
        return True
    return False


def _process_entry(entry: str) -> dict:
    counts = {"eps": 0, "shares": 0, "float": 0, "company": 0, "skipped": 0, "error": None}
    try:
        raw = _W["zip"].read(entry)
        obj = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - never let one bad file kill the run
        counts["error"] = "%s: %s" % (entry, exc)
        return counts

    cik = obj.get("cik")
    if cik is None:
        m = re.search(r"(\d+)", entry)
        cik = int(m.group(1)) if m else 0
    cik = int(cik)

    entity_name = obj.get("entityName")
    if entity_name:
        fh, w = _W["files"]["company"]
        w.writerow([cik, entity_name])
        counts["company"] = 1

    facts = obj.get("facts")
    if not isinstance(facts, dict):
        return counts

    best_eps: dict = {}
    best_shares: dict = {}
    best_float: dict = {}

    for ns in ("us-gaap", "dei"):
        nsd = facts.get(ns)
        if not isinstance(nsd, dict):
            continue
        for concept, body in nsd.items():
            full = "%s:%s" % (ns, concept)
            if full in EPS_CONCEPTS:
                kind, req_unit = "eps", EPS_CONCEPTS[full]
            elif full in SHARE_CONCEPTS:
                kind, req_unit = "shares", SHARE_CONCEPTS[full]
            elif full in FLOAT_CONCEPTS:
                kind, req_unit = "float", FLOAT_CONCEPTS[full]
            else:
                continue

            units = body.get("units") if isinstance(body, dict) else None
            if not isinstance(units, dict):
                continue
            arr = units.get(req_unit)
            if not isinstance(arr, list):
                continue

            for f in arr:
                if not isinstance(f, dict):
                    continue
                end = f.get("end")
                val = f.get("val")
                if not end or val is None:
                    continue
                try:
                    pe = dt.date.fromisoformat(end[:10])
                    start = f.get("start")
                    if start:
                        ps = dt.date.fromisoformat(start[:10])
                        is_instant = False
                        duration = (pe - ps).days
                    else:
                        ps = pe
                        is_instant = True
                        duration = None
                except Exception:
                    continue

                # sanity: drop absurd period ends (e.g. mis-typed 2207Q1 facts)
                if pe < _W["min_pe"] or pe > _W["max_pe"]:
                    counts["skipped"] += 1
                    continue

                filed = f.get("filed") or ""
                accn = f.get("accn") or ""
                fy = f.get("fy")
                fp = f.get("fp")
                form = f.get("form")
                frame = f.get("frame")
                # earliest filed (as originally reported) wins; None -> +inf so
                # a real filing always beats a missing filed/accn
                rank = (filed if filed else _RANK_MAX, accn if accn else _RANK_MAX)

                if kind == "eps":
                    row = [
                        cik, full, req_unit, ps.isoformat(), pe.isoformat(),
                        _s(duration), "true" if is_instant else "false",
                        _num(val), accn, _s(fy), _s(fp), _s(form), filed or None, _s(frame),
                    ]
                    key = (full, ps, pe, req_unit)
                    if _dedup_store(best_eps, key, rank, row):
                        counts["eps"] += 0  # counted after dedup below
                elif kind == "shares":
                    row = [
                        cik, full, req_unit, ps.isoformat(), pe.isoformat(),
                        _s(duration), "true" if is_instant else "false",
                        _num(val), accn, _s(fy), _s(fp), _s(form), filed or None, _s(frame),
                    ]
                    key = (full, ps, pe, req_unit)
                    _dedup_store(best_shares, key, rank, row)
                else:  # float
                    row = [
                        cik, full, ps.isoformat(), pe.isoformat(),
                        "true" if is_instant else "false",
                        _num(val), accn, _s(fy), _s(fp), _s(form), filed or None,
                    ]
                    key = (full, ps, pe)
                    _dedup_store(best_float, key, rank, row)

    for store, key in ((best_eps, "eps"), (best_shares, "shares"), (best_float, "float")):
        fh, w = _W["files"][key]
        for _rank, row in store.values():
            w.writerow(row)
            counts[key] += 1

    # flush buffers so a crash loses at most the current entry
    for fh, _w in _W["files"].values():
        fh.flush()
    return counts


# --------------------------------------------------------------------------- #
# Ticker map / company loading
# --------------------------------------------------------------------------- #
def load_ticker_map(path: str, logger) -> tuple[list, list]:
    with open(path, "r", encoding="utf-8") as fh:
        obj = json.load(fh)

    rows: list[list] = []
    companies: dict = {}
    fields = obj.get("fields") if isinstance(obj, dict) else None
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, list):
        raise ValueError("unexpected ticker map format in %s" % path)

    if fields:
        idx = {name: i for i, name in enumerate(fields)}
        ci, ni, ti, ei = idx.get("cik"), idx.get("name"), idx.get("ticker"), idx.get("exchange")
        for rec in data:
            if not isinstance(rec, (list, tuple)):
                continue
            try:
                cik = int(rec[ci])
            except Exception:
                continue
            name = rec[ni] if ni is not None else None
            ticker = rec[ti] if ti is not None else None
            exchange = rec[ei] if ei is not None else None
            _add_tm_row(rows, companies, cik, name, ticker, exchange)
    else:
        for rec in data:
            if not isinstance(rec, dict):
                continue
            try:
                cik = int(rec.get("cik"))
            except Exception:
                continue
            _add_tm_row(rows, companies, cik, rec.get("name"), rec.get("ticker"), rec.get("exchange"))

    # dedup by (cik, ticker_edgar)
    seen = set()
    deduped = []
    for r in rows:
        k = (r[0], r[1])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(r)

    company_rows = [[cik, name] for cik, name in companies.items()]
    logger.info("ticker map: %d rows, %d companies", len(deduped), len(company_rows))
    return deduped, company_rows


def _add_tm_row(rows, companies, cik, name, ticker, exchange) -> None:
    if name:
        companies.setdefault(cik, name)
    edgar = (ticker or "").strip().upper()
    yahoo = _norm_ticker(ticker)
    if not edgar or not yahoo or yahoo == "NONE-":
        return
    rows.append([cik, edgar, yahoo, exchange])


# --------------------------------------------------------------------------- #
# Derivation: eps_quarterly
# --------------------------------------------------------------------------- #
class Fact:
    __slots__ = ("ps", "pe", "dd", "instant", "value", "filed", "accn", "form")

    def __init__(self, ps, pe, dd, instant, value, filed, accn, form):
        self.ps, self.pe, self.dd, self.instant = ps, pe, dd, instant
        self.value, self.filed, self.accn, self.form = value, filed, accn, form


def read_eps_shards(shard_dir: str, logger) -> dict:
    by_cik: dict = {}
    n = 0
    for path in sorted(glob.glob(os.path.join(shard_dir, "eps_facts.part*.csv"))):
        with open(path, "r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                try:
                    cik = int(r["cik"])
                    ps = dt.date.fromisoformat(r["period_start"])
                    pe = dt.date.fromisoformat(r["period_end"])
                    dd = int(r["duration_days"]) if r["duration_days"] else None
                    instant = r["is_instant"] == "true"
                    value = float(r["value"])
                except Exception:
                    continue
                fact = Fact(ps, pe, dd, instant, value, r["filed"], r["accn"], r["form"])
                by_cik.setdefault(cik, {}).setdefault(r["concept"], []).append(fact)
                n += 1
    logger.info("read %d eps facts across %d ciks", n, len(by_cik))
    return by_cik


def _cmp_candidates(a, b):
    # period_end DESC (latest period wins the snapped quarter slot)
    if a["pe"] != b["pe"]:
        return -1 if a["pe"] > b["pe"] else 1
    # is_derived ASC (reported wins)
    if a["is_derived"] != b["is_derived"]:
        return 1 if a["is_derived"] else -1
    # diluted preferred
    ca = 0 if "Diluted" in a["concept"] else 1
    cb = 0 if "Diluted" in b["concept"] else 1
    if ca != cb:
        return ca - cb
    # filed DESC
    fa, fb = a["filed"] or "", b["filed"] or ""
    if fa != fb:
        return -1 if fa > fb else 1
    # accn DESC
    aa, ab = a["accn"] or "", b["accn"] or ""
    if aa != ab:
        return -1 if aa > ab else 1
    return 0


# A quarter is only usable if the value is finite and not a source artifact.
EPS_ABS_MAX = 1e5


def _derive_for_concept(quarterly: list, annuals: list, concept: str) -> list:
    """Derive Q4 candidates for a single concept.

    ``quarterly``/``annuals`` only ever contain facts of ``concept`` so a
    subtraction never mixes Basic and Diluted.
    """
    derived: list[dict] = []
    for ann in annuals:
        S, E = ann.ps, ann.pe
        if any(q.pe == E for q in quarterly):
            continue  # reported Q4 for this concept already covers the period
        cand = sorted([q for q in quarterly if q.ps >= S and q.pe <= E], key=lambda x: x.ps)
        if len(cand) != 3:
            continue
        if cand[0].ps != S:
            continue
        if not (cand[0].pe < cand[1].pe < cand[2].pe):
            continue
        if not (1 <= (cand[1].ps - cand[0].pe).days <= 7 and 1 <= (cand[2].ps - cand[1].pe).days <= 7):
            continue
        if not (cand[2].pe < E):
            continue
        if not (80 <= (E - cand[2].pe).days + 1 <= 130):
            continue
        derived.append({
            "concept": concept,
            "is_derived": True,
            "ps": cand[2].pe + dt.timedelta(days=1),
            "pe": E,
            "value": ann.value - (cand[0].value + cand[1].value + cand[2].value),
            "filed": ann.filed,
            "accn": ann.accn,
        })
    return derived


def derive_eps_quarters(by_cik: dict, logger, collision_path: str) -> tuple[list, int, int]:
    rows: list[list] = []
    n_reported = n_derived = n_dropped = n_dropped_collisions = 0
    collision_fh = open(collision_path, "w", newline="", encoding="utf-8")
    cw = csv.writer(collision_fh)
    cw.writerow([
        "cik", "quarter", "n_candidates", "chosen_concept", "chosen_is_derived",
        "chosen_period_end", "chosen_filed", "chosen_accn", "chosen_eps", "all_candidates",
    ])

    basic_c = "us-gaap:EarningsPerShareBasic"
    diluted_c = "us-gaap:EarningsPerShareDiluted"
    # order matters: Basic first, then Diluted so Diluted wins ties per period
    concept_order = [basic_c, diluted_c]

    for cik, concepts in by_cik.items():
        quarterly_by_c: dict = {}
        annuals_by_c: dict = {}
        for c in concept_order:
            facts = concepts.get(c)
            if not facts:
                continue
            quarterly_by_c[c] = [
                f for f in facts if (not f.instant and f.dd is not None and 75 <= f.dd <= 115)
            ]
            annuals_by_c[c] = [
                f for f in facts
                if (not f.instant and f.dd is not None and 340 <= f.dd <= 385
                    and (f.form or "").startswith("10-K"))
            ]

        if not quarterly_by_c and not annuals_by_c:
            continue

        candidates: list[dict] = []

        # --- reported: choose the concept PER PERIOD (Diluted preferred) ---- #
        period_best: dict = {}
        for c in concept_order:
            for q in quarterly_by_c.get(c, []):
                period_best[(q.ps, q.pe)] = (c, q)  # Diluted overwrites Basic
        for c, q in period_best.values():
            candidates.append({
                "concept": c, "is_derived": False, "ps": q.ps, "pe": q.pe,
                "value": q.value, "filed": q.filed, "accn": q.accn,
            })

        # --- derived Q4: run separately per concept (no cross-concept math) - #
        for c in concept_order:
            candidates.extend(_derive_for_concept(
                quarterly_by_c.get(c, []), annuals_by_c.get(c, []), c
            ))

        # snap 52/53-week period ends that fall in the first days of a quarter
        groups: dict = {}
        for cand in candidates:
            groups.setdefault(snap_quarter(cand["pe"]), []).append(cand)

        for quarter, cands in groups.items():
            best = sorted(cands, key=functools.cmp_to_key(_cmp_candidates))[0]
            # exclude source artifacts / absurd EPS values from the derived table
            try:
                if abs(float(best["value"])) > EPS_ABS_MAX:
                    n_dropped += 1
                    if len(cands) > 1:
                        n_dropped_collisions += 1
                    continue
            except Exception:
                n_dropped += 1
                if len(cands) > 1:
                    n_dropped_collisions += 1
                continue
            # only log collisions whose winner actually reaches the DB, so the
            # audit file's chosen_* fields always match the stored row
            if len(cands) > 1:
                cw.writerow([
                    cik, quarter, len(cands), best["concept"], best["is_derived"],
                    best["pe"].isoformat(), best["filed"] or "", best["accn"] or "",
                    best["value"],
                    " | ".join(
                        "%s%s@%s=%s" % (
                            c["concept"].split(":")[-1],
                            "D" if c["is_derived"] else "R",
                            c["pe"].isoformat(), c["value"],
                        )
                        for c in cands
                    ),
                ])
            rows.append([
                cik, quarter, best["value"], best["concept"],
                "true" if best["is_derived"] else "false",
                best["ps"].isoformat(), best["pe"].isoformat(),
                best["filed"] or None, best["accn"] or None,
            ])
            if best["is_derived"]:
                n_derived += 1
            else:
                n_reported += 1

    collision_fh.close()
    logger.info(
        "eps_quarterly: %d rows (%d reported, %d derived), dropped %d |eps|>%g rows "
        "(%d were collision groups)",
        len(rows), n_reported, n_derived, n_dropped, EPS_ABS_MAX, n_dropped_collisions,
    )
    return rows, n_reported, n_derived


# --------------------------------------------------------------------------- #
# Derivation: shares_quarterly
# --------------------------------------------------------------------------- #
def read_shares_shards(shard_dir: str, logger) -> dict:
    by_cik: dict = {}
    n = 0
    for path in sorted(glob.glob(os.path.join(shard_dir, "shares_facts.part*.csv"))):
        with open(path, "r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                try:
                    cik = int(r["cik"])
                    ps = dt.date.fromisoformat(r["period_start"])
                    pe = dt.date.fromisoformat(r["period_end"])
                    instant = r["is_instant"] == "true"
                    value = int(round(float(r["value"])))
                except Exception:
                    continue
                by_cik.setdefault(cik, []).append(
                    (r["concept"], ps, pe, instant, value, r["filed"])
                )
                n += 1
    logger.info("read %d shares facts across %d ciks", n, len(by_cik))
    return by_cik


MAX_SHARES = 10 ** 13


def derive_shares_quarters(by_cik: dict, eps_rows: list, logger) -> list:
    # candidate quarters per cik (eps quarters are already snapped; snap shares ends too)
    cand_q: dict = {}
    for row in eps_rows:
        cand_q.setdefault(int(row[0]), set()).add(row[1])
    for cik, facts in by_cik.items():
        s = cand_q.setdefault(cik, set())
        for _concept, _ps, pe, _inst, _val, _filed in facts:
            s.add(snap_quarter(pe))

    dei = "dei:EntityCommonStockSharesOutstanding"
    cs = "us-gaap:CommonStockSharesOutstanding"
    rows: list[list] = []
    tier_counts = {1: 0, 2: 0, 3: 0, "skip": 0}

    def valid(f) -> bool:
        try:
            v = f[4]
            return v is not None and 0 < v <= MAX_SHARES
        except Exception:
            return False

    for cik, quarters in cand_q.items():
        facts = by_cik.get(cik)
        if not facts:
            continue
        dei_f = [f for f in facts if f[0] == dei and f[3]]
        cs_f = [f for f in facts if f[0] == cs and f[3]]
        # pre-sort once per cik (independent of E)
        dei_asc = sorted(dei_f, key=lambda f: (f[2].toordinal(), _neg_str(f[5] or "")))
        dei_desc = sorted(dei_f, key=lambda f: (-f[2].toordinal(), _neg_str(f[5] or "")))

        for q in sorted(quarters):
            E = quarter_end(q)
            chosen = None
            tier = "skip"

            # Tier 1: dei in [E, E+75d], earliest as_of, tie latest filed
            for f in dei_asc:
                if E <= f[2] <= E + dt.timedelta(days=75) and valid(f):
                    chosen = (f[4], dei, f[2], f[5])
                    tier = 1
                    break

            # Tier 2: dei in [E-45d, E), latest as_of, tie latest filed
            if chosen is None:
                for f in dei_desc:
                    if E - dt.timedelta(days=45) <= f[2] < E and valid(f):
                        chosen = (f[4], dei, f[2], f[5])
                        tier = 2
                        break

            # Tier 3: us-gaap nearest within 120d, prefer as_of >= E, tie latest filed
            if chosen is None:
                cs_sorted = sorted(
                    cs_f,
                    key=lambda f: (
                        abs((f[2] - E).days),
                        0 if f[2] >= E else 1,
                        _neg_str(f[5] or ""),
                    ),
                )
                for f in cs_sorted:
                    if abs((f[2] - E).days) <= 120 and valid(f):
                        chosen = (f[4], cs, f[2], f[5])
                        tier = 3
                        break

            if chosen is None:
                tier_counts["skip"] += 1
                continue

            tier_counts[tier] += 1
            rows.append([cik, q, chosen[0], chosen[1], chosen[2].isoformat(), chosen[3] or None])

    logger.info(
        "shares_quarterly: %d rows; tier1=%d tier2=%d tier3=%d skipped_quarters=%d",
        len(rows), tier_counts[1], tier_counts[2], tier_counts[3], tier_counts["skip"],
    )
    return rows


def _neg_str(s: str) -> str:
    """Sort helper: produce a key that sorts strings in reverse under ascending sort."""
    return "".join(chr(0x10FFFF - ord(ch)) for ch in s)


# --------------------------------------------------------------------------- #
# CSV writers
# --------------------------------------------------------------------------- #
def write_rows(path: str, cols: list, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow(r)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="EDGAR companyfacts -> PostgreSQL ETL")
    p.add_argument("--zip", default=env("EDGAR_ZIP", "data/companyfacts.zip"))
    p.add_argument("--ticker-map", default=TICKER_MAP_PATH)
    p.add_argument("--workers", type=int, default=int(env("EDGAR_WORKERS", "10")))
    p.add_argument("--ciks", default="", help="comma separated CIK list (smoke test filter)")
    p.add_argument("--reset", action="store_true", help="TRUNCATE tables before loading")
    p.add_argument("--log-file", default="logs/edgar_etl.log")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logger = setup_logging("edgar_etl", args.log_file)
    t0 = time.time()
    logger.info("starting EDGAR ETL zip=%s workers=%d reset=%s", args.zip, args.workers, args.reset)

    shard_dir = "data/csv_edgar"
    meta_dir = "data/csv_meta"
    os.makedirs(shard_dir, exist_ok=True)
    os.makedirs(meta_dir, exist_ok=True)

    # --- ticker map -------------------------------------------------------- #
    if not os.path.exists(args.ticker_map) or os.path.getsize(args.ticker_map) == 0:
        download(TICKER_MAP_URL, args.ticker_map, SEC_USER_AGENT, logger)
    tm_rows, company_rows = load_ticker_map(args.ticker_map, logger)
    tm_path = os.path.join(meta_dir, "ticker_map.csv")
    company_path = os.path.join(meta_dir, "company_map.csv")
    write_rows(tm_path, TICKER_COLS, tm_rows)
    write_rows(company_path, COMPANY_COLS, company_rows)

    conn = get_conn()

    if args.reset:
        logger.info("truncating tables (--reset)")
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE company, ticker_map, eps_facts, shares_facts, "
                "public_float_facts, eps_quarterly, shares_quarterly"
            )
        conn.commit()

    upsert_csv(conn, "ticker_map", TICKER_COLS, tm_path)
    upsert_csv(conn, "company", COMPANY_COLS, company_path)

    # --- clean worker shards ---------------------------------------------- #
    for path in glob.glob(os.path.join(shard_dir, "*.part*.csv")):
        os.remove(path)

    # --- entries to parse -------------------------------------------------- #
    cik_filter = None
    if args.ciks:
        cik_filter = {int(x) for x in args.ciks.split(",") if x.strip()}

    with zipfile.ZipFile(args.zip, "r") as zf:
        entries = sorted(zf.namelist())
    if cik_filter is not None:
        wanted = {_entry_name(c) for c in cik_filter}
        entries = [e for e in entries if e in wanted]
    logger.info("parsing %d entries", len(entries))

    # --- parallel parse ---------------------------------------------------- #
    totals = {"eps": 0, "shares": 0, "float": 0, "company": 0, "skipped": 0}
    errors = 0
    done = 0
    n_workers = max(1, args.workers)
    if entries:
        with mp.Pool(
            processes=n_workers,
            initializer=_init_worker,
            initargs=(args.zip, shard_dir),
        ) as pool:
            for item in pool.imap_unordered(_process_entry, entries, chunksize=8):
                done += 1
                if item.get("error"):
                    errors += 1
                    logger.warning("parse error: %s", item["error"])
                for k in totals:
                    totals[k] += item.get(k, 0)
                if done % 500 == 0 or done == len(entries):
                    logger.info(
                        "progress %d/%d files, eps=%d shares=%d float=%d skipped=%d elapsed=%.1fs",
                        done, len(entries), totals["eps"], totals["shares"],
                        totals["float"], totals["skipped"], time.time() - t0,
                    )
    logger.info("parse complete: %s errors=%d", totals, errors)

    # --- load fact shards -------------------------------------------------- #
    for key, table, _stem, cols in SHARD_SPECS:
        if key == "company":
            continue
        files = sorted(glob.glob(os.path.join(shard_dir, "%s.part*.csv" % _stem)))
        for path in files:
            copy_csv(conn, table, cols, path)
        logger.info("loaded %s from %d shards", table, len(files))

    # company names seen during parse (fill CIKs missing from ticker map)
    company_shards = sorted(glob.glob(os.path.join(shard_dir, "company_names.part*.csv")))
    if company_shards:
        merged = os.path.join(meta_dir, "company_names_all.csv")
        with open(merged, "w", newline="", encoding="utf-8") as out:
            w = csv.writer(out)
            w.writerow(COMPANY_COLS)
            for path in company_shards:
                with open(path, "r", newline="", encoding="utf-8") as fh:
                    reader = csv.reader(fh)
                    next(reader, None)
                    for r in reader:
                        if r:
                            w.writerow(r)
        upsert_csv(conn, "company", COMPANY_COLS, merged)
        logger.info("upserted company names from %d shards", len(company_shards))

    # --- derive eps_quarterly --------------------------------------------- #
    by_cik = read_eps_shards(shard_dir, logger)
    eps_rows, n_reported, n_derived = derive_eps_quarters(
        by_cik, logger, "data/collisions.csv"
    )
    eps_path = os.path.join(meta_dir, "eps_quarterly.csv")
    write_rows(eps_path, ["cik", "quarter", "eps", "concept", "is_derived",
                          "period_start", "period_end", "filed", "accn"], eps_rows)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE eps_quarterly")
    conn.commit()
    copy_csv(conn, "eps_quarterly",
             ["cik", "quarter", "eps", "concept", "is_derived", "period_start",
              "period_end", "filed", "accn"], eps_path)

    # --- derive shares_quarterly ------------------------------------------ #
    shares_by_cik = read_shares_shards(shard_dir, logger)
    shares_rows = derive_shares_quarters(shares_by_cik, eps_rows, logger)
    shares_path = os.path.join(meta_dir, "shares_quarterly.csv")
    write_rows(shares_path,
               ["cik", "quarter", "shares", "source_concept", "as_of", "filed"], shares_rows)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE shares_quarterly")
    conn.commit()
    copy_csv(conn, "shares_quarterly",
             ["cik", "quarter", "shares", "source_concept", "as_of", "filed"], shares_path)

    # --- summary ----------------------------------------------------------- #
    with conn.cursor() as cur:
        def scalar(sql):
            cur.execute(sql)
            return cur.fetchone()[0]

        counts = {
            "company": scalar("SELECT count(*) FROM company"),
            "ticker_map": scalar("SELECT count(*) FROM ticker_map"),
            "eps_facts": scalar("SELECT count(*) FROM eps_facts"),
            "shares_facts": scalar("SELECT count(*) FROM shares_facts"),
            "public_float_facts": scalar("SELECT count(*) FROM public_float_facts"),
            "eps_quarterly": scalar("SELECT count(*) FROM eps_quarterly"),
            "shares_quarterly": scalar("SELECT count(*) FROM shares_quarterly"),
        }
        eps_ciks = scalar("SELECT count(DISTINCT cik) FROM eps_quarterly")
        shares_ciks = scalar("SELECT count(DISTINCT cik) FROM shares_quarterly")
        eps_basic = scalar("SELECT count(*) FROM eps_quarterly WHERE concept='us-gaap:EarningsPerShareBasic'")
        eps_bad = scalar("SELECT count(*) FROM eps_quarterly WHERE abs(eps) > 1e5")
        shares_bad = scalar("SELECT count(*) FROM shares_quarterly WHERE shares <= 0 OR shares > 1e13")

    conn.close()
    elapsed = time.time() - t0
    logger.info("=" * 70)
    logger.info("EDGAR ETL summary (%.1fs)", elapsed)
    logger.info("  files parsed      : %d (errors %d)", done, errors)
    logger.info("  facts skipped     : %d (period_end out of [1990-01-01, today+1d])", totals["skipped"])
    logger.info("  eps_facts         : %d", counts["eps_facts"])
    logger.info("  shares_facts      : %d", counts["shares_facts"])
    logger.info("  public_float_facts: %d", counts["public_float_facts"])
    logger.info("  eps_quarterly     : %d (%d reported, %d derived) covering %d CIKs",
                counts["eps_quarterly"], n_reported, n_derived, eps_ciks)
    logger.info("  eps_quarterly basic-concept rows: %d; |eps|>1e5 rows: %d", eps_basic, eps_bad)
    logger.info("  shares_quarterly  : %d covering %d CIKs; out-of-range rows: %d",
                counts["shares_quarterly"], shares_ciks, shares_bad)
    logger.info("  company / tm rows : %d / %d", counts["company"], counts["ticker_map"])
    logger.info("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
