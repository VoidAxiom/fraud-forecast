"""Refund-abuse fraud pattern for Phase 3 simulator behavior."""

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
from simulator.fraud_patterns.stolen_card import FraudPatternContext

LONDON_TZ = ZoneInfo("Europe/London")


def _lognormal_meaned_int(
    rng: random.Random,
    mean_pence: int,
    sigma: float,
) -> int:
    mu = math.log(mean_pence) - 0.5 * sigma**2
    return max(1000, int(math.exp(rng.gauss(mu, sigma))))


@register("refund_abuse", 0.10)
async def generate_refund_abuse_fraud(
    ctx: FraudPatternContext,
) -> tuple[dict[str, Any], GroundTruth]:
    uid = uuid.UUID(int=ctx.rng.getrandbits(128))
    order_id = uuid.UUID(int=ctx.rng.getrandbits(128))
    if ctx.now.tzinfo is None:
        raise ValueError(
            "FraudPatternContext.now must be timezone-aware; "
            "got a naive datetime which would be treated as host-local time"
        )

    pattern_notes = f"refund_abuser_uid={uid}, _refund_abuser_filter=refunds_lifetime__gte_3"

    order_total_pence: int = _lognormal_meaned_int(
        rng=ctx.rng,
        mean_pence=3500,
        sigma=0.4,
    )

    order_dict: dict[str, Any] = {
        "order_id": order_id,
        "user_id": uid,
        "order_total_pence": order_total_pence,
        "payment_method_id": "ABUSER_SAVED",
        "delivery_address_id": "ABUSER_SAVED",
        "is_new_device": False,
        "ip_type": "uk",
        "avs_result": "MATCH",
        "cvv_result": "MATCH",
        "card_country": "GB",
        "card_funding_type": "DEBIT",
        "is_high_end_cart": False,
    }

    return order_dict, GroundTruth(
        order_id=order_id,
        is_fraud=True,
        fraud_category="refund_abuse",
        pattern_notes=pattern_notes,
        ring_id=None,
    )
