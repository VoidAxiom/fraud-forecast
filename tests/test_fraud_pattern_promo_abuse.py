"""Tests for the promo-abuse fraud simulator pattern."""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from datetime import datetime
from typing import Any
from uuid import UUID

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo

import asyncpg

from simulator.fraud_patterns import GroundTruth
from simulator.fraud_patterns.promo_abuse import (
    PROMO_ABUSE_RINGS,
    PromoAbuseRing,
    WELCOME10,
    generate_promo_abuse_fraud,
    init_rings,
    init_rings_from_db,
)
from simulator.fraud_patterns.stolen_card import FraudPatternContext

LONDON_TZ_TEST: ZoneInfo = ZoneInfo("Europe/London")
DATABASE_URL_SIMULATOR: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://simulator_user:simulator_dev_password@postgres:5432/fraud_platform",
)


def test_promo_abuse_ring_init() -> None:
    init_rng: random.Random = random.Random(42)
    init_rings(rng=init_rng, n_rings=50)

    ring_count: int = len(PROMO_ABUSE_RINGS)
    assert ring_count == 50

    ring_ids: set[UUID] = {ring.ring_id for ring in PROMO_ABUSE_RINGS}
    assert len(ring_ids) == ring_count

    payment_sizes: list[int] = [len(ring.payment_pool) for ring in PROMO_ABUSE_RINGS]
    assert all(3 <= size <= 8 for size in payment_sizes)

    email_patterns: list[str] = [ring.email_pattern for ring in PROMO_ABUSE_RINGS]
    assert all(len(pattern) > 0 for pattern in email_patterns)


def test_init_rings_from_db_idempotent() -> None:
    async def _run() -> None:
        conn = await asyncpg.connect(DATABASE_URL_SIMULATOR)
        before_ids: set[UUID] = set()
        have_before_ids = False
        try:
            before_ids = {
                UUID(str(row["ring_id"]))
                for row in await conn.fetch("SELECT ring_id FROM sim.fraud_promo_rings")
            }
            have_before_ids = True

            first_rng = random.Random(101)
            first_rng_state = first_rng.getstate()
            await init_rings_from_db(first_rng, conn, n_rings=10)
            assert first_rng.getstate() == first_rng_state
            first_ring_ids: list[UUID] = [ring.ring_id for ring in PROMO_ABUSE_RINGS]
            assert len(first_ring_ids) == 10

            second_rng = random.Random(202)
            second_rng_state = second_rng.getstate()
            await init_rings_from_db(second_rng, conn, n_rings=10)
            assert second_rng.getstate() == second_rng_state
            second_ring_ids: list[UUID] = [ring.ring_id for ring in PROMO_ABUSE_RINGS]
            assert len(second_ring_ids) == 10
            assert second_ring_ids == first_ring_ids
        finally:
            try:
                if have_before_ids:
                    current_ids = {
                        UUID(str(row["ring_id"]))
                        for row in await conn.fetch("SELECT ring_id FROM sim.fraud_promo_rings")
                    }
                    created_ring_ids = sorted(current_ids - before_ids, key=str)
                    if created_ring_ids:
                        await conn.execute(
                            "DELETE FROM sim.fraud_promo_rings WHERE ring_id = ANY($1::uuid[])",
                            created_ring_ids,
                        )
            finally:
                await conn.close()

    asyncio.run(_run())


def test_init_rings_from_db_loads_from_db() -> None:
    async def _run() -> None:
        conn = await asyncpg.connect(DATABASE_URL_SIMULATOR)
        inserted_ring_ids: list[UUID] = [
            UUID("00000000-0000-0000-0000-000000000101"),
            UUID("00000000-0000-0000-0000-000000000102"),
            UUID("00000000-0000-0000-0000-000000000103"),
        ]
        try:
            await conn.execute(
                "DELETE FROM sim.fraud_promo_rings WHERE ring_id = ANY($1::uuid[])",
                inserted_ring_ids,
            )
            for index, ring_id in enumerate(inserted_ring_ids):
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
                            base_ip_prefix,
                            created_at
                        )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, TIMESTAMPTZ '1900-01-01')
                    """,
                    ring_id,
                    UUID(f"00000000-0000-0000-0000-00000000020{index}"),
                    json.dumps(
                        {
                            "lat": 51.50 + (index * 0.01),
                            "lon": -0.10,
                            "postcode": f"W1{index:02d} 0AA",
                            "city": "London",
                        }
                    ),
                    json.dumps(
                        [
                            {
                                "card_bin": f"40000{index}",
                                "last4": f"100{index}",
                                "funding": "PREPAID",
                            }
                        ]
                    ),
                    f"known_ring_{index}{{n}}@mailinator.com",
                    [],
                    f"192.168.{index}",
                )

            await init_rings_from_db(random.Random(303), conn, n_rings=len(inserted_ring_ids))

            loaded_ring_ids: set[UUID] = {ring.ring_id for ring in PROMO_ABUSE_RINGS}
            assert set(inserted_ring_ids).issubset(loaded_ring_ids)
        finally:
            try:
                await conn.execute(
                    "DELETE FROM sim.fraud_promo_rings WHERE ring_id = ANY($1::uuid[])",
                    inserted_ring_ids,
                )
            finally:
                await conn.close()

    asyncio.run(_run())


def test_init_rings_from_db_member_user_ids_resolvable() -> None:
    init_rings(random.Random(42), n_rings=10)
    assert all(isinstance(ring.created_users, list) for ring in PROMO_ABUSE_RINGS)


def test_promo_abuse_pattern_returns_ground_truth() -> None:
    init_rng: random.Random = random.Random(42)
    init_rings(rng=init_rng)

    ctx: FraudPatternContext = FraudPatternContext(
        now=datetime(2024, 5, 24, 12, 0, tzinfo=LONDON_TZ_TEST),
        rng=random.Random(99),
    )
    order_dict, gt = asyncio.run(generate_promo_abuse_fraud(ctx))

    assert isinstance(order_dict, dict)
    assert isinstance(gt, GroundTruth)
    assert gt.is_fraud is True
    assert gt.fraud_category == "promo_abuse"
    assert gt.ring_id is not None
    assert isinstance(gt.ring_id, UUID)


def test_promo_abuse_ring_consistency() -> None:
    init_rng: random.Random = random.Random(42)
    init_rings(rng=init_rng)
    fixed_now: datetime = datetime(2024, 5, 24, 12, 0, tzinfo=LONDON_TZ_TEST)
    shared_rng: random.Random = random.Random(7)

    orders_by_ring: dict[UUID, list[dict[str, Any]]] = {}
    for _ in range(100):
        ctx: FraudPatternContext = FraudPatternContext(now=fixed_now, rng=shared_rng)
        order_dict, gt = asyncio.run(generate_promo_abuse_fraud(ctx))
        ring_id: UUID | None = gt.ring_id
        assert ring_id is not None
        existing_orders: list[dict[str, Any]] | None = orders_by_ring.get(ring_id)
        if existing_orders is None:
            orders_by_ring[ring_id] = [order_dict]
        else:
            existing_orders.append(order_dict)

    rings_by_id: dict[UUID, PromoAbuseRing] = {
        ring.ring_id: ring for ring in PROMO_ABUSE_RINGS
    }

    for ring_id, orders in orders_by_ring.items():
        if len(orders) < 2:
            continue

        ring: PromoAbuseRing = rings_by_id[ring_id]
        device_ids: set[UUID] = {order["device_id"] for order in orders}
        assert len(device_ids) == 1
        assert all(order["payment"] in ring.payment_pool for order in orders)

        base_lat: float = ring.base_address["lat"]
        base_lon: float = ring.base_address["lon"]
        for order in orders:
            delivery_lat: float = order["delivery_lat"]
            delivery_lon: float = order["delivery_lon"]
            assert abs(delivery_lat - base_lat) <= 0.0045
            assert abs(delivery_lon - base_lon) <= 0.0045


def test_promo_abuse_user_reuse_distribution() -> None:
    init_rng: random.Random = random.Random(42)
    init_rings(rng=init_rng)
    ring: PromoAbuseRing = PROMO_ABUSE_RINGS[0]

    rng: random.Random = random.Random(5)
    new_user_generations: int = 0
    for _ in range(200):
        users_before: int = len(ring.created_users)
        _, user_id = ring.generate_next_order(rng)
        if len(ring.created_users) > users_before:
            new_user_generations += 1
            assert len(ring.created_users) == users_before + 1
        else:
            assert user_id in ring.created_users
        if users_before >= 30:
            assert user_id in ring.created_users

    assert len(ring.created_users) >= 20
    assert len(ring.created_users) <= 30
    assert new_user_generations >= 20


def test_promo_abuse_uses_welcome_promo() -> None:
    init_rng: random.Random = random.Random(42)
    init_rings(rng=init_rng)

    ctx: FraudPatternContext = FraudPatternContext(
        now=datetime(2026, 1, 1, 12, 0, tzinfo=LONDON_TZ_TEST),
        rng=random.Random(77),
    )

    for _ in range(50):
        order_dict, _gt = asyncio.run(generate_promo_abuse_fraud(ctx))
        assert order_dict["promo"] == "WELCOME10"


def test_promo_abuse_order_just_above_min_order() -> None:
    init_rng: random.Random = random.Random(42)
    init_rings(rng=init_rng)

    ctx: FraudPatternContext = FraudPatternContext(
        now=datetime(2026, 1, 2, 12, 0, tzinfo=LONDON_TZ_TEST),
        rng=random.Random(33),
    )

    min_order: int = WELCOME10.min_order_pence
    max_order: int = WELCOME10.min_order_pence + 500

    for _ in range(100):
        order_dict, _gt = asyncio.run(generate_promo_abuse_fraud(ctx))
        order_total: int = order_dict["order_total_pence"]
        assert min_order <= order_total <= max_order
        assert order_total >= 2000
