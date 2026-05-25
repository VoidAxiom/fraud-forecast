"""Account-takeover fraud pattern for Phase 2-C simulator behavior."""

from __future__ import annotations

import math
import random
import uuid
from typing import Any

from simulator.fraud_patterns import GroundTruth, register
from simulator.fraud_patterns.stolen_card import FraudPatternContext


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


@register("account_takeover", 0.20)
async def generate_account_takeover_fraud(
    ctx: FraudPatternContext,
) -> tuple[dict[str, Any], GroundTruth]:
    victim_user_id = uuid.UUID(int=ctx.rng.getrandbits(128))

    device_id = uuid.UUID(int=ctx.rng.getrandbits(128))
    device_platform = ctx.rng.choice(["iOS", "Android", "Web"])

    ip_country = _weighted_choice(
        ctx.rng,
        [
            ("NG", 0.15),
            ("RU", 0.12),
            ("CN", 0.10),
            ("US", 0.10),
            ("VN", 0.08),
            ("PK", 0.08),
            ("UA", 0.07),
            ("GB_different_city", 0.20),
            ("other", 0.10),
        ],
    )

    if ctx.rng.random() < 0.30:
        payment_method_id: str | uuid.UUID = uuid.UUID(int=ctx.rng.getrandbits(128))
        is_new_payment_method = True
    else:
        payment_method_id = "VICTIM_SAVED"
        is_new_payment_method = False

    if ctx.rng.random() < 0.90:
        delivery_address_id: str | uuid.UUID = uuid.UUID(int=ctx.rng.getrandbits(128))
        is_new_delivery_address = True
    else:
        delivery_address_id = "VICTIM_SAVED"
        is_new_delivery_address = False

    if ctx.rng.random() < 0.50:
        target_mean = 2500
        sigma = 0.4
        order_value_mode = "normal"
    else:
        target_mean = 8000
        sigma = 0.5
        order_value_mode = "high_value"

    mu = math.log(target_mean) - (sigma**2) / 2
    raw = math.exp(ctx.rng.gauss(mu, sigma))
    order_total_pence = max(1, int(raw))

    may_collide = ctx.rng.random() < 0.30

    order_dict: dict[str, Any] = {
        "order_id": uuid.UUID(int=ctx.rng.getrandbits(128)),
        "victim_user_id": victim_user_id,
        "device_id": device_id,
        "device_platform": device_platform,
        "ip_country": ip_country,
        "payment_method_id": payment_method_id,
        "is_new_payment_method": is_new_payment_method,
        "delivery_address_id": delivery_address_id,
        "is_new_delivery_address": is_new_delivery_address,
        "order_total_pence": order_total_pence,
        "order_value_mode": order_value_mode,
        "placed_at": ctx.now,
    }

    # Phase 2-C resolves victim_user_id via DB filter; this packet stamps the filter criteria into pattern_notes
    pattern_notes = f"victim_user_id={victim_user_id}, new_device, ip_country={ip_country}"
    if may_collide:
        pattern_notes += ", may_collide_with_real_order=true"
    pattern_notes += ", _target_user_filter={\"risk_tier\": \"TRUSTED\", \"min_orders_lifetime\": 10}"

    gt = GroundTruth(
        order_id=order_dict["order_id"],
        is_fraud=True,
        fraud_category="account_takeover",
        pattern_notes=pattern_notes,
        ring_id=None,
    )

    return order_dict, gt
