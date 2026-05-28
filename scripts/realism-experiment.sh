#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/realism-experiment.sh HYPOTHESIS [--baseline-parquet PATH] [--baseline-report-dir PATH]

Runs one realism testbed hypothesis inside the Docker Compose app service.
USAGE
}

if [ "$#" -eq 0 ]; then
  echo "realism-experiment: hypothesis tag is required" >&2
  usage >&2
  exit 2
fi

case "$1" in
  -h|--help)
    usage
    exit 0
    ;;
  --*)
    echo "realism-experiment: first argument must be a hypothesis tag" >&2
    usage >&2
    exit 2
    ;;
esac

HYPOTHESIS="$1"
shift

BASELINE_PARQUET="${ML_DATA_DIR:-ml/data/training/latest.parquet}"
BASELINE_REPORT_DIR=""
REPORT_DIR="reports/realism/exp_${HYPOTHESIS}_$(date +%Y%m%d_%H%M%S)"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --baseline-parquet)
      if [ "$#" -lt 2 ]; then
        echo "realism-experiment: --baseline-parquet requires a path" >&2
        usage >&2
        exit 2
      fi
      BASELINE_PARQUET="$2"
      shift 2
      ;;
    --baseline-report-dir)
      if [ "$#" -lt 2 ]; then
        echo "realism-experiment: --baseline-report-dir requires a path" >&2
        usage >&2
        exit 2
      fi
      BASELINE_REPORT_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "realism-experiment: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

ARGS=(
  python -m ml.quality.testbed
  --hypothesis "$HYPOTHESIS"
  --baseline-parquet "$BASELINE_PARQUET"
  --report-dir "$REPORT_DIR"
)

if [ -n "$BASELINE_REPORT_DIR" ]; then
  ARGS+=(--baseline-report-dir "$BASELINE_REPORT_DIR")
fi

# docker compose inherits COMPOSE_PROJECT_NAME when the caller sets it.
docker compose run --rm app "${ARGS[@]}"

printf '%s\n' "$REPORT_DIR"
