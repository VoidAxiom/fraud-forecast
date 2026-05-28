from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ml.quality.hypothesis_registry import HYPOTHESES, HypothesisSpec

# The worker packet requires Python 3.8-compatible typing names here.
# ruff: noqa: UP006, UP007, UP045


METRIC_KEYS: Tuple[str, ...] = ("val_aucpr", "experiment_aucpr", "auprc", "val_auprc")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_json_object(path: Path) -> Dict[str, Any]:
    loaded: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return {str(key): value for key, value in loaded.items()}


def _write_json_object(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _metric_as_float(metrics: Dict[str, Any], source: str) -> float:
    for metric_key in METRIC_KEYS:
        value = metrics.get(metric_key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    raise ValueError(f"Could not find numeric AUPRC metric in {source}")


def _baseline_metrics_path(baseline_report_dir: str) -> Path:
    path = Path(baseline_report_dir)
    if path.is_file() or path.suffix == ".json":
        return path
    return path / "metrics.json"


def _load_baseline_aucpr(baseline_report_dir: Optional[str]) -> Optional[float]:
    if baseline_report_dir is None:
        return None
    metrics_path = _baseline_metrics_path(baseline_report_dir)
    return _metric_as_float(_read_json_object(metrics_path), str(metrics_path))


def _top10_importances(metrics: Dict[str, Any]) -> Dict[str, float]:
    raw_importances = metrics.get("top10_importances")
    if not isinstance(raw_importances, dict):
        raise ValueError("Experiment metrics did not include top10_importances")

    parsed_importances: List[Tuple[str, float]] = []
    for feature_name, importance in raw_importances.items():
        if isinstance(importance, bool) or not isinstance(importance, (int, float)):
            raise ValueError(f"Feature importance for {feature_name!r} is not numeric")
        parsed_importances.append((str(feature_name), float(importance)))

    parsed_importances.sort(key=lambda item: item[1], reverse=True)
    return dict(parsed_importances[:10])


def _build_comparison(
    hypothesis: str,
    spec: HypothesisSpec,
    metrics: Dict[str, Any],
    baseline_aucpr: Optional[float],
) -> Dict[str, Any]:
    experiment_aucpr = _metric_as_float(metrics, "experiment metrics")
    delta: Optional[float] = None if baseline_aucpr is None else experiment_aucpr - baseline_aucpr

    return {
        "hypothesis": hypothesis,
        "baseline_aucpr": baseline_aucpr,
        "experiment_aucpr": experiment_aucpr,
        "delta": delta,
        "top10_importances": _top10_importances(metrics),
        "status": spec["status"],
    }


def _format_metric(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:.6f}"


def _format_delta(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.6f}"


def _comparison_markdown(comparison: Dict[str, Any], description: str) -> str:
    top10_importances = comparison["top10_importances"]
    if not isinstance(top10_importances, dict):
        raise ValueError("comparison top10_importances must be a JSON object")

    baseline_aucpr = comparison["baseline_aucpr"]
    experiment_aucpr = comparison["experiment_aucpr"]
    delta = comparison["delta"]
    if baseline_aucpr is not None and not isinstance(baseline_aucpr, float):
        raise ValueError("comparison baseline_aucpr must be a float or null")
    if not isinstance(experiment_aucpr, float):
        raise ValueError("comparison experiment_aucpr must be a float")
    if delta is not None and not isinstance(delta, float):
        raise ValueError("comparison delta must be a float or null")

    lines = [
        "# VOI-269 Quality Testbed Comparison",
        "",
        f"Hypothesis: `{comparison['hypothesis']}`",
        "",
        description,
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Baseline AUPRC | {_format_metric(baseline_aucpr)} |",
        f"| Experiment AUPRC | {_format_metric(experiment_aucpr)} |",
        f"| Delta | {_format_delta(delta)} |",
        "",
        "## Top 10 Feature Importances",
        "",
        "| Rank | Feature | Gain |",
        "| ---: | --- | ---: |",
    ]

    for rank, (feature_name, importance) in enumerate(top10_importances.items(), start=1):
        if isinstance(importance, bool) or not isinstance(importance, (int, float)):
            raise ValueError(f"comparison importance for {feature_name!r} must be numeric")
        lines.append(f"| {rank} | `{feature_name}` | {float(importance):.6f} |")

    if not top10_importances:
        lines.append("| N/A | N/A | N/A |")

    return "\n".join(lines) + "\n"


def _write_reports(
    report_dir: Path,
    metrics: Dict[str, Any],
    comparison: Dict[str, Any],
    description: str,
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    _write_json_object(report_dir / "metrics.json", metrics)
    _write_json_object(report_dir / "comparison.json", comparison)
    (report_dir / "comparison.md").write_text(
        _comparison_markdown(comparison, description),
        encoding="utf-8",
    )


def _build_subprocess_script(
    hypothesis: str,
    spec: HypothesisSpec,
    baseline_parquet: Path,
    transform_output_dir: Path,
    metrics_path: Path,
    work_dir: Path,
) -> str:
    payload: Dict[str, Any] = {
        "hypothesis": hypothesis,
        "repo_root": str(_repo_root()),
        "baseline_parquet": str(baseline_parquet),
        "transform_output_dir": str(transform_output_dir),
        "metrics_path": str(metrics_path),
        "work_dir": str(work_dir),
        "feature_spec_removals": spec["feature_spec_removals"],
    }
    payload_json = json.dumps(payload, sort_keys=True)
    template = r"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

PAYLOAD_JSON = __PAYLOAD_JSON__
PAYLOAD: Dict[str, Any] = json.loads(PAYLOAD_JSON)

sys.path.insert(0, str(PAYLOAD["repo_root"]))
Path(str(PAYLOAD["work_dir"])).mkdir(parents=True, exist_ok=True)
os.chdir(str(PAYLOAD["work_dir"]))

import tensorflow as tf  # type: ignore[import-untyped]  # TensorFlow stubs are incomplete.
import tensorflow_transform as tft  # type: ignore[import-untyped]
import xgboost as xgb  # type: ignore[import-untyped]
from sklearn.metrics import average_precision_score  # type: ignore[import-untyped]

from ml.transform import preprocessing as preprocessing_module
from ml.transform import run_transform

xgboost_module = importlib.import_module("ml.training.train_xgboost")

FEATURE_SPEC_REMOVALS: Dict[str, List[str]] = {
    str(category): [str(feature_name) for feature_name in feature_names]
    for category, feature_names in PAYLOAD["feature_spec_removals"].items()
}
HIGH_CARD_REMOVALS = FEATURE_SPEC_REMOVALS.get("HIGH_CARD_HASH", [])
LOW_CARD_REMOVALS = FEATURE_SPEC_REMOVALS.get("LOW_CARD_CATEGORICAL", [])
NUMERICAL_REMOVALS = FEATURE_SPEC_REMOVALS.get("NUMERICAL_FEATURES", [])
ALL_REMOVALS = HIGH_CARD_REMOVALS + LOW_CARD_REMOVALS + NUMERICAL_REMOVALS


def _remove_list_items(values: List[str], removals: List[str]) -> None:
    if not removals:
        return
    removal_set = set(removals)
    values[:] = [value for value in values if value not in removal_set]


def _remove_tuple_items(values: Tuple[str, ...], removals: List[str]) -> Tuple[str, ...]:
    if not removals:
        return values
    removal_set = set(removals)
    return tuple(value for value in values if value not in removal_set)


def _patch_run_transform() -> None:
    for feature_name in HIGH_CARD_REMOVALS:
        run_transform.HIGH_CARD_HASH_FEATURES.pop(feature_name, None)
    _remove_list_items(run_transform.LOW_CARD_CATEGORICAL, LOW_CARD_REMOVALS)
    _remove_list_items(run_transform.NUMERICAL_FEATURES, NUMERICAL_REMOVALS)
    _remove_list_items(run_transform.FLOAT_NUMERICAL_FEATURES, NUMERICAL_REMOVALS)
    run_transform.INTEGER_NUMERICAL_FEATURES.difference_update(NUMERICAL_REMOVALS)
    run_transform.NULLABLE_CATEGORICALS.difference_update(LOW_CARD_REMOVALS)
    run_transform.STRING_FEATURES = _remove_tuple_items(run_transform.STRING_FEATURES, ALL_REMOVALS)
    for feature_name in ALL_REMOVALS:
        run_transform.FEATURE_SPEC.pop(feature_name, None)


def _patched_preprocessing_fn(inputs: Dict[str, Any]) -> Dict[str, Any]:
    outputs: Dict[str, Any] = {}

    for feature_name in tuple(run_transform.NUMERICAL_FEATURES):
        if feature_name in {
            "total_pence",
            "subtotal_pence",
            "user_lifetime_order_count",
            "device_lifetime_order_count",
        }:
            value = tf.math.log1p(tf.cast(inputs[feature_name], tf.float32))
        else:
            value = tf.cast(inputs[feature_name], tf.float32)
        outputs[feature_name] = tft.scale_to_z_score(value)

    nullable_categoricals = set(run_transform.NULLABLE_CATEGORICALS)
    for feature_name in tuple(run_transform.LOW_CARD_CATEGORICAL):
        value = inputs[feature_name]
        if feature_name in nullable_categoricals:
            value = tf.where(tf.equal(value, b""), tf.constant(b"UNKNOWN"), value)
        outputs[feature_name] = tft.compute_and_apply_vocabulary(
            value,
            top_k=20,
            num_oov_buckets=1,
            vocab_filename=f"vocab_{feature_name}",
        )

    for feature_name, buckets in dict(run_transform.HIGH_CARD_HASH_FEATURES).items():
        outputs[feature_name] = tft.hash_strings(inputs[feature_name], hash_buckets=buckets)

    for feature_name in tuple(run_transform.BOOLEAN_FEATURES):
        outputs[feature_name] = tf.cast(inputs[feature_name], tf.int64)

    outputs["geo_mismatch_score"] = tft.scale_to_z_score(
        tf.cast(inputs["ip_to_delivery_distance_km"], tf.float32)
        + tf.cast(inputs["billing_to_delivery_distance_km"], tf.float32)
    )
    outputs["card_country_mismatch"] = tf.cast(
        tf.not_equal(inputs["card_issuer_country"], inputs["ip_country"]),
        tf.int64,
    )
    outputs["velocity_ratio_1h_vs_lifetime"] = tft.scale_to_z_score(
        tf.cast(inputs["user_orders_1h_at_order_time"], tf.float32)
        / (tf.cast(inputs["user_lifetime_order_count"], tf.float32) + 1.0)
    )
    outputs["label"] = tf.cast(inputs["gt_is_fraud"], tf.int64)
    return outputs


def _patch_preprocessing() -> None:
    preprocessing_module.preprocessing_fn = _patched_preprocessing_fn
    run_transform.preprocessing_fn = _patched_preprocessing_fn


def _patch_xgboost_module() -> None:
    xgboost_module.FLOAT_FEATURE_NAMES = _remove_tuple_items(
        xgboost_module.FLOAT_FEATURE_NAMES,
        NUMERICAL_REMOVALS,
    )
    xgboost_module.INT_FEATURE_NAMES = _remove_tuple_items(
        xgboost_module.INT_FEATURE_NAMES,
        HIGH_CARD_REMOVALS + LOW_CARD_REMOVALS,
    )
    xgboost_module.FEATURE_NAMES = (
        xgboost_module.FLOAT_FEATURE_NAMES + xgboost_module.INT_FEATURE_NAMES
    )
    for feature_name in ALL_REMOVALS:
        xgboost_module._FEATURE_SPEC.pop(feature_name, None)


def _predict_scores(model: Any, dmatrix: Any) -> Any:
    best_ntree_limit = getattr(model, "best_ntree_limit", 0)
    if isinstance(best_ntree_limit, int) and best_ntree_limit > 0:
        try:
            return model.predict(dmatrix, ntree_limit=best_ntree_limit)
        except TypeError:
            pass

    best_iteration = getattr(model, "best_iteration", None)
    if best_iteration is not None:
        try:
            return model.predict(dmatrix, iteration_range=(0, int(best_iteration) + 1))
        except TypeError:
            pass

    return model.predict(dmatrix)


def _top10_importances(importance: Dict[str, float]) -> Dict[str, float]:
    sorted_importance = sorted(importance.items(), key=lambda item: item[1], reverse=True)
    return {
        feature_name: float(importance_score)
        for feature_name, importance_score in sorted_importance[:10]
    }


def main() -> None:
    _patch_run_transform()
    _patch_preprocessing()
    _patch_xgboost_module()

    transform_output_dir = str(PAYLOAD["transform_output_dir"])
    Path(transform_output_dir).mkdir(parents=True, exist_ok=True)
    run_transform.run_pipeline(
        str(PAYLOAD["baseline_parquet"]),
        transform_output_dir,
        _patched_preprocessing_fn,
    )

    train_dir = os.path.join(transform_output_dir, "train")
    val_dir = os.path.join(transform_output_dir, "val")
    model, importance = xgboost_module.train_xgboost(train_dir, val_dir, hyperparams={})

    x_val, y_val = xgboost_module.tfrecords_to_numpy(val_dir)
    dval = xgb.DMatrix(x_val, feature_names=list(xgboost_module.FEATURE_NAMES))
    y_scores = _predict_scores(model, dval)
    val_aucpr = float(average_precision_score(y_val, y_scores))

    metrics: Dict[str, Any] = {
        "hypothesis": str(PAYLOAD["hypothesis"]),
        "val_aucpr": val_aucpr,
        "auprc": val_aucpr,
        "top10_importances": _top10_importances(importance),
        "feature_names": list(xgboost_module.FEATURE_NAMES),
    }
    Path(str(PAYLOAD["metrics_path"])).write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
"""
    return textwrap.dedent(template).replace("__PAYLOAD_JSON__", json.dumps(payload_json))


def _run_experiment_subprocess(
    hypothesis: str,
    spec: HypothesisSpec,
    baseline_parquet: Path,
) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ff_quality_testbed_") as temp_dir:
        temp_root = Path(temp_dir)
        metrics_path = temp_root / "metrics.json"
        script_path = temp_root / "run_experiment.py"
        transform_output_dir = temp_root / "transform"
        work_dir = temp_root / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

        script_path.write_text(
            _build_subprocess_script(
                hypothesis=hypothesis,
                spec=spec,
                baseline_parquet=baseline_parquet,
                transform_output_dir=transform_output_dir,
                metrics_path=metrics_path,
                work_dir=work_dir,
            ),
            encoding="utf-8",
        )

        completed = subprocess.run([sys.executable, str(script_path)], check=False)
        if completed.returncode != 0:
            raise RuntimeError(
                f"Hypothesis subprocess failed with exit code {completed.returncode}",
            )

        return _read_json_object(metrics_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run VOI-269 feature realism quality testbed.")
    parser.add_argument("--hypothesis", required=True, choices=sorted(HYPOTHESES))
    parser.add_argument("--baseline-parquet", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument(
        "--baseline-report-dir",
        help="Directory containing metrics.json, or a metrics JSON file, for AUPRC delta.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    hypothesis = str(args.hypothesis)
    spec = HYPOTHESES[hypothesis]
    if spec["status"] == "not_yet_implemented":
        print(f"WARNING: {hypothesis} is not yet implemented: {spec['description']}")
        return

    baseline_report_dir: Optional[str]
    if args.baseline_report_dir is None:
        baseline_report_dir = None
    else:
        baseline_report_dir = str(args.baseline_report_dir)

    baseline_aucpr = _load_baseline_aucpr(baseline_report_dir)
    report_dir = Path(str(args.report_dir))
    baseline_parquet = Path(str(args.baseline_parquet)).resolve()

    metrics = _run_experiment_subprocess(
        hypothesis=hypothesis,
        spec=spec,
        baseline_parquet=baseline_parquet,
    )
    comparison = _build_comparison(
        hypothesis=hypothesis,
        spec=spec,
        metrics=metrics,
        baseline_aucpr=baseline_aucpr,
    )
    _write_reports(report_dir, metrics, comparison, spec["description"])
    print(f"Wrote comparison report to {report_dir / 'comparison.json'}")
    print(f"Wrote markdown report to {report_dir / 'comparison.md'}")


if __name__ == "__main__":
    main()
