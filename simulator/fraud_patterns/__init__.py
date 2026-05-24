"""Fraud-pattern dispatcher. Every sibling *.py module self-registers via @register.

The dispatcher auto-imports all sibling modules at package-import time, triggering
each module's @register decorator side-effect. Downstream P3-C..H packets drop in
new files without touching this dispatcher or any other pattern file - zero seam.
"""

from __future__ import annotations

import importlib
import pkgutil
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Tuple
from uuid import UUID


# Public types
@dataclass(frozen=True)
class GroundTruth:
    order_id: UUID
    is_fraud: bool
    fraud_category: str | None
    pattern_notes: str | None
    ring_id: UUID | None


PatternFn = Callable[..., Awaitable[Tuple["Order", GroundTruth]]]  # type: ignore[name-defined]  # noqa: F821, UP006

# Registry — modules append via @register at import time
_REGISTRY: dict[str, tuple[PatternFn, float]] = {}


def register(name: str, weight: float) -> Callable[[PatternFn], PatternFn]:
    def deco(fn: PatternFn) -> PatternFn:
        if name in _REGISTRY:
            raise ValueError(f"fraud pattern '{name}' already registered")
        _REGISTRY[name] = (fn, weight)
        return fn

    return deco


# Auto-discover: import every sibling module at package-import time so @register fires
for _info in pkgutil.iter_modules(__path__):
    if not _info.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_info.name}")


async def generate_fraud_order(*args: Any, **kwargs: Any) -> Any:
    """Pick a pattern by weighted random + invoke it. Spec § "Modified simulator/generator.py".

    Distribution per spec/PHASE_3.md § "The Seven Fraud Patterns" / "Distribution of fraud":
      stolen_card 30%, account_takeover 20%, promo_abuse 25%, refund_abuse 10%,
      collusive_merchant 5%, triangulation 5%, reseller 5%.

    Until P3-C..H land, only stolen_card is registered (weight 0.30); weights normalize
    automatically so a single-pattern dispatcher dispatches 100% to that pattern.
    """
    if not _REGISTRY:
        raise RuntimeError("no fraud patterns registered")

    items = list(_REGISTRY.items())
    weights = [w for _, (_fn, w) in items]
    _, (chosen_fn, _w) = random.choices(items, weights=weights, k=1)[0]
    return await chosen_fn(*args, **kwargs)
