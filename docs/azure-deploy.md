# Deploying ValInvest on Azure

A complete, copy-pasteable deployment of the ValInvest value-investing stack to
Azure:

- **Static website** (`Blob Storage` `$web`) serving a small React SPA + Plotly chart,
- **API** (`Azure Functions`, Python 3.13, Flex Consumption) that owns the DB
  credentials and reuses `dashboard/backtest.py` unchanged,
- **Managed database** (`Azure Database for PostgreSQL` Flexible Server 16),
- **Scheduled ETL** (`Azure Container Apps` Job) running the same EDGAR/Yahoo
  pipeline from this repo, on a cron schedule, independent of your machine,
- **ACR** for the ETL image.

The existing local workflow (`./run.sh`, Docker Compose, Streamlit) continues to
work exactly as described in the main `README.md`; this guide only **adds** files.

---

## 0. Architecture and what changes

```
browser ──HTTPS──► Blob Storage static website ($web)
                        │  fetch() JSON
                        ▼
              Function App (Python 3.13)  ──── psycopg (sslmode=require) ────►  PostgreSQL
              /api/options                                                      Flexible Server 16
              /api/backtest   (caches value_panel in memory)                         ▲
              /api/health                                                            │ psycopg
                                                                                     │
              Container Apps Job (cron) ── runs edgar_etl.py, yahoo_etl.py, ────────┘
                                            postprocess.sql, view.sql, prepare.py
```

A static site cannot safely hold database credentials, and Streamlit cannot be a
static site — hence the API layer. Only the Function App and the ETL job ever see
`DATABASE_URL`.

### Files this guide adds (nothing else is modified)

| Path | Purpose |
| --- | --- |
| `.dockerignore` | keeps `data/` (1.8 GB) out of every image build context |
| `deploy/azure/Dockerfile.job` | cloud ETL image: ETL deps + `psql` + `curl` |
| `deploy/azure/cloud_entrypoint.sh` | downloads `companyfacts.zip`, runs the full pipeline |
| `deploy/azure/deploy_api.sh` | copies `backtest.py`, zips and deploys the Function App |
| `deploy/azure/deploy_web.sh` | `npm run build` + uploads `web/dist` to `$web` |
| `api/function_app.py` | HTTP endpoints (v2 model) |
| `api/host.json`, `api/requirements.txt` | Functions host config + deps |
| `api/backtest.py` | **generated** by `deploy_api.sh` (copy of `dashboard/backtest.py`) |
| `web/package.json`, `web/vite.config.mjs`, `web/index.html` | Vite + React project |
| `web/src/main.jsx`, `web/src/App.jsx`, `web/src/Plot.jsx`, `web/src/api.js`, `web/src/styles.css` | the SPA |

### Approximate monthly cost (check current Azure pricing)

| Resource | SKU | Notes | Cost |
| --- | --- | --- | --- |
| PostgreSQL Flexible Server | Burstable `Standard_B1ms`, 32 GiB, 7-day backup | can be stopped between sessions | ~$16–22 |
| Container Apps Job | 2 vCPU / 4 GiB, weekly, ~2 h per run | per-second billing | ~$1.5–3 |
| Function App | Flex Consumption, `http=1` always-ready (2 GB) | set always-ready to 0 to pay only per call | ~$0–20 |
| Azure Container Registry | Basic | | $5 |
| Storage accounts (2) | Standard LRS | static site + Functions host | <$1 |
| **Total** | | | **~$23–50** |

Keeping the DB stopped when idle and always-ready at 0 puts the floor near
$10–15/month.

---

## 1. Prerequisites

- Windows/macOS/Linux host with `docker` (only for the optional dump/restore),
  `zip`, `curl`, `git`.
- **Azure CLI ≥ 2.60** and the Container Apps extension:
  ```bash
  az login
  az account set --subscription "<SUBSCRIPTION_NAME_OR_ID>"
  az extension add --name containerapp --upgrade
  ```
- **Node 20+ / npm** (frontend build). No Azure Functions Core Tools needed —
  the guide deploys the API with `az functionapp deployment source config-zip`.
- Pick a region that supports **both** PostgreSQL Flexible Server and Flex
  Consumption Functions (e.g. `eastus2`, `centralus`, `westus3`):
  ```bash
  az functionapp list-flexconsumption-locations --query "sort_by(@, &name)[].name" -o tsv
  ```
- An SEC-compliant user agent string with real contact info (SEC blocks generic
  agents): `ValInvest Research your-email@example.com`.

---

## 2. Shell setup

Run everything in the repo root (`/home/n/Coding/valinvest`). Keep this shell for
the whole guide:

```bash
cd /home/n/Coding/valinvest

export RG="rg-valinvest"
export REGION="eastus2"                       # must be Flex-Consumption-capable
export PREFIX="valinvest-$(openssl rand -hex 2)"   # globally-unique suffix

export PG="${PREFIX}-pg"                      # 3-63 chars, lowercase/hyphen
export ACR="${PREFIX//-/}acr"                 # alphanumeric only
export ACA_ENV="${PREFIX}-env"
export SAFN="${PREFIX//-/}fn"                 # Functions host storage
export SAWEB="${PREFIX//-/}web"               # static website storage
export FN_APP="${PREFIX}-api"                 # 2-60 chars, globally unique
export JOB="${PREFIX}-etl"

# Alphanumeric password avoids URL-encoding issues in the DSN.
export PGPASS="$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 28)"
export DB_URL="postgresql://valinvest:${PGPASS}@${PG}.postgres.database.azure.com:5432/valinvest?sslmode=require"
export SEC_USER_AGENT="ValInvest Research you@example.com"

echo "$DB_URL"   # keep this private
```

> **Note:** `${PG}` must not already exist in the region. If a resource-name
> collision happens, re-run with a new `PREFIX`.

---

## 3. Foundation: resource group, storage, registry

```bash
az group create --name "$RG" --location "$REGION"

# Functions host storage (public blob access OFF)
az storage account create \
  --resource-group "$RG" --name "$SAFN" --location "$REGION" \
  --sku Standard_LRS --kind StorageV2 --allow-blob-public-access false

# Static website storage (public blob access ON is required by static websites)
az storage account create \
  --resource-group "$RG" --name "$SAWEB" --location "$REGION" \
  --sku Standard_LRS --kind StorageV2 --allow-blob-public-access true

# Container registry for the ETL image (admin user enabled for simple job pull)
az acr create --resource-group "$RG" --name "$ACR" --sku Basic --admin-enabled true
```

---

## 4. PostgreSQL Flexible Server

The CLI default storage is 128 GiB — pass `--storage-size 32` explicitly. The
local DB is ≈5.2 GB, so 32 GiB leaves room for WAL, indexes, and backups.

```bash
az postgres flexible-server create \
  --resource-group "$RG" \
  --name "$PG" \
  --location "$REGION" \
  --admin-user valinvest \
  --admin-password "$PGPASS" \
  --tier Burstable --sku-name Standard_B1ms \
  --version 16 \
  --storage-size 32 --storage-auto-grow Disabled \
  --backup-retention 7 \
  --public-access 0.0.0.0 \
  --yes

az postgres flexible-server db create --resource-group "$RG" --server-name "$PG" --database-name valinvest
```

`--public-access 0.0.0.0` creates the *"allow Azure services"* rule — sufficient
for the Function App and the Container Apps job. It does **not** let your laptop
connect; add your IP temporarily if you plan to run `psql`/`pg_restore` from
here (see §5B and §12). SSL is enforced by default; the `?sslmode=require` in
`$DB_URL` satisfies it for both `psql` and `psycopg`.

Smoke-test connectivity from a container:

```bash
docker run --rm postgres:16-alpine \
  psql "$DB_URL" -c "select version();"
```

If that fails with `no pg_hba.conf entry`, add your current IP for the duration
of the session and remove the rule afterwards:

```bash
MYIP="$(curl -fsS https://api.ipify.org)"
az postgres flexible-server firewall-rule create -g "$RG" -s "$PG" -n local \
  --start-ip-address "$MYIP" --end-ip-address "$MYIP"

# ... do your psql/pg_restore work ...

az postgres flexible-server firewall-rule delete -g "$RG" -s "$PG" -n local --yes
```

---

## 5. Seed the database

Two options — pick one.

### Option A (recommended): let the first cloud ETL run seed it

Skip this section entirely. §6 creates the job; the first execution downloads
`companyfacts.zip`, builds every table, the view, and the derived
`value_panel`/`stock_risk_quarter` tables. Total time ≈1.5–3 h (Yahoo dominates).

### Option B: migrate your existing local database

This gets the API working immediately and is the choice if your local data is
already current. Roughly 5 GB of traffic; at 20 Mbps upstream that is ~35 min.

```bash
# 1. Dump the running local database (custom format, compressed)
docker compose exec -T db pg_dump -U valinvest -d valinvest -Fc > valinvest.dump
ls -lh valinvest.dump

# 2. Temporarily allow this machine through the Azure firewall
MYIP="$(curl -fsS https://api.ipify.org)"
az postgres flexible-server firewall-rule create -g "$RG" -s "$PG" -n local-restore \
  --start-ip-address "$MYIP" --end-ip-address "$MYIP"

# 3. Restore with a throwaway matching-major client (pg_restore streams from the file)
docker run --rm -v "$PWD":/dump -w /dump postgres:16-alpine \
  pg_restore --no-owner --no-acl \
  -d "$DB_URL" valinvest.dump

# 4. Confirm and remove the temporary firewall rule
docker run --rm postgres:16-alpine psql "$DB_URL" -c \
  'SELECT count(*) FROM value_panel;'
az postgres flexible-server firewall-rule delete -g "$RG" -s "$PG" -n local-restore --yes
```

The dump includes `value_panel` and `stock_risk_quarter`, so the dashboard API is
immediately functional. The scheduled job will still do a full `--reset` rebuild
on its next run — that is intentional and safe; it just re-derives everything
from source.

---

## 6. Cloud ETL image + scheduled Container Apps job

### 6.1 `.dockerignore` (repo root)

The ACR build uses the repo root as its build context, so this file keeps the
1.8 GB `data/` directory (and `logs/`, `.git`, frontend `node_modules`) out of
the upload. The existing Compose build context is `./etl`, so `run.sh` behavior
is unchanged.

```gitignore
# .dockerignore
.git
data
logs
**/__pycache__
**/*.pyc
*.log
.env
.env.*
web/node_modules
web/dist
```

### 6.2 `deploy/azure/Dockerfile.job`

```dockerfile
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-client curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /work

COPY etl/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /work
RUN chmod +x deploy/azure/cloud_entrypoint.sh

ENTRYPOINT ["bash", "deploy/azure/cloud_entrypoint.sh"]
```

### 6.3 `deploy/azure/cloud_entrypoint.sh`

`edgar_etl.py` only downloads the ticker map — `companyfacts.zip` must already
exist — so the entrypoint fetches the 1.31 GiB SEC archive first, then runs the
same sequence `run.sh` uses (SQL via `psql`, ETLs via `--reset`, then
`postprocess.sql` → `view.sql` → `prepare.py --force` → `validate.sql`).

```bash
#!/usr/bin/env bash
# Cloud ETL entrypoint: full ValInvest rebuild against an Azure PostgreSQL server.
# Env: DATABASE_URL, SEC_USER_AGENT (required); SKIP_EDGAR/SKIP_YAHOO=1 optional.
set -euo pipefail

log() { printf '[cloud-etl] %s\n' "$*"; }

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${SEC_USER_AGENT:?SEC_USER_AGENT is required by SEC}"

mkdir -p data logs

if [ ! -s data/companyfacts.zip ]; then
  log "downloading companyfacts.zip (~1.3 GiB)"
  curl -fsSL --retry 5 --retry-delay 10 --retry-all-errors \
    -A "$SEC_USER_AGENT" \
    -o data/companyfacts.zip.tmp \
    https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip
  mv data/companyfacts.zip.tmp data/companyfacts.zip
else
  log "companyfacts.zip already present"
fi

run_sql() {
  log "applying $1"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$1"
}

run_sql sql/schema.sql

if [ "${SKIP_EDGAR:-0}" != "1" ]; then
  log "EDGAR ETL (reset)"
  python etl/edgar_etl.py --reset
fi

if [ "${SKIP_YAHOO:-0}" != "1" ]; then
  log "Yahoo ETL (reset) -- this is the long part"
  python etl/yahoo_etl.py --reset
fi

run_sql sql/postprocess.sql
run_sql sql/view.sql

log "rebuilding stock_risk_quarter + value_panel"
python dashboard/prepare.py --force

log "validation (read-only)"
psql "$DATABASE_URL" -f sql/validate.sql || log "WARNING: validation reported an error"

log "done"
```

### 6.4 Build the image in ACR (no local Docker required)

```bash
az acr build \
  --registry "$ACR" \
  --image "${PREFIX}-etl:cloud" \
  --file deploy/azure/Dockerfile.job .
```

### 6.5 Create the environment and the scheduled job

A default (consumption-only) Container Apps environment caps each replica at
**2 vCPU / 4 GiB**; that yields **8 GiB of ephemeral disk**, which comfortably
holds the 1.31 GiB zip plus the ~1.8 GiB of EDGAR shard CSVs. `EDGAR_WORKERS=2`
matches the CPU; Yahoo is network-bound so `YF_WORKERS=8` with `YF_RATE=5`
still applies the same global request rate as local.

```bash
az containerapp env create -g "$RG" -n "$ACA_ENV" -l "$REGION"

ACR_USER="$(az acr credential show -n "$ACR" --query username -o tsv)"
ACR_PASS="$(az acr credential show -n "$ACR" --query 'passwords[0].value' -o tsv)"

az containerapp job create \
  --name "$JOB" --resource-group "$RG" --environment "$ACA_ENV" \
  --trigger-type Schedule \
  --cron-expression "0 6 * * 0" \
  --replica-timeout 14400 \
  --replica-retry-limit 0 \
  --parallelism 1 --replica-completion-count 1 \
  --image "${ACR}.azurecr.io/${PREFIX}-etl:cloud" \
  --registry-server "${ACR}.azurecr.io" \
  --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
  --cpu 2.0 --memory 4Gi \
  --secrets "db-url=$DB_URL" \
  --env-vars \
    "DATABASE_URL=secretref:db-url" \
    "SEC_USER_AGENT=$SEC_USER_AGENT" \
    "EDGAR_WORKERS=2" \
    "YF_WORKERS=8" \
    "YF_RATE=5" \
    "EDGAR_ZIP=/work/data/companyfacts.zip"
```

Run it now to seed the database (this is the §5 Option A path):

```bash
az containerapp job start -n "$JOB" -g "$RG"

# watch executions
az containerapp job execution list -n "$JOB" -g "$RG" -o table

# stream the latest execution's logs (execution name from the command above)
az containerapp job logs show -n "$JOB" -g "$RG" \
  --execution "<EXECUTION_NAME>" --container main
```

Container stdout (all ETL progress — `setup_logging` writes to stdout) is also
in the environment's Log Analytics workspace, table `ContainerAppConsoleLogs_CL`.

**Cron reference:** `0 6 * * 0` = Sundays 06:00 UTC. For more frequent Yahoo price
refreshes, create a second job from the same image with
`SKIP_EDGAR=1` and a daily cron after the weekly job has run at least once
(a full weekly `--reset` is usually plenty for a personal deployment).

---

## 7. API: Azure Functions (Python 3.13)

### 7.1 `api/requirements.txt`

```text
azure-functions>=1.21.0
pandas==3.0.5
numpy
psycopg[binary]~=3.2
```

### 7.2 `api/host.json`

```json
{
  "version": "2.0",
  "logging": {
    "applicationInsights": {
      "samplingSettings": { "isEnabled": true, "excludedTypes": "Request" }
    }
  },
  "extensionBundle": {
    "id": "Microsoft.Azure.Functions.ExtensionBundle",
    "version": "[4.0.0, 5.0.0)"
  }
}
```

### 7.3 `api/function_app.py`

A thin JSON layer over the untouched `backtest.py`. The 607k-row `value_panel` is
loaded once per warm instance and re-used until the data changes (new max `qidx`)
or the TTL (30 min) expires.

```python
"""Azure Functions API for the ValInvest value backtest.

Endpoints (anonymous, read-only):
    GET /api/health
    GET /api/options
    GET /api/backtest?pe_min=0&pe_max=15&...

The heavy lifting is backtest.py, copied next to this file at deploy time by
deploy/azure/deploy_api.sh.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import threading
import time

import azure.functions as func
import numpy as np
import pandas as pd
import psycopg

import backtest

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

DB_URL = os.environ["DATABASE_URL"]
PANEL_TTL_SECONDS = int(os.environ.get("PANEL_TTL_SECONDS", "1800"))

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

_LOCK = threading.Lock()
_PANEL: pd.DataFrame | None = None
_PANEL_KEY: tuple | None = None
_PANEL_TS = 0.0


def _db_key() -> tuple:
    with psycopg.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("SELECT min(qidx), max(qidx) FROM value_panel")
        return cur.fetchone()


def _get_panel() -> pd.DataFrame:
    """Return the cached value_panel, reloading when the data or TTL changed."""
    global _PANEL, _PANEL_KEY, _PANEL_TS
    key = _db_key()
    now = time.monotonic()
    with _LOCK:
        stale = (
            _PANEL is None
            or key != _PANEL_KEY
            or (now - _PANEL_TS) > PANEL_TTL_SECONDS
        )
        if stale:
            _PANEL = backtest.load_panel(DB_URL)
            _PANEL_KEY = key
            _PANEL_TS = now
        return _PANEL


def _clean(obj):
    """Recursively make numpy/pandas scalars JSON-safe (NaN/Inf -> None)."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return _clean(float(obj))
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else obj
    if isinstance(obj, (pd.Timestamp, datetime.datetime, datetime.date)):
        return obj.isoformat()
    return obj


def _records(df: pd.DataFrame | None) -> list:
    if df is None or len(df) == 0:
        return []
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime("%Y-%m-%d")
    out = out.astype(object).where(pd.notna(out), None)
    return _clean(out.to_dict(orient="records"))


def _json(body, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(body), status_code=status, mimetype="application/json"
    )


def _param(req: func.HttpRequest, name: str, default, cast):
    raw = req.params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise ValueError(f"invalid {name}={raw!r}")


def _parse_bool(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _params_from_request(req: func.HttpRequest) -> dict:
    p = {
        "pe_min": _param(req, "pe_min", 0.0, float),
        "pe_max": _param(req, "pe_max", 15.0, float),
        "mcap_min_b": _param(req, "mcap_min_b", 0.5, float),
        "mcap_max_b": _param(req, "mcap_max_b", 500.0, float),
        "min_div_yield": _param(req, "min_div_yield", 0.0, float),
        "require_pos_eps4": _param(req, "require_pos_eps4", True, _parse_bool),
        "vol_max": _param(req, "vol_max", None, float),
        "min_ret_12m": _param(req, "min_ret_12m", None, float),
        "freq": _param(req, "freq", "quarterly", str),
        "start_year": _param(req, "start_year", None, int),
        "delist_mode": _param(req, "delist_mode", "carry", str),
    }
    if p["freq"] not in backtest.FREQ_MODS:
        raise ValueError("freq must be quarterly|semiannual|annual")
    if p["delist_mode"] not in {"carry", "writeoff"}:
        raise ValueError("delist_mode must be carry|writeoff")
    if p["mcap_min_b"] > p["mcap_max_b"]:
        raise ValueError("mcap_min_b must be <= mcap_max_b")
    return p


@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    try:
        panel = _get_panel()
        return _json(
            {
                "ok": True,
                "panel_rows": int(len(panel)),
                "first_quarter": str(panel["quarter"].min()),
                "last_quarter": str(panel["quarter"].max()),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": str(exc)}, status=503)


@app.route(route="options", methods=["GET"])
def options(req: func.HttpRequest) -> func.HttpResponse:
    first = last = None
    try:
        panel = _get_panel()
        first = str(panel["quarter"].min())
        last = str(panel["quarter"].max())
    except Exception:  # noqa: BLE001 - options still works without data
        pass
    start_years = ["Earliest available"]
    if first:
        first_year = max(2008, int(first[:4]))
        start_years += [str(y) for y in range(first_year, datetime.date.today().year + 1)]
    return _json(
        {
            "defaults": backtest.DEFAULT_PARAMS,
            "frequencies": FREQ_LABELS,
            "vol_filters": VOL_FILTERS,
            "start_years": start_years,
            "data": {"first_quarter": first, "last_quarter": last},
        }
    )


@app.route(route="backtest", methods=["GET"])
def backtest_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    try:
        params = _params_from_request(req)
    except ValueError as exc:
        return _json({"error": str(exc)}, status=400)

    try:
        panel = _get_panel()
    except Exception as exc:  # noqa: BLE001
        return _json(
            {"error": f"database unavailable: {exc}"}, status=503
        )

    result = backtest.run_backtest(panel, params)
    return _json(
        {
            "params": params,
            "matching_count": backtest.count_matching(panel, params),
            "stats": _clean(result.stats),
            "series": _records(result.series),
            "holdings": _records(result.holdings),
        }
    )
```

### 7.4 `deploy/azure/deploy_api.sh`

```bash
#!/usr/bin/env bash
# Package and deploy the Functions API.
# Usage: RG=rg-valinvest deploy/azure/deploy_api.sh <function-app-name>
set -euo pipefail
cd "$(dirname "$0")/../.."

: "${RG:?set RG to the resource group}"
FN_APP="${1:?usage: deploy_api.sh <function-app-name>}"

# Single source of truth: backtest.py is generated, never edited in api/.
cp dashboard/backtest.py api/backtest.py

rm -f /tmp/valinvest-api.zip
( cd api && zip -qr /tmp/valinvest-api.zip . -x '*.pyc' -x '__pycache__/*' )

az functionapp deployment source config-zip \
  --resource-group "$RG" \
  --name "$FN_APP" \
  --src /tmp/valinvest-api.zip \
  --build-remote true
```

### 7.5 Create the Function App and deploy

`--always-ready-instances http=1` keeps one 2048 MB instance warm and removes
almost all cold-start latency, at a continuous baseline cost. Drop it (delete the
flag) to pay only per execution and accept ~5–15 s cold starts.

```bash
az functionapp create \
  --resource-group "$RG" \
  --name "$FN_APP" \
  --storage-account "$SAFN" \
  --flexconsumption-location "$REGION" \
  --runtime python --runtime-version 3.13 \
  --always-ready-instances http=1

az functionapp config appsettings set -g "$RG" -n "$FN_APP" --settings \
  "DATABASE_URL=$DB_URL" \
  "PANEL_TTL_SECONDS=1800"

# Allow the static site origin to call the API (get the exact origin first).
WEB_URL="$(az storage account show -g "$RG" -n "$SAWEB" --query primaryEndpoints.web -o tsv)"
WEB_URL="${WEB_URL%/}"
az functionapp cors add -g "$RG" --name "$FN_APP" --allowed-origins "$WEB_URL"

# Deploy the code (needs: zip, and a location since --build-remote uses it)
LOCATION="$REGION" RG="$RG" bash deploy/azure/deploy_api.sh "$FN_APP"
```

Verify:

```bash
BASE="https://${FN_APP}.azurewebsites.net"
curl -fsS "$BASE/api/health" | jq .
curl -fsS "$BASE/api/options" | jq '.data'
curl -fsS "$BASE/api/backtest?pe_max=15" | jq '.stats'
```

> If the Functions host reports a `TimeoutException` at startup, it is the
> 30 s host-init limit: keep imports at module level minimal (this file already
> does) and keep the first `value_panel` read inside request handling.

---

## 8. Static frontend (Vite + React + Plotly)

A minimal SPA reproducing the Streamlit dashboard's core controls, metrics,
equity chart, and latest-holdings table. The API base URL is baked in at build
time via `VITE_API_BASE_URL`. The matching-count display is read from the
backtest response (`matching_count`), so it refreshes when the backtest runs.

### 8.1 `web/package.json`

```json
{
  "name": "valinvest-web",
  "private": true,
  "version": "1.0.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "plotly.js-dist-min": "^2.35.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.4",
    "vite": "^6.0.0"
  }
}
```

### 8.2 `web/vite.config.mjs`

```js
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
});
```

### 8.3 `web/index.html`

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>ValInvest — Value Portfolio vs S&amp;P 500</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
```

### 8.4 `web/src/main.jsx`

```jsx
import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import "./styles.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
```

### 8.5 `web/src/api.js`

```js
const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");

async function get(path) {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${path} failed: HTTP ${res.status} ${text.slice(0, 200)}`);
  }
  return res.json();
}

export function fetchOptions() {
  return get("/api/options");
}

export function fetchBacktest(params) {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    qs.set(key, String(value));
  }
  return get(`/api/backtest?${qs.toString()}`);
}

export { API_BASE };
```

### 8.6 `web/src/Plot.jsx`

```jsx
import { useEffect, useRef } from "react";
import Plotly from "plotly.js-dist-min";

export default function Plot({ series, logScale }) {
  const ref = useRef(null);

  useEffect(() => {
    if (!ref.current || !series || series.length === 0) return;
    const trace = (name, key, color) => ({
      x: series.map((row) => row.date),
      y: series.map((row) => row[key]),
      mode: "lines",
      name,
      line: { color },
      hovertemplate: "%{x}<br>$%{y:.2f}<extra>" + name + "</extra>",
    });
    const layout = {
      margin: { l: 55, r: 20, t: 20, b: 40 },
      hovermode: "x unified",
      legend: { orientation: "h" },
      yaxis: {
        title: "Value ($, start = 100)",
        type: logScale ? "log" : "linear",
      },
    };
    Plotly.react(
      ref.current,
      [
        trace("Value portfolio", "portfolio", "#1f77b4"),
        trace("S&P 500 (SPY)", "benchmark", "#636363"),
      ],
      layout,
      { responsive: true, displaylogo: false }
    );
    return () => Plotly.purge(ref.current);
  }, [series, logScale]);

  return <div ref={ref} style={{ width: "100%", height: "480px" }} />;
}
```

### 8.7 `web/src/App.jsx`

```jsx
import { useEffect, useMemo, useState } from "react";
import Plot from "./Plot.jsx";
import { fetchBacktest, fetchOptions } from "./api.js";

const fmtPct = (v) => (v == null ? "n/a" : `${(v * 100).toFixed(2)}%`);
const fmtUsd = (v) => (v == null ? "n/a" : `$${Number(v).toFixed(2)}`);

export default function App() {
  const [options, setOptions] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [ranOnce, setRanOnce] = useState(false);

  const [freqLabel, setFreqLabel] = useState("Quarterly");
  const [peMin, setPeMin] = useState(0);
  const [peMax, setPeMax] = useState(15);
  const [mcapMin, setMcapMin] = useState(0.5);
  const [mcapMax, setMcapMax] = useState(500);
  const [divYield, setDivYield] = useState(0);
  const [requirePosEps4, setRequirePosEps4] = useState(true);
  const [volLabel, setVolLabel] = useState("No limit");
  const [minRet, setMinRet] = useState(-100);
  const [startYear, setStartYear] = useState("Earliest available");
  const [logScale, setLogScale] = useState(false);

  useEffect(() => {
    fetchOptions().then(setOptions).catch((e) => setError(String(e)));
  }, []);

  const params = useMemo(
    () => ({
      pe_min: peMin,
      pe_max: peMax,
      mcap_min_b: mcapMin,
      mcap_max_b: mcapMax,
      min_div_yield: divYield / 100,
      require_pos_eps4: requirePosEps4,
      vol_max: options?.vol_filters?.[volLabel] ?? null,
      min_ret_12m: minRet === -100 ? null : minRet / 100,
      freq: options?.frequencies?.[freqLabel] ?? "quarterly",
      start_year: startYear === "Earliest available" ? null : Number(startYear),
      delist_mode: "carry",
    }),
    [
      peMin, peMax, mcapMin, mcapMax, divYield, requirePosEps4,
      volLabel, minRet, freqLabel, startYear, options,
    ]
  );

  async function run() {
    setLoading(true);
    setError(null);
    try {
      setResult(await fetchBacktest(params));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (options && !ranOnce) {
      setRanOnce(true);
      run();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options]);

  const stats = result?.stats ?? {};
  const series = result?.series ?? [];
  const holdings = result?.holdings ?? [];
  const matchingCount = result?.matching_count;
  const latestQuarter = holdings.length
    ? holdings.reduce(
        (max, h) => (h.rebalance_quarter > max ? h.rebalance_quarter : max),
        holdings[0].rebalance_quarter
      )
    : null;
  const latest = holdings
    .filter((h) => h.rebalance_quarter === latestQuarter)
    .sort((a, b) => b.weight - a.weight);

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>ValInvest</h1>

        <label>
          Rebalance frequency
          <select value={freqLabel} onChange={(e) => setFreqLabel(e.target.value)}>
            {Object.keys(options?.frequencies ?? { Quarterly: "quarterly" }).map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
        </label>

        <label>
          P/E min: {peMin}
          <input type="range" min="0" max="60" step="0.5" value={peMin}
                 onChange={(e) => setPeMin(Number(e.target.value))} />
        </label>
        <label>
          P/E max: {peMax}
          <input type="range" min="0" max="60" step="0.5" value={peMax}
                 onChange={(e) => setPeMax(Number(e.target.value))} />
        </label>

        <label>
          Min market cap ($B)
          <input type="number" min="0" step="0.5" value={mcapMin}
                 onChange={(e) => setMcapMin(Number(e.target.value))} />
        </label>
        <label>
          Max market cap ($B)
          <input type="number" min="0" step="10" value={mcapMax}
                 onChange={(e) => setMcapMax(Number(e.target.value))} />
        </label>

        <label>
          Min dividend yield (%): {divYield.toFixed(1)}
          <input type="range" min="0" max="10" step="0.1" value={divYield}
                 onChange={(e) => setDivYield(Number(e.target.value))} />
        </label>

        <label className="check">
          <input type="checkbox" checked={requirePosEps4}
                 onChange={(e) => setRequirePosEps4(e.target.checked)} />
          Require positive EPS in each of the last 4 quarters
        </label>

        <label>
          Volatility filter
          <select value={volLabel} onChange={(e) => setVolLabel(e.target.value)}>
            {Object.keys(options?.vol_filters ?? { "No limit": null }).map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
        </label>

        <label>
          Crash filter: min 12-month return (%): {minRet}
          <input type="range" min="-100" max="0" step="5" value={minRet}
                 onChange={(e) => setMinRet(Number(e.target.value))} />
        </label>

        <label>
          Start year
          <select value={startYear} onChange={(e) => setStartYear(e.target.value)}>
            {(options?.start_years ?? ["Earliest available"]).map((y) => (
              <option key={y}>{y}</option>
            ))}
          </select>
        </label>

        <label className="check">
          <input type="checkbox" checked={logScale}
                 onChange={(e) => setLogScale(e.target.checked)} />
          Log scale on chart
        </label>

        <button onClick={run} disabled={loading}>
          {loading ? "Running…" : "Run backtest"}
        </button>

        {matchingCount != null && (
          <p className="muted">
            {matchingCount} value stock{matchingCount === 1 ? "" : "s"} matching
          </p>
        )}

        {options?.data?.last_quarter && (
          <p className="muted">
            Data window: {options.data.first_quarter} → {options.data.last_quarter}
          </p>
        )}
      </aside>

      <main className="content">
        <h2>Value Portfolio vs S&amp;P 500</h2>
        {error && <p className="error">{error}</p>}
        {stats.settled === false && stats.message && (
          <p className="warn">{stats.message}</p>
        )}

        <div className="metrics">
          <div>
            <span>Portfolio final value</span>
            <strong>{fmtUsd(stats.portfolio_final)}</strong>
          </div>
          <div>
            <span>S&amp;P 500 final value</span>
            <strong>{fmtUsd(stats.benchmark_final)}</strong>
          </div>
          <div>
            <span>Portfolio CAGR</span>
            <strong>{fmtPct(stats.portfolio_cagr)}</strong>
            <em>
              {stats.excess_cagr == null
                ? ""
                : `${stats.excess_cagr >= 0 ? "+" : ""}${fmtPct(stats.excess_cagr)} vs S&P 500`}
            </em>
          </div>
          <div>
            <span>Portfolio max drawdown</span>
            <strong>{fmtPct(stats.portfolio_max_dd)}</strong>
          </div>
        </div>

        {stats.start_quarter && (
          <p className="muted">
            {stats.start_quarter} → {stats.end_quarter} · {stats.n_rebalances}{" "}
            rebalances · benchmark = SPY total return.
          </p>
        )}

        <Plot series={series} logScale={logScale} />

        <h3>Latest rebalance holdings ({latestQuarter ?? "n/a"})</h3>
        {latest.length === 0 ? (
          <p className="muted">No holdings were selected.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Ticker</th><th>Name</th><th>Weight</th><th>P/E</th>
                <th>Mkt cap ($B)</th><th>Dividend yield</th>
                <th>Vol 12m</th><th>12m return</th>
              </tr>
            </thead>
            <tbody>
              {latest.map((h) => (
                <tr key={h.ticker}>
                  <td>{h.ticker}</td>
                  <td>{h.name}</td>
                  <td>{(h.weight * 100).toFixed(2)}%</td>
                  <td>{h.pe == null ? "n/a" : h.pe.toFixed(2)}</td>
                  <td>{h.mktcap_b == null ? "n/a" : h.mktcap_b.toFixed(2)}</td>
                  <td>{fmtPct(h.div_yield)}</td>
                  <td>{fmtPct(h.vol_252d)}</td>
                  <td>{fmtPct(h.ret_252d)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </main>
    </div>
  );
}
```

### 8.8 `web/src/styles.css`

```css
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, sans-serif; color: #1c1c1c; }
.layout { display: flex; min-height: 100vh; }
.sidebar {
  width: 300px; flex: 0 0 300px; padding: 16px; background: #f6f8fa;
  border-right: 1px solid #ddd; display: flex; flex-direction: column; gap: 12px;
}
.sidebar h1 { font-size: 20px; margin: 0 0 4px; }
.sidebar label { display: flex; flex-direction: column; font-size: 13px; gap: 4px; }
.sidebar label.check { flex-direction: row; align-items: center; gap: 8px; }
.sidebar select, .sidebar input[type="number"] { padding: 4px; }
.sidebar button {
  margin-top: 8px; padding: 10px; font-size: 14px; cursor: pointer;
  background: #1f77b4; color: white; border: none; border-radius: 4px;
}
.sidebar button:disabled { opacity: 0.6; cursor: wait; }
.content { flex: 1; padding: 20px 28px; min-width: 0; }
.metrics { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.metrics > div {
  border: 1px solid #e3e3e3; border-radius: 6px; padding: 10px;
  display: flex; flex-direction: column; gap: 4px;
}
.metrics span { font-size: 12px; color: #666; }
.metrics strong { font-size: 20px; }
.metrics em { font-size: 12px; color: #444; font-style: normal; }
.muted { color: #666; font-size: 13px; }
.error { color: #b00020; }
.warn { color: #8a6d00; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; }
```

### 8.9 `deploy/azure/deploy_web.sh`

```bash
#!/usr/bin/env bash
# Build the SPA and upload it to the static-website $web container.
# Usage: RG=rg-valinvest SAWEB=acct VITE_API_BASE_URL=https://app.azurewebsites.net \
#          deploy/azure/deploy_web.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

: "${RG:?set RG}"
: "${SAWEB:?set SAWEB to the static website storage account}"
: "${VITE_API_BASE_URL:?set VITE_API_BASE_URL to https://<function-app>.azurewebsites.net}"

( cd web && npm install && VITE_API_BASE_URL="$VITE_API_BASE_URL" npm run build )

WEB_CONN="$(az storage account show-connection-string -g "$RG" -n "$SAWEB" -o tsv)"
az storage blob upload-batch \
  --connection-string "$WEB_CONN" \
  -d '$web' -s web/dist --overwrite
```

### 8.10 Enable the static website and deploy

```bash
WEB_CONN="$(az storage account show-connection-string -g "$RG" -n "$SAWEB" -o tsv)"

az storage blob service-properties update \
  --connection-string "$WEB_CONN" \
  --static-website \
  --index-document index.html \
  --404-document index.html

VITE_API_BASE_URL="https://${FN_APP}.azurewebsites.net" \
  RG="$RG" SAWEB="$SAWEB" bash deploy/azure/deploy_web.sh

az storage account show -g "$RG" -n "$SAWEB" --query primaryEndpoints.web -o tsv
```

Open the printed `https://<account>.zXX.web.core.windows.net/` URL. (Azure
provides HTTPS on that hostname; a custom domain requires Front Door or Azure CDN
in front of the `$web` endpoint.)

> **Alternative — Azure Static Web Apps:** if you want GitHub CI/CD, per-PR
> preview environments, and free custom-domain TLS, create a Static Web App
> (`az staticwebapp create`) with `app_location: web`, `output_location: dist`,
> and keep this same separate Function App as the API. The SPA is
> framework-agnostic and needs only `VITE_API_BASE_URL`; remember to add the SWA
> origin to Function CORS.

---

## 9. End-to-end verification

```bash
BASE="https://${FN_APP}.azurewebsites.net"
WEB_URL="$(az storage account show -g "$RG" -n "$SAWEB" --query primaryEndpoints.web -o tsv)"

# 1. API is live and sees data
curl -fsS "$BASE/api/health" | jq '{ok, panel_rows, first_quarter, last_quarter}'

# 2. A real backtest returns the expected shape
curl -fsS "$BASE/api/backtest?pe_max=15&freq=quarterly" \
  | jq '{settled: .stats.settled, start: .stats.start_quarter,
         end: .stats.end_quarter, rebalances: .stats.n_rebalances,
         curve_points: (.series | length), holdings: (.holdings | length)}'

# 3. Static site answers and contains the app shell
curl -fsS "$WEB_URL" | grep -q '<div id="root">' && echo "web ok"

# 4. ETL job history
az containerapp job execution list -n "$JOB" -g "$RG" -o table
```

Known-good data anchors (mirroring `sql/validate.sql`) for a fresh full build:
`value_panel ≈ 607k` rows, coverage `1962Q1–` the last complete quarter, and the
AAPL anchors in `README.md` §8. Run the full validator any time from your laptop:

```bash
docker run --rm -v "$PWD":/sql -w /sql postgres:16-alpine \
  psql "$DB_URL" -f sql/validate.sql
```

---

## 10. Operations

| Task | Command |
| --- | --- |
| Run the ETL now | `az containerapp job start -n "$JOB" -g "$RG"` |
| List executions | `az containerapp job execution list -n "$JOB" -g "$RG" -o table` |
| Tail an execution | `az containerapp job logs show -n "$JOB" -g "$RG" --execution <NAME> --container main` |
| Query logs in Log Analytics | table `ContainerAppConsoleLogs_CL` |
| Redeploy the API after code changes | `RG="$RG" bash deploy/azure/deploy_api.sh "$FN_APP"` |
| Update the ETL image/job | `az acr build --registry "$ACR" --image "${PREFIX}-etl:cloud" -f deploy/azure/Dockerfile.job .` then `az containerapp job update -n "$JOB" -g "$RG" --image "${ACR}.azurecr.io/${PREFIX}-etl:cloud"` |
| Rotate the DB password | create new password, `az postgres flexible-server update -g "$RG" -n "$PG" -p "<new>"`, then update `--secrets` on the job and `DATABASE_URL` on the Function App |
| Stop the DB to save cost | `az postgres flexible-server stop -g "$RG" -n "$PG"` (auto-restarts after 7 days; storage still bills) |
| Start it again | `az postgres flexible-server start -g "$RG" -n "$PG"` |
| Back up | Azure automated backups (7 days) are on; ad-hoc: `docker run --rm postgres:16-alpine pg_dump "$DB_URL" -Fc > valinvest-$(date +%F).dump` |
| Teardown everything | `az group delete -n "$RG" --yes --no-wait` |

The API cache is keyed on `max(qidx)` and also expires after
`PANEL_TTL_SECONDS`; a completed ETL run therefore surfaces on the site within
30 minutes at worst, or on the first request after the new quarter's rows land
(whichever comes first).

---

## 11. Security notes

- **Credentials live only server-side.** `DATABASE_URL` exists in two places: the
  Function App settings and the Container Apps job secret. The static bundle
  contains only the API base URL.
- **The API is anonymous and read-only** (public market data, CORS limited to the
  static site origin). If you want it private, front the site with Azure Static
  Web Apps authentication (Entra ID) and make the Function App non-public, or add
  API Management. Note that putting a Function *key* in a browser bundle is not
  real protection.
- **Prefer managed identity over secrets** as a follow-up:
  - ACR pull: give the job a system-assigned identity with the `AcrPull` role
    instead of registry admin credentials.
  - Key Vault: store `DATABASE_URL` in a vault and use a Key Vault reference in
    the Function App setting
    (`@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/db-url/)`)
    with a managed identity holding *Key Vault Secrets User*.
- **Network hardening:** switch PostgreSQL to private access (VNet integration)
  and integrate the Function App (`--vnet/--subnet`, subnet delegated to
  `Microsoft.App/environments`) and the Container Apps environment with the
  VNet. Then remove the `0.0.0.0` public firewall rule.
- **SEC compliance:** the `SEC_USER_AGENT` you deploy must be a real, descriptive
  identifier with contact information — generic agents get blocked.
- Add `api/backtest.py`, `web/node_modules/`, `web/dist/`, and `*.dump` to
  `.gitignore` if you commit any of this.

---

## 12. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `psql: error: connection to server ... no pg_hba.conf entry` | Your laptop IP is not allowed and/or `sslmode` missing. Public access `0.0.0.0` covers Azure services only; add a temporary firewall rule for your IP and keep `?sslmode=require`. |
| `psycopg.OperationalError: SSL connection is required` | Append `?sslmode=require` to `DATABASE_URL` everywhere (job secret and Function app setting). |
| Function returns `503 database unavailable: relation "value_panel" does not exist` | The ETL job has not completed a first successful run (or `prepare.py` failed). Check `az containerapp job execution list` and logs. |
| Function first request takes 10–20 s | Cold start. Add/keep `--always-ready-instances http=1`, or accept it. |
| Function host logs `System.TimeoutException` at startup | The 30 s host-init limit was exceeded by module imports; don't do heavy work (DB reads, panel loads) at import time. |
| Browser console: CORS error | Origin mismatch (trailing slash, `http` vs `https`, or no custom domain yet). Re-run `az functionapp cors add` with the exact site origin, or `az functionapp cors show`. |
| `az containerapp job create` → image pull failure | ACR credentials wrong/expired, or the image tag built in a different registry. Rebuild with `az acr build`; refresh `--registry-password`. |
| Job fails with `disk full` | Ephemeral disk is 8 GiB at 2 vCPU. Remove stale shards between runs (the entrypoint already cleans them via `edgar_etl.py --reset`) or mount an Azure Files share for `/work/data`. |
| `psql: command not found` in the job | You built from the old `etl/Dockerfile` instead of `deploy/azure/Dockerfile.job` (which installs `postgresql-client`). |
| `prepare.py` takes very long in the cloud | It scans `price_daily` on a B1ms server with a 2 vCPU job. It is a one-time ~5–30 min cost per rebuild; for faster rebuilds, temporarily run the job at a larger size/DB tier. |
| Yahoo run dies mid-way | Job retries are disabled (`--replica-retry-limit 0`), but `yahoo_etl.py` resumes without `--reset` if the volume persisted. In the ephemeral container the simplest fix is a fresh `az containerapp job start`. Keep `YF_RATE` at 5 to avoid throttling. |
| `Value for parameter @Microsoft.KeyVault(...) was not resolved` | The Function App's managed identity lacks *Key Vault Secrets User*, or the secret URI is wrong. |
| Static site 404s | `--index-document`/`--404-document` not set, or files uploaded somewhere other than the `$web` container. Re-run §8.10 with `-d '$web'`. |
| Resource-name collision at creation | Azure names for Function Apps, storage accounts, and Postgres are globally unique. Re-run §2 with a new `PREFIX` (and optionally a new `RG`). |

---

## 13. Quick teardown and cost control

```bash
# Cheapest steady state: no warm instances, DB stopped.
az postgres flexible-server stop -g "$RG" -n "$PG"

# Full teardown (irreversible; deletes the database and all data)
az group delete -n "$RG" --yes --no-wait
```

Keep the local Docker stack as your fast development loop (`./run.sh`), and use
Azure for hosting: the cloud is the same code, the same SQL, and the same
`backtest.py`.
