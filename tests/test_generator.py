from __future__ import annotations

import asyncio
import inspect
import os
import random
import re
import uuid
from contextlib import ExitStack
from datetime import datetime, time, timezone
from typing import Optional
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import redis.asyncio as aioredis

from simulator.cart_builder import Cart
from simulator.fraud_patterns import GroundTruth
from simulator.generator import (
    _DEFAULT_LIVE_RATE_MULTIPLIER,
    _apply_fraud_identity_overrides,
    _apply_fraud_order_attrs,
    _parse_live_rate_multiplier_env,
    _parse_fraud_rate,
    _read_runtime_rate_optional,
    _resolve_card_country,
    _select_order_type,
    apply_promo,
    create_one_order,
    insert_order,
    load_active_promos,
    load_config_from_env,
    load_store_hours,
    load_stores_by_city,
    main,
    pick_store_for_user,
)


DATABASE_URL_SIMULATOR = os.getenv(
    "DATABASE_URL_SIMULATOR",
    "postgresql://simulator_user:simulator_dev_password@postgres:5432/fraud_platform",
)
DATABASE_URL_APP = os.getenv(
    "DATABASE_URL",
    "postgresql://app:app_dev_password@postgres:5432/fraud_platform",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

ORDER_NUMBER_RE = re.compile(r"^JE-\d{4}-[A-Z2-7]{10}$")


class _FixedUserPicker:
    def __init__(self, user_id: uuid.UUID) -> None:
        self._user_id = user_id

    def pick(self, _rng: random.Random) -> uuid.UUID:
        return self._user_id


class _FakePromoConn:
    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts

    async def fetchval(
        self,
        _query: str,
        _user_id: uuid.UUID,
        promo_code: str,
    ) -> int:
        return self.counts.get(promo_code, 0)


async def _pick_user(
    conn: asyncpg.Connection,
    *,
    require_no_prior_orders: bool,
) -> uuid.UUID:
    if require_no_prior_orders:
        row = await conn.fetchrow(
            """
            SELECT u.user_id
            FROM users u
            WHERE u.account_status = 'ACTIVE'
              AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.user_id)
              AND EXISTS (SELECT 1 FROM payment_methods pm WHERE pm.user_id = u.user_id)
            LIMIT 1
            """,
        )
        if row is not None:
            return row["user_id"]

    row = await conn.fetchrow(
        """
        SELECT u.user_id
        FROM users u
        WHERE u.account_status = 'ACTIVE'
          AND EXISTS (SELECT 1 FROM payment_methods pm WHERE pm.user_id = u.user_id)
        LIMIT 1
        """,
    )
    if row is not None:
        return row["user_id"]

    row = await conn.fetchrow(
        "SELECT u.user_id FROM users u WHERE u.account_status = 'ACTIVE' LIMIT 1",
    )
    if row is not None:
        return row["user_id"]

    raise RuntimeError("no eligible active user found for generator test")


async def _run_generator_batch(
    pool: asyncpg.Pool,
    redis: aioredis.Redis,
    *,
    sample_size: int,
    seed: int,
    fixed_user_id: uuid.UUID | None = None,
    require_no_prior_orders: bool = False,
) -> tuple[uuid.UUID, list[asyncpg.Record]]:
    stores_by_city = await load_stores_by_city(pool)
    store_hours_by_store_id = await load_store_hours(pool)
    promos = await load_active_promos(pool)

    async with pool.acquire() as conn:
        user_id = fixed_user_id or await _pick_user(
            conn,
            require_no_prior_orders=require_no_prior_orders,
        )
        before_order_count = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE user_id = $1",
            user_id,
        )
    picker = _FixedUserPicker(user_id)

    rng = random.Random(seed)
    for _ in range(sample_size):
        await create_one_order(
            pool=pool,
            user_picker=picker,
            stores_by_city=stores_by_city,
            promos=promos,
            store_hours_by_store_id=store_hours_by_store_id,
            rng=rng,
            scoring_enabled=False,
        )

    async with pool.acquire() as conn:
        after_order_count = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE user_id = $1",
            user_id,
        )

    created_count = int(after_order_count - before_order_count)
    assert created_count == sample_size

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT order_id, order_number, order_type, promo_code,
                   is_new_payment_method, payment_method_id
            FROM orders
            WHERE user_id = $1
            ORDER BY placed_at DESC, order_id DESC
            LIMIT $2
            """,
            user_id,
            created_count,
        )

    if len(rows) < sample_size:
        raise AssertionError("insufficient orders generated")

    rows = list(reversed(rows))
    return user_id, rows


def test_is_new_user_promo_logic() -> None:
    """is_new_user_promo is True only for WELCOME-prefixed promo codes."""

    welcome_promo = {"promo_code": "WELCOME10", "promo_id": uuid.uuid4()}
    other_promo = {"promo_code": "SUMMER20", "promo_id": uuid.uuid4()}
    none_promo = None

    def _check(promo) -> bool:
        return (
            promo is not None
            and isinstance(promo.get("promo_code"), str)
            and promo["promo_code"].startswith("WELCOME")
        )

    assert _check(welcome_promo) is True
    assert _check(other_promo) is False
    assert _check(none_promo) is False


def test_select_order_type_requires_pickup_for_fallback() -> None:
    rng = random.Random()
    store = {
        "accepts_delivery": False,
        "accepts_pickup": False,
        "accepts_in_store": False,
    }
    with patch.object(rng, "random", return_value=0.99), pytest.raises(
        RuntimeError,
        match="no eligible order type for store",
    ):
        _select_order_type(rng, store)


def test_pick_store_for_user_raises_when_no_store_open_now() -> None:
    rng = random.Random(42)
    store_id = uuid.uuid4()
    user_data = {
        "default_address": {
            "city": "London",
            "latitude": 51.5,
            "longitude": -0.1,
        },
    }
    stores_by_city = {
        "London": [
            {
                "store_id": store_id,
                "store_name": "Closed Bistro",
                "city": "London",
                "latitude": 51.503,
                "longitude": -0.12,
            },
        ],
    }
    mock_weekday = datetime(2026, 1, 1).isoweekday() % 7
    store_hours_by_store_id = {
        store_id: [
            {
                "day_of_week": mock_weekday,
                "open_time": time(8, 0),
                "close_time": time(10, 0),
            },
        ],
    }

    mock_now = datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc)
    with patch("simulator.generator.datetime.now", return_value=mock_now), pytest.raises(
        RuntimeError,
        match="no stores in current open-hours window",
    ):
        pick_store_for_user(rng, user_data, stores_by_city, store_hours_by_store_id)


def test_select_order_type_falls_back_to_pickup_when_only_pickup_enabled() -> None:
    rng = random.Random()
    store = {
        "accepts_delivery": False,
        "accepts_pickup": True,
        "accepts_in_store": False,
    }
    with patch.object(rng, "random", return_value=0.99):
        assert _select_order_type(rng, store) == "PICKUP"


def test_apply_promo_ignores_new_user_only_for_repeat_orders() -> None:
    async def _run() -> None:
        user_id = uuid.uuid4()
        conn = _FakePromoConn({"WELCOME10": 0, "SUMMER20": 0})
        promos = [
            {
                "promo_code": "WELCOME10",
                "promo_type": "NEW_USER",
                "min_order_pence": 0,
                "max_redemptions_per_user": 1,
            },
            {
                "promo_code": "SUMMER20",
                "promo_type": "PERCENT_OFF",
                "min_order_pence": 0,
                "max_redemptions_per_user": 1,
            },
        ]

        rng = random.Random(1)
        with patch.object(rng, "random", return_value=0.01), patch.object(
            rng,
            "choice",
            side_effect=lambda options: options[0],
        ):
            result = await apply_promo(
                conn,
                user_id,
                rng,
                False,
                promos,
                1000,
            )

        assert result is not None
        assert result["promo_code"] == "SUMMER20"

    asyncio.run(_run())


def test_apply_promo_enforces_max_redemptions_per_user() -> None:
    async def _run() -> None:
        user_id = uuid.uuid4()
        conn = _FakePromoConn({"WELCOME10": 2, "SUMMER20": 1})
        promos = [
            {
                "promo_code": "WELCOME10",
                "promo_type": "NEW_USER",
                "min_order_pence": 0,
                "max_redemptions_per_user": 1,
            },
            {
                "promo_code": "SUMMER20",
                "promo_type": "PERCENT_OFF",
                "min_order_pence": 0,
                "max_redemptions_per_user": 1,
            },
        ]

        rng = random.Random(1)
        with patch.object(rng, "random", return_value=0.01):
            result = await apply_promo(
                conn,
                user_id,
                rng,
                False,
                promos,
                1000,
            )

        assert result is None

    asyncio.run(_run())


def test_apply_promo_first_order_respects_max_redemptions_per_user() -> None:
    async def _run() -> None:
        user_id = uuid.uuid4()
        conn = _FakePromoConn({"WELCOME10": 1})
        promos = [
            {
                "promo_code": "WELCOME10",
                "promo_type": "NEW_USER",
                "min_order_pence": 0,
                "max_redemptions_per_user": 1,
            }
        ]

        rng = random.Random(1)
        with patch.object(rng, "random", return_value=0.01):
            result = await apply_promo(
                conn,
                user_id,
                rng,
                True,
                promos,
                1000,
            )

        assert result is None

    asyncio.run(_run())


def test_select_order_type_falls_back_to_dine_in_only_when_enabled() -> None:
    rng = random.Random()
    store = {
        "accepts_delivery": False,
        "accepts_pickup": False,
        "accepts_in_store": True,
    }

    with patch.object(rng, "random", return_value=0.74):
        assert _select_order_type(rng, store) == "DINE_IN"

    with patch.object(rng, "random", return_value=0.94):
        assert _select_order_type(rng, store) == "DINE_IN"


def test_select_order_type_raises_when_no_dine_in_is_eligible() -> None:
    rng = random.Random()
    store = {
        "accepts_delivery": False,
        "accepts_pickup": False,
        "accepts_in_store": False,
    }

    with patch.object(rng, "random", return_value=0.74), pytest.raises(
        RuntimeError,
        match="no eligible order type for store",
    ):
        _select_order_type(rng, store)

    with patch.object(rng, "random", return_value=0.94), pytest.raises(
        RuntimeError,
        match="no eligible order type for store",
    ):
        _select_order_type(rng, store)


def test_generator_creates_valid_order() -> None:
    async def _run() -> None:
        pool = await asyncpg.create_pool(DATABASE_URL_SIMULATOR, min_size=2, max_size=5)
        app_pool = await asyncpg.create_pool(DATABASE_URL_APP, min_size=1, max_size=3)
        redis = aioredis.from_url(REDIS_URL)
        try:
            _, order_rows = await _run_generator_batch(
                pool,
                redis,
                sample_size=1,
                seed=42,
            )

            assert len(order_rows) == 1
            order_ids: list[uuid.UUID] = [row["order_id"] for row in order_rows]

            async with app_pool.acquire() as conn:
                order_item_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM order_items WHERE order_id = ANY($1::uuid[])",
                    order_ids,
                )
                order_event_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM order_events
                    WHERE order_id = ANY($1::uuid[]) AND event_type = 'ORDER_PLACED'
                    """,
                    order_ids,
                )
                gt_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM sim.simulator_ground_truth
                    WHERE order_id = ANY($1::uuid[])
                    """,
                    order_ids,
                )

            assert order_item_count >= 1
            assert order_event_count == 1
            assert gt_count == 1
        finally:
            await redis.close()
            await app_pool.close()
            await pool.close()

    asyncio.run(_run())


def test_insert_order_builds_item_rows_with_cartitem_name() -> None:
    source = inspect.getsource(insert_order)
    assert "item.item_name" not in source
    assert "item.name" in source


def test_apply_fraud_order_attrs_maps_fields() -> None:
    snapshot = {
        "order_type": "DELIVERY",
        "card_issuer_country": "GB",
        "card_funding_type": "DEBIT",
        "is_digital_native_bank": False,
        "ip_country": "GB",
        "ip_is_proxy": True,
        "ip_is_vpn": False,
        "ip_is_tor": True,
        "ip_is_hosting": True,
        "delivery_address_type": "HOME",
        "is_new_payment_method": False,
        "total_pence": 2500,
    }
    fraud_dict = {
        "order_id": uuid.UUID(int=10),
        "order_total_pence": 9999,
        "card_country": "US",
        "card_funding_type": "CREDIT",
        "avs_result": "NO_MATCH",
        "cvv_result": "MATCH",
        "address_type": "HOTEL",
        "is_new_device": True,
        "ip_type": "vpn",
        "is_high_end_cart": True,
        "variant": "foreign_card",
        "is_digital_native_bank": True,
        "placed_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "is_night_order": True,
    }

    _apply_fraud_order_attrs(snapshot, fraud_dict)

    assert snapshot["card_issuer_country"] == "US"
    assert snapshot["card_funding_type"] == "CREDIT"
    assert snapshot["is_digital_native_bank"] is True
    assert snapshot["ip_country"] == "GB"
    assert snapshot["ip_is_vpn"] is True
    assert snapshot["ip_is_proxy"] is False
    assert snapshot["ip_is_tor"] is False
    assert snapshot["ip_is_hosting"] is False
    assert snapshot["delivery_address_type"] == "HOTEL"
    assert snapshot["total_pence"] == 9999
    assert snapshot["is_new_payment_method"] is False
    assert "order_id" not in snapshot
    assert "placed_at" not in snapshot
    assert "variant" not in snapshot
    assert snapshot["avs_result"] == "NO_MATCH"
    assert snapshot["cvv_result"] == "MATCH"
    assert "is_new_device" not in snapshot


def test_apply_fraud_order_attrs_propagates_identity_ids() -> None:
    """FK id overrides and total_pence override are applied; sentinel strings are skipped."""
    fraud_user_id = uuid.UUID(int=100)
    fraud_store_id = uuid.UUID(int=200)
    fraud_device_id = uuid.UUID(int=300)
    fraud_pm_id = uuid.UUID(int=400)
    fraud_addr_id = uuid.UUID(int=500)

    snapshot = {
        "user_id": uuid.UUID(int=1),
        "store_id": uuid.UUID(int=2),
        "device_id": uuid.UUID(int=3),
        "payment_method_id": uuid.UUID(int=4),
        "delivery_address_id": uuid.UUID(int=5),
        "total_pence": 1000,
        "order_type": "DELIVERY",
    }
    fraud_dict = {
        "user_id": fraud_user_id,
        "store_id": fraud_store_id,
        "device_id": fraud_device_id,
        "payment_method_id": str(fraud_pm_id),
        "delivery_address_id": fraud_addr_id,
        "order_total_pence": 8500,
        "avs_result": "NO_MATCH",
        "cvv_result": "NO_MATCH",
    }

    _apply_fraud_order_attrs(snapshot, fraud_dict)

    assert snapshot["user_id"] == fraud_user_id
    assert snapshot["store_id"] == fraud_store_id
    assert snapshot["device_id"] == fraud_device_id
    assert snapshot["payment_method_id"] == fraud_pm_id
    assert snapshot["delivery_address_id"] == fraud_addr_id
    assert snapshot["total_pence"] == 8500
    assert snapshot["avs_result"] == "NO_MATCH"
    assert snapshot["cvv_result"] == "NO_MATCH"


def test_apply_fraud_order_attrs_uses_victim_user_id() -> None:
    victim_user_id = uuid.UUID(int=600)
    snapshot = {
        "user_id": uuid.UUID(int=1),
        "total_pence": 1000,
        "order_type": "DELIVERY",
    }
    fraud_dict = {
        "victim_user_id": str(victim_user_id),
        "order_total_pence": 8500,
    }

    _apply_fraud_order_attrs(snapshot, fraud_dict)

    assert snapshot["user_id"] == victim_user_id
    assert snapshot["total_pence"] == 8500


def test_apply_fraud_identity_overrides_synthesizes_missing_user_row() -> None:
    async def _run() -> None:
        class _MissingUserConn:
            async def fetchrow(self, _query: str, *_args: object) -> None:
                return None

        fraud_user_id = uuid.UUID(int=1000)
        snapshot = {
            "user_id": uuid.UUID(int=1),
            "user_email": "legit@example.com",
            "user_email_domain": "example.com",
            "user_phone": "07123456789",
            "user_risk_tier_at_order": "LOW",
            "user_account_age_days": 365,
            "user_total_orders_lifetime": 42,
            "user_total_orders_30d": 8,
            "user_total_spend_lifetime_pence": 123456,
            "is_first_order_for_user": False,
        }
        fraud_dict = {"user_id": fraud_user_id}

        _apply_fraud_order_attrs(snapshot, fraud_dict)
        await _apply_fraud_identity_overrides(_MissingUserConn(), snapshot, fraud_dict)

        assert snapshot["user_id"] == fraud_user_id
        assert snapshot["user_email"] == f"{fraud_user_id}@fraud.test"
        assert snapshot["user_email_domain"] == "fraud.test"
        assert snapshot["user_phone"] is None
        assert snapshot["user_risk_tier_at_order"] is None
        assert snapshot["user_account_age_days"] == 0
        assert snapshot["user_total_orders_lifetime"] == 0
        assert snapshot["user_total_orders_30d"] == 0
        assert snapshot["user_total_spend_lifetime_pence"] == 0
        assert snapshot["is_first_order_for_user"] is True

    asyncio.run(_run())


def test_apply_fraud_identity_overrides_synthesizes_missing_victim_user_row() -> None:
    async def _run() -> None:
        class _MissingVictimUserConn:
            def __init__(self) -> None:
                self.requested_user_id: uuid.UUID | None = None

            async def fetchrow(self, _query: str, user_id: uuid.UUID) -> None:
                self.requested_user_id = user_id
                return None

        victim_user_id = uuid.UUID(int=1001)
        snapshot = {
            "user_id": uuid.UUID(int=1),
            "user_email": "legit@example.com",
            "user_email_domain": "example.com",
            "user_phone": "07123456789",
            "user_risk_tier_at_order": "LOW",
            "user_account_age_days": 365,
            "user_total_orders_lifetime": 42,
            "user_total_orders_30d": 8,
            "user_total_spend_lifetime_pence": 123456,
            "is_first_order_for_user": False,
        }
        fraud_dict = {"victim_user_id": victim_user_id}
        conn = _MissingVictimUserConn()

        _apply_fraud_order_attrs(snapshot, fraud_dict)
        await _apply_fraud_identity_overrides(conn, snapshot, fraud_dict)

        assert conn.requested_user_id == victim_user_id
        assert snapshot["user_id"] == victim_user_id
        assert snapshot["user_email"] == f"{victim_user_id}@fraud.test"
        assert snapshot["user_email_domain"] == "fraud.test"
        assert snapshot["user_account_age_days"] == 0
        assert snapshot["user_total_orders_lifetime"] == 0
        assert snapshot["user_total_orders_30d"] == 0
        assert snapshot["user_total_spend_lifetime_pence"] == 0
        assert snapshot["is_first_order_for_user"] is True

    asyncio.run(_run())


def test_apply_fraud_identity_overrides_synthesizes_missing_store_required_snapshot() -> None:
    async def _run() -> None:
        class _MissingStoreConn:
            def __init__(self) -> None:
                self.requested_store_id: uuid.UUID | None = None

            async def fetchrow(self, _query: str, store_id: uuid.UUID) -> None:
                self.requested_store_id = store_id
                return None

        fraud_store_id = uuid.UUID(int=2001)
        snapshot = {
            "store_id": uuid.UUID(int=2),
            "merchant_id": uuid.UUID(int=3),
            "store_city": "London",
            "store_country": "GB",
            "store_latitude": 51.5,
            "store_longitude": -0.1,
        }
        fraud_dict = {"store_id": str(fraud_store_id)}
        conn = _MissingStoreConn()

        _apply_fraud_order_attrs(snapshot, fraud_dict)
        await _apply_fraud_identity_overrides(conn, snapshot, fraud_dict)

        assert conn.requested_store_id == fraud_store_id
        assert snapshot["store_id"] == fraud_store_id
        assert snapshot["merchant_id"] == uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
        assert snapshot["store_city"] == "FRAUD_RING"
        assert snapshot["store_country"] == "GB"
        assert snapshot["store_latitude"] == 0.0
        assert snapshot["store_longitude"] == 0.0

    asyncio.run(_run())


def test_apply_fraud_order_attrs_skips_sentinel_ids() -> None:
    """Sentinel strings VICTIM_SAVED / ABUSER_SAVED are not written to snapshot."""
    legit_pm_id = uuid.UUID(int=4)
    legit_addr_id = uuid.UUID(int=5)

    snapshot = {
        "payment_method_id": legit_pm_id,
        "delivery_address_id": legit_addr_id,
    }
    fraud_dict = {
        "payment_method_id": "VICTIM_SAVED",
        "delivery_address_id": "ABUSER_SAVED",
    }

    _apply_fraud_order_attrs(snapshot, fraud_dict)

    assert snapshot["payment_method_id"] == legit_pm_id
    assert snapshot["delivery_address_id"] == legit_addr_id


def test_apply_fraud_order_attrs_uses_explicit_ip_country() -> None:
    snapshot = {"ip_country": "GB"}

    _apply_fraud_order_attrs(snapshot, {"ip_country": "NG"})

    assert snapshot["ip_country"] == "NG"


def test_apply_fraud_order_attrs_prefers_explicit_ip_country_over_ip_type() -> None:
    snapshot = {
        "ip_country": "GB",
        "ip_is_proxy": True,
        "ip_is_vpn": False,
        "ip_is_tor": True,
        "ip_is_hosting": True,
    }

    _apply_fraud_order_attrs(snapshot, {"ip_country": "RU", "ip_type": "vpn"})

    assert snapshot["ip_country"] == "RU"
    assert snapshot["ip_is_vpn"] is True
    assert snapshot["ip_is_proxy"] is False
    assert snapshot["ip_is_tor"] is False
    assert snapshot["ip_is_hosting"] is False


def test_apply_fraud_order_attrs_invalid_explicit_ip_country_falls_back_to_gb() -> None:
    snapshot = {"ip_country": "NG"}

    _apply_fraud_order_attrs(snapshot, {"ip_country": "unknown"})

    assert snapshot["ip_country"] == "GB"


def test_apply_fraud_order_attrs_resolves_card_country_descriptors() -> None:
    snapshot = {"card_issuer_country": "GB"}

    _apply_fraud_order_attrs(snapshot, {"card_country": "GB_different_city"})
    assert snapshot["card_issuer_country"] == "GB"

    _apply_fraud_order_attrs(snapshot, {"card_country": "foreign_other"})
    assert snapshot["card_issuer_country"] == "BR"


def test_resolve_card_country_normalizes_iso2_case() -> None:
    assert _resolve_card_country("us") == "US"


def test_parse_fraud_rate_defaults_and_clamps() -> None:
    assert _parse_fraud_rate(None) == 0.02
    assert _parse_fraud_rate("") == 0.02
    assert _parse_fraud_rate("2%") == 0.02
    assert _parse_fraud_rate("-0.5") == 0.0
    assert _parse_fraud_rate("1.5") == 1.0
    assert _parse_fraud_rate("0.25") == 0.25


def test_generate_order_uses_now_param() -> None:
    async def _run() -> None:
        injected_now = datetime(2025, 1, 7, 19, 0, tzinfo=timezone.utc)
        user_id = uuid.UUID(int=1)
        store_id = uuid.UUID(int=2)
        payment_method_id = uuid.UUID(int=3)
        device_id = uuid.UUID(int=4)
        user_data = {
            "user": {"user_id": user_id},
            "addresses": [],
            "default_address": {"city": "London"},
            "devices": [{"device_id": device_id}],
            "payment_methods": [{"payment_method_id": payment_method_id}],
        }
        store = {
            "store_id": store_id,
            "accepts_in_store": False,
        }
        cart = Cart(store_id=store_id, items=[])
        captured_snapshot_placed_at: list[datetime] = []
        captured_insert_placed_at: list[datetime] = []

        class _FakeAcquire:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, *_args: object) -> None:
                return None

        class _FakePool:
            def acquire(self) -> _FakeAcquire:
                return _FakeAcquire()

        async def fake_load_user_data(_conn: object, _user_id: uuid.UUID) -> dict[str, object]:
            return user_data

        async def fake_is_new_payment_method(
            _conn: object,
            _user_id: uuid.UUID,
            _payment_method_id: uuid.UUID,
        ) -> bool:
            return False

        async def fake_load_menu_items(_conn: object, _store_id: uuid.UUID) -> list[object]:
            return [object()]

        async def fake_read_user_order_metrics(
            _conn: object,
            _user_id: uuid.UUID,
        ) -> tuple[int, int, int]:
            return 0, 0, 0

        async def fake_apply_promo(
            _conn: object,
            _user_id: uuid.UUID,
            _rng: random.Random,
            _is_first_order_for_user: bool,
            _promos: list[dict[str, object]],
            _subtotal_pence: int,
        ) -> None:
            return None

        def fake_build_snapshot(**kwargs: object) -> dict[str, object]:
            placed_at = kwargs["placed_at"]
            assert isinstance(placed_at, datetime)
            captured_snapshot_placed_at.append(placed_at)
            return {}

        async def fake_insert_order(
            _conn: object,
            _snapshot: dict[str, object],
            _cart: Cart,
            placed_at: datetime,
            *,
            is_fraud: bool = False,
            fraud_category: str | None = None,
            pattern_notes: str | None = None,
            ring_id: uuid.UUID | None = None,
            rng: Optional[random.Random] = None,
        ) -> tuple[uuid.UUID, datetime]:
            assert not is_fraud
            assert fraud_category is None
            assert pattern_notes is None
            assert ring_id is None
            captured_insert_placed_at.append(placed_at)
            return uuid.UUID(int=99), placed_at

        async def fake_notify_order_placed(_conn: object, _order_id: uuid.UUID) -> None:
            return None

        rng = random.Random(123)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"FRAUD_INJECTION_RATE": "0.0"}))
            stack.enter_context(patch("simulator.generator.load_user_data", fake_load_user_data))
            stack.enter_context(
                patch("simulator.generator.pick_store_for_user", return_value=store)
            )
            stack.enter_context(
                patch("simulator.generator._select_order_type", return_value="PICKUP")
            )
            stack.enter_context(
                patch("simulator.generator.pick_channel_for_user", return_value="WEB")
            )
            stack.enter_context(
                patch(
                    "simulator.generator.pick_device_and_ip",
                    return_value=({"device_id": device_id}, "81.2.3.4"),
                )
            )
            stack.enter_context(
                patch("simulator.generator._is_new_payment_method", fake_is_new_payment_method)
            )
            stack.enter_context(patch("simulator.generator._load_menu_items", fake_load_menu_items))
            stack.enter_context(
                patch("simulator.generator.build_realistic_cart", return_value=cart)
            )
            stack.enter_context(
                patch("simulator.generator._read_user_order_metrics", fake_read_user_order_metrics)
            )
            stack.enter_context(patch("simulator.generator.apply_promo", fake_apply_promo))
            stack.enter_context(
                patch("simulator.generator.compute_pricing", return_value=(0, 0, 0, 0))
            )
            stack.enter_context(patch("simulator.generator._build_snapshot", fake_build_snapshot))
            stack.enter_context(
                patch(
                    "simulator.generator.generate_order_number",
                    return_value="JE-2025-AAAAAAAAAA",
                )
            )
            stack.enter_context(patch("simulator.generator.insert_order", fake_insert_order))
            stack.enter_context(
                patch("simulator.generator.notify_order_placed", fake_notify_order_placed)
            )

            await create_one_order(
                pool=_FakePool(),
                user_picker=_FixedUserPicker(user_id),
                stores_by_city={"London": [store]},
                store_hours_by_store_id={store_id: []},
                promos=[],
                rng=rng,
                scoring_enabled=False,
                now=injected_now,
            )

        assert captured_snapshot_placed_at == [injected_now]
        assert captured_insert_placed_at == [injected_now]

    asyncio.run(_run())


def test_fraud_injection_rate_over_500_orders() -> None:
    async def _run() -> None:
        sample_size = 500
        fraud_rate = 0.02
        tolerance = 0.005
        fraud_target = int(sample_size * fraud_rate)
        user_id = uuid.UUID(int=1)
        store_id = uuid.UUID(int=2)
        payment_method_id = uuid.UUID(int=3)
        device_id = uuid.UUID(int=4)
        ring_id = uuid.UUID(int=5)
        user_data = {
            "user": {"user_id": user_id},
            "addresses": [],
            "default_address": {"city": "London"},
            "devices": [{"device_id": device_id}],
            "payment_methods": [{"payment_method_id": payment_method_id}],
        }
        store = {
            "store_id": store_id,
            "accepts_in_store": False,
        }
        cart = Cart(store_id=store_id, items=[])
        fraud_rolls = [
            (fraud_rate / 2 if index < fraud_target else fraud_rate + tolerance + 0.1)
            for index in range(sample_size)
        ]
        random_rolls: list[float] = []
        for fraud_roll in fraud_rolls:
            random_rolls.extend([fraud_roll, 0.10])

        inserted_fraud_flags: list[bool] = []

        class _FakeAcquire:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, *_args: object) -> None:
                return None

        class _FakePool:
            def acquire(self) -> _FakeAcquire:
                return _FakeAcquire()

        async def fake_load_user_data(_conn: object, _user_id: uuid.UUID) -> dict[str, object]:
            return user_data

        async def fake_is_new_payment_method(
            _conn: object,
            _user_id: uuid.UUID,
            _payment_method_id: uuid.UUID,
        ) -> bool:
            return False

        async def fake_load_menu_items(_conn: object, _store_id: uuid.UUID) -> list[object]:
            return [object()]

        async def fake_read_user_order_metrics(
            _conn: object,
            _user_id: uuid.UUID,
        ) -> tuple[int, int, int]:
            return 0, 0, 0

        async def fake_apply_promo(
            _conn: object,
            _user_id: uuid.UUID,
            _rng: random.Random,
            _is_first_order_for_user: bool,
            _promos: list[dict[str, object]],
            _subtotal_pence: int,
        ) -> None:
            return None

        async def fake_insert_order(
            _conn: object,
            _snapshot: dict[str, object],
            _cart: Cart,
            placed_at: datetime,
            *,
            is_fraud: bool = False,
            fraud_category: str | None = None,
            pattern_notes: str | None = None,
            ring_id: uuid.UUID | None = None,
            rng: Optional[random.Random] = None,
        ) -> tuple[uuid.UUID, datetime]:
            inserted_fraud_flags.append(is_fraud)
            if is_fraud:
                assert fraud_category == "stolen_card"
                assert pattern_notes == "stubbed fraud pattern"
                assert ring_id == uuid.UUID(int=5)
            else:
                assert fraud_category is None
                assert pattern_notes is None
                assert ring_id is None
            return uuid.UUID(int=len(inserted_fraud_flags)), placed_at

        async def fake_notify_order_placed(_conn: object, _order_id: uuid.UUID) -> None:
            return None

        fraud_dispatcher = AsyncMock(
            return_value=(
                {},
                GroundTruth(
                    order_id=uuid.UUID(int=9),
                    is_fraud=True,
                    fraud_category="stolen_card",
                    pattern_notes="stubbed fraud pattern",
                    ring_id=ring_id,
                ),
            )
        )
        rng = random.Random(123)

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"FRAUD_INJECTION_RATE": str(fraud_rate)}))
            stack.enter_context(patch.object(rng, "random", side_effect=random_rolls))
            stack.enter_context(patch("simulator.generator.load_user_data", fake_load_user_data))
            stack.enter_context(
                patch("simulator.generator.pick_store_for_user", return_value=store)
            )
            stack.enter_context(
                patch("simulator.generator._select_order_type", return_value="PICKUP")
            )
            stack.enter_context(
                patch("simulator.generator.pick_channel_for_user", return_value="WEB")
            )
            stack.enter_context(
                patch(
                    "simulator.generator.pick_device_and_ip",
                    return_value=({"device_id": device_id}, "81.2.3.4"),
                )
            )
            stack.enter_context(
                patch("simulator.generator._is_new_payment_method", fake_is_new_payment_method)
            )
            stack.enter_context(patch("simulator.generator._load_menu_items", fake_load_menu_items))
            stack.enter_context(
                patch("simulator.generator.build_realistic_cart", return_value=cart)
            )
            stack.enter_context(
                patch("simulator.generator._read_user_order_metrics", fake_read_user_order_metrics)
            )
            stack.enter_context(patch("simulator.generator.apply_promo", fake_apply_promo))
            stack.enter_context(
                patch("simulator.generator.compute_pricing", return_value=(0, 0, 0, 0))
            )
            stack.enter_context(patch("simulator.generator._build_snapshot", return_value={}))
            stack.enter_context(
                patch(
                    "simulator.generator.generate_order_number",
                    return_value="JE-0000-AAAAAAAAAA",
                )
            )
            stack.enter_context(patch("simulator.generator.generate_fraud_order", fraud_dispatcher))
            stack.enter_context(patch("simulator.generator.insert_order", fake_insert_order))
            stack.enter_context(
                patch("simulator.generator.notify_order_placed", fake_notify_order_placed)
            )

            for _ in range(sample_size):
                await create_one_order(
                    pool=_FakePool(),
                    user_picker=_FixedUserPicker(user_id),
                    stores_by_city={"London": [store]},
                    store_hours_by_store_id={store_id: []},
                    promos=[],
                    rng=rng,
                    scoring_enabled=False,
                )

        fraud_count = sum(1 for is_fraud in inserted_fraud_flags if is_fraud)
        injection_rate = fraud_count / sample_size

        assert len(inserted_fraud_flags) == sample_size
        assert abs(injection_rate - fraud_rate) <= tolerance
        assert fraud_dispatcher.await_count == fraud_count

    asyncio.run(_run())


def test_fraud_order_propagates_avs_cvv_to_insert_order() -> None:
    """avs_result and cvv_result from fraud_dict reach the snapshot passed to insert_order."""

    async def _run() -> None:
        import uuid as _uuid

        user_id = _uuid.UUID(int=1)
        store_id = _uuid.UUID(int=2)
        payment_method_id = _uuid.UUID(int=3)
        device_id = _uuid.UUID(int=4)
        ring_id = _uuid.UUID(int=5)
        user_data: dict[str, object] = {
            "user": {"user_id": user_id},
            "addresses": [],
            "default_address": {"city": "London"},
            "devices": [{"device_id": device_id}],
            "payment_methods": [{"payment_method_id": payment_method_id}],
        }
        store: dict[str, object] = {
            "store_id": store_id,
            "accepts_in_store": False,
        }
        cart = Cart(store_id=store_id, items=[])

        captured_snapshots: list[dict[str, object]] = []

        class _FakeAcquire:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, *_args: object) -> None:
                return None

        class _FakePool:
            def acquire(self) -> _FakeAcquire:
                return _FakeAcquire()

        async def fake_load_user_data(_conn: object, _uid: uuid.UUID) -> dict[str, object]:
            return user_data

        async def fake_is_new_payment_method(
            _conn: object, _uid: uuid.UUID, _pmid: uuid.UUID
        ) -> bool:
            return False

        async def fake_load_menu_items(_conn: object, _sid: uuid.UUID) -> list[object]:
            return [object()]

        async def fake_read_user_order_metrics(
            _conn: object, _uid: uuid.UUID
        ) -> tuple[int, int, int]:
            return 0, 0, 0

        async def fake_apply_promo(
            _conn: object,
            _uid: uuid.UUID,
            _rng: random.Random,
            _first: bool,
            _promos: list[dict[str, object]],
            _sub: int,
        ) -> None:
            return None

        async def fake_insert_order(
            _conn: object,
            snapshot: dict[str, object],
            _cart: Cart,
            _placed_at: datetime,
            *,
            is_fraud: bool = False,
            fraud_category: str | None = None,
            pattern_notes: str | None = None,
            ring_id: uuid.UUID | None = None,
            rng: Optional[random.Random] = None,
        ) -> tuple[uuid.UUID, datetime]:
            captured_snapshots.append(dict(snapshot))
            return uuid.UUID(int=99), datetime.now(tz=timezone.utc)

        async def fake_notify_order_placed(_conn: object, _oid: uuid.UUID) -> None:
            return None

        async def fake_identity_overrides(
            _conn: object,
            _snapshot: dict[str, object],
            _fraud_dict: dict[str, object],
        ) -> None:
            return None

        fraud_dict: dict[str, object] = {
            "avs_result": "NO_MATCH",
            "cvv_result": "NO_MATCH",
            "order_total_pence": 7500,
        }
        fraud_dispatcher = AsyncMock(
            return_value=(
                fraud_dict,
                GroundTruth(
                    order_id=uuid.UUID(int=9),
                    is_fraud=True,
                    fraud_category="stolen_card",
                    pattern_notes="stubbed",
                    ring_id=ring_id,
                ),
            )
        )
        rng = random.Random(123)
        identity_overrides_target = (
            f"{_apply_fraud_identity_overrides.__module__}._apply_fraud_identity_overrides"
        )

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"FRAUD_INJECTION_RATE": "1.0"}))
            stack.enter_context(patch("simulator.generator.load_user_data", fake_load_user_data))
            stack.enter_context(
                patch("simulator.generator.pick_store_for_user", return_value=store)
            )
            stack.enter_context(
                patch("simulator.generator._select_order_type", return_value="PICKUP")
            )
            stack.enter_context(
                patch("simulator.generator.pick_channel_for_user", return_value="WEB")
            )
            stack.enter_context(
                patch(
                    "simulator.generator.pick_device_and_ip",
                    return_value=({"device_id": device_id}, "81.2.3.4"),
                )
            )
            stack.enter_context(
                patch("simulator.generator._is_new_payment_method", fake_is_new_payment_method)
            )
            stack.enter_context(patch("simulator.generator._load_menu_items", fake_load_menu_items))
            stack.enter_context(
                patch("simulator.generator.build_realistic_cart", return_value=cart)
            )
            stack.enter_context(
                patch("simulator.generator._read_user_order_metrics", fake_read_user_order_metrics)
            )
            stack.enter_context(patch("simulator.generator.apply_promo", fake_apply_promo))
            stack.enter_context(
                patch("simulator.generator.compute_pricing", return_value=(0, 0, 0, 0))
            )
            stack.enter_context(patch("simulator.generator._build_snapshot", return_value={}))
            stack.enter_context(
                patch(
                    "simulator.generator.generate_order_number",
                    return_value="JE-0000-AAAAAAAAAA",
                )
            )
            stack.enter_context(patch("simulator.generator.generate_fraud_order", fraud_dispatcher))
            stack.enter_context(patch(identity_overrides_target, fake_identity_overrides))
            stack.enter_context(patch("simulator.generator.insert_order", fake_insert_order))
            stack.enter_context(
                patch("simulator.generator.notify_order_placed", fake_notify_order_placed)
            )

            await create_one_order(
                pool=_FakePool(),
                user_picker=_FixedUserPicker(user_id),
                stores_by_city={"London": [store]},
                store_hours_by_store_id={store_id: []},
                promos=[],
                rng=rng,
                scoring_enabled=False,
            )

        assert len(captured_snapshots) == 1
        snap = captured_snapshots[0]
        assert snap.get("avs_result") == "NO_MATCH", f"avs_result missing from snapshot: {snap}"
        assert snap.get("cvv_result") == "NO_MATCH", f"cvv_result missing from snapshot: {snap}"
        assert snap.get("total_pence") == 7500, f"total_pence not overridden: {snap}"

    asyncio.run(_run())


def test_generator_order_number_unique() -> None:
    async def _run() -> None:
        pool = await asyncpg.create_pool(DATABASE_URL_SIMULATOR, min_size=2, max_size=5)
        redis = aioredis.from_url(REDIS_URL)
        try:
            _, order_rows = await _run_generator_batch(
                pool,
                redis,
                sample_size=100,
                seed=101,
            )

            order_numbers = [row["order_number"] for row in order_rows]
            assert len(order_numbers) == 100
            assert len(set(order_numbers)) == 100
            assert all(
                ORDER_NUMBER_RE.match(order_number) is not None for order_number in order_numbers
            )
        finally:
            await redis.close()
            await pool.close()

    asyncio.run(_run())


def test_generator_order_type_distribution() -> None:
    async def _run() -> None:
        pool = await asyncpg.create_pool(DATABASE_URL_SIMULATOR, min_size=2, max_size=5)
        redis = aioredis.from_url(REDIS_URL)
        try:
            _, order_rows = await _run_generator_batch(
                pool,
                redis,
                sample_size=100,
                seed=202,
            )

            order_types = [row["order_type"] for row in order_rows]
            delivery_count = order_types.count("DELIVERY")
            pickup_count = order_types.count("PICKUP")
            dine_in_count = order_types.count("DINE_IN")

            assert 65 <= delivery_count <= 85
            assert 10 <= pickup_count <= 30
            assert 0 <= dine_in_count <= 15
            assert delivery_count + pickup_count + dine_in_count == 100
        finally:
            await redis.close()
            await pool.close()

    asyncio.run(_run())


def test_generator_payment_new_card_distribution() -> None:
    async def _run() -> None:
        pool = await asyncpg.create_pool(DATABASE_URL_SIMULATOR, min_size=2, max_size=5)
        redis = aioredis.from_url(REDIS_URL)
        try:
            async with pool.acquire() as conn:
                user_id = await _pick_user(conn, require_no_prior_orders=False)
                payment_methods_before = await conn.fetchval(
                    "SELECT COUNT(*) FROM payment_methods WHERE user_id = $1",
                    user_id,
                )

            _, order_rows = await _run_generator_batch(
                pool,
                redis,
                sample_size=100,
                seed=303,
                fixed_user_id=user_id,
            )

            async with pool.acquire() as conn:
                payment_methods_after = await conn.fetchval(
                    "SELECT COUNT(*) FROM payment_methods WHERE user_id = $1",
                    user_id,
                )

            new_payment_methods = int(payment_methods_after - payment_methods_before)
            assert 0 <= new_payment_methods <= 15
            assert len(order_rows) == 100
        finally:
            await redis.close()
            await pool.close()

    asyncio.run(_run())


def test_generator_promo_application() -> None:
    async def _run() -> None:
        pool = await asyncpg.create_pool(DATABASE_URL_SIMULATOR, min_size=2, max_size=5)
        redis = aioredis.from_url(REDIS_URL)
        try:
            _, order_rows = await _run_generator_batch(
                pool,
                redis,
                sample_size=100,
                seed=404,
                require_no_prior_orders=True,
            )

            welcome_count = sum(1 for row in order_rows if row["promo_code"] == "WELCOME10")
            assert 0 < welcome_count <= 90
        finally:
            await redis.close()
            await pool.close()

    asyncio.run(_run())


def test_generator_notify_fires() -> None:
    async def _run() -> None:
        pool = await asyncpg.create_pool(DATABASE_URL_SIMULATOR, min_size=2, max_size=5)
        redis = aioredis.from_url(REDIS_URL)
        notifier: asyncpg.Connection | None = None
        try:
            notifier = await asyncpg.connect(DATABASE_URL_SIMULATOR)
            stores_by_city = await load_stores_by_city(pool)
            store_hours_by_store_id = await load_store_hours(pool)
            promos = await load_active_promos(pool)

            async with pool.acquire() as conn:
                user_id = await _pick_user(conn, require_no_prior_orders=False)
            picker = _FixedUserPicker(user_id)

            event = asyncio.Event()
            received: list[str] = []

            def on_notify(
                _conn: asyncpg.Connection, _pid: int, _channel: str, payload: str
            ) -> None:
                received.append(payload)
                event.set()

            await notifier.add_listener("order_placed", on_notify)

            rng = random.Random(505)
            await create_one_order(
                pool=pool,
                user_picker=picker,
                stores_by_city=stores_by_city,
                promos=promos,
                store_hours_by_store_id=store_hours_by_store_id,
                rng=rng,
                scoring_enabled=False,
            )
            await asyncio.wait_for(event.wait(), timeout=2.0)
        finally:
            if notifier is not None:
                await notifier.remove_listener("order_placed", on_notify)
                await notifier.close()
            await redis.close()
            await pool.close()

        assert received

        for payload in received:
            raw_payload = payload.decode() if isinstance(payload, bytes) else payload
            uuid.UUID(raw_payload)

    asyncio.run(_run())


def test_live_rate_multiplier_preserved_across_report_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StopMain(Exception):
        pass

    class _FakeAcquire:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _traceback: object,
        ) -> None:
            return None

    class _FakePool:
        def acquire(self) -> _FakeAcquire:
            return _FakeAcquire()

        async def close(self) -> None:
            return None

    class _FakeRedis:
        async def close(self) -> None:
            return None

    class _FakeUserPicker:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def refresh(self) -> None:
            return None

    async def _run() -> None:
        fake_pool = _FakePool()
        fake_redis = _FakeRedis()
        runtime_rate_mock = AsyncMock(return_value=999)
        original_sleep = asyncio.sleep
        report_seen = False
        sleep_calls = 0
        seen_multipliers: list[float] = []

        async def _sleep(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            await original_sleep(0)
            if report_seen:
                raise _StopMain
            if sleep_calls > 200:
                raise AssertionError("throughput report block was not reached")

        def _record_report(_message: str) -> None:
            nonlocal report_seen
            report_seen = True

        def _current_rate(
            *,
            now: object,
            multiplier: float,
            day_multiplier_cache: object,
        ) -> float:
            del now, day_multiplier_cache
            seen_multipliers.append(multiplier)
            return 1000.0

        monkeypatch.setenv("LIVE_RATE_MULTIPLIER", "2.0")
        monkeypatch.setenv("ORDERS_PER_SECOND", "1")
        config = load_config_from_env()

        with ExitStack() as stack:
            stack.enter_context(
                patch("simulator.generator.load_config_from_env", return_value=config)
            )
            stack.enter_context(
                patch(
                    "simulator.generator.asyncpg.create_pool",
                    new=AsyncMock(return_value=fake_pool),
                )
            )
            stack.enter_context(
                patch("simulator.generator.aioredis.from_url", return_value=fake_redis)
            )
            stack.enter_context(
                patch(
                    "simulator.generator.load_stores_by_city",
                    new=AsyncMock(
                        return_value={"London": [{"store_id": uuid.uuid4()}]},
                    ),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.load_store_hours",
                    new=AsyncMock(return_value={}),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.load_active_promos",
                    new=AsyncMock(return_value=[]),
                )
            )
            stack.enter_context(
                patch("simulator.generator.WeightedUserPicker", new=_FakeUserPicker)
            )
            stack.enter_context(
                patch(
                    "simulator.generator.write_simulator_epoch",
                    new=AsyncMock(return_value="epoch"),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.init_collusive_stores",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.init_promo_abuse_rings",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.init_reseller_accounts",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.init_triangulation_accounts",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.create_one_order",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator._read_runtime_rate_optional",
                    new=runtime_rate_mock,
                )
            )
            stack.enter_context(
                patch("simulator.generator.current_rate", side_effect=_current_rate)
            )
            stack.enter_context(
                patch("simulator.generator.logger.info", side_effect=_record_report)
            )
            stack.enter_context(patch("simulator.generator.asyncio.sleep", side_effect=_sleep))

            with pytest.raises(_StopMain):
                await main()

        assert report_seen is True
        assert seen_multipliers[-1] == 2.0
        runtime_rate_mock.assert_not_awaited()

    asyncio.run(_run())


def test_live_rate_multiplier_invalid_does_not_block_redis_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StopMain(Exception):
        pass

    class _FakeAcquire:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(
            self,
            _exc_type: object,
            _exc: object,
            _traceback: object,
        ) -> None:
            return None

    class _FakePool:
        def acquire(self) -> _FakeAcquire:
            return _FakeAcquire()

        async def close(self) -> None:
            return None

    class _FakeRedis:
        async def close(self) -> None:
            return None

    class _FakeUserPicker:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def refresh(self) -> None:
            return None

    async def _run() -> None:
        fake_pool = _FakePool()
        fake_redis = _FakeRedis()
        runtime_rate_mock = AsyncMock(return_value=999)
        original_sleep = asyncio.sleep
        report_seen = False
        sleep_calls = 0

        async def _sleep(_delay: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            await original_sleep(0)
            if report_seen:
                raise _StopMain
            if sleep_calls > 200:
                raise AssertionError("throughput report block was not reached")

        def _record_report(_message: str) -> None:
            nonlocal report_seen
            report_seen = True

        monkeypatch.setenv("LIVE_RATE_MULTIPLIER", "not-a-float")
        monkeypatch.setenv("ORDERS_PER_SECOND", "1")
        config = load_config_from_env()

        with ExitStack() as stack:
            stack.enter_context(
                patch("simulator.generator.load_config_from_env", return_value=config)
            )
            stack.enter_context(
                patch(
                    "simulator.generator.asyncpg.create_pool",
                    new=AsyncMock(return_value=fake_pool),
                )
            )
            stack.enter_context(
                patch("simulator.generator.aioredis.from_url", return_value=fake_redis)
            )
            stack.enter_context(
                patch(
                    "simulator.generator.load_stores_by_city",
                    new=AsyncMock(
                        return_value={"London": [{"store_id": uuid.uuid4()}]},
                    ),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.load_store_hours",
                    new=AsyncMock(return_value={}),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.load_active_promos",
                    new=AsyncMock(return_value=[]),
                )
            )
            stack.enter_context(
                patch("simulator.generator.WeightedUserPicker", new=_FakeUserPicker)
            )
            stack.enter_context(
                patch(
                    "simulator.generator.write_simulator_epoch",
                    new=AsyncMock(return_value="epoch"),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.init_collusive_stores",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.init_promo_abuse_rings",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.init_reseller_accounts",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.init_triangulation_accounts",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator.create_one_order",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "simulator.generator._read_runtime_rate_optional",
                    new=runtime_rate_mock,
                )
            )
            stack.enter_context(patch("simulator.generator.current_rate", return_value=1000.0))
            stack.enter_context(
                patch("simulator.generator.logger.info", side_effect=_record_report)
            )
            stack.enter_context(patch("simulator.generator.asyncio.sleep", side_effect=_sleep))

            with pytest.raises(_StopMain):
                await main()

        assert report_seen is True
        runtime_rate_mock.assert_awaited_once_with(fake_redis)

    asyncio.run(_run())


def test_runtime_rate_optional_distinguishes_absent_from_explicit() -> None:
    """An explicit rate equal to ORDERS_PER_SECOND is not treated as absent."""

    async def _run() -> None:
        redis_conn = AsyncMock()
        redis_conn.get.return_value = None

        assert await _read_runtime_rate_optional(redis_conn) is None

        redis_conn.get.return_value = b"50"

        assert await _read_runtime_rate_optional(redis_conn) == 50

    asyncio.run(_run())


def test_live_rate_empty_string_uses_default_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="simulator.generator")
    multiplier, override = _parse_live_rate_multiplier_env("")
    assert multiplier == _DEFAULT_LIVE_RATE_MULTIPLIER == 0.02
    assert override is False
    assert not any("invalid_live_rate_multiplier" in r.getMessage() for r in caplog.records)


def test_live_rate_unset_uses_default_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="simulator.generator")
    multiplier, override = _parse_live_rate_multiplier_env(None)
    assert multiplier == _DEFAULT_LIVE_RATE_MULTIPLIER == 0.02
    assert override is False
    assert not any("invalid_live_rate_multiplier" in r.getMessage() for r in caplog.records)


def test_live_rate_invalid_string_warns_and_falls_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="simulator.generator")
    multiplier, override = _parse_live_rate_multiplier_env("foo")
    assert multiplier == _DEFAULT_LIVE_RATE_MULTIPLIER == 0.02
    assert override is False
    warnings = [r for r in caplog.records if "invalid_live_rate_multiplier" in r.getMessage()]
    assert len(warnings) == 1


def test_live_rate_valid_value_used(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING", logger="simulator.generator")
    multiplier, override = _parse_live_rate_multiplier_env("0.05")
    assert multiplier == 0.05
    assert override is True
    assert not any("invalid_live_rate_multiplier" in r.getMessage() for r in caplog.records)
