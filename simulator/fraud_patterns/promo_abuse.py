"""Promo-abuse fraud pattern for Phase 3 simulator behavior."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from uuid import UUID

from simulator.fraud_patterns import GroundTruth, register
from simulator.fraud_patterns.stolen_card import FraudPatternContext


class _Welcome10(NamedTuple):
    min_order_pence: int
    discount_pence: int


WELCOME10 = _Welcome10(min_order_pence=2000, discount_pence=200)
PROMO_ABUSE_RINGS: list[PromoAbuseRing] = []


@dataclass
class PromoAbuseRing:
    ring_id: UUID
    device_id: UUID
    base_address: dict[str, Any]
    payment_pool: list[dict[str, str]]
    email_pattern: str
    created_users: list[UUID] = field(default_factory=list)
    base_ip_prefix: str = "192.168.1"

    def generate_next_order(self, rng: random.Random) -> tuple[dict[str, Any], UUID]:
        user_id, email = _select_or_create_user(self, rng)
        payment = _rotate_payment(self)
        delivery_jitter = _delivery_jitter(rng)
        order_dict: dict[str, Any] = {
            "order_id": _uuid_from_rng(rng),
            "ring_id": self.ring_id,
            "device_id": self.device_id,
            "user_id": user_id,
            "email": email,
            "ip_address": _next_ip_address(self.base_ip_prefix, rng),
            "payment": payment,
            "delivery_lat": self.base_address["lat"] + delivery_jitter[0],
            "delivery_lon": self.base_address["lon"] + delivery_jitter[1],
            "promo": "WELCOME10",
            "order_total_pence": int(WELCOME10.min_order_pence + rng.uniform(0, 500)),
        }
        return order_dict, user_id


def _uuid_from_rng(rng: random.Random) -> UUID:
    return UUID(int=rng.getrandbits(128))


def _create_base_address(rng: random.Random) -> dict[str, Any]:
    return {
        "lat": rng.uniform(51.45, 51.55),
        "lon": rng.uniform(-0.20, 0.05),
        "postcode": f"W1{rng.randint(1, 99):02d} 0{rng.randint(1, 9)}{rng.randint(1, 9)}",
        "city": "London",
    }


def _create_prepaid_pool(rng: random.Random) -> list[dict[str, str]]:
    cards = rng.randint(3, 8)
    return [_create_prepaid_card(rng) for _ in range(cards)]


def _create_prepaid_card(rng: random.Random) -> dict[str, str]:
    return {
        "card_bin": f"{rng.randint(100000, 999999):06d}",
        "last4": f"{rng.randint(0, 9999):04d}",
        "funding": "PREPAID",
    }


def _create_ring(rng: random.Random, ring_index: int) -> PromoAbuseRing:
    return PromoAbuseRing(
        ring_id=_uuid_from_rng(rng),
        device_id=_uuid_from_rng(rng),
        base_address=_create_base_address(rng),
        payment_pool=_create_prepaid_pool(rng),
        email_pattern=f"ringfraud_{ring_index}{{n}}@mailinator.com",
        base_ip_prefix=f"192.168.{rng.randint(0, 254)}",
    )


def init_rings(rng: random.Random, n_rings: int = 50) -> None:
    PROMO_ABUSE_RINGS.clear()
    for ring_index in range(n_rings):
        PROMO_ABUSE_RINGS.append(_create_ring(rng, ring_index))


def _delivery_jitter(rng: random.Random) -> tuple[float, float]:
    max_delta = 0.0045
    return rng.uniform(-max_delta, max_delta), rng.uniform(-max_delta, max_delta)


def _next_ip_address(base_ip_prefix: str, rng: random.Random) -> str:
    return f"{base_ip_prefix}.{rng.randint(1, 254)}"


def _rotate_payment(ring: PromoAbuseRing) -> dict[str, str]:
    payment = ring.payment_pool.pop(0)
    ring.payment_pool.append(payment)
    return payment


def _select_or_create_user(
    ring: PromoAbuseRing,
    rng: random.Random,
) -> tuple[UUID, str]:
    if len(ring.created_users) < 30 and (not ring.created_users or rng.random() < 0.7):
        user_id = _uuid_from_rng(rng)
        ring.created_users.append(user_id)
        user_index = len(ring.created_users)
    else:
        user_id = rng.choice(ring.created_users)
        user_index = ring.created_users.index(user_id) + 1

    email = ring.email_pattern.format(n=user_index)
    return user_id, email


@register("promo_abuse", 0.25)
async def generate_promo_abuse_fraud(
    ctx: FraudPatternContext,
) -> tuple[dict[str, Any], GroundTruth]:
    if not PROMO_ABUSE_RINGS:
        # P3-B no-implicit-defaults convention: callers (simulator startup and tests)
        # must call init_rings(rng) explicitly; there is no safe implicit fallback.
        raise RuntimeError(
            "PROMO_ABUSE_RINGS is empty — call init_rings(rng) at simulator startup"
        )

    ring = ctx.rng.choice(PROMO_ABUSE_RINGS)
    order_dict, _ = ring.generate_next_order(ctx.rng)

    return order_dict, GroundTruth(
        order_id=order_dict["order_id"],
        is_fraud=True,
        fraud_category="promo_abuse",
        pattern_notes=f"ring_id={ring.ring_id}",
        ring_id=ring.ring_id,
    )
