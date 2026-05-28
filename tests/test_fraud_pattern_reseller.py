"""Tests for the reseller fraud simulator pattern."""

from __future__ import annotations

import asyncio
import json
import random
import sys
import unittest.mock
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo

from simulator.fraud_patterns import GroundTruth, _REGISTRY
from simulator.fraud_patterns.reseller import (
    RESELLER_ACCOUNTS,
    generate_reseller_fraud,
    init_reseller_accounts,
)
from simulator.fraud_patterns.stolen_card import FraudPatternContext

LONDON_TZ_TEST = ZoneInfo("Europe/London")


def _ctx(now: datetime | None = None, rng_seed: int = 99) -> FraudPatternContext:
    return FraudPatternContext(
        now=(datetime(2026, 1, 1, 12, 0, tzinfo=LONDON_TZ_TEST) if now is None else now),
        rng=random.Random(rng_seed),
    )


def _prime_transaction_mock(conn: unittest.mock.AsyncMock) -> None:
    conn.execute.return_value = None
    txn_mock = unittest.mock.AsyncMock()
    txn_mock.__aenter__ = unittest.mock.AsyncMock(return_value=txn_mock)
    txn_mock.__aexit__ = unittest.mock.AsyncMock(return_value=None)
    conn.transaction = unittest.mock.Mock()
    conn.transaction.return_value = txn_mock


@pytest.fixture
def mock_conn() -> unittest.mock.AsyncMock:
    conn = unittest.mock.AsyncMock()
    conn.fetch.return_value = []
    conn.executemany.return_value = None
    _prime_transaction_mock(conn)
    return conn


def _fake_row(i: int) -> dict[str, Any]:
    return {
        "account_id": UUID(int=i + 1),
        "reseller_address": {
            "lat": 51.5,
            "lon": -0.1,
            "postcode": "E1 1AA",
            "city": "London",
        },
        "delivery_address_uuid": UUID(int=i + 1001),
        "device_uuid": UUID(int=i + 2001),
        "preferred_store_ids": [UUID(int=i + 3001)],
    }


def _prime_empty_persistent_db(
    conn: unittest.mock.AsyncMock,
) -> dict[UUID, dict[str, Any]]:
    rows_dict: dict[UUID, dict[str, Any]] = {}

    async def _fetch(_query: str) -> list[dict[str, Any]]:
        return list(rows_dict.values())

    async def _executemany(
        _query: str,
        values: list[tuple[UUID, str, UUID, UUID, list[UUID]]],
    ) -> None:
        for (
            account_id,
            reseller_address,
            delivery_address_uuid,
            device_uuid,
            preferred_store_ids,
        ) in values:
            if account_id not in rows_dict:
                rows_dict[account_id] = {
                    "account_id": account_id,
                    "reseller_address": json.loads(reseller_address),
                    "delivery_address_uuid": delivery_address_uuid,
                    "device_uuid": device_uuid,
                    "preferred_store_ids": list(preferred_store_ids),
                }

    conn.fetch.side_effect = _fetch
    conn.executemany.side_effect = _executemany
    _prime_transaction_mock(conn)
    return rows_dict


def test_reseller_init(mock_conn: unittest.mock.AsyncMock) -> None:
    _prime_empty_persistent_db(mock_conn)
    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(42),
            conn=mock_conn,
            store_id_pool=[UUID(int=i + 1) for i in range(5)],
            n=50,
        )
    )

    assert len(RESELLER_ACCOUNTS) == 50
    account_ids = {account.account_id for account in RESELLER_ACCOUNTS}
    assert len(account_ids) == 50


def test_init_idempotent_across_restarts(mock_conn: unittest.mock.AsyncMock) -> None:
    rows_50 = [_fake_row(i) for i in range(50)]
    mock_conn.fetch.return_value = rows_50
    _prime_transaction_mock(mock_conn)

    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(42),
            conn=mock_conn,
            store_id_pool=[UUID(int=i + 1) for i in range(5)],
            n=50,
        )
    )
    first_account_ids = [account.account_id for account in RESELLER_ACCOUNTS]

    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(999),
            conn=mock_conn,
            store_id_pool=[UUID(int=i + 1) for i in range(5)],
            n=50,
        )
    )
    second_account_ids = [account.account_id for account in RESELLER_ACCOUNTS]

    assert second_account_ids == first_account_ids
    assert mock_conn.fetch.call_count == 2
    mock_conn.executemany.assert_not_called()


def test_init_loads_existing_from_db(mock_conn: unittest.mock.AsyncMock) -> None:
    rows_30 = [_fake_row(i) for i in range(30)]
    rows_50 = [_fake_row(i) for i in range(50)]
    mock_conn.fetch.side_effect = [rows_30, rows_50]
    _prime_transaction_mock(mock_conn)

    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(42),
            conn=mock_conn,
            store_id_pool=[UUID(int=i + 1) for i in range(5)],
            n=50,
        )
    )

    assert len(RESELLER_ACCOUNTS) == 50
    mock_conn.executemany.assert_called_once()


def test_init_no_pk_collision_on_topup(mock_conn: unittest.mock.AsyncMock) -> None:
    rows_dict = _prime_empty_persistent_db(mock_conn)
    store_id_pool = [UUID(int=i + 1) for i in range(5)]

    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(42),
            conn=mock_conn,
            store_id_pool=store_id_pool,
            n=50,
        )
    )
    assert len(RESELLER_ACCOUNTS) == 50

    # Remove positions 0..19 so the top-up must recreate earlier RNG positions.
    account_ids_to_remove = list(rows_dict)[:20]
    assert len(account_ids_to_remove) == 20
    for account_id in account_ids_to_remove:
        del rows_dict[account_id]
    assert len(rows_dict) == 30

    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(42),
            conn=mock_conn,
            store_id_pool=store_id_pool,
            n=50,
        )
    )

    assert len(RESELLER_ACCOUNTS) == 50
    assert len({account.account_id for account in RESELLER_ACCOUNTS}) == 50
    assert mock_conn.executemany.call_count == 2


def test_init_consumes_same_rng_draws_on_full_warm_restart() -> None:
    store_id_pool = [UUID(int=i + 1) for i in range(5)]

    cold_conn = unittest.mock.AsyncMock()
    _prime_empty_persistent_db(cold_conn)
    cold_rng = random.Random(42)
    asyncio.run(
        init_reseller_accounts(
            rng=cold_rng,
            conn=cold_conn,
            store_id_pool=store_id_pool,
            n=50,
        )
    )
    cold_next = cold_rng.getrandbits(128)

    warm_conn = unittest.mock.AsyncMock()
    warm_conn.fetch.return_value = [_fake_row(i) for i in range(50)]
    _prime_transaction_mock(warm_conn)
    warm_rng = random.Random(42)
    asyncio.run(
        init_reseller_accounts(
            rng=warm_rng,
            conn=warm_conn,
            store_id_pool=store_id_pool,
            n=50,
        )
    )
    warm_next = warm_rng.getrandbits(128)

    assert warm_next == cold_next
    warm_conn.executemany.assert_not_called()


def test_registered() -> None:
    assert "reseller" in _REGISTRY
    _, weight = _REGISTRY["reseller"]
    assert abs(weight - 0.05) < 1e-9


def test_returns_ground_truth(mock_conn: unittest.mock.AsyncMock) -> None:
    _prime_empty_persistent_db(mock_conn)
    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(42),
            conn=mock_conn,
            store_id_pool=[UUID(int=i + 1) for i in range(5)],
        )
    )

    order_dict, gt = asyncio.run(generate_reseller_fraud(_ctx()))

    assert isinstance(order_dict, dict)
    assert isinstance(gt, GroundTruth)
    assert gt.is_fraud is True
    assert gt.fraud_category == "reseller"
    assert gt.ring_id is None
    assert isinstance(order_dict["delivery_address_id"], UUID)
    assert isinstance(order_dict["device_id"], UUID)
    assert isinstance(order_dict["user_id"], UUID)
    assert isinstance(order_dict["store_id"], UUID)


def test_bulk_cart(mock_conn: unittest.mock.AsyncMock) -> None:
    _prime_empty_persistent_db(mock_conn)
    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(42),
            conn=mock_conn,
            store_id_pool=[UUID(int=i + 1) for i in range(5)],
        )
    )
    rng: random.Random = random.Random(123)
    item_counts: list[int] = []
    ctx_time = datetime(2026, 1, 2, 10, 0, tzinfo=LONDON_TZ_TEST)

    for _ in range(1000):
        ctx = FraudPatternContext(now=ctx_time, rng=rng)
        order_dict, _ = asyncio.run(generate_reseller_fraud(ctx))
        item_count = order_dict["item_count"]
        assert 10 <= item_count <= 25
        item_counts.append(item_count)
        assert isinstance(order_dict["user_id"], UUID)
        assert isinstance(order_dict["store_id"], UUID)

    mean_item_count = sum(item_counts) / len(item_counts)
    assert 16.0 <= mean_item_count <= 19.0


def test_delivery_address_stable(mock_conn: unittest.mock.AsyncMock) -> None:
    _prime_empty_persistent_db(mock_conn)
    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(42),
            conn=mock_conn,
            store_id_pool=[UUID(int=i + 1) for i in range(5)],
        )
    )

    original_accounts = list(RESELLER_ACCOUNTS)
    single_account = RESELLER_ACCOUNTS[0]

    try:
        RESELLER_ACCOUNTS.clear()
        RESELLER_ACCOUNTS.append(single_account)

        ctx_time = datetime(2026, 1, 3, 11, 0, tzinfo=LONDON_TZ_TEST)
        rng: random.Random = random.Random(321)

        address_ids: list[UUID] = []
        device_ids: list[UUID] = []
        for _ in range(10):
            order_dict, _ = asyncio.run(
                generate_reseller_fraud(FraudPatternContext(now=ctx_time, rng=rng))
            )
            assert isinstance(order_dict["delivery_address_id"], UUID)
            assert isinstance(order_dict["device_id"], UUID)
            address_ids.append(order_dict["delivery_address_id"])
            device_ids.append(order_dict["device_id"])

        assert all(address == address_ids[0] for address in address_ids)
        assert all(device_id == device_ids[0] for device_id in device_ids)
    finally:
        RESELLER_ACCOUNTS.clear()
        RESELLER_ACCOUNTS.extend(original_accounts)


def test_value_scales_with_items(mock_conn: unittest.mock.AsyncMock) -> None:
    _prime_empty_persistent_db(mock_conn)
    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(42),
            conn=mock_conn,
            store_id_pool=[UUID(int=i + 1) for i in range(5)],
        )
    )
    ctx_time = datetime(2026, 1, 4, 9, 0, tzinfo=LONDON_TZ_TEST)
    low_totals: list[int] = []
    high_totals: list[int] = []

    class _FixedItemCountRng(random.Random):
        def __init__(self, forced_item_count: int) -> None:
            super().__init__()
            self._forced_item_count = forced_item_count

        def randint(self, a: int, b: int) -> int:
            if a == 10 and b == 25:
                return self._forced_item_count
            return super().randint(a, b)

    def _sample_total_for_item_count(item_count: int, seed: int) -> int:
        base_rng = random.Random(seed)
        rng = _FixedItemCountRng(item_count)
        rng.setstate(base_rng.getstate())
        order_dict, _ = asyncio.run(
            generate_reseller_fraud(FraudPatternContext(now=ctx_time, rng=rng))
        )
        assert order_dict["item_count"] == item_count
        return order_dict["order_total_pence"]

    for seed in range(100):
        low_totals.append(_sample_total_for_item_count(item_count=10, seed=seed))
        high_totals.append(_sample_total_for_item_count(item_count=25, seed=seed))

    assert all(high > low for low, high in zip(low_totals, high_totals))
    mean_low = sum(low_totals) / len(low_totals)
    mean_high = sum(high_totals) / len(high_totals)
    assert mean_high > mean_low
