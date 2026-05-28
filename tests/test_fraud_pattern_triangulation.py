"""Tests for the triangulation fraud simulator pattern."""

from __future__ import annotations

import asyncio
import os
import random
import sys
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import asyncpg

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo

from simulator.fraud_patterns import GroundTruth, _REGISTRY
from simulator.fraud_patterns.triangulation import (
    TRIANGULATION_ACCOUNTS,
    TriangulationAccount,
    generate_triangulation_fraud,
    init_accounts,
)
from simulator.fraud_patterns.stolen_card import FraudPatternContext

DATABASE_URL_SIMULATOR = os.getenv(
    "DATABASE_URL_SIMULATOR",
    "postgresql://simulator_user:simulator_dev_password@postgres:5432/fraud_platform",
)
LONDON_TZ_TEST: ZoneInfo = ZoneInfo("Europe/London")


def _make_ctx(seed: int = 42) -> FraudPatternContext:
    return FraudPatternContext(
        now=datetime(2024, 6, 1, 14, 0, tzinfo=LONDON_TZ_TEST),
        rng=random.Random(seed),
    )


def _init_accounts_with_mock_db(rng: random.Random, n: int = 30) -> None:
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    conn.execute.return_value = "DELETE 0"
    conn.executemany.return_value = None
    asyncio.run(init_accounts(rng, conn, n=n))


def test_init_accounts_idempotent() -> None:
    """init_accounts loads persisted identities instead of reseeding when row count matches."""

    async def _run() -> None:
        conn = await asyncpg.connect(DATABASE_URL_SIMULATOR)
        try:
            await conn.execute("DELETE FROM sim.fraud_triangulation_accounts")

            await init_accounts(random.Random(31), conn, n=5)
            first_account_ids = {acc.account_id for acc in TRIANGULATION_ACCOUNTS}
            assert len(first_account_ids) == 5

            await init_accounts(random.Random(31), conn, n=5)
            second_account_ids = {acc.account_id for acc in TRIANGULATION_ACCOUNTS}

            assert second_account_ids == first_account_ids
            row_count = await conn.fetchval(
                "SELECT count(*) FROM sim.fraud_triangulation_accounts"
            )
            assert row_count == 5
        finally:
            await conn.execute("DELETE FROM sim.fraud_triangulation_accounts")
            await conn.close()
            TRIANGULATION_ACCOUNTS.clear()

    asyncio.run(_run())


def test_init_accounts_load_from_db() -> None:
    """init_accounts populates TRIANGULATION_ACCOUNTS from persisted DB rows."""

    async def _run() -> None:
        conn = await asyncpg.connect(DATABASE_URL_SIMULATOR)
        base_created_at = datetime(2024, 6, 1, 12, 0, tzinfo=LONDON_TZ_TEST)
        seed_rows = [
            (
                UUID(int=100 + i),
                UUID(int=200 + i),
                base_created_at + timedelta(minutes=i),
            )
            for i in range(3)
        ]
        try:
            await conn.execute("DELETE FROM sim.fraud_triangulation_accounts")
            await conn.executemany(
                """
                INSERT INTO sim.fraud_triangulation_accounts (account_id, device_id, created_at)
                VALUES ($1, $2, $3)
                """,
                seed_rows,
            )

            await init_accounts(random.Random(999), conn, n=len(seed_rows))

            expected_pairs = [(account_id, device_id) for account_id, device_id, _ in seed_rows]
            loaded_pairs = [
                (acc.account_id, acc.device_id) for acc in TRIANGULATION_ACCOUNTS
            ]
            assert loaded_pairs == expected_pairs
            assert all(acc.cards_used_count == 0 for acc in TRIANGULATION_ACCOUNTS)
            assert all(acc.delivery_addresses_used_count == 0 for acc in TRIANGULATION_ACCOUNTS)
        finally:
            await conn.execute("DELETE FROM sim.fraud_triangulation_accounts")
            await conn.close()
            TRIANGULATION_ACCOUNTS.clear()

    asyncio.run(_run())


def test_triangulation_init() -> None:
    """init_accounts(rng, n=30) produces 30 accounts with distinct IDs."""
    rng = random.Random(1)
    _init_accounts_with_mock_db(rng, n=30)

    assert len(TRIANGULATION_ACCOUNTS) == 30

    ids: set[UUID] = {acc.account_id for acc in TRIANGULATION_ACCOUNTS}
    assert len(ids) == 30, "all account_ids must be distinct"

    device_ids: set[UUID] = {acc.device_id for acc in TRIANGULATION_ACCOUNTS}
    assert len(device_ids) == 30, "all device_ids must be distinct"


def test_triangulation_returns_ground_truth() -> None:
    """generate_triangulation_fraud returns is_fraud=True, fraud_category='triangulation'."""
    _init_accounts_with_mock_db(random.Random(2), n=30)
    ctx = _make_ctx(seed=99)

    order_dict, gt = asyncio.run(generate_triangulation_fraud(ctx))

    assert isinstance(order_dict, dict)
    assert isinstance(gt, GroundTruth)
    assert gt.is_fraud is True
    assert gt.fraud_category == "triangulation"
    assert "user_id" in order_dict
    assert isinstance(order_dict["user_id"], UUID)
    assert isinstance(gt.order_id, UUID)
    assert gt.ring_id is None


def test_triangulation_registered() -> None:
    """'triangulation' is in _REGISTRY with weight 0.05."""
    _init_accounts_with_mock_db(random.Random(3), n=30)

    assert "triangulation" in _REGISTRY, "'triangulation' must be auto-discovered in _REGISTRY"
    _fn, weight = _REGISTRY["triangulation"]
    assert abs(weight - 0.05) < 1e-9, f"expected weight 0.05, got {weight}"


def test_triangulation_device_consistency() -> None:
    """Within one account, multiple orders share the same device_id."""
    _init_accounts_with_mock_db(random.Random(4), n=30)

    # Pin to the first account for determinism
    account: TriangulationAccount = TRIANGULATION_ACCOUNTS[0]
    expected_device: UUID = account.device_id

    # Monkey-patch rng.choice to always return account[0]
    class _PinnedCtx(random.Random):
        def choice(self, seq: Any) -> Any:  # type: ignore[override]
            # If it's a list of TriangulationAccount, always pick index 0
            if seq and isinstance(seq[0], TriangulationAccount):
                return seq[0]
            return super().choice(seq)

    pin_rng = _PinnedCtx(5)
    ctx = FraudPatternContext(
        now=datetime(2024, 6, 1, 14, 0, tzinfo=LONDON_TZ_TEST),
        rng=pin_rng,
    )

    device_ids: set[UUID] = set()
    user_ids: set[UUID] = set()
    for _ in range(10):
        order_dict, _ = asyncio.run(generate_triangulation_fraud(ctx))
        device_ids.add(order_dict["device_id"])
        user_ids.add(order_dict["user_id"])

    assert len(device_ids) == 1, f"expected 1 unique device_id, got {len(device_ids)}: {device_ids}"
    assert expected_device in device_ids
    assert len(user_ids) == 1, "pinned account must produce consistent user_id"


def test_triangulation_address_diversity() -> None:
    """Across 100 orders from one account, >= 80% have unique delivery addresses."""
    _init_accounts_with_mock_db(random.Random(6), n=30)

    account: TriangulationAccount = TRIANGULATION_ACCOUNTS[0]
    account.delivery_addresses_used_count = 0
    account.cards_used_count = 0

    class _PinRng2(random.Random):
        def choice(self, seq: Any) -> Any:  # type: ignore[override]
            if seq and isinstance(seq[0], TriangulationAccount):
                return seq[0]
            return super().choice(seq)

    pin_rng = _PinRng2(7)
    ctx = FraudPatternContext(
        now=datetime(2024, 6, 1, 14, 0, tzinfo=LONDON_TZ_TEST),
        rng=pin_rng,
    )

    addresses: list[UUID] = []
    for _ in range(100):
        order_dict, _ = asyncio.run(generate_triangulation_fraud(ctx))
        addresses.append(order_dict["delivery_address_id"])

    unique_count = len(set(addresses))
    pct_unique = unique_count / len(addresses)
    assert pct_unique >= 0.80, f"expected >= 80% unique addresses, got {pct_unique:.1%} ({unique_count}/100)"


def test_triangulation_card_diversity() -> None:
    """Across 100 orders from one account, >= 80% use unique cards."""
    _init_accounts_with_mock_db(random.Random(8), n=30)

    account: TriangulationAccount = TRIANGULATION_ACCOUNTS[0]
    account.delivery_addresses_used_count = 0
    account.cards_used_count = 0

    class _PinRng3(random.Random):
        def choice(self, seq: Any) -> Any:  # type: ignore[override]
            if seq and isinstance(seq[0], TriangulationAccount):
                return seq[0]
            return super().choice(seq)

    pin_rng = _PinRng3(9)
    ctx = FraudPatternContext(
        now=datetime(2024, 6, 1, 14, 0, tzinfo=LONDON_TZ_TEST),
        rng=pin_rng,
    )

    payment_method_ids: list[str] = []
    for _ in range(100):
        order_dict, _ = asyncio.run(generate_triangulation_fraud(ctx))
        payment_method_ids.append(order_dict["payment_method_id"])

    unique_count = len(set(payment_method_ids))
    pct_unique = unique_count / len(payment_method_ids)
    assert pct_unique >= 0.80, f"expected >= 80% unique cards, got {pct_unique:.1%} ({unique_count}/100)"


def test_triangulation_iso2_invariant() -> None:
    """Card/IP countries should always be the same valid ISO-2 code."""
    _init_accounts_with_mock_db(random.Random(10), n=30)
    ctx = _make_ctx(seed=11)

    for _ in range(200):
        order_dict, _ = asyncio.run(generate_triangulation_fraud(ctx))
        card_country = order_dict["card_country"]
        ip_country = order_dict["ip_country"]

        assert isinstance(card_country, str)
        assert isinstance(ip_country, str)
        assert len(card_country) == 2
        assert len(ip_country) == 2
        assert card_country.isupper()
        assert ip_country.isupper()
        assert card_country == ip_country


def test_triangulation_counter_increments() -> None:
    """Order generation should increment both triangulation counters by one each call."""
    rng = random.Random(12)
    _init_accounts_with_mock_db(rng, n=1)

    class _PinnedCtx(random.Random):
        def choice(self, seq: Any) -> Any:  # type: ignore[override]
            return TRIANGULATION_ACCOUNTS[0]

    ctx = FraudPatternContext(
        now=datetime(2024, 6, 1, 14, 0, tzinfo=LONDON_TZ_TEST),
        rng=_PinnedCtx(12),
    )

    for _ in range(5):
        asyncio.run(generate_triangulation_fraud(ctx))

    account = TRIANGULATION_ACCOUNTS[0]
    assert account.cards_used_count == 5
    assert account.delivery_addresses_used_count == 5
