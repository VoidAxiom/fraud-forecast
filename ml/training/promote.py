"""Promotion gate for evaluated Phase 5 fraud models."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from ml.registry import ModelRegistry

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo

# The packet requires Python 3.8-compatible typing names here.
# ruff: noqa: UP006, UP007, UP045

LONDON_TZ = ZoneInfo("Europe/London")
HEADLINE_METRICS: Tuple[str, ...] = ("auprc", "auroc", "brier_score")
AUPRC_TOLERANCE = 0.01
RECALL_DROP_FACTOR = 0.9

_LOGGER = logging.getLogger(__name__)


class PromotionGateError(Exception):
    """Raised when a candidate model does not satisfy the promotion gate."""


def _load_metrics(metrics_path: Path) -> Dict[str, Any]:
    with metrics_path.open("r", encoding="utf-8") as metrics_file:
        metrics = json.load(metrics_file)

    if not isinstance(metrics, dict):
        raise ValueError(f"Metrics file must contain a JSON object: {metrics_path}")
    return {str(key): value for key, value in metrics.items()}


def _load_version_metrics(reports_root: Path, version: str) -> Dict[str, Any]:
    return _load_metrics(reports_root / version / "metrics.json")


def _optional_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _metric_as_float(metrics: Dict[str, Any], metric_name: str, label: str) -> float:
    value = _optional_float(metrics.get(metric_name))
    if value is None:
        raise ValueError(f"{label} metrics missing numeric {metric_name!r}")
    return value


def _headline_delta(
    candidate_metrics: Dict[str, Any],
    production_metrics: Optional[Dict[str, Any]],
) -> Dict[str, float]:
    if production_metrics is None:
        return {}

    metrics_delta: Dict[str, float] = {}
    for metric_name in HEADLINE_METRICS:
        candidate_value = _optional_float(candidate_metrics.get(metric_name))
        production_value = _optional_float(production_metrics.get(metric_name))
        if candidate_value is not None and production_value is not None:
            metrics_delta[metric_name] = candidate_value - production_value
    return metrics_delta


def _recall_gate_failures(
    candidate_metrics: Dict[str, Any],
    production_metrics: Dict[str, Any],
) -> List[str]:
    failures: List[str] = []
    common_metric_names = sorted(set(candidate_metrics).intersection(production_metrics))
    for metric_name in common_metric_names:
        if not metric_name.startswith("recall_"):
            continue

        candidate_recall = _metric_as_float(candidate_metrics, metric_name, "candidate")
        production_recall = _metric_as_float(production_metrics, metric_name, "production")
        minimum_recall = production_recall * RECALL_DROP_FACTOR
        if candidate_recall < minimum_recall:
            failures.append(
                f"{metric_name} "
                f"(candidate {candidate_recall:.6f} < minimum {minimum_recall:.6f}; "
                f"production {production_recall:.6f})"
            )
    return failures


def _enforce_gate(
    candidate_metrics: Dict[str, Any],
    production_metrics: Dict[str, Any],
) -> None:
    candidate_auprc = _metric_as_float(candidate_metrics, "auprc", "candidate")
    production_auprc = _metric_as_float(production_metrics, "auprc", "production")
    minimum_auprc = production_auprc - AUPRC_TOLERANCE
    if candidate_auprc < minimum_auprc:
        raise PromotionGateError(
            f"Candidate AUPRC {candidate_auprc:.6f} is below production AUPRC "
            f"{production_auprc:.6f} minus tolerance {AUPRC_TOLERANCE:.6f} "
            f"(minimum {minimum_auprc:.6f})"
        )

    recall_failures = _recall_gate_failures(candidate_metrics, production_metrics)
    if recall_failures:
        raise PromotionGateError(
            "Per-category recall gate failed; drops greater than 10% for: "
            + ", ".join(recall_failures)
        )


def _append_promotion_log(
    reports_root: Path,
    result: Dict[str, Any],
) -> None:
    reports_root.mkdir(parents=True, exist_ok=True)
    event = dict(result)
    event["timestamp"] = datetime.now(tz=LONDON_TZ).isoformat()

    with (reports_root / "promotion_log.jsonl").open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(event, sort_keys=True) + "\n")


def promote(
    version: str,
    model_type: str,
    force: bool = False,
    registry_root: Union[Path, str] = Path("/var/lib/models"),
    reports_root: Union[Path, str] = Path("ml/training/reports"),
) -> Dict[str, Any]:
    """Promote a candidate model version when it satisfies the evaluation gate."""
    reports_path = Path(reports_root)
    candidate_metrics = _load_version_metrics(reports_path, version)

    registry = ModelRegistry(registry_root)
    production_version = registry.get_current(model_type)
    production_metrics: Optional[Dict[str, Any]] = None
    if production_version is not None:
        production_metrics_path = reports_path / production_version / "metrics.json"
        if production_metrics_path.exists():
            production_metrics = _load_metrics(production_metrics_path)

    if not force and production_metrics is not None:
        _enforce_gate(candidate_metrics, production_metrics)

    _LOGGER.info(
        "Shadow A/B test stub: logging comparison between candidate %s and production %s",
        version,
        production_version,
    )
    registry.promote(model_type, version)

    result: Dict[str, Any] = {
        "version": version,
        "model_type": model_type,
        "forced": force,
        "previous_version": production_version,
        "metrics_delta": _headline_delta(candidate_metrics, production_metrics),
    }
    _append_promotion_log(reports_path, result)
    return result


def main() -> None:
    """CLI entry point for model promotion."""
    parser = argparse.ArgumentParser(description="Promote an evaluated fraud model version.")
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--model-type",
        required=True,
        choices=("xgboost", "dnn", "ensemble_config"),
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    result = promote(
        version=str(args.version),
        model_type=str(args.model_type),
        force=bool(args.force),
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = ["PromotionGateError", "promote", "main"]
