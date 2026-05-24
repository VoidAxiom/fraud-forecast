#!/usr/bin/env bash
# Entry-point for `make seed`. Runs the seed loader inside the compose stack
# against whatever DATABASE_URL the simulator service has configured.
set -euo pipefail
SCALE="${1:-1.0}"
WORKERS="${2:-8}"
SEED="${3:-42}"
docker compose --profile tools run --rm simulator \
  python -m simulator.seed --scale "$SCALE" --workers "$WORKERS" --seed "$SEED"
