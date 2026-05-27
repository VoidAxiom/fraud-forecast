from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Protocol, Sequence, Tuple, Type, cast
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
import tensorflow as tf
import xgboost as xgb

from ml.training.data_loader import _REQUIRED_COLUMNS, TrainingDataConfig, load_training_data
from ml.training.train_dnn import NUM_FEATURES, build_dnn_model, build_serving_model
from ml.transform.preprocessing import preprocessing_fn
from ml.transform.run_transform import FEATURE_SPEC as _PRODUCTION_FEATURE_SPEC
from tests.fixtures.synthetic_training_data import make_synthetic_df

# The packet requires Python 3.8-compatible typing names here.
# ruff: noqa: UP006


class _TrainXGBoostModule(Protocol):
    FLOAT_FEATURE_NAMES: Tuple[str, ...]
    INT_FEATURE_NAMES: Tuple[str, ...]
    FEATURE_NAMES: Tuple[str, ...]

    def tfrecords_to_numpy(
        self,
        tfrecord_path: str,
    ) -> Tuple[np.ndarray[Any, np.dtype[Any]], np.ndarray[Any, np.dtype[Any]]]: ...

    def train_xgboost(
        self,
        train_data: str,
        val_data: str,
        hyperparams: Dict[str, object],
    ) -> Tuple[xgb.Booster, Dict[str, float]]: ...


class _TrainDnnModule(Protocol):
    FLOAT_FEATURE_NAMES: Tuple[str, ...]
    INT_FEATURE_NAMES: Tuple[str, ...]
    FEATURE_ORDER: Tuple[str, ...]
    NUM_FEATURES: int

    def build_dnn_model(self, input_dim: int) -> tf.keras.Model: ...

    def train_dnn(
        self,
        train_ds: tf.data.Dataset,
        val_ds: tf.data.Dataset,
        hyperparams: Dict[str, object],
    ) -> Tuple[tf.keras.Model, object]: ...


class _EvaluateModule(Protocol):
    FRAUD_CATEGORIES: Tuple[str, ...]

    def evaluate(
        self,
        model_path: str,
        test_data: Tuple[np.ndarray[Any, np.dtype[Any]], np.ndarray[Any, np.dtype[Any]]],
        ground_truth_categories: np.ndarray[Any, np.dtype[Any]],
        report_dir: str,
    ) -> Dict[str, object]: ...

    def best_ensemble_weights(
        self,
        xgb_scores: np.ndarray[Any, np.dtype[Any]],
        dnn_scores: np.ndarray[Any, np.dtype[Any]],
        y_test: np.ndarray[Any, np.dtype[Any]],
    ) -> Tuple[float, float]: ...


class _PromotionModule(Protocol):
    PromotionGateError: Type[Exception]

    def promote(
        self,
        version: str,
        model_type: str,
        force: bool = False,
        registry_root: Path = Path("models"),
        reports_root: Path = Path("ml/training/reports"),
    ) -> Dict[str, object]: ...


class _ModelRegistry(Protocol):
    def promote(self, model_type: str, version: str) -> None: ...

    def get_current(self, model_type: str) -> str: ...


class _ModelRegistryFactory(Protocol):
    def __call__(self, root: Path) -> _ModelRegistry: ...


class _PreprocessingModule(Protocol):
    tft: object

    def preprocessing_fn(self, inputs: Dict[str, object]) -> Dict[str, object]: ...


class _FakeTft:
    def __init__(self) -> None:
        self.vocabulary_calls: List[Tuple[str, int]] = []

    def scale_to_z_score(self, value: object) -> object:
        return tf.cast(value, tf.float32)

    def compute_and_apply_vocabulary(
        self,
        values: object,
        top_k: int,
        num_oov_buckets: int,
        vocab_filename: str,
    ) -> object:
        del top_k
        self.vocabulary_calls.append((vocab_filename, num_oov_buckets))
        return tf.zeros(tf.shape(values), dtype=tf.int64)

    def hash_strings(self, values: object, hash_buckets: int) -> object:
        del hash_buckets
        return tf.zeros(tf.shape(values), dtype=tf.int64)


def _train_xgboost_module() -> _TrainXGBoostModule:
    return cast(
        _TrainXGBoostModule,
        importlib.import_module("ml.training.train_xgboost"),
    )


def _train_dnn_module() -> _TrainDnnModule:
    return cast(_TrainDnnModule, importlib.import_module("ml.training.train_dnn"))


def _evaluate_module() -> _EvaluateModule:
    return cast(_EvaluateModule, importlib.import_module("ml.training.evaluate"))


def _promote_module() -> _PromotionModule:
    return cast(_PromotionModule, importlib.import_module("ml.training.promote"))


def _preprocessing_module() -> _PreprocessingModule:
    return cast(
        _PreprocessingModule,
        importlib.import_module("ml.transform.preprocessing"),
    )


def _model_registry(root: Path) -> _ModelRegistry:
    registry_module = importlib.import_module("ml.registry.model_registry")
    registry_cls = cast(_ModelRegistryFactory, vars(registry_module)["ModelRegistry"])
    return registry_cls(root)


def _serialized_example(
    label: int,
    row_index: int,
    float_features: Sequence[str],
    int_features: Sequence[str],
) -> bytes:
    features = {
        feature_name: tf.train.Feature(
            float_list=tf.train.FloatList(value=[float(row_index) + 0.5]),
        )
        for feature_name in float_features
    }
    features.update(
        {
            feature_name: tf.train.Feature(
                int64_list=tf.train.Int64List(value=[row_index + 1]),
            )
            for feature_name in int_features
        },
    )
    features["label"] = tf.train.Feature(int64_list=tf.train.Int64List(value=[label]))
    example = tf.train.Example(features=tf.train.Features(feature=features))
    return bytes(example.SerializeToString())


def _write_tfrecord(
    directory: Path,
    labels: Sequence[int],
    float_features: Sequence[str],
    int_features: Sequence[str],
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tfrecord_path = directory / "part-00000-of-00001"
    writer = tf.io.TFRecordWriter(str(tfrecord_path))
    try:
        for row_index, label in enumerate(labels):
            writer.write(
                _serialized_example(
                    label=label,
                    row_index=row_index,
                    float_features=float_features,
                    int_features=int_features,
                ),
            )
    finally:
        writer.close()
    return tfrecord_path


def _raw_tft_feature_spec(include_row_id: bool = False) -> Dict[str, object]:
    feature_spec: Dict[str, object] = dict(_PRODUCTION_FEATURE_SPEC)
    if include_row_id:
        feature_spec["row_id"] = tf.io.FixedLenFeature([], tf.int64)
    return feature_spec


def _raw_tft_row(
    card_brand: bytes = b"gb",
    row_id: int = 0,
    include_row_id: bool = False,
) -> Dict[str, object]:
    row: Dict[str, object] = {}
    for feature_name, spec in _PRODUCTION_FEATURE_SPEC.items():
        if spec.dtype == tf.float32:
            row[feature_name] = 1.0
        elif spec.dtype == tf.int64:
            row[feature_name] = 1
        elif spec.dtype == tf.bool:
            row[feature_name] = False
        else:
            row[feature_name] = b"gb"
    row["card_brand"] = card_brand
    if include_row_id:
        row["row_id"] = row_id
    return row


def _raw_preprocessing_inputs() -> Dict[str, object]:
    numeric_features = (
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
    )
    categorical_features = {
        "order_channel": b"app",
        "order_type": b"delivery",
        "payment_type": b"card",
        "card_brand": b"UNSEEN_BRAND",
        "card_funding_type": b"debit",
        "device_type": b"mobile",
        "platform": b"ios",
        "merchant_category": b"restaurant",
        "delivery_address_type": b"",
        "cancellation_reason": b"",
        "card_bin": b"400000",
        "card_issuer_bank": b"barclays",
        "ip_country": b"GB",
        "store_city": b"London",
        "browser_name": b"Chrome",
        "user_email_domain": b"example.com",
        "card_issuer_country": b"IE",
    }
    boolean_features = (
        "is_first_order_for_user",
        "is_new_payment_method",
        "is_new_delivery_address",
        "is_guest_checkout",
        "is_digital_native_bank",
        "ip_is_proxy",
        "ip_is_vpn",
        "ip_is_tor",
        "ip_is_hosting",
    )

    inputs: Dict[str, object] = {
        feature_name: tf.constant([1.0], dtype=tf.float32) for feature_name in numeric_features
    }
    inputs.update(
        {
            feature_name: tf.constant([value])
            for feature_name, value in categorical_features.items()
        },
    )
    inputs.update(
        {feature_name: tf.constant([False], dtype=tf.bool) for feature_name in boolean_features},
    )
    inputs["gt_is_fraud"] = tf.constant([False], dtype=tf.bool)
    return inputs


def _write_metrics(reports_root: Path, version: str, auprc: float) -> None:
    metrics = {
        "auprc": auprc,
        "auroc": 0.9,
        "brier_score": 0.1,
    }
    metrics_path = reports_root / version / "metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, sort_keys=True) + "\n", encoding="utf-8")


def test_data_loader_excludes_unfinalised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert tmp_path.exists()
    placed_at = [
        datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 2, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 3, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 25, 10, 0, 0, tzinfo=timezone.utc),
    ]
    mock_cutoff = datetime(2026, 5, 26, tzinfo=timezone.utc)
    return_df = make_synthetic_df(n_rows=5, n_fraud=0, seed=1)
    return_df["order_id"] = ["old-1", "old-2", "old-3", "recent-1", "recent-2"]
    return_df["placed_at"] = placed_at

    engine = MagicMock()
    get_engine_mock = MagicMock(return_value=engine)
    monkeypatch.setattr("ml.training.data_loader.get_engine", get_engine_mock)

    def fake_read_sql_query(
        query: object,
        conn: object,
        params: Mapping[str, object],
    ) -> pd.DataFrame:
        del conn
        query_text = str(query)
        buffer_expression = (
            "NOW() - (CAST(:label_finalisation_buffer_days AS INTEGER) * INTERVAL '1 day')"
        )
        assert buffer_expression in query_text
        assert "label_finalisation_buffer_days" in params
        assert params == {
            "start_date": placed_at[0] - timedelta(hours=1),
            "end_date": placed_at[-1] + timedelta(hours=1),
            "label_finalisation_buffer_days": 45,
        }
        buffer_days = params["label_finalisation_buffer_days"]
        assert isinstance(buffer_days, int)
        cutoff = mock_cutoff - timedelta(days=int(buffer_days))
        return return_df[return_df["placed_at"] < cutoff]

    def fake_to_parquet(self: pd.DataFrame, path: object, index: bool = False) -> None:
        del self, path, index

    monkeypatch.setattr("ml.training.data_loader.pd.read_sql_query", fake_read_sql_query)
    monkeypatch.setattr("ml.training.data_loader.pd.DataFrame.to_parquet", fake_to_parquet)

    config = TrainingDataConfig(
        start_date=placed_at[0] - timedelta(hours=1),
        end_date=placed_at[-1] + timedelta(hours=1),
        label_finalisation_buffer_days=45,
    )
    result = load_training_data(config)

    get_engine_mock.assert_called_once_with(role="training")
    result_order_ids = result["order_id"].tolist()
    assert len(result) == 3
    assert "old-1" in result_order_ids
    assert "old-2" in result_order_ids
    assert "old-3" in result_order_ids
    assert "recent-1" not in result_order_ids
    assert "recent-2" not in result_order_ids


def test_data_loader_no_future_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify hand-computed ground-truth window counts mirror SQL RANGE semantics.

    The expected_1h and expected_24h values are hand-computed counts for
    RANGE BETWEEN ... PRECEDING AND CURRENT ROW EXCLUDE CURRENT ROW semantics.
    """
    assert tmp_path.exists()
    placed_at = [
        datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 13, 30, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 2, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 2, 13, 30, 0, tzinfo=timezone.utc),
    ]
    # 1h prior counts: t1 sees t0; t2 sees t1 exactly 60m prior; t3/t4 see none.
    expected_1h = [0, 1, 1, 0, 0]
    # 24h prior counts: t3 sees t0/t1/t2; t4 sees t2 at exactly 24h plus t3.
    expected_24h = [0, 1, 2, 3, 2]

    return_df = make_synthetic_df(n_rows=5, n_fraud=0, seed=42)
    return_df["order_id"] = ["t0", "t1", "t2", "t3", "t4"]
    return_df["user_id"] = ["user-1", "user-1", "user-1", "user-1", "user-1"]
    return_df["placed_at"] = placed_at

    engine = MagicMock()
    get_engine_mock = MagicMock(return_value=engine)
    monkeypatch.setattr("ml.training.data_loader.get_engine", get_engine_mock)

    def fake_read_sql_query(
        query: object,
        conn: object,
        params: Mapping[str, object],
    ) -> pd.DataFrame:
        del conn
        query_text = str(query)
        assert "COUNT(*) OVER" in query_text
        assert "PARTITION BY o.user_id" in query_text
        assert "RANGE BETWEEN INTERVAL '1 hour' PRECEDING AND CURRENT ROW" in query_text
        assert "RANGE BETWEEN INTERVAL '24 hours' PRECEDING AND CURRENT ROW" in query_text
        assert "EXCLUDE CURRENT ROW" in query_text
        assert params == {
            "start_date": placed_at[0] - timedelta(hours=1),
            "end_date": placed_at[-1] + timedelta(hours=1),
            "label_finalisation_buffer_days": 0,
        }

        base = return_df[["order_id", "user_id", "placed_at"]].copy()

        def count_prior_window(row: pd.Series, window_seconds: int) -> int:
            """Count rows in [t - window, t), matching EXCLUDE CURRENT ROW."""
            t = row["placed_at"]
            cutoff = t - pd.Timedelta(seconds=window_seconds)
            mask = (
                (base["user_id"] == row["user_id"])
                & (base["placed_at"] >= cutoff)
                & (base["placed_at"] < t)
            )
            return int(mask.sum())

        computed_1h = base.apply(lambda row: count_prior_window(row, 3600), axis=1)
        computed_24h = base.apply(lambda row: count_prior_window(row, 86400), axis=1)

        df_out = return_df.copy()
        df_out["user_orders_1h_at_order_time"] = computed_1h.values
        df_out["user_orders_24h_at_order_time"] = computed_24h.values
        return df_out

    def fake_to_parquet(self: pd.DataFrame, path: object, index: bool = False) -> None:
        del self, path, index

    monkeypatch.setattr("ml.training.data_loader.pd.read_sql_query", fake_read_sql_query)
    monkeypatch.setattr("ml.training.data_loader.pd.DataFrame.to_parquet", fake_to_parquet)

    config = TrainingDataConfig(
        start_date=placed_at[0] - timedelta(hours=1),
        end_date=placed_at[-1] + timedelta(hours=1),
        label_finalisation_buffer_days=0,
    )
    result = load_training_data(config)

    get_engine_mock.assert_called_once_with(role="training")
    assert len(result) == 5
    assert result["user_orders_1h_at_order_time"].tolist() == expected_1h
    assert result["user_orders_24h_at_order_time"].tolist() == expected_24h
    for column in ("order_id", "user_id", "placed_at", *_REQUIRED_COLUMNS):
        assert column in result.columns


def test_preprocessing_fn_handles_oov(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fit a real TFT vocab on known values; verify OOV maps to a different
    index than in-vocab values, proving compute_and_apply_vocabulary bucketing."""
    del monkeypatch

    import json as _json

    import apache_beam as beam
    import tensorflow_transform.beam as tft_beam
    from tensorflow_transform.tf_metadata import dataset_metadata, schema_utils

    feature_spec = _raw_tft_feature_spec(include_row_id=True)
    metadata = dataset_metadata.DatasetMetadata(schema_utils.schema_from_feature_spec(feature_spec))

    def _prod_preprocessing_fn_with_row_id(inputs: Dict[str, object]) -> Dict[str, object]:
        outputs: Dict[str, object] = dict(preprocessing_fn(inputs))
        outputs["row_id"] = tf.cast(inputs["row_id"], tf.int64)
        return outputs

    # row_id semantics: 0 = vocab data (ignored), 1 = test in-vocab, 2 = test OOV.
    vocab_data: List[Dict[str, object]] = [
        _raw_tft_row(card_brand=b"visa", row_id=0, include_row_id=True) for _ in range(10)
    ] + [_raw_tft_row(card_brand=b"mastercard", row_id=0, include_row_id=True) for _ in range(10)]
    test_data: List[Dict[str, object]] = [
        _raw_tft_row(card_brand=b"visa", row_id=1, include_row_id=True),
        _raw_tft_row(card_brand=b"discover", row_id=2, include_row_id=True),
    ]

    output_prefix = str(tmp_path / "output")

    with tft_beam.Context(temp_dir=str(tmp_path / "beam_temp")):  # noqa: SIM117
        with beam.Pipeline(runner="DirectRunner") as pipeline:
            raw_vocab_data = pipeline | "CreateVocabData" >> beam.Create(vocab_data)
            (transformed_vocab_dataset, transform_fn) = (
                raw_vocab_data,
                metadata,
            ) | "AnalyzeAndTransform" >> tft_beam.AnalyzeAndTransformDataset(
                _prod_preprocessing_fn_with_row_id
            )
            transformed_vocab_data, _transformed_metadata = transformed_vocab_dataset
            transformed_test_dataset = (
                (
                    (pipeline | "CreateTestData" >> beam.Create(test_data)),
                    metadata,
                ),
                transform_fn,
            ) | "TransformTestData" >> tft_beam.TransformDataset()
            transformed_test_data, _test_metadata = transformed_test_dataset
            transformed_data = (
                transformed_vocab_data,
                transformed_test_data,
            ) | "FlattenTransformedData" >> beam.Flatten()
            (
                transformed_data
                | "ToJson"
                >> beam.Map(
                    lambda r: _json.dumps(
                        {"card_brand": int(r["card_brand"]), "row_id": int(r["row_id"])}
                    )
                )
                | "WriteOutput" >> beam.io.WriteToText(output_prefix)
            )

    # Read back all output shards and parse.
    import glob as _glob

    rows: List[Dict[str, int]] = []
    for shard_path in sorted(_glob.glob(output_prefix + "-*")):
        with open(shard_path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(_json.loads(line))

    in_vocab_idx: int = next(r["card_brand"] for r in rows if r["row_id"] == 1)
    oov_idx: int = next(r["card_brand"] for r in rows if r["row_id"] == 2)

    assert in_vocab_idx != oov_idx, (
        f"in-vocab 'visa' ({in_vocab_idx}) and OOV 'discover' ({oov_idx}) "
        "must map to different indices to prove OOV bucketing works"
    )
    assert in_vocab_idx >= 0
    assert oov_idx >= 0
    assert oov_idx == 2


def test_xgboost_trains_and_predicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    train_xgboost_module = _train_xgboost_module()
    train_path = tmp_path / "train"
    val_path = tmp_path / "val"
    labels = ([0] * 50) + ([1] * 5)
    _write_tfrecord(
        train_path,
        labels,
        train_xgboost_module.FLOAT_FEATURE_NAMES,
        train_xgboost_module.INT_FEATURE_NAMES,
    )
    _write_tfrecord(
        val_path,
        labels,
        train_xgboost_module.FLOAT_FEATURE_NAMES,
        train_xgboost_module.INT_FEATURE_NAMES,
    )

    real_train = xgb.train

    def train_with_fewer_rounds(*args: object, **kwargs: object) -> xgb.Booster:
        kwargs["num_boost_round"] = 3
        kwargs["early_stopping_rounds"] = 2
        kwargs["verbose_eval"] = False
        return real_train(*args, **kwargs)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ml.training.train_xgboost.xgb.train", train_with_fewer_rounds)

    model, _importance = train_xgboost_module.train_xgboost(
        str(train_path),
        str(val_path),
        hyperparams={"max_depth": 2},
    )
    X_val, _y_val = train_xgboost_module.tfrecords_to_numpy(str(val_path))
    predictions = model.predict(
        xgb.DMatrix(X_val, feature_names=list(train_xgboost_module.FEATURE_NAMES)),
    )

    assert isinstance(model, xgb.Booster)
    assert predictions.shape == (len(labels),)
    assert np.all(predictions >= 0.0)
    assert np.all(predictions <= 1.0)


def test_dnn_trains_one_epoch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    train_dnn_module = _train_dnn_module()
    tf.keras.backend.clear_session()
    tf.random.set_seed(42)
    rng = np.random.default_rng(42)
    features = rng.random((100, train_dnn_module.NUM_FEATURES)).astype(np.float32)
    labels = np.array([0] * 90 + [1] * 10, dtype=np.int32)
    train_ds = tf.data.Dataset.from_tensor_slices((features, labels))
    val_ds = tf.data.Dataset.from_tensor_slices((features, labels))

    monkeypatch.chdir(tmp_path)
    model, _history = train_dnn_module.train_dnn(
        train_ds,
        val_ds,
        hyperparams={"epochs": 1},
    )
    predictions = np.asarray(model.predict(features[:5], verbose=0), dtype=np.float64)

    assert isinstance(model, tf.keras.Model)
    assert predictions.shape == (5, 1)
    assert np.all(predictions >= 0.0)
    assert np.all(predictions <= 1.0)


def test_evaluate_produces_all_metrics(tmp_path: Path) -> None:
    evaluate_module = _evaluate_module()
    synthetic_df = make_synthetic_df(n_rows=100, n_fraud=10)
    y_true = synthetic_df["is_fraud"].astype(np.int64).to_numpy()
    y_pred = np.linspace(0.05, 0.95, num=100, dtype=np.float64)
    categories = synthetic_df["fraud_category"].to_numpy()

    metrics = evaluate_module.evaluate(
        "models/test",
        (y_true, y_pred),
        categories,
        report_dir=str(tmp_path / "reports"),
    )

    for key in (
        "auprc",
        "auroc",
        "precision_at_95_recall",
        "recall_at_99_precision",
        "brier_score",
    ):
        assert key in metrics
        assert isinstance(metrics[key], float)

    for threshold in (0.3, 0.5, 0.7, 0.85):
        assert f"cm_at_{threshold}" in metrics


def test_ensemble_weights_search() -> None:
    evaluate_module = _evaluate_module()
    y_test = np.array([0, 0, 1, 1], dtype=np.int64)
    xgb_scores = np.array([0.2, 0.1, 0.8, 0.9], dtype=np.float64)
    dnn_scores = np.array([1.0, 0.9, 0.0, 0.1], dtype=np.float64)

    weights = evaluate_module.best_ensemble_weights(xgb_scores, dnn_scores, y_test)

    assert sum(weights) == pytest.approx(1.0)
    assert weights in (
        (0.3, 0.7),
        (0.4, 0.6),
        (0.5, 0.5),
        (0.6, 0.4),
        (0.7, 0.3),
    )
    assert weights == (0.7, 0.3)


def test_promote_blocks_regression(tmp_path: Path) -> None:
    promote_module = _promote_module()
    registry_root = tmp_path / "registry"
    reports_root = tmp_path / "reports"
    production_version = "v20260301_120000_aaaaaaaa"
    candidate_version = "v20260302_120000_bbbbbbbb"
    (registry_root / "xgboost" / production_version).mkdir(parents=True)
    (registry_root / "xgboost" / candidate_version).mkdir(parents=True)
    registry = _model_registry(registry_root)
    registry.promote("xgboost", production_version)
    _write_metrics(reports_root, production_version, auprc=0.85)
    _write_metrics(reports_root, candidate_version, auprc=0.80)

    with pytest.raises(promote_module.PromotionGateError, match="AUPRC"):
        promote_module.promote(
            version=candidate_version,
            model_type="xgboost",
            registry_root=registry_root,
            reports_root=reports_root,
        )

    assert registry.get_current("xgboost") == production_version


def test_savedmodel_serving_signature_works(tmp_path: Path) -> None:
    import apache_beam as beam
    import tensorflow_transform.beam as tft_beam
    from tensorflow_transform.tf_metadata import dataset_metadata, schema_utils

    tf.keras.backend.clear_session()
    tf.random.set_seed(42)
    feature_spec = _raw_tft_feature_spec()
    metadata = dataset_metadata.DatasetMetadata(schema_utils.schema_from_feature_spec(feature_spec))
    single_row = _raw_tft_row()

    with tft_beam.Context(temp_dir=str(tmp_path / "beam_temp2")):  # noqa: SIM117
        with beam.Pipeline(runner="DirectRunner") as pipeline:
            raw_data = pipeline | beam.Create([single_row])
            transform_fn = (
                raw_data,
                metadata,
            ) | tft_beam.AnalyzeDataset(preprocessing_fn)
            transform_fn | tft_beam.WriteTransformFn(str(tmp_path / "transform_output"))

    dnn_model = build_dnn_model(input_dim=NUM_FEATURES)
    saved_model_path = build_serving_model(
        str(tmp_path / "transform_output"),
        dnn_model,
        output_dir=str(tmp_path / "serving"),
        version="v_test",
    )

    loaded = tf.saved_model.load(saved_model_path)
    signature = loaded.signatures["serving_default"]

    input_batch: Dict[str, object] = {}
    for feature_name, spec in _PRODUCTION_FEATURE_SPEC.items():
        if spec.dtype == tf.float32:
            input_batch[feature_name] = tf.constant([1.0], dtype=tf.float32)
        elif spec.dtype == tf.int64:
            input_batch[feature_name] = tf.constant([1], dtype=tf.int64)
        elif spec.dtype == tf.bool:
            input_batch[feature_name] = tf.constant([False], dtype=tf.bool)
        else:
            input_batch[feature_name] = tf.constant([b"gb"], dtype=tf.string)
    # gt_is_fraud is a training-only label; build_serving_model strips it from the serving spec.
    # Do not include it in the input_batch passed to the serving signature.
    input_batch.pop("gt_is_fraud", None)
    result = cast(Mapping[str, object], signature(**input_batch))
    output = np.asarray(next(iter(result.values())), dtype=np.float64)

    assert output.shape == (1, 1)
    assert np.all(output >= 0.0)
    assert np.all(output <= 1.0)
