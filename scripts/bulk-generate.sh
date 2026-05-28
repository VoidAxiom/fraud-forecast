#!/usr/bin/env bash
# bulk-generate.sh — generate N days of past-dated orders deterministically.
# Default rate produces ~6M orders for a 30-day window (rate_multiplier=0.05)
#
# Feature aggregator approach (option a):
#   Stops feature_aggregator before bulk so NOTIFY triggers fire into void.
#   Redis stays empty until the aggregator restarts and rebuilds.
#   Phase 5 training reads Postgres directly — empty Redis is fine.
#   Phase 6 scoring eval needs a separate warm-from-Postgres packet first.
#
# Usage:
#   bash scripts/bulk-generate.sh --days 30 --end-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --seed 42
#
# Environment:
#   COMPOSE_PROJECT_NAME — defaults to fraud-forecast
#   BULK_RATE_MULTIPLIER — passed to python -m simulator.bulk_generate (default 0.05)
#   COMPOSE_FILE — if set, passed through to docker compose

# Known limitations (v1):
#   KNOWN LIMITATION (v1, tracked in VOI-324): bulk-generated orders use the
#   current state of promos and aggregate windows (NOW()-relative), not the
#   historical state at each order's placed_at. For Phase 5 v1 evaluation
#   this is acceptable since promos are mostly static seed data; for
#   publication-quality training data, VOI-324 fixes this.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-fraud-forecast}"
BULK_RATE_MULTIPLIER="${BULK_RATE_MULTIPLIER:-0.05}"
export COMPOSE_PROJECT_NAME

restart_feature_aggregator() {
  local status=$?

  echo "[bulk-generate] restarting feature_aggregator..."
  docker compose start feature_aggregator 2>/dev/null || true

  if [ "$status" -eq 0 ]; then
    echo "[bulk-generate] completed successfully."
  else
    echo "[bulk-generate] completed with exit status $status." >&2
  fi

  exit "$status"
}

trap restart_feature_aggregator EXIT

cd "$REPO_ROOT"

echo "[bulk-generate] starting bulk generation."
echo "[bulk-generate] COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME BULK_RATE_MULTIPLIER=$BULK_RATE_MULTIPLIER"
echo "[bulk-generate] stopping feature_aggregator..."
docker compose stop feature_aggregator

echo "[bulk-generate] running simulator.bulk_generate; final output line is summary JSON."
docker compose run --rm \
  -e BULK_RATE_MULTIPLIER="$BULK_RATE_MULTIPLIER" \
  app python -m simulator.bulk_generate "$@"
