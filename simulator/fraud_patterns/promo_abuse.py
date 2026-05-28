"""Promo-abuse fraud pattern for Phase 3 simulator behavior."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any, NamedTuple
from uuid import UUID

import asyncpg

from simulator.fraud_patterns import GroundTruth, register
from simulator.fraud_patterns.stolen_card import FraudPatternContext


class _Welcome10(NamedTuple):
    min_order_pence: int
    discount_pence: int


WELCOME10 = _Welcome10(min_order_pence=2000, discount_pence=200)
_PROMO_RING_BOOTSTRAP_SEED: int = 0xF00D9A5E
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


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _ring_from_row(row: asyncpg.Record) -> PromoAbuseRing:
    base_address = _json_value(row["base_address"])
    payment_pool = _json_value(row["payment_pool"])
    created_user_ids = row["created_user_ids"] or []
    return PromoAbuseRing(
        ring_id=UUID(str(row["ring_id"])),
        device_id=UUID(str(row["device_id"])),
        base_address=dict(base_address),
        payment_pool=[dict(payment) for payment in payment_pool],
        email_pattern=row["email_pattern"],
        created_users=[UUID(str(user_id)) for user_id in created_user_ids],
        base_ip_prefix=row["base_ip_prefix"],
    )


async def _load_rings_from_db(conn: asyncpg.Connection) -> list[PromoAbuseRing]:
    rows = await conn.fetch(
        """
        SELECT
            ring_id,
            device_id,
            base_address,
            payment_pool,
            email_pattern,
            created_user_ids,
            base_ip_prefix
        FROM sim.fraud_promo_rings
        ORDER BY created_at, ring_id
        """
    )
    return [_ring_from_row(row) for row in rows]


def _populate_rings(rings: list[PromoAbuseRing], n_rings: int) -> None:
    PROMO_ABUSE_RINGS.clear()
    PROMO_ABUSE_RINGS.extend(rings[:n_rings])


async def _insert_ring(conn: asyncpg.Connection, ring: PromoAbuseRing) -> None:
    await conn.execute(
        """
        INSERT INTO sim.fraud_promo_rings
            (
                ring_id,
                device_id,
                base_address,
                payment_pool,
                email_pattern,
                created_user_ids,
                base_ip_prefix
            )
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (ring_id) DO NOTHING
        """,
        ring.ring_id,
        ring.device_id,
        json.dumps(ring.base_address),
        json.dumps(ring.payment_pool),
        ring.email_pattern,
        ring.created_users,
        ring.base_ip_prefix,
    )


async def init_rings_from_db(
    rng: random.Random,
    conn: asyncpg.Connection,
    n_rings: int = 50,
) -> None:
    """Load promo-abuse rings from DB; top up if fewer than n_rings exist."""
    existing_rings = await _load_rings_from_db(conn)
    if len(existing_rings) >= n_rings:
        _populate_rings(existing_rings, n_rings)
        return

    ring_rng = random.Random(_PROMO_RING_BOOTSTRAP_SEED)
    existing_ids = {ring.ring_id for ring in existing_rings}
    ring_index = 0
    while len(existing_ids) < n_rings:
        ring = _create_ring(ring_rng, ring_index)
        ring_index += 1
        if ring.ring_id not in existing_ids:
            await _insert_ring(conn, ring)
            existing_ids.add(ring.ring_id)

    _populate_rings(await _load_rings_from_db(conn), n_rings)


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


async def _persist_created_users(
    conn: asyncpg.Connection,
    ring: PromoAbuseRing,
) -> None:
    status = await conn.execute(
        """
        UPDATE sim.fraud_promo_rings
        SET created_user_ids = $1
        WHERE ring_id = $2
        """,
        ring.created_users,
        ring.ring_id,
    )
    if status == "UPDATE 0":
        raise RuntimeError(f"promo-abuse ring not found in DB: {ring.ring_id}")


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
    created_users_before = len(ring.created_users)
    order_dict, _ = ring.generate_next_order(ctx.rng)
    if ctx.conn is not None and len(ring.created_users) > created_users_before:
        await _persist_created_users(ctx.conn, ring)

    return order_dict, GroundTruth(
        order_id=order_dict["order_id"],
        is_fraud=True,
        fraud_category="promo_abuse",
        pattern_notes=f"ring_id={ring.ring_id}",
        ring_id=ring.ring_id,
    )
