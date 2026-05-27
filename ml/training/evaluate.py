"""Evaluation utilities for Phase 5 fraud models."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(tempfile.gettempdir()) / "fraud_forecast_matplotlib"),
)
os.environ.setdefault(
    "XDG_CACHE_HOME",
    str(Path(tempfile.gettempdir()) / "fraud_forecast_cache"),
)

import matplotlib
import numpy as np
from sklearn.calibration import calibration_curve
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    precision_recall_curve,
    roc_auc_score,
    roc_curve,
)

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# The packet requires Python 3.8-compatible typing names here.
# ruff: noqa: UP006

FRAUD_CATEGORIES: Tuple[str, ...] = (
    "stolen_card",
    "account_takeover",
    "promo_abuse",
    "refund_abuse",
    "collusive_merchant",
    "triangulation",
    "reseller",
)
CONFUSION_MATRIX_THRESHOLDS: Tuple[float, ...] = (0.3, 0.5, 0.7, 0.85)


def _as_1d_array(values: np.ndarray, dtype: Any) -> np.ndarray:
    return np.asarray(values, dtype=dtype).reshape(-1)


def precision_at_recall(
    y_true: np.ndarray,
    y_scores: np.ndarray,
    target_recall: float,
) -> float:
    precision_values, recall_values, _thresholds = precision_recall_curve(
        _as_1d_array(y_true, np.int64),
        _as_1d_array(y_scores, np.float64),
    )
    valid_precisions = np.asarray(precision_values)[np.asarray(recall_values) >= target_recall]
    if valid_precisions.size == 0:
        return 0.0
    return float(np.max(valid_precisions))


def recall_at_precision(
    y_true: np.ndarray,
    y_scores: np.ndarray,
    target_precision: float,
) -> float:
    precision_values, recall_values, _thresholds = precision_recall_curve(
        _as_1d_array(y_true, np.int64),
        _as_1d_array(y_scores, np.float64),
    )
    valid_recalls = np.asarray(recall_values)[np.asarray(precision_values) >= target_precision]
    if valid_recalls.size == 0:
        return 0.0
    return float(np.max(valid_recalls))


def compute_metrics(y_true: np.ndarray, y_scores: np.ndarray) -> Dict[str, Any]:
    y_true_array = _as_1d_array(y_true, np.int64)
    y_score_array = _as_1d_array(y_scores, np.float64)
    metrics: Dict[str, Any] = {
        "auprc": float(average_precision_score(y_true_array, y_score_array)),
        "auroc": float(roc_auc_score(y_true_array, y_score_array)),
        "precision_at_95_recall": precision_at_recall(y_true_array, y_score_array, 0.95),
        "recall_at_99_precision": recall_at_precision(y_true_array, y_score_array, 0.99),
        "brier_score": float(brier_score_loss(y_true_array, y_score_array)),
    }

    for threshold in CONFUSION_MATRIX_THRESHOLDS:
        predictions = (y_score_array >= threshold).astype(np.int64)
        matrix = confusion_matrix(y_true_array, predictions, labels=[0, 1])
        matrix_rows: List[List[int]] = [[int(cell) for cell in row] for row in matrix.tolist()]
        metrics[f"cm_at_{threshold}"] = matrix_rows

    return metrics


def _ensure_parent_directory(save_to: str) -> None:
    Path(save_to).parent.mkdir(parents=True, exist_ok=True)


def plot_score_distributions(y_true: np.ndarray, y_scores: np.ndarray, save_to: str) -> None:
    y_true_array = _as_1d_array(y_true, np.int64)
    y_score_array = _as_1d_array(y_scores, np.float64)
    fig, ax = plt.subplots(figsize=(8, 5))
    try:
        bins = np.linspace(0.0, 1.0, 21)
        ax.hist(
            y_score_array[y_true_array == 0],
            bins=bins,
            alpha=0.7,
            label="Non-fraud",
        )
        ax.hist(
            y_score_array[y_true_array == 1],
            bins=bins,
            alpha=0.7,
            label="Fraud",
        )
        ax.set_xlabel("Predicted fraud score")
        ax.set_ylabel("Order count")
        ax.set_title("Fraud Score Distributions")
        ax.legend()
        fig.tight_layout()
        _ensure_parent_directory(save_to)
        fig.savefig(save_to)
    finally:
        plt.close(fig)


def plot_pr_curve(y_true: np.ndarray, y_scores: np.ndarray, save_to: str) -> None:
    precision_values, recall_values, _thresholds = precision_recall_curve(
        _as_1d_array(y_true, np.int64),
        _as_1d_array(y_scores, np.float64),
    )
    fig, ax = plt.subplots(figsize=(8, 5))
    try:
        ax.plot(recall_values, precision_values)
        ax.set_xlabel("Recall")
        ax.set_ylabel("Precision")
        ax.set_title("Precision-Recall Curve")
        ax.set_xlim(0.0, 1.0)
        ax.set_ylim(0.0, 1.05)
        fig.tight_layout()
        _ensure_parent_directory(save_to)
        fig.savefig(save_to)
    finally:
        plt.close(fig)


def plot_roc_curve(y_true: np.ndarray, y_scores: np.ndarray, save_to: str) -> None:
    false_positive_rate, true_positive_rate, _thresholds = roc_curve(
        _as_1d_array(y_true, np.int64),
        _as_1d_array(y_scores, np.float64),
    )
    fig, ax = plt.subplots(figsize=(8, 5))
    try:
        ax.plot(false_positive_rate, true_positive_rate)
        ax.plot([0.0, 1.0], [0.0, 1.0], linestyle="--", color="grey")
        ax.set_xlabel("False positive rate")
        ax.set_ylabel("True positive rate")
        ax.set_title("ROC Curve")
        ax.set_xlim(0.0, 1.0)
        ax.set_ylim(0.0, 1.05)
        fig.tight_layout()
        _ensure_parent_directory(save_to)
        fig.savefig(save_to)
    finally:
        plt.close(fig)


def plot_calibration(y_true: np.ndarray, y_scores: np.ndarray, save_to: str) -> None:
    fraction_of_positives, mean_predicted_value = calibration_curve(
        _as_1d_array(y_true, np.int64),
        _as_1d_array(y_scores, np.float64),
        n_bins=10,
    )
    fig, ax = plt.subplots(figsize=(8, 5))
    try:
        ax.plot(mean_predicted_value, fraction_of_positives, marker="o")
        ax.plot([0.0, 1.0], [0.0, 1.0], linestyle="--", color="grey")
        ax.set_xlabel("Mean predicted probability")
        ax.set_ylabel("Fraction of positives")
        ax.set_title("Calibration Curve")
        ax.set_xlim(0.0, 1.0)
        ax.set_ylim(0.0, 1.05)
        fig.tight_layout()
        _ensure_parent_directory(save_to)
        fig.savefig(save_to)
    finally:
        plt.close(fig)


def _json_compatible(value: Any) -> Any:
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return value


def _format_report_value(value: Any) -> str:
    native_value = _json_compatible(value)
    if isinstance(native_value, float):
        return f"{native_value:.6f}"
    if isinstance(native_value, (list, dict)):
        return json.dumps(native_value, sort_keys=True)
    return str(native_value)


def save_report(metrics: Dict[str, Any], report_dir: str) -> None:
    report_path = Path(report_dir)
    report_path.mkdir(parents=True, exist_ok=True)

    serializable_metrics = _json_compatible(metrics)
    (report_path / "metrics.json").write_text(
        json.dumps(serializable_metrics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = ["# Evaluation Report", "", "| Metric | Value |", "| --- | --- |"]
    for metric_name in sorted(metrics):
        lines.append(f"| {metric_name} | {_format_report_value(metrics[metric_name])} |")
    (report_path / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def evaluate(
    model_path: str,
    test_data: Tuple[np.ndarray, np.ndarray],
    ground_truth_categories: np.ndarray,
) -> Dict[str, Any]:
    y_test, y_pred = test_data
    y_test_array = _as_1d_array(y_test, np.int64)
    y_pred_array = _as_1d_array(y_pred, np.float64)
    categories = _as_1d_array(ground_truth_categories, str)
    metrics = compute_metrics(y_test_array, y_pred_array)

    for category in FRAUD_CATEGORIES:
        category_mask = categories == category
        if int(category_mask.sum()) == 0:
            metrics[f"recall_{category}"] = 0.0
        else:
            metrics[f"recall_{category}"] = float((y_pred_array[category_mask] >= 0.5).mean())

    version = Path(model_path).name
    report_dir = str(Path("ml") / "training" / "reports" / version)
    plot_score_distributions(
        y_test_array, y_pred_array, save_to=str(Path(report_dir) / "score_dist.png")
    )
    plot_pr_curve(y_test_array, y_pred_array, save_to=str(Path(report_dir) / "pr_curve.png"))
    plot_roc_curve(y_test_array, y_pred_array, save_to=str(Path(report_dir) / "roc_curve.png"))
    plot_calibration(y_test_array, y_pred_array, save_to=str(Path(report_dir) / "calibration.png"))
    save_report(metrics, report_dir)

    return metrics


def evaluate_ensemble(
    xgb_scores: np.ndarray,
    dnn_scores: np.ndarray,
    y_test: np.ndarray,
    weights: Tuple[float, float] = (0.6, 0.4),
) -> Dict[str, Any]:
    ensemble_scores = weights[0] * xgb_scores + weights[1] * dnn_scores
    return compute_metrics(y_test, ensemble_scores)


def best_ensemble_weights(
    xgb_scores: np.ndarray,
    dnn_scores: np.ndarray,
    y_test: np.ndarray,
) -> Tuple[float, float]:
    weight_grid: Tuple[Tuple[float, float], ...] = (
        (0.3, 0.7),
        (0.4, 0.6),
        (0.5, 0.5),
        (0.6, 0.4),
        (0.7, 0.3),
    )
    best_weights = weight_grid[0]
    best_auprc = float("-inf")
    for weights in weight_grid:
        metrics = evaluate_ensemble(xgb_scores, dnn_scores, y_test, weights=weights)
        auprc = float(metrics["auprc"])
        if auprc > best_auprc:
            best_auprc = auprc
            best_weights = weights
    return best_weights


def main() -> None:
    """CLI entry point for evaluating saved fraud model predictions."""
    parser = argparse.ArgumentParser(description="Evaluate fraud model predictions.")
    parser.add_argument("--version", required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--test-data-path", required=True)
    parser.add_argument("--reports-dir", default="ml/training/reports")
    args = parser.parse_args()

    with np.load(str(args.test_data_path), allow_pickle=False) as test_data_file:
        y_true = np.asarray(test_data_file["y_true"])
        y_pred = np.asarray(test_data_file["y_pred"])
        if "categories" in test_data_file.files:
            categories = np.asarray(test_data_file["categories"])
        else:
            categories = np.full(_as_1d_array(y_true, np.int64).shape, "", dtype=str)

    metrics = evaluate(str(args.model_path), (y_true, y_pred), categories)
    save_report(metrics, str(Path(str(args.reports_dir)) / str(args.version)))
    print(json.dumps(_json_compatible(metrics), sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = [
    "evaluate",
    "evaluate_ensemble",
    "best_ensemble_weights",
    "compute_metrics",
    "precision_at_recall",
    "recall_at_precision",
    "save_report",
    "plot_score_distributions",
    "plot_pr_curve",
    "plot_roc_curve",
    "plot_calibration",
    "main",
]
