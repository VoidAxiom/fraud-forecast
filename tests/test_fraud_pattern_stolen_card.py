"""Tests for the stolen-card fraud simulator pattern."""

from __future__ import annotations

import asyncio
import uuid
import math
import random
import sys
from datetime import datetime

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo

from simulator.fraud_patterns import GroundTruth, _REGISTRY, generate_fraud_order
from simulator.fraud_patterns.stolen_card import (
    FraudPatternContext,
    _weighted_choice,
    generate_stolen_card_fraud,
)


def test_stolen_card_pattern_is_registered_with_expected_weight() -> None:
    assert "stolen_card" in _REGISTRY
    _, weight = _REGISTRY["stolen_card"]
    assert math.isclose(weight, 0.30, rel_tol=0, abs_tol=1e-12)


def test_generate_stolen_card_fraud_returns_expected_shape_and_truth() -> None:
    ctx = FraudPatternContext(
        rng=random.Random(123),
        now=datetime(2026, 1, 2, 3, 45, tzinfo=ZoneInfo("Europe/London")),
    )
    order_dict, gt = asyncio.run(generate_stolen_card_fraud(ctx))
    required_keys = {
        "order_total_pence",
        "card_country",
        "card_funding_type",
        "avs_result",
        "cvv_result",
        "address_type",
        "is_new_device",
        "ip_type",
        "is_high_end_cart",
        "variant",
        "is_digital_native_bank",
    }
    assert required_keys.issubset(order_dict)
    assert isinstance(order_dict["order_total_pence"], int)
    assert isinstance(order_dict["is_new_device"], bool)
    assert order_dict["is_digital_native_bank"] is False
    assert gt.is_fraud is True
    assert gt.fraud_category == "stolen_card"
    assert isinstance(gt, GroundTruth)


def test_generate_stolen_card_fraud_is_deterministic_for_order_id() -> None:
    now = datetime(2024, 1, 15, 3, 0, tzinfo=ZoneInfo("Europe/London"))
    ctx1 = FraudPatternContext(rng=random.Random(42), now=now)
    ctx2 = FraudPatternContext(rng=random.Random(42), now=now)

    _, gt1 = asyncio.run(generate_stolen_card_fraud(ctx1))
    _, gt2 = asyncio.run(generate_stolen_card_fraud(ctx2))

    assert gt1.order_id == gt2.order_id
    assert isinstance(gt1.order_id, uuid.UUID)


def test_is_night_order_rate_is_approximately_40_percent() -> None:
    """is_night_order must hit ~40% rate regardless of wall-clock (seeded RNG)."""
    # daytime ctx.now to prove it's not wall-clock-dependent
    ctx = FraudPatternContext(
        rng=random.Random(42),
        now=datetime(2026, 1, 2, 14, 0, tzinfo=ZoneInfo("Europe/London")),
    )
    results = [
        asyncio.run(generate_stolen_card_fraud(ctx))[0]["is_night_order"]
        for _ in range(500)
    ]
    rate = sum(results) / len(results)
    assert 0.30 <= rate <= 0.50, f"night-order rate {rate:.3f} outside [0.30, 0.50]"


def test_weighted_choice_returns_choice_from_values() -> None:
    value = _weighted_choice(random.Random(7), [("A", 1.0), ("B", 2.0), ("C", 3.0)])
    assert value in {"A", "B", "C"}


def test_generate_fraud_order_dispatches_to_stolen_card_pattern() -> None:
    order_dict, gt = asyncio.run(generate_fraud_order())
    assert isinstance(order_dict, dict)
    assert isinstance(gt, GroundTruth)
    assert gt.fraud_category == "stolen_card"


def test_generate_fraud_order_is_deterministic_with_seeded_rng() -> None:
    """Same RNG seed must produce same order_id and order content end-to-end."""
    order1, gt1 = asyncio.run(generate_fraud_order(rng=random.Random(7777)))
    order2, gt2 = asyncio.run(generate_fraud_order(rng=random.Random(7777)))
    assert gt1.order_id == gt2.order_id
    assert order1["order_total_pence"] == order2["order_total_pence"]
    assert order1["variant"] == order2["variant"]
    assert order1["is_night_order"] == order2["is_night_order"]


def test_stolen_card_variants_are_within_generous_bounds() -> None:
    ctx = FraudPatternContext(
        rng=random.Random(888),
        now=datetime(2026, 1, 4, 13, 0, tzinfo=ZoneInfo("Europe/London")),
    )
    variant_counts: dict[str, int] = {"A": 0, "B": 0, "C": 0}
    for _ in range(1000):
        order_dict, _gt = asyncio.run(generate_stolen_card_fraud(ctx))
        variant_counts[order_dict["variant"]] += 1

    assert 500 <= variant_counts["A"] <= 700
    assert 200 <= variant_counts["B"] <= 400
    assert 50 <= variant_counts["C"] <= 200


def test_stolen_card_total_is_floored_to_two_thousand_pence() -> None:
    ctx = FraudPatternContext(
        rng=random.Random(99),
        now=datetime(2026, 1, 5, 14, 0, tzinfo=ZoneInfo("Europe/London")),
    )
    for _ in range(100):
        order_dict, _gt = asyncio.run(generate_stolen_card_fraud(ctx))
        assert order_dict["order_total_pence"] >= 2000
