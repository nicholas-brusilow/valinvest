"""Plain-python test runner for :mod:`backtest` (no pytest dependency).

Run inside the etl container:
    python dashboard/test_backtest.py

Exits non-zero if any case fails.  All panels are synthetic; no DB is touched.
"""
from __future__ import annotations

import datetime
import math
import os
import sys
import traceback

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest  # noqa: E402

PASSES: list[str] = []
FAILURES: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
    except AssertionError as exc:
        FAILURES.append(name)
        print(f"FAIL {name}: {exc}")
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc()
    else:
        PASSES.append(name)
        print(f"PASS {name}")


def approx(a, b, tol=1e-9) -> bool:
    return a is not None and math.isclose(float(a), float(b), rel_tol=tol, abs_tol=tol)


# --------------------------------------------------------------------------- #
# Panel builders
# --------------------------------------------------------------------------- #


def make_row(
    ticker: str,
    quarter: str,
    px: float,
    div: float = 0.0,
    pe: float = 5.0,
    mktcap: float = 10e9,
    div_yield: float = 0.0,
    eps4_min: float = 1.0,
    vol: float = 0.2,
    ret: float = 0.0,
    name: str | None = None,
) -> dict:
    return {
        "ticker": ticker,
        "quarter": quarter,
        "qidx": backtest.qidx(quarter),
        "px_adj": px,
        "div_adj": div,
        "pe": pe,
        "mktcap": mktcap,
        "div_yield": div_yield,
        "eps4_min": eps4_min,
        "vol_252d": vol,
        "ret_252d": ret,
        "name": name or ticker,
    }


def panel(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def make_raw(
    ticker: str,
    quarters: list[str],
    eps: list[float],
    px: float = 100.0,
    shares: float = 1000.0,
    filed: list | None = None,
    period_end: list | None = None,
    price_date: list | None = None,
) -> list[dict]:
    out = []
    for i, q in enumerate(quarters):
        out.append(
            {
                "ticker": ticker,
                "quarter": q,
                "price_traded": px,
                "shares": shares,
                "eps": eps[i],
                "eps_filed": (filed[i] if filed else None),
                "eps_period_end": (
                    period_end[i] if period_end else _q_end_date(q)
                ),
                "eps_is_derived": False,
                "px_adj": px,
                "price_date": price_date[i] if price_date else _q_end_date(q),
                "div_adj": 0.0,
                "vol_252d": 0.2,
                "ret_252d": 0.0,
                "off_high_252d": 0.0,
                "cik": 1,
                "name": ticker,
            }
        )
    return out


def _q_end_date(quarter: str) -> datetime.date:
    idx = backtest.qidx(quarter)
    ts = backtest._quarter_end(idx)
    return ts.date()


# --------------------------------------------------------------------------- #
# 1. price-only return
# --------------------------------------------------------------------------- #


def test_price_only_return():
    quarters = [f"2019Q{i}" for i in (1, 2, 3, 4)] + [f"2020Q{i}" for i in (1, 2, 3, 4)]
    rows = []
    aaa_px = {
        "2020Q1": 100.0,
        "2020Q2": 110.0,
        "2020Q3": 110.0,
        "2020Q4": 110.0,
    }
    spy_px = {
        "2020Q1": 100.0,
        "2020Q2": 105.0,
        "2020Q3": 105.0,
        "2020Q4": 105.0,
    }
    for q in quarters:
        rows.append(make_row("AAA", q, aaa_px.get(q, np.nan), pe=5.0))
        rows.append(make_row("SPY", q, spy_px.get(q, np.nan), pe=np.nan))
    result = backtest.run_backtest(
        panel(rows),
        {
            "pe_min": 0.0,
            "pe_max": 15.0,
            "mcap_min_b": 2.0,
            "mcap_max_b": 500.0,
            "freq": "quarterly",
        },
    )
    assert approx(result.stats["portfolio_final"], 110.0), result.stats
    assert approx(result.stats["benchmark_final"], 105.0), result.stats
    assert result.stats["start_quarter"] == "2020Q1", result.stats


# --------------------------------------------------------------------------- #
# 2. dividend included
# --------------------------------------------------------------------------- #


def test_dividend_included():
    quarters = [f"2019Q{i}" for i in (1, 2, 3, 4)] + [f"2020Q{i}" for i in (1, 2, 3, 4)]
    rows = []
    aaa_px = {"2020Q1": 100.0, "2020Q2": 110.0, "2020Q3": 110.0, "2020Q4": 110.0}
    aaa_div = {"2020Q2": 2.0}
    for q in quarters:
        rows.append(
            make_row("AAA", q, aaa_px.get(q, np.nan), div=aaa_div.get(q, 0.0), pe=5.0)
        )
        rows.append(make_row("SPY", q, 100.0, pe=np.nan))
    result = backtest.run_backtest(
        panel(rows),
        {"pe_min": 0.0, "pe_max": 15.0, "freq": "quarterly"},
    )
    assert approx(result.stats["portfolio_final"], 112.0), result.stats


# --------------------------------------------------------------------------- #
# 3. PE / mktcap / yield filters
# --------------------------------------------------------------------------- #


def test_filters():
    rows = [
        make_row("GOOD", "2020Q1", 100.0, pe=5.0, mktcap=10e9, div_yield=0.02),
        make_row("HIGHPE", "2020Q1", 100.0, pe=50.0, mktcap=10e9, div_yield=0.02),
        make_row("TINY", "2020Q1", 100.0, pe=5.0, mktcap=0.1e9, div_yield=0.02),
        make_row("BIG", "2020Q1", 100.0, pe=5.0, mktcap=900e9, div_yield=0.02),
        make_row("LOWYIELD", "2020Q1", 100.0, pe=5.0, mktcap=10e9, div_yield=0.0),
    ]
    uni = panel(rows)
    base = {
        "pe_min": 0.0,
        "pe_max": 15.0,
        "mcap_min_b": 2.0,
        "mcap_max_b": 500.0,
        "min_div_yield": 0.01,
    }
    sel = backtest.select_holdings(uni, base)
    assert set(sel["ticker"]) == {"GOOD"}, sel["ticker"].tolist()

    # PE filter: relax mcap/yield, keep only HIGHPE excluded by PE.
    sel = backtest.select_holdings(
        uni, {**base, "pe_max": 10.0, "min_div_yield": 0.0, "mcap_min_b": 0.0}
    )
    assert "HIGHPE" not in set(sel["ticker"]), sel["ticker"].tolist()
    assert "GOOD" in set(sel["ticker"])

    # mcap filter: TINY excluded, BIG excluded.
    sel = backtest.select_holdings(uni, {**base, "min_div_yield": 0.0})
    assert "TINY" not in set(sel["ticker"])
    assert "BIG" not in set(sel["ticker"])

    # yield filter: LOWYIELD excluded when a floor is set, included at 0.
    sel = backtest.select_holdings(uni, {**base, "mcap_min_b": 0.0, "mcap_max_b": None})
    assert "LOWYIELD" not in set(sel["ticker"])
    sel0 = backtest.select_holdings(
        uni, {**base, "mcap_min_b": 0.0, "mcap_max_b": None, "min_div_yield": 0.0}
    )
    assert "LOWYIELD" in set(sel0["ticker"])


# --------------------------------------------------------------------------- #
# 4. require_pos_eps4
# --------------------------------------------------------------------------- #


def test_require_pos_eps4():
    quarters = ["2019Q1", "2019Q2", "2019Q3", "2019Q4", "2020Q1"]
    raw = make_raw("POS", quarters, [1, 1, 1, 1, 1])
    raw += make_raw("NEG", quarters, [1, 1, -1, 1, 1])
    feat = backtest.build_features(panel(raw), pd.DataFrame())
    at = feat.loc[feat["quarter"] == "2020Q1"]
    pos_min = at.loc[at["ticker"] == "POS", "eps4_min"].iloc[0]
    neg_min = at.loc[at["ticker"] == "NEG", "eps4_min"].iloc[0]
    assert approx(pos_min, 1.0), pos_min
    assert approx(neg_min, -1.0), neg_min

    p = {
        "pe_min": 0.0,
        "pe_max": 100.0,
        "mcap_min_b": None,
        "mcap_max_b": None,
        "require_pos_eps4": True,
    }
    sel = backtest.select_holdings(at, p)
    assert set(sel["ticker"]) == {"POS"}, sel["ticker"].tolist()


# --------------------------------------------------------------------------- #
# 5. vol / 12m return filters
# --------------------------------------------------------------------------- #


def test_vol_and_ret_filters():
    rows = [
        make_row("A", "2020Q1", 100.0, vol=0.3, ret=0.2),
        make_row("B", "2020Q1", 100.0, vol=0.8, ret=0.2),
        make_row("C", "2020Q1", 100.0, vol=np.nan, ret=0.2),
        make_row("D", "2020Q1", 100.0, vol=0.3, ret=0.6),
    ]
    uni = panel(rows)
    base = {"pe_min": 0.0, "pe_max": 15.0}
    sel = backtest.select_holdings(uni, {**base, "vol_max": 0.5})
    assert set(sel["ticker"]) == {"A", "D"}, sel["ticker"].tolist()
    assert "C" not in set(sel["ticker"])  # NaN vol excluded when a cap is set

    sel = backtest.select_holdings(uni, {**base, "min_ret_12m": 0.5})
    assert set(sel["ticker"]) == {"D"}, sel["ticker"].tolist()
    # NaN vol is allowed when no cap is set.
    sel = backtest.select_holdings(uni, base)
    assert "C" in set(sel["ticker"])


# --------------------------------------------------------------------------- #
# 6. all eligible names are held equal-weighted (no top-N)
# --------------------------------------------------------------------------- #


def test_all_eligible_equal_weight():
    rows = [
        make_row("AAA", "2020Q1", 100.0, pe=5.0),
        make_row("BBB", "2020Q1", 100.0, pe=5.0),
        make_row("CCC", "2020Q1", 100.0, pe=3.0),
        make_row("DDD", "2020Q1", 100.0, pe=3.0),
        make_row("HIGHPE", "2020Q1", 100.0, pe=50.0),
    ]
    uni = panel(rows)
    sel = backtest.select_holdings(uni, {"pe_min": 0.0, "pe_max": 15.0})
    assert sel["ticker"].tolist() == ["CCC", "DDD", "AAA", "BBB"], sel["ticker"].tolist()
    assert all(approx(w, 0.25) for w in sel["weight"]), sel["weight"].tolist()
    assert "HIGHPE" not in set(sel["ticker"])
    # A stale ``n_stocks`` key is ignored: all eligible names are still held.
    stale = backtest.select_holdings(
        uni, {"n_stocks": 2, "pe_min": 0.0, "pe_max": 15.0}
    )
    assert stale["ticker"].tolist() == ["CCC", "DDD", "AAA", "BBB"], stale[
        "ticker"
    ].tolist()


# --------------------------------------------------------------------------- #
# 7. rebalance frequencies
# --------------------------------------------------------------------------- #


def test_rebalance_frequencies():
    quarters = [f"2020Q{i}" for i in (1, 2, 3, 4)] + [f"2021Q{i}" for i in (1, 2, 3, 4)]
    rows = [make_row("AAA", q, 100.0, pe=5.0) for q in quarters]
    base = {"pe_min": 0.0, "pe_max": 15.0}

    ann = backtest.run_backtest(panel(rows), {**base, "freq": "annual"})
    assert ann.holdings["rebalance_quarter"].tolist() == ["2020Q4"], ann.holdings[
        "rebalance_quarter"
    ].tolist()

    semi = backtest.run_backtest(panel(rows), {**base, "freq": "semiannual"})
    assert semi.holdings["rebalance_quarter"].tolist() == [
        "2020Q2",
        "2020Q4",
        "2021Q2",
    ], semi.holdings["rebalance_quarter"].tolist()

    qtr = backtest.run_backtest(panel(rows), {**base, "freq": "quarterly"})
    assert qtr.holdings["rebalance_quarter"].tolist() == [
        "2020Q1",
        "2020Q2",
        "2020Q3",
        "2020Q4",
        "2021Q1",
        "2021Q2",
        "2021Q3",
    ], qtr.holdings["rebalance_quarter"].tolist()


# --------------------------------------------------------------------------- #
# 8. weight drift
# --------------------------------------------------------------------------- #


def test_weight_drift():
    rows = []
    a_px = {"2020Q1": 100.0, "2020Q2": 200.0, "2020Q3": 200.0}
    b_px = {"2020Q1": 100.0, "2020Q2": 100.0, "2020Q3": 100.0}
    for q in ("2020Q1", "2020Q2", "2020Q3"):
        rows.append(make_row("AAA", q, a_px[q], pe=5.0))
        rows.append(make_row("BBB", q, b_px[q], pe=6.0))
    result = backtest.run_backtest(
        panel(rows),
        {"pe_min": 0.0, "pe_max": 15.0, "freq": "quarterly"},
    )
    # Period 1: (2.0 + 1.0)/2 = 1.5 -> 150.  Weights drift to 2/3, 1/3.
    # Period 2: both flat -> value stays 150.
    by_q = result.series.set_index("quarter")["portfolio"]
    assert approx(by_q["2020Q2"], 150.0), by_q.to_dict()
    assert approx(result.stats["portfolio_final"], 150.0), result.stats


# --------------------------------------------------------------------------- #
# 9. split normalization in build_features
# --------------------------------------------------------------------------- #


def test_split_normalization():
    quarters = ["2019Q1", "2019Q2", "2019Q3", "2019Q4", "2020Q1"]
    fwd_raw = make_raw("FWD", quarters, [0, 0, 0, 10, 10], px=50.0)
    rev_raw = make_raw("REV", quarters, [0, 0, 0, 10, 10], px=50.0)

    splits = pd.DataFrame(
        [
            {"ticker_yahoo": "FWD", "split_date": datetime.date(2020, 6, 30), "ratio": 2.0},
            {"ticker_yahoo": "REV", "split_date": datetime.date(2020, 6, 30), "ratio": 0.5},
        ]
    )
    feat = backtest.build_features(panel(fwd_raw + rev_raw), splits)

    fwd = feat[feat["ticker"] == "FWD"].set_index("quarter")
    rev = feat[feat["ticker"] == "REV"].set_index("quarter")
    assert approx(fwd.loc["2019Q4", "eps_present"], 5.0), fwd["eps_present"].to_dict()
    assert approx(rev.loc["2019Q4", "eps_present"], 20.0), rev["eps_present"].to_dict()
    # FWD ttm at 2020Q1 = 5 (only 2019Q4 is non-zero) -> pe = 50/5 = 10.
    assert approx(fwd.loc["2020Q1", "pe"], 10.0), fwd.loc["2020Q1", "pe"]


# --------------------------------------------------------------------------- #
# 10. look-ahead
# --------------------------------------------------------------------------- #


def test_no_lookahead():
    quarters = ["2019Q1", "2019Q2", "2019Q3", "2019Q4", "2020Q1"]
    raw = make_raw("AAA", quarters, [1, 2, 3, 4, 100], px=100.0)
    feat = backtest.build_features(panel(raw), pd.DataFrame()).set_index("quarter")
    # ttm at 2020Q1 must be 1+2+3+4 = 10, never including eps(2020Q1)=100.
    assert approx(feat.loc["2020Q1", "ttm_eps"], 10.0), feat.loc["2020Q1", "ttm_eps"]
    assert approx(feat.loc["2020Q1", "pe"], 10.0), feat.loc["2020Q1", "pe"]


# --------------------------------------------------------------------------- #
# 11. benchmark rebasing
# --------------------------------------------------------------------------- #


def test_benchmark_rebase():
    quarters = [f"2019Q{i}" for i in (1, 2, 3, 4)] + [f"2020Q{i}" for i in (1, 2, 3, 4)]
    rows = []
    for q in quarters:
        rows.append(make_row("AAA", q, 100.0, pe=5.0))
        rows.append(make_row("SPY", q, 200.0, pe=np.nan))
    result = backtest.run_backtest(
        panel(rows), {"pe_min": 0.0, "pe_max": 15.0}
    )
    first = result.series.iloc[0]
    assert approx(first["portfolio"], 100.0), first.to_dict()
    assert approx(first["benchmark"], 100.0), first.to_dict()


# --------------------------------------------------------------------------- #
# 12. last_complete_quarter
# --------------------------------------------------------------------------- #


def test_last_complete_quarter():
    assert backtest.last_complete_quarter(datetime.date(2026, 9, 15)) == "2026Q2"
    assert backtest.last_complete_quarter(datetime.date(2026, 1, 2)) == "2025Q4"
    assert backtest.last_complete_quarter(datetime.date(2026, 3, 31)) == "2025Q4"
    assert backtest.last_complete_quarter(datetime.date(2026, 4, 1)) == "2026Q1"


# --------------------------------------------------------------------------- #
# 13. delist carry vs writeoff
# --------------------------------------------------------------------------- #


def test_delist_modes():
    rows = [
        make_row("XYZ", "2020Q1", 100.0, pe=5.0),
        make_row("XYZ", "2020Q2", np.nan, pe=np.nan),
        make_row("XYZ", "2020Q3", 100.0, pe=5.0),
    ]
    params = {"pe_min": 0.0, "pe_max": 15.0, "freq": "quarterly"}
    carry = backtest.run_backtest(panel(rows), {**params, "delist_mode": "carry"})
    assert approx(carry.stats["portfolio_final"], 100.0), carry.stats
    writeoff = backtest.run_backtest(panel(rows), {**params, "delist_mode": "writeoff"})
    assert approx(writeoff.stats["portfolio_final"], 0.0), writeoff.stats


# --------------------------------------------------------------------------- #
# 14. holdings market cap column is populated in billions
# --------------------------------------------------------------------------- #


def test_holdings_mktcap_column():
    quarters = ["2020Q1", "2020Q2", "2020Q3"]
    rows = []
    px = {"2020Q1": 100.0, "2020Q2": 110.0, "2020Q3": 121.0}
    for q in quarters:
        rows.append(make_row("AAA", q, px[q], pe=5.0, mktcap=10e9))
        rows.append(make_row("SPY", q, 100.0, pe=np.nan))
    result = backtest.run_backtest(
        panel(rows), {"pe_min": 0.0, "pe_max": 15.0}
    )
    assert "mktcap_b" in result.holdings.columns, result.holdings.columns.tolist()
    assert not result.holdings["mktcap_b"].isna().all(), result.holdings
    assert approx(result.holdings["mktcap_b"].iloc[0], 10.0), result.holdings
    # Empty results must expose the same holdings schema.
    empty = backtest._empty_result()
    assert list(empty.holdings.columns) == list(result.holdings.columns)


# --------------------------------------------------------------------------- #
# 15. VALUE_PANEL_COLUMNS matches build_features() output exactly
# --------------------------------------------------------------------------- #


def test_value_panel_columns():
    quarters = [f"2019Q{i}" for i in (1, 2, 3, 4)] + [f"2020Q{i}" for i in (1, 2, 3, 4)]
    raw = make_raw("AAA", quarters, [1.0, 1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 2.0])
    # build_features preserves the raw column order, and PANEL_SQL selects
    # "quarter" before "ticker"; mirror that order here.
    raw_columns = (
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
    )
    raw_df = pd.DataFrame(raw)[list(raw_columns)]
    feat = backtest.build_features(raw_df, pd.DataFrame())
    assert set(backtest.VALUE_PANEL_COLUMNS) == set(feat.columns), (
        "set mismatch: "
        f"{set(backtest.VALUE_PANEL_COLUMNS) ^ set(feat.columns)}"
    )
    assert tuple(backtest.VALUE_PANEL_COLUMNS) == tuple(feat.columns), (
        "order mismatch: expected "
        f"{tuple(backtest.VALUE_PANEL_COLUMNS)}, got {tuple(feat.columns)}"
    )


# --------------------------------------------------------------------------- #
# 16. count_matching helper
# --------------------------------------------------------------------------- #


def test_count_matching():
    rows = [
        make_row("AAA", "2020Q1", 100.0, pe=5.0),
        make_row("BBB", "2020Q1", 100.0, pe=12.0),
        make_row("CCC", "2020Q1", 100.0, pe=20.0),
        make_row("AAA", "2020Q2", 100.0, pe=5.0),
        make_row("BBB", "2020Q2", 100.0, pe=12.0),
        make_row("CCC", "2020Q2", 100.0, pe=20.0),
    ]
    uni = panel(rows)
    filt = {"pe_min": 0.0, "pe_max": 15.0}
    assert backtest.count_matching(uni, filt) == 2
    assert backtest.count_matching(uni, filt, quarter="2020Q1") == 2
    assert backtest.count_matching(uni, {"pe_min": 0.0, "pe_max": 10.0}) == 1
    assert backtest.count_matching(uni, filt, quarter="1990Q1") == 0
    assert backtest.count_matching(panel([]), filt) == 0
    nan_idx = pd.DataFrame({"qidx": [np.nan, np.nan]})
    assert backtest.count_matching(nan_idx, filt) == 0


# --------------------------------------------------------------------------- #
# 17. start quarter is the first with any eligible name
# --------------------------------------------------------------------------- #


def test_start_quarter_first_eligible():
    rows = []
    px = {"2020Q1": 100.0, "2020Q2": 110.0, "2020Q3": 121.0, "2020Q4": 133.1}
    for q in ("2020Q1", "2020Q2", "2020Q3", "2020Q4"):
        pe = np.nan if q in ("2020Q1", "2020Q2") else 5.0
        rows.append(make_row("AAA", q, px[q], pe=pe))
        rows.append(make_row("SPY", q, 100.0, pe=np.nan))
    result = backtest.run_backtest(
        panel(rows), {"pe_min": 0.0, "pe_max": 15.0, "freq": "quarterly"}
    )
    assert result.stats["settled"] is True, result.stats
    assert result.stats["start_quarter"] == "2020Q3", result.stats
    assert result.holdings["rebalance_quarter"].tolist() == ["2020Q3"], result.holdings[
        "rebalance_quarter"
    ].tolist()


# --------------------------------------------------------------------------- #
# 18. no eligible holdings anywhere -> unsettled
# --------------------------------------------------------------------------- #


def test_no_eligible_returns_unsettled():
    rows = []
    for q in ("2020Q1", "2020Q2"):
        rows.append(make_row("AAA", q, 100.0, pe=np.nan))
        rows.append(make_row("SPY", q, 100.0, pe=np.nan))
    result = backtest.run_backtest(
        panel(rows), {"pe_min": 0.0, "pe_max": 15.0, "freq": "quarterly"}
    )
    assert result.stats["settled"] is False, result.stats
    assert result.stats["start_quarter"] is None, result.stats
    assert result.series.empty, result.series
    assert result.holdings.empty, result.holdings
    assert "message" in result.stats, result.stats


# --------------------------------------------------------------------------- #
# 19. eligible only in the final quarter -> no evaluable period
# --------------------------------------------------------------------------- #


def test_single_point_guard():
    rows = [
        make_row("AAA", "2020Q1", 100.0, pe=np.nan),
        make_row("AAA", "2020Q2", 110.0, pe=np.nan),
        make_row("AAA", "2020Q3", 121.0, pe=5.0),
        make_row("SPY", "2020Q1", 100.0, pe=np.nan),
        make_row("SPY", "2020Q2", 100.0, pe=np.nan),
        make_row("SPY", "2020Q3", 100.0, pe=np.nan),
    ]
    result = backtest.run_backtest(
        panel(rows), {"pe_min": 0.0, "pe_max": 15.0, "freq": "quarterly"}
    )
    assert result.stats["settled"] is False, result.stats
    assert result.series.empty, result.series
    assert "message" in result.stats, result.stats


# --------------------------------------------------------------------------- #
# 20. quarterly_comparison returns / winners
# --------------------------------------------------------------------------- #


def equity_series(quarters: list[str], port: list[float], bench: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "quarter": quarters,
            "date": [backtest._quarter_end(backtest.qidx(q)) for q in quarters],
            "portfolio": port,
            "benchmark": bench,
        }
    )


def test_quarterly_comparison_returns():
    series = equity_series(
        ["2020Q1", "2020Q2", "2020Q3"], [100.0, 110.0, 99.0], [100.0, 105.0, 105.0]
    )
    comp = backtest.quarterly_comparison(series)
    assert list(comp.columns) == list(backtest.COMPARISON_COLUMNS), comp.columns.tolist()
    assert len(comp) == 2, comp
    assert comp["quarter"].tolist() == ["2020Q2", "2020Q3"], comp["quarter"].tolist()
    assert approx(comp["portfolio_return"].iloc[0], 0.10), comp
    assert approx(comp["portfolio_return"].iloc[1], -0.10), comp
    assert approx(comp["benchmark_return"].iloc[0], 0.05), comp
    assert approx(comp["benchmark_return"].iloc[1], 0.00), comp
    assert approx(comp["difference"].iloc[0], 0.05), comp
    assert approx(comp["difference"].iloc[1], -0.10), comp
    assert comp["winner"].tolist() == [backtest.WIN_VALUE, backtest.WIN_BENCH], comp[
        "winner"
    ].tolist()


# --------------------------------------------------------------------------- #
# 21. quarterly_comparison ties and zero benchmark returns
# --------------------------------------------------------------------------- #


def test_quarterly_comparison_ties():
    series = equity_series(
        ["2020Q1", "2020Q2", "2020Q3"], [100.0, 110.0, 132.0], [100.0, 110.0, 121.0]
    )
    comp = backtest.quarterly_comparison(series)
    assert comp["winner"].tolist() == [backtest.WIN_TIE, backtest.WIN_VALUE], comp[
        "winner"
    ].tolist()

    flat = equity_series(
        ["2020Q1", "2020Q2", "2020Q3"], [100.0, 110.0, 99.0], [100.0, 100.0, 100.0]
    )
    comp = backtest.quarterly_comparison(flat)
    assert approx(comp["benchmark_return"].iloc[0], 0.0), comp["benchmark_return"].iloc[0]
    assert not pd.isna(comp["benchmark_return"].iloc[0]), comp["benchmark_return"].iloc[0]
    assert comp["winner"].iloc[1] == backtest.WIN_BENCH, comp["winner"].iloc[1]


# --------------------------------------------------------------------------- #
# 22. quarterly_comparison guards
# --------------------------------------------------------------------------- #


def test_quarterly_comparison_empty_guard():
    empty = backtest.quarterly_comparison(pd.DataFrame())
    assert empty.empty, empty
    assert list(empty.columns) == list(backtest.COMPARISON_COLUMNS), empty.columns.tolist()

    single = backtest.quarterly_comparison(
        equity_series(["2020Q1"], [100.0], [100.0])
    )
    assert single.empty, single
    assert list(single.columns) == list(backtest.COMPARISON_COLUMNS), single.columns.tolist()

    zero_prev = backtest.quarterly_comparison(
        equity_series(["2020Q1", "2020Q2", "2020Q3"], [100.0, 0.0, 50.0], [100.0, 100.0, 100.0])
    )
    diffs = zero_prev["difference"].dropna()
    assert not np.isinf(diffs).any(), diffs.tolist()
    assert zero_prev["winner"].iloc[-1] == backtest.WIN_NA, zero_prev["winner"].iloc[-1]


# --------------------------------------------------------------------------- #
# 23. run_backtest populates comparison + win stats
# --------------------------------------------------------------------------- #


def test_quarterly_comparison_stats():
    quarters = ["2020Q1", "2020Q2", "2020Q3"]
    aaa_px = {"2020Q1": 100.0, "2020Q2": 110.0, "2020Q3": 99.0}
    spy_px = {"2020Q1": 100.0, "2020Q2": 105.0, "2020Q3": 105.0}
    rows = []
    for q in quarters:
        rows.append(make_row("AAA", q, aaa_px[q], pe=5.0))
        rows.append(make_row("SPY", q, spy_px[q], pe=np.nan))
    result = backtest.run_backtest(
        panel(rows), {"pe_min": 0.0, "pe_max": 15.0, "freq": "quarterly"}
    )
    assert list(result.comparison.columns) == list(backtest.COMPARISON_COLUMNS), result.comparison.columns.tolist()
    assert len(result.comparison) == 2, result.comparison
    assert result.comparison["quarter"].tolist() == ["2020Q2", "2020Q3"], result.comparison[
        "quarter"
    ].tolist()
    assert result.stats["value_win_quarters"] == 1, result.stats
    assert result.stats["benchmark_win_quarters"] == 1, result.stats
    assert result.stats["tie_quarters"] == 0, result.stats
    assert result.stats["comparison_quarters"] == 2, result.stats
    assert approx(result.stats["portfolio_mean_quarterly_return"], 0.0), result.stats
    assert approx(result.stats["portfolio_median_quarterly_return"], 0.0), result.stats
    assert approx(result.stats["benchmark_mean_quarterly_return"], 0.025), result.stats
    assert approx(result.stats["benchmark_median_quarterly_return"], 0.025), result.stats

    # Second run where mean != median, proving the two statistics are wired to
    # the right computation.
    quarters2 = ["2021Q1", "2021Q2", "2021Q3", "2021Q4"]
    aaa_px2 = {"2021Q1": 100.0, "2021Q2": 110.0, "2021Q3": 99.0, "2021Q4": 108.9}
    spy_px2 = {"2021Q1": 100.0, "2021Q2": 105.0, "2021Q3": 105.0, "2021Q4": 110.25}
    rows2 = []
    for q in quarters2:
        rows2.append(make_row("AAA", q, aaa_px2[q], pe=5.0))
        rows2.append(make_row("SPY", q, spy_px2[q], pe=np.nan))
    result2 = backtest.run_backtest(
        panel(rows2), {"pe_min": 0.0, "pe_max": 15.0, "freq": "quarterly"}
    )
    assert result2.stats["value_win_quarters"] == 2, result2.stats
    assert result2.stats["benchmark_win_quarters"] == 1, result2.stats
    assert result2.stats["comparison_quarters"] == 3, result2.stats
    assert approx(result2.stats["portfolio_mean_quarterly_return"], 0.1 / 3), result2.stats
    assert approx(result2.stats["portfolio_median_quarterly_return"], 0.10), result2.stats
    assert approx(result2.stats["benchmark_mean_quarterly_return"], 0.1 / 3), result2.stats
    assert approx(result2.stats["benchmark_median_quarterly_return"], 0.05), result2.stats


# --------------------------------------------------------------------------- #
# 24. empty result comparison schema
# --------------------------------------------------------------------------- #


def test_empty_result_comparison_schema():
    result = backtest._empty_result()
    assert list(result.comparison.columns) == list(backtest.COMPARISON_COLUMNS), result.comparison.columns.tolist()
    assert result.comparison.empty, result.comparison
    for key in (
        "value_win_quarters",
        "benchmark_win_quarters",
        "tie_quarters",
        "comparison_quarters",
    ):
        assert result.stats[key] == 0, (key, result.stats)
    for key in (
        "portfolio_mean_quarterly_return",
        "benchmark_mean_quarterly_return",
        "portfolio_median_quarterly_return",
        "benchmark_median_quarterly_return",
    ):
        assert result.stats[key] is None, (key, result.stats)


# --------------------------------------------------------------------------- #

TESTS = [
    ("price_only_return", test_price_only_return),
    ("dividend_included", test_dividend_included),
    ("filters", test_filters),
    ("require_pos_eps4", test_require_pos_eps4),
    ("vol_and_ret_filters", test_vol_and_ret_filters),
    ("all_eligible_equal_weight", test_all_eligible_equal_weight),
    ("rebalance_frequencies", test_rebalance_frequencies),
    ("weight_drift", test_weight_drift),
    ("split_normalization", test_split_normalization),
    ("no_lookahead", test_no_lookahead),
    ("benchmark_rebase", test_benchmark_rebase),
    ("last_complete_quarter", test_last_complete_quarter),
    ("delist_modes", test_delist_modes),
    ("holdings_mktcap_column", test_holdings_mktcap_column),
    ("value_panel_columns", test_value_panel_columns),
    ("count_matching", test_count_matching),
    ("start_quarter_first_eligible", test_start_quarter_first_eligible),
    ("no_eligible_returns_unsettled", test_no_eligible_returns_unsettled),
    ("single_point_guard", test_single_point_guard),
    ("quarterly_comparison_returns", test_quarterly_comparison_returns),
    ("quarterly_comparison_ties", test_quarterly_comparison_ties),
    ("quarterly_comparison_empty_guard", test_quarterly_comparison_empty_guard),
    ("quarterly_comparison_stats", test_quarterly_comparison_stats),
    ("empty_result_comparison_schema", test_empty_result_comparison_schema),
]


def main() -> int:
    for name, fn in TESTS:
        check(name, fn)
    print()
    print(f"summary: {len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        print("failed: " + ", ".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
