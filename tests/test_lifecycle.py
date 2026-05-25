from __future__ import annotations

import datetime as dt
import json
import random
import uuid

import asyncpg
import pytest

from simulator.lifecycle import (
    DATABASE_URL,
    _coerce_uuid,
    _compute_transition,
    _seeded_rng,
    advance_order,
    SIMULATION_TIME_COMPRESSION,
)


class _ForcedRng(random.Random):
    def __init__(self, values: list[float]) -> None:
        super().__init__(42)
        self._values = iter(values)

    def random(self) -> float:
        try:
            return next(self._values)
        except StopIteration:
            return super().random()


def _find_pickup_order_for_no_show(is_no_show: bool) -> uuid.UUID:
    for raw in range(1, 10000):
        candidate = uuid.UUID(int=raw)
        if (_seeded_rng(candidate, "pickup_no_show").random() < 0.05) == is_no_show:
            return candidate
    raise RuntimeError("No matching seeded pickup order_id found in search window")


def test_should_transition_placed_to_accepted() -> None:
    rng = random.Random(1)
    status, reason = _compute_transition(
        current_status="PLACED",
        order_type="DELIVERY",
        simulated_elapsed_seconds=30,
        rng=rng,
    )

    assert status == "ACCEPTED"
    assert reason is None


def test_should_transition_placed_to_cancelled() -> None:
    rng = _ForcedRng([0.99, 0.3])
    status, reason = _compute_transition(
        current_status="PLACED",
        order_type="DELIVERY",
        simulated_elapsed_seconds=60,
        rng=rng,
    )

    assert status == "CANCELLED"
    assert reason == "cancelled_by=USER"


def test_ready_pickup_delivers_at_sampled_time_or_waits_for_no_show() -> None:
    cancel_order_id = _find_pickup_order_for_no_show(True)
    delivered_order_id = _find_pickup_order_for_no_show(False)

    status, reason = _compute_transition(
        current_status="READY",
        order_type="PICKUP",
        order_id=cancel_order_id,
        simulated_elapsed_seconds=61,
        rng=random.Random(7),
    )
    assert status is None
    assert reason is None

    status, reason = _compute_transition(
        current_status="READY",
        order_type="PICKUP",
        order_id=cancel_order_id,
        simulated_elapsed_seconds=3500,
        rng=random.Random(7),
    )
    assert status is None

    status, reason = _compute_transition(
        current_status="READY",
        order_type="PICKUP",
        order_id=cancel_order_id,
        simulated_elapsed_seconds=3601,
        rng=random.Random(7),
    )
    assert status == "CANCELLED"
    assert reason == "no-show"

    delivery_delay_seconds = _seeded_rng(
        delivered_order_id,
        "pickup_ready_delay_minutes",
    ).uniform(60.0, 1800.0)

    status, reason = _compute_transition(
        current_status="READY",
        order_type="PICKUP",
        order_id=delivered_order_id,
        simulated_elapsed_seconds=30,
        rng=random.Random(7),
    )
    assert status is None

    status, reason = _compute_transition(
        current_status="READY",
        order_type="PICKUP",
        order_id=delivered_order_id,
        simulated_elapsed_seconds=delivery_delay_seconds,
        rng=random.Random(7),
    )
    assert status == "DELIVERED"
    assert reason is None


def test_in_transit_uses_deterministic_distance_factor_per_order() -> None:
    order_id = uuid.uuid4()
    distance_km = 3.0
    factor = _seeded_rng(order_id, "in_transit_factor").uniform(0.8, 1.5)
    threshold_seconds = distance_km * 2.0 * 60.0 * factor

    status, reason = _compute_transition(
        current_status="IN_TRANSIT",
        order_type="DELIVERY",
        order_id=order_id,
        simulated_elapsed_seconds=threshold_seconds - 1,
        rng=_ForcedRng([0.01]),
    )
    assert status is None
    assert reason is None

    status, reason = _compute_transition(
        current_status="IN_TRANSIT",
        order_type="DELIVERY",
        order_id=order_id,
        simulated_elapsed_seconds=threshold_seconds + 1,
        rng=_ForcedRng([0.01]),
    )
    assert status == "DELIVERED"
    assert reason is None


@pytest.mark.asyncio
async def test_lifecycle_processes_placed_order_to_accepted() -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    user_id = uuid.uuid4()
    merchant_id = uuid.uuid4()
    store_id = uuid.uuid4()
    order_id = uuid.uuid4()

    email = f"lifecycle-{order_id}@example.com"
    placed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    event_time = placed_at - dt.timedelta(minutes=3)
    order_number = f"LC-{order_id.hex[:14]}"

    try:
        await conn.execute(
            "INSERT INTO users (user_id, email, password_hash) VALUES ($1, $2, $3)",
            user_id,
            email,
            "fixture_password",
        )
        await conn.execute(
            "INSERT INTO merchants (merchant_id, legal_name, brand_name) "
            "VALUES ($1, 'Lifecycle Test', 'Lifecycle Test')",
            merchant_id,
        )
        await conn.execute(
            "INSERT INTO stores (store_id, merchant_id, store_name, address_line_1, "
            "city, postcode, latitude, longitude) "
            "VALUES ($1, $2, 'Lifecycle Store', '1 Life Way', 'London', 'NW1 2DE', "
            "51.501, -0.142)",
            store_id,
            merchant_id,
        )
        await conn.execute(
            """
            INSERT INTO orders (
                order_id, order_number, order_status, order_channel, order_type,
                placed_at, user_id, user_account_age_days, user_total_orders_lifetime,
                user_total_orders_30d, user_total_spend_lifetime_pence, user_email,
                user_email_domain, store_id, merchant_id, store_city,
                store_latitude,
                store_longitude, item_count, unique_item_count,
                subtotal_pence, total_pence,
                payment_type
            ) VALUES (
                $1, $2, 'PLACED', 'WEB', 'DELIVERY',
                $3, $4, 1, 0, 0, 1000,
                $5, 'example.com', $6, $7, 'London', 51.501, -0.142,
                1, 1, 1000, 1100, 'CREDIT_CARD'
            )
            """,
            order_id,
            order_number,
            placed_at,
            user_id,
            email,
            store_id,
            merchant_id,
        )
        await conn.execute(
            """
            INSERT INTO order_events (
                order_id, order_placed_at, event_type, event_data,
                actor_type, created_at
            ) VALUES (
                $1, $2, 'ORDER_PLACED', $3::jsonb, 'SIMULATOR', $4
            )
            """,
            order_id,
            placed_at,
            json.dumps({"seed": str(order_id)}),
            event_time,
        )

        order = await conn.fetchrow(
            "SELECT * FROM orders WHERE order_id = $1",
            order_id,
        )
        assert order is not None
        last_event_at = await conn.fetchval(
            """
            SELECT MAX(created_at) FROM order_events
            WHERE order_id = $1
            """,
            order_id,
        )
        assert isinstance(last_event_at, dt.datetime)

        transitioned = await advance_order(
            conn,
            dict(order),
            last_state_at=last_event_at,
            rng=random.Random(1),
            simulation_time_compression=SIMULATION_TIME_COMPRESSION,
        )
        assert transitioned is True

        final_status = await conn.fetchval(
            "SELECT order_status FROM orders WHERE order_id = $1",
            order_id,
        )
        event_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM order_events
            WHERE order_id = $1 AND event_type = 'ORDER_ACCEPTED'
            """,
            order_id,
        )

        assert final_status == "ACCEPTED"
        assert event_count == 1
    finally:
        await conn.execute("DELETE FROM order_events WHERE order_id = $1", order_id)
        await conn.execute("DELETE FROM orders WHERE order_id = $1", order_id)
        await conn.execute("DELETE FROM stores WHERE store_id = $1", store_id)
        await conn.execute("DELETE FROM merchants WHERE merchant_id = $1", merchant_id)
        await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
        await conn.close()


@pytest.mark.asyncio
async def test_driver_assigned_when_order_becomes_accepted() -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    user_id = uuid.uuid4()
    merchant_id = uuid.uuid4()
    store_id = uuid.uuid4()
    driver_id = uuid.uuid4()
    order_id = uuid.uuid4()

    email = f"lifecycle-driver-{order_id}@example.com"
    placed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    event_time = placed_at - dt.timedelta(minutes=3)
    order_number = f"LC-{order_id.hex[:14]}"

    try:
        await conn.execute(
            "INSERT INTO users (user_id, email, password_hash) VALUES ($1, $2, $3)",
            user_id,
            email,
            "fixture_password",
        )
        await conn.execute(
            "INSERT INTO merchants (merchant_id, legal_name, brand_name) "
            "VALUES ($1, 'Lifecycle Test', 'Lifecycle Test')",
            merchant_id,
        )
        await conn.execute(
            "INSERT INTO stores (store_id, merchant_id, store_name, address_line_1, "
            "city, postcode, latitude, longitude) "
            "VALUES ($1, $2, 'Lifecycle Store', '1 Life Way', 'London', 'NW1 2DE', "
            "51.501, -0.142)",
            store_id,
            merchant_id,
        )
        await conn.execute(
            """
            INSERT INTO drivers (
                driver_id, first_name, last_name, email, home_city, status,
                completed_deliveries
            ) VALUES ($1, 'Alex', 'Driver', $2, 'London', 'ACTIVE', 0)
            """,
            driver_id,
            f"driver-{driver_id.hex[:8]}@example.com",
        )
        await conn.execute(
            """
            INSERT INTO orders (
                order_id, order_number, order_status, order_channel, order_type,
                placed_at, user_id, user_account_age_days, user_total_orders_lifetime,
                user_total_orders_30d, user_total_spend_lifetime_pence, user_email,
                user_email_domain, store_id, merchant_id, store_city,
                store_latitude,
                store_longitude, item_count, unique_item_count,
                subtotal_pence, total_pence,
                payment_type
            ) VALUES (
                $1, $2, 'PLACED', 'WEB', 'DELIVERY',
                $3, $4, 1, 0, 0, 1000,
                $5, 'example.com', $6, $7, 'London', 51.501, -0.142,
                1, 1, 1000, 1100, 'CREDIT_CARD'
            )
            """,
            order_id,
            order_number,
            placed_at,
            user_id,
            email,
            store_id,
            merchant_id,
        )
        await conn.execute(
            """
            INSERT INTO order_events (
                order_id, order_placed_at, event_type, event_data,
                actor_type, created_at
            ) VALUES (
                $1, $2, 'ORDER_PLACED', $3::jsonb, 'SIMULATOR', $4
            )
            """,
            order_id,
            placed_at,
            json.dumps({"seed": str(order_id)}),
            event_time,
        )

        order = await conn.fetchrow(
            "SELECT * FROM orders WHERE order_id = $1",
            order_id,
        )
        assert order is not None
        last_event_at = await conn.fetchval(
            """
            SELECT MAX(created_at) FROM order_events
            WHERE order_id = $1
            """,
            order_id,
        )
        assert isinstance(last_event_at, dt.datetime)

        transitioned = await advance_order(
            conn,
            dict(order),
            last_state_at=last_event_at,
            rng=random.Random(1),
            simulation_time_compression=SIMULATION_TIME_COMPRESSION,
        )
        assert transitioned is True

        final_state = await conn.fetchrow(
            "SELECT order_status, driver_id FROM orders WHERE order_id = $1",
            order_id,
        )
        assert final_state is not None
        assert final_state["order_status"] == "ACCEPTED"
        assert _coerce_uuid(final_state["driver_id"]) == driver_id

    finally:
        await conn.execute("DELETE FROM order_events WHERE order_id = $1", order_id)
        await conn.execute("DELETE FROM orders WHERE order_id = $1", order_id)
        await conn.execute("DELETE FROM drivers WHERE driver_id = $1", driver_id)
        await conn.execute("DELETE FROM stores WHERE store_id = $1", store_id)
        await conn.execute("DELETE FROM merchants WHERE merchant_id = $1", merchant_id)
        await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)
        await conn.close()
