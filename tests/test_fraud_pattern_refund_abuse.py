"""Tests for the refund-abuse fraud simulator pattern."""

from __future__ import annotations

import asyncio
import math
import random
import sys
from datetime import datetime

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo

from simulator.fraud_patterns import _REGISTRY
from simulator.fraud_patterns.stolen_card import FraudPatternContext
from simulator.fraud_patterns.refund_abuse import generate_refund_abuse_fraud


def test_refund_abuse_registered() -> None:
    assert "refund_abuse" in _REGISTRY
    _, weight = _REGISTRY["refund_abuse"]
    assert math.isclose(weight, 0.10, rel_tol=0, abs_tol=1e-12)


def test_refund_abuse_returns_ground_truth() -> None:
    ctx = FraudPatternContext(
        now=datetime(2024, 5, 24, 12, 0, tzinfo=ZoneInfo("Europe/London")),
        rng=random.Random(1),
    )
    _, gt = asyncio.run(generate_refund_abuse_fraud(ctx))

    assert gt.is_fraud is True
    assert gt.fraud_category == "refund_abuse"


def test_refund_abuse_order_looks_normal() -> None:
    ctx = FraudPatternContext(
        now=datetime(2024, 5, 24, 12, 0, tzinfo=ZoneInfo("Europe/London")),
        rng=random.Random(2),
    )
    order_dict, _ = asyncio.run(generate_refund_abuse_fraud(ctx))

    assert order_dict["payment_method_id"] == "ABUSER_SAVED"
    assert order_dict["delivery_address_id"] == "ABUSER_SAVED"
    assert order_dict["is_new_device"] is False


def test_refund_abuse_value_elevated() -> None:
    shared_rng = random.Random(42)
    now = datetime(2024, 5, 24, 12, 0, tzinfo=ZoneInfo("Europe/London"))

    totals: list[int] = []
    for _ in range(1000):
        ctx = FraudPatternContext(now=now, rng=shared_rng)
        order_dict, _ = asyncio.run(generate_refund_abuse_fraud(ctx))
        totals.append(order_dict["order_total_pence"])

    assert sum(totals) / len(totals) > 3000


def test_refund_abuse_pattern_notes_filter() -> None:
    now = datetime(2024, 5, 24, 12, 0, tzinfo=ZoneInfo("Europe/London"))

    for _ in range(10):
        ctx = FraudPatternContext(now=now, rng=random.Random(3))
        _, gt = asyncio.run(generate_refund_abuse_fraud(ctx))

        assert gt.pattern_notes is not None
        assert "_refund_abuser_filter=refunds_lifetime__gte_3" in gt.pattern_notes
