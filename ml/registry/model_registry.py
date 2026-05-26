"""Filesystem-backed version registry for trained model artefacts."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Pattern, Union

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo

LONDON_TZ = ZoneInfo("Europe/London")
MODEL_FILENAME = "model.bst"
PRODUCTION_LINK = "production"
VERSION_RE: Pattern[str] = re.compile(r"^v_\d{8}_\d{6}_[0-9a-f]{8}$")


class ModelRegistry:
    """Pure-filesystem store for versioned model artefacts."""

    def __init__(
        self,
        root: Union[str, Path] = Path("/var/lib/models"),  # noqa: UP007 - packet requires Python 3.8 syntax.
    ) -> None:
        self.root = Path(root)

    def save(
        self,
        model_type: str,
        model_artefact: Union[bytes, Path],  # noqa: UP007 - packet requires Python 3.8 syntax.
        metadata: Dict[str, object],  # noqa: UP006 - packet requires Python 3.8 syntax.
    ) -> str:
        """Save bytes, a file Path, or a directory Path as a new version."""
        now = datetime.now(tz=LONDON_TZ)
        version = now.strftime("v_%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        model_type_dir = self._model_type_dir(model_type)
        version_dir = model_type_dir / version

        created = False
        try:
            model_type_dir.mkdir(parents=True, exist_ok=True)
            version_dir.mkdir()
            created = True

            model_path = version_dir / MODEL_FILENAME
            if isinstance(model_artefact, Path) and model_artefact.is_dir():
                model_path = version_dir / "saved_model"

            self._write_model_artefact(model_artefact, model_path)
            self._write_metadata(metadata, version, now, version_dir / "metadata.json")
        except BaseException:
            if created:
                shutil.rmtree(version_dir, ignore_errors=True)
            raise

        return version

    def promote(self, model_type: str, version: str) -> None:
        """Atomically swap the production symlink to point at `version`."""
        if not VERSION_RE.fullmatch(version):
            raise ValueError(f"Invalid model version: {version}")

        model_type_dir = self._model_type_dir(model_type)
        version_dir = model_type_dir / version
        if not version_dir.is_dir():
            raise FileNotFoundError(f"Model version does not exist: {version}")
        if version_dir.is_symlink():
            raise ValueError(
                f"Version dir is a symlink, not a real directory: {version}"
            )

        prod_symlink = model_type_dir / PRODUCTION_LINK
        tmp_symlink = (
            model_type_dir
            / f".{PRODUCTION_LINK}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        )

        if tmp_symlink.exists() or tmp_symlink.is_symlink():
            tmp_symlink.unlink()

        try:
            os.symlink(version, str(tmp_symlink))
            os.replace(str(tmp_symlink), str(prod_symlink))
        finally:
            if tmp_symlink.exists() or tmp_symlink.is_symlink():
                tmp_symlink.unlink()

    def list_versions(self, model_type: str) -> List[str]:  # noqa: UP006 - packet requires Python 3.8 syntax.
        """Return all versions for a model_type in chronological order."""
        model_type_dir = self._model_type_dir(model_type)
        if not model_type_dir.exists():
            return []

        versions = [
            entry.name
            for entry in model_type_dir.iterdir()
            if entry.is_dir() and not entry.is_symlink() and VERSION_RE.fullmatch(entry.name)
        ]
        return sorted(versions, key=lambda version: version[:18])

    def get_current(self, model_type: str) -> Optional[str]:  # noqa: UP045 - packet requires Python 3.8 syntax.
        """Return the version the production symlink currently points to, or None."""
        prod_symlink = self._model_type_dir(model_type) / PRODUCTION_LINK
        if not prod_symlink.is_symlink():
            return None

        return prod_symlink.resolve(strict=False).name

    def _model_type_dir(self, model_type: str) -> Path:
        if not model_type or os.sep in model_type or "\\" in model_type or model_type.startswith("."):
            raise ValueError(f"Invalid model_type: {model_type!r}")

        model_type_dir = self.root / model_type
        resolved_root = self.root.resolve(strict=False)
        resolved_model_type_dir = model_type_dir.resolve(strict=False)
        try:
            common_path = os.path.commonpath(
                [str(resolved_root), str(resolved_model_type_dir)]
            )
        except ValueError:
            raise ValueError(f"Invalid model_type: {model_type!r}") from None

        if common_path != str(resolved_root):
            raise ValueError(f"Invalid model_type: {model_type!r}")

        return model_type_dir

    def _write_model_artefact(
        self,
        model_artefact: Union[bytes, Path],  # noqa: UP007 - packet requires Python 3.8 syntax.
        model_path: Path,
    ) -> None:
        """Write bytes or copy a file/directory Path model artefact."""
        if isinstance(model_artefact, bytes):
            model_path.write_bytes(model_artefact)
            return

        if isinstance(model_artefact, Path):
            if model_artefact.is_dir():
                shutil.copytree(str(model_artefact), str(model_path))
                return

            shutil.copyfile(str(model_artefact), str(model_path))
            return

        raise TypeError("model_artefact must be bytes or pathlib.Path")

    def _write_metadata(
        self,
        metadata: Dict[str, object],  # noqa: UP006 - packet requires Python 3.8 syntax.
        version: str,
        saved_at: datetime,
        metadata_path: Path,
    ) -> None:
        metadata_payload = dict(metadata)
        metadata_payload["saved_at"] = saved_at.isoformat()
        metadata_payload["version"] = version

        with metadata_path.open("w", encoding="utf-8") as metadata_file:
            json.dump(metadata_payload, metadata_file, sort_keys=True)
            metadata_file.write("\n")
