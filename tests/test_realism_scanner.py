from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from ml.quality.scanner import run_scanner


@pytest.fixture
def minimal_config_path(tmp_path: Path) -> Path:
    config_path = tmp_path / "expected_distributions.yaml"
    config_path.write_text(
        yaml.safe_dump({"version": "1", "columns": {}}, sort_keys=True),
        encoding="utf-8",
    )
    return config_path


def _scan_frame(
    frame: pd.DataFrame,
    tmp_path: Path,
    config_path: Path,
) -> list[dict[str, Any]]:
    input_path = tmp_path / "input.parquet"
    report_dir = tmp_path / "reports"
    frame.to_parquet(input_path)
    return run_scanner(
        input_path=str(input_path),
        config_path=str(config_path),
        report_dir=str(report_dir),
    )


def test_sentinel_detection(tmp_path: Path, minimal_config_path: Path) -> None:
    labels = [0, 1] * 10
    frame = pd.DataFrame(
        {
            "store_city": ["FRAUD_RING" if label == 1 else "London" for label in labels],
            "gt_is_fraud": labels,
        },
    )

    findings = _scan_frame(frame, tmp_path, minimal_config_path)

    assert any(
        finding["check_name"] == "sentinel_label_leak" and finding["column"] == "store_city"
        for finding in findings
    )


def test_constant_column_flagged(tmp_path: Path, minimal_config_path: Path) -> None:
    frame = pd.DataFrame({"ip_country": ["GB"] * 12})

    findings = _scan_frame(frame, tmp_path, minimal_config_path)

    assert any(
        finding["check_name"] == "constant_column"
        and finding["column"] == "ip_country"
        and finding["severity"] == "fyi"
        for finding in findings
    )


def test_narrow_distribution_flagged(tmp_path: Path, minimal_config_path: Path) -> None:
    frame = pd.DataFrame({"user_account_age_days": [1, 2, 3] * 4})

    findings = _scan_frame(frame, tmp_path, minimal_config_path)

    assert any(
        finding["check_name"] == "narrow_numeric_distribution"
        and finding["column"] == "user_account_age_days"
        for finding in findings
    )


def test_distribution_vs_yaml(tmp_path: Path) -> None:
    input_path = tmp_path / "input.parquet"
    config_path = tmp_path / "expected_distributions.yaml"
    report_dir = tmp_path / "reports"
    pd.DataFrame({"subtotal_pence": [50] * 12}).to_parquet(input_path)
    config_path.write_text(
        'version: "1"\n'
        "columns:\n"
        "  subtotal_pence:\n"
        '    description: "test"\n'
        '    source: "https://example.com"\n'
        "    p50_min: 800\n"
        "    p50_max: 2500\n",
        encoding="utf-8",
    )

    findings = run_scanner(
        input_path=str(input_path),
        config_path=str(config_path),
        report_dir=str(report_dir),
    )

    assert any(
        finding["check_name"] == "distribution_profile"
        and finding["column"] == "subtotal_pence"
        for finding in findings
    )


def test_no_findings_no_crash(tmp_path: Path, minimal_config_path: Path) -> None:
    countries = [f"C{index:02d}" for index in range(20)]
    frame = pd.DataFrame(
        {
            "subtotal_pence": [800 + ((index * 43) % 1701) for index in range(60)],
            "ip_country": [countries[index % len(countries)] for index in range(60)],
        },
    )

    findings = _scan_frame(frame, tmp_path, minimal_config_path)

    assert isinstance(findings, list)


def test_null_platform_sentinel_detected(tmp_path: Path, minimal_config_path: Path) -> None:
    """NULL value that exclusively maps to fraud=1 should be flagged as sentinel."""
    frame = pd.DataFrame(
        {
            "platform": [None] * 10 + ["iOS"] * 10,
            "gt_is_fraud": [1] * 10 + [0] * 10,
        }
    )

    findings = _scan_frame(frame, tmp_path, minimal_config_path)

    assert any(
        finding["check_name"] == "sentinel_label_leak"
        and finding["column"] == "platform"
        and finding["actual"]["value"] == "__NULL__"
        for finding in findings
    ), f"Expected NULL sentinel finding, got: {findings}"
