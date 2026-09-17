"""Pure data/logic layer for the ValInvest value-investing backtest.

This module has no Streamlit (or other UI) imports so it can be unit-tested and
reused headlessly.

Data-basis rules (critical):
  * ``price_quarterly.close_raw`` is the *split-adjusted* quarter-end close and
    ``dividend.amount`` is the *split-adjusted* dividend.  The as-traded view
    columns (``price`` / ``Dividends``) are NEVER used for return math because
    they mix split bases (AAPL 2020Q2->Q3 would show -68% instead of +27%).
  * EPS is stored as originally reported, so it is normalized to the present
    split-adjusted basis using the filing date:
        F_after(d) = product of stock_split.ratio where split_date > d
        eps_present = eps / F_after(filed or period_end)
  * View share counts are already normalized to their own quarter's price_date
    basis, so ``shares_present = shares * F_after(price_date)``.

Selection is lagged by one quarter: at quarter ``t`` the trailing four-quarter
EPS uses ``eps_present`` over ``t-4 .. t-1`` and shares use the last count known
as of ``t-1``.  This is enforced by the ``shift(1)`` / ``ffill(limit=4).shift(1)``
built in :func:`build_features`, so ``select_holdings`` never sees the future.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

DEFAULT_DB_URL = "postgresql://valinvest:valinvest@db:5432/valinvest"

# Exact output column order of :func:`build_features`.  Frozen here so that
# ``prepare.py`` can materialize the panel into ``value_panel`` and the tests can
# assert the schema has not drifted.
VALUE_PANEL_COLUMNS: tuple[str, ...] = (
    "qidx",
    "quarter",
    "ticker",
    "price_traded",
    "shares",
    "eps",
    "eps_filed",
    "eps_period_end",
    "eps_is_derived",
    "px_adj",
    "price_date",
    "div_adj",
    "vol_252d",
    "ret_252d",
    "off_high_252d",
    "cik",
    "name",
    "eps_present",
    "shares_present",
    "ttm_eps",
    "eps4_min",
    "ttm_div",
    "shares_lag",
    "pe",
    "mktcap",
    "div_yield",
)

# --------------------------------------------------------------------------- #
# Quarter helpers
# --------------------------------------------------------------------------- #


def qidx(quarter: str) -> int:
    """``'2010Q3'`` -> ``2010*4 + 2``."""
    year, q = quarter.upper().split("Q")
    return int(year) * 4 + (int(q) - 1)


def quarter_from_idx(idx: int) -> str:
    """``2010*4 + 2`` -> ``'2010Q3'``."""
    idx = int(idx)
    return f"{idx // 4}Q{idx % 4 + 1}"


def last_complete_quarter(today: datetime.date | None = None) -> str:
    """Return the most recent fully completed calendar quarter.

    The current calendar quarter is partial, so this is the current quarter
    minus one quarter.
    """
    if today is None:
        today = datetime.date.today()
    current_q = (today.month - 1) // 3 + 1
    return quarter_from_idx(today.year * 4 + (current_q - 1) - 1)


def _quarter_end(idx: int) -> pd.Timestamp:
    year = idx // 4
    month = 3 * (idx % 4 + 1)
    return pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)


# --------------------------------------------------------------------------- #
# Database loading
# --------------------------------------------------------------------------- #

PANEL_SQL = """
WITH tk AS (
  SELECT ticker_yahoo, MIN(cik) AS cik
  FROM ticker_map
  GROUP BY ticker_yahoo
),
div_agg AS (
  SELECT ticker_yahoo, quarter, SUM(amount) AS div_adj
  FROM dividend
  GROUP BY ticker_yahoo, quarter
)
SELECT qf."quarter"                                       AS quarter,
       qf."ticker symbol"                                 AS ticker,
       qf."price"::double precision                        AS price_traded,
       qf."shares outstanding"::double precision           AS shares,
       qf."EPS"::double precision                          AS eps,
       e.filed                                            AS eps_filed,
       e.period_end                                       AS eps_period_end,
       e.is_derived                                       AS eps_is_derived,
       pq.close_raw::double precision                     AS px_adj,
       pq.price_date                                      AS price_date,
       d.div_adj::double precision                        AS div_adj,
       r.vol_252d, r.ret_252d, r.off_high_252d,
       tk.cik                                             AS cik,
       c.entity_name                                      AS name
FROM quarterly_fundamentals qf
LEFT JOIN tk   ON tk.ticker_yahoo = qf."ticker symbol"
LEFT JOIN eps_quarterly e
       ON e.cik = tk.cik AND e.quarter = qf."quarter"
LEFT JOIN price_quarterly pq
       ON pq.ticker_yahoo = qf."ticker symbol" AND pq.quarter = qf."quarter"
LEFT JOIN div_agg d
       ON d.ticker_yahoo = qf."ticker symbol" AND d.quarter = qf."quarter"
LEFT JOIN stock_risk_quarter r
       ON r.ticker_yahoo = qf."ticker symbol" AND r.quarter = qf."quarter"
LEFT JOIN company c ON c.cik = tk.cik
"""


def compute_panel(db_url: str = DEFAULT_DB_URL) -> pd.DataFrame:
    """Fetch the raw quarterly panel and run :func:`build_features`.

    All quarters are fetched, including the current partial one; ``load_panel``
    (and ``run_backtest``) are responsible for trimming to the last complete
    calendar quarter.
    """
    import psycopg  # imported lazily so the pure-logic tests need no DB

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(PANEL_SQL)
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()
        with conn.cursor() as cur:
            cur.execute("SELECT ticker_yahoo, split_date, ratio FROM stock_split")
            split_cols = [d.name for d in cur.description]
            split_rows = cur.fetchall()

    raw = pd.DataFrame(rows, columns=cols)
    splits = pd.DataFrame(split_rows, columns=split_cols)
    return build_features(raw, splits)


def _coerce_panel_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize the dtypes read back from ``value_panel``.

    Dates stored as ``DATE`` come back as ``datetime.date`` objects just like the
    raw fetch path, so the conversions below are intentionally no-ops in the
    common case; they only guard against a driver returning a wider type.
    """
    if "qidx" in df.columns:
        df["qidx"] = df["qidx"].astype("int64")
    if "eps_is_derived" in df.columns:
        col = df["eps_is_derived"]
        if not col.isna().any():
            df["eps_is_derived"] = col.astype(bool)
    for col in ("eps_filed", "eps_period_end", "price_date"):
        if col in df.columns and not pd.api.types.is_object_dtype(df[col]):
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    for col in ("ticker", "quarter"):
        if col in df.columns:
            df[col] = df[col].astype(str)
    return df


def _load_value_panel(db_url: str) -> pd.DataFrame | None:
    """Read the materialized panel, or return ``None`` when it is absent/empty."""
    import psycopg

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('value_panel')")
            if cur.fetchone()[0] is None:
                return None
            cur.execute("SELECT count(*) FROM value_panel")
            if not cur.fetchone()[0]:
                return None
            cols = ", ".join(f'"{c}"' for c in VALUE_PANEL_COLUMNS)
            cur.execute(f"SELECT {cols} FROM value_panel ORDER BY qidx, ticker")
            result_cols = [d.name for d in cur.description]
            rows = cur.fetchall()

    df = pd.DataFrame(rows, columns=result_cols)
    df = _coerce_panel_dtypes(df)
    cutoff = qidx(last_complete_quarter())
    return df.loc[df["qidx"] <= cutoff].reset_index(drop=True)


def load_panel(
    db_url: str = DEFAULT_DB_URL, force_compute: bool = False
) -> pd.DataFrame:
    """Load the full quarterly panel.

    Fast path: read the precomputed ``value_panel`` table (populated by
    ``dashboard/prepare.py``), filtered to the last complete calendar quarter.
    If the table is missing or empty -- or ``force_compute`` is set -- fall back
    to :func:`compute_panel` (raw SQL fetch + :func:`build_features`).
    """
    if not force_compute:
        panel = _load_value_panel(db_url)
        if panel is not None:
            return panel
    return compute_panel(db_url)


# --------------------------------------------------------------------------- #
# Feature engineering
# --------------------------------------------------------------------------- #


def _split_info(splits: pd.DataFrame | None) -> dict:
    """Map ``ticker -> (sorted split dates, log cumulative ratios)``."""
    info: dict = {}
    if splits is None or len(splits) == 0:
        return info
    sp = splits
    for ticker, g in sp.groupby("ticker_yahoo", sort=False):
        dates = pd.to_datetime(g["split_date"]).values.astype("datetime64[D]")
        ratios = g["ratio"].astype(float).values
        mask = (~np.isnat(dates)) & (ratios > 0)
        dates = dates[mask]
        ratios = ratios[mask]
        if len(dates) == 0:
            continue
        order = np.argsort(dates)
        dates = dates[order]
        logr = np.log(ratios[order])
        cum = np.concatenate(([0.0], np.cumsum(logr)))
        info[ticker] = (dates, cum)
    return info


def _factor_after(info: dict, ticker: str, dates: np.ndarray) -> np.ndarray:
    """``F_after(d)`` for an array of ``datetime64[D]`` dates (``split > d``)."""
    n = len(dates)
    out = np.ones(n, dtype="float64")
    if ticker not in info:
        return out
    split_dates, cum = info[ticker]
    valid = ~np.isnat(dates)
    if valid.any() and len(split_dates):
        idx = np.searchsorted(split_dates, dates[valid], side="right")
        out[valid] = np.exp(cum[-1] - cum[idx])
    return out


def build_features(raw: pd.DataFrame, splits: pd.DataFrame) -> pd.DataFrame:
    """Normalize EPS/shares to the present split basis and add lagged features.

    Adds ``eps_present``, ``shares_present``, ``ttm_eps``, ``eps4_min``,
    ``ttm_div``, ``shares_lag``, ``pe``, ``mktcap``, ``div_yield`` and a dense
    ``qidx`` column.  Each ticker is reindexed over its contiguous ``qidx``
    range so rolling windows see calendar quarters (missing quarters are NaN).
    """
    raw = raw.copy()
    info = _split_info(splits)
    frames = []

    for ticker, g in raw.groupby("ticker", sort=False):
        g = g.drop_duplicates(subset=["quarter"]).copy()
        g["qidx"] = g["quarter"].map(qidx)
        g = g.sort_values("qidx")
        lo, hi = int(g["qidx"].min()), int(g["qidx"].max())
        idx = pd.RangeIndex(lo, hi + 1)

        g = g.set_index("qidx").reindex(idx)
        g.index.name = "qidx"
        g["ticker"] = ticker
        g["quarter"] = [quarter_from_idx(i) for i in idx]
        for col in ("cik", "name"):
            if col in g.columns:
                g[col] = g[col].ffill().bfill()

        def _raw(col: str) -> pd.Series:
            if col in g.columns:
                return g[col]
            return pd.Series(np.nan, index=g.index)

        def _series(col: str) -> pd.Series:
            return pd.to_numeric(_raw(col), errors="coerce").astype("float64")

        filed = pd.to_datetime(_raw("eps_filed"), errors="coerce")
        period_end = pd.to_datetime(_raw("eps_period_end"), errors="coerce")
        basis = filed.fillna(period_end)
        fac_eps = _factor_after(info, ticker, basis.values.astype("datetime64[D]"))

        eps = _series("eps")
        g["eps_present"] = (eps / pd.Series(fac_eps, index=g.index)).where(eps.notna())

        price_date = pd.to_datetime(_raw("price_date"), errors="coerce")
        fac_shares = _factor_after(
            info, ticker, price_date.values.astype("datetime64[D]")
        )
        shares = _series("shares")
        g["shares_present"] = shares * pd.Series(fac_shares, index=g.index)

        g["div_adj"] = _series("div_adj").fillna(0.0)

        eps_present = g["eps_present"]
        eps_count = eps_present.rolling(4).count().shift(1)
        g["ttm_eps"] = eps_present.rolling(4).sum().shift(1).where(eps_count == 4)
        g["eps4_min"] = eps_present.rolling(4).min().shift(1).where(eps_count == 4)
        g["ttm_div"] = g["div_adj"].rolling(4).sum()
        g["shares_lag"] = g["shares_present"].ffill(limit=4).shift(1)

        px = _series("px_adj")
        g["pe"] = (px / g["ttm_eps"]).where(g["ttm_eps"] > 0)
        g["mktcap"] = px * g["shares_lag"]
        g["div_yield"] = (g["ttm_div"] / px).where(px > 0)

        frames.append(g.reset_index())

    if not frames:
        return raw.assign(qidx=pd.Series(dtype="int64"))
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

_HOLDING_COLUMNS = [
    "ticker",
    "name",
    "weight",
    "pe",
    "mktcap",
    "div_yield",
    "vol_252d",
    "ret_252d",
]

# Output schema for the ``holdings`` frame: unlike an ``_eligible``/selected row
# (which carries raw-dollar ``mktcap``), the holdings table reports market cap in
# billions under ``mktcap_b``.
_HOLDINGS_OUTPUT_COLUMNS = [
    "rebalance_quarter",
    "date",
    "ticker",
    "name",
    "weight",
    "pe",
    "mktcap_b",
    "div_yield",
    "vol_252d",
    "ret_252d",
]


def _eligible(universe: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Apply the value filters; returns rows passing every constraint."""
    if len(universe) == 0:
        return universe

    pe_min = params.get("pe_min", 0.0)
    pe_max = params.get("pe_max", 15.0)
    mcap_min_b = params.get("mcap_min_b")
    mcap_max_b = params.get("mcap_max_b")
    min_div_yield = params.get("min_div_yield", 0.0)

    df = universe
    mask = (
        (df["px_adj"] > 0)
        & df["pe"].notna()
        & (df["pe"] >= pe_min)
        & (df["pe"] <= pe_max)
    )
    # Only constrain market cap when at least one bound is supplied; otherwise a
    # missing share count must not silently drop a qualifying name.
    if mcap_min_b is not None or mcap_max_b is not None:
        mcap_min = 0.0 if mcap_min_b is None else mcap_min_b * 1e9
        mcap_max = np.inf if mcap_max_b is None else mcap_max_b * 1e9
        mask &= (
            df["mktcap"].notna()
            & (df["mktcap"] >= mcap_min)
            & (df["mktcap"] <= mcap_max)
        )
    # No dividend data means 0 paid, so NaN yield is treated as 0.
    mask &= df["div_yield"].fillna(0.0) >= min_div_yield

    if params.get("require_pos_eps4", False):
        mask &= df["eps4_min"] > 0

    vol_max = params.get("vol_max")
    if vol_max is not None:
        mask &= df["vol_252d"].notna() & (df["vol_252d"] <= vol_max)

    min_ret = params.get("min_ret_12m")
    if min_ret is not None:
        mask &= df["ret_252d"].notna() & (df["ret_252d"] >= min_ret)

    return df.loc[mask]


def select_holdings(universe: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Select every qualifying name, equal-weighted.

    All rows passing the screens are held (no top-N cap).  Sorting is by ``pe``
    ascending then ``ticker`` ascending purely for deterministic output order.
    Returns an empty frame with the right columns when nothing qualifies.
    """
    eligible = _eligible(universe, params)
    if len(eligible) == 0:
        return eligible.assign(weight=pd.Series(dtype="float64"))[
            [c for c in _HOLDING_COLUMNS if c in eligible.columns or c == "weight"]
        ]

    selected = eligible.sort_values(["pe", "ticker"], kind="mergesort").copy()
    selected["weight"] = 1.0 / len(selected)
    return selected


def count_matching(
    panel: pd.DataFrame, params: dict, quarter: str | None = None
) -> int:
    """Number of ``_eligible`` rows in ``panel`` at ``quarter`` (default: max qidx).

    Pure logic, no Streamlit: the dashboard uses it to show how many names pass
    the current screens at the most recent quarter in the panel.
    """
    if panel is None or len(panel) == 0:
        return 0
    if "qidx" not in panel.columns:
        if "quarter" not in panel.columns:
            return 0
        panel = panel.assign(qidx=panel["quarter"].map(qidx))
    if quarter is not None:
        q = qidx(quarter)
    else:
        q = panel["qidx"].max()
        if pd.isna(q):
            return 0
    return int(len(_eligible(panel.loc[panel["qidx"] == q], params)))


# --------------------------------------------------------------------------- #
# Backtest
# --------------------------------------------------------------------------- #

FREQ_MODS = {
    "quarterly": frozenset({0, 1, 2, 3}),
    "semiannual": frozenset({1, 3}),
    "annual": frozenset({3}),
}

DEFAULT_PARAMS = {
    "pe_min": 0.0,
    "pe_max": 15.0,
    "mcap_min_b": 0.5,
    "mcap_max_b": 500.0,
    "min_div_yield": 0.0,
    "require_pos_eps4": True,
    "vol_max": None,
    "min_ret_12m": None,
    "freq": "quarterly",
    "start_year": None,
    "delist_mode": "carry",
}


@dataclass
class BacktestResult:
    series: pd.DataFrame
    holdings: pd.DataFrame
    stats: dict = field(default_factory=dict)


def _empty_result(settled: bool = False, message: str | None = None) -> BacktestResult:
    stats: dict = {
        "start_quarter": None,
        "end_quarter": None,
        "years": 0.0,
        "n_rebalances": 0,
        "avg_holdings": 0.0,
        "min_holdings": 0,
        "portfolio_final": None,
        "benchmark_final": None,
        "portfolio_cagr": None,
        "benchmark_cagr": None,
        "excess_cagr": None,
        "portfolio_total_return": None,
        "benchmark_total_return": None,
        "portfolio_max_dd": None,
        "benchmark_max_dd": None,
        "settled": settled,
        "rebalance_quarters": [],
    }
    if message:
        stats["message"] = message
    return BacktestResult(
        series=pd.DataFrame(columns=["quarter", "date", "portfolio", "benchmark"]),
        holdings=pd.DataFrame(columns=_HOLDINGS_OUTPUT_COLUMNS),
        stats=stats,
    )


def _ticker_returns(
    g: pd.DataFrame, min_q: int, max_q: int, delist_mode: str
) -> pd.Series:
    """Quarterly total-return series for one ticker, indexed by ``qidx``.

    ``carry`` (default): a missing split-adjusted price is forward-filled, so a
    delisting contributes 0% for the missing quarter(s).
    ``writeoff``: the first quarter whose own price goes missing after having
    been present returns -1.0 (total loss) and every later quarter returns 0.0.
    """
    idx = pd.RangeIndex(min_q, max_q + 1)
    s = g.drop_duplicates(subset=["qidx"]).set_index("qidx")
    px = pd.to_numeric(s["px_adj"], errors="coerce").reindex(idx)
    div = pd.to_numeric(s["div_adj"], errors="coerce").reindex(idx).fillna(0.0)

    px_ff = px.ffill()
    prev = px_ff.shift(1)
    r = (px_ff + div) / prev - 1.0

    if delist_mode == "writeoff":
        present = px.notna()
        first_missing = (~present) & present.shift(1, fill_value=False)
        if first_missing.any():
            first = first_missing.idxmax()
            r.loc[first] = -1.0
            r.loc[idx > first] = 0.0
    return r.fillna(0.0)


def _max_drawdown(values: pd.Series) -> float:
    if len(values) == 0:
        return float("nan")
    peak = values.cummax()
    dd = values / peak - 1.0
    return float(dd.min())


def _cagr(final: float | None, years: float) -> float | None:
    if final is None or years <= 0 or final <= 0:
        return None
    return (final / 100.0) ** (1.0 / years) - 1.0


def run_backtest(panel: pd.DataFrame, params: dict) -> BacktestResult:
    """Run the equal-weighted value backtest over ``panel``.

    ``panel`` is expected to come from :func:`load_panel` but any frame with the
    same columns (including a dense ``qidx``) works.  The run ends at
    ``panel.qidx.max()``; ``load_panel`` is responsible for excluding the
    current partial quarter.
    """
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})

    if len(panel) == 0:
        return _empty_result(settled=False, message="empty panel")

    panel = panel.copy()
    if "qidx" not in panel.columns:
        panel["qidx"] = panel["quarter"].map(qidx)
    panel["qidx"] = panel["qidx"].astype(int)

    first_q = int(panel["qidx"].min())
    last_q = int(panel["qidx"].max())
    if last_q <= first_q:
        return _empty_result(
            settled=False, message="panel has fewer than two quarters"
        )

    freq = p["freq"] if p["freq"] in FREQ_MODS else "quarterly"
    mods = FREQ_MODS[freq]
    start_year = p["start_year"]
    delist_mode = p["delist_mode"]

    grid = [q for q in range(first_q, last_q + 1) if (q % 4) in mods]
    if start_year is not None:
        grid = [q for q in grid if q >= int(start_year) * 4]
    if not grid:
        return _empty_result(settled=False, message="no rebalance dates")

    counts = {q: len(_eligible(panel.loc[panel["qidx"] == q], p)) for q in grid}
    start_q = next((q for q in grid if counts.get(q, 0) >= 1), None)
    if start_q is None:
        return _empty_result(
            settled=False, message="no quarter has any eligible holdings"
        )
    schedule = [q for q in grid if start_q <= q < last_q]
    if not schedule:
        return _empty_result(settled=False, message="no evaluable holding periods")

    # Per-ticker quarterly return series over the full panel range.
    returns = {
        ticker: _ticker_returns(g, first_q, last_q, delist_mode)
        for ticker, g in panel.groupby("ticker", sort=False)
    }
    spy_r = returns.get("SPY")

    value = 100.0
    bench = 100.0
    port_points = [(start_q, 100.0)]
    bench_points = [(start_q, 100.0)]
    holdings_rows: list[dict] = []
    holdings_counts: list[int] = []
    weights: dict[str, float] = {}

    for t in range(start_q, last_q):
        if t in schedule:
            selected = select_holdings(panel.loc[panel["qidx"] == t], p)
            weights = {
                rec["ticker"]: float(rec["weight"]) for rec in selected.to_dict("records")
            }
            holdings_counts.append(len(selected))
            for rec in selected.to_dict("records"):
                mktcap = rec.get("mktcap")
                holdings_rows.append(
                    {
                        "rebalance_quarter": quarter_from_idx(t),
                        "date": _quarter_end(t),
                        "ticker": rec.get("ticker"),
                        "name": rec.get("name"),
                        "weight": rec.get("weight"),
                        "pe": rec.get("pe"),
                        "mktcap_b": (
                            float(mktcap) / 1e9 if pd.notna(mktcap) else np.nan
                        ),
                        "div_yield": rec.get("div_yield"),
                        "vol_252d": rec.get("vol_252d"),
                        "ret_252d": rec.get("ret_252d"),
                    }
                )

        if weights:
            period_r = {
                ticker: float(returns[ticker].get(t + 1, 0.0))
                for ticker in weights
            }
            r_port = sum(weights[ticker] * period_r[ticker] for ticker in weights)
            if (1.0 + r_port) != 0.0:
                weights = {
                    ticker: weights[ticker]
                    * (1.0 + period_r[ticker])
                    / (1.0 + r_port)
                    for ticker in weights
                }
            else:
                weights = {}
        else:
            r_port = 0.0

        value *= 1.0 + r_port
        r_bench = float(spy_r.get(t + 1, 0.0)) if spy_r is not None else 0.0
        bench *= 1.0 + r_bench

        port_points.append((t + 1, value))
        bench_points.append((t + 1, bench))

    series = pd.DataFrame(
        {
            "quarter": [quarter_from_idx(q) for q, _ in port_points],
            "date": [_quarter_end(q) for q, _ in port_points],
            "portfolio": [v for _, v in port_points],
            "benchmark": [b for _, b in bench_points],
        }
    )
    holdings = pd.DataFrame(holdings_rows, columns=_HOLDINGS_OUTPUT_COLUMNS)

    start_date = series["date"].iloc[0]
    end_date = series["date"].iloc[-1]
    years = (end_date - start_date).days / 365.25

    series_port = series["portfolio"]
    series_bench = series["benchmark"]
    port_cagr = _cagr(value, years)
    bench_cagr = _cagr(bench, years)

    stats = {
        "start_quarter": quarter_from_idx(start_q),
        "end_quarter": quarter_from_idx(last_q),
        "years": years,
        "n_rebalances": len(schedule),
        "avg_holdings": float(np.mean(holdings_counts)) if holdings_counts else 0.0,
        "min_holdings": int(min(holdings_counts)) if holdings_counts else 0,
        "portfolio_final": value,
        "benchmark_final": bench,
        "portfolio_cagr": port_cagr,
        "benchmark_cagr": bench_cagr,
        "excess_cagr": (
            port_cagr - bench_cagr
            if port_cagr is not None and bench_cagr is not None
            else None
        ),
        "portfolio_total_return": value / 100.0 - 1.0,
        "benchmark_total_return": bench / 100.0 - 1.0,
        "portfolio_max_dd": _max_drawdown(series_port),
        "benchmark_max_dd": _max_drawdown(series_bench),
        "settled": True,
        "rebalance_quarters": [quarter_from_idx(q) for q in schedule],
    }
    return BacktestResult(series=series, holdings=holdings, stats=stats)
