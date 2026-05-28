from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import pytest
import tensorflow as tf  # type: ignore[import-untyped]
import xgboost as xgb

# The packet requires Python 3.8-compatible typing names here.
# ruff: noqa: UP006

from ml.training.evaluate import (
    best_ensemble_weights,
    compute_metrics,
    evaluate,
    evaluate_ensemble,
    main as evaluate_main,
    plot_calibration,
    plot_pr_curve,
    plot_roc_curve,
    plot_score_distributions,
    precision_at_recall,
    recall_at_precision,
    save_report,
)
from ml.training.train_xgboost import FEATURE_NAMES, LABEL_NAME, _FEATURE_SPEC, tfrecords_to_numpy


def _labels_and_scores() -> Tuple[np.ndarray, np.ndarray]:
    y_true = np.array([0, 0, 1, 1, 0, 1], dtype=np.int64)
    y_scores = np.array([0.1, 0.2, 0.8, 0.9, 0.3, 0.7], dtype=np.float64)
    return y_true, y_scores


def _serialized_tfrecord_example(label: int, row_index: int) -> bytes:
    features: Dict[str, Any] = {}
    base_value = 5.0 if label == 1 else 0.0
    for feature_name in FEATURE_NAMES:
        spec = _FEATURE_SPEC[feature_name]
        if spec.dtype == tf.float32:
            features[feature_name] = tf.train.Feature(
                float_list=tf.train.FloatList(value=[base_value + (0.01 * float(row_index))]),
            )
        elif spec.dtype == tf.int64:
            features[feature_name] = tf.train.Feature(
                int64_list=tf.train.Int64List(value=[int(base_value) + row_index + 1]),
            )
        else:
            raise AssertionError(f"Unsupported TFRecord dtype for {feature_name}: {spec.dtype}")

    features[LABEL_NAME] = tf.train.Feature(int64_list=tf.train.Int64List(value=[label]))
    example = tf.train.Example(features=tf.train.Features(feature=features))
    return bytes(example.SerializeToString())


def _write_gzip_tfrecord(directory: Path) -> Path:
    labels = (1, 0, 1, 0)
    directory.mkdir(parents=True, exist_ok=True)
    tfrecord_path = directory / "part-0.tfrecord.gz"
    writer = tf.io.TFRecordWriter(str(tfrecord_path), options="GZIP")
    try:
        for row_index, label in enumerate(labels):
            writer.write(_serialized_tfrecord_example(label=label, row_index=row_index))
    finally:
        writer.close()
    return tfrecord_path


def _write_xgboost_model(tfrecord_dir: Path, model_path: Path) -> None:
    x_test, y_test = tfrecords_to_numpy(str(tfrecord_dir))
    model_path.parent.mkdir(parents=True, exist_ok=True)
    dtrain = xgb.DMatrix(x_test, label=y_test)
    model = xgb.train(
        {
            "objective": "binary:logistic",
            "eval_metric": "aucpr",
            "max_depth": 1,
            "eta": 1.0,
            "min_child_weight": 0.0,
            "seed": 7,
            "nthread": 1,
            "verbosity": 0,
        },
        dtrain,
        num_boost_round=1,
        verbose_eval=False,
    )
    model.save_model(str(model_path))


def test_compute_metrics_produces_all_headline_metrics_and_confusion_matrices() -> None:
    y_true, y_scores = _labels_and_scores()

    metrics = compute_metrics(y_true, y_scores)

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
        matrix = metrics[f"cm_at_{threshold}"]
        assert len(matrix) == 2
        assert all(len(row) == 2 for row in matrix)
        assert all(isinstance(cell, int) for row in matrix for cell in row)


def test_precision_at_recall_and_recall_at_precision_known_values() -> None:
    y_true = np.array([0, 0, 1, 1], dtype=np.int64)
    y_scores = np.array([0.1, 0.4, 0.35, 0.8], dtype=np.float64)

    # At recall 1.0, thresholds retain both positives and one negative: 2 / 3 precision.
    assert precision_at_recall(y_true, y_scores, 1.0) == pytest.approx(2.0 / 3.0)
    # Precision >= 0.75 is only achieved after one positive is below threshold: 1 / 2 recall.
    assert recall_at_precision(y_true, y_scores, 0.75) == pytest.approx(0.5)


def test_evaluate_computes_per_category_recall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    y_true = np.array([1, 1, 1, 1, 0, 1], dtype=np.int64)
    y_scores = np.array([0.8, 0.2, 0.9, 0.4, 0.1, 0.7], dtype=np.float64)
    categories = np.array(
        [
            "stolen_card",
            "stolen_card",
            "account_takeover",
            "promo_abuse",
            "legit",
            "refund_abuse",
        ],
    )

    monkeypatch.chdir(tmp_path)
    (tmp_path / "models" / "xgboost" / "test-version").mkdir(parents=True, exist_ok=True)
    metrics = evaluate("models/xgboost/test-version", (y_true, y_scores), categories)

    # stolen_card: one score >= 0.5 out of two category examples = 1 / 2.
    assert metrics["recall_stolen_card"] == pytest.approx(0.5)
    # account_takeover: one score >= 0.5 out of one category example = 1 / 1.
    assert metrics["recall_account_takeover"] == pytest.approx(1.0)
    # promo_abuse: zero scores >= 0.5 out of one category example = 0 / 1.
    assert metrics["recall_promo_abuse"] == pytest.approx(0.0)
    assert metrics["recall_refund_abuse"] == pytest.approx(1.0)
    assert "recall_collusive_merchant" not in metrics
    assert "recall_triangulation" not in metrics
    assert "recall_reseller" not in metrics
    assert (tmp_path / "ml" / "training" / "reports" / "test-version" / "metrics.json").exists()


def test_evaluate_ensemble_returns_metrics_dict() -> None:
    y_test = np.array([0, 0, 1, 1], dtype=np.int64)
    xgb_scores = np.array([0.1, 0.2, 0.8, 0.9], dtype=np.float64)
    dnn_scores = np.array([0.2, 0.1, 0.7, 0.8], dtype=np.float64)

    metrics = evaluate_ensemble(xgb_scores, dnn_scores, y_test)

    assert "auprc" in metrics
    assert isinstance(metrics["auprc"], float)


def test_best_ensemble_weights_returns_best_grid_weight() -> None:
    y_test = np.array([0, 0, 1, 1], dtype=np.int64)
    xgb_scores = np.array([0.2, 0.1, 0.8, 0.9], dtype=np.float64)
    dnn_scores = np.array([1.0, 0.9, 0.0, 0.1], dtype=np.float64)

    weights = best_ensemble_weights(xgb_scores, dnn_scores, y_test)

    assert weights == (0.7, 0.3)


def test_save_report_writes_json_and_markdown(tmp_path: Path) -> None:
    metrics: Dict[str, Any] = {
        "auprc": np.float64(0.75),
        "count": np.int64(4),
        "cm_at_0.5": np.array([[2, 0], [0, 2]], dtype=np.int64),
    }

    save_report(metrics, str(tmp_path))

    metrics_path = tmp_path / "metrics.json"
    report_path = tmp_path / "report.md"
    assert metrics_path.exists()
    assert report_path.exists()
    saved_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert saved_metrics["auprc"] == pytest.approx(0.75)
    assert saved_metrics["count"] == 4
    assert saved_metrics["cm_at_0.5"] == [[2, 0], [0, 2]]
    assert "| auprc | 0.750000 |" in report_path.read_text(encoding="utf-8")


def test_main_loads_npz_saves_versioned_report_and_prints_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    y_true = np.array([0, 1, 1, 0], dtype=np.int64)
    y_pred = np.array([0.1, 0.9, 0.4, 0.2], dtype=np.float64)
    categories = np.array(["legit", "stolen_card", "promo_abuse", "legit"])
    test_data_path = tmp_path / "test-data.npz"
    reports_dir = tmp_path / "reports"
    np.savez(test_data_path, y_true=y_true, y_pred=y_pred, categories=categories)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate",
            "--version",
            "cli-version",
            "--model-path",
            "models/xgboost/artifact-version",
            "--test-data-path",
            str(test_data_path),
            "--reports-dir",
            str(reports_dir),
        ],
    )

    evaluate_main()

    printed_metrics = json.loads(capsys.readouterr().out)
    saved_metrics = json.loads(
        (reports_dir / "cli-version" / "metrics.json").read_text(encoding="utf-8")
    )
    assert printed_metrics["auprc"] == pytest.approx(1.0)
    assert printed_metrics["recall_stolen_card"] == pytest.approx(1.0)
    assert printed_metrics["recall_promo_abuse"] == pytest.approx(0.0)
    assert saved_metrics == printed_metrics
    assert (reports_dir / "cli-version" / "score_dist.png").exists()
    assert (reports_dir / "cli-version" / "pr_curve.png").exists()
    assert (reports_dir / "cli-version" / "roc_curve.png").exists()
    assert (reports_dir / "cli-version" / "calibration.png").exists()


def test_main_loads_tfrecord_directory_and_runs_evaluate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tfrecord_dir = tmp_path / "test_tfrecord"
    _write_gzip_tfrecord(tfrecord_dir)
    model_path = tmp_path / "models" / "xgboost" / "test-version" / "model.bst"
    _write_xgboost_model(tfrecord_dir, model_path)
    reports_dir = tmp_path / "reports"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate",
            "--version",
            "tfrecord-version",
            "--model-path",
            str(model_path),
            "--test-data-path",
            str(tfrecord_dir),
            "--reports-dir",
            str(reports_dir),
        ],
    )

    evaluate_main()

    printed_metrics = json.loads(capsys.readouterr().out)
    assert "auprc" in printed_metrics
    assert (reports_dir / "tfrecord-version" / "metrics.json").exists()


def test_main_tfrecord_branch_uses_model_predictions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tfrecord_dir = tmp_path / "test_tfrecord"
    _write_gzip_tfrecord(tfrecord_dir)
    model_path = tmp_path / "models" / "xgboost" / "test-version" / "model.bst"
    _write_xgboost_model(tfrecord_dir, model_path)
    reports_dir = tmp_path / "reports"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate",
            "--version",
            "tfrecord-predictions",
            "--model-path",
            str(model_path),
            "--test-data-path",
            str(tfrecord_dir),
            "--reports-dir",
            str(reports_dir),
        ],
    )

    evaluate_main()

    printed_metrics = json.loads(capsys.readouterr().out)
    assert 0.0 <= printed_metrics["auprc"] <= 1.0


def test_main_uses_empty_categories_when_npz_omits_categories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    y_true = np.array([0, 1, 1, 0], dtype=np.int64)
    y_pred = np.array([0.1, 0.9, 0.4, 0.2], dtype=np.float64)
    test_data_path = tmp_path / "test-data.npz"
    np.savez(test_data_path, y_true=y_true, y_pred=y_pred)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate",
            "--version",
            "missing-categories",
            "--model-path",
            "models/dnn/artifact-version",
            "--test-data-path",
            str(test_data_path),
        ],
    )

    evaluate_main()

    printed_metrics = json.loads(capsys.readouterr().out)
    assert "recall_stolen_card" not in printed_metrics
    assert "recall_account_takeover" not in printed_metrics
    assert (
        tmp_path / "ml" / "training" / "reports" / "missing-categories" / "metrics.json"
    ).exists()


def test_plotting_functions_write_png_files(tmp_path: Path) -> None:
    y_true, y_scores = _labels_and_scores()
    destinations = {
        "score_dist": tmp_path / "score_dist.png",
        "pr_curve": tmp_path / "pr_curve.png",
        "roc_curve": tmp_path / "roc_curve.png",
        "calibration": tmp_path / "calibration.png",
    }

    plot_score_distributions(y_true, y_scores, str(destinations["score_dist"]))
    plot_pr_curve(y_true, y_scores, str(destinations["pr_curve"]))
    plot_roc_curve(y_true, y_scores, str(destinations["roc_curve"]))
    plot_calibration(y_true, y_scores, str(destinations["calibration"]))

    for destination in destinations.values():
        assert destination.exists()


def test_evaluate_ensemble_handles_column_vector_dnn_scores() -> None:
    rng = np.random.default_rng(42)
    y_test = np.where(rng.integers(0, 10, size=100) == 0, 1, 0).astype(np.int64)
    xgb_scores = rng.random(100, dtype=np.float64)
    dnn_scores = rng.random((100, 1), dtype=np.float64)

    metrics = evaluate_ensemble(xgb_scores, dnn_scores, y_test)

    assert "auprc" in metrics
    assert isinstance(metrics["auprc"], float)
    assert 0.0 <= metrics["auprc"] <= 1.0


def test_evaluate_ensemble_raises_on_mismatched_lengths() -> None:
    rng = np.random.default_rng(42)
    y_test = np.where(rng.integers(0, 10, size=10) == 0, 1, 0).astype(np.int64)
    xgb_scores = rng.random(10, dtype=np.float64)
    dnn_scores = rng.random(8, dtype=np.float64)

    with pytest.raises(ValueError, match="length"):
        evaluate_ensemble(xgb_scores, dnn_scores, y_test)
