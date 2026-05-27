from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import tensorflow as tf

from ml.training.train_dnn import (
    FEATURE_ORDER,
    FLOAT_FEATURE_NAMES,
    INT_FEATURE_NAMES,
    LABEL_NAME,
    NUM_FEATURES,
    _passthrough_feature_spec,
    _serving_feature_spec,
    build_dnn_model,
    compute_class_weight,
    make_dataset,
    train_dnn,
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


def test_build_dnn_model_output_shape() -> None:
    model = build_dnn_model(input_dim=42)

    assert model.output_shape == (None, 1)


def test_serving_feature_spec_excludes_training_labels() -> None:
    passthrough_spec = _passthrough_feature_spec()
    raw_spec = dict(passthrough_spec)
    raw_spec[LABEL_NAME] = tf.io.FixedLenFeature([], tf.int64)
    raw_spec["gt_is_fraud"] = tf.io.FixedLenFeature([], tf.int64)
    raw_spec["gt_review_reason"] = tf.io.FixedLenFeature([], tf.int64)
    raw_spec["label_source"] = tf.io.FixedLenFeature([], tf.int64)

    serving_spec = _serving_feature_spec(raw_spec)

    assert LABEL_NAME not in passthrough_spec
    assert LABEL_NAME not in serving_spec
    assert "gt_is_fraud" not in serving_spec
    assert "gt_review_reason" not in serving_spec
    assert "label_source" not in serving_spec
    assert set(serving_spec) == set(FEATURE_ORDER)


def test_compute_class_weight_uses_training_label_ratio() -> None:
    num_examples = 100
    features = np.zeros((num_examples, NUM_FEATURES), dtype=np.float32)
    labels = np.array([0] * 90 + [1] * 10, dtype=np.int32)
    train_ds = tf.data.Dataset.from_tensor_slices((features, labels))

    class_weight = compute_class_weight(train_ds)

    # 90 negative examples / 10 positive examples = 9.0 positive-class weight.
    assert class_weight == {0: 1.0, 1: 9.0}


def test_train_dnn_one_epoch_smoke(tmp_path: Path) -> None:
    tf.keras.backend.clear_session()
    tf.random.set_seed(1234)
    num_examples = 120
    features = np.arange(num_examples * NUM_FEATURES, dtype=np.float32).reshape(
        num_examples,
        NUM_FEATURES,
    )
    features = features / np.float32(num_examples * NUM_FEATURES)
    labels = (np.arange(num_examples) % 10 == 0).astype(np.int32)
    train_ds = tf.data.Dataset.from_tensor_slices((features, labels))
    val_ds = tf.data.Dataset.from_tensor_slices((features, labels))

    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        model, _history = train_dnn(train_ds, val_ds, hyperparams={"epochs": 1})
    finally:
        os.chdir(previous_cwd)

    predictions = model.predict(features[:10], verbose=0)

    assert predictions.shape == (10, 1)
    assert np.all(predictions >= 0.0)
    assert np.all(predictions <= 1.0)


def test_make_dataset_synthetic(tmp_path: Path) -> None:
    labels = [0, 1, 0]
    _write_tfrecord(tmp_path, labels)

    dataset = make_dataset(str(tmp_path))
    feature_vector, label = next(iter(dataset))

    assert len(FEATURE_ORDER) == NUM_FEATURES
    assert tuple(feature_vector.shape.as_list()) == (NUM_FEATURES,)
    assert feature_vector.dtype == tf.float32
    assert label.dtype == tf.int32
    assert int(label.numpy()) == labels[0]
