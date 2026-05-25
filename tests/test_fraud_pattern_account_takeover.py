"""Tests for the account-takeover fraud simulator pattern."""

from __future__ import annotations

import asyncio
import math
import random
import sys
import uuid
from datetime import datetime

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo

from simulator.fraud_patterns import GroundTruth, _REGISTRY
from simulator.fraud_patterns.account_takeover import generate_account_takeover_fraud
from simulator.fraud_patterns.stolen_card import FraudPatternContext

LONDON_TZ_TEST = ZoneInfo("Europe/London")


def _ctx(seed: int) -> FraudPatternContext:
    return FraudPatternContext(
        now=datetime(2024, 5, 24, 12, 0, tzinfo=LONDON_TZ_TEST),
        rng=random.Random(seed),
    )


def test_ato_pattern_returns_ground_truth() -> None:
    order_dict, gt = asyncio.run(generate_account_takeover_fraud(_ctx(42)))
    assert isinstance(order_dict, dict)
    assert isinstance(gt, GroundTruth)
    assert gt.is_fraud is True
    assert gt.fraud_category == "account_takeover"
    assert "victim_user_id=" in gt.pattern_notes
    assert "ip_country=" in gt.pattern_notes
    assert "new_device" in gt.pattern_notes
    assert isinstance(order_dict["order_id"], uuid.UUID)


def test_ato_pattern_registered() -> None:
    assert "account_takeover" in _REGISTRY
    _fn, weight = _REGISTRY["account_takeover"]
    assert math.isclose(weight, 0.20, rel_tol=0, abs_tol=1e-12)


def test_ato_ip_country_distribution() -> None:
    expected_proportions = {
        "NG": 0.15,
        "RU": 0.12,
        "CN": 0.10,
        "US": 0.10,
        "VN": 0.08,
        "PK": 0.08,
        "UA": 0.07,
        "GB_different_city": 0.20,
        "other": 0.10,
    }
    counts = {country: 0 for country in expected_proportions}
    ctx = _ctx(123)
    for _ in range(1000):
        order_dict, _gt = asyncio.run(generate_account_takeover_fraud(ctx))
        ip_country = order_dict["ip_country"]
        assert ip_country in counts
        counts[ip_country] += 1

    total = 1000
    for country, expected in expected_proportions.items():
        fraction = counts[country] / total
        assert math.isclose(fraction, expected, rel_tol=0, abs_tol=0.03)


def test_ato_new_device_always() -> None:
    for _ in range(1000):
        order_dict, gt = asyncio.run(generate_account_takeover_fraud(_ctx(456)))
        assert "device_id" in order_dict
        assert order_dict["device_id"] is not None
        assert "new_device" in gt.pattern_notes


def test_ato_new_delivery_address_distribution() -> None:
    true_count = 0
    ctx = _ctx(789)
    for _ in range(1000):
        order_dict, _gt = asyncio.run(generate_account_takeover_fraud(ctx))
        if order_dict["is_new_delivery_address"] is True:
            true_count += 1

    fraction = true_count / 1000
    assert 0.85 <= fraction <= 0.95


def test_ato_payment_distribution() -> None:
    true_count = 0
    ctx = _ctx(101)
    for _ in range(1000):
        order_dict, _gt = asyncio.run(generate_account_takeover_fraud(ctx))
        if order_dict["is_new_payment_method"] is True:
            true_count += 1

    fraction = true_count / 1000
    assert 0.25 <= fraction <= 0.35


def test_ato_order_value_bimodal() -> None:
    normal_totals: list[int] = []
    high_value_totals: list[int] = []
    ctx = _ctx(202)
    for _ in range(1000):
        order_dict, _gt = asyncio.run(generate_account_takeover_fraud(ctx))
        order_value_mode = order_dict["order_value_mode"]
        order_total_pence = order_dict["order_total_pence"]
        if order_value_mode == "normal":
            normal_totals.append(order_total_pence)
        elif order_value_mode == "high_value":
            high_value_totals.append(order_total_pence)
        else:
            raise AssertionError(f"unexpected order_value_mode: {order_value_mode}")

    normal_count = len(normal_totals)
    high_value_count = len(high_value_totals)
    assert 420 <= normal_count <= 580
    assert 420 <= high_value_count <= 580

    normal_mode_mean = sum(normal_totals) / normal_count
    high_value_mean = sum(high_value_totals) / high_value_count
    assert normal_mode_mean < 5000
    assert high_value_mean > 5000
    assert high_value_mean > normal_mode_mean * 2.0
