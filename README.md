# ValInvest — EDGAR + Yahoo Finance → PostgreSQL

A Dockerized data pipeline that merges the SEC EDGAR XBRL `companyfacts` archive
with Yahoo Finance daily market data into a single quarterly fundamentals view
(`quarterly_fundamentals`) in PostgreSQL 16.

**Status: built, loaded, and independently verified on 2026-09-14.** The stack is
running on this machine, the database is persisted in a Docker volume, and the
view has **599,310 rows** (9,957 tickers, 1962Q1–2026Q3). Nothing needs to be
re-run to use the data — see *Fresh-context quickstart* below. A value-investing
dashboard (portfolio vs S&P 500) launches with a single `./run.sh` — see §15.

---

## 0. Fresh-context quickstart (read this first)

You are re-opening this project with no prior conversation. Everything you need
is on disk and in the running Docker stack. **Do not re-run the full pipeline
unless you actually need fresh data** — a full Yahoo fetch takes ~1–2 hours.

```bash
cd /home/n/Coding/valinvest

# 1. Is the stack up? (db + etl containers)
docker compose ps

# 2. If db is not running, start it (data persists in the valinvest_pgdata volume)
docker compose up -d db etl

# 3. Query the deliverable (column names contain spaces → always quote them)
docker compose exec -T db psql -U valinvest -d valinvest -c \
  "SELECT \"quarter\",\"ticker symbol\",\"price\",\"shares outstanding\",\"EPS\",\"Dividends\" \
   FROM quarterly_fundamentals WHERE \"ticker symbol\"='AAPL' ORDER BY \"quarter\" DESC LIMIT 8;"

# 4. Sanity-verify the load is intact (all checks must pass, ~30 s)
docker compose exec -T db psql -U valinvest -d valinvest < sql/validate.sql

# 5. Launch the value-investing dashboard (fast path; builds the derived
#    tables on first run, ~5 min) and open http://localhost:8501
./run.sh
```

Connection details:

| Item | Value |
| --- | --- |
| Host / port | `localhost:5432` (bound by the `db` container) |
| Database / user / password | `valinvest` / `valinvest` / `valinvest` |
| psql inside Docker | `docker compose exec db psql -U valinvest -d valinvest` |
| Persistence | Docker named volume `valinvest_pgdata` (≈5.2 GB DB) |
| Host requirements | Docker 29.x + Compose v5.x; Linux with ~12 cores / 30 GB RAM |

**Rules that matter for a fresh session**

- All Python must run **inside the `etl` container** (`python:3.13-slim`). The
  host's native Python is intentionally not used.
- SQL files are always piped to psql from the host via **stdin**
  (`docker compose exec -T db psql ... < file.sql`), never `-f host/path`
  (paths are resolved inside the container).
- Never run `docker compose down -v` — that deletes the data volume.
- Files written by the container into `data/` and `logs/` are root-owned; use
  `sudo` or Docker to clean them up.
- `ps`/`pgrep` are **not installed** in `python:3.13-slim`; use
  `docker compose top` to inspect processes.

---

## 1. What was built

```
SEC EDGAR companyfacts.zip ──► etl/edgar_etl.py ──► eps_facts / shares_facts / public_float_facts
SEC ticker map JSON ─────────►                    ──► eps_quarterly / shares_quarterly
                                                  └► company / ticker_map
Yahoo Finance (yfinance) ────► etl/yahoo_etl.py ──► price_daily / price_quarterly
                                                  └► dividend / stock_split / yahoo_fetch_log
                                                             │
                              sql/postprocess.sql ───────────┤ (split-straddle share normalization)
                                                             ▼
                          view: quarterly_fundamentals  ("quarter","ticker symbol","price",
                                                          "shares outstanding","EPS","Dividends")
```

- **`db`** — `postgres:16-alpine`, tuned for bulk loads
  (`synchronous_commit=off`, `shared_buffers=2GB`, `max_wal_size=4GB`),
  healthcheck via `pg_isready`, volume `pgdata`.
- **`etl`** — `python:3.13-slim`, repo bind-mounted at `/work`, idles via
  `sleep infinity`; all jobs are launched with `docker compose exec`.
- Dependencies pinned in `etl/requirements.txt`:
  `yfinance==1.7.0`, `pandas==3.0.5`, `psycopg[binary]~=3.2`,
  `requests~=2.32`.

### Loaded data at a glance (snapshot 2026-09-14)

| Table | Rows | Notes |
| --- | ---: | --- |
| `company` | 19,001 | CIK → entity name (from ticker map + parsed JSONs) |
| `ticker_map` | 10,425 | CIK ↔ EDGAR ticker ↔ Yahoo ticker ↔ exchange (8,020 CIKs) |
| `eps_facts` | 958,843 | all EPS facts (basic + diluted) |
| `shares_facts` | 1,954,140 | all DEI/US-GAAP share-count facts |
| `public_float_facts` | 101,982 | `dei:EntityPublicFloat` |
| `eps_quarterly` | 290,847 | 255,468 reported + 35,379 derived (11,032 CIKs) |
| `shares_quarterly` | 400,741 | 15,286 CIKs; 338,830 dei / 61,911 us-gaap |
| `price_daily` | 34,265,468 | 9,620 tickers, 1962-01-02 … 2026-09-14 |
| `price_quarterly` | 551,639 | one row per (ticker, quarter) |
| `dividend` | 288,036 | 4,195 tickers; ex-dates 1962-01-16 … 2026-09-14 |
| `stock_split` | 9,416 | ratio semantics below |
| `yahoo_fetch_log` | 10,425 | 9,620 ok / 805 empty / 0 error |
| **`quarterly_fundamentals`** | **599,310** | 9,957 tickers, 259 quarters (1962Q1–2026Q3) |

View non-null coverage: `price` 551,639 (92.05%), `shares outstanding` 263,766,
`EPS` 225,793, `Dividends` 228,317. Database size ≈ **5,236 MB** (price_daily
≈4.1 GB). Zero duplicate `(quarter, ticker)`, zero all-NULL rows.

---

## 2. Repository layout

```
/home/n/Coding/valinvest/
├── docker-compose.yml        # db + etl + dashboard services, pgdata volume, healthcheck
├── run.sh                    # single launcher: ETL-if-needed, derived tables, dashboard
├── README.md                 # this document
├── dashboard/
│   ├── app.py                # Streamlit UI (controls, equity curve, holdings)
│   ├── backtest.py           # UI-free engine: panel loader, screen, rebalanced backtest
│   ├── prepare.py            # builds stock_risk_quarter + value_panel (idempotent, --force)
│   └── test_backtest.py      # 19 synthetic unit tests (plain-python runner)
├── etl/
│   ├── Dockerfile            # FROM python:3.13-slim, pip install requirements
│   ├── requirements.txt      # pinned dependencies
│   ├── common.py             # env config, psycopg3 conn, COPY-from-CSV, quarter helpers,
│   │                         # setup_logging, download(url, path, ua), chunked()
│   ├── edgar_etl.py          # parallel parse of companyfacts.zip + derivation + COPY
│   └── yahoo_etl.py          # threaded yfinance fetch, chunked COPY, resume, --replace
├── sql/
│   ├── schema.sql            # all tables/indexes (idempotent CREATE IF NOT EXISTS)
│   ├── postprocess.sql       # after Yahoo load, before view (idempotent)
│   ├── view.sql              # quarterly_fundamentals definition
│   ├── validate.sql          # ~30 assertion blocks + coverage summaries
│   └── dashboard.sql         # stock_risk_quarter + value_panel DDL (§15.3)
├── data/
│   ├── companyfacts.zip             # SEC full XBRL archive, 1,408,785,961 bytes
│   ├── company_tickers_exchange.json# cached CIK→ticker/exchange map
│   ├── collisions.csv               # EPS quarter-resolution audit (~35k groups)
│   ├── yahoo_failures.csv           # per-ticker fetch errors (header only: 0 errors)
│   ├── csv_edgar/                   # per-worker fact shards (retained)
│   ├── csv_meta/                    # ticker/company/quarterly staging CSVs (retained)
│   └── csv_yahoo/                   # transient chunk CSVs (emptied after each chunk)
└── logs/                     # edgar_etl.log, yahoo_etl.log, smoke/restore/replace logs
```

---

## 3. Data sources

### 3.1 SEC EDGAR XBRL `companyfacts`

- URL: `https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip`
  → `data/companyfacts.zip` (1.31 GiB, **20,359** `CIK##########.json` files,
  ~18 GiB uncompressed; one JSON per CIK).
- Structure: `{"cik":N,"entityName":"…","facts":{"dei":{…},"us-gaap":{…}}}`;
  each concept has `.units[unit][]` entries with `start` (durations only), `end`,
  `val`, `accn`, `fy`, `fp`, `form`, `filed`, optional `frame`.
- Extracted concepts:
  - `dei:EntityCommonStockSharesOutstanding`, `dei:EntityPublicFloat`
  - `us-gaap:EarningsPerShareBasic`, `us-gaap:EarningsPerShareDiluted`
    (unit must be exactly `USD/shares`)
  - `us-gaap:CommonStockSharesOutstanding`, `us-gaap:CommonStockSharesIssued`,
    `us-gaap:WeightedAverageNumberOfSharesOutstandingBasic`,
    `us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding`
    (unit `shares`)
- **EDGAR has no per-share closing-price concept** (only share-comp exercise
  prices and aggregate `EntityPublicFloat`). All `price` data is therefore
  Yahoo-sourced. `public_float_facts` is stored as the closest EDGAR
  price-adjacent data but is not used by the view.
- SEC requires a descriptive User-Agent (`SEC_USER_AGENT`, default
  `ValInvest Research research@example.com`) and limits automated access to
  ~10 requests/second; the pipeline downloads once, then parses locally.

### 3.2 CIK → ticker map

`https://www.sec.gov/files/company_tickers_exchange.json` (fields
`cik,name,ticker,exchange`). 10,426 records → **10,425 unique tickers,
8,020 CIKs**; 1,442 CIKs have more than one ticker (GOOG/GOOGL/GOOGM/GOOGN).
Normalization to Yahoo format: `re.sub(r"[./]", "-", ticker.strip().upper())`;
the single placeholder `NONE.` is dropped. `ticker_yahoo` is unique across CIKs
(0 collisions), so the view cannot be duplicated by the map.

### 3.3 Yahoo Finance via `yfinance==1.7.0`

- Universe = all 10,425 mapped tickers (no exchange filter). Result: **9,620 ok,
  805 empty** (delisted/OTC/warrants/units/preferred), **0 errors**; 68.5 min at
  ~2.5 tickers/s with 8 workers and a global 5 req/s token-bucket limiter.
- Per-ticker call (one request returns OHLCV + dividends + splits):
  ```python
  yf.Ticker(sym).history(period="max", interval="1d",
                         auto_adjust=False, actions=True,
                         repair=False, keepna=False)
  ```
  `auto_adjust=False` is required — the default (`True`) drops `Adj Close`.
  Returned columns: `Open, High, Low, Close, Adj Close, Volume, Dividends,
  Stock Splits` (plus `Capital Gains` only for some ETFs, deliberately ignored).
- Index is tz-aware `America/New_York` → normalized to tz-naive; duplicate index
  rows dropped (`keep="last"`). Dividend ex-dates are stored date-only (the raw
  index carries a time component).
- Robustness: 4 attempts with 2/4/8/16 s backoff + jitter; per-chunk (300
  tickers) COPY commits; resume via `yahoo_fetch_log` (`ok`/`empty` skipped);
  `--replace` mode deletes and re-fetches a ticker list (used for repairs).
- **Semantics discovered during verification (important):**
  - Yahoo `Close` is **split-adjusted** to the present (not dividend-adjusted).
  - Yahoo `Dividends` are **split-adjusted** too (AAPL 2020-02-07 = 0.1925 =
    $0.77 ÷ 4; NVDA 2024-03-05 = 0.004 = $0.04 ÷ 10).
  - Yahoo's dividend split-adjustment is *occasionally inconsistent with its own
    split events* (see §9).

---

## 4. Pipeline stages (what each script does)

### 4.1 `etl/edgar_etl.py` (≈132 s full run, 10 workers)

```bash
docker compose exec -T etl python etl/edgar_etl.py [--reset] [--workers N]
    [--ciks 320193,789019] [--zip PATH] [--ticker-map PATH] [--log-file logs/edgar_etl.log]
```

1. Load/download the ticker map; build `company` + `ticker_map` (deduped).
2. `--reset` truncates the 7 EDGAR-owned tables first (idempotent full reload).
3. Multiprocess parse of the zip; each worker opens its own `ZipFile` and writes
   CSV shards (`data/csv_edgar/`). For every fact:
   - `period_start = start or end`, `duration_days`, `is_instant`;
   - **dedup key `(cik, concept, period_start, period_end, unit)` keeping the
     earliest `(filed, accn)`** — i.e. the value **as originally reported**.
     Later filings restate prior periods for splits (AAPL 2020Q2: originally
     2.58, later restated 0.65); earliest-filed keeps one consistent, as-reported
     basis. Missing `filed`/`accn` are treated as +∞.
   - Sanity: facts with `period_end < 1990-01-01` or `> today+1d` are skipped
     (53 facts on the current load) — removes bogus labels like `2207Q1` and
     future-dated facts.
4. COPY shards into `eps_facts`, `shares_facts`, `public_float_facts`; insert
   entity names with `ON CONFLICT DO NOTHING`.
5. Build `eps_quarterly` and `shares_quarterly` in Python (rules in §5).

### 4.2 `etl/yahoo_etl.py` (≈68.5 min full run)

```bash
docker compose exec -T etl python etl/yahoo_etl.py [--reset] [--workers 8]
    [--rate 5] [--chunk 300] [--tickers AAPL,MSFT] [--replace]
    [--log-file logs/yahoo_etl.log]
```

1. Universe from `ticker_map` (DB) or the cached JSON; `--tickers` overrides.
2. `--reset` truncates the 5 Yahoo-owned tables.
3. Resume: tickers with `yahoo_fetch_log.status IN ('ok','empty')` are skipped.
4. Fetch each ticker (thread pool + rate limiter), build rows:
   - `price_daily`: all daily OHLCV rows with valid `Close`;
   - `dividend`: `Dividends != 0` rows → `ex_date`, split-adjusted `amount`,
     as-paid `amount_as_reported`, calendar `quarter`;
   - `stock_split`: `Stock Splits != 0` rows;
   - `price_quarterly`: last trading day per calendar quarter.
5. Chunked CSV → COPY; upsert `yahoo_fetch_log`; log progress
   (`done/total`, ok/empty/error, rows, skipped, elapsed).

### 4.3 `sql/postprocess.sql` (≈30 s, idempotent)

Runs after the Yahoo load and before the view:
- **D2:** deletes `price_quarterly` rows with `close_as_traded < 1e-6` (corrupt
  extreme reverse-split artifacts).
- **D1:** fills `shares_quarterly.shares_at_price_basis` — normalizes the
  as-reported share count to the quarter-end `price_date` split basis when a
  split straddles `(price_date, as_of]` (see §5.3).

### 4.4 `sql/view.sql`

Creates/refreshes `quarterly_fundamentals` (definition in §6).

### 4.5 `sql/validate.sql`

~30 standalone assertion blocks with expected results; all pass on the current
load (see §8). Deliberately written so a human can eyeball each block.

---

## 5. Data semantics (the decisions that matter)

Everything below was verified against raw SEC JSON, live Yahoo data, and SEC
`CommonStockDividendsPerShareDeclared` ground truth. **The view presents a
consistent as-reported / as-traded / as-paid basis** (i.e. historical values as
they were at the time, matching the filings and actual cash paid).

### 5.1 Calendar-quarter labels (`snap_quarter`)

- `quarter` is a calendar-quarter label `YYYYQn` derived from the period-end date.
- 52/53-week filers close on the nearest weekend, so a period can end in the
  first days of the next calendar quarter. A period end with `day <= 7` is
  attributed to the **previous** quarter (`2023-07-01 → 2023Q2`; Apple's fiscal
  Q3). This prevents a fiscal period from stealing the calendar slot of the
  derived Jul–Sep quarter.
- `snap_quarter` is used for **all** `eps_quarterly`/`shares_quarterly` labels
  (including derived Q4). Yahoo price/dividend rows use the plain calendar
  quarter (dates are calendar dates).

### 5.2 EPS (`eps_quarterly`, one row per `(cik, quarter)`)

- Candidates are 3-month duration facts (`75 ≤ duration_days ≤ 115`).
- **Concept is chosen per period**: for each `(cik, period_start, period_end)`
  the Diluted fact wins if it exists for that exact period, else Basic.
- **Q4 derivation** (Q4 is usually not reported as a standalone 3-month fact):
  for each 10-K annual fact (`340 ≤ duration_days ≤ 385`, form starts with
  `10-K`) and each concept separately:
  - if a reported quarterly fact with the same `period_end` exists, use it;
  - else take the quarterly facts of the **same concept** inside the annual
    window; require exactly 3, sorted, `first.start == S`, strictly increasing
    ends, gaps `1..7` days, `last.end < E`, residual `80..130` days;
  - `Q4 = annual − (q1+q2+q3)`, `is_derived = true`.
  - Never mixes Basic/Diluted arithmetic.
- **Resolution of multiple candidates per `(cik, quarter)`** (fiscal/calendar
  boundary collisions): rank by `period_end DESC, is_derived ASC` (reported wins),
  then Diluted, then `filed DESC`, `accn DESC`. Every multi-candidate group is
  logged to `data/collisions.csv` with the winning row’s fields
  (`is_derived` split: 255,468 reported / 35,379 derived; 17,364 Basic-concept
  rows are retained where a CIK/period has no Diluted fact).
- Rows with `|eps| > 1e5` are dropped from `eps_quarterly` (raw `eps_facts`
  untouched).
- **Known approximation:** derived Q4 = annual − 3 quarters can differ by ~$0.01
  from a directly reported Q4 because the annual figure is itself rounded
  (MSFT FY2024 Q4: derived 2.94 vs ~2.95 reported; no standalone fact exists in
  EDGAR to use instead).

### 5.3 Shares outstanding (`shares_quarterly`, one row per `(cik, quarter)`)

Quarter end `E` = calendar end of the label. Candidates tried in bounded tiers:
1. `dei:EntityCommonStockSharesOutstanding` with `E ≤ as_of ≤ E+75d`
   (earliest `as_of`, then latest filed);
2. dei with `E−45d ≤ as_of < E` (latest `as_of`, then latest filed);
3. `us-gaap:CommonStockSharesOutstanding` with `|as_of − E| ≤ 120d`
   (nearest, preferring `as_of ≥ E`, then latest filed);
4. otherwise no row.

Implausible values (`≤ 0` or `> 1e13`) are skipped and the next candidate is
tried. `shares` is the raw as-reported count at `as_of`; `shares_at_price_basis`
(postprocess) converts it to the quarter-end `price_date` split basis when a
split straddles the two dates:

```
price_date > as_of → shares × Π(ratios with as_of < split_date ≤ price_date)
as_of > price_date → shares ÷ Π(ratios with price_date < split_date ≤ as_of)
otherwise          → unchanged
```

Multi-class CIKs use the deterministic `MIN(ticker_yahoo)` split history.
Invalid normalizations fall back to raw `shares` (0 fallbacks currently; 1,013
rows normalized). The view uses `COALESCE(shares_at_price_basis, shares)`.
Example: CHDN 2018Q4 → 40,284,299 post-split cover-date shares ÷ 3 =
13,428,100 → market cap $3.28 B (correct), not $9.83 B.

### 5.4 Price (`price_quarterly`)

- `close_raw` = Yahoo `Close` on the last trading day of the quarter
  (split-adjusted, not dividend-adjusted).
- `close_adj` = Yahoo `Adj Close` for the same day (split + dividend adjusted).
- `close_as_traded` = historical as-traded close:
  `close_raw × Π(split ratio for every split with split_date > price_date)`.
  Forward splits have `ratio > 1` (AAPL 4.0, 7.0; NVDA 10.0), reverse splits
  have `ratio < 1` (NICH 0.000017, CMCT 0.1, DOMH 0.058824).
  Verified: AAPL 2014-06-30 `23.2325 × 4 = 92.93` (actual traded price);
  AAPL 2020-06-30 `91.199997 × 4 = 364.80`.
- A quarter row is written only if `1e-6 ≤ close_as_traded ≤ 1e7`; invalid or
  pathological values are skipped (counted in the ETL summary) and `price_daily`
  keeps the raw vendor values.
- The view’s `"price"` = `COALESCE(close_as_traded, close_raw)`.
- `price_daily` has **no** as-traded column; derive per day via `stock_split` or
  read quarter-level from `price_quarterly.close_as_traded`.

### 5.5 Dividends (`dividend`, PK `(ticker_yahoo, ex_date)`)

- One row per ex-date event; `quarter` = calendar quarter of `ex_date`.
- `amount` = Yahoo `Dividends` (split-adjusted, as delivered).
- `amount_as_reported` = as-paid cash:
  `amount × Π(split ratio for every split with split_date > ex_date)`
  (AAPL 2020-02-07 `0.1925 × 4 = 0.77`; WHLR 2016-11-28
  `6.27e9 × 2.87e-12 = 0.018`; NVDA 2024-03-05 `0.004 × 10 = 0.04`).
- Fetch-time guards: skip non-finite, `amount ≤ 0`, `amount_as_reported > 1e5`,
  or (when a valid as-traded close exists on/before the ex-date)
  `amount_as_reported > as_traded_close`. The `Capital Gains` column is
  deliberately excluded (cash dividends only).
- **The view sums `COALESCE(amount_as_reported, amount)` per quarter, so each
  event is counted exactly once** (PK on `(ticker, ex_date)`; no double
  counting). Full reconciliation against the view is checked in `validate.sql`
  and returned 0 mismatches.
- `VALIDATE`: AAPL calendar-2024 dividends 0.24 / 0.25 / 0.25 / 0.25 (total
  0.99); calendar-2020 as-paid 0.77 / 0.82 / 0.82 / 0.205 (Nov-2020 ex-date is
  post-split).

### 5.6 Basis summary

| Data | Stored basis | Notes |
| --- | --- | --- |
| EDGAR EPS/shares | as originally reported | earliest-filed dedup; shares normalized to price basis in view |
| `price_quarterly.close_raw` | split-adjusted | Yahoo `Close` |
| `price_quarterly.close_adj` | split + dividend adjusted | Yahoo `Adj Close` |
| `price_quarterly.close_as_traded` | **as-traded** (view `price`) | recovered via split factors |
| `dividend.amount` | split-adjusted | Yahoo `Dividends` |
| `dividend.amount_as_reported` | **as-paid** (view `Dividends`) | recovered via split factors |

---

## 6. The view: `quarterly_fundamentals`

Exactly six columns, one row per `(quarter, ticker symbol)`; all component joins
are `LEFT JOIN`s, so a missing component yields NULL rather than dropping the
row. Column names contain spaces and mixed case — **always quote them**.

### 6.1 Definition (`sql/view.sql`)

```sql
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
  SELECT k.cik, tm.ticker_yahoo AS ticker, k.quarter
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
LEFT JOIN eps_quarterly e     ON e.cik = s.cik            AND e.quarter = s.quarter
LEFT JOIN shares_quarterly sh ON sh.cik = s.cik           AND sh.quarter = s.quarter
LEFT JOIN price_quarterly p   ON p.ticker_yahoo = s.ticker AND p.quarter = s.quarter
LEFT JOIN div_q dq            ON dq.ticker_yahoo = s.ticker AND dq.quarter = s.quarter;
```

### 6.2 Column contract

| Column | Type | Meaning |
| --- | --- | --- |
| `"quarter"` | text | Calendar quarter `YYYYQn` (snapped, §5.1) |
| `"ticker symbol"` | text | Yahoo/EDGAR ticker (Yahoo format, e.g. `BRK-B`) |
| `"price"` | numeric | As-traded close of the last trading day in the quarter |
| `"shares outstanding"` | numeric | As-reported count, normalized to the `price_date` split basis |
| `"EPS"` | numeric | As-originally-reported quarterly diluted (else basic) EPS; Q4 derived when needed |
| `"Dividends"` | numeric | Sum of as-paid cash dividends with ex-date in the quarter (counted once) |

### 6.3 Example rows

```
 quarter | ticker symbol |     price      | shares outstanding |   EPS    | Dividends
---------+---------------+----------------+--------------------+----------+-----------
 2020Q1  | AAPL          | 254.289992     |         4334335000 |   2.550000 |  0.770000
 2020Q2  | AAPL          | 364.799988     |         4275634000 |   2.580000 |  0.820000
 2020Q3  | AAPL          | 115.809998     |        17001802000 |   0.730000 |  0.820000
 2020Q4  | AAPL          | 132.690002     |        16788096000 |   1.680000 |  0.205000
 2023Q3  | AAPL          | 171.210007     |        15552752000 |   1.470000 |  0.240000   -- EPS derived
 2024Q4  | GOOG          | 190.440002     |        12211000000 |   2.140000 |  0.200000
 2024Q4  | GOOGL         | 189.300003     |        12211000000 |   2.140000 |  0.200000
 2018Q4  | CHDN          | 243.939996     |          13428100 |   0.280000 |  1.630002   -- see §9 caveat
```

---

## 7. Runbook

### 7.1 Launch / reload

```bash
./run.sh                 # default: build if needed, start db+etl, run the ETL
                         # only when the view is empty, build the derived
                         # dashboard tables if missing (~5 min), start the
                         # dashboard → http://localhost:8501
./run.sh --with-etl      # force the full ETL first (full Yahoo fetch ≈1–2 h)
./run.sh --rebuild-derived  # force rebuild stock_risk_quarter + value_panel
./run.sh --no-build      # skip `docker compose build`
./run.sh --skip-etl      # accepted no-op alias (ETL runs only when the DB is empty)
```

`run.sh` waits for the DB healthcheck; all SQL is piped via stdin. When the ETL
path runs, the scripts are invoked with `--reset`, i.e. a **full rebuild**, not
an incremental update. On a loaded DB the default `./run.sh` is fast and
idempotent. See §15 for the dashboard itself.

### 7.2 Detached full load (recommended for the 1–2 h Yahoo job)

```bash
docker compose build
docker compose up -d db etl
docker compose exec -T db psql -U valinvest -d valinvest -v ON_ERROR_STOP=1 < sql/schema.sql

docker compose exec -d etl sh -c 'mkdir -p logs && python etl/edgar_etl.py --reset > logs/edgar_etl.log 2>&1'
docker compose exec -d etl sh -c 'python etl/yahoo_etl.py --reset > logs/yahoo_etl.log 2>&1'

docker compose top                                    # both processes running?
docker compose exec etl tail -f logs/yahoo_etl.log    # progress lines every chunk
docker compose exec -T db psql -U valinvest -d valinvest -c \
  "SELECT status, count(*) FROM yahoo_fetch_log GROUP BY status;"

# after both finish:
docker compose exec -T db psql -U valinvest -d valinvest -v ON_ERROR_STOP=1 < sql/postprocess.sql
docker compose exec -T db psql -U valinvest -d valinvest -v ON_ERROR_STOP=1 < sql/view.sql
docker compose exec -T db psql -U valinvest -d valinvest -v ON_ERROR_STOP=1 < sql/validate.sql
```

The EDGAR and Yahoo jobs may run concurrently (separate inputs/tables). Yahoo
commits per 300-ticker chunk, so an interrupted run resumes on restart without
`--reset` (already-`ok`/`empty` tickers are skipped).

### 7.3 Targeted repair / re-fetch

```bash
# Re-fetch specific tickers, replacing their rows and bypassing resume:
docker compose exec -T etl python etl/yahoo_etl.py --tickers AAPL,WHLR,CMCT --replace

# EDGAR smoke test on a few CIKs (fast):
docker compose exec -T etl python etl/edgar_etl.py --ciks 320193,789019 --reset
```

### 7.4 Fresh data update

`companyfacts.zip` is re-published nightly and Yahoo data moves daily. To bring
the dataset current:
1. Download a new `data/companyfacts.zip` (same URL; SEC User-Agent required).
2. Rerun the full pipeline (`./run.sh`), which rebuilds everything from scratch.
   EDGAR alone is ~2–3 min; the Yahoo fetch dominates (~1–2 h).

Incremental tricks (use with care):
- EDGAR only: `edgar_etl.py --reset` + `postprocess.sql` + `view.sql` +
  `validate.sql` (adds the new quarter’s EPS/shares).
- Yahoo only for a subset: `yahoo_etl.py --tickers ... --replace`.
- Note: plain reruns of `yahoo_etl.py` without `--reset` **skip already-fetched
  tickers**, so they will not pick up new quarters for them.

### 7.5 Backup / teardown

```bash
# logical backup (optional; DB ≈5 GB)
docker compose exec -T db pg_dump -U valinvest -d valinvest | gzip > valinvest_backup.sql.gz

# stop containers (data survives)
docker compose down

# DESTRUCTIVE: also wipes the pgdata volume (≈5 GB reload)
# docker compose down -v
```

### 7.6 Hosting on Azure (static site + managed PostgreSQL)

A complete deployment guide for running the ETL as a scheduled Azure Container
Apps job, the database on PostgreSQL Flexible Server, and the dashboard as a
static SPA (Blob `$web`) backed by a Python Functions API that reuses
`dashboard/backtest.py`: **[`docs/azure-deploy.md`](docs/azure-deploy.md)**.

---

## 8. Validation & known-good anchors

`sql/validate.sql` contains ~30 blocks; all pass on the current load. Key
assertions and expected values:

| Check | Expected |
| --- | --- |
| View columns | exactly 6, names `quarter`, `ticker symbol`, `price`, `shares outstanding`, `EPS`, `Dividends` |
| Duplicate `(quarter,ticker)` in view | 0 |
| All-NULL view rows | 0 |
| Dividend reconciliation (view = `SUM(amount_as_reported)`) | 0 mismatches |
| `dividend` duplicate `(ticker, ex_date)` | 0 |
| Glitch dividends (as-paid `> 1e5` or `>` as-traded ex-date close) | 0 |
| Malformed quarter labels in `eps_quarterly`/`shares_quarterly` | 0 |
| `eps_quarterly` with `|eps| > 1e5` | 0 |
| `shares_quarterly` outside `(0, 1e13]` or `|as_of−E| > 120d` | 0 |
| `price_quarterly.close_as_traded` outside `[1e-6, 1e6]` | 0 (max ≈ 798,442 = BRK-A) |
| Future quarters (`> 2026Q3`) | 0 |
| **AAPL EPS** | 2017Q2=1.67, 2017Q3=2.07; 2020Q1=2.55, Q2=2.58, Q3=0.73, Q4=1.68; 2022Q4=1.88, 2023Q1=1.52, 2023Q2=1.26, 2023Q3=1.47 (derived), 2023Q4=2.18, 2024Q1=1.53, 2024Q2=1.40, 2024Q3=0.97 (derived) |
| **AAPL shares** | 2024Q1=15,334,082,000 (as_of 2024-04-19) … 2024Q4=15,022,073,000 (2025-01-17), all `dei` |
| **AAPL dividends** | 2024: 0.24/0.25/0.25/0.25; 2020 as-paid: 0.77/0.82/0.82/0.205 |
| **AAPL price** | 2024Q4=250.42, 2024Q3=233.00; 2020Q2=364.80, 2020Q1=254.29 |
| Other derived Q4 anchors | MSFT 2024Q2=2.94, JNJ 2024Q4=1.41, COST 2024Q3=5.28 (16-week Q4), DE 2024Q4=4.57 — all `is_derived=true` |
| Multi-class CIK 1652044 | GOOG and GOOGL both present; class-specific price/dividends, shared EPS/shares |
| Split-straddle normalization | 0 unnormalized straddles; CHDN 2018Q4, GIII 2015Q1, HEI 2017Q1, MBIN 2021Q4 normalized to the price-date basis |

Independent verification performed by separate Checker agents confirmed:
extraction fidelity vs raw JSON (counts and values row-for-row), earliest-filed
selection, price values vs live Yahoo × split factors, dividends vs live Yahoo
and SEC declared amounts, market caps, and view semantics. See §11 for the
history.

---

## 9. Known limitations & vendor quirks

1. **EDGAR has no share price.** `price` is entirely Yahoo-sourced; a Yahoo
   outage or a delisted ticker leaves `price` NULL (7.95% of view rows).
2. **805 of 10,425 mapped tickers returned no Yahoo data** (delisted, OTC,
   warrants/units/preferred). Their view rows carry NULL price/dividends.
3. **Yahoo dividend split-adjustment is occasionally inconsistent with its own
   split events.** Example CHDN: its 2023-05-22 2:1 split is not reflected in
   pre-2023 dividends. SEC `us-gaap:CommonStockDividendsPerShareDeclared` gives
   0.357 / 0.382 / 0.409 for FY2022/23/24; Yahoo’s raw `amount` and our
   `amount_as_reported` match SEC for 2023–24, but the 2022 reconstruction is 2×
   the SEC value because the missed split factor was multiplied in. Both bases
   are stored (`amount`, `amount_as_reported`) so either can be audited.
4. **A small residue of implausible dividends remains:** 608 quarter-sums imply
   >15% quarterly yield, 108 imply >50% (max ~19×). Almost all are
   warrants/OTC tickers lacking a contemporaneous price for the
   `as-paid > as-traded close` guard to compare against. Treat extreme implied
   yields with suspicion.
5. **Derived Q4 is an approximation** (annual − 3 quarters; rounding can differ
   by ~$0.01 from a directly reported Q4 — MSFT 2024Q2 2.94 vs ~2.95).
6. **`stock_split.ratio` is `NUMERIC(18,6)`**, so cumulative factors can drift
   by ≤ ~0.0017 absolute vs live full-precision ratios (negligible; noted for
   reproducibility).
7. **EDGAR coverage:** ~23% of CIKs have no usable shares facts and ~42% no
   usable EPS facts, so NULLs in those view columns are expected, not load bugs.
8. **`eps_quarterly`/`shares_quarterly` are keyed `(cik, quarter)`.** Fiscal/calendar
   boundary collisions are resolved per §5.2 and audited in
   `data/collisions.csv`.
9. **`data/csv_edgar/` and `data/csv_meta/` shards and `collisions.csv` are
   retained** (useful for audit); `data/csv_yahoo/` is emptied after each chunk.
10. **One known data nuance:** the 148 quarters whose Yahoo last-day `Close`
    was ≥1e12 (dropped by the numeric guard) may lack `price_quarterly` rows;
    the dump-level cleanup removed 43 such rows whose as-traded value rounded to
    0 at `NUMERIC(28,10)`.
11. **The newest quarter is partial.** At the 2026-09-14 snapshot the current
    quarter (2026Q3) contains prices through the last available trading day and
    any dividends already paid, but EPS/shares only where a filing exists —
    e.g. AAPL 2026Q3 has price 333.08 / shares 14,608,963,000 / dividends 0.27
    and NULL EPS (AAPL’s fiscal Q4 FY2026 had not been filed yet). When
    filtering for complete fundamentals, exclude the current quarter or require
    `"EPS" IS NOT NULL`.

---

## 10. Common queries

```sql
-- Time series for one ticker
SELECT * FROM quarterly_fundamentals
WHERE "ticker symbol" = 'MSFT' ORDER BY "quarter";

-- Latest four quarters for a ticker (note quoted identifiers)
SELECT "quarter","price","shares outstanding","EPS","Dividends"
FROM quarterly_fundamentals
WHERE "ticker symbol" = 'AAPL' ORDER BY "quarter" DESC LIMIT 4;

-- Derived P/E, market cap, quarterly dividend yield
SELECT "quarter","ticker symbol","price","EPS","Dividends",
       CASE WHEN "EPS" > 0 THEN ROUND("price"/"EPS", 1) END                  AS pe,
       ROUND("price" * "shares outstanding" / 1e9, 3)                       AS mktcap_b,
       CASE WHEN "price" > 0 THEN ROUND(100*"Dividends"/"price", 3) END     AS yield_pct
FROM quarterly_fundamentals
WHERE "ticker symbol" = 'AAPL' AND "quarter" >= '2022Q1'
ORDER BY "quarter";

-- Dividend payers in a quarter
SELECT "ticker symbol","Dividends" FROM quarterly_fundamentals
WHERE "quarter" = '2024Q4' AND "Dividends" > 0 ORDER BY "Dividends" DESC LIMIT 20;

-- Coverage by column
SELECT count(*) AS rows,
       count(*) FILTER (WHERE "price" IS NOT NULL)              AS price,
       count(*) FILTER (WHERE "shares outstanding" IS NOT NULL) AS shares,
       count(*) FILTER (WHERE "EPS" IS NOT NULL)                AS eps,
       count(*) FILTER (WHERE "Dividends" IS NOT NULL)          AS divs
FROM quarterly_fundamentals;

-- Sources / provenance
SELECT status, count(*) FROM yahoo_fetch_log GROUP BY status;
SELECT * FROM price_quarterly WHERE ticker_yahoo = 'AAPL' ORDER BY quarter DESC LIMIT 3;
SELECT * FROM dividend WHERE ticker_yahoo = 'AAPL' ORDER BY ex_date DESC LIMIT 6;
SELECT * FROM eps_facts WHERE cik = 320193 AND concept = 'EarningsPerShareDiluted'
ORDER BY period_end DESC LIMIT 6;
```

Remember: `quarterly_fundamentals` has **no `cik` column** — join through
`ticker_map(cik, ticker_yahoo)` if you need CIK or entity names (`company`).

---

## 11. Configuration reference

| Variable | Default | Used by | Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | `postgresql://valinvest:valinvest@db:5432/valinvest` | both ETLs | psycopg3 DSN |
| `SEC_USER_AGENT` | `ValInvest Research research@example.com` | EDGAR download | SEC requires a descriptive UA |
| `EDGAR_ZIP` | `data/companyfacts.zip` | edgar_etl | input archive |
| `TICKER_MAP_URL` | `https://www.sec.gov/files/company_tickers_exchange.json` | both | cached to `TICKER_MAP_PATH` |
| `TICKER_MAP_PATH` | `data/company_tickers_exchange.json` | both | |
| `EDGAR_WORKERS` | `10` | edgar_etl | multiprocessing pool |
| `YF_WORKERS` | `8` | yahoo_etl | thread pool |
| `YF_RATE` | `5` | yahoo_etl | global requests/second (token bucket) |

Compose details: `db` publishes `5432:5432`; `etl` mounts `./:/work` and idles on
`sleep infinity`; healthcheck gate is `depends_on: db: condition: service_healthy`.
The `db` container runs with `synchronous_commit=off`, `shared_buffers=2GB`,
`max_wal_size=4GB` for bulk-load speed.

---

## 12. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `psql: could not connect` | `docker compose up -d db`; check `docker compose ps` health |
| `relation "quarterly_fundamentals" does not exist` | `docker compose exec -T db psql ... < sql/view.sql` (and make sure postprocess ran first) |
| `column "shares_at_price_basis" does not exist` | Run `sql/postprocess.sql` (it adds the column) |
| Yahoo run died mid-way | Rerun **without** `--reset`; resume skips `ok`/`empty` tickers |
| Ticker data corrupt/glitched | `yahoo_etl.py --tickers X,Y --replace`, then re-run postprocess/view/validate |
| `docker compose exec etl ps ...` prints nothing | `ps` isn’t in the slim image; use `docker compose top` |
| Permission denied deleting `data/csv_*`/logs | Files are root-owned (container); use `sudo` or `docker run --rm -v ...` |
| Need to inspect a container process log | `docker compose exec etl tail -f logs/edgar_etl.log` (host logs also in `logs/`) |
| Disk pressure | DB ≈5.2 GB + `data/` ≈1.8 GB; `docker system df`; remove old volumes/images if needed |

---

## 13. Decision log (why the data looks the way it does)

These were hard-won during the build and are recorded so a fresh session does
not have to rediscover them:

1. **EDGAR `companyfacts.zip` was chosen over the quarterly Financial Statement
   Data Sets** — one download, includes `dei` facts and full history, and the
   JSON structure is stable.
2. **Earliest-filed dedup for facts** — later filings restate prior periods for
   splits; earliest-filed produces an as-reported panel (AAPL 2020Q2 = 2.58, not
   the restated 0.65).
3. **Q4 derivation instead of relying on reported Q4 facts** — most 10-Ks don’t
   tag a standalone Q4 3-month fact; the tiling algorithm with guards was
   validated against AAPL 0.97, MSFT 2.94, JNJ 1.41, COST 5.28 (16-week),
   DE 4.57.
4. **`snap_quarter` (first-7-days rule)** — fixes 52/53-week boundary
   misattribution (AAPL period ending 2023-07-01 would otherwise land in
   2023Q3 and collide with the derived Jul–Sep quarter).
5. **Bounded shares tiers** — prevents attaching a share count from a different
   era to a quarter (earlier unbounded fallback produced >11k rows >1 year off).
6. **As-traded price / as-paid dividends recovery** — Yahoo returns
   split-adjusted `Close` and `Dividends`; multiplying by subsequent split
   ratios restores the as-traded/as-paid values that pair consistently with
   as-reported EDGAR EPS/shares. Verified with AAPL 2014/2020, NVDA 2024, WHLR,
   CMCT and SEC declared-dividend ground truth.
7. **Two earlier cleanup passes were reverted** after discovering Yahoo
   dividends are split-adjusted (values like WHLR’s 6.27e9 were legitimate
   events; they were restored by re-fetching 127 tickers).
8. **`shares_at_price_basis` normalization** — fixed 1,013 rows where a split
   straddled quarter-end price and cover-date shares, which made `price ×
   shares` wrong (CHDN 2018Q4 etc.).
9. **Validation-first culture** — every stage has assertions in
   `sql/validate.sql`; any schema or semantics change should add a block there.

---

## 14. Suggested extension points (for building on top)

The view is the intended interface. Natural next steps and their hooks:

- **Valuation screens** — join `quarterly_fundamentals` with `company`/`ticker_map`
  for names/sectors; use `pe`, `yield_pct`, `mktcap_b` as in §10. Remember EPS
  is quarterly (annualize ×4 only with care) and Q4-derived rows exist.
- **Time-series features** — `price_daily` (34M rows, PK `(ticker, date)`) has
  raw split-adjusted OHLCV; add rolling returns via window functions. For
  as-traded daily prices, un-adjust with `stock_split`.
- **Dividend analytics** — `dividend` has both bases; `amount_as_reported` for
  cash-flow style analysis, `amount` to match Yahoo’s published series.
- **EDGAR deep dives** — `eps_facts`/`shares_facts` retain all periods
  (quarterly, YTD, annual) and weighted-average share concepts for TTM/quality
  checks; add new concepts in `edgar_etl.py` (single filter list + COPY schema).
- **If you change the schema**: update `sql/schema.sql`, `sql/postprocess.sql`
  (if it depends on the Yahoo load), `sql/view.sql`, then `sql/validate.sql`.
  Rebuild the view via stdin psql (`< sql/postprocess.sql` then `< sql/view.sql`)
  and refresh the dashboard tables with `./run.sh --rebuild-derived`.
- **Do not change dedup/basis rules casually** — the anchors in §8 are the
  contract; keep them green.

---

## 15. Value investing dashboard (`./run.sh`)

An interactive Streamlit dashboard compares a **configurable value portfolio**
against the **S&P 500 (SPY total return)**, both rebased to $100 at the same
start date, over the full backtestable window (currently **2008Q3–2026Q2**;
EDGAR XBRL coverage is sparse before ~2010, so the earliest rebalances hold only
a handful of names).

```bash
./run.sh                     # build if needed → db+etl → ETL only if DB empty →
                             # derived tables if missing (~5 min) → dashboard
                             # → http://localhost:8501
./run.sh --with-etl          # force the full ETL first (Yahoo ≈1–2 h)
./run.sh --rebuild-derived   # force rebuild stock_risk_quarter + value_panel
./run.sh --no-build          # skip the image build
./run.sh --skip-etl          # accepted no-op alias (ETL runs only when DB empty)
```

`run.sh` is idempotent on the loaded DB (view check → derived-table check →
start the `dashboard` service on the shared `valinvest-etl` image, port 8501).
`docker compose down` stops everything and keeps `pgdata`; never use
`docker compose down -v`.

### 15.1 Controls (sidebar)

| Control | Meaning |
| --- | --- |
| Matching count | Read-only live count ("N value stocks matching") of names passing the current filters; there is no top-N cap |
| Rebalance frequency | Quarterly, Semiannually (calendar Q2/Q4), Annually (Q4) |
| P/E range | TTM P/E, split-adjusted, positive EPS required |
| Market cap range | bounds in $B |
| Min dividend yield | trailing 4 quarters of cash dividends / price |
| Require positive EPS in last 4 quarters | rejects names with a loss quarter (value-trap guard) |
| Volatility filter | max trailing 12-month annualized daily-return volatility (30–80% or no limit) |
| Crash filter | min trailing 12-month price return (−100% = off) |
| Start year | earliest feasible or 2008–2026 |
| Log scale | log y-axis toggle |

### 15.2 Methodology (as implemented)

- **Selection** at rebalance quarter `t` uses only data known by then:
  price(t), TTM EPS over quarters `t-4..t-1` (one-quarter reporting lag, no
  look-ahead), last-known shares as of `≤ t-1`, and trailing 4-quarter
  dividends. All names passing the screens are held, equal-weighted, sorted by
  lowest P/E then ticker only for deterministic ordering (no top-N cap).
- **Returns** are price appreciation + dividends:
  `(close_raw_q + SUM(dividend.amount)_q) / close_raw_{q-1} - 1`, i.e. the
  split-adjusted close and split-adjusted cash dividends. The view's as-traded
  `"price"` / as-paid `"Dividends"` columns are deliberately **not** used for
  return math (they would be wrong across split boundaries, e.g. AAPL 2020Q2→Q3
  would read −68% instead of +27%). EPS and shares are normalized to the present
  split basis with `F_after(filed)` / `F_after(price_date)` (log-space products
  over `stock_split`), so P/E and market cap are split-consistent; e.g. CHDN
  2018Q4 EPS 0.28 → 0.14, not 0.0467.
- **Portfolio**: equal weight at each rebalance, weights drift between
  rebalances; no transaction costs or taxes. Positions that delist mid-hold are
  carried at their last price (`delist_mode='carry'`; delisting returns are not
  in the data).
- **Benchmark**: SPY (S&P 500 ETF) total return via the same formula (≈0.09%/yr
  expense drag vs the index). No index-membership or survivorship correction;
  the universe is the SEC-filer/EDGAR ticker set.
- **Window**: starts at the first calendar rebalance quarter with at least one
  eligible name (2008Q3 for the default screen); the current partial quarter is
  excluded.

### 15.3 Derived tables (`sql/dashboard.sql` + `dashboard/prepare.py`)

| Table | Rows (snapshot) | Content |
| --- | ---: | --- |
| `stock_risk_quarter` | 551,816 | per (ticker, quarter-end): annualized 252-day volatility, 252-day return, distance from 52-week high (daily closes, corrupt values `≤1e-6`/`≥1e7` masked) |
| `value_panel` | 607,414 | full featured panel: split-normalized P/E, market cap, dividend yield, TTM metrics, risk columns |

`dashboard/backtest.py` is UI-free and covered by 19 synthetic unit tests:

```bash
docker compose exec -T etl python dashboard/test_backtest.py   # 19 passed
```

The Streamlit app (`dashboard/app.py`) only renders; the panel read is cached
(~6 s from `value_panel`). If the derived tables are missing the engine falls
back to an on-the-fly computation (~2 min) — `./run.sh --rebuild-derived`
refreshes them after a data reload.

### 15.4 Reference run (reproducibility anchor)

Default screen with `mcap ≥ $2B`, quarterly rebalance, all qualifying names
held: **2008Q3 → 2026Q2, portfolio $537.51 vs S&P 500 $889.43** (CAGR 9.94% vs
13.10%, max drawdown −35.64% vs −30.41%; 71 rebalances, 215 names on average).
This is what this screen actually returns on this data, not investment advice;
low-P/E screens are sensitive to the filters and the rebalance calendar.

---

*Built 2026-09-14. Data as-of: EDGAR `companyfacts` downloaded 2026-09-14;
Yahoo daily history through 2026-09-14; view quarters 1962Q1–2026Q3. All counts
and examples in this document were verified against the live database at that
time. Value dashboard (§15) added 2026-09-15 and independently verified.*
