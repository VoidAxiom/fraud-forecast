#!/usr/bin/env bash
# Phase 5 live-on-main training pipeline.
# Orchestrates: data_loader -> parquet -> TFT preprocess -> XGBoost -> DNN -> XGBoost eval.
# Run from primary checkout. Reads from primary Postgres + Redis.
set -uo pipefail
# Note: NOT -e -- we want subsequent steps to run even if step 3 (XGBoost) fails,
# so we can surface step 4 (DNN) and step 5 (eval) bugs in the same run.
# Each step prints its exit status; tail the log to spot failures.

cd "$(dirname "$0")/.."

RUN_ID="${RUN_ID:-run_$(date +%Y%m%d_%H%M%S)}"
PARQUET_PATH="ml/data/training/latest.parquet"
TRANSFORM_OUTPUT_DIR="ml/data/transformed"
TRANSFORM_RUN_DIR="${TRANSFORM_OUTPUT_DIR}/${RUN_ID}"
TRAIN_DAYS="${TRAIN_DAYS:-90}"
LABEL_BUFFER_DAYS="${LABEL_BUFFER_DAYS:-45}"

echo "=== Phase 5 live-on-main training pipeline ==="
echo "RUN_ID:              $RUN_ID"
echo "TRAIN_DAYS:          $TRAIN_DAYS (rolling window, default covers finalized-label orders)"
echo "LABEL_BUFFER_DAYS:   $LABEL_BUFFER_DAYS (chargeback finalisation buffer; set to 0 for fresh data)"
echo "PARQUET_PATH:        $PARQUET_PATH"
echo "TRANSFORM_RUN:       $TRANSFORM_RUN_DIR"
echo "---"

echo "[1/5] Generating training parquet via data_loader..."
docker compose --profile tools run --rm app python -c "
from datetime import datetime, timezone, timedelta
from pathlib import Path
from ml.training.data_loader import TrainingDataConfig, load_training_data

end = datetime.now(timezone.utc)
start = end - timedelta(days=${TRAIN_DAYS})
config = TrainingDataConfig(start_date=start, end_date=end, label_finalisation_buffer_days=${LABEL_BUFFER_DAYS})
df = load_training_data(config)
out = Path('$PARQUET_PATH')
out.parent.mkdir(parents=True, exist_ok=True)
df.to_parquet(out)
print(f'Saved {len(df)} rows to {out}')
print(f'Fraud rate: {df[\"gt_is_fraud\"].mean()*100:.2f}%')
print(f'Date range: {df[\"placed_at\"].min()} -> {df[\"placed_at\"].max()}')
"
STEP1_EXIT=$?
if [ $STEP1_EXIT -ne 0 ]; then
  echo "ERROR: step 1 (data_loader) failed with exit $STEP1_EXIT -- aborting"
  exit $STEP1_EXIT
fi

echo ""
echo "[2/5] TFT preprocessing (run_transform) -> $TRANSFORM_RUN_DIR..."
make train \
  TRAIN_INPUT_PARQUET="$PARQUET_PATH" \
  TRAIN_OUTPUT_DIR="$TRANSFORM_OUTPUT_DIR" \
  TRAIN_RUN_ID="$RUN_ID"
STEP2_EXIT=$?
if [ $STEP2_EXIT -ne 0 ]; then
  echo "ERROR: step 2 (TFT preprocessing) failed with exit $STEP2_EXIT -- aborting"
  exit $STEP2_EXIT
fi

echo ""
echo "[3/5] XGBoost training..."
STEP3_START=$(date +%s)
docker compose --profile tools run --rm app python -c "
from ml.training.train_xgboost import train_xgboost

train_path = '$TRANSFORM_RUN_DIR/train'
val_path = '$TRANSFORM_RUN_DIR/val'
hyperparams = {}
booster, metrics = train_xgboost(train_path, val_path, hyperparams)
print(f'XGBoost done. Metrics: {metrics}')
"

XGB_DIR=$(ls -td models/xgboost/v*/ 2>/dev/null | head -1)
if [ -z "$XGB_DIR" ]; then
  echo "ERROR: step 3 produced no XGBoost model directory under models/xgboost/v*/ -- step 3 may have failed"
  echo "Skipping step 5 (evaluate). Check step 3 output above."
  exit 1
fi
DIR_MTIME=$(stat -c %Y "$XGB_DIR" 2>/dev/null || stat -f %m "$XGB_DIR" 2>/dev/null)
if [ -n "$DIR_MTIME" ] && { [ "$DIR_MTIME" -lt "$STEP3_START" ] || [ "$(($(date +%s) - DIR_MTIME))" -gt 1800 ]; }; then
  echo "ERROR: newest XGBoost model dir ${XGB_DIR} was not created during step 3 or is older than 30 minutes -- step 3 may have failed (stale artifact from prior run)"
  echo "Skipping step 5 (evaluate). Check step 3 output above."
  exit 1
fi
XGB_MODEL_PATH="${XGB_DIR}model.bin"
echo "XGBoost model: $XGB_MODEL_PATH"

echo ""
echo "[4/5] DNN training..."
docker compose --profile tools run --rm app python -m ml.training.train_dnn \
  --version "$RUN_ID" \
  --epochs "${DNN_EPOCHS:-5}"
STEP4_EXIT=$?
if [ $STEP4_EXIT -ne 0 ]; then
  echo "ERROR: step 4 (DNN training) failed with exit $STEP4_EXIT -- aborting"
  exit $STEP4_EXIT
fi

echo ""
echo "[5/5] XGBoost evaluation..."
# evaluate scores the XGBoost model against the transformed test split.
docker compose --profile tools run --rm app python -m ml.training.evaluate \
  --version "$RUN_ID" \
  --model-path "$XGB_MODEL_PATH" \
  --test-data-path "$TRANSFORM_RUN_DIR/test" \
  --reports-dir "$TRANSFORM_RUN_DIR/reports"
STEP5_EXIT=$?
if [ $STEP5_EXIT -ne 0 ]; then
  echo "ERROR: step 5 (XGBoost evaluation) failed with exit $STEP5_EXIT -- aborting"
  exit $STEP5_EXIT
fi

echo ""
echo "=== DONE ==="
echo "Reports:   $TRANSFORM_RUN_DIR/reports/$RUN_ID/"
echo "Metrics:   $TRANSFORM_RUN_DIR/reports/$RUN_ID/metrics.json"
echo ""
echo "Acceptance check (spec/PHASE_5.md):"
echo "  AUPRC >= 0.75"
echo "  stolen_card recall >= 0.70"
echo "  account_takeover recall >= 0.60"
echo "  promo_abuse recall >= 0.80"
echo "  others recall >= 0.40"
echo ""
METRICS_PATH="$TRANSFORM_RUN_DIR/reports/$RUN_ID/metrics.json"
if [ -f "$METRICS_PATH" ]; then
  echo "metrics.json found: $METRICS_PATH"
  cat "$METRICS_PATH" | python3 -m json.tool
  python3 -c '
import json
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as metrics_file:
    metrics = json.load(metrics_file)

thresholds = {
    "auprc": 0.70,
    "recall_stolen_card": 0.70,
    "recall_account_takeover": 0.60,
    "recall_promo_abuse": 0.80,
    "recall_refund_abuse": 0.40,
    "recall_collusive_merchant": 0.40,
    "recall_triangulation": 0.40,
    "recall_reseller": 0.40,
}

failures = []
for key, threshold in thresholds.items():
    value = metrics.get(key)
    if value is None:
        print(f"  WARNING: {key} not in metrics.json (category absent from test set) -- skipped")
        continue
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        failures.append(f"{key} has non-numeric value {value!r} (target >= {threshold:.2f})")
        continue
    if numeric_value < threshold:
        failures.append(f"{key} {numeric_value:.6f} < {threshold:.2f}")

if failures:
    print("Phase 5 acceptance threshold failures:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)

print("Phase 5 acceptance thresholds met.")
' "$METRICS_PATH"
  METRICS_EXIT=$?
  if [ $METRICS_EXIT -ne 0 ]; then
    exit $METRICS_EXIT
  fi
else
  echo "metrics.json not produced at $METRICS_PATH -- check step 5 errors above"
  exit 1
fi
