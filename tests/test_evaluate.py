from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import pytest

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


def _labels_and_scores() -> Tuple[np.ndarray, np.ndarray]:
    y_true = np.array([0, 0, 1, 1, 0, 1], dtype=np.int64)
    y_scores = np.array([0.1, 0.2, 0.8, 0.9, 0.3, 0.7], dtype=np.float64)
    return y_true, y_scores


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
    metrics = evaluate("models/xgboost/test-version", (y_true, y_scores), categories)

    # stolen_card: one score >= 0.5 out of two category examples = 1 / 2.
    assert metrics["recall_stolen_card"] == pytest.approx(0.5)
    # account_takeover: one score >= 0.5 out of one category example = 1 / 1.
    assert metrics["recall_account_takeover"] == pytest.approx(1.0)
    # promo_abuse: zero scores >= 0.5 out of one category example = 0 / 1.
    assert metrics["recall_promo_abuse"] == pytest.approx(0.0)
    assert metrics["recall_refund_abuse"] == pytest.approx(1.0)
    assert metrics["recall_collusive_merchant"] == pytest.approx(0.0)
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
    assert printed_metrics["recall_stolen_card"] == pytest.approx(0.0)
    assert printed_metrics["recall_account_takeover"] == pytest.approx(0.0)
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
