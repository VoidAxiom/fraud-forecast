"""Tests for the triangulation fraud simulator pattern."""

from __future__ import annotations

import asyncio
import random
import sys
from datetime import datetime
from typing import Any
from uuid import UUID

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

LONDON_TZ_TEST: ZoneInfo = ZoneInfo("Europe/London")


def _make_ctx(seed: int = 42) -> FraudPatternContext:
    return FraudPatternContext(
        now=datetime(2024, 6, 1, 14, 0, tzinfo=LONDON_TZ_TEST),
        rng=random.Random(seed),
    )


def test_triangulation_init() -> None:
    """init_accounts(rng, n=30) produces 30 accounts with distinct IDs."""
    rng = random.Random(1)
    init_accounts(rng, n=30)

    assert len(TRIANGULATION_ACCOUNTS) == 30

    ids: set[UUID] = {acc.account_id for acc in TRIANGULATION_ACCOUNTS}
    assert len(ids) == 30, "all account_ids must be distinct"

    device_ids: set[UUID] = {acc.device_id for acc in TRIANGULATION_ACCOUNTS}
    assert len(device_ids) == 30, "all device_ids must be distinct"


def test_triangulation_returns_ground_truth() -> None:
    """generate_triangulation_fraud returns is_fraud=True, fraud_category='triangulation'."""
    init_accounts(random.Random(2), n=30)
    ctx = _make_ctx(seed=99)

    order_dict, gt = asyncio.run(generate_triangulation_fraud(ctx))

    assert isinstance(order_dict, dict)
    assert isinstance(gt, GroundTruth)
    assert gt.is_fraud is True
    assert gt.fraud_category == "triangulation"
    assert isinstance(gt.order_id, UUID)
    assert gt.ring_id is None


def test_triangulation_registered() -> None:
    """'triangulation' is in _REGISTRY with weight 0.05."""
    init_accounts(random.Random(3), n=30)

    assert "triangulation" in _REGISTRY, "'triangulation' must be auto-discovered in _REGISTRY"
    _fn, weight = _REGISTRY["triangulation"]
    assert abs(weight - 0.05) < 1e-9, f"expected weight 0.05, got {weight}"


def test_triangulation_device_consistency() -> None:
    """Within one account, multiple orders share the same device_id."""
    init_accounts(random.Random(4), n=30)

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
    for _ in range(10):
        order_dict, _ = asyncio.run(generate_triangulation_fraud(ctx))
        device_ids.add(order_dict["device_id"])

    assert len(device_ids) == 1, f"expected 1 unique device_id, got {len(device_ids)}: {device_ids}"
    assert expected_device in device_ids


def test_triangulation_address_diversity() -> None:
    """Across 100 orders from one account, >= 80% have unique delivery addresses."""
    init_accounts(random.Random(6), n=30)

    account: TriangulationAccount = TRIANGULATION_ACCOUNTS[0]
    account.delivery_addresses_used.clear()
    account.cards_used.clear()

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
    init_accounts(random.Random(8), n=30)

    account: TriangulationAccount = TRIANGULATION_ACCOUNTS[0]
    account.delivery_addresses_used.clear()
    account.cards_used.clear()

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
