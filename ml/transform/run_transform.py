from __future__ import annotations

import argparse
import os
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime

import apache_beam as beam
import pandas as pd
import tensorflow as tf
import tensorflow_transform as tft
import tensorflow_transform.beam as tft_beam
from tensorflow_transform.tf_metadata import dataset_metadata, schema_utils

from ml.transform.preprocessing import preprocessing_fn

NUMERICAL_FEATURES = [
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
]

INTEGER_NUMERICAL_FEATURES = {
    "user_orders_1h_at_order_time",
    "user_orders_24h_at_order_time",
    "device_lifetime_order_count",
    "device_unique_users_lifetime",
    "user_lifetime_order_count",
    "item_count",
    "user_spend_24h_pence",
    "subtotal_pence",
    "total_pence",
    "time_to_checkout_seconds",
    "ip_unique_users_24h",
}

FLOAT_NUMERICAL_FEATURES = [
    feature_name
    for feature_name in NUMERICAL_FEATURES
    if feature_name not in INTEGER_NUMERICAL_FEATURES
]

LOW_CARD_CATEGORICAL = [
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
]

HIGH_CARD_HASH_FEATURES = {
    "card_bin": 1000,
    "card_issuer_bank": 100,
    "ip_country": 50,
    "store_city": 100,
    "browser_name": 30,
    "user_email_domain": 200,
}

BOOLEAN_FEATURES = [
    "is_first_order_for_user",
    "is_new_payment_method",
    "is_new_delivery_address",
    "is_guest_checkout",
    "is_digital_native_bank",
    "ip_is_proxy",
    "ip_is_vpn",
    "ip_is_tor",
    "ip_is_hosting",
]

STRING_FEATURES = (
    tuple(LOW_CARD_CATEGORICAL) + tuple(HIGH_CARD_HASH_FEATURES.keys()) + ("card_issuer_country",)
)

NULLABLE_CATEGORICALS = {"delivery_address_type", "cancellation_reason"}

FEATURE_SPEC = {
    feature_name: tf.io.FixedLenFeature([], tf.int64) for feature_name in INTEGER_NUMERICAL_FEATURES
}
FEATURE_SPEC.update(
    {
        feature_name: tf.io.FixedLenFeature([], tf.float32)
        for feature_name in FLOAT_NUMERICAL_FEATURES
    }
)
FEATURE_SPEC.update(
    {feature_name: tf.io.FixedLenFeature([], tf.string) for feature_name in STRING_FEATURES}
)
FEATURE_SPEC.update(
    {feature_name: tf.io.FixedLenFeature([], tf.int64) for feature_name in BOOLEAN_FEATURES}
)
FEATURE_SPEC["gt_is_fraud"] = tf.io.FixedLenFeature([], tf.int64)


def row_to_dict(row: Mapping[str, object]) -> dict[str, object]:
    converted: dict[str, object] = {}
    for feature_name, spec in FEATURE_SPEC.items():
        value = row.get(feature_name)
        if spec.dtype == tf.string:
            converted[feature_name] = (
                b"" if value is None else value.encode("utf-8") if isinstance(value, str) else value
            )
        elif spec.dtype == tf.bool:
            converted[feature_name] = False if value is None else bool(value)
        elif spec.dtype == tf.int64:
            converted[feature_name] = 0 if value is None else int(value)
        elif spec.dtype == tf.float32:
            converted[feature_name] = 0.0 if value is None else float(value)
        else:
            converted[feature_name] = value
    return converted


def raw_row_to_dict(row: Mapping[str, object]) -> dict[str, object]:
    return dict(row)


def _to_utc_datetime(value: object) -> datetime:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("placed_at must not be null")
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.to_pydatetime()


def _compute_placed_at_cutoffs(input_parquet: str) -> tuple[datetime, datetime]:
    placed_at_frame = pd.read_parquet(input_parquet, columns=["placed_at"])
    if "placed_at" not in placed_at_frame.columns:
        raise ValueError("input parquet must contain placed_at")

    placed_at = pd.to_datetime(placed_at_frame["placed_at"], utc=True)
    if placed_at.empty:
        raise ValueError("input parquet must contain at least one row")
    if placed_at.isna().any():
        raise ValueError("placed_at must not contain null values")

    return (
        _to_utc_datetime(placed_at.quantile(0.8)),
        _to_utc_datetime(placed_at.quantile(0.9)),
    )


def _split_bucket(
    row: Mapping[str, object],
    n_parts: int,
    train_cutoff: datetime,
    val_cutoff: datetime,
) -> int:
    if n_parts != 3:
        raise ValueError(f"Expected 3 partitions, got {n_parts}")

    placed_at = _to_utc_datetime(row.get("placed_at"))
    if placed_at < train_cutoff:
        return 0
    if placed_at < val_cutoff:
        return 1
    return 2


def run_pipeline(
    input_parquet: str,
    output_dir: str,
    preprocessing_fn: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    raw_metadata = dataset_metadata.DatasetMetadata(
        schema_utils.schema_from_feature_spec(FEATURE_SPEC)
    )
    temp_dir = tempfile.mkdtemp()
    train_cutoff, val_cutoff = _compute_placed_at_cutoffs(input_parquet)
    for split_name in ("train", "val", "test"):
        os.makedirs(os.path.join(output_dir, split_name), exist_ok=True)

    with beam.Pipeline(runner="DirectRunner") as p:
        raw_data = (
            p
            | "ReadParquet" >> beam.io.ReadFromParquet(input_parquet)
            | "ToRawDict" >> beam.Map(raw_row_to_dict)
        )
        train_raw, val_raw, test_raw = (
            raw_data
            | "SplitByTime"
            >> beam.Partition(_split_bucket, 3, train_cutoff, val_cutoff)
        )
        train_features = train_raw | "TrainToDict" >> beam.Map(row_to_dict)
        val_features = val_raw | "ValToDict" >> beam.Map(row_to_dict)
        test_features = test_raw | "TestToDict" >> beam.Map(row_to_dict)

        with tft_beam.Context(temp_dir=temp_dir):
            (train_transformed_dataset, transform_fn) = (
                train_features,
                raw_metadata,
            ) | "AnalyzeAndTransformTrain" >> tft_beam.AnalyzeAndTransformDataset(
                preprocessing_fn
            )
            train_transformed, transformed_metadata = train_transformed_dataset
            val_transformed_dataset = (
                (val_features, raw_metadata),
                transform_fn,
            ) | "TransformVal" >> tft_beam.TransformDataset()
            val_transformed, _ = val_transformed_dataset
            test_transformed_dataset = (
                (test_features, raw_metadata),
                transform_fn,
            ) | "TransformTest" >> tft_beam.TransformDataset()
            test_transformed, _ = test_transformed_dataset

            coder = tft.coders.ExampleProtoCoder(transformed_metadata.schema)

            for split_name, split_pc in (
                ("train", train_transformed),
                ("val", val_transformed),
                ("test", test_transformed),
            ):
                _ = (
                    split_pc
                    | f"Encode_{split_name}" >> beam.Map(coder.encode)
                    | f"Write_{split_name}"
                    >> beam.io.WriteToTFRecord(
                        os.path.join(output_dir, split_name, "shard"),
                        file_name_suffix=".tfrecord.gz",
                    )
                )

            _ = transform_fn | "WriteTransformFn" >> tft_beam.WriteTransformFn(output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TF Transform preprocessing pipeline.")
    parser.add_argument("--input-parquet", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    run_output_dir = os.path.join(args.output_dir, args.run_id)
    run_pipeline(args.input_parquet, run_output_dir, preprocessing_fn)


if __name__ == "__main__":
    main()
