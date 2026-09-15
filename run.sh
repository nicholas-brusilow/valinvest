#!/usr/bin/env bash
# ValInvest single launcher: Dockerised EDGAR/Yahoo -> PostgreSQL pipeline plus
# the Streamlit dashboard.
#
# Usage:
#   ./run.sh                    # build if needed, start db+etl, build derived
#                               # tables if missing, start the dashboard
#   ./run.sh --with-etl         # force the full ETL pipeline first
#                               # (schema, EDGAR reset, Yahoo reset, view, validate)
#   ./run.sh --rebuild-derived  # force rebuilding stock_risk_quarter + value_panel
#                               # (dashboard/prepare.py --force, ~5 min)
#   ./run.sh --no-build         # skip `docker compose build`
#   ./run.sh --skip-etl         # accepted no-op alias: ETL only runs when the DB
#                               # is empty
#
# After startup the dashboard is served at http://localhost:8501.
# `docker compose down` stops everything but keeps the pgdata volume.
set -euo pipefail

cd "$(dirname "$0")"

log() { printf '[run.sh] %s\n' "$*"; }

BUILD=1
WITH_ETL=0
REBUILD_DERIVED=0
for arg in "$@"; do
  case "$arg" in
    --with-etl) WITH_ETL=1 ;;
    --rebuild-derived) REBUILD_DERIVED=1 ;;
    --no-build) BUILD=0 ;;
    --skip-etl)
      log "--skip-etl: accepted no-op; ETL runs only when the DB is empty"
      ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

if [ "$BUILD" -eq 1 ]; then
  log "building images"
  docker compose build
else
  log "--no-build: skipping image build"
fi

log "starting db + etl"
docker compose up -d db etl

log "waiting for db to become healthy"
for i in $(seq 1 60); do
  status="$(docker compose ps --format json db 2>/dev/null | sed -n 's/.*"Health":"\([^"]*\)".*/\1/p' || true)"
  if [ "$status" = "healthy" ]; then
    log "db is healthy"
    break
  fi
  if [ "$i" -eq 60 ]; then
    log "ERROR: db did not become healthy in time" >&2
    exit 1
  fi
  sleep 2
done

# SQL is always piped to psql from the host via stdin (never -f with a host path).
apply_sql() {
  log "applying $1"
  docker compose exec -T db psql -U valinvest -d valinvest -v ON_ERROR_STOP=1 < "$1"
}

db_query() {
  docker compose exec -T db psql -U valinvest -d valinvest -tAc "$1" | tr -d '[:space:]'
}

# --------------------------------------------------------------------------- #
# Is the analytics view present / populated?
# --------------------------------------------------------------------------- #
# schema.sql is fully idempotent (CREATE TABLE / INDEX IF NOT EXISTS); applying
# it first keeps the postprocess/view step below safe on a fresh database.
log "ensuring schema exists (idempotent)"
apply_sql sql/schema.sql

view_exists="$(db_query "SELECT to_regclass('quarterly_fundamentals') IS NOT NULL")"
rows=0
if [ "$view_exists" != "t" ]; then
  log "quarterly_fundamentals view is missing; applying postprocess.sql + view.sql"
  apply_sql sql/postprocess.sql
  apply_sql sql/view.sql
  rows=0
else
  rows="$(db_query "SELECT count(*) FROM quarterly_fundamentals")"
  log "quarterly_fundamentals has ${rows} rows"
fi

# --------------------------------------------------------------------------- #
# ETL: forced, or when the view has no data.
# --------------------------------------------------------------------------- #
if [ "$WITH_ETL" -eq 1 ] || [ "${rows:-0}" = "0" ]; then
  log "running full ETL path (Yahoo fetch can take 1-2 hours)"
  apply_sql sql/schema.sql
  log "running EDGAR ETL"
  docker compose exec -T etl python etl/edgar_etl.py --reset
  log "running Yahoo ETL"
  docker compose exec -T etl python etl/yahoo_etl.py --reset
  apply_sql sql/postprocess.sql
  apply_sql sql/view.sql
  apply_sql sql/validate.sql
else
  log "view already populated; skipping ETL (use --with-etl to force)"
fi

# --------------------------------------------------------------------------- #
# Derived tables (stock_risk_quarter + value_panel).
# --------------------------------------------------------------------------- #
need_derived=0
for table in stock_risk_quarter value_panel; do
  exists="$(db_query "SELECT to_regclass('$table') IS NOT NULL")"
  count=0
  if [ "$exists" = "t" ]; then
    count="$(db_query "SELECT count(*) FROM $table")"
  fi
  log "${table}: exists=${exists} rows=${count}"
  if [ "$exists" != "t" ] || [ "${count:-0}" = "0" ]; then
    need_derived=1
  fi
done

if [ "$REBUILD_DERIVED" -eq 1 ]; then
  need_derived=1
fi

if [ "$need_derived" -eq 1 ]; then
  log "building derived tables (stock_risk_quarter + value_panel), expect ~5 min"
  if [ "$REBUILD_DERIVED" -eq 1 ]; then
    docker compose exec -T etl python dashboard/prepare.py --force
  else
    docker compose exec -T etl python dashboard/prepare.py
  fi
else
  log "derived tables already present; skipping prepare.py"
fi

# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
log "starting dashboard"
docker compose up -d dashboard

if command -v curl >/dev/null 2>&1; then
  log "waiting for dashboard to answer on http://localhost:8501"
  for i in $(seq 1 30); do
    if curl -fsS -o /dev/null "http://localhost:8501" 2>/dev/null; then
      break
    fi
    if [ "$i" -eq 30 ]; then
      log "WARNING: dashboard did not answer within ~60s; check 'docker compose logs dashboard'" >&2
    fi
    sleep 2
  done
else
  log "curl not found; skipping the readiness wait"
fi

log "Dashboard: http://localhost:8501"
log "stop with: docker compose down   (keeps the pgdata volume)"
