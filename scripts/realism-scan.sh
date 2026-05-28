#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/realism-scan.sh [--input PATH] [--config PATH]

Runs the realism scanner inside the Docker Compose app service.
USAGE
}

INPUT="${ML_DATA_DIR:-ml/data/training/latest.parquet}"
CONFIG="${ML_QUALITY_CONFIG:-ml/quality/expected_distributions.yaml}"
REPORT_DIR="reports/realism/scan_$(date +%Y%m%d_%H%M%S)"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --input)
      if [ "$#" -lt 2 ]; then
        echo "realism-scan: --input requires a path" >&2
        usage >&2
        exit 2
      fi
      INPUT="$2"
      shift 2
      ;;
    --config)
      if [ "$#" -lt 2 ]; then
        echo "realism-scan: --config requires a path" >&2
        usage >&2
        exit 2
      fi
      CONFIG="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "realism-scan: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# docker compose inherits COMPOSE_PROJECT_NAME when the caller sets it.
docker compose run --rm \
  -v "$(pwd)/reports:/app/reports" \
  app python -m ml.quality.scanner \
  --input "$INPUT" \
  --config "$CONFIG" \
  --report-dir "$REPORT_DIR"

printf '%s\n' "$REPORT_DIR"
