"""Tests for the collusive-merchant fraud simulator pattern."""

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
from simulator.fraud_patterns.collusive_merchant import (
    COLLUSIVE_STORES,
    generate_collusive_merchant_fraud,
    init_collusive_stores,
)
from simulator.fraud_patterns.stolen_card import FraudPatternContext

LONDON_TZ_TEST = ZoneInfo("Europe/London")


def _ctx(seed: int = 42, hour: int = 12) -> FraudPatternContext:
    return FraudPatternContext(
        now=datetime(2024, 5, 24, hour, 0, tzinfo=LONDON_TZ_TEST),
        rng=random.Random(seed),
    )


def test_collusive_init() -> None:
    init_rng = random.Random(42)
    init_collusive_stores(rng=init_rng)

    assert len(COLLUSIVE_STORES) == 10
    assert len(set(COLLUSIVE_STORES)) == 10


def test_registered() -> None:
    assert "collusive_merchant" in _REGISTRY
    _, weight = _REGISTRY["collusive_merchant"]
    assert abs(weight - 0.05) < 1e-12


def test_returns_ground_truth() -> None:
    init_rng = random.Random(11)
    init_collusive_stores(rng=init_rng)

    ctx = _ctx(seed=7)
    order_dict, gt = asyncio.run(generate_collusive_merchant_fraud(ctx))

    assert isinstance(order_dict, dict)
    assert isinstance(gt, GroundTruth)
    assert order_dict["order_id"] == gt.order_id
    assert gt.is_fraud is True
    assert gt.fraud_category == "collusive_merchant"
    assert isinstance(gt.pattern_notes, str)
    assert f"store_id={order_dict['store_id']}" == gt.pattern_notes
    assert gt.ring_id is not None
    assert gt.ring_id in COLLUSIVE_STORES


def test_avs_cvv_mostly_match() -> None:
    init_rng = random.Random(123)
    init_collusive_stores(rng=init_rng)
    ctx = FraudPatternContext(
        now=datetime(2024, 5, 24, 12, 0, tzinfo=LONDON_TZ_TEST),
        rng=random.Random(777),
    )

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
    init_rng = random.Random(99)
    init_collusive_stores(rng=init_rng)
    ctx = FraudPatternContext(
        now=datetime(2024, 5, 24, 12, 0, tzinfo=LONDON_TZ_TEST),
        rng=random.Random(7),
    )

    store_counts: dict[UUID, int] = {store_id: 0 for store_id in COLLUSIVE_STORES}
    for _ in range(1000):
        _, gt = asyncio.run(generate_collusive_merchant_fraud(ctx))
        assert isinstance(gt.pattern_notes, str)
        _, store_text = gt.pattern_notes.split("=", 1)
        store = UUID(store_text)
        assert store in store_counts
        store_counts[store] += 1

    assert len(store_counts) == 10
    expected = 100
    tolerance = 30
    assert all(
        expected - tolerance <= count <= expected + tolerance for count in store_counts.values()
    )


def test_value_in_normal_range() -> None:
    init_rng = random.Random(321)
    init_collusive_stores(rng=init_rng)
    ctx = FraudPatternContext(
        now=datetime(2024, 5, 24, 12, 0, tzinfo=LONDON_TZ_TEST),
        rng=random.Random(1234),
    )

    totals: list[int] = []
    for _ in range(1000):
        order_dict, _gt = asyncio.run(generate_collusive_merchant_fraud(ctx))
        totals.append(order_dict["order_total_pence"])

    average = sum(totals) / len(totals)
    assert 1500 <= average <= 2500
