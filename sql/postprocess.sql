-- ValInvest post-processing.
--
-- Runs AFTER the Yahoo load (price_quarterly / stock_split must exist) and
-- BEFORE sql/view.sql.  Idempotent; safe to re-run.
--
--  D1: add shares_quarterly.shares_at_price_basis, normalizing the as-reported
--      share count to the quarter-end price_date split basis.
--  D2: drop ultra-tiny corrupt as-traded prices (< 1e-6), consistent with the
--      ETL quarter skip rule (1e-6 <= close_as_traded <= 1e7).
--  D3: purge tickers with corrupt vendor price series (split-adjusted quarterly
--      close jumps > 100x up or < 1/100 down between quarters); mirrors
--      etl/excluded_tickers.txt and the ETL filters.

-- -------------------------------------------------------------------------- #
-- D1 column
-- -------------------------------------------------------------------------- #
ALTER TABLE shares_quarterly ADD COLUMN IF NOT EXISTS shares_at_price_basis NUMERIC(28,0);

-- -------------------------------------------------------------------------- #
-- D2: remove ultra-tiny corrupt as-traded quarterly prices
-- -------------------------------------------------------------------------- #
\echo 'price_quarterly rows with close_as_traded < 1e-6 (deleted):'
DELETE FROM price_quarterly WHERE close_as_traded < 1e-6;

-- -------------------------------------------------------------------------- #
-- D3: purge tickers with corrupt vendor price series
-- -------------------------------------------------------------------------- #
-- A ticker whose split-adjusted quarterly close (price_quarterly.close_raw)
-- jumps more than 100x up, or less than 1/100 down, between consecutive
-- quarters is corrupt vendor data and is removed from the dataset entirely.
-- This is the same authoritative rule stored in etl/excluded_tickers.txt and
-- enforced by the ETLs.  D3 runs BEFORE D1's shares normalization so that
-- MIN(ticker_yahoo) per CIK stays stable across runs.
CREATE TEMP TABLE _excluded_tickers AS
WITH x AS (
  SELECT ticker_yahoo, quarter, close_raw,
         LAG(close_raw) OVER (PARTITION BY ticker_yahoo ORDER BY quarter) AS prev
  FROM price_quarterly WHERE close_raw IS NOT NULL AND close_raw > 0
)
SELECT DISTINCT ticker_yahoo FROM x
WHERE prev > 0 AND (close_raw / prev > 100 OR close_raw / prev < 0.01);

\echo 'corrupt-price tickers excluded (D3):'
SELECT count(*) FROM _excluded_tickers;

DELETE FROM dividend        WHERE ticker_yahoo IN (SELECT ticker_yahoo FROM _excluded_tickers);
DELETE FROM stock_split     WHERE ticker_yahoo IN (SELECT ticker_yahoo FROM _excluded_tickers);
DELETE FROM price_daily     WHERE ticker_yahoo IN (SELECT ticker_yahoo FROM _excluded_tickers);
DELETE FROM price_quarterly WHERE ticker_yahoo IN (SELECT ticker_yahoo FROM _excluded_tickers);
DELETE FROM yahoo_fetch_log WHERE ticker_yahoo IN (SELECT ticker_yahoo FROM _excluded_tickers);
DELETE FROM ticker_map      WHERE ticker_yahoo IN (SELECT ticker_yahoo FROM _excluded_tickers);

DO $$
BEGIN
  IF to_regclass('public.stock_risk_quarter') IS NOT NULL THEN
    DELETE FROM stock_risk_quarter
    WHERE ticker_yahoo IN (SELECT ticker_yahoo FROM _excluded_tickers);
  END IF;
  IF to_regclass('public.value_panel') IS NOT NULL THEN
    DELETE FROM value_panel
    WHERE ticker IN (SELECT ticker_yahoo FROM _excluded_tickers);
  END IF;
END $$;

DROP TABLE _excluded_tickers;

-- -------------------------------------------------------------------------- #
-- D1: normalize shares to the price_date split basis
-- -------------------------------------------------------------------------- #
-- If a split straddles (price_date, as_of], the as-reported share count is on
-- a different basis than the as-traded quarter-end price:
--   * price_date > as_of : shares * product(ratios in (as_of, price_date])
--   * as_of > price_date : shares / product(ratios in (price_date, as_of])
--   * otherwise          : shares unchanged
-- Deterministic ticker for multi-class CIKs: MIN(ticker_yahoo) per CIK.
CREATE TEMP TABLE _shares_norm AS
WITH tk AS (
  SELECT cik, MIN(ticker_yahoo) AS ticker_yahoo
  FROM ticker_map GROUP BY cik
),
pd AS (
  SELECT tk.cik, tk.ticker_yahoo, pq.quarter, pq.price_date
  FROM tk JOIN price_quarterly pq ON pq.ticker_yahoo = tk.ticker_yahoo
),
f AS (
  SELECT sq.cik, sq.quarter, sq.shares, sq.as_of, pd.price_date,
         CASE
           WHEN pd.price_date IS NULL OR sq.as_of IS NULL THEN 1.0
           WHEN pd.price_date > sq.as_of THEN COALESCE((
             SELECT EXP(SUM(LN(s.ratio))) FROM stock_split s
             WHERE s.ticker_yahoo = pd.ticker_yahoo
               AND s.split_date > sq.as_of AND s.split_date <= pd.price_date
               AND s.ratio > 0), 1.0)
           WHEN sq.as_of > pd.price_date THEN 1.0 / NULLIF(COALESCE((
             SELECT EXP(SUM(LN(s.ratio))) FROM stock_split s
             WHERE s.ticker_yahoo = pd.ticker_yahoo
               AND s.split_date > pd.price_date AND s.split_date <= sq.as_of
               AND s.ratio > 0), 1.0), 0)
           ELSE 1.0
         END AS factor
  FROM shares_quarterly sq
  LEFT JOIN pd ON pd.cik = sq.cik AND pd.quarter = sq.quarter
)
SELECT cik, quarter, shares,
       ROUND(shares * factor) AS normalized,
       (shares IS NULL OR factor IS NULL OR ROUND(shares * factor) IS NULL
        OR ROUND(shares * factor) <= 0 OR ROUND(shares * factor) > 1e13) AS fallback
FROM f;

\echo 'shares normalization (total / changed / fallback-to-raw):'
SELECT count(*) AS total,
       count(*) FILTER (WHERE NOT fallback AND normalized IS DISTINCT FROM shares) AS normalized_changed,
       count(*) FILTER (WHERE fallback) AS fallbacks
FROM _shares_norm;

UPDATE shares_quarterly sq
SET shares_at_price_basis = CASE WHEN n.fallback THEN sq.shares ELSE n.normalized END
FROM _shares_norm n
WHERE n.cik = sq.cik AND n.quarter = sq.quarter;

\echo 'shares_quarterly rows missing shares_at_price_basis (expect 0):'
SELECT count(*) FROM shares_quarterly WHERE shares_at_price_basis IS NULL;

DROP TABLE IF EXISTS _shares_norm;
