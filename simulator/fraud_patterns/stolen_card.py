"""Stolen-card fraud pattern for Phase 3 simulator behavior."""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
import uuid

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo

from simulator.fraud_patterns import GroundTruth, register

if TYPE_CHECKING:
    from simulator.models import Order as _Order  # type: ignore[import]  # noqa: F401


@dataclass
class FraudPatternContext:
    rng: random.Random = field(default_factory=random.Random)
    now: datetime = field(default_factory=lambda: datetime.now(tz=ZoneInfo("Europe/London")))


def _weighted_choice(rng: random.Random, choices: list[tuple[str, float]]) -> str:
    total_weight = sum(weight for _value, weight in choices)
    if total_weight <= 0:
        raise ValueError("choice weights must be positive")

    normalized = [(value, weight / total_weight) for value, weight in choices]
    draw = rng.random()
    cumulative = 0.0
    for value, weight in normalized:
        cumulative += weight
        if draw < cumulative:
            return value

    return normalized[-1][0]


@register("stolen_card", 0.30)
async def generate_stolen_card_fraud(
    ctx: FraudPatternContext | None = None,
) -> tuple[dict[str, Any], GroundTruth]:
    if ctx is None:
        ctx = FraudPatternContext()

    variant_roll = ctx.rng.random()
    if variant_roll < 0.60:
        variant = "A"
        ctx.rng.uniform(1, 48)
    elif variant_roll < 0.90:
        variant = "B"
        ctx.rng.randint(0, 2)
    else:
        variant = "C"
        ctx.rng.randint(10, 30)

    if variant == "C":
        card_country = _weighted_choice(
            ctx.rng,
            [("US", 0.20), ("RU", 0.10), ("NG", 0.08), ("IN", 0.07), ("CN", 0.05), ("foreign_other", 0.15)],
        )
    else:
        card_country = _weighted_choice(
            ctx.rng,
            [("GB", 0.35), ("US", 0.20), ("RU", 0.10), ("NG", 0.08), ("IN", 0.07), ("CN", 0.05), ("foreign_other", 0.15)],
        )

    card_funding_type = _weighted_choice(
        ctx.rng,
        [("CREDIT", 0.6), ("PREPAID", 0.3), ("DEBIT", 0.1)],
    )

    avs_result = "NO_MATCH" if ctx.rng.random() < 0.65 else "MATCH"
    cvv_result = "NO_MATCH" if ctx.rng.random() < 0.40 else "MATCH"

    order_total = ctx.rng.gauss(6500, 2500)
    order_total_pence = max(2000, int(order_total))

    address_type = _weighted_choice(
        ctx.rng,
        [("HOTEL", 0.30), ("COMMERCIAL", 0.20), ("RESIDENTIAL_NEW", 0.30), ("RESIDENTIAL_USER", 0.20)],
    )
    is_new_device = ctx.rng.random() < 0.80
    ip_type = _weighted_choice(
        ctx.rng,
        [("uk", 0.60), ("vpn", 0.25), ("foreign", 0.15)],
    )
    is_high_end_cart = ctx.rng.random() < 0.60

    order_dict: dict[str, Any] = {
        "order_total_pence": order_total_pence,
        "card_country": card_country,
        "card_funding_type": card_funding_type,
        "avs_result": avs_result,
        "cvv_result": cvv_result,
        "address_type": address_type,
        "is_new_device": is_new_device,
        "ip_type": ip_type,
        "is_high_end_cart": is_high_end_cart,
        "variant": variant,
        "is_digital_native_bank": False,
        "is_night_order": 2 <= ctx.now.hour < 6,
    }

    # Spec says 40% of stolen-card fraud is night-hour oriented; record that signal.
    pattern_notes = f"variant={variant}, avs={avs_result}"
    gt = GroundTruth(
        order_id=uuid.UUID(int=ctx.rng.getrandbits(128)),
        is_fraud=True,
        fraud_category="stolen_card",
        pattern_notes=pattern_notes,
        ring_id=None,
    )

    return order_dict, gt
