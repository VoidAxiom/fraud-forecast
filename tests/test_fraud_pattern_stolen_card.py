"""Tests for the stolen-card fraud simulator pattern."""

from __future__ import annotations

import asyncio
import pytest
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
    NIGHT_HOURS,
    _weighted_choice,
    generate_stolen_card_fraud,
)

LONDON_TZ_TEST = ZoneInfo("Europe/London")


def _ctx(seed: int = 42, hour: int = 12) -> FraudPatternContext:
    return FraudPatternContext(
        now=datetime(2024, 5, 24, hour, 0, tzinfo=LONDON_TZ_TEST),
        rng=random.Random(seed),
    )


def test_stolen_card_pattern_is_registered_with_expected_weight() -> None:
    assert "stolen_card" in _REGISTRY
    _, weight = _REGISTRY["stolen_card"]
    assert math.isclose(weight, 0.30, rel_tol=0, abs_tol=1e-12)


def test_register_rejects_zero_weight() -> None:
    from simulator.fraud_patterns import register

    with pytest.raises(ValueError, match="must have weight > 0"):
        @register("test_zero", 0.0)
        async def _dummy(ctx: FraudPatternContext) -> None:
            return None

    with pytest.raises(ValueError, match="must have weight > 0"):
        @register("test_neg", -1.0)
        async def _dummy(ctx: FraudPatternContext) -> None:
            return None


def test_fraud_pattern_context_now_is_required() -> None:
    with pytest.raises(TypeError):
        FraudPatternContext()


def test_naive_datetime_raises() -> None:
    ctx_naive = FraudPatternContext(
        now=datetime(2024, 3, 15, 14, 0, 0), rng=random.Random(0)
    )

    with pytest.raises(ValueError):
        asyncio.run(generate_stolen_card_fraud(ctx=ctx_naive))


def test_generate_stolen_card_fraud_returns_expected_shape_and_truth() -> None:
    ctx = FraudPatternContext(
        rng=random.Random(123),
        now=datetime(2026, 1, 2, 3, 45, tzinfo=ZoneInfo("Europe/London")),
    )
    order_dict, gt = asyncio.run(generate_stolen_card_fraud(ctx))
    required_keys = {
        "order_id",
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
        "placed_at",
    }
    assert required_keys.issubset(order_dict)
    assert isinstance(order_dict["order_id"], uuid.UUID)
    assert isinstance(order_dict["order_total_pence"], int)
    assert isinstance(order_dict["placed_at"], datetime)
    assert isinstance(order_dict["is_new_device"], bool)
    assert order_dict["is_digital_native_bank"] is False
    assert order_dict["order_id"] == gt.order_id
    assert gt.is_fraud is True
    assert gt.fraud_category == "stolen_card"
    assert isinstance(gt, GroundTruth)


def test_generate_stolen_card_fraud_is_deterministic_for_order_id() -> None:
    now = datetime(2024, 1, 15, 3, 0, tzinfo=ZoneInfo("Europe/London"))
    ctx1 = FraudPatternContext(rng=random.Random(42), now=now)
    ctx2 = FraudPatternContext(rng=random.Random(42), now=now)

    order1, gt1 = asyncio.run(generate_stolen_card_fraud(ctx1))
    order2, gt2 = asyncio.run(generate_stolen_card_fraud(ctx2))

    assert gt1.order_id == gt2.order_id
    assert order1["order_id"] == order2["order_id"]
    assert order1["order_id"] == gt1.order_id
    assert isinstance(gt1.order_id, uuid.UUID)


def test_placed_at_night_skew_rate_and_derivation_invariant() -> None:
    """placed_at hour is skewed to 2-5am and is_night_order is derived from it."""
    # daytime ctx.now to prove it's not wall-clock-dependent
    ctx = FraudPatternContext(
        rng=random.Random(42),
        now=datetime(2026, 1, 2, 14, 0, tzinfo=ZoneInfo("Europe/London")),
    )
    results = [asyncio.run(generate_stolen_card_fraud(ctx))[0] for _ in range(1000)]
    night_hours = set(NIGHT_HOURS)
    rate = sum(1 for order_dict in results if order_dict["placed_at"].hour in night_hours) / len(
        results
    )

    assert 0.35 <= rate <= 0.45, f"night-order rate {rate:.3f} outside [0.35, 0.45]"
    assert all(
        order_dict["is_night_order"] == (order_dict["placed_at"].hour in night_hours)
        for order_dict in results
    )


def test_weighted_choice_returns_choice_from_values() -> None:
    value = _weighted_choice(random.Random(7), [("A", 1.0), ("B", 2.0), ("C", 3.0)])
    assert value in {"A", "B", "C"}


def test_generate_fraud_order_dispatches_to_stolen_card_pattern() -> None:
    order_dict, gt = asyncio.run(generate_fraud_order(_ctx()))
    assert isinstance(order_dict, dict)
    assert isinstance(gt, GroundTruth)
    assert gt.fraud_category == "stolen_card"


def test_generate_fraud_order_is_deterministic_with_seeded_rng() -> None:
    """Same RNG seed must produce same order_id and order content end-to-end."""
    order1, gt1 = asyncio.run(generate_fraud_order(_ctx(seed=7777)))
    order2, gt2 = asyncio.run(generate_fraud_order(_ctx(seed=7777)))
    assert order1["order_id"] == order2["order_id"]
    assert gt1.order_id == gt2.order_id
    assert order1["order_total_pence"] == order2["order_total_pence"]
    assert order1["variant"] == order2["variant"]
    assert order1["is_night_order"] == order2["is_night_order"]
    assert order1["placed_at"] == order2["placed_at"]


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
