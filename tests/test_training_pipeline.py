from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Mapping, Protocol, Sequence, Tuple, Type, cast
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
import tensorflow as tf
import xgboost as xgb

from ml.training.data_loader import _REQUIRED_COLUMNS, TrainingDataConfig, load_training_data
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


def test_data_loader_excludes_unfinalised(tmp_path: Path) -> None:
    assert tmp_path.exists()
    now_utc = datetime.now(timezone.utc)
    df = pd.DataFrame(
        {
            "order_id": [1, 2, 3, 4, 5],
            "placed_at": [
                now_utc - timedelta(days=60, minutes=2),
                now_utc - timedelta(days=60, minutes=1),
                now_utc - timedelta(days=60),
                now_utc - timedelta(days=20, minutes=1),
                now_utc - timedelta(days=20),
            ],
        },
    )

    cutoff = now_utc - timedelta(days=45)
    filtered = df[df["placed_at"] < cutoff]

    assert filtered["order_id"].tolist() == [1, 2, 3]
    assert filtered["placed_at"].max() < cutoff


def test_data_loader_no_future_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the loader routes through real SQL window logic via patched pd.read_sql_query."""
    assert tmp_path.exists()
    t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    fixture_df = make_synthetic_df(n_rows=3, n_fraud=0, seed=42)
    fixture_df["order_id"] = ["t1", "t2", "t3"]
    fixture_df["user_id"] = ["user-1", "user-1", "user-1"]
    fixture_df["placed_at"] = [t1, t1 + timedelta(hours=2), t1 + timedelta(hours=4)]

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
            "start_date": t1 - timedelta(hours=1),
            "end_date": t1 + timedelta(hours=5),
            "label_finalisation_buffer_days": 0,
        }
        return fixture_df

    def fake_to_parquet(self: pd.DataFrame, path: object, index: bool = False) -> None:
        del self, path, index

    monkeypatch.setattr("ml.training.data_loader.pd.read_sql_query", fake_read_sql_query)
    monkeypatch.setattr("ml.training.data_loader.pd.DataFrame.to_parquet", fake_to_parquet)

    config = TrainingDataConfig(
        start_date=t1 - timedelta(hours=1),
        end_date=t1 + timedelta(hours=5),
        label_finalisation_buffer_days=0,
    )
    result = load_training_data(config)

    get_engine_mock.assert_called_once_with(role="training")
    assert len(result) == 3
    for column in ("order_id", "user_id", "placed_at", *_REQUIRED_COLUMNS):
        assert column in result.columns


def test_preprocessing_fn_handles_oov(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tft = _FakeTft()
    fake_tft_module = ModuleType("tensorflow_transform")
    fake_tft_module.__dict__["scale_to_z_score"] = fake_tft.scale_to_z_score
    fake_tft_module.__dict__["compute_and_apply_vocabulary"] = fake_tft.compute_and_apply_vocabulary
    fake_tft_module.__dict__["hash_strings"] = fake_tft.hash_strings
    monkeypatch.setitem(sys.modules, "tensorflow_transform", fake_tft_module)
    preprocessing_module = _preprocessing_module()
    monkeypatch.setattr(preprocessing_module, "tft", fake_tft)

    outputs = preprocessing_module.preprocessing_fn(_raw_preprocessing_inputs())
    card_brand = np.asarray(outputs["card_brand"])

    assert card_brand.tolist() == [0]
    assert ("vocab_card_brand", 1) in fake_tft.vocabulary_calls


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
    train_dnn_module = _train_dnn_module()
    tf.keras.backend.clear_session()
    tf.random.set_seed(42)
    model = train_dnn_module.build_dnn_model(input_dim=train_dnn_module.NUM_FEATURES)
    wrapper = tf.Module()
    wrapper.model = model

    def serve(**features: object) -> Dict[str, object]:
        feature_columns = [
            tf.reshape(tf.cast(features[feature_name], tf.float32), [-1, 1])
            for feature_name in train_dnn_module.FEATURE_ORDER
        ]
        feature_matrix = tf.concat(feature_columns, axis=1)
        wrapper_model = cast(tf.keras.Model, wrapper.model)
        return {"scores": wrapper_model(feature_matrix, training=False)}

    input_spec: Dict[str, object] = {}
    input_spec.update(
        {
            feature_name: tf.TensorSpec(shape=[None], dtype=tf.float32, name=feature_name)
            for feature_name in train_dnn_module.FLOAT_FEATURE_NAMES
        },
    )
    input_spec.update(
        {
            feature_name: tf.TensorSpec(shape=[None], dtype=tf.int64, name=feature_name)
            for feature_name in train_dnn_module.INT_FEATURE_NAMES
        },
    )
    concrete_fn = tf.function(serve).get_concrete_function(**input_spec)
    saved_model_path = tmp_path / "saved_model"
    tf.saved_model.save(
        wrapper,
        str(saved_model_path),
        signatures={"serving_default": concrete_fn},
    )

    loaded = tf.saved_model.load(str(saved_model_path))
    input_batch: Dict[str, object] = {}
    input_batch.update(
        {
            feature_name: tf.constant([0.0] * 5, dtype=tf.float32)
            for feature_name in train_dnn_module.FLOAT_FEATURE_NAMES
        },
    )
    input_batch.update(
        {
            feature_name: tf.constant([0] * 5, dtype=tf.int64)
            for feature_name in train_dnn_module.INT_FEATURE_NAMES
        },
    )
    signature = loaded.signatures["serving_default"]
    result = cast(Mapping[str, object], signature(**input_batch))
    output = np.asarray(next(iter(result.values())), dtype=np.float64)

    assert output.shape == (5, 1)
    assert np.all(output >= 0.0)
    assert np.all(output <= 1.0)
