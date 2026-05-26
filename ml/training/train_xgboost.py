"""XGBoost training for transformed Phase 5 fraud TFRecords."""

from __future__ import annotations

import importlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import tensorflow as tf  # type: ignore[import-untyped]  # TensorFlow 2.3 lacks complete stubs.
import xgboost as xgb

# The packet requires Python 3.8-compatible typing names here.
# ruff: noqa: UP006

FLOAT_FEATURE_NAMES: Tuple[str, ...] = (
    "user_account_age_days",
    "user_lifetime_order_count",
    "user_lifetime_chargeback_rate",
    "user_orders_1h_at_order_time",
    "user_orders_24h_at_order_time",
    "user_spend_24h_pence",
    "device_lifetime_order_count",
    "device_unique_users_lifetime",
    "payment_lifetime_chargeback_rate",
    "ip_unique_users_24h",
    "store_chargeback_rate",
    "merchant_chargeback_rate",
    "email_domain_chargeback_rate",
    "subtotal_pence",
    "total_pence",
    "item_count",
    "delivery_distance_km",
    "ip_to_delivery_distance_km",
    "billing_to_delivery_distance_km",
    "time_to_checkout_seconds",
    "geo_mismatch_score",
    "velocity_ratio_1h_vs_lifetime",
)

INT_FEATURE_NAMES: Tuple[str, ...] = (
    "order_channel",
    "order_type",
    "payment_type",
    "card_brand",
    "card_funding_type",
    "device_type",
    "platform",
    "merchant_category",
    "delivery_address_type",
    "cancellation_reason",
    "card_bin",
    "card_issuer_bank",
    "ip_country",
    "store_city",
    "browser_name",
    "user_email_domain",
    "is_first_order_for_user",
    "is_new_payment_method",
    "is_new_delivery_address",
    "is_guest_checkout",
    "is_digital_native_bank",
    "ip_is_proxy",
    "ip_is_vpn",
    "ip_is_tor",
    "ip_is_hosting",
    "card_country_mismatch",
)

FEATURE_NAMES: Tuple[str, ...] = FLOAT_FEATURE_NAMES + INT_FEATURE_NAMES
LABEL_NAME = "label"
_BATCH_SIZE = 4096

_FEATURE_SPEC: Dict[str, Any] = {
    feature_name: tf.io.FixedLenFeature([], tf.float32) for feature_name in FLOAT_FEATURE_NAMES
}
_FEATURE_SPEC.update(
    {feature_name: tf.io.FixedLenFeature([], tf.int64) for feature_name in INT_FEATURE_NAMES}
)
_FEATURE_SPEC[LABEL_NAME] = tf.io.FixedLenFeature([], tf.int64)


def _has_glob_pattern(path: str) -> bool:
    return any(character in path for character in "*?[]")


def _is_file(path: str) -> bool:
    return not tf.io.gfile.isdir(path)


def _resolve_tfrecord_files(tfrecord_path: str) -> List[str]:
    if _has_glob_pattern(tfrecord_path):
        matches = tf.io.gfile.glob(tfrecord_path)
    else:
        matches = tf.io.gfile.glob(os.path.join(tfrecord_path, "*.gz"))
        if not matches:
            matches = tf.io.gfile.glob(os.path.join(tfrecord_path, "*"))
        if not matches:
            matches = tf.io.gfile.glob(tfrecord_path)
        if not matches:
            matches = tf.io.gfile.glob(tfrecord_path + "*")

    file_paths = sorted(path for path in matches if _is_file(path))
    if not file_paths:
        raise ValueError(f"No TFRecord files found for path: {tfrecord_path}")
    return file_paths


def tfrecords_to_numpy(tfrecord_path: str) -> Tuple[np.ndarray, np.ndarray]:  # type: ignore[type-arg]  # Packet requires exact np.ndarray return signature.
    file_paths = _resolve_tfrecord_files(tfrecord_path)
    compression_type = "GZIP" if all(path.endswith(".gz") for path in file_paths) else ""
    dataset = tf.data.TFRecordDataset(file_paths, compression_type=compression_type)

    x_batches: List[Any] = []
    y_batches: List[Any] = []
    for serialized_batch in dataset.batch(_BATCH_SIZE):
        parsed_features: Dict[str, Any] = tf.io.parse_example(
            serialized_batch,
            _FEATURE_SPEC,
        )
        feature_columns: List[Any] = [
            tf.cast(parsed_features[feature_name], tf.float32).numpy()
            for feature_name in FEATURE_NAMES
        ]
        x_batch = np.stack(feature_columns, axis=1).astype(np.float32, copy=False)
        y_batch = parsed_features[LABEL_NAME].numpy().astype(np.int64, copy=False)
        x_batches.append(x_batch)
        y_batches.append(y_batch)

    if not x_batches:
        return (
            np.empty((0, len(FEATURE_NAMES)), dtype=np.float32),
            np.empty((0,), dtype=np.int64),
        )

    return np.concatenate(x_batches, axis=0), np.concatenate(y_batches, axis=0)


def _london_now() -> datetime:
    try:
        zoneinfo_module = importlib.import_module("zoneinfo")
    except ImportError:
        zoneinfo_module = importlib.import_module("backports.zoneinfo")

    zone_info = zoneinfo_module.ZoneInfo
    return datetime.now(zone_info("Europe/London"))


def _best_val_aucpr(evals_result: Dict[str, Dict[str, List[float]]]) -> float:
    val_aucpr = evals_result.get("val", {}).get("aucpr", [])
    if not val_aucpr:
        raise RuntimeError("XGBoost training did not report val aucpr")
    return max(float(metric_value) for metric_value in val_aucpr)


def _best_iteration(model: xgb.Booster, evals_result: Dict[str, Dict[str, List[float]]]) -> int:
    best_iteration = getattr(model, "best_iteration", None)
    if best_iteration is not None:
        return int(best_iteration)

    val_aucpr = evals_result.get("val", {}).get("aucpr", [])
    return max(len(val_aucpr) - 1, 0)


def _json_compatible(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return value


def _save_training_artifacts(
    model: xgb.Booster,
    params: Dict[str, Any],
    evals_result: Dict[str, Dict[str, List[float]]],
    num_train_rows: int,
    num_val_rows: int,
) -> None:
    trained_at = _london_now()
    version = trained_at.strftime("v%Y%m%d_%H%M%S")
    model_dir = Path("models") / "xgboost" / version
    model_dir.mkdir(parents=True, exist_ok=True)

    model.save_model(str(model_dir / "model.bin"))
    (model_dir / "feature_names.json").write_text(
        json.dumps(FEATURE_NAMES, indent=2) + "\n",
        encoding="utf-8",
    )

    val_auprc = _best_val_aucpr(evals_result)
    metadata: Dict[str, Any] = {
        "version": version,
        "best_iteration": _best_iteration(model, evals_result),
        "best_score": val_auprc,
        "hyperparams": _json_compatible(params),
        "trained_at": trained_at.isoformat(),
        "num_features": len(FEATURE_NAMES),
        "num_train_rows": num_train_rows,
        "num_val_rows": num_val_rows,
        "val_auprc": val_auprc,
    }
    (model_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def train_xgboost(
    train_data: str,
    val_data: str,
    hyperparams: Dict[str, Any],
) -> Tuple[xgb.Booster, Dict[str, float]]:
    """
    Reads TFRecords (uses tf.data -> numpy), trains XGBoost.
    """
    X_train, y_train = tfrecords_to_numpy(train_data)
    X_val, y_val = tfrecords_to_numpy(val_data)

    # Class imbalance: ~2% positives. Use scale_pos_weight.
    scale_pos_weight = (y_train == 0).sum() / (y_train == 1).sum()

    params: Dict[str, Any] = {
        "objective": "binary:logistic",
        "eval_metric": ["aucpr", "logloss"],
        "max_depth": hyperparams.get("max_depth", 8),
        "learning_rate": hyperparams.get("learning_rate", 0.05),
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_weight": 5,
        "scale_pos_weight": scale_pos_weight,
        "tree_method": "hist",
        "n_jobs": -1,
    }

    feature_names = list(FEATURE_NAMES)
    dtrain = xgb.DMatrix(X_train, label=y_train, feature_names=feature_names)
    dval = xgb.DMatrix(X_val, label=y_val, feature_names=feature_names)
    evals_result: Dict[str, Dict[str, List[float]]] = {}

    model = xgb.train(
        params,
        dtrain,
        num_boost_round=500,
        evals=[(dtrain, "train"), (dval, "val")],
        early_stopping_rounds=30,
        evals_result=evals_result,
        verbose_eval=10,
    )

    # Feature importance (gain) for monitoring.
    raw_importance: Dict[str, Any] = model.get_score(importance_type="gain")
    importance = {
        feature_name: float(importance_score)
        for feature_name, importance_score in raw_importance.items()
    }

    _save_training_artifacts(
        model,
        params,
        evals_result,
        num_train_rows=int(X_train.shape[0]),
        num_val_rows=int(X_val.shape[0]),
    )

    return model, importance


__all__ = ["FEATURE_NAMES", "tfrecords_to_numpy", "train_xgboost"]
