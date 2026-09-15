CREATE TABLE IF NOT EXISTS company (cik BIGINT PRIMARY KEY, entity_name TEXT);
CREATE TABLE IF NOT EXISTS ticker_map (
  cik BIGINT NOT NULL, ticker_edgar TEXT NOT NULL, ticker_yahoo TEXT NOT NULL, exchange TEXT,
  PRIMARY KEY (cik, ticker_edgar));
CREATE INDEX IF NOT EXISTS ix_tm_yf ON ticker_map(ticker_yahoo);
CREATE TABLE IF NOT EXISTS eps_facts (
  cik BIGINT NOT NULL, concept TEXT NOT NULL, unit TEXT NOT NULL,
  period_start DATE NOT NULL, period_end DATE NOT NULL, duration_days INT, is_instant BOOLEAN NOT NULL,
  value NUMERIC(18,6), accn TEXT, fy INT, fp TEXT, form TEXT, filed DATE, frame TEXT,
  PRIMARY KEY (cik, concept, period_start, period_end));
CREATE TABLE IF NOT EXISTS shares_facts (
  cik BIGINT NOT NULL, concept TEXT NOT NULL, unit TEXT NOT NULL,
  period_start DATE NOT NULL, period_end DATE NOT NULL, duration_days INT, is_instant BOOLEAN NOT NULL,
  value NUMERIC(28,0), accn TEXT, fy INT, fp TEXT, form TEXT, filed DATE, frame TEXT,
  PRIMARY KEY (cik, concept, period_start, period_end));
CREATE TABLE IF NOT EXISTS public_float_facts (
  cik BIGINT NOT NULL, concept TEXT NOT NULL, period_start DATE NOT NULL, period_end DATE NOT NULL,
  is_instant BOOLEAN NOT NULL, value NUMERIC(28,0), accn TEXT, fy INT, fp TEXT, form TEXT, filed DATE,
  PRIMARY KEY (cik, concept, period_start, period_end));
CREATE TABLE IF NOT EXISTS eps_quarterly (
  cik BIGINT NOT NULL, quarter TEXT NOT NULL, eps NUMERIC(18,6), concept TEXT NOT NULL,
  is_derived BOOLEAN NOT NULL DEFAULT FALSE, period_start DATE, period_end DATE NOT NULL,
  filed DATE, accn TEXT, PRIMARY KEY (cik, quarter));
CREATE TABLE IF NOT EXISTS shares_quarterly (
  cik BIGINT NOT NULL, quarter TEXT NOT NULL, shares NUMERIC(28,0),
  shares_at_price_basis NUMERIC(28,0), source_concept TEXT NOT NULL,
  as_of DATE NOT NULL, filed DATE, PRIMARY KEY (cik, quarter));
CREATE TABLE IF NOT EXISTS price_daily (
  ticker_yahoo TEXT NOT NULL, date DATE NOT NULL, open NUMERIC(18,6), high NUMERIC(18,6), low NUMERIC(18,6),
  close NUMERIC(18,6), adj_close NUMERIC(18,6), volume BIGINT,
  PRIMARY KEY (ticker_yahoo, date));
CREATE TABLE IF NOT EXISTS dividend (
  ticker_yahoo TEXT NOT NULL, ex_date DATE NOT NULL, amount NUMERIC(18,6) NOT NULL,
  amount_as_reported NUMERIC(28,10), quarter TEXT NOT NULL,
  currency TEXT, PRIMARY KEY (ticker_yahoo, ex_date));
CREATE INDEX IF NOT EXISTS ix_div_tq ON dividend(ticker_yahoo, quarter);
CREATE TABLE IF NOT EXISTS stock_split (
  ticker_yahoo TEXT NOT NULL, split_date DATE NOT NULL, ratio NUMERIC(18,6) NOT NULL,
  PRIMARY KEY (ticker_yahoo, split_date));
CREATE TABLE IF NOT EXISTS price_quarterly (
  ticker_yahoo TEXT NOT NULL, quarter TEXT NOT NULL, close_raw NUMERIC(18,6), close_adj NUMERIC(18,6),
  close_as_traded NUMERIC(28,10), currency TEXT, price_date DATE NOT NULL,
  PRIMARY KEY (ticker_yahoo, quarter));
CREATE TABLE IF NOT EXISTS yahoo_fetch_log (
  ticker_yahoo TEXT PRIMARY KEY, status TEXT NOT NULL, rows INT, error TEXT, fetched_at TIMESTAMPTZ NOT NULL DEFAULT now());
