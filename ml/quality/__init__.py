"""Distribution quality profiling for realism-testbed training parquet files.

The package exposes a CLI/API scanner that compares training-data distributions
against sourced YAML expectations, highlights narrow numeric distributions,
detects label-leak sentinel string values, and reports constant columns.

The VOI-269 spec suggested Deepchecks or Evidently for this quality layer, but
both packages break the numpy<1.19 pin required by TensorFlow 2.3 in this
project. This module therefore uses scipy and pandas only.
"""

from __future__ import annotations

from typing import Any

__all__ = ["run_scanner"]


def __getattr__(name: str) -> Any:
    if name == "run_scanner":
        from ml.quality.scanner import run_scanner

        return run_scanner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
