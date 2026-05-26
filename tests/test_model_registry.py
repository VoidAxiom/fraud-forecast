from __future__ import annotations

import json
import uuid
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Iterator

import pytest

from ml.registry import ModelRegistry
from ml.registry import model_registry


def _uuid_with_prefix(prefix: str) -> uuid.UUID:
    return uuid.UUID(prefix + ("0" * 24))


def test_save_bytes_lists_version_and_current_is_none_before_promote(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)

    version = registry.save("xgboost", b"model-bytes", {"metric": "auc"})

    assert registry.list_versions("xgboost") == [version]
    assert registry.get_current("xgboost") is None


def test_string_root_is_coerced_to_path(tmp_path: Path) -> None:
    registry = ModelRegistry(str(tmp_path))

    version = registry.save("xgboost", b"model-bytes", {})

    assert registry.list_versions("xgboost") == [version]


def test_promote_sets_current_version(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)
    version = registry.save("xgboost", b"model-bytes", {"metric": "auc"})

    registry.promote("xgboost", version)

    assert registry.get_current("xgboost") == version


def test_get_current_returns_none_for_broken_symlink(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)
    model_type_dir = tmp_path / "xgboost"
    model_type_dir.mkdir()
    (model_type_dir / "production").symlink_to(
        "v20260301_120000_abcdef12",
        target_is_directory=True,
    )

    assert registry.get_current("xgboost") is None


def test_save_multiple_versions_lists_chronologically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions_to_save = [
        "v20260301_120000_ffffffff",
        "v20260301_120000_aaaaaaaa",
        "v20260308_120000_88888888",
    ]
    save_microseconds = [100000, 200000, 100000]
    uuid_suffixes = [version.rsplit("_", 1)[1] for version in versions_to_save]

    class Clock:
        @classmethod
        def now(cls, tz: tzinfo) -> datetime:
            version = versions_to_save.pop(0)
            timestamp = version.rsplit("_", 1)[0]
            return datetime.strptime(timestamp, "v%Y%m%d_%H%M%S").replace(
                tzinfo=tz,
                microsecond=save_microseconds.pop(0),
            )

    def next_uuid() -> uuid.UUID:
        return _uuid_with_prefix(uuid_suffixes.pop(0))

    monkeypatch.setattr(model_registry, "datetime", Clock)
    monkeypatch.setattr(uuid, "uuid4", next_uuid)
    registry = ModelRegistry(tmp_path)

    first = registry.save("xgboost", b"first", {})
    second = registry.save("xgboost", b"second", {})
    third = registry.save("xgboost", b"third", {})

    model_type_dir = tmp_path / "xgboost"
    listing_order = [
        model_type_dir / second,
        model_type_dir / first,
        model_type_dir / third,
    ]
    original_iterdir = Path.iterdir

    def ordered_iterdir(path: Path) -> Iterator[Path]:
        if path == model_type_dir:
            return iter(listing_order)
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", ordered_iterdir)

    assert registry.list_versions("xgboost") == [first, second, third]


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


def test_directory_artefact_copies_saved_model_dir(tmp_path: Path) -> None:
    source_dir = tmp_path / "source_saved_model"
    variables_dir = source_dir / "variables"
    graph_content = b"saved-model-graph"
    weights_content = b"saved-model-weights"
    index_content = "saved-model-index"
    variables_dir.mkdir(parents=True)
    (source_dir / "saved_model.pb").write_bytes(graph_content)
    (variables_dir / "variables.data-00000-of-00001").write_bytes(weights_content)
    (variables_dir / "variables.index").write_text(index_content, encoding="utf-8")
    registry = ModelRegistry(tmp_path)

    version = registry.save("dnn", source_dir, {})

    saved_model_dir = tmp_path / "dnn" / version / "saved_model"
    assert saved_model_dir.is_dir()
    assert (saved_model_dir / "saved_model.pb").read_bytes() == graph_content
    assert (
        saved_model_dir / "variables" / "variables.data-00000-of-00001"
    ).read_bytes() == weights_content
    assert (saved_model_dir / "variables" / "variables.index").read_text(
        encoding="utf-8"
    ) == index_content


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
    fixed_uuid = _uuid_with_prefix("abcdef12")

    class Clock:
        @classmethod
        def now(cls, tz: tzinfo) -> datetime:
            return fixed_time

    monkeypatch.setattr(model_registry, "datetime", Clock)
    monkeypatch.setattr(uuid, "uuid4", lambda: fixed_uuid)
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
    versions_to_save = ["v20260301_120000_aaaaaaaa", "v20260308_120000_bbbbbbbb"]
    uuid_suffixes = [version.rsplit("_", 1)[1] for version in versions_to_save]

    class Clock:
        @classmethod
        def now(cls, tz: tzinfo) -> datetime:
            version = versions_to_save.pop(0)
            timestamp = version.rsplit("_", 1)[0]
            return datetime.strptime(timestamp, "v%Y%m%d_%H%M%S").replace(tzinfo=tz)

    def next_uuid() -> uuid.UUID:
        if uuid_suffixes:
            return _uuid_with_prefix(uuid_suffixes.pop(0))
        return _uuid_with_prefix("cccccccc")

    monkeypatch.setattr(model_registry, "datetime", Clock)
    monkeypatch.setattr(uuid, "uuid4", next_uuid)
    registry = ModelRegistry(tmp_path)
    first = registry.save("xgboost", b"first", {})
    second = registry.save("xgboost", b"second", {})

    registry.promote("xgboost", first)
    assert registry.get_current("xgboost") == first

    registry.promote("xgboost", second)

    assert registry.get_current("xgboost") == second


def test_promote_nonexistent_version_raises_error(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)

    with pytest.raises(FileNotFoundError, match="Model version does not exist"):
        registry.promote("xgboost", "v20260301_120000_abcdef12")


def test_promote_rejects_symlink_version_dir(tmp_path: Path) -> None:
    registry = ModelRegistry(tmp_path)
    model_type_dir = tmp_path / "xgboost"
    target_dir = tmp_path / "outside_registry"
    version = "v20260301_120000_abcdef12"
    model_type_dir.mkdir()
    target_dir.mkdir()
    (model_type_dir / version).symlink_to(target_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="Version dir is a symlink"):
        registry.promote("xgboost", version)

    assert registry.get_current("xgboost") is None


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


def test_get_current_returns_none_when_production_points_outside_model_type_dir(
    tmp_path: Path,
) -> None:
    registry = ModelRegistry(tmp_path)
    model_type_dir = tmp_path / "xgboost"
    outside_version_dir = tmp_path / "v20260301_120000_abcdef12"
    model_type_dir.mkdir()
    outside_version_dir.mkdir()
    (model_type_dir / "production").symlink_to(outside_version_dir, target_is_directory=True)

    assert registry.get_current("xgboost") is None
