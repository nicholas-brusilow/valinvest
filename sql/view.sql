CREATE OR REPLACE VIEW quarterly_fundamentals AS
WITH div_q AS (
  SELECT ticker_yahoo, quarter, SUM(COALESCE(amount_as_reported, amount)) AS dividends
  FROM dividend GROUP BY ticker_yahoo, quarter
),
keys AS (
  SELECT cik, quarter FROM eps_quarterly
  UNION SELECT cik, quarter FROM shares_quarterly
),
spine AS (
  SELECT k.cik,
         tm.ticker_yahoo AS ticker,
         k.quarter
  FROM keys k JOIN ticker_map tm ON tm.cik = k.cik
  UNION
  SELECT tm.cik, p.ticker_yahoo, p.quarter
  FROM price_quarterly p LEFT JOIN ticker_map tm ON tm.ticker_yahoo = p.ticker_yahoo
  UNION
  SELECT tm.cik, d.ticker_yahoo, d.quarter
  FROM dividend d LEFT JOIN ticker_map tm ON tm.ticker_yahoo = d.ticker_yahoo
)
SELECT s.quarter                           AS "quarter",
       s.ticker                            AS "ticker symbol",
       COALESCE(p.close_as_traded, p.close_raw) AS "price",
       COALESCE(sh.shares_at_price_basis, sh.shares) AS "shares outstanding",
       e.eps                               AS "EPS",
       dq.dividends                        AS "Dividends"
FROM spine s
LEFT JOIN eps_quarterly e ON e.cik = s.cik AND e.quarter = s.quarter
LEFT JOIN shares_quarterly sh ON sh.cik = s.cik AND sh.quarter = s.quarter
LEFT JOIN price_quarterly p ON p.ticker_yahoo = s.ticker AND p.quarter = s.quarter
LEFT JOIN div_q dq ON dq.ticker_yahoo = s.ticker AND dq.quarter = s.quarter;
