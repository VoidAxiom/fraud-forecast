from __future__ import annotations

from typing import Dict, List, Literal, TypedDict

# The worker packet requires Python 3.8-compatible typing names here.
# ruff: noqa: UP006

HypothesisStatus = Literal["implemented", "not_yet_implemented"]


class HypothesisSpec(TypedDict):
    description: str
    status: HypothesisStatus
    feature_spec_removals: Dict[str, List[str]]


HYPOTHESES: Dict[str, HypothesisSpec] = {
    "h1-no-store-city": {
        "description": "Remove store_city from high-cardinality hashed features.",
        "status": "implemented",
        "feature_spec_removals": {"HIGH_CARD_HASH": ["store_city"]},
    },
    "h2-no-cancellation-reason": {
        "description": "Remove cancellation_reason from low-cardinality categorical features.",
        "status": "implemented",
        "feature_spec_removals": {"LOW_CARD_CATEGORICAL": ["cancellation_reason"]},
    },
    "h3-no-lifetime-cb-rate": {
        "description": "Remove user_lifetime_chargeback_rate from numerical features.",
        "status": "implemented",
        "feature_spec_removals": {
            "NUMERICAL_FEATURES": ["user_lifetime_chargeback_rate"],
        },
    },
    "h4-realistic-delivery-timing": {
        "description": "Not implemented: lifecycle-side simulator change required.",
        "status": "not_yet_implemented",
        "feature_spec_removals": {},
    },
    "h5-stochastic-patterns": {
        "description": "Not implemented: complex simulator change required.",
        "status": "not_yet_implemented",
        "feature_spec_removals": {},
    },
}


__all__ = ["HYPOTHESES", "HypothesisSpec", "HypothesisStatus"]
