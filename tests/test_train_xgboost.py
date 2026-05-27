from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import tensorflow as tf  # type: ignore[import-untyped]
import xgboost as xgb

from ml.training.train_xgboost import (
    FEATURE_NAMES,
    FLOAT_FEATURE_NAMES,
    INT_FEATURE_NAMES,
    tfrecords_to_numpy,
    train_xgboost,
)


def _serialized_example(label: int, row_index: int) -> bytes:
    features = {
        feature_name: tf.train.Feature(
            float_list=tf.train.FloatList(value=[float(row_index) + 0.5]),
        )
        for feature_name in FLOAT_FEATURE_NAMES
    }
    features.update(
        {
            feature_name: tf.train.Feature(
                int64_list=tf.train.Int64List(value=[row_index + 1]),
            )
            for feature_name in INT_FEATURE_NAMES
        },
    )
    features["label"] = tf.train.Feature(int64_list=tf.train.Int64List(value=[label]))
    example = tf.train.Example(features=tf.train.Features(feature=features))
    return example.SerializeToString()


def _write_tfrecord(directory: Path, labels: Sequence[int]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tfrecord_path = directory / "train-00000-of-00001"
    writer = tf.io.TFRecordWriter(str(tfrecord_path))
    try:
        for row_index, label in enumerate(labels):
            writer.write(_serialized_example(label=label, row_index=row_index))
    finally:
        writer.close()
    return tfrecord_path


def test_tfrecords_to_numpy_empty_raises() -> None:
    with pytest.raises(ValueError, match="No TFRecord files found"):
        tfrecords_to_numpy("/nonexistent/path")


def test_tfrecords_to_numpy_synthetic(tmp_path: Path) -> None:
    labels = [0, 1, 0, 1, 0]
    _write_tfrecord(tmp_path, labels)

    X, y = tfrecords_to_numpy(str(tmp_path))

    assert len(FEATURE_NAMES) == 48
    assert X.shape == (len(labels), 48)
    assert y.shape == (len(labels),)
    assert X.dtype == np.float32
    assert y.dtype == np.int64
    assert y.tolist() == labels


def test_train_xgboost_end_to_end(tmp_path: Path) -> None:
    train_path = tmp_path / "train"
    val_path = tmp_path / "val"
    labels = ([0] * 100) + ([1] * 5)
    _write_tfrecord(train_path, labels)
    _write_tfrecord(val_path, labels)

    real_train = xgb.train

    def train_with_fewer_rounds(*args: object, **kwargs: object) -> xgb.Booster:
        kwargs["num_boost_round"] = 5
        kwargs["early_stopping_rounds"] = 2
        kwargs["verbose_eval"] = False
        return real_train(*args, **kwargs)

    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        with patch("ml.training.train_xgboost.xgb.train", side_effect=train_with_fewer_rounds):
            model, importance = train_xgboost(
                str(train_path),
                str(val_path),
                hyperparams={"max_depth": 2},
            )
    finally:
        os.chdir(previous_cwd)

    assert isinstance(model, xgb.Booster)
    assert isinstance(importance, dict)
    assert all(isinstance(key, str) for key in importance)
    assert all(isinstance(value, float) for value in importance.values())
    assert set(importance).issubset(FEATURE_NAMES)
    assert model.feature_names == list(FEATURE_NAMES)
    assert list((tmp_path / "models" / "xgboost").glob("v*/model.bin"))


def test_train_xgboost_feature_names() -> None:
    assert len(FEATURE_NAMES) == 48
    assert all(isinstance(feature_name, str) for feature_name in FEATURE_NAMES)
    assert len(set(FEATURE_NAMES)) == len(FEATURE_NAMES)
