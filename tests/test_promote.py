from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import pytest

import ml.training.promote as promote_module
from ml.training.promote import PromotionGateError

# The packet requires Python 3.8-compatible typing names here.
# ruff: noqa: UP006, UP007, UP045


class RecordingRegistry:
    def __init__(self, current_version: Optional[str]) -> None:
        self.current_version = current_version
        self.promotions: List[Tuple[str, str]] = []
        self.roots: List[Path] = []

    def get_current(self, model_type: str) -> Optional[str]:
        return self.current_version

    def promote(self, model_type: str, version: str) -> None:
        self.promotions.append((model_type, version))
        self.current_version = version


def _patch_registry(
    monkeypatch: pytest.MonkeyPatch,
    registry: RecordingRegistry,
) -> None:
    def registry_factory(root: Union[Path, str]) -> RecordingRegistry:
        registry.roots.append(Path(root))
        return registry

    monkeypatch.setattr(promote_module, "ModelRegistry", registry_factory)


def _write_metrics(
    reports_root: Path,
    version: str,
    overrides: Optional[Dict[str, float]] = None,
) -> None:
    metrics: Dict[str, float] = {
        "auprc": 0.8,
        "auroc": 0.9,
        "brier_score": 0.1,
        "recall_stolen_card": 0.8,
        "recall_account_takeover": 0.7,
    }
    if overrides is not None:
        metrics.update(overrides)

    metrics_path = reports_root / version / "metrics.json"
    metrics_path.parent.mkdir(parents=True)
    metrics_path.write_text(json.dumps(metrics, sort_keys=True) + "\n", encoding="utf-8")


def test_candidate_worse_by_more_than_auprc_tolerance_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = tmp_path / "reports"
    production_version = "v20260301_120000_aaaaaaaa"
    candidate_version = "v20260302_120000_bbbbbbbb"
    _write_metrics(reports_root, production_version, {"auprc": 0.8})
    _write_metrics(reports_root, candidate_version, {"auprc": 0.78})
    registry = RecordingRegistry(production_version)
    _patch_registry(monkeypatch, registry)

    with pytest.raises(PromotionGateError, match="AUPRC"):
        promote_module.promote(
            candidate_version,
            "xgboost",
            registry_root=tmp_path / "registry",
            reports_root=reports_root,
        )

    assert registry.promotions == []


def test_candidate_within_auprc_tolerance_and_no_recall_drop_is_promoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = tmp_path / "reports"
    production_version = "v20260301_120000_aaaaaaaa"
    candidate_version = "v20260302_120000_bbbbbbbb"
    _write_metrics(reports_root, production_version, {"auprc": 0.8})
    _write_metrics(reports_root, candidate_version, {"auprc": 0.795})
    registry = RecordingRegistry(production_version)
    _patch_registry(monkeypatch, registry)

    result = promote_module.promote(
        candidate_version,
        "xgboost",
        registry_root=tmp_path / "registry",
        reports_root=reports_root,
    )

    assert registry.promotions == [("xgboost", candidate_version)]
    assert result["previous_version"] == production_version
    metrics_delta = result["metrics_delta"]
    assert isinstance(metrics_delta, dict)
    assert metrics_delta["auprc"] == pytest.approx(-0.005)


def test_per_category_recall_drop_greater_than_ten_percent_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = tmp_path / "reports"
    production_version = "v20260301_120000_aaaaaaaa"
    candidate_version = "v20260302_120000_bbbbbbbb"
    _write_metrics(
        reports_root,
        production_version,
        {"auprc": 0.8, "recall_stolen_card": 0.8},
    )
    _write_metrics(
        reports_root,
        candidate_version,
        {"auprc": 0.8, "recall_stolen_card": 0.7},
    )
    registry = RecordingRegistry(production_version)
    _patch_registry(monkeypatch, registry)

    with pytest.raises(PromotionGateError, match="recall_stolen_card"):
        promote_module.promote(
            candidate_version,
            "xgboost",
            registry_root=tmp_path / "registry",
            reports_root=reports_root,
        )

    assert registry.promotions == []


def test_force_overrides_promotion_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = tmp_path / "reports"
    production_version = "v20260301_120000_aaaaaaaa"
    candidate_version = "v20260302_120000_bbbbbbbb"
    _write_metrics(
        reports_root,
        production_version,
        {"auprc": 0.9, "recall_stolen_card": 0.9},
    )
    _write_metrics(
        reports_root,
        candidate_version,
        {"auprc": 0.5, "recall_stolen_card": 0.1},
    )
    registry = RecordingRegistry(production_version)
    _patch_registry(monkeypatch, registry)

    result = promote_module.promote(
        candidate_version,
        "xgboost",
        force=True,
        registry_root=tmp_path / "registry",
        reports_root=reports_root,
    )

    assert registry.promotions == [("xgboost", candidate_version)]
    assert result["forced"] is True


def test_no_existing_production_version_promotes_first_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = tmp_path / "reports"
    candidate_version = "v20260302_120000_bbbbbbbb"
    _write_metrics(reports_root, candidate_version, {"auprc": 0.5})
    registry = RecordingRegistry(None)
    _patch_registry(monkeypatch, registry)

    result = promote_module.promote(
        candidate_version,
        "xgboost",
        registry_root=tmp_path / "registry",
        reports_root=reports_root,
    )

    assert registry.promotions == [("xgboost", candidate_version)]
    assert result["previous_version"] is None
    assert result["metrics_delta"] == {}


def test_promote_returns_expected_result_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = tmp_path / "reports"
    production_version = "v20260301_120000_aaaaaaaa"
    candidate_version = "v20260302_120000_bbbbbbbb"
    _write_metrics(reports_root, production_version, {"auprc": 0.8, "auroc": 0.9})
    _write_metrics(reports_root, candidate_version, {"auprc": 0.81, "auroc": 0.92})
    registry = RecordingRegistry(production_version)
    _patch_registry(monkeypatch, registry)

    result = promote_module.promote(
        candidate_version,
        "dnn",
        registry_root=tmp_path / "registry",
        reports_root=reports_root,
    )

    assert set(result) == {
        "version",
        "model_type",
        "forced",
        "previous_version",
        "metrics_delta",
    }
    assert result["version"] == candidate_version
    assert result["model_type"] == "dnn"
    assert result["forced"] is False
    assert result["previous_version"] == production_version
    assert result["metrics_delta"] == {
        "auprc": pytest.approx(0.01),
        "auroc": pytest.approx(0.02),
        "brier_score": pytest.approx(0.0),
    }
