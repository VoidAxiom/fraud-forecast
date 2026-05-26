from __future__ import annotations

import asyncio
import datetime as dt
import os
import random
import time
from dataclasses import dataclass
from typing import Any, List, cast
from uuid import UUID, uuid4

import asyncpg
import pytest
import redis as redis_lib

from shared.db import get_session
from shared.models import Device, PaymentMethod, Session as SessionModel, Store, User, UserAddress
from shared.money import VATLineItem, calculate_vat
from simulator.cart_builder import Cart, CartItem, MenuItemLike, UserProfile, build_realistic_cart
from simulator.generator import (
    LONDON_TZ,
    create_one_order,
    generate_order_number,
    load_active_promos,
    pick_store_for_user,
)
from simulator.snapshot_builder import IPAddress, build_order_snapshot
from simulator.user_picker import WeightedUserPicker


DATABASE_URL_SIMULATOR = os.getenv(
    "DATABASE_URL_SIMULATOR",
    "postgresql://simulator_user:simulator_dev_password@postgres:5432/fraud_platform",
)
DATABASE_URL_APP = os.getenv(
    "DATABASE_URL_APP",
    os.getenv(
        "DATABASE_URL",
        "postgresql://app:app_dev_password@postgres:5432/fraud_platform",
    ),
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


@dataclass
class MenuItemFixture:
    item_id: UUID
    item_name: str
    category: str
    price_pence: int
    is_hot_food: bool


class _FixedUserPicker(WeightedUserPicker):
    def __init__(self, user_id: UUID) -> None:
        self._fixed_user_id = user_id

    def pick(self, _rng: random.Random) -> UUID:
        return self._fixed_user_id


def _build_menu_items() -> list[MenuItemLike]:
    menu_items: list[MenuItemFixture] = []

    for idx in range(8):
        menu_items.append(
            MenuItemFixture(
                item_id=UUID(int=1000 + idx),
                item_name=f"Main {idx + 1}",
                category="MAIN",
                price_pence=900 + (idx * 100),
                is_hot_food=True,
            ),
        )

    for idx in range(6):
        menu_items.append(
            MenuItemFixture(
                item_id=UUID(int=2000 + idx),
                item_name=f"Side {idx + 1}",
                category="SIDE",
                price_pence=350 + (idx * 50),
                is_hot_food=False,
            ),
        )

    for idx in range(4):
        menu_items.append(
            MenuItemFixture(
                item_id=UUID(int=3000 + idx),
                item_name=f"Drink {idx + 1}",
                category="DRINK",
                price_pence=250 + (idx * 25),
                is_hot_food=False,
            ),
        )

    for idx in range(2):
        menu_items.append(
            MenuItemFixture(
                item_id=UUID(int=4000 + idx),
                item_name=f"Other {idx + 1}",
                category="OTHER",
                price_pence=500,
                is_hot_food=False,
            ),
        )

    return cast(List[MenuItemLike], menu_items)


def _build_snapshot_cart(store_id: UUID) -> Cart:
    return Cart(
        store_id=store_id,
        items=[
            CartItem(
                item_id=UUID(int=5001),
                name="Margherita Pizza",
                qty=1,
                unit_price_pence=1000,
                is_hot_food=True,
            ),
            CartItem(
                item_id=UUID(int=5002),
                name="Sparkling Water",
                qty=1,
                unit_price_pence=500,
                is_hot_food=False,
            ),
        ],
    )


async def _fetch_store(
    conn: asyncpg.Connection,
    store_id: UUID,
) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        SELECT store_id, store_name, city, latitude, longitude, cuisine_types, price_tier,
               accepts_in_store, accepts_delivery, accepts_pickup, is_active,
               delivery_radius_km, merchant_id, country
        FROM stores
        WHERE store_id = $1
        """,
        store_id,
    )
    if row is None:
        raise AssertionError("store fixture was not inserted")
    return dict(row)


async def _fetch_store_hours(
    conn: asyncpg.Connection,
    store_id: UUID,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT store_id, day_of_week, open_time, close_time
        FROM store_hours
        WHERE store_id = $1
        ORDER BY day_of_week, open_time
        """,
        store_id,
    )
    return [dict(row) for row in rows]


def test_cart_builder() -> None:
    rng = random.Random(42)
    menu_items = _build_menu_items()
    user_profile = UserProfile(
        user_id=UUID(int=99),
        preferred_cuisines=["ITALIAN", "INDIAN"],
    )

    carts: list[Cart] = []
    for _ in range(1000):
        carts.append(
            build_realistic_cart(
                UUID(int=0),
                user_profile,
                menu_items,
                rng,
            ),
        )

    main_item_ids = {item.item_id for item in menu_items if item.category == "MAIN"}
    carts_with_main = sum(
        1 for cart in carts if any(item.item_id in main_item_ids for item in cart.items)
    )
    item_counts = [cart.item_count for cart in carts]
    mean_item_count = sum(item_counts) / len(item_counts)

    assert carts_with_main / len(carts) >= 0.90
    assert 2.5 <= mean_item_count <= 3.5


def test_vat_calculation() -> None:
    assert calculate_vat([VATLineItem(1000, is_hot_food=True)]) == 200

    assert (
        calculate_vat(
            [
                VATLineItem(1000, is_hot_food=True),
                VATLineItem(500, is_hot_food=False),
            ],
        )
        == 200
    )

    # 1000 cold -> 0; 250 delivery fee hot -> 50; 100 service fee hot -> 20
    assert (
        calculate_vat(
            [
                VATLineItem(1000, is_hot_food=False),
                VATLineItem(250, is_hot_food=True),
                VATLineItem(100, is_hot_food=True),
            ],
        )
        == 70
    )


@pytest.mark.asyncio
async def test_snapshot_builder() -> None:
    user_id = uuid4()
    merchant_id = uuid4()
    store_id = uuid4()
    address_id = uuid4()
    device_id = uuid4()
    session_id = uuid4()
    payment_method_id = uuid4()
    order_id = uuid4()

    created_at = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=dt.timezone.utc)
    placed_at = dt.datetime(2026, 5, 24, 12, 30, 0, tzinfo=dt.timezone.utc)
    second_placed_at = dt.datetime(2026, 5, 24, 12, 45, 0, tzinfo=dt.timezone.utc)

    conn = await asyncpg.connect(DATABASE_URL_APP)
    redis_client = redis_lib.Redis.from_url(REDIS_URL)
    redis_key = f"user_stats:{user_id}"

    try:
        await conn.execute(
            """
            INSERT INTO users (user_id, email, password_hash, created_at)
            VALUES ($1, $2, $3, $4)
            """,
            user_id,
            f"snapshot-{user_id}@example.com",
            "pw",
            created_at,
        )
        await conn.execute(
            """
            INSERT INTO merchants (merchant_id, legal_name, brand_name)
            VALUES ($1, 'Snapshot Merchant', 'Snapshot Merchant')
            """,
            merchant_id,
        )
        await conn.execute(
            """
            INSERT INTO stores (
                store_id, merchant_id, store_name, address_line_1, city, postcode,
                latitude, longitude
            ) VALUES (
                $1, $2, 'Snapshot Store', '1 Test Road', 'London', 'SW1A 1AA',
                51.5074, -0.1278
            )
            """,
            store_id,
            merchant_id,
        )
        await conn.execute(
            """
            INSERT INTO user_addresses (
                address_id, user_id, address_line_1, city, postcode,
                latitude, longitude, address_type, is_default
            ) VALUES (
                $1, $2, '1 Delivery Lane', 'London', 'SW1A 1AA',
                51.5000, -0.1000, 'RESIDENTIAL', true
            )
            """,
            address_id,
            user_id,
        )
        await conn.execute(
            """
            INSERT INTO devices (
                device_id, device_fingerprint, device_type, platform, os_version,
                app_version, browser_name, browser_version, unique_users_count
            ) VALUES (
                $1, $2, 'MOBILE_APP', 'iOS', '16.5', '5.0',
                'Safari', '17', 3
            )
            """,
            device_id,
            f"fp-{device_id}",
        )
        await conn.execute(
            "INSERT INTO user_devices (user_id, device_id) VALUES ($1, $2)",
            user_id,
            device_id,
        )
        await conn.execute(
            """
            INSERT INTO sessions (
                session_id, user_id, device_id, ip_address, ip_country, ip_city
            ) VALUES (
                $1, $2, $3, '127.0.0.1', 'GB', 'London'
            )
            """,
            session_id,
            user_id,
            device_id,
        )
        await conn.execute(
            """
            INSERT INTO payment_methods (
                payment_method_id, user_id, payment_type, card_bin, card_last_four,
                card_brand, card_funding_type, card_issuer_country, is_digital_native_bank,
                billing_address_id, unique_users_count
            ) VALUES (
                $1, $2, 'CREDIT_CARD', '123456', '1111',
                'VISA', 'DEBIT', 'GB', false, $3, 7
            )
            """,
            payment_method_id,
            user_id,
            address_id,
        )

        cart = _build_snapshot_cart(store_id)
        ip = IPAddress(
            ip_address="1.2.3.4",
            ip_country="GB",
            ip_city="London",
            ip_is_proxy=True,
            city_centroid_lat=51.4800,
            city_centroid_lon=-0.1500,
        )

        with get_session("app") as db_session:
            user: User = db_session.query(User).filter_by(user_id=user_id).one()
            store: Store = db_session.query(Store).filter_by(store_id=store_id).one()
            user_address: UserAddress = (
                db_session.query(UserAddress).filter_by(address_id=address_id).one()
            )
            payment_method: PaymentMethod = (
                db_session.query(PaymentMethod).filter_by(payment_method_id=payment_method_id).one()
            )
            device: Device = db_session.query(Device).filter_by(device_id=device_id).one()
            session: SessionModel = (
                db_session.query(SessionModel).filter_by(session_id=session_id).one()
            )

            first_snapshot = build_order_snapshot(
                user=user,
                store=store,
                cart=cart,
                delivery_address=user_address,
                payment_method=payment_method,
                device=device,
                session=session,
                ip=ip,
                promo=None,
                placed_at=placed_at,
                db_session=db_session,
                redis_client=redis_client,
            )

            assert first_snapshot["is_first_order_for_user"] is True
            assert first_snapshot["user_total_orders_lifetime"] == 0

            await conn.execute(
                """
                INSERT INTO orders (
                    order_id, order_number, order_status, order_channel, order_type,
                    placed_at, user_id, user_account_age_days, user_total_orders_lifetime,
                    user_total_orders_30d, user_total_spend_lifetime_pence, user_email,
                    user_email_domain, store_id, merchant_id, store_city, store_country,
                    store_latitude, store_longitude, delivery_address_id, delivery_latitude,
                    delivery_longitude, delivery_address_type, item_count, unique_item_count,
                    subtotal_pence, vat_pence, delivery_fee_pence, service_fee_pence,
                    tip_pence, discount_pence, total_pence, currency, payment_type,
                    payment_method_id
                ) VALUES (
                    $1, $2, 'PLACED', 'WEB', 'DELIVERY',
                    $3, $4, 1, 0, 0, 1500, $5, 'example.com',
                    $6, $7, 'London', 'GB', 51.5074, -0.1278,
                    $8, 51.5000, -0.1000, 'RESIDENTIAL',
                    1, 1, 1500, 200, 0, 0, 0, 0, 1500, 'GBP',
                    'CREDIT_CARD', $9
                )
                """,
                order_id,
                f"SNAP-{order_id.hex[:8]}",
                second_placed_at,
                user_id,
                user.email,
                store_id,
                merchant_id,
                address_id,
                payment_method_id,
            )

            redis_client.delete(redis_key)

            second_snapshot = build_order_snapshot(
                user=user,
                store=store,
                cart=cart,
                delivery_address=user_address,
                payment_method=payment_method,
                device=device,
                session=session,
                ip=ip,
                promo=None,
                placed_at=second_placed_at,
                db_session=db_session,
                redis_client=redis_client,
            )

            assert second_snapshot["is_first_order_for_user"] is False
            assert second_snapshot["user_total_orders_lifetime"] == 1
    finally:
        redis_client.delete(redis_key)
        await conn.execute("DELETE FROM orders WHERE order_id = $1", order_id)
        await conn.execute(
            "DELETE FROM payment_methods WHERE payment_method_id = $1",
            payment_method_id,
        )
        await conn.execute("DELETE FROM sessions WHERE session_id = $1", session_id)
        await conn.execute("DELETE FROM user_devices WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM devices WHERE device_id = $1", device_id)
        await conn.execute("DELETE FROM user_addresses WHERE address_id = $1", address_id)
        await conn.execute("DELETE FROM stores WHERE store_id = $1", store_id)
        await conn.execute("DELETE FROM merchants WHERE merchant_id = $1", merchant_id)
        await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
        await conn.close()
        redis_client.close()


def test_order_uniqueness() -> None:
    rng = random.Random(99)
    order_numbers = [generate_order_number(rng) for _ in range(100)]

    assert len(order_numbers) == len(set(order_numbers))


@pytest.mark.asyncio
@pytest.mark.slow
async def test_throughput() -> None:
    if not os.getenv("RUN_SLOW_TESTS"):
        pytest.skip(
            "slow; set RUN_SLOW_TESTS=1 to run: "
            "RUN_SLOW_TESTS=1 pytest tests/test_simulator.py::test_throughput",
        )

    original_simulation_time_compression = os.environ.get("SIMULATION_TIME_COMPRESSION")
    os.environ["SIMULATION_TIME_COMPRESSION"] = "1"

    setup_conn = await asyncpg.connect(DATABASE_URL_APP)
    pool: asyncpg.Pool | None = None

    user_id = uuid4()
    merchant_id = uuid4()
    store_id = uuid4()
    address_id = uuid4()
    device_id = uuid4()
    payment_method_id = uuid4()

    try:
        await setup_conn.execute(
            "INSERT INTO users (user_id, email, password_hash) VALUES ($1, $2, $3)",
            user_id,
            f"throughput-{user_id}@example.com",
            "pw",
        )
        await setup_conn.execute(
            """
            INSERT INTO merchants (merchant_id, legal_name, brand_name)
            VALUES ($1, 'Throughput Merchant', 'Throughput Merchant')
            """,
            merchant_id,
        )
        await setup_conn.execute(
            """
            INSERT INTO stores (
                store_id, merchant_id, store_name, address_line_1, city, postcode,
                latitude, longitude, accepts_delivery, accepts_pickup, accepts_in_store
            ) VALUES (
                $1, $2, 'Throughput Store', '1 Throughput Ave', 'London', 'N1 1AA',
                51.5074, -0.1278, true, false, false
            )
            """,
            store_id,
            merchant_id,
        )
        await setup_conn.execute(
            """
            INSERT INTO user_addresses (
                address_id, user_id, address_line_1, city, postcode,
                latitude, longitude, is_default
            ) VALUES (
                $1, $2, '1 Home Lane', 'London', 'SW1A 1AA',
                51.5000, -0.1000, true
            )
            """,
            address_id,
            user_id,
        )
        await setup_conn.execute(
            """
            INSERT INTO devices (
                device_id, device_fingerprint, device_type, platform, os_version,
                app_version, browser_name, browser_version
            ) VALUES (
                $1, $2, 'MOBILE_APP', 'iOS', '16.5', '5.0',
                'Safari', '17'
            )
            """,
            device_id,
            f"throughput-device-{device_id}",
        )
        await setup_conn.execute(
            "INSERT INTO user_devices (user_id, device_id) VALUES ($1, $2)",
            user_id,
            device_id,
        )
        await setup_conn.execute(
            """
            INSERT INTO payment_methods (
                payment_method_id, user_id, payment_type, card_bin, card_last_four,
                card_brand, card_funding_type, card_issuer_country, is_digital_native_bank,
                billing_address_id, unique_users_count
            ) VALUES (
                $1, $2, 'CREDIT_CARD', '123456', '1111',
                'VISA', 'DEBIT', 'GB', false, $3, 1
            )
            """,
            payment_method_id,
            user_id,
            address_id,
        )
        await setup_conn.executemany(
            """
            INSERT INTO store_hours (store_id, day_of_week, open_time, close_time)
            VALUES ($1, $2, $3, $4)
            """,
            [
                (store_id, day_of_week, dt.time(0, 0), dt.time(23, 59, 59))
                for day_of_week in range(7)
            ],
        )
        await setup_conn.executemany(
            """
            INSERT INTO menu_items (
                item_id, store_id, item_name, category, price_pence, is_hot_food
            ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
            [
                (uuid4(), store_id, "Throughput Main", "MAIN", 1000, True),
                (uuid4(), store_id, "Throughput Side", "SIDE", 400, False),
                (uuid4(), store_id, "Throughput Drink", "DRINK", 250, False),
            ],
        )

        store = await _fetch_store(setup_conn, store_id)
        store_hours = await _fetch_store_hours(setup_conn, store_id)
        stores_by_city: dict[str, list[dict[str, Any]]] = {"London": [store]}
        store_hours_by_store_id: dict[UUID, list[dict[str, Any]]] = {
            store_id: store_hours,
        }

        pool = await asyncpg.create_pool(
            DATABASE_URL_SIMULATOR,
            min_size=1,
            max_size=10,
        )
        promos = await load_active_promos(pool)
        picker = _FixedUserPicker(user_id)

        errors = 0
        target_orders = 60 * 50
        interval_seconds = 1.0 / 50.0
        start = time.perf_counter()

        for iteration in range(1, target_orders + 1):
            try:
                await create_one_order(
                    pool=pool,
                    user_picker=picker,
                    stores_by_city=stores_by_city,
                    store_hours_by_store_id=store_hours_by_store_id,
                    promos=promos,
                    rng=random.Random(iteration),
                    scoring_enabled=False,
                )
            except Exception:
                errors += 1

            delay = (start + (iteration * interval_seconds)) - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)

        elapsed_wall = time.perf_counter() - start
        created_count_raw = await setup_conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE user_id = $1",
            user_id,
        )
        created_count = int(created_count_raw)

        assert created_count >= 2800
        assert errors == 0
        assert elapsed_wall <= 180.0
    finally:
        if pool is not None:
            await pool.close()

        order_rows = await setup_conn.fetch(
            "SELECT order_id FROM orders WHERE user_id = $1",
            user_id,
        )
        order_ids = [row["order_id"] for row in order_rows]
        if order_ids:
            await setup_conn.execute(
                "DELETE FROM order_events WHERE order_id = ANY($1::uuid[])",
                order_ids,
            )
            await setup_conn.execute(
                "DELETE FROM order_items WHERE order_id = ANY($1::uuid[])",
                order_ids,
            )
            await setup_conn.execute(
                "DELETE FROM simulator_ground_truth WHERE order_id = ANY($1::uuid[])",
                order_ids,
            )
            await setup_conn.execute("DELETE FROM orders WHERE user_id = $1", user_id)

        await setup_conn.execute("DELETE FROM menu_items WHERE store_id = $1", store_id)
        await setup_conn.execute("DELETE FROM store_hours WHERE store_id = $1", store_id)
        await setup_conn.execute("DELETE FROM payment_methods WHERE user_id = $1", user_id)
        await setup_conn.execute("DELETE FROM user_devices WHERE user_id = $1", user_id)
        await setup_conn.execute("DELETE FROM devices WHERE device_id = $1", device_id)
        await setup_conn.execute("DELETE FROM user_addresses WHERE user_id = $1", user_id)
        await setup_conn.execute("DELETE FROM stores WHERE store_id = $1", store_id)
        await setup_conn.execute("DELETE FROM merchants WHERE merchant_id = $1", merchant_id)
        await setup_conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
        await setup_conn.close()

        if original_simulation_time_compression is None:
            os.environ.pop("SIMULATION_TIME_COMPRESSION", None)
        else:
            os.environ["SIMULATION_TIME_COMPRESSION"] = original_simulation_time_compression


@pytest.mark.asyncio
async def test_store_hours_respected() -> None:
    merchant_id = uuid4()
    open_store_id = uuid4()
    closed_store_id = uuid4()
    user_id = uuid4()
    address_id = uuid4()

    now_london = dt.datetime.now(tz=LONDON_TZ)
    now_time = now_london.time()
    if now_time >= dt.time(12, 0):
        closed_open = dt.time(0, 0)
        closed_close = dt.time(11, 58)
    else:
        closed_open = dt.time(13, 0)
        closed_close = dt.time(14, 0)

    conn = await asyncpg.connect(DATABASE_URL_SIMULATOR)

    try:
        await conn.execute(
            """
            INSERT INTO merchants (merchant_id, legal_name, brand_name)
            VALUES ($1, 'Store Hours Merchant', 'Store Hours Merchant')
            """,
            merchant_id,
        )
        await conn.execute(
            "INSERT INTO users (user_id, email, password_hash) VALUES ($1, $2, $3)",
            user_id,
            f"store-hours-{user_id}@example.com",
            "pw",
        )
        await conn.execute(
            """
            INSERT INTO user_addresses (
                address_id, user_id, address_line_1, city, postcode, latitude, longitude
            ) VALUES (
                $1, $2, 'Home', 'London', 'SW1A 1AA', 51.5000, -0.1000
            )
            """,
            address_id,
            user_id,
        )
        await conn.execute(
            """
            INSERT INTO stores (
                store_id, merchant_id, store_name, address_line_1, city, postcode,
                latitude, longitude
            ) VALUES (
                $1, $2, 'Open Store', '1 Main', 'London', 'SW1A 2AA',
                51.5074, -0.1278
            )
            """,
            open_store_id,
            merchant_id,
        )
        await conn.execute(
            """
            INSERT INTO stores (
                store_id, merchant_id, store_name, address_line_1, city, postcode,
                latitude, longitude
            ) VALUES (
                $1, $2, 'Closed Store', '2 Side', 'London', 'SW1A 3AA',
                51.5050, -0.1350
            )
            """,
            closed_store_id,
            merchant_id,
        )
        await conn.executemany(
            """
            INSERT INTO store_hours (store_id, day_of_week, open_time, close_time)
            VALUES ($1, $2, $3, $4)
            """,
            [
                (open_store_id, day_of_week, dt.time(0, 0), dt.time(23, 59, 59))
                for day_of_week in range(7)
            ],
        )
        await conn.executemany(
            """
            INSERT INTO store_hours (store_id, day_of_week, open_time, close_time)
            VALUES ($1, $2, $3, $4)
            """,
            [(closed_store_id, day_of_week, closed_open, closed_close) for day_of_week in range(7)],
        )

        user_data: dict[str, Any] = {
            "default_address": {
                "city": "London",
                "latitude": 51.5000,
                "longitude": -0.1000,
            },
        }
        open_store = await _fetch_store(conn, open_store_id)
        closed_store = await _fetch_store(conn, closed_store_id)
        open_hours = await _fetch_store_hours(conn, open_store_id)
        closed_hours = await _fetch_store_hours(conn, closed_store_id)

        chosen = pick_store_for_user(
            random.Random(1),
            user_data,
            {"London": [open_store]},
            {open_store_id: open_hours},
        )
        assert chosen["store_id"] == open_store_id

        with pytest.raises(RuntimeError, match="no stores in current open-hours window"):
            pick_store_for_user(
                random.Random(2),
                user_data,
                {"London": [closed_store]},
                {closed_store_id: closed_hours},
            )
    finally:
        await conn.execute(
            "DELETE FROM store_hours WHERE store_id = ANY($1::uuid[])",
            [open_store_id, closed_store_id],
        )
        await conn.execute(
            "DELETE FROM stores WHERE store_id = ANY($1::uuid[])",
            [open_store_id, closed_store_id],
        )
        await conn.execute("DELETE FROM user_addresses WHERE address_id = $1", address_id)
        await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM merchants WHERE merchant_id = $1", merchant_id)
        await conn.close()
