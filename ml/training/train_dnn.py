"""Keras DNN training for transformed Phase 5 fraud TFRecords."""

from __future__ import annotations

import importlib
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import tensorflow as tf  # TensorFlow 2.3 lacks complete stubs.

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
FEATURE_ORDER: Tuple[str, ...] = FEATURE_NAMES
NUM_FEATURES = len(FEATURE_NAMES)
LABEL_NAME = "label"
_BATCH_SIZE = 512

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
    return not bool(tf.io.gfile.isdir(path))


def _resolve_tfrecord_files(tfrecord_path: str) -> List[str]:
    if _has_glob_pattern(tfrecord_path):
        matches = list(tf.io.gfile.glob(tfrecord_path))
    else:
        matches = list(tf.io.gfile.glob(os.path.join(tfrecord_path, "*.gz")))
        if not matches:
            matches = list(tf.io.gfile.glob(os.path.join(tfrecord_path, "*")))
        if not matches:
            matches = list(tf.io.gfile.glob(tfrecord_path))
        if not matches:
            matches = list(tf.io.gfile.glob(tfrecord_path + "*"))

    file_paths = sorted(path for path in matches if _is_file(path))
    if not file_paths:
        raise ValueError(f"No TFRecord files found for path: {tfrecord_path}")
    return file_paths


def _london_now() -> datetime:
    zoneinfo_module: Any
    try:
        zoneinfo_module = importlib.import_module("zoneinfo")
    except ImportError:
        zoneinfo_module = importlib.import_module("backports.zoneinfo")

    return datetime.now(zoneinfo_module.ZoneInfo("Europe/London"))


def _new_version() -> str:
    return f"{_london_now().strftime('v%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _data_autotune() -> Any:
    return getattr(tf.data, "AUTOTUNE", tf.data.experimental.AUTOTUNE)


def _parse_tfrecord_example(serialized_example: Any) -> Tuple[Any, Any]:
    parsed_features: Dict[str, Any] = tf.io.parse_single_example(
        serialized_example,
        _FEATURE_SPEC,
    )
    feature_vector = tf.stack(
        [tf.cast(parsed_features[feature_name], tf.float32) for feature_name in FEATURE_ORDER]
    )
    return feature_vector, tf.cast(parsed_features[LABEL_NAME], tf.int32)


def make_dataset(tfrecord_path: str) -> tf.data.Dataset:
    """Load TFRecords into a tf.data.Dataset of (feature_vector, label) tuples."""
    file_paths = _resolve_tfrecord_files(tfrecord_path)
    compression_type = "GZIP" if all(path.endswith(".gz") for path in file_paths) else ""
    dataset = tf.data.TFRecordDataset(file_paths, compression_type=compression_type)
    return dataset.map(_parse_tfrecord_example)


def build_dnn_model(input_dim: int) -> tf.keras.Model:
    inputs = tf.keras.Input(shape=(input_dim,))
    x = tf.keras.layers.Dense(256, activation="relu")(inputs)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.Dropout(0.3)(x)
    x = tf.keras.layers.Dense(128, activation="relu")(x)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.Dropout(0.3)(x)
    x = tf.keras.layers.Dense(64, activation="relu")(x)
    x = tf.keras.layers.Dropout(0.2)(x)
    x = tf.keras.layers.Dense(32, activation="relu")(x)
    output = tf.keras.layers.Dense(1, activation="sigmoid")(x)
    model = tf.keras.Model(inputs, output)
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=0.001),
        loss="binary_crossentropy",
        metrics=[
            tf.keras.metrics.AUC(curve="PR", name="auprc"),
            tf.keras.metrics.AUC(curve="ROC", name="auroc"),
            tf.keras.metrics.Precision(name="precision"),
            tf.keras.metrics.Recall(name="recall"),
        ],
    )
    return model


def train_dnn(
    train_ds: tf.data.Dataset,
    val_ds: tf.data.Dataset,
    hyperparams: Dict[str, Any],
) -> Tuple[tf.keras.Model, Any]:
    model = build_dnn_model(input_dim=NUM_FEATURES)
    class_weight = {0: 1.0, 1: 50.0}
    version = _new_version()
    checkpoint_path = Path("models") / "dnn" / version / "checkpoint"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    callbacks = [
        tf.keras.callbacks.EarlyStopping(
            monitor="val_auprc",
            mode="max",
            patience=5,
            restore_best_weights=True,
        ),
        tf.keras.callbacks.ReduceLROnPlateau(
            monitor="val_auprc",
            mode="max",
            factor=0.5,
            patience=3,
            min_lr=1e-6,
        ),
        tf.keras.callbacks.ModelCheckpoint(
            filepath=str(checkpoint_path),
            save_best_only=True,
            monitor="val_auprc",
            mode="max",
        ),
    ]

    history = model.fit(
        train_ds.batch(_BATCH_SIZE).prefetch(_data_autotune()),
        validation_data=val_ds.batch(_BATCH_SIZE),
        epochs=int(hyperparams.get("epochs", 30)),
        class_weight=class_weight,
        callbacks=callbacks,
    )

    return model, history


def _feature_matrix_from_dict(features: Dict[str, Any]) -> Any:
    feature_columns = [
        tf.reshape(tf.cast(features[feature_name], tf.float32), [-1, 1])
        for feature_name in FEATURE_ORDER
    ]
    return tf.concat(feature_columns, axis=1)


def _passthrough_feature_spec() -> Dict[str, Any]:
    return {feature_name: _FEATURE_SPEC[feature_name] for feature_name in FEATURE_ORDER}


def _tensor_specs_from_feature_spec(feature_spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        feature_name: tf.TensorSpec(shape=[None], dtype=spec.dtype, name=feature_name)
        for feature_name, spec in feature_spec.items()
    }


def build_serving_model(transform_fn_path: str, dnn_model: tf.keras.Model) -> str:
    import tensorflow_transform as tft

    tft_output = tft.TFTransformOutput(transform_fn_path)
    transform_layer: Any = None
    try:
        transform_layer = tft_output.transform_features_layer()
        feature_spec = tft_output.raw_feature_spec()
    except AttributeError:
        transform_layer = None
        feature_spec = _passthrough_feature_spec()

    def serve(raw_features: Dict[str, Any]) -> Any:
        if transform_layer is None:
            transformed_features = raw_features
        else:
            transformed_features = transform_layer(raw_features)
        x = _feature_matrix_from_dict(transformed_features)
        return dnn_model(x)

    version = _new_version()
    saved_model_path = str(Path("models") / "dnn" / version / "saved_model")
    serving_fn = tf.function(serve)
    serving_signature = serving_fn.get_concrete_function(
        _tensor_specs_from_feature_spec(feature_spec)
    )
    tf.saved_model.save(
        dnn_model,
        saved_model_path,
        signatures={"serving_default": serving_signature},
    )
    return saved_model_path


__all__ = [
    "FEATURE_NAMES",
    "FEATURE_ORDER",
    "FLOAT_FEATURE_NAMES",
    "INT_FEATURE_NAMES",
    "LABEL_NAME",
    "NUM_FEATURES",
    "build_dnn_model",
    "build_serving_model",
    "make_dataset",
    "train_dnn",
]
