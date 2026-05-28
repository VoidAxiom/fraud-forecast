from __future__ import annotations

import random
import uuid
from datetime import date, datetime, timedelta

from simulator.cart_builder import Cart
from simulator.generator import LONDON_TZ, _build_snapshot, current_rate


def test_current_rate_returns_higher_at_peak() -> None:
    target_date = date(2025, 1, 7)
    day_multiplier_cache = {target_date: 1.0}

    peak_rate = current_rate(
        now=datetime(2025, 1, 7, 19, 0, tzinfo=LONDON_TZ),
        multiplier=1.0,
        day_multiplier_cache=day_multiplier_cache,
    )
    overnight_rate = current_rate(
        now=datetime(2025, 1, 7, 4, 0, tzinfo=LONDON_TZ),
        multiplier=1.0,
        day_multiplier_cache=day_multiplier_cache,
    )

    assert peak_rate > overnight_rate


def test_current_rate_weekend_boost() -> None:
    tuesday = date(2025, 1, 7)
    friday = date(2025, 1, 10)
    day_multiplier_cache = {tuesday: 1.0, friday: 1.0}

    tuesday_rate = current_rate(
        now=datetime(2025, 1, 7, 19, 0, tzinfo=LONDON_TZ),
        multiplier=1.0,
        day_multiplier_cache=day_multiplier_cache,
    )
    friday_rate = current_rate(
        now=datetime(2025, 1, 10, 19, 0, tzinfo=LONDON_TZ),
        multiplier=1.0,
        day_multiplier_cache=day_multiplier_cache,
    )

    assert friday_rate > tuesday_rate


def test_day_multiplier_caching(monkeypatch: object) -> None:
    now = datetime(2025, 1, 8, 12, 0, tzinfo=LONDON_TZ)
    target_date = now.date()
    calls: list[int | float | str | bytes | bytearray | None] = []
    real_random = random.Random

    class _CountingRandom(real_random):
        def __init__(self, seed: int | float | str | bytes | bytearray | None = None) -> None:
            calls.append(seed)
            super().__init__(seed)

    monkeypatch.setattr("simulator.generator.random.Random", _CountingRandom)

    day_multiplier_cache: dict[date, float] = {}
    first_rate = current_rate(
        now=now,
        multiplier=1.0,
        day_multiplier_cache=day_multiplier_cache,
    )
    second_rate = current_rate(
        now=now,
        multiplier=1.0,
        day_multiplier_cache=day_multiplier_cache,
    )

    assert target_date in day_multiplier_cache
    assert first_rate == second_rate
    assert calls == [target_date.toordinal()]


def test_generate_order_uses_now_param() -> None:
    user_id = uuid.UUID(int=1)
    store_id = uuid.UUID(int=2)
    merchant_id = uuid.UUID(int=3)
    payment_method_id = uuid.UUID(int=4)
    device_id = uuid.UUID(int=5)
    placed_at = datetime(2025, 1, 7, 19, 0, tzinfo=LONDON_TZ)

    snapshot = _build_snapshot(
        user={
            "user_id": user_id,
            "email": "timing@example.com",
            "created_at": placed_at - timedelta(days=14),
            "risk_tier": "LOW",
        },
        store={
            "store_id": store_id,
            "merchant_id": merchant_id,
            "city": "London",
            "country": "GB",
            "latitude": 51.5074,
            "longitude": -0.1278,
        },
        cart=Cart(store_id=store_id, items=[]),
        delivery_address=None,
        payment_method={
            "payment_method_id": payment_method_id,
            "payment_type": "CREDIT_CARD",
        },
        device={
            "device_id": device_id,
            "device_type": "MOBILE_APP",
            "platform": "iOS",
        },
        ip_address="81.2.3.4",
        order_type="PICKUP",
        order_channel="IOS_APP",
        promo=None,
        applied_discount=0,
        pricing_tuple=(0, 0, 0, 0),
        user_total_orders_lifetime=0,
        user_total_orders_30d=0,
        user_total_spend_lifetime_pence=0,
        is_new_address=None,
        is_new_payment_method=False,
        rng=random.Random(7),
        placed_at=placed_at,
    )

    assert snapshot["user_account_age_days"] == 14
