from __future__ import annotations

import asyncio
import os
import random
import re
import uuid

import asyncpg
import pytest
import redis.asyncio as aioredis

from simulator.generator import (
    _read_runtime_rate,
    create_one_order,
    load_active_promos,
    load_config_from_env,
    load_stores_by_city,
    main,
)
from simulator.user_picker import WeightedUserPicker


DATABASE_URL_SIMULATOR = os.getenv(
    "DATABASE_URL_SIMULATOR",
    "postgresql://simulator_user:simulator_dev_password@postgres:5432/fraud_platform",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

ORDER_NUMBER_RE = re.compile(r"^JE-\d{4}-[A-Z2-7]{6}$")

_pool: asyncpg.Pool | None = None
_redis: aioredis.Redis | None = None


@pytest.fixture(scope="session")
def asyncpg_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = asyncio.run(asyncpg.create_pool(DATABASE_URL_SIMULATOR, min_size=2, max_size=10))
    return _pool


@pytest.fixture(scope="session")
def redis_client() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL)
    return _redis


class _FixedUserPicker:
    def __init__(self, user_id: uuid.UUID) -> None:
        self._user_id = user_id

    def pick(self, _rng: random.Random) -> uuid.UUID:
        return self._user_id


async def _pick_user(
    conn: asyncpg.Connection,
    *,
    require_no_prior_orders: bool,
) -> uuid.UUID:
    if require_no_prior_orders:
        row = await conn.fetchrow(
            """
            SELECT u.user_id
            FROM users u
            WHERE u.account_status = 'ACTIVE'
              AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.user_id)
              AND EXISTS (SELECT 1 FROM payment_methods pm WHERE pm.user_id = u.user_id)
            LIMIT 1
            """,
        )
        if row is not None:
            return row["user_id"]

    row = await conn.fetchrow(
        """
        SELECT u.user_id
        FROM users u
        WHERE u.account_status = 'ACTIVE'
          AND EXISTS (SELECT 1 FROM payment_methods pm WHERE pm.user_id = u.user_id)
        LIMIT 1
        """,
    )
    if row is not None:
        return row["user_id"]

    row = await conn.fetchrow(
        "SELECT u.user_id FROM users u WHERE u.account_status = 'ACTIVE' LIMIT 1",
    )
    if row is not None:
        return row["user_id"]

    raise RuntimeError("no eligible active user found for generator test")


async def _run_generator_batch(
    pool: asyncpg.Pool,
    redis: aioredis.Redis,
    *,
    sample_size: int,
    seed: int,
    fixed_user_id: uuid.UUID | None = None,
    require_no_prior_orders: bool = False,
) -> tuple[uuid.UUID, list[asyncpg.Record]]:
    stores_by_city = await load_stores_by_city(pool)
    promos = await load_active_promos(pool)

    async with pool.acquire() as conn:
        user_id = fixed_user_id or await _pick_user(
            conn,
            require_no_prior_orders=require_no_prior_orders,
        )
        before_order_count = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE user_id = $1",
            user_id,
        )

    if fixed_user_id is None:
        picker: WeightedUserPicker | _FixedUserPicker = WeightedUserPicker(
            pool,
            redis,
            refresh_every=300_000,
        )
        await picker.refresh()
    else:
        picker = _FixedUserPicker(user_id)

    rng = random.Random(seed)
    for _ in range(sample_size):
        await create_one_order(
            pool=pool,
            user_picker=picker,
            stores_by_city=stores_by_city,
            promos=promos,
            rng=rng,
            scoring_enabled=False,
        )

    async with pool.acquire() as conn:
        after_order_count = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE user_id = $1",
            user_id,
        )

    created_count = int(after_order_count - before_order_count)
    assert created_count == sample_size

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT order_id, order_number, order_type, promo_code,
                   is_new_payment_method, payment_method_id
            FROM orders
            WHERE user_id = $1
            ORDER BY placed_at DESC, order_id DESC
            LIMIT $2
            """,
            user_id,
            created_count,
        )

    if len(rows) < sample_size:
        raise AssertionError("insufficient orders generated")

    rows = list(reversed(rows))
    return user_id, rows


def test_generator_creates_valid_order(asyncpg_pool: asyncpg.Pool, redis_client: aioredis.Redis) -> None:
    async def _run() -> None:
        _, order_rows = await _run_generator_batch(
            asyncpg_pool,
            redis_client,
            sample_size=1,
            seed=42,
        )

        assert len(order_rows) == 1
        order_ids: list[uuid.UUID] = [row["order_id"] for row in order_rows]

        async with asyncpg_pool.acquire() as conn:
            order_item_count = await conn.fetchval(
                "SELECT COUNT(*) FROM order_items WHERE order_id = ANY($1::uuid[])",
                order_ids,
            )
            order_event_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM order_events
                WHERE order_id = ANY($1::uuid[]) AND event_type = 'ORDER_PLACED'
                """,
                order_ids,
            )
            gt_count = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM simulator_ground_truth
                WHERE order_id = ANY($1::uuid[]) AND is_fraud = FALSE
                """,
                order_ids,
            )

        assert order_item_count >= 1
        assert order_event_count == 1
        assert gt_count == 1

    asyncio.run(_run())


def test_generator_order_number_unique(asyncpg_pool: asyncpg.Pool, redis_client: aioredis.Redis) -> None:
    async def _run() -> None:
        _, order_rows = await _run_generator_batch(
            asyncpg_pool,
            redis_client,
            sample_size=100,
            seed=101,
        )

        order_numbers = [row["order_number"] for row in order_rows]
        assert len(order_numbers) == 100
        assert len(set(order_numbers)) == 100
        assert all(ORDER_NUMBER_RE.match(order_number) is not None for order_number in order_numbers)

    asyncio.run(_run())


def test_generator_order_type_distribution(asyncpg_pool: asyncpg.Pool, redis_client: aioredis.Redis) -> None:
    async def _run() -> None:
        _, order_rows = await _run_generator_batch(
            asyncpg_pool,
            redis_client,
            sample_size=100,
            seed=202,
        )

        order_types = [row["order_type"] for row in order_rows]
        delivery_count = order_types.count("DELIVERY")
        pickup_count = order_types.count("PICKUP")
        dine_in_count = order_types.count("DINE_IN")

        assert 65 <= delivery_count <= 85
        assert 10 <= pickup_count <= 30
        assert 0 <= dine_in_count <= 15
        assert delivery_count + pickup_count + dine_in_count == 100

    asyncio.run(_run())


def test_generator_payment_new_card_distribution(
    asyncpg_pool: asyncpg.Pool,
    redis_client: aioredis.Redis,
) -> None:
    async def _run() -> None:
        async with asyncpg_pool.acquire() as conn:
            user_id = await _pick_user(conn, require_no_prior_orders=False)
            payment_methods_before = await conn.fetchval(
                "SELECT COUNT(*) FROM payment_methods WHERE user_id = $1",
                user_id,
            )

        _, order_rows = await _run_generator_batch(
            asyncpg_pool,
            redis_client,
            sample_size=100,
            seed=303,
            fixed_user_id=user_id,
        )

        async with asyncpg_pool.acquire() as conn:
            payment_methods_after = await conn.fetchval(
                "SELECT COUNT(*) FROM payment_methods WHERE user_id = $1",
                user_id,
            )

        new_payment_methods = int(payment_methods_after - payment_methods_before)
        assert 0 <= new_payment_methods <= 15
        assert len(order_rows) == 100

    asyncio.run(_run())


def test_generator_promo_application(asyncpg_pool: asyncpg.Pool, redis_client: aioredis.Redis) -> None:
    async def _run() -> None:
        _, order_rows = await _run_generator_batch(
            asyncpg_pool,
            redis_client,
            sample_size=100,
            seed=404,
            require_no_prior_orders=True,
        )

        welcome_count = sum(1 for row in order_rows if row["promo_code"] == "WELCOME10")
        assert 0 < welcome_count <= 90

    asyncio.run(_run())


def test_generator_notify_fires(asyncpg_pool: asyncpg.Pool, redis_client: aioredis.Redis) -> None:
    async def _run() -> None:
        notifier = await asyncpg.connect(DATABASE_URL_SIMULATOR)
        stores_by_city = await load_stores_by_city(asyncpg_pool)
        promos = await load_active_promos(asyncpg_pool)

        async with asyncpg_pool.acquire() as conn:
            user_id = await _pick_user(conn, require_no_prior_orders=False)
        picker = _FixedUserPicker(user_id)

        event = asyncio.Event()
        received: list[str] = []

        def on_notify(_conn: asyncpg.Connection, _pid: int, _channel: str, payload: str) -> None:
            received.append(payload)
            event.set()

        await notifier.add_listener("order_placed", on_notify)

        rng = random.Random(505)
        try:
            await create_one_order(
                pool=asyncpg_pool,
                user_picker=picker,
                stores_by_city=stores_by_city,
                promos=promos,
                rng=rng,
                scoring_enabled=False,
            )
            await asyncio.wait_for(event.wait(), timeout=2.0)
        finally:
            await notifier.remove_listener("order_placed", on_notify)
            await notifier.close()

        assert received

        for payload in received:
            raw_payload = payload.decode() if isinstance(payload, bytes) else payload
            uuid.UUID(raw_payload)

    asyncio.run(_run())


def test_generator_rate_runtime_override() -> None:
    async def _run() -> None:
        redis_conn = aioredis.from_url(REDIS_URL)
        previous_rate = await redis_conn.get("simulator:rate_per_second")
        try:
            assert asyncio.iscoroutinefunction(main)
            config = load_config_from_env()
            await redis_conn.set("simulator:rate_per_second", "5")
            assert await _read_runtime_rate(redis_conn, fallback=1) == 5
            await redis_conn.delete("simulator:rate_per_second")
            assert await _read_runtime_rate(redis_conn, fallback=config.orders_per_second) == config.orders_per_second
        finally:
            if previous_rate is None:
                await redis_conn.delete("simulator:rate_per_second")
            else:
                await redis_conn.set("simulator:rate_per_second", previous_rate)
            await redis_conn.close()

    asyncio.run(_run())
