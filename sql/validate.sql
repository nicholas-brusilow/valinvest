-- ValInvest validation queries.
-- Run with:  docker compose exec -T db psql -U valinvest -d valinvest -v ON_ERROR_STOP=1 < sql/validate.sql
-- or simply: docker compose exec -T db psql -U valinvest -d valinvest < sql/validate.sql
-- Comments state the expected result. These queries are read-only and safe to run
-- on partial data (some rows simply won't match until the relevant ETL finished).

\echo '=== 1. AAPL (cik=320193) EPS: expect 2017Q2=1.670000 (period end 2017-07-01, reported), 2017Q3=2.070000 reported ==='
SELECT quarter, eps, is_derived, concept, period_start, period_end
FROM eps_quarterly
WHERE cik = 320193 AND quarter IN ('2017Q2','2017Q3')
ORDER BY quarter;

\echo '=== 2. AAPL 2022Q4..2024Q3 EPS: expect 2022Q4=1.880000, 2023Q1=1.520000, 2023Q2=1.260000, 2023Q3=1.470000 (derived), 2023Q4=2.180000, 2024Q1=1.530000, 2024Q2=1.400000, 2024Q3=0.970000 (derived) ==='
SELECT quarter, eps, is_derived, period_start, period_end
FROM eps_quarterly
WHERE cik = 320193 AND quarter IN ('2022Q4','2023Q1','2023Q2','2023Q3','2023Q4','2024Q1','2024Q2','2024Q3')
ORDER BY quarter;

\echo '=== 3. Other derived Q4 checks ==='
\echo '--- MSFT (789019) 2024Q2: expect 2.940000 derived=true'
SELECT quarter, eps, is_derived FROM eps_quarterly WHERE cik = 789019 AND quarter = '2024Q2';
\echo '--- JNJ (200406) 2024Q4: expect 1.410000 derived=true'
SELECT quarter, eps, is_derived FROM eps_quarterly WHERE cik = 200406 AND quarter = '2024Q4';
\echo '--- COST (909832) 2024Q3: expect 5.280000 derived=true (16-week Q4)'
SELECT quarter, eps, is_derived FROM eps_quarterly WHERE cik = 909832 AND quarter = '2024Q3';
\echo '--- DE (315189) 2024Q4: expect 4.570000 derived=true'
SELECT quarter, eps, is_derived FROM eps_quarterly WHERE cik = 315189 AND quarter = '2024Q4';

\echo '=== 4. AAPL shares_quarterly: expect 2024Q1=15334082000 (as_of 2024-04-19), 2024Q2=15204137000 (2024-07-19), 2024Q3=15115823000 (2024-10-18), 2024Q4=15022073000 (2025-01-17), all dei ==='
SELECT quarter, shares, source_concept, as_of
FROM shares_quarterly
WHERE cik = 320193 AND quarter IN ('2024Q1','2024Q2','2024Q3','2024Q4')
ORDER BY quarter;

\echo '=== 5. AAPL calendar-2024 dividends: expect Q1=0.240000, Q2=0.250000, Q3=0.250000, Q4=0.250000 (total 0.990000) ==='
SELECT quarter, SUM(amount) AS total
FROM dividend
WHERE ticker_yahoo = 'AAPL' AND quarter IN ('2024Q1','2024Q2','2024Q3','2024Q4')
GROUP BY quarter ORDER BY quarter;

\echo '=== 5b. AAPL calendar-2020 dividends (AS-PAID): expect Q1=0.77, Q2=0.82, Q3=0.82, Q4=0.205 (Nov-2020 ex-date is post-split) ==='
SELECT ex_date, quarter, amount AS split_adjusted, amount_as_reported AS as_paid
FROM dividend
WHERE ticker_yahoo = 'AAPL' AND quarter LIKE '2020%'
ORDER BY ex_date;

\echo '=== 5c. AAPL 2020 quarterly EPS (earliest-filed = as originally reported): expect 2020Q1=2.55, 2020Q2=2.58 (pre-split), 2020Q3=0.73, 2020Q4=1.68 (post-split) ==='
SELECT quarter, eps, is_derived, concept, filed
FROM eps_quarterly
WHERE cik = 320193 AND quarter LIKE '2020%'
ORDER BY quarter;

\echo '=== 6. AAPL as-traded price: 2024Q4=250.42 and 2024Q3=233.00 (unchanged); 2020Q2=364.80, 2020Q1=254.29 (close_raw x 4) ==='
SELECT quarter, close_raw, close_adj, close_as_traded, price_date
FROM price_quarterly
WHERE ticker_yahoo = 'AAPL'
  AND quarter IN ('2020Q1','2020Q2','2020Q3','2024Q3','2024Q4')
ORDER BY quarter;

\echo '=== 6b. Split-straddle normalization: 0 rows where a split lies in (price_date, as_of] and shares were not normalized ==='
SELECT count(*) AS unnormalized_straddles
FROM shares_quarterly sq
JOIN (SELECT cik, MIN(ticker_yahoo) AS ticker FROM ticker_map GROUP BY cik) tk ON tk.cik = sq.cik
JOIN price_quarterly pq ON pq.ticker_yahoo = tk.ticker AND pq.quarter = sq.quarter
WHERE EXISTS (
  SELECT 1 FROM stock_split s WHERE s.ticker_yahoo = tk.ticker
   AND ((pq.price_date > sq.as_of AND s.split_date > sq.as_of AND s.split_date <= pq.price_date)
     OR (sq.as_of > pq.price_date AND s.split_date > pq.price_date AND s.split_date <= sq.as_of)))
AND sq.shares_at_price_basis = sq.shares;

\echo '=== 6c. shares_at_price_basis present, and straddle examples normalized to the price_date basis ==='
SELECT count(*) AS missing_basis FROM shares_quarterly WHERE shares_at_price_basis IS NULL;
-- Written rule: when as_of > price_date, divide by the split ratio in (price_date, as_of].
--   CHDN 2018Q4: 40,284,299 / 3    = 13,428,100  -> true market cap ~3.28B
--   GIII 2015Q1: 44,980,194 / 2    = 22,490,097  -> ~2.53B
--   HEI  2017Q1: 33,763,000 / 1.25 = 27,010,400  -> ~2.36B
--   MBIN 2021Q4: 43,267,776 / 1.5  = 28,845,184  -> ~1.37B
-- (The prompt's 29.48/10.13/3.68/3.07B equal the *un-normalized* value times the
--  ratio, i.e. the inverse direction; the rule above is the economically correct
--  one and reproduces each issuer's actual historical market cap.)
SELECT "ticker symbol","quarter","price","shares outstanding",
       ROUND("price"*"shares outstanding"/1e9,4) AS market_cap_b
FROM quarterly_fundamentals
WHERE ("ticker symbol"='CHDN' AND "quarter"='2018Q4')
   OR ("ticker symbol"='GIII' AND "quarter"='2015Q1')
   OR ("ticker symbol"='HEI'  AND "quarter"='2017Q1')
   OR ("ticker symbol"='MBIN' AND "quarter"='2021Q4')
ORDER BY "ticker symbol";

\echo '=== 7. EPS concept coverage: Basic rows should be in the thousands (was 0), and no |eps| > 1e5 ==='
SELECT concept, count(*) FROM eps_quarterly GROUP BY concept ORDER BY concept;
SELECT count(*) AS eps_over_1e5 FROM eps_quarterly WHERE abs(eps) > 1e5;

\echo '=== 8. Malformed quarter labels in eps_quarterly / shares_quarterly: expect 0 rows each ==='
SELECT quarter, count(*) FROM eps_quarterly
WHERE quarter !~ '^(19|20)[0-9]{2}Q[1-4]$' GROUP BY quarter;
SELECT quarter, count(*) FROM shares_quarterly
WHERE quarter !~ '^(19|20)[0-9]{2}Q[1-4]$' GROUP BY quarter;

\echo '=== 9. shares_quarterly as-of is bounded to +/-120 days of the calendar quarter end: expect 0 rows ==='
SELECT count(*) AS shares_asof_out_of_range
FROM shares_quarterly
WHERE abs(as_of - (
    make_date(substr(quarter,1,4)::int, substr(quarter,6,1)::int*3, 1)
    + interval '1 month - 1 day')::date) > 120;

\echo '=== 10. Implausible shares values: expect 0 rows (shares <= 0 OR shares > 1e13) ==='
SELECT count(*) AS shares_out_of_range
FROM shares_quarterly WHERE shares <= 0 OR shares > 1e13;

\echo '=== 11. View has exactly 6 columns with the exact quoted names ==='
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'quarterly_fundamentals'
ORDER BY ordinal_position;
-- expect exactly: quarter, ticker symbol, price, shares outstanding, EPS, Dividends

\echo '=== 12. No duplicate (quarter, ticker symbol) in the view: expect 0 rows ==='
SELECT "quarter", "ticker symbol", count(*)
FROM quarterly_fundamentals
GROUP BY 1, 2
HAVING count(*) > 1;

\echo '=== 13. No duplicate (ticker, ex_date) in dividend: expect 0 rows ==='
SELECT ticker_yahoo, ex_date, count(*)
FROM dividend
GROUP BY 1, 2
HAVING count(*) > 1;

\echo '=== 14. Sample view rows for AAPL (2020 as-traded + 2024) ==='
SELECT * FROM quarterly_fundamentals
WHERE "ticker symbol" = 'AAPL'
  AND "quarter" IN ('2020Q1','2020Q2','2020Q3','2024Q1','2024Q2','2024Q3','2024Q4')
ORDER BY "quarter";

\echo '=== 15. Table row-count summary ==='
SELECT 'company' AS table_name, count(*) FROM company
UNION ALL SELECT 'ticker_map', count(*) FROM ticker_map
UNION ALL SELECT 'eps_facts', count(*) FROM eps_facts
UNION ALL SELECT 'shares_facts', count(*) FROM shares_facts
UNION ALL SELECT 'public_float_facts', count(*) FROM public_float_facts
UNION ALL SELECT 'eps_quarterly', count(*) FROM eps_quarterly
UNION ALL SELECT 'shares_quarterly', count(*) FROM shares_quarterly
UNION ALL SELECT 'price_daily', count(*) FROM price_daily
UNION ALL SELECT 'dividend', count(*) FROM dividend
UNION ALL SELECT 'stock_split', count(*) FROM stock_split
UNION ALL SELECT 'price_quarterly', count(*) FROM price_quarterly
UNION ALL SELECT 'yahoo_fetch_log', count(*) FROM yahoo_fetch_log
UNION ALL SELECT 'quarterly_fundamentals', count(*) FROM quarterly_fundamentals;

\echo '=== 16. Coverage summary ==='
SELECT
  (SELECT count(*) FROM eps_quarterly)                    AS eps_rows,
  (SELECT count(*) FROM eps_quarterly WHERE is_derived)   AS eps_derived_rows,
  (SELECT count(*) FROM eps_quarterly WHERE concept='us-gaap:EarningsPerShareBasic') AS eps_basic_rows,
  (SELECT count(DISTINCT cik) FROM eps_quarterly)         AS eps_ciks,
  (SELECT count(*) FROM shares_quarterly)                 AS shares_rows,
  (SELECT count(DISTINCT cik) FROM shares_quarterly)      AS shares_ciks,
  (SELECT count(DISTINCT ticker_yahoo) FROM price_daily)  AS price_tickers,
  (SELECT count(DISTINCT ticker_yahoo) FROM dividend)     AS dividend_tickers,
  (SELECT count(DISTINCT ticker_yahoo) FROM price_quarterly) AS price_quarter_tickers;

\echo '=== 17. No all-NULL view rows (price, shares, EPS, Dividends all NULL): expect 0 ==='
SELECT count(*) AS all_null_view_rows
FROM quarterly_fundamentals
WHERE "price" IS NULL AND "shares outstanding" IS NULL
  AND "EPS" IS NULL AND "Dividends" IS NULL;

\echo '=== 18. As-paid dividends are plausible: 0 rows with amount_as_reported > 1e5; max shown ==='
SELECT count(*) FILTER (WHERE amount_as_reported > 100000) AS as_reported_over_1e5,
       max(amount_as_reported) AS max_amount_as_reported
FROM dividend;

\echo '=== 19. Every price_quarterly row has a valid as-traded price in [1e-6, 1e6]: expect 0 invalid, 0 below/above floor ==='
SELECT count(*) AS bad_price_quarterly
FROM price_quarterly WHERE close_as_traded IS NULL OR close_as_traded <= 0;
SELECT count(*) FILTER (WHERE close_as_traded < 1e-6) AS at_below_floor,
       count(*) FILTER (WHERE close_as_traded > 1000000) AS at_over_1e6,
       min(close_as_traded) AS min_at,
       max(close_as_traded) AS max_at
FROM price_quarterly;

\echo '=== 19b. D3 stale price_quarterly rows are absent: expect 0 ==='
SELECT count(*) AS stale_price_rows
FROM price_quarterly
WHERE (ticker_yahoo='ADTX' AND quarter='2020Q4')
   OR (ticker_yahoo='MRDN' AND quarter='2010Q1')
   OR (ticker_yahoo='NUWE' AND quarter='2016Q1')
   OR (ticker_yahoo='XTIA' AND quarter IN ('2012Q3','2013Q1'));

\echo '=== 20. No future quarter labels: max(quarter) must not exceed the current calendar quarter ==='
SELECT max(quarter) AS max_quarter, to_char(now(), 'YYYY"Q"Q') AS current_quarter
FROM quarterly_fundamentals;
SELECT count(*) AS future_quarters
FROM quarterly_fundamentals
WHERE "quarter" > to_char(now(), 'YYYY"Q"Q');

\echo '=== 21. Multi-class tickers (GOOG/GOOGL share CIK 1652044): both present in the view ==='
SELECT ticker_edgar, ticker_yahoo FROM ticker_map WHERE cik = 1652044 ORDER BY ticker_yahoo;
SELECT "ticker symbol", count(*) AS rows
FROM quarterly_fundamentals WHERE "ticker symbol" IN ('GOOG','GOOGL')
GROUP BY 1 ORDER BY 1;
SELECT * FROM quarterly_fundamentals
WHERE "ticker symbol" IN ('GOOG','GOOGL') AND "quarter" = '2024Q4'
ORDER BY "ticker symbol";

\echo '=== 22. No glitch dividends on the AS-PAID basis: amount_as_reported > 1e5 OR > as-traded close on/before ex-date: expect 0 ==='
SELECT count(*) AS glitch_dividends
FROM (
  SELECT d.ticker_yahoo, d.amount_as_reported,
         (SELECT ROUND(pd.close * COALESCE((
             SELECT EXP(SUM(LN(s.ratio))) FROM stock_split s
             WHERE s.ticker_yahoo = d.ticker_yahoo AND s.split_date > pd.date AND s.ratio > 0
           ), 1.0), 10)
          FROM price_daily pd
          WHERE pd.ticker_yahoo = d.ticker_yahoo AND pd.date <= d.ex_date
            AND pd.close > 0 AND pd.close < 1e12
          ORDER BY pd.date DESC LIMIT 1) AS at_close
  FROM dividend d
) x
WHERE x.amount_as_reported > 100000
   OR (x.at_close IS NOT NULL AND x.amount_as_reported > x.at_close);

\echo '=== 22b. Restored dividend events are present (as-reported): WHLR 2016-2017, CMCT 2022, SBLK pre-2016 ==='
SELECT 'WHLR 2016-2017' AS bucket, count(*) FROM dividend
WHERE ticker_yahoo='WHLR' AND ex_date BETWEEN '2016-01-01' AND '2017-12-31'
UNION ALL SELECT 'CMCT 2022', count(*) FROM dividend
WHERE ticker_yahoo='CMCT' AND ex_date BETWEEN '2022-01-01' AND '2022-12-31'
UNION ALL SELECT 'SBLK pre-2016', count(*) FROM dividend
WHERE ticker_yahoo='SBLK' AND ex_date < '2016-01-01';

\echo '=== 22c. VHI/NICH glitch events are gone (as-paid vs as-traded close) ==='
SELECT 'VHI' AS ticker, count(*) AS glitch_rows FROM dividend d
WHERE ticker_yahoo='VHI' AND amount_as_reported > 100000
UNION ALL SELECT 'NICH', count(*) FROM dividend WHERE ticker_yahoo='NICH';

\echo '=== 22d. Available bases documented: split-adjusted amount vs as-paid amount_as_reported (sample) ==='
SELECT ticker_yahoo, ex_date, amount AS split_adjusted, amount_as_reported AS as_paid
FROM dividend
WHERE (ticker_yahoo='AAPL' AND ex_date IN ('2020-02-07','2020-05-08','2020-08-07','2020-11-06'))
   OR (ticker_yahoo='WHLR' AND ex_date='2016-11-28')
   OR (ticker_yahoo='CMCT' AND ex_date='2022-09-30')
   OR (ticker_yahoo='NVDA' AND ex_date='2024-03-05')
ORDER BY ticker_yahoo, ex_date;

\echo '=== 23. View price/dividend/shares coverage ==='
SELECT count(*) AS view_rows,
       count("price") AS price_not_null,
       round(100.0 * count("price") / count(*), 2) AS price_pct_not_null,
       count("shares outstanding") AS shares_not_null,
       count("Dividends") AS dividends_not_null
FROM quarterly_fundamentals;

-- NOTE: collisions.csv (data/collisions.csv) winner consistency is verified
-- out-of-band by the smoke test in etl/edgar_etl.py's audit file; see README.
