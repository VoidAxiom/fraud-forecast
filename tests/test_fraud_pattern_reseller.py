"""Tests for the reseller fraud simulator pattern."""

from __future__ import annotations

import asyncio
import random
import sys
from datetime import datetime
from uuid import UUID

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


def test_reseller_init() -> None:
    init_reseller_accounts(rng=random.Random(42), n=50)

    assert len(RESELLER_ACCOUNTS) == 50
    account_ids = {account.account_id for account in RESELLER_ACCOUNTS}
    assert len(account_ids) == 50


def test_registered() -> None:
    assert "reseller" in _REGISTRY
    _, weight = _REGISTRY["reseller"]
    assert abs(weight - 0.05) < 1e-9


def test_returns_ground_truth() -> None:
    init_reseller_accounts(rng=random.Random(42))

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


def test_bulk_cart() -> None:
    init_reseller_accounts(rng=random.Random(42))
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


def test_delivery_address_stable() -> None:
    init_reseller_accounts(rng=random.Random(42))

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


def test_value_scales_with_items() -> None:
    init_reseller_accounts(rng=random.Random(42))
    rng: random.Random = random.Random(777)
    ctx_time = datetime(2026, 1, 4, 9, 0, tzinfo=LONDON_TZ_TEST)
    totals: list[int] = []

    for _ in range(500):
        order_dict, _ = asyncio.run(
            generate_reseller_fraud(FraudPatternContext(now=ctx_time, rng=rng))
        )
        totals.append(order_dict["order_total_pence"])

    mean_total = sum(totals) / len(totals)
    assert 10000 <= mean_total <= 20000
