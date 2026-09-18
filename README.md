# ValInvest

**A self-hosted pipeline and dashboard that turns SEC filings and market data into a quarterly fundamentals database — and backtests a value-investing screen against the S&P 500.**

ValInvest joins two free data sources into PostgreSQL:

- **SEC EDGAR XBRL company facts** — what companies actually reported (EPS and share counts).
- **Yahoo Finance via `yfinance`** — what the market actually paid (daily prices, dividends, stock splits).

A Dockerized ETL (Python + SQL) loads both and produces one analysis-ready view, `quarterly_fundamentals`, with exactly one row per ticker-quarter. On top of it, a Streamlit dashboard screens for low-P/E value stocks, builds an equal-weighted portfolio, and compares it with SPY (S&P 500 total return).

Everything runs locally with Docker Compose. Nothing leaves your machine except the HTTP requests to SEC and Yahoo.

---

## Contents

- [Current state](#current-state)
- [Quick start](#quick-start)
- [What you get](#what-you-get)
- [How it works](#how-it-works)
- [Data semantics](#data-semantics)
- [The dashboard](#the-dashboard)
- [Operations](#operations)
- [Repository layout](#repository-layout)
- [Configuration](#configuration)
- [Validation and known limitations](#validation-and-known-limitations)
- [Common queries](#common-queries)
- [Extending the project](#extending-the-project)
- [Azure deployment](#azure-deployment)

---

## Current state

The stack in this checkout is **loaded and running**. Counts below were verified against the live database on **2026-09-18**.

| Item | Value |
| --- | --- |
| Analysis view | `quarterly_fundamentals` — **588,021 rows**, 9,766 tickers, 1962Q1–2026Q3 |
| Database | PostgreSQL 16, **≈5.5 GB**, persisted in the Docker volume `valinvest_pgdata` |
| Data as of | EDGAR `companyfacts` downloaded 2026-09-14; Yahoo history through 2026-09-14 |
| Dashboard | http://localhost:8501 |
| Tests | 24 backtest tests + 14 settings tests, all passing |

> The newest quarter is partial: prices and paid dividends are current, but EPS/shares exist only where a filing exists. Exclude the current quarter (or require `"EPS" IS NOT NULL`) for complete-fundamentals analysis.

## Quick start

**Already built (this checkout):**

```bash
./run.sh                 # starts db + etl + dashboard; skips the ETL (the view is loaded)
# then open http://localhost:8501
```

`run.sh` is idempotent on a loaded database: it starts the containers, checks the view, builds the derived dashboard tables if missing (~5 min), and launches Streamlit.

**From a fresh clone:** the database volume starts empty, so the first run performs the full load. The SEC archive is gitignored and the ETL does **not** download it — fetch it first (SEC requires a descriptive `User-Agent`):

```bash
git clone <repo-url> valinvest && cd valinvest
mkdir -p data
curl -A "ValInvest Research research@example.com" \
  -o data/companyfacts.zip \
  https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip
./run.sh                 # full pipeline; the Yahoo fetch takes 1–2 hours
```

Prerequisites: Docker Engine with Compose v2 (`docker compose`), ~10 GB free disk (5.5 GB database + 1.8 GB raw data + images). A full load is happiest on a many-core machine with 16 GB+ RAM (the original build used ~12 cores / 30 GB).

**Query the data.** Column names contain spaces and mixed case, so always quote them:

```bash
docker compose exec db psql -U valinvest -d valinvest
```
```sql
SELECT "quarter", "ticker symbol", "price", "shares outstanding", "EPS", "Dividends"
FROM quarterly_fundamentals
WHERE "ticker symbol" = 'AAPL'
ORDER BY "quarter" DESC
LIMIT 8;
```

**Sanity-check the load** (~30 s, read-only). Every block prints its expected result next to the actual value:

```bash
docker compose exec -T db psql -U valinvest -d valinvest < sql/validate.sql
```

---

## What you get

### 1. The view: `quarterly_fundamentals`

Six columns, one row per `(quarter, ticker symbol)`. All source joins are LEFT JOINs, so a missing component yields NULL rather than dropping the row.

| Column | Type | Meaning |
| --- | --- | --- |
| `"quarter"` | text | Calendar quarter `YYYYQn` (see [Quarter labels](#quarter-labels)) |
| `"ticker symbol"` | text | Yahoo-format ticker (e.g. `BRK-B`); EDGAR tickers live in `ticker_map` |
| `"price"` | numeric | As-traded close on the last trading day of the quarter |
| `"shares outstanding"` | numeric | As-reported share count, normalized to the `price` split basis |
| `"EPS"` | numeric | As-originally-reported quarterly diluted (else basic) EPS; Q4 derived when needed |
| `"Dividends"` | numeric | Cash dividends with an ex-date in the quarter, summed as paid |

Example rows (selected from the live view; the database keeps full precision):

| quarter | ticker symbol | price | shares outstanding | EPS | Dividends |
| --- | --- | ---: | ---: | ---: | ---: |
| 2020Q1 | AAPL | 254.289992 | 4,334,335,000 | 2.55 | 0.77 |
| 2020Q2 | AAPL | 364.799988 | 4,275,634,000 | 2.58 | 0.82 |
| 2020Q3 | AAPL | 115.809998 | 17,001,802,000 | 0.73 | 0.82 |
| 2020Q4 | AAPL | 132.690002 | 16,788,096,000 | 1.68 | 0.205 |
| 2023Q3 | AAPL | 171.210007 | 15,552,752,000 | 1.47 *(derived)* | 0.24 |
| 2024Q4 | GOOG | 190.440002 | 12,211,000,000 | 2.14 | 0.20 |
| 2024Q4 | GOOGL | 189.300003 | 12,211,000,000 | 2.14 | 0.20 |
| 2018Q4 | CHDN | 243.939996 | 13,428,100 *(split-normalized)* | 0.28 | 1.630002 |
| 2026Q3 | AAPL | 333.079987 | 14,608,963,000 | NULL *(not filed yet)* | 0.27 |

The view deliberately has **no `cik` column** — join through `ticker_map(cik, ticker_yahoo)` if you need CIKs or entity names from `company`.

<details>
<summary>View definition (<code>sql/view.sql</code>)</summary>

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

</details>

### 2. The raw tables

Everything the view is built from is queryable:

| Table | Rows | Content |
| --- | ---: | --- |
| `company` | 19,001 | CIK → entity name |
| `ticker_map` | 10,234 | CIK ↔ EDGAR ticker ↔ Yahoo ticker ↔ exchange |
| `eps_facts` | 958,843 | Every EPS fact extracted from EDGAR (basic + diluted) |
| `shares_facts` | 1,954,140 | Every DEI/US-GAAP share-count fact |
| `public_float_facts` | 101,982 | `dei:EntityPublicFloat` (not used by the view) |
| `eps_quarterly` | 290,847 | Derived quarterly EPS (255,468 reported + 35,379 derived; 11,032 CIKs) |
| `shares_quarterly` | 400,741 | Quarterly share counts (15,286 CIKs; 338,830 dei / 61,911 us-gaap) |
| `price_daily` | 33,595,882 | Daily OHLCV, 9,429 tickers, 1962-01-02 … 2026-09-14 |
| `price_quarterly` | 541,006 | One row per (ticker, quarter): raw / adjusted / as-traded close |
| `dividend` | 287,824 | One row per ex-date (4,182 tickers), split-adjusted and as-paid |
| `stock_split` | 9,025 | Split dates and ratios |
| `yahoo_fetch_log` | 10,234 | 9,429 ok / 805 empty / 0 errors |
| `quarterly_fundamentals` | 588,021 | The view |

View coverage: `price` 541,006 rows (92.0%), `shares outstanding` 258,932 (44.0%), `EPS` 223,217 (38.0%), `Dividends` 228,116 (38.8%). Database size ≈ 5,488 MB (`price_daily` alone ≈ 4 GB).

### 3. The Streamlit dashboard

An interactive screen-and-backtest UI — see [The dashboard](#the-dashboard).

### 4. Validation harness

`sql/validate.sql` is ~30 read-only assertions you can run any time; every one passes on the current load.

---

## How it works

```
SEC EDGAR companyfacts.zip ──► etl/edgar_etl.py ──► eps_facts / shares_facts / public_float_facts
SEC ticker map JSON ─────────►                    ──► eps_quarterly / shares_quarterly
                                                  └► company / ticker_map

Yahoo Finance (yfinance) ────► etl/yahoo_etl.py ──► price_daily / price_quarterly
                                                  └► dividend / stock_split / yahoo_fetch_log

                              sql/postprocess.sql ─► share normalization, corrupt-data purge
                              sql/view.sql ────────► quarterly_fundamentals
                              sql/validate.sql ────► assertions + coverage report

                              dashboard/prepare.py ► stock_risk_quarter / value_panel
                              dashboard/app.py ────► Streamlit dashboard :8501
```

| Stage | Script | What it does | Time |
| --- | --- | --- | --- |
| Schema | `sql/schema.sql` | Idempotent table/index DDL | seconds |
| EDGAR ETL | `etl/edgar_etl.py` | Parses the 1.31 GiB archive (20,359 JSON files) with 10 workers; dedupes facts to the earliest filing; derives quarterly EPS and shares; COPYs into PostgreSQL | ~2–3 min |
| Yahoo ETL | `etl/yahoo_etl.py` | Fetches daily history for the mapped tickers (8 workers, global 5 req/s); writes prices, dividends, splits, quarter-end prices; resumable per 300-ticker chunk | ~1–2 h |
| Postprocess | `sql/postprocess.sql` | Normalizes share counts to the price basis; drops corrupt prices and corrupt-price tickers | ~30 s |
| View | `sql/view.sql` | Builds `quarterly_fundamentals` | seconds |
| Validate | `sql/validate.sql` | Prints ~30 checks with expected results | ~30 s |
| Dashboard prep | `dashboard/prepare.py` | Materializes `stock_risk_quarter` + `value_panel` | ~5 min |

### Data sources

**SEC EDGAR `companyfacts`**
`https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip` → `data/companyfacts.zip` (1,408,785,961 bytes; one JSON per CIK). Extracted concepts: `dei:EntityCommonStockSharesOutstanding`, `dei:EntityPublicFloat`, `us-gaap:EarningsPerShareBasic/Diluted`, and several `us-gaap` share-count concepts. SEC requires a descriptive `User-Agent` (`SEC_USER_AGENT`) and limits automated access to ~10 requests/second; the archive is fetched once (by you — `edgar_etl.py` only auto-downloads the CIK→ticker map, not `companyfacts.zip`) and parsed locally. Note that EDGAR has **no per-share closing-price concept**, which is why all price data comes from Yahoo.

**CIK → ticker map**
`https://www.sec.gov/files/company_tickers_exchange.json` (10,426 records), normalized to Yahoo ticker format (`re.sub(r"[./]", "-", TICKER.strip().upper())`).

**Yahoo Finance via `yfinance==1.7.0`**
One `Ticker(sym).history(period="max", interval="1d", auto_adjust=False, actions=True)` call per ticker returns OHLCV plus dividends and splits. `auto_adjust=False` is required — the default drops `Adj Close`. The ETL retries with backoff, rate-limits globally, commits per chunk, and resumes from `yahoo_fetch_log` without re-fetching completed tickers.

---

## Data semantics

This is the part worth understanding before trusting a number: the pipeline stores each value on the basis that makes it historically meaningful.

### Quarter labels

`quarter` is the calendar quarter of the period-end date. 52/53-week filers (Apple, Costco) close on the nearest weekend, so their period can end in the first days of the next calendar quarter. Any period ending on days 1–7 is attributed to the **previous** quarter — Apple's 2023-07-01 fiscal Q3 becomes `2023Q2`, not `2023Q3`. This keeps fiscal periods from stealing the next calendar slot. Yahoo price/dividend rows use plain calendar quarters because those are real calendar dates.

### EPS (`eps_quarterly`, one row per CIK-quarter)

- Candidates are 3-month facts (75–115 days); for the same period, Diluted wins over Basic.
- **Q4 is usually not reported standalone.** It is derived as `annual 10-K − (Q1 + Q2 + Q3)` using same-concept quarters that exactly tile the annual window; derived rows carry `is_derived = true`. Because the annual figure is rounded, a derived Q4 can differ by ~$0.01 from a directly reported one (MSFT 2024Q2: derived 2.94 vs ~2.95; no standalone EDGAR fact exists to use instead).
- **Facts are deduplicated to the earliest filing** per `(cik, concept, period, unit)` — i.e. the value as originally reported. Apple's 2020Q2 EPS is stored as 2.58 (pre-split, as filed), not the later restated 0.65.
- When fiscal/calendar boundaries produce several candidates for one quarter, a deterministic ranking picks the winner (`period_end DESC`, then reported over derived, then Diluted, then latest filed). Every group is audited in `data/collisions.csv`.

### Shares outstanding (`shares_quarterly`, one row per CIK-quarter)

Candidates are tried in tiers: `dei:EntityCommonStockSharesOutstanding` first (up to 75 days after quarter end, then up to 45 days before), then `us-gaap:CommonStockSharesOutstanding` within ±120 days. Implausible counts (≤ 0 or > 1e13) are skipped and the next candidate is tried.

Reported counts are on the filing's split basis. `shares_at_price_basis` converts them to the quarter-end price date's basis when a split falls between the two dates:

```
price_date > as_of → shares × Π(ratios with as_of < split_date ≤ price_date)
as_of > price_date → shares ÷ Π(ratios with price_date < split_date ≤ as_of)
otherwise          → unchanged
```

Example: CHDN 2018Q4 is reported as 40,284,299 post-split cover-date shares → normalized to 13,428,100 → market cap $3.28 B (correct), not $9.83 B. The view uses the normalized value (958 rows currently have a normalized count different from the reported one).

### Price (`price_quarterly`)

- `close_raw` — Yahoo `Close` on the quarter's last trading day (**split-adjusted**, not dividend-adjusted).
- `close_adj` — Yahoo `Adj Close` for the same day (split **and** dividend adjusted).
- `close_as_traded` — the historical as-traded close, recovered by multiplying by the split ratios dated after the price date. AAPL 2014-06-30: `23.2325 × 4 = 92.93`; AAPL 2020-06-30: `91.199997 × 4 = 364.80`.
- The view's `"price"` is `COALESCE(close_as_traded, close_raw)`. Quarter rows are only written for sane values (`1e-6 ≤ close_as_traded ≤ 1e7`); `price_daily` keeps the raw vendor values.

### Dividends (`dividend`, one row per ex-date)

- `amount` — Yahoo's `Dividends` (**split-adjusted**, as delivered).
- `amount_as_reported` — as-paid cash, recovered with later split ratios: AAPL 2020-02-07 `0.1925 × 4 = 0.77`; NVDA 2024-03-05 `0.004 × 10 = 0.04`.
- The view sums `amount_as_reported` per quarter, so every event is counted exactly once (primary key on `(ticker, ex_date)`). Fetch-time guards drop non-finite, ≤ 0, absurd (> 1e5), or larger-than-share-price values. Capital-gains distributions are deliberately excluded.
- Reconciliation against SEC `CommonStockDividendsPerShareDeclared` returned 0 mismatches.

### Basis summary

| Data | Stored basis | Notes |
| --- | --- | --- |
| EDGAR EPS/shares | As originally reported | Earliest-filed dedup; shares normalized to price basis in the view |
| `price_quarterly.close_raw` | Split-adjusted | Yahoo `Close` |
| `price_quarterly.close_adj` | Split + dividend adjusted | Yahoo `Adj Close` |
| `price_quarterly.close_as_traded` | **As-traded** (view `"price"`) | Recovered via split factors |
| `dividend.amount` | Split-adjusted | Yahoo `Dividends` |
| `dividend.amount_as_reported` | **As-paid** (view `"Dividends"`) | Recovered via split factors |

The point of the as-reported / as-traded / as-paid basis is that price, shares, EPS, and dividends can be compared consistently at any point in history instead of mixing split bases (which is why the backtest never uses the view's as-traded columns for return math — see [Methodology](#methodology)).

---

## The dashboard

```bash
./run.sh                 # http://localhost:8501
```

The UI compares a configurable value portfolio with the S&P 500, both rebased to $100 at the same start date, over the backtestable window (currently up to 2026Q2 — the partial current quarter is excluded). All return math and screening logic live in `dashboard/backtest.py`, which is UI-free and unit-tested; `dashboard/app.py` only renders.

### Controls (sidebar)

| Control | Default | Meaning |
| --- | --- | --- |
| Matching count | — | Live read-only count of names passing the current filters; there is no top-N cap |
| Rebalance frequency | Quarterly | Quarterly, Semiannually (calendar Q2/Q4), or Annually (Q4) |
| P/E range | 0–15 | Trailing-12-month P/E, split-adjusted; positive EPS required |
| Market cap range | $0–100 M | Bounds in $M |
| Min dividend yield | 0% | Trailing 4 quarters of cash dividends ÷ price |
| Require positive EPS in last 4 quarters | On | Value-trap guard: rejects names with a loss quarter |
| Volatility filter | No limit | Max trailing 12-month annualized daily-return volatility (30–80%) |
| Crash filter | Off (−100%) | Min trailing 12-month price return |
| Start year | Earliest available | 2008–2026, or the earliest feasible rebalance |
| Log scale | Off | Log y-axis toggle |
| Save / Apply settings | — | Persists screening criteria to `dashboard/saved_settings.json` (override path with `VALINVEST_SETTINGS_PATH`) |

### Methodology

- **No look-ahead.** At rebalance quarter `t`, the TTM EPS uses quarters `t−4 … t−1` (one-quarter reporting lag), shares are the last count known by `t−1`, and price is the quarter-end close.
- **Selection:** all names passing the screens are held, equal-weighted; sorting by lowest P/E then ticker is only for deterministic ordering.
- **Returns:** price appreciation + dividends, computed from split-adjusted `close_raw` and split-adjusted `dividend.amount`. The view's as-traded `"price"`/as-paid `"Dividends"` are deliberately not used for return math — they would break across split boundaries (AAPL 2020Q2→Q3 would read −68% instead of +27%). EPS and shares are re-normalized to the present split basis before P/E and market cap are computed.
- **Portfolio:** equal weight at each rebalance, weights drift between rebalances. No transaction costs or taxes. Positions that delist mid-hold are carried at their last price (no delisting returns in the data).
- **Benchmark:** SPY total return via the same formula. The universe is the EDGAR filer/ticker set, so index membership and survivorship are not modeled.
- **Head-to-head:** the app also reports each quarter's portfolio vs S&P return, the difference, win counts, and mean/median quarterly returns.

### Reference runs

Computed on the current database (2026-09-18), $100 start, dividends included:

| Screen | Window | Portfolio | S&P 500 (SPY) | CAGR | Max drawdown |
| --- | --- | ---: | ---: | ---: | ---: |
| App defaults: P/E 0–15, mcap $0–100 M, positive EPS ×4, quarterly | 2010Q1–2026Q2 | $2,549.20 | $851.63 | 22.05% vs 14.09% | −22.00% vs −23.92% |
| Same, but mcap ≥ $2 B | 2008Q3–2026Q2 | $548.58 | $889.43 | 10.07% vs 13.10% | −35.64% vs −30.41% |

These are what these screens actually return on this dataset — not investment advice. Low-P/E screens are sensitive to the filters and the rebalance calendar.

### Derived tables and tests

`dashboard/prepare.py` builds two tables that make the app fast:

| Table | Rows | Content |
| --- | ---: | --- |
| `stock_risk_quarter` | 541,123 | Per (ticker, quarter): annualized 252-day volatility, 252-day return, distance from 52-week high |
| `value_panel` | 595,899 | The full featured panel: split-normalized P/E, market cap, dividend yield, TTM metrics, risk columns |

If the tables are missing, the engine falls back to computing the panel on the fly (~2 min). Rebuild with `./run.sh --rebuild-derived` after a data reload. Unit tests:

```bash
docker compose exec -T etl python dashboard/test_backtest.py   # 24 passed
docker compose exec -T etl python dashboard/test_settings.py   # 14 passed
```

---

## Operations

### `./run.sh` flags

| Flag | Effect |
| --- | --- |
| *(none)* | Build if needed, start `db` + `etl`, run the ETL only if the view is empty, build derived tables if missing, start the dashboard |
| `--with-etl` | Force the full ETL first (full rebuild; Yahoo ≈1–2 h) |
| `--rebuild-derived` | Force rebuild of `stock_risk_quarter` + `value_panel` (~5 min) |
| `--no-build` | Skip `docker compose build` |
| `--skip-etl` | Accepted no-op alias; the ETL only runs when the DB is empty anyway |

### Detached full load (recommended for the long Yahoo job)

```bash
docker compose build
docker compose up -d db etl
docker compose exec -T db psql -U valinvest -d valinvest -v ON_ERROR_STOP=1 < sql/schema.sql

docker compose exec -d etl sh -c 'mkdir -p logs && python etl/edgar_etl.py --reset > logs/edgar_etl.log 2>&1'
docker compose exec -d etl sh -c 'python etl/yahoo_etl.py --reset > logs/yahoo_etl.log 2>&1'

docker compose top                                    # are both jobs running?
docker compose exec etl tail -f logs/yahoo_etl.log    # progress every 300-ticker chunk

# When both finish:
docker compose exec -T db psql -U valinvest -d valinvest -v ON_ERROR_STOP=1 < sql/postprocess.sql
docker compose exec -T db psql -U valinvest -d valinvest -v ON_ERROR_STOP=1 < sql/view.sql
docker compose exec -T db psql -U valinvest -d valinvest -v ON_ERROR_STOP=1 < sql/validate.sql
```

EDGAR and Yahoo can run concurrently (separate inputs and tables). Yahoo commits every 300 tickers, so an interrupted run resumes where it left off **without** `--reset` — already-`ok`/`empty` tickers are skipped. When the load is done, `./run.sh` starts the dashboard (and builds the derived tables on first run).

### Repair / targeted re-fetch

```bash
# Re-fetch specific tickers, replacing their rows and bypassing resume:
docker compose exec -T etl python etl/yahoo_etl.py --tickers AAPL,WHLR,CMCT --replace
# then re-run postprocess.sql, view.sql and ./run.sh --rebuild-derived

# Fast EDGAR smoke test for a few CIKs:
docker compose exec -T etl python etl/edgar_etl.py --ciks 320193,789019 --reset
```

### Staying current

`companyfacts.zip` is republished nightly and Yahoo data moves daily.

1. Download a fresh `data/companyfacts.zip` yourself from the URL in [Data sources](#data-sources) — SEC requires a descriptive User-Agent. The ETL does not download the archive (it only auto-fetches the CIK→ticker map).
2. Run `./run.sh --with-etl` to rebuild everything from scratch. EDGAR alone is ~2–3 min; the Yahoo fetch dominates.

Incremental shortcuts, use with care:

- EDGAR only: `edgar_etl.py --reset`, then `postprocess.sql` → `view.sql` → `validate.sql` (adds the new quarter's EPS/shares).
- Yahoo only for a subset: `yahoo_etl.py --tickers ... --replace`.
- A plain `yahoo_etl.py` rerun **skips** already-fetched tickers, so it will not pick up new quarters for them.

### Backup and teardown

```bash
# Logical backup (optional; database ≈5 GB)
docker compose exec -T db pg_dump -U valinvest -d valinvest | gzip > valinvest_backup.sql.gz

# Stop containers; the data volume survives
docker compose down

# DESTRUCTIVE — also deletes the pgdata volume and loses the load:
# docker compose down -v
```

---

## Repository layout

```
valinvest/
├── docker-compose.yml          # db + etl + dashboard services, pgdata volume, healthcheck
├── run.sh                      # single launcher: ETL-if-needed, derived tables, dashboard
├── dashboard/
│   ├── app.py                  # Streamlit UI (controls, equity curve, holdings, comparison)
│   ├── backtest.py             # UI-free engine: panel loader, screen, rebalanced backtest
│   ├── prepare.py              # builds stock_risk_quarter + value_panel (idempotent, --force)
│   ├── settings_store.py       # UI-free settings persistence/validation
│   ├── test_backtest.py        # 24 synthetic unit tests
│   └── test_settings.py        # 14 settings-persistence tests
├── etl/
│   ├── Dockerfile              # FROM python:3.13-slim
│   ├── requirements.txt        # yfinance, pandas, psycopg, requests, streamlit, plotly
│   ├── common.py               # config, psycopg3 helpers, COPY-from-CSV, quarter helpers
│   ├── edgar_etl.py            # EDGAR parse + quarterly derivation + COPY
│   ├── yahoo_etl.py            # threaded yfinance fetch + chunked COPY + resume
│   └── excluded_tickers.txt    # corrupt-price tickers (see limitations)
├── sql/
│   ├── schema.sql              # all tables/indexes (idempotent)
│   ├── postprocess.sql         # share normalization + corrupt-data purge (idempotent)
│   ├── view.sql                # quarterly_fundamentals definition
│   ├── validate.sql            # ~30 assertions + coverage summaries
│   └── dashboard.sql           # stock_risk_quarter + value_panel DDL
├── data/                       # inputs and staging (gitignored, ~1.8 GB)
│   ├── companyfacts.zip        # SEC full XBRL archive (1.31 GiB)
│   ├── company_tickers_exchange.json
│   ├── collisions.csv          # EPS quarter-resolution audit
│   ├── yahoo_failures.csv      # per-ticker fetch errors (header only: 0 errors)
│   ├── csv_edgar/  csv_meta/   # retained staging shards
│   └── csv_yahoo/              # transient chunk CSVs (emptied after each chunk)
├── logs/                       # edgar_etl.log, yahoo_etl.log, ...
├── docs/
│   └── azure-deploy.md         # Azure deployment guide
└── README.md
```

---

## Configuration

| Variable | Default | Used by | Notes |
| --- | --- | --- | --- |
| `DATABASE_URL` | `postgresql://valinvest:valinvest@db:5432/valinvest` | ETLs, dashboard | psycopg3 DSN |
| `SEC_USER_AGENT` | `ValInvest Research research@example.com` | EDGAR download | SEC requires a descriptive User-Agent |
| `EDGAR_ZIP` | `data/companyfacts.zip` | `edgar_etl.py` | Input archive |
| `TICKER_MAP_URL` | `https://www.sec.gov/files/company_tickers_exchange.json` | both ETLs | Cached to `TICKER_MAP_PATH` |
| `TICKER_MAP_PATH` | `data/company_tickers_exchange.json` | both ETLs | |
| `EDGAR_WORKERS` | `10` | `edgar_etl.py` | Multiprocessing pool |
| `YF_WORKERS` | `8` | `yahoo_etl.py` | Thread pool |
| `YF_RATE` | `5` | `yahoo_etl.py` | Global requests/second (token bucket) |
| `VALINVEST_SETTINGS_PATH` | `dashboard/saved_settings.json` | dashboard | Where saved screens live |

Compose details: `db` publishes `5432:5432` and runs with `synchronous_commit=off`, `shared_buffers=2GB`, `max_wal_size=4GB` for bulk-load speed. `etl` bind-mounts the repo at `/work` and idles on `sleep infinity`; jobs are launched with `docker compose exec`. The `dashboard` service runs Streamlit on port 8501 using the same image as `etl`.

**Gotchas for the container environment**

- Run all Python inside the `etl` container (the host's Python is intentionally not used).
- Pipe SQL to psql via stdin (`docker compose exec -T db psql ... < file.sql`), never `-f /host/path` — paths resolve inside the container.
- Files written by the container into `data/` and `logs/` are root-owned; clean them up with `sudo` or Docker.
- `ps`/`pgrep` are not installed in the slim image; use `docker compose top`.

---

## Validation and known limitations

### What `sql/validate.sql` covers

All ~30 blocks pass on the current load. Key anchors (one row notes an invariant checked directly rather than in `validate.sql`):

| Check | Expected |
| --- | --- |
| View schema | Exactly 6 columns with the documented names |
| Duplicate `(quarter, ticker)` in the view | 0 |
| All-NULL view rows | 0 |
| View dividends vs `SUM(amount_as_reported)` *(checked by direct query, not in `validate.sql`)* | 0 mismatches |
| Duplicate `(ticker, ex_date)` dividends | 0 |
| Malformed quarter labels in `eps_quarterly` / `shares_quarterly` | 0 |
| EPS values with absolute value > 1e5, shares outside `(0, 1e13]`, as-traded price out of range | 0 |
| Future quarters (> 2026Q3) | 0 |
| AAPL EPS | 2020Q1 2.55, Q2 2.58, Q3 0.73, Q4 1.68; 2023Q3 1.47 (derived) |
| AAPL shares | 2024Q4 = 15,022,073,000 (all `dei`) |
| AAPL dividends | 2024: 0.24/0.25/0.25/0.25; 2020 as-paid: 0.77/0.82/0.82/0.205 |
| AAPL price | 2024Q4 250.42, 2024Q3 233.00; 2020Q2 364.80 |
| Other derived Q4 anchors | MSFT 2024Q2 2.94, JNJ 2024Q4 1.41, COST 2024Q3 5.28 (16-week Q4), DE 2024Q4 4.57 |
| Multi-class CIK 1652044 | GOOG and GOOGL both present with class-specific price/dividends, shared EPS/shares |
| Split-straddle normalization | 0 unnormalized straddles (CHDN, GIII, HEI, MBIN verified) |

### Known limitations

1. **No share price in EDGAR.** `"price"` is entirely Yahoo-sourced; a Yahoo outage or a delisted ticker leaves it NULL (8.0% of view rows).
2. **805 of 10,234 mapped tickers returned no Yahoo data** (delisted, OTC, warrants/units/preferred); their view rows carry NULL price/dividends.
3. **Yahoo's dividend split-adjustment is occasionally inconsistent with its own split events.** Example: CHDN's 2023-05-22 2:1 split is not reflected in its pre-2023 dividends, so `amount_as_reported` for 2022 is 2× the SEC-declared value. Both `amount` and `amount_as_reported` are stored so either can be audited.
4. **A residue of extreme implied yields remains:** 595 quarter-sums imply > 15% quarterly yield and 103 imply > 50% (max 3.63×). Each has a contemporaneous price for the as-paid-vs-price guard to compare against, and the guard passes, so these are not missing-price artifacts — but treat extreme yields with suspicion.
5. **Derived Q4 is an approximation** (annual − 3 quarters; rounding can differ by ~$0.01).
6. **Split ratios are stored as `NUMERIC(18,6)`**, so cumulative factors can drift by ≤ ~0.0017 absolute versus full precision.
7. **EDGAR coverage is uneven:** of the 19,001 CIKs in `company`, `shares_quarterly` covers 15,286 (~80%) and `eps_quarterly` covers 11,032 (~58%), so NULLs in those view columns are expected, not load bugs.
8. **`eps_quarterly` and `shares_quarterly` are keyed `(cik, quarter)`.** Fiscal/calendar collisions are resolved deterministically and audited in `data/collisions.csv`.
9. **The staging shards are retained** (`data/csv_edgar/`, `data/csv_meta/`, `data/collisions.csv`) for audit; `data/csv_yahoo/` is emptied after each chunk.
10. **The newest quarter is partial.** At the current snapshot (2026Q3) prices and paid dividends are current, but EPS/shares only exist where a filing exists.
11. **Corrupt-price tickers are excluded.** Any ticker whose split-adjusted quarterly close jumps > 100× up or < 1/100 down between consecutive quarters is treated as corrupt vendor data and removed everywhere. The list (`etl/excluded_tickers.txt`, **191 tickers**, e.g. WFCNP, AMCCF) is re-applied automatically by both ETLs and by `postprocess.sql` (D3), so excluded names are never re-ingested.

---

## Common queries

```sql
-- Time series for one ticker
SELECT * FROM quarterly_fundamentals
WHERE "ticker symbol" = 'MSFT' ORDER BY "quarter";

-- Latest four quarters (note the quoted identifiers)
SELECT "quarter", "price", "shares outstanding", "EPS", "Dividends"
FROM quarterly_fundamentals
WHERE "ticker symbol" = 'AAPL' ORDER BY "quarter" DESC LIMIT 4;

-- Derived P/E, market cap and dividend yield
SELECT "quarter", "price", "EPS", "Dividends",
       CASE WHEN "EPS" > 0 THEN ROUND("price" / "EPS", 1) END              AS pe,
       ROUND("price" * "shares outstanding" / 1e9, 3)                      AS mktcap_b,
       CASE WHEN "price" > 0 THEN ROUND(100 * "Dividends" / "price", 3) END AS yield_pct
FROM quarterly_fundamentals
WHERE "ticker symbol" = 'AAPL' AND "quarter" >= '2022Q1'
ORDER BY "quarter";

-- Dividend payers in a quarter
SELECT "ticker symbol", "Dividends" FROM quarterly_fundamentals
WHERE "quarter" = '2024Q4' AND "Dividends" > 0
ORDER BY "Dividends" DESC LIMIT 20;

-- Column coverage
SELECT count(*) AS rows,
       count(*) FILTER (WHERE "price" IS NOT NULL)              AS price,
       count(*) FILTER (WHERE "shares outstanding" IS NOT NULL) AS shares,
       count(*) FILTER (WHERE "EPS" IS NOT NULL)                AS eps,
       count(*) FILTER (WHERE "Dividends" IS NOT NULL)          AS divs
FROM quarterly_fundamentals;

-- Provenance
SELECT status, count(*) FROM yahoo_fetch_log GROUP BY status;
SELECT * FROM dividend WHERE ticker_yahoo = 'AAPL' ORDER BY ex_date DESC LIMIT 6;
```

To attach CIK, entity name or exchange, join through the map:

```sql
SELECT qf."quarter", qf."ticker symbol", c.entity_name, tm.exchange
FROM quarterly_fundamentals qf
JOIN ticker_map tm ON tm.ticker_yahoo = qf."ticker symbol"
JOIN company    c  ON c.cik = tm.cik
WHERE qf."quarter" = '2024Q4' AND qf."ticker symbol" = 'AAPL';
```

---

## Extending the project

- **Valuation screens:** join the view with `company`/`ticker_map`; EPS is quarterly, so annualize carefully (Q4-derived rows exist).
- **Time-series features:** `price_daily` (33.6 M rows, PK `(ticker, date)`) has split-adjusted OHLCV for rolling returns. For as-traded daily prices, un-adjust with `stock_split`.
- **Dividend analytics:** `dividend` stores both bases — use `amount_as_reported` for cash-flow-style work and `amount` to match Yahoo's published series.
- **EDGAR deep dives:** `eps_facts`/`shares_facts` retain all periods (quarterly, YTD, annual) and weighted-average concepts; add new concepts in `edgar_etl.py` (concept registry at the top).
- **Schema changes:** update `sql/schema.sql`, `sql/postprocess.sql` (if it depends on the Yahoo load), then `sql/view.sql`, then add assertions to `sql/validate.sql`. Re-apply via stdin psql and refresh dashboard tables with `./run.sh --rebuild-derived`.
- **Don't change dedup/basis rules casually** — the anchors in [Validation](#validation-and-known-limitations) are the contract.

---

## Azure deployment

Want the same stack in the cloud (static web app + Functions API + managed PostgreSQL + scheduled Container Apps ETL)? A complete guide lives in [`docs/azure-deploy.md`](docs/azure-deploy.md). The local Docker workflow stays exactly as described here.

---

*Built 2026-09-14; dashboard added 2026-09-15; corrupt-price exclusion sweep applied 2026-09-17. All counts, examples and reference runs in this document were re-verified against the live database on 2026-09-18 (EDGAR data as of 2026-09-14; Yahoo daily history through 2026-09-14; view quarters 1962Q1–2026Q3).*
