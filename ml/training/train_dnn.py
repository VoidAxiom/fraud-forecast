"""Keras DNN training for transformed Phase 5 fraud TFRecords."""

from __future__ import annotations

import importlib
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
_LABEL_FIELD_NAMES = frozenset((LABEL_NAME, "gt_is_fraud"))
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


def compute_class_weight(train_ds: tf.data.Dataset) -> Dict[int, float]:
    neg = 0
    pos = 0
    for _feature_vector, label in train_ds:
        label_value = int(tf.reshape(label, []).numpy())
        if label_value == 0:
            neg += 1
        else:
            pos += 1

    if pos == 0:
        raise ValueError(
            f"Training dataset has no positive (fraud) examples "
            f"({neg} negatives, 0 positives). "
            "Cannot compute class weights for one-class data."
        )
    if neg == 0:
        raise ValueError(
            f"Training dataset has no negative (non-fraud) examples "
            f"(0 negatives, {pos} positives). "
            "Cannot compute class weights for one-class data."
        )
    return {0: 1.0, 1: float(neg) / float(pos)}


def train_dnn(
    train_ds: tf.data.Dataset,
    val_ds: tf.data.Dataset,
    hyperparams: Dict[str, Any],
) -> Tuple[tf.keras.Model, Any]:
    model = build_dnn_model(input_dim=NUM_FEATURES)
    class_weight = compute_class_weight(train_ds)
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
    # FEATURE_ORDER intentionally excludes LABEL_NAME; keep the fallback serving-only.
    return {
        feature_name: _FEATURE_SPEC[feature_name]
        for feature_name in FEATURE_ORDER
        if feature_name not in _LABEL_FIELD_NAMES
    }


def _is_training_label_field(feature_name: str) -> bool:
    return feature_name in _LABEL_FIELD_NAMES or feature_name.startswith(("gt_", "label"))


def _serving_feature_spec(feature_spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        feature_name: spec
        for feature_name, spec in feature_spec.items()
        if not _is_training_label_field(feature_name)
    }


def _tensor_specs_from_feature_spec(feature_spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        feature_name: tf.TensorSpec(shape=[None], dtype=spec.dtype, name=feature_name)
        for feature_name, spec in feature_spec.items()
    }


# TF 2.3 stubs expose Module as Any, but runtime tracking requires tf.Module.
class _ServingWrapper(tf.Module):  # type: ignore[misc]
    dnn_model: tf.keras.Model
    transform_layer: Any
    _inject_gt_is_fraud: bool
    _gt_is_fraud_dtype: Any

    def __init__(
        self,
        dnn_model: tf.keras.Model,
        transform_layer: Any,
        inject_gt_is_fraud: bool = False,
        gt_is_fraud_dtype: Any = tf.bool,
    ) -> None:
        super().__init__()
        self.dnn_model = dnn_model
        self.transform_layer = transform_layer
        self._inject_gt_is_fraud = inject_gt_is_fraud
        self._gt_is_fraud_dtype = gt_is_fraud_dtype

    # TF 2.3 stubs leave tf.function untyped, but SavedModel needs this trace.
    @tf.function  # type: ignore[misc]
    def serve(self, raw_features: Dict[str, Any]) -> Any:
        if self.transform_layer is None:
            transformed_features = raw_features
        else:
            if self._inject_gt_is_fraud:
                # gt_is_fraud is training-only and not part of the serving signature.
                batch_size = tf.shape(raw_features[next(iter(FEATURE_ORDER))])[0]
                features_with_label = dict(raw_features)
                features_with_label["gt_is_fraud"] = tf.zeros(
                    [batch_size],
                    dtype=self._gt_is_fraud_dtype,
                )
                transformed_features = self.transform_layer(features_with_label)
            else:
                transformed_features = self.transform_layer(raw_features)
        x = _feature_matrix_from_dict(transformed_features)
        return self.dnn_model(x)


def build_serving_model(
    transform_fn_path: str,
    dnn_model: tf.keras.Model,
    output_dir: str = "models/dnn",
    version: Optional[str] = None,
) -> str:
    import tensorflow_transform as tft

    tft_output = tft.TFTransformOutput(transform_fn_path)
    transform_layer: Any = None
    inject_gt_is_fraud = False
    try:
        tft_raw_spec = tft_output.raw_feature_spec()
        inject_gt_is_fraud = "gt_is_fraud" in tft_raw_spec
        gt_is_fraud_dtype = (
            tft_raw_spec["gt_is_fraud"].dtype if inject_gt_is_fraud else tf.bool
        )
        transform_layer = tft_output.transform_features_layer()
        feature_spec = _serving_feature_spec(tft_raw_spec)
    except AttributeError:
        transform_layer = None
        gt_is_fraud_dtype = tf.bool
        feature_spec = _passthrough_feature_spec()

    resolved_version = version or _new_version()
    saved_model_path = str(Path(output_dir) / resolved_version / "saved_model")
    wrapper = _ServingWrapper(
        dnn_model=dnn_model,
        transform_layer=transform_layer,
        inject_gt_is_fraud=inject_gt_is_fraud,
        gt_is_fraud_dtype=gt_is_fraud_dtype,
    )
    serving_signature = wrapper.serve.get_concrete_function(
        _tensor_specs_from_feature_spec(feature_spec)
    )
    tf.saved_model.save(
        wrapper,
        saved_model_path,
        signatures={"serving_default": serving_signature},
    )
    return saved_model_path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Train Keras DNN fraud model")
    parser.add_argument(
        "--tfrecord-path",
        default="ml/data/transformed",
        help="Path or glob to TFRecord files",
    )
    parser.add_argument(
        "--transform-fn-path",
        default=None,
        help="Path to TFT transform_fn directory (optional)",
    )
    parser.add_argument(
        "--output-dir",
        default="models/dnn",
        help="Base directory for model output",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=30,
        help="Number of training epochs",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Version string (default: auto-generated)",
    )
    args = parser.parse_args()

    full_ds = make_dataset(args.tfrecord_path)

    total = sum(1 for _ in full_ds)
    train_size = int(total * 0.8)
    val_size = int(total * 0.1)

    train_ds = full_ds.take(train_size)
    val_ds = full_ds.skip(train_size).take(val_size)
    test_ds = full_ds.skip(train_size + val_size)
    del test_ds

    hyperparams = {"epochs": args.epochs}
    print(f"Training DNN on {train_size} examples...")
    model, history = train_dnn(train_ds, val_ds, hyperparams)

    val_metrics = {
        key: values[-1]
        for key, values in history.history.items()
        if key.startswith("val_")
    }
    print(f"Final validation metrics: {val_metrics}")

    resolved_transform_fn: Optional[str] = args.transform_fn_path
    if resolved_transform_fn is None:
        candidate_transform_fn = str(Path(args.tfrecord_path) / "transform_fn")
        if os.path.isdir(candidate_transform_fn):
            resolved_transform_fn = candidate_transform_fn

    if resolved_transform_fn:
        saved_model_path = build_serving_model(
            resolved_transform_fn,
            model,
            output_dir=args.output_dir,
            version=args.version,
        )
        print(f"SavedModel exported to: {saved_model_path}")
    else:
        import warnings

        warnings.warn(
            "No transform_fn_path provided or found; saving bare Keras model "
            "without TFT preprocessing. "
            "Pass --transform-fn-path to include the TF Transform layer.",
            stacklevel=2,
        )
        version = args.version or _new_version()
        output_path = str(Path(args.output_dir) / version / "saved_model")
        Path(output_path).mkdir(parents=True, exist_ok=True)
        tf.saved_model.save(model, output_path)
        print(f"Model saved to: {output_path}")


__all__ = [
    "FEATURE_NAMES",
    "FEATURE_ORDER",
    "FLOAT_FEATURE_NAMES",
    "INT_FEATURE_NAMES",
    "LABEL_NAME",
    "NUM_FEATURES",
    "build_dnn_model",
    "build_serving_model",
    "compute_class_weight",
    "main",
    "make_dataset",
    "train_dnn",
]


if __name__ == "__main__":
    main()
