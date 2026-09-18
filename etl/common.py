"""Shared helpers for the ValInvest EDGAR/Yahoo -> PostgreSQL ETL.

All database writes go through COPY (see ``copy_csv``) except for small
``INSERT ... ON CONFLICT`` upserts into staging tables.
"""
from __future__ import annotations

import datetime as dt
import functools
import logging
import os
import sys
import urllib.request
from typing import Iterable, Iterator


# --------------------------------------------------------------------------- #
# Environment / config
# --------------------------------------------------------------------------- #
def env(name: str, default=None):
    """Read an environment variable, treating empty strings as unset."""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


DATABASE_URL = env("DATABASE_URL", "postgresql://valinvest:valinvest@db:5432/valinvest")
SEC_USER_AGENT = env("SEC_USER_AGENT", "ValInvest Research research@example.com")
TICKER_MAP_URL = env("TICKER_MAP_URL", "https://www.sec.gov/files/company_tickers_exchange.json")
TICKER_MAP_PATH = env("TICKER_MAP_PATH", "data/company_tickers_exchange.json")

EXCLUDED_TICKERS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "excluded_tickers.txt"
)


@functools.lru_cache(maxsize=1)
def excluded_tickers() -> frozenset[str]:
    """Tickers removed from the dataset (corrupt vendor price series).

    See ``etl/excluded_tickers.txt`` for the rule and the current list.
    """
    try:
        with open(EXCLUDED_TICKERS_PATH, "r", encoding="utf-8") as fh:
            return frozenset(
                line.strip().upper()
                for line in fh
                if line.strip() and not line.lstrip().startswith("#")
            )
    except OSError:
        return frozenset()


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def get_conn():
    """Open a psycopg3 connection using DATABASE_URL."""
    import psycopg

    return psycopg.connect(DATABASE_URL)


def copy_csv(conn, table: str, columns, path: str, header: bool = True) -> int:
    """COPY a CSV file into ``table`` via STDIN.

    ``columns`` is the ordered list of destination column names and must match
    the CSV column order.  Returns the number of bytes streamed.
    """
    cols = ", ".join('"%s"' % c for c in columns)
    sql = (
        "COPY %s (%s) FROM STDIN WITH (FORMAT csv, HEADER %s)"
        % (table, cols, "true" if header else "false")
    )
    written = 0
    with conn.cursor() as cur:
        with cur.copy(sql) as cp:
            with open(path, "rb") as fh:
                while True:
                    block = fh.read(1 << 20)
                    if not block:
                        break
                    cp.write(block)
                    written += len(block)
    conn.commit()
    return written


def upsert_csv(conn, table: str, columns, path: str, conflict_cols=None, header: bool = True) -> int:
    """COPY a CSV into a TEMP stage table then INSERT ... ON CONFLICT DO NOTHING.

    Used for the small dimension tables (company / ticker_map) so repeated runs
    are idempotent.  ``conflict_cols`` is informational; the target table's own
    conflict target is used.
    """
    cols = ", ".join('"%s"' % c for c in columns)
    stage = "stage_%s" % table
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS pg_temp.%s" % stage)
        cur.execute("CREATE TEMP TABLE %s (LIKE %s INCLUDING DEFAULTS) ON COMMIT DROP" % (stage, table))
        copy_sql = (
            "COPY %s (%s) FROM STDIN WITH (FORMAT csv, HEADER %s)"
            % (stage, cols, "true" if header else "false")
        )
        with cur.copy(copy_sql) as cp:
            with open(path, "rb") as fh:
                while True:
                    block = fh.read(1 << 20)
                    if not block:
                        break
                    cp.write(block)
        cur.execute(
            "INSERT INTO %s SELECT * FROM %s ON CONFLICT DO NOTHING" % (table, stage)
        )
        inserted = cur.rowcount
    conn.commit()
    return inserted


# --------------------------------------------------------------------------- #
# Dates / misc
# --------------------------------------------------------------------------- #
def quarter_label(d) -> str:
    """Return a calendar-quarter label like ``2024Q1`` for a date/datetime/ISO str."""
    if isinstance(d, str):
        d = dt.date.fromisoformat(d[:10])
    elif isinstance(d, dt.datetime):
        d = d.date()
    return "%dQ%d" % (d.year, (d.month - 1) // 3 + 1)


def quarter_end(label: str) -> dt.date:
    """Return the last calendar day of a ``YYYYQn`` label."""
    year = int(label[:4])
    q = int(label[-1])
    month = q * 3
    if q == 4:
        nxt = dt.date(year + 1, 1, 1)
    else:
        nxt = dt.date(year, month + 1, 1)
    return nxt - dt.timedelta(days=1)


def snap_quarter(d) -> str:
    """Quarter label with a 7-day snap-back for 52/53-week fiscal period ends.

    Quarterly period ends that fall in the first 7 days of a calendar quarter
    are attributed to the *previous* calendar quarter (e.g. 2023-07-01 ->
    ``2023Q2``).  52/53-week filers (Apple, Costco, ...) close their quarters on
    the nearest Saturday and a period ending July 1 is their fiscal Q3, not the
    calendar Q3.  Without the snap, the fiscal period would steal the calendar
    Q3 slot from the derived Jul-Sep quarter.

    Use this for EDGAR-derived quarter labels (``eps_quarterly`` /
    ``shares_quarterly``); keep plain :func:`quarter_label` for Yahoo market
    data, which is real calendar data.
    """
    if isinstance(d, str):
        d = dt.date.fromisoformat(d[:10])
    elif isinstance(d, dt.datetime):
        d = d.date()
    if d.day <= 7:
        d = d - dt.timedelta(days=7)
    return quarter_label(d)


def setup_logging(name: str, logfile: str | None = None, level=logging.INFO) -> logging.Logger:
    """Configure a logger that writes to stdout and optionally a file."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(process)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if logfile:
        parent = os.path.dirname(logfile)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    logger.propagate = False
    return logger


def download(url: str, path: str, user_agent: str = SEC_USER_AGENT, logger=None) -> str:
    """Download ``url`` to ``path`` atomically if it is not already present."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if logger:
        logger.info("downloading %s -> %s", url, path)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Encoding": "identity",
        },
    )
    tmp = path + ".tmp"
    with urllib.request.urlopen(req, timeout=180) as resp:
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        data = resp.read()
    if encoding == "gzip" or data[:2] == b"\x1f\x8b":
        import gzip

        data = gzip.decompress(data)
    with open(tmp, "wb") as out:
        out.write(data)
    os.replace(tmp, path)
    return path


def chunked(iterable: Iterable, n: int) -> Iterator[list]:
    """Yield lists of at most ``n`` items from ``iterable``."""
    chunk: list = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= n:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
