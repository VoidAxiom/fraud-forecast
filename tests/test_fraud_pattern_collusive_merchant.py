"""Tests for the collusive-merchant fraud simulator pattern."""

from __future__ import annotations

import asyncio
import math
import os
import random
import sys
import uuid
from datetime import datetime
from typing import Any, AsyncIterator
from uuid import UUID

import asyncpg
import pytest

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo

from simulator.fraud_patterns import GroundTruth, _REGISTRY
from simulator.fraud_patterns.collusive_merchant import (
    COLLUSIVE_STORES,
    generate_collusive_merchant_fraud,
    init_collusive_stores,
)
from simulator.fraud_patterns.stolen_card import FraudPatternContext


DATABASE_URL_SIMULATOR = os.getenv(
    "DATABASE_URL_SIMULATOR",
    "postgresql://simulator_user:simulator_dev_password@postgres:5432/fraud_platform",
)
LONDON_TZ_TEST = ZoneInfo("Europe/London")


class _FakeConn:
    def __init__(self, rows: list[Any] = []) -> None:  # noqa: B006
        self._rows = list(rows)

    async def fetch(self, query: str) -> list[Any]:
        _ = query
        return self._rows

    async def executemany(self, query: str, args: list[tuple[UUID]]) -> None:
        _ = query
        existing = {_as_uuid(row["store_id"]) for row in self._rows}
        for (store_id,) in args:
            if store_id not in existing:
                self._rows.append({"store_id": store_id})
                existing.add(store_id)


@pytest.fixture
async def db_conn() -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(DATABASE_URL_SIMULATOR)
    transaction = conn.transaction()
    await transaction.start()
    try:
        await conn.execute("DELETE FROM sim.fraud_collusive_stores")
        COLLUSIVE_STORES.clear()
        yield conn
    finally:
        COLLUSIVE_STORES.clear()
        await transaction.rollback()
        await conn.close()


def _ctx(seed: int = 42, hour: int = 12) -> FraudPatternContext:
    return FraudPatternContext(
        now=datetime(2024, 5, 24, hour, 0, tzinfo=LONDON_TZ_TEST),
        rng=random.Random(seed),
    )


def _store_pool(count: int = 20) -> list[UUID]:
    return [UUID(int=index + 1) for index in range(count)]


def _as_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _init_with_fake_db(seed: int = 42, n: int = 10) -> list[UUID]:
    store_pool = _store_pool()
    asyncio.run(
        init_collusive_stores(
            random.Random(seed),
            _FakeConn(),
            store_pool=store_pool,
            n=n,
        )
    )
    assert len(COLLUSIVE_STORES) == n
    return store_pool


async def _create_store_pool(
    conn: asyncpg.Connection,
    *,
    count: int,
    label: str,
) -> list[UUID]:
    merchant_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO merchants (merchant_id, legal_name, brand_name)
        VALUES ($1, $2, $3)
        """,
        merchant_id,
        f"{label} merchant",
        f"{label} merchant",
    )

    store_ids = [uuid.uuid4() for _ in range(count)]
    await conn.executemany(
        """
        INSERT INTO stores (
            store_id, merchant_id, store_name, address_line_1, city, postcode,
            latitude, longitude
        ) VALUES (
            $1, $2, $3, '1 Test Road', 'London', 'EC1A 1BB',
            51.5074, -0.1278
        )
        """,
        [
            (store_id, merchant_id, f"{label} store {index}")
            for index, store_id in enumerate(store_ids, start=1)
        ],
    )
    return store_ids


async def _fetch_collusive_store_ids(conn: asyncpg.Connection) -> set[UUID]:
    rows = await conn.fetch(
        """
        SELECT store_id
        FROM sim.fraud_collusive_stores
        ORDER BY store_id
        """
    )
    return {_as_uuid(row["store_id"]) for row in rows}


def test_registered() -> None:
    assert "collusive_merchant" in _REGISTRY
    _, weight = _REGISTRY["collusive_merchant"]
    assert math.isclose(weight, 0.05, rel_tol=0, abs_tol=1e-12)


def test_returns_ground_truth() -> None:
    _init_with_fake_db(seed=11)

    ctx = _ctx(seed=7)
    order_dict, gt = asyncio.run(generate_collusive_merchant_fraud(ctx))

    assert isinstance(order_dict, dict)
    assert isinstance(gt, GroundTruth)
    assert order_dict["order_id"] == gt.order_id
    assert gt.is_fraud is True
    assert gt.fraud_category == "collusive_merchant"
    assert isinstance(gt.pattern_notes, str)
    assert f"store_id={order_dict['store_id']}" == gt.pattern_notes
    assert gt.ring_id == order_dict["store_id"]
    assert gt.ring_id in COLLUSIVE_STORES


def test_avs_cvv_mostly_match() -> None:
    _init_with_fake_db(seed=123)
    ctx = _ctx(seed=777)

    avs_matches = 0
    cvv_matches = 0
    iterations = 1000

    for _ in range(iterations):
        order_dict, _gt = asyncio.run(generate_collusive_merchant_fraud(ctx))
        if order_dict["avs_result"] == "MATCH":
            avs_matches += 1
        if order_dict["cvv_result"] == "MATCH":
            cvv_matches += 1

    assert avs_matches / iterations >= 0.75
    assert cvv_matches / iterations >= 0.75


def test_store_concentration() -> None:
    _init_with_fake_db(seed=99)
    ctx = _ctx(seed=7)

    store_counts: dict[UUID, int] = {store_id: 0 for store_id in COLLUSIVE_STORES}
    for _ in range(1000):
        order_dict, gt = asyncio.run(generate_collusive_merchant_fraud(ctx))
        store_id = _as_uuid(order_dict["store_id"])
        assert store_id in store_counts
        assert gt.ring_id == store_id
        store_counts[store_id] += 1

    assert len(store_counts) == 10
    assert sum(store_counts.values()) == 1000
    assert all(count > 0 for count in store_counts.values())


def test_value_in_normal_range() -> None:
    _init_with_fake_db(seed=321)
    ctx = _ctx(seed=1234)

    totals: list[int] = []
    for _ in range(1000):
        order_dict, _gt = asyncio.run(generate_collusive_merchant_fraud(ctx))
        total = order_dict["order_total_pence"]
        assert isinstance(total, int)
        totals.append(total)

    average = sum(totals) / len(totals)
    assert min(totals) >= 500
    assert 1500 <= average <= 2500


@pytest.mark.asyncio
async def test_idempotent_init(db_conn: asyncpg.Connection) -> None:
    store_pool = await _create_store_pool(db_conn, count=15, label="collusive idempotent")
    existing_stores = store_pool[:10]
    await db_conn.executemany(
        "INSERT INTO sim.fraud_collusive_stores (store_id) VALUES ($1)",
        [(store_id,) for store_id in existing_stores],
    )

    await init_collusive_stores(
        random.Random(42),
        db_conn,
        store_pool=store_pool,
        n=10,
    )

    db_store_ids = await _fetch_collusive_store_ids(db_conn)
    assert len(db_store_ids) == 10
    assert db_store_ids == set(existing_stores)
    assert COLLUSIVE_STORES == db_store_ids


@pytest.mark.asyncio
async def test_load_from_db_tops_up(db_conn: asyncpg.Connection) -> None:
    store_pool = await _create_store_pool(db_conn, count=15, label="collusive fill")
    seeded_stores = store_pool[:5]
    await db_conn.executemany(
        "INSERT INTO sim.fraud_collusive_stores (store_id) VALUES ($1)",
        [(store_id,) for store_id in seeded_stores],
    )

    await init_collusive_stores(
        random.Random(7),
        db_conn,
        store_pool=store_pool,
        n=10,
    )

    db_store_ids = await _fetch_collusive_store_ids(db_conn)
    assert len(db_store_ids) == 10
    assert set(seeded_stores).issubset(db_store_ids)
    assert db_store_ids.issubset(set(store_pool))
    assert COLLUSIVE_STORES == db_store_ids
