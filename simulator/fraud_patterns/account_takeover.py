"""Account-takeover fraud pattern for Phase 2-C simulator behavior."""

from __future__ import annotations

import math
import random
import sys
import uuid
from typing import Any

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo

from simulator.fraud_patterns import GroundTruth, register
from simulator.fraud_patterns._entity_factory import create_fresh_device
from simulator.fraud_patterns.stolen_card import FraudPatternContext

LONDON_TZ = ZoneInfo("Europe/London")

_IP_COUNTRY_CATEGORIES = [
    ("NG", 0.15),
    ("RU", 0.12),
    ("CN", 0.10),
    ("US", 0.10),
    ("VN", 0.08),
    ("PK", 0.08),
    ("UA", 0.07),
    ("GB_different_city", 0.20),
    ("other", 0.10),
]

_IP_COUNTRY_RESOLUTION: dict[str, str | None] = {
    "NG": "NG",
    "RU": "RU",
    "CN": "CN",
    "US": "US",
    "VN": "VN",
    "PK": "PK",
    "UA": "UA",
    "GB_different_city": "GB",
    "other": None,  # resolved at runtime from _OTHER_ISO2_POOL
}

_OTHER_ISO2_POOL: list[str] = [
    "BR",
    "IN",
    "EG",
    "TR",
    "TH",
    "ID",
    "PH",
    "BD",
    "MX",
    "CO",
    "AR",
    "PE",
    "VE",
    "KE",
    "GH",
    "ZA",
]


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

    if ctx.conn is None:
        device_id = uuid.UUID(int=ctx.rng.getrandbits(128))
        device_platform = ctx.rng.choice(["iOS", "Android", "Web"])
    else:
        device_platform = ctx.rng.choice(["iOS", "Android", "Web"])
        device_id = await create_fresh_device(
            ctx.rng,
            ctx.conn,
            platform_bias=device_platform,
        )

    ip_country_category = _weighted_choice(ctx.rng, _IP_COUNTRY_CATEGORIES)
    _iso2_raw = _IP_COUNTRY_RESOLUTION[ip_country_category]
    ip_country_iso2: str = _iso2_raw if _iso2_raw is not None else ctx.rng.choice(_OTHER_ISO2_POOL)

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

    if ctx.now.tzinfo is None:
        raise ValueError(
            "FraudPatternContext.now must be timezone-aware; "
            "got a naive datetime which would be treated as host-local time"
        )
    _now_london = ctx.now.astimezone(LONDON_TZ)

    may_collide = ctx.rng.random() < 0.30

    order_dict: dict[str, Any] = {
        "order_id": uuid.UUID(int=ctx.rng.getrandbits(128)),
        "victim_user_id": victim_user_id,
        "device_id": device_id,
        "device_platform": device_platform,
        "ip_country": ip_country_iso2,
        "payment_method_id": payment_method_id,
        "is_new_payment_method": is_new_payment_method,
        "delivery_address_id": delivery_address_id,
        "is_new_delivery_address": is_new_delivery_address,
        "order_total_pence": order_total_pence,
        "order_value_mode": order_value_mode,
        "placed_at": _now_london,
    }

    # Phase 2-C resolves victim_user_id via DB filter; this packet stamps the filter criteria into pattern_notes
    pattern_notes = f"victim_user_id={victim_user_id}, new_device, ip_country={ip_country_category}"
    if may_collide:
        pattern_notes += ", may_collide_with_real_order=true"
    pattern_notes += ', _target_user_filter={"risk_tier": "TRUSTED", "min_orders_lifetime": 10}'

    gt = GroundTruth(
        order_id=order_dict["order_id"],
        is_fraud=True,
        fraud_category="account_takeover",
        pattern_notes=pattern_notes,
        ring_id=None,
    )

    return order_dict, gt
