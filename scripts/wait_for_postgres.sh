#!/usr/bin/env bash
set -euo pipefail
TIMEOUT="${POSTGRES_WAIT_TIMEOUT:-30}"
start=$(date +%s)
while true; do
  if docker compose exec -T postgres pg_isready -U app -d fraud_platform >/dev/null 2>&1; then
    echo "postgres ready"
    exit 0
  fi
  elapsed=$(( $(date +%s) - start ))
  if (( elapsed >= TIMEOUT )); then
    echo "postgres did not become ready within ${TIMEOUT}s" >&2
    docker compose ps >&2
    exit 1
  fi
  sleep 1
done
