-- ValInvest dashboard precomputation DDL (idempotent).
--
-- stock_risk_quarter holds trailing 252-trading-day risk metrics sampled at the
-- last available observation of each calendar quarter:
--   vol_252d      : annualized stdev of daily returns over the trailing 252 rows
--   ret_252d      : close / close.shift(252) - 1
--   off_high_252d : close / rolling-252d max - 1
-- Returns/vol use price_daily.close (split-adjusted), never the as-traded view.

CREATE TABLE IF NOT EXISTS stock_risk_quarter (
  ticker_yahoo TEXT NOT NULL, quarter TEXT NOT NULL, price_date DATE NOT NULL,
  vol_252d DOUBLE PRECISION, ret_252d DOUBLE PRECISION, off_high_252d DOUBLE PRECISION,
  PRIMARY KEY (ticker_yahoo, quarter));
CREATE INDEX IF NOT EXISTS ix_stock_risk_quarter_quarter ON stock_risk_quarter(quarter);

-- value_panel is the materialized output of backtest.build_features(): the full
-- quarterly panel with split-normalized EPS/shares and the lagged valuation
-- features.  Column order below matches build_features() output exactly (the
-- dense range index -- qidx -- is reset to the first column by reset_index()).
-- dashboard/prepare.py COPYs into it; dashboard/backtest.py::load_panel reads it
-- as a fast path and falls back to on-the-fly computation when it is missing.
CREATE TABLE IF NOT EXISTS value_panel (
  qidx            INTEGER NOT NULL,
  quarter         TEXT NOT NULL,
  ticker          TEXT NOT NULL,
  price_traded    DOUBLE PRECISION,
  shares          DOUBLE PRECISION,
  eps             DOUBLE PRECISION,
  eps_filed       DATE,
  eps_period_end  DATE,
  eps_is_derived  BOOLEAN,
  px_adj          DOUBLE PRECISION,
  price_date      DATE,
  div_adj         DOUBLE PRECISION,
  vol_252d        DOUBLE PRECISION,
  ret_252d        DOUBLE PRECISION,
  off_high_252d   DOUBLE PRECISION,
  cik             BIGINT,
  name            TEXT,
  eps_present     DOUBLE PRECISION,
  shares_present  DOUBLE PRECISION,
  ttm_eps         DOUBLE PRECISION,
  eps4_min        DOUBLE PRECISION,
  ttm_div         DOUBLE PRECISION,
  shares_lag      DOUBLE PRECISION,
  pe              DOUBLE PRECISION,
  mktcap          DOUBLE PRECISION,
  div_yield       DOUBLE PRECISION,
  PRIMARY KEY (ticker, quarter));
CREATE INDEX IF NOT EXISTS ix_value_panel_quarter ON value_panel(quarter);
CREATE INDEX IF NOT EXISTS ix_value_panel_qidx ON value_panel(qidx);
