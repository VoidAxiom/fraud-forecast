from __future__ import annotations

import pytest

from ml.quality.hypothesis_registry import HYPOTHESES
from ml.quality.testbed import _build_comparison, main


def test_hypothesis_registry_keys() -> None:
    expected_keys = {
        "h1-no-store-city",
        "h2-no-cancellation-reason",
        "h3-no-lifetime-cb-rate",
        "h4-realistic-delivery-timing",
        "h5-stochastic-patterns",
    }

    assert expected_keys.issubset(HYPOTHESES)


def test_hypothesis_spec_fields() -> None:
    spec = HYPOTHESES["h1-no-store-city"]

    assert spec["status"] == "implemented"
    assert spec["feature_spec_removals"]["HIGH_CARD_HASH"] == ["store_city"]


def test_not_implemented_hypothesis() -> None:
    assert HYPOTHESES["h4-realistic-delivery-timing"]["status"] == "not_yet_implemented"


def test_testbed_not_implemented_prints_warning(capsys: pytest.CaptureFixture[str]) -> None:
    main(
        [
            "--hypothesis",
            "h4-realistic-delivery-timing",
            "--baseline-parquet",
            "x.parquet",
            "--report-dir",
            "x",
        ],
    )

    captured = capsys.readouterr()
    assert "not yet implemented" in captured.out or "WARNING" in captured.out


def test_comparison_json_written() -> None:
    spec = HYPOTHESES["h1-no-store-city"]
    metrics = {"val_aucpr": 0.95, "top10_importances": {"feature_a": 1.5, "feature_b": 0.8}}

    comparison = _build_comparison(
        "h1-no-store-city",
        spec,
        metrics,
        baseline_aucpr=0.99620,
    )

    assert comparison["experiment_aucpr"] == 0.95
    assert comparison["delta"] == pytest.approx(-0.04620, abs=1e-4)
    assert comparison["hypothesis"] == "h1-no-store-city"
