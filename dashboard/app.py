"""Streamlit dashboard for the ValInvest value-investing backtest.

This module is intentionally thin: all data loading and return math live in
``dashboard/backtest.py`` (UI-free and unit-tested).  The app renders screening
controls in the sidebar, runs the cached backtest, and shows the equity curve,
summary metrics, latest holdings and an interactive rebalance history.
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(
    page_title="ValInvest — Value vs S&P 500",
    layout="wide",
    page_icon="📈",
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest  # noqa: E402 - local module in this directory

DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://valinvest:valinvest@db:5432/valinvest"
)

FREQ_LABELS = {
    "Quarterly": "quarterly",
    "Semiannually (Q2/Q4)": "semiannual",
    "Annually (Q4)": "annual",
}
VOL_FILTERS = {
    "No limit": None,
    "≤ 30%": 0.30,
    "≤ 40%": 0.40,
    "≤ 50%": 0.50,
    "≤ 60%": 0.60,
    "≤ 80%": 0.80,
}
VOL_HELP = (
    "Excludes stocks whose trailing 12-month annualized volatility exceeds the "
    "limit. A very low P/E often reflects a collapsing share price (a value trap), "
    "so capping volatility screens those out."
)
START_YEARS = ["Earliest available"] + [str(y) for y in range(2008, 2027)]


@st.cache_data(show_spinner="Loading fundamentals panel ...")
def get_panel() -> pd.DataFrame:
    """Load (and cache) the quarterly fundamentals panel for the current DB."""
    return backtest.load_panel(DB_URL)


@st.cache_data(show_spinner="Running backtest ...")
def run_cached(params_key: tuple) -> backtest.BacktestResult:
    """Run the backtest, cached on a hashable tuple of the screen parameters."""
    params = dict(params_key)
    return backtest.run_backtest(get_panel(), params)


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _fmt_dollar(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


# --------------------------------------------------------------------------- #
# Sidebar — screening criteria
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Screening criteria")

    n_stocks = st.slider(
        "Number of value stocks", min_value=5, max_value=50, value=20, key="n_stocks"
    )

    freq_label = st.radio(
        "Rebalance frequency",
        list(FREQ_LABELS),
        index=0,
        key="freq",
    )
    freq = FREQ_LABELS[freq_label]

    pe_range = st.slider(
        "P/E (TTM, split-adjusted) range",
        min_value=0.0,
        max_value=60.0,
        value=(0.0, 15.0),
        step=0.5,
        key="pe_range",
    )

    mcap_min = st.number_input(
        "Min market cap ($B)",
        min_value=0.0,
        value=0.5,
        step=0.5,
        key="mcap_min",
    )
    mcap_max = st.number_input(
        "Max market cap ($B)",
        min_value=0.0,
        value=500.0,
        step=10.0,
        key="mcap_max",
    )

    div_yield_pct = st.slider(
        "Min dividend yield (%)",
        min_value=0.0,
        max_value=10.0,
        value=0.0,
        step=0.1,
        key="div_yield",
    )

    require_pos_eps4 = st.checkbox(
        "Require positive EPS in each of the last 4 quarters",
        value=True,
        key="require_pos_eps4",
    )

    vol_label = st.selectbox(
        "Volatility filter (max 12-month annualized)",
        list(VOL_FILTERS),
        index=0,
        help=VOL_HELP,
        key="vol_filter",
    )

    min_ret_pct = st.slider(
        "Crash filter: minimum 12-month price return (%)",
        min_value=-100,
        max_value=0,
        value=-100,
        step=5,
        key="min_ret",
    )

    start_year_label = st.selectbox(
        "Start year", START_YEARS, index=0, key="start_year"
    )

    log_scale = st.checkbox("Log scale on chart", value=False, key="log_scale")


# --------------------------------------------------------------------------- #
# Validate inputs
# --------------------------------------------------------------------------- #
if mcap_min > mcap_max:
    st.error("Min market cap must be less than or equal to max market cap.")
    st.stop()

params = {
    "n_stocks": int(n_stocks),
    "pe_min": float(pe_range[0]),
    "pe_max": float(pe_range[1]),
    "mcap_min_b": float(mcap_min),
    "mcap_max_b": float(mcap_max),
    "min_div_yield": float(div_yield_pct) / 100.0,
    "require_pos_eps4": bool(require_pos_eps4),
    "vol_max": VOL_FILTERS[vol_label],
    "min_ret_12m": None if min_ret_pct == -100 else float(min_ret_pct) / 100.0,
    "freq": freq,
    "start_year": None if start_year_label == "Earliest available" else int(start_year_label),
    "delist_mode": "carry",
}

# --------------------------------------------------------------------------- #
# Load + run
# --------------------------------------------------------------------------- #
try:
    result = run_cached(tuple(sorted(params.items())))
except Exception as exc:  # noqa: BLE001 - surface a friendly message to the user
    st.error(f"Backtest failed: {exc}")
    st.info(
        "Check that the database is running and the derived tables are built "
        "(run `./run.sh` from the project root)."
    )
    st.stop()

stats = result.stats
series = result.series
holdings = result.holdings

# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.title("ValInvest — Value Portfolio vs S&P 500")

if not stats.get("settled") or series.empty:
    st.warning(
        stats.get(
            "message",
            "No backtest result for these criteria. Try loosening the screens.",
        )
    )
    st.stop()

st.caption(
    f"Analyzed {stats['start_quarter']} → {stats['end_quarter']} "
    f"({stats['n_rebalances']} rebalances, {freq_label.lower()}) · "
    "benchmark = SPY total return."
)

# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
port_cagr = stats.get("portfolio_cagr")
bench_cagr = stats.get("benchmark_cagr")
excess_cagr = stats.get("excess_cagr")
port_dd = stats.get("portfolio_max_dd")
bench_dd = stats.get("benchmark_max_dd")
dd_delta = None if port_dd is None or bench_dd is None else port_dd - bench_dd

m1, m2, m3, m4 = st.columns(4)
with m1:
    st.metric("Portfolio final value", _fmt_dollar(stats.get("portfolio_final")))
with m2:
    st.metric("S&P 500 final value", _fmt_dollar(stats.get("benchmark_final")))
with m3:
    st.metric(
        "Portfolio CAGR",
        _fmt_pct(port_cagr),
        delta=None if excess_cagr is None else f"{excess_cagr:+.2%} vs S&P 500",
    )
with m4:
    st.metric(
        "Portfolio max drawdown",
        _fmt_pct(port_dd),
        delta=None if dd_delta is None else f"{dd_delta:+.2%} vs S&P 500",
        delta_color="normal",
    )

# --------------------------------------------------------------------------- #
# Equity curve
# --------------------------------------------------------------------------- #
fig = go.Figure()
fig.add_trace(
    go.Scatter(
        x=series["date"],
        y=series["portfolio"],
        mode="lines",
        name="Value portfolio",
        line=dict(color="#1f77b4"),
        hovertemplate="%{x|%Y-%m-%d}<br>$%{y:.2f}<extra>Value portfolio</extra>",
    )
)
fig.add_trace(
    go.Scatter(
        x=series["date"],
        y=series["benchmark"],
        mode="lines",
        name="S&P 500 (SPY)",
        line=dict(color="#636363"),
        hovertemplate="%{x|%Y-%m-%d}<br>$%{y:.2f}<extra>S&P 500 (SPY)</extra>",
    )
)
fig.update_layout(
    hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    yaxis_title="Value ($, start = 100)",
    margin=dict(l=10, r=10, t=40, b=10),
)
if log_scale:
    fig.update_yaxes(type="log")
st.plotly_chart(fig, width="stretch")

# --------------------------------------------------------------------------- #
# Latest holdings
# --------------------------------------------------------------------------- #
if holdings.empty:
    st.subheader("Latest rebalance holdings")
    st.info("No holdings were selected.")
else:
    latest_q = max(holdings["rebalance_quarter"].astype(str))
    st.subheader(f"Latest rebalance holdings ({latest_q})")
    latest = holdings.loc[holdings["rebalance_quarter"] == latest_q].copy()
    latest = latest.sort_values(["weight", "ticker"], ascending=[False, True],
                                kind="mergesort")

    display = pd.DataFrame(
        {
            "Ticker": latest["ticker"].astype(str).to_numpy(),
            "Name": latest["name"].fillna("").astype(str).to_numpy(),
            "Weight (%)": (latest["weight"] * 100).round(2).to_numpy(),
            "P/E": latest["pe"].round(2).to_numpy(),
            "Mkt cap ($B)": latest["mktcap_b"].round(2).to_numpy(),
            "Dividend yield (%)": (latest["div_yield"] * 100).round(2).to_numpy(),
            "Vol 12m (%)": (latest["vol_252d"] * 100).round(2).to_numpy(),
            "12m return (%)": (latest["ret_252d"] * 100).round(2).to_numpy(),
        }
    )
    st.dataframe(display, hide_index=True, width="stretch")
    st.caption(
        f"{len(latest)} holdings selected at the latest rebalance "
        f"(requested {params['n_stocks']})."
    )
    if stats.get("min_holdings", 0) < params["n_stocks"]:
        st.caption(
            f"⚠️ Some rebalances held fewer than the requested "
            f"{params['n_stocks']} names (minimum {stats['min_holdings']})."
        )

# --------------------------------------------------------------------------- #
# Rebalance history
# --------------------------------------------------------------------------- #
with st.expander("Rebalance history"):
    if holdings.empty:
        st.write("No rebalances were recorded.")
    else:
        hist = holdings.sort_values(
            ["rebalance_quarter", "ticker"], kind="mergesort"
        )
        rows = []
        previous: set[str] = set()
        for quarter, group in hist.groupby("rebalance_quarter", sort=True):
            tickers = set(group["ticker"].astype(str))
            rows.append(
                {
                    "Rebalance": quarter,
                    "Holdings": len(tickers),
                    "New names": len(tickers - previous),
                }
            )
            previous = tickers
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

# --------------------------------------------------------------------------- #
# Methodology & caveats
# --------------------------------------------------------------------------- #
with st.expander("Methodology & caveats"):
    st.markdown(
        """
- **1-quarter reporting lag:** TTM EPS covers quarters t-4…t-1 and share counts
  are as of t-1, so selection never uses future data (no look-ahead).
- **Returns:** split-adjusted price appreciation plus cash dividends (as-reported
  XBRL era; data starts ~2009-2010).
- **Weighting:** equal weight at each rebalance, with drift between rebalances.
- **No frictions:** transaction costs and taxes are ignored.
- **Benchmark:** SPY is used as the S&P 500 total-return proxy (no index
  membership data; ~0.09%/yr expense drag).
- **Delistings:** delisted positions are carried at their last price (no
  delisting-return data available).
- **Survivorship bias:** the universe comes from SEC filers/tickers still
  present in the data.
- **Partial quarter:** the current, incomplete quarter is always excluded.
- **Calendar anchoring:** rebalances happen at calendar quarter ends
  (semiannual = Q2/Q4, annual = Q4).
        """
    )
