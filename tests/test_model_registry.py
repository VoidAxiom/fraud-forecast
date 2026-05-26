from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ml.registry import ModelRegistry
from ml.registry import model_registry


def test_save_bytes_lists_version_and_current_is_none_before_promote(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)

    version = registry.save("xgboost", b"model-bytes", {"metric": "auc"})

    assert registry.list_versions("xgboost") == [version]
    assert registry.get_current("xgboost") is None


def test_promote_sets_current_version(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)
    version = registry.save("xgboost", b"model-bytes", {"metric": "auc"})

    registry.promote("xgboost", version)

    assert registry.get_current("xgboost") == version


def test_save_multiple_versions_lists_chronologically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions_to_save = [
        "v_20260308_120000",
        "v_20260301_120000",
        "v_20260315_120000",
    ]

    class Clock:
        @classmethod
        def now(cls, tz: object) -> object:
            version = versions_to_save.pop(0)
            return datetime.strptime(version, "v_%Y%m%d_%H%M%S").replace(tzinfo=tz)

    monkeypatch.setattr(model_registry, "datetime", Clock)
    registry = ModelRegistry(tmp_path)

    first = registry.save("xgboost", b"first", {})
    second = registry.save("xgboost", b"second", {})
    third = registry.save("xgboost", b"third", {})

    assert registry.list_versions("xgboost") == sorted([first, second, third])


def test_bytes_artefact_writes_model_bst(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)
    content = b"xgboost-model-content"

    version = registry.save("xgboost", content, {})

    assert (tmp_path / "xgboost" / version / "model.bst").read_bytes() == content


def test_path_artefact_writes_model_bst(tmp_path: Path) -> None:
    source_path = tmp_path / "source.bst"
    content = b"source-model-content"
    source_path.write_bytes(content)
    registry = ModelRegistry(tmp_path)

    version = registry.save("xgboost", source_path, {})

    assert (tmp_path / "xgboost" / version / "model.bst").read_bytes() == content


def test_save_cleans_up_on_failed_artefact(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)
    missing_source_path = tmp_path / "does_not_exist.bst"

    with pytest.raises(FileNotFoundError):
        registry.save("xgboost", missing_source_path, {})

    assert registry.list_versions("xgboost") == []


def test_save_collision_does_not_delete_existing_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_time = datetime(2026, 3, 8, 12, 0, 0, tzinfo=model_registry.LONDON_TZ)

    class Clock:
        @classmethod
        def now(cls, tz: object) -> datetime:
            return fixed_time

    monkeypatch.setattr(model_registry, "datetime", Clock)
    registry = ModelRegistry(tmp_path)
    content = b"original-model-content"

    version = registry.save("xgboost", content, {})

    with pytest.raises(FileExistsError):
        registry.save("xgboost", b"replacement-model-content", {})

    assert registry.list_versions("xgboost") == [version]
    assert (tmp_path / "xgboost" / version / "model.bst").read_bytes() == content


def test_metadata_json_contains_metadata_saved_at_and_version(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)
    metadata = {
        "training_config": {"max_depth": 5},
        "metrics": {"auc": 0.91},
    }

    version = registry.save("xgboost", b"model-bytes", metadata)
    metadata_path = tmp_path / "xgboost" / version / "metadata.json"

    with metadata_path.open("r", encoding="utf-8") as metadata_file:
        saved_metadata = json.load(metadata_file)

    assert saved_metadata["training_config"] == metadata["training_config"]
    assert saved_metadata["metrics"] == metadata["metrics"]
    assert saved_metadata["version"] == version
    assert isinstance(saved_metadata["saved_at"], str)
    assert saved_metadata["saved_at"]


def test_atomic_promote_replaces_existing_production_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions_to_save = ["v_20260301_120000", "v_20260308_120000"]

    class Clock:
        @classmethod
        def now(cls, tz: object) -> object:
            version = versions_to_save.pop(0)
            return datetime.strptime(version, "v_%Y%m%d_%H%M%S").replace(tzinfo=tz)

    monkeypatch.setattr(model_registry, "datetime", Clock)
    registry = ModelRegistry(tmp_path)
    first = registry.save("xgboost", b"first", {})
    second = registry.save("xgboost", b"second", {})

    registry.promote("xgboost", first)
    assert registry.get_current("xgboost") == first

    registry.promote("xgboost", second)

    assert registry.get_current("xgboost") == second


def test_promote_nonexistent_version_raises_error(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)

    with pytest.raises((FileNotFoundError, ValueError)):
        registry.promote("xgboost", "v_20260301_120000")


def test_invalid_model_type_absolute_path(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)

    with pytest.raises(ValueError, match="Invalid model_type"):
        registry.save("/tmp/evil", b"x", {})


def test_invalid_model_type_dotdot(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)

    with pytest.raises(ValueError, match="Invalid model_type"):
        registry.save("../escape", b"x", {})


def test_invalid_model_type_empty(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)

    with pytest.raises(ValueError, match="Invalid model_type"):
        registry.save("", b"x", {})
