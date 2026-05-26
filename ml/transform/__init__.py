from __future__ import annotations

from typing import Any

__all__ = ["preprocessing_fn", "run_pipeline"]


def __getattr__(name: str) -> Any:
    if name == "preprocessing_fn":
        from ml.transform.preprocessing import preprocessing_fn

        return preprocessing_fn
    if name == "run_pipeline":
        from ml.transform.run_transform import run_pipeline

        return run_pipeline
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
