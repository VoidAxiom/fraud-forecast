"""CLI and Python API for training-data distribution quality scans."""

from __future__ import annotations

import argparse
import importlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# The packet requires Python 3.8-compatible typing names here.
# ruff: noqa: UP006, UP007, UP045

Finding = Dict[str, Any]
Config = Dict[str, Any]

_SEVERITIES: Tuple[str, ...] = ("critical", "suggestive", "fyi")
_PERCENTILE_CHECKS: Tuple[Tuple[str, str, str, float], ...] = (
    ("p50", "p50_min", "p50_max", 50.0),
    ("p95", "p95_min", "p95_max", 95.0),
)
MIN_SENTINEL_SUPPORT = 10


def _finding(
    column: str,
    severity: str,
    check_name: str,
    metric: str,
    expected: Any,
    actual: Any,
    recommendation: str,
) -> Finding:
    if severity not in _SEVERITIES:
        raise ValueError(f"Unsupported severity: {severity}")
    return {
        "column": column,
        "severity": severity,
        "check_name": check_name,
        "metric": metric,
        "expected": _json_compatible(expected),
        "actual": _json_compatible(actual),
        "recommendation": recommendation,
    }


def _json_compatible(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (str, int, bool)):
        return value

    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_compatible(item())
        except (TypeError, ValueError):
            pass

    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass

    return str(value)


def _require_mapping(value: Any, context: str) -> Config:
    if not isinstance(value, dict):
        raise ValueError(f"Expected mapping for {context}")
    return {str(key): item for key, item in value.items()}


def _load_config(config_path: Path) -> Config:
    yaml_module: Any = importlib.import_module("yaml")

    with config_path.open("r", encoding="utf-8") as config_file:
        loaded = yaml_module.safe_load(config_file)

    config = _require_mapping(loaded, "config root")
    columns = _require_mapping(config.get("columns"), "columns")
    config["columns"] = {
        column: _require_mapping(entry, f"columns.{column}") for column, entry in columns.items()
    }
    return config


def _optional_float(value: Any, field_name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric or null")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric or null") from exc


def _percentile(series: pd.Series, percentile: float) -> Optional[float]:
    stats_module: Any = importlib.import_module("scipy.stats")

    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return float(stats_module.scoreatpercentile(values.to_numpy(), percentile))


def _outside_range(
    actual: Optional[float],
    expected_min: Optional[float],
    expected_max: Optional[float],
) -> bool:
    if actual is None:
        return True
    if expected_min is not None and actual < expected_min:
        return True
    return expected_max is not None and actual > expected_max


def _range_expected(expected_min: Optional[float], expected_max: Optional[float]) -> Dict[str, Any]:
    return {"min": expected_min, "max": expected_max}


def _profile_expected_distributions(frame: pd.DataFrame, config: Config) -> List[Finding]:
    findings: List[Finding] = []
    columns = _require_mapping(config.get("columns"), "columns")
    for column in sorted(columns):
        entry = _require_mapping(columns[column], f"columns.{column}")
        if column not in frame.columns:
            findings.append(
                _finding(
                    column=column,
                    severity="critical",
                    check_name="distribution_profile",
                    metric="missing_column",
                    expected="Column present in parquet",
                    actual="missing",
                    recommendation="Add the expected column to the training parquet or remove it from the sourced config.",
                )
            )
            continue

        series = frame[column]
        for metric, min_key, max_key, percentile in _PERCENTILE_CHECKS:
            expected_min = _optional_float(entry.get(min_key), f"{column}.{min_key}")
            expected_max = _optional_float(entry.get(max_key), f"{column}.{max_key}")
            if expected_min is None and expected_max is None:
                continue
            actual = _percentile(series, percentile)
            if _outside_range(actual, expected_min, expected_max):
                findings.append(
                    _finding(
                        column=column,
                        severity="critical",
                        check_name="distribution_profile",
                        metric=metric,
                        expected=_range_expected(expected_min, expected_max),
                        actual=actual,
                        recommendation="Revisit simulator generation logic or update the sourced expectation if the data source has changed.",
                    )
                )

        fraud_min = _optional_float(
            entry.get("expected_fraud_rate_min"),
            f"{column}.expected_fraud_rate_min",
        )
        fraud_max = _optional_float(
            entry.get("expected_fraud_rate_max"),
            f"{column}.expected_fraud_rate_max",
        )
        if fraud_min is not None or fraud_max is not None:
            actual_fraud_rate = _fraud_rate(series)
            if _outside_range(actual_fraud_rate, fraud_min, fraud_max):
                findings.append(
                    _finding(
                        column=column,
                        severity="critical",
                        check_name="distribution_profile",
                        metric="fraud_rate",
                        expected=_range_expected(fraud_min, fraud_max),
                        actual=actual_fraud_rate,
                        recommendation="Adjust fraud injection rates or verify that this parquet is drawn from the intended population.",
                    )
                )

    return findings


def _fraud_rate(series: pd.Series) -> Optional[float]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.mean())


def _profile_narrow_numeric_distributions(frame: pd.DataFrame) -> List[Finding]:
    findings: List[Finding] = []
    if frame.empty:
        return findings

    for column in sorted(str(name) for name in frame.columns):
        if column == "gt_is_fraud":
            continue
        series = frame[column]
        if not pd.api.types.is_numeric_dtype(series):
            continue
        unique_count = int(series.nunique(dropna=False))
        if _is_binary_numeric_indicator(series, unique_count):
            continue
        if 1 < unique_count < 5:
            findings.append(
                _finding(
                    column=column,
                    severity="suggestive",
                    check_name="narrow_numeric_distribution",
                    metric="unique_values",
                    expected={">=": 5},
                    actual=unique_count,
                    recommendation="Check whether the simulator collapsed a continuous signal into a small number of buckets.",
                )
            )
    return findings


def _is_binary_numeric_indicator(series: pd.Series, unique_count: int) -> bool:
    if unique_count != 2:
        return False
    return set(series.dropna().unique()) == {0, 1}


def _profile_constant_columns(frame: pd.DataFrame) -> List[Finding]:
    findings: List[Finding] = []
    if frame.empty:
        return findings

    for column in sorted(str(name) for name in frame.columns):
        series = frame[column]
        if int(series.nunique(dropna=False)) == 1:
            findings.append(
                _finding(
                    column=column,
                    severity="fyi",
                    check_name="constant_column",
                    metric="unique_values",
                    expected={">": 1},
                    actual={
                        "unique_values": 1,
                        "value": series.iloc[0],
                    },
                    recommendation="Remove the column from training or restore real variation if the signal is expected to vary.",
                )
            )
    return findings


def _is_string_column(series: pd.Series) -> bool:
    if pd.api.types.is_string_dtype(series):
        return True
    if pd.api.types.is_object_dtype(series) or str(series.dtype) == "category":
        non_null = series.dropna()
        if non_null.empty:
            return False
        return bool(non_null.map(lambda value: isinstance(value, str)).all())
    return False


def _profile_label_leak_sentinels(frame: pd.DataFrame) -> List[Finding]:
    findings: List[Finding] = []
    if "gt_is_fraud" not in frame.columns or frame.empty:
        return findings

    labels = pd.to_numeric(frame["gt_is_fraud"], errors="coerce")
    for column in sorted(str(name) for name in frame.columns):
        if column == "gt_is_fraud":
            continue
        series = frame[column]
        if not _is_string_column(series):
            continue

        value_labels = pd.DataFrame({"value": series, "label": labels})
        # Drop rows where LABEL is null (can't classify). Keep NULL values; NULL
        # can itself be a fraud-only sentinel (e.g. platform=NULL always fraud).
        value_labels = value_labels.dropna(subset=["label"])
        if value_labels.empty:
            continue
        value_labels["value"] = value_labels["value"].fillna("__NULL__").astype(str)
        value_labels["label"] = value_labels["label"].astype(int)
        value_labels = value_labels[value_labels["label"].isin([0, 1])]
        if value_labels.empty:
            continue

        grouped = value_labels.groupby("value", sort=True)["label"].agg(["count", "nunique", "min"])
        for value, row in grouped.iterrows():
            if int(row["count"]) < MIN_SENTINEL_SUPPORT:
                continue
            if int(row["nunique"]) != 1:
                continue
            label_value = int(row["min"])
            if label_value != 1:
                continue
            findings.append(
                _finding(
                    column=column,
                    severity="critical",
                    check_name="sentinel_label_leak",
                    metric="value_has_single_label",
                    expected="No string value should map exclusively to gt_is_fraud=1 (fraud-only sentinel indicates label leakage).",
                    actual={
                        "value": str(value),
                        "gt_is_fraud": label_value,
                        "rows": int(row["count"]),
                    },
                    recommendation="Remove fraud-only sentinel shortcuts from generated categorical values and encode fraud through realistic feature interactions.",
                )
            )

    return findings


def _sort_findings(findings: List[Finding]) -> List[Finding]:
    return sorted(
        findings,
        key=lambda finding: (
            str(finding["column"]),
            str(finding["severity"]),
            str(finding["check_name"]),
            str(finding["metric"]),
            json.dumps(finding["actual"], sort_keys=True, allow_nan=False),
        ),
    )


def _format_report_value(value: Any) -> str:
    native_value = _json_compatible(value)
    if isinstance(native_value, float):
        return f"{native_value:.6f}"
    if isinstance(native_value, (dict, list)):
        return json.dumps(native_value, sort_keys=True, allow_nan=False)
    if native_value is None:
        return "null"
    return str(native_value)


def _escape_markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _write_markdown(findings: List[Finding], report_path: Path) -> None:
    lines: List[str] = [
        "# Distribution Quality Findings",
        "",
        f"Finding count: {len(findings)}",
        "",
    ]
    if not findings:
        lines.append("No findings.")
    else:
        lines.extend(
            [
                "| Severity | Column | Check | Metric | Expected | Actual | Recommendation |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for finding in findings:
            row = [
                _format_report_value(finding["severity"]),
                _format_report_value(finding["column"]),
                _format_report_value(finding["check_name"]),
                _format_report_value(finding["metric"]),
                _format_report_value(finding["expected"]),
                _format_report_value(finding["actual"]),
                _format_report_value(finding["recommendation"]),
            ]
            lines.append("| " + " | ".join(_escape_markdown_cell(item) for item in row) + " |")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_reports(findings: List[Finding], report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "findings.json").write_text(
        json.dumps(findings, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    _write_markdown(findings, report_dir / "findings.md")


def run_scanner(input_path: str, config_path: str, report_dir: str) -> List[Finding]:
    """Profile a training parquet file and write JSON/Markdown findings."""
    frame = pd.read_parquet(Path(input_path))
    config = _load_config(Path(config_path))
    findings: List[Finding] = []
    findings.extend(_profile_expected_distributions(frame, config))
    findings.extend(_profile_narrow_numeric_distributions(frame))
    findings.extend(_profile_constant_columns(frame))
    findings.extend(_profile_label_leak_sentinels(frame))
    sorted_findings = _sort_findings(findings)
    _write_reports(sorted_findings, Path(report_dir))
    return sorted_findings


def main() -> None:
    """CLI entry point for distribution quality scans."""
    parser = argparse.ArgumentParser(
        description="Profile training parquet distributions against sourced expectations.",
    )
    parser.add_argument("--input", required=True, help="Path to the training parquet file.")
    parser.add_argument("--config", required=True, help="Path to expected_distributions.yaml.")
    parser.add_argument("--report-dir", required=True, help="Directory for findings.json/md.")
    args = parser.parse_args()

    findings = run_scanner(
        input_path=str(args.input),
        config_path=str(args.config),
        report_dir=str(args.report_dir),
    )
    print(
        json.dumps(
            {"findings": len(findings), "report_dir": str(args.report_dir)},
            sort_keys=True,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()


__all__ = ["run_scanner", "main"]
