from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

import asyncpg

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://app:app_dev_password@postgres:5432/fraud_platform",
)


def _load_simulation_time_compression() -> int:
    raw = os.environ.get("SIMULATION_TIME_COMPRESSION", "60")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 60
    return value if value > 0 else 1


SIMULATION_TIME_COMPRESSION = _load_simulation_time_compression()

_POLL_INTERVAL_SECONDS = 5
_MAX_ORDERS_PER_POLL = 5000
_CHUNK_SIZE = 100
_DEFAULT_PREP_TIME_MIN = 10.0
_DEFAULT_DISTANCE_KM = 3.0

_TERMINAL_STATES = {"DELIVERED", "CANCELLED", "REFUNDED", "FAILED"}
_STATUS_TIMESTAMP_BY_STATE = {
    "ACCEPTED": "accepted_at",
    "READY": "ready_at",
    "PICKED_UP": "picked_up_at",
    "DELIVERED": "delivered_at",
    "CANCELLED": "cancelled_at",
}
_EVENT_BY_STATE = {
    "ACCEPTED": "ORDER_ACCEPTED",
    "PREPARING": "ORDER_PREPARING",
    "READY": "ORDER_READY",
    "PICKED_UP": "ORDER_PICKED_UP",
    "IN_TRANSIT": "ORDER_IN_TRANSIT",
    "DELIVERED": "ORDER_DELIVERED",
    "CANCELLED": "ORDER_CANCELLED",
    "FAILED": "ORDER_FAILED",
}


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _seed_from_uuid(seed_uuid: uuid.UUID, namespace: str) -> int:
    digest = hashlib.sha256(f"{seed_uuid}:{namespace}".encode()).hexdigest()
    return int(digest[:16], 16)


def _seeded_rng(seed_uuid: uuid.UUID, namespace: str) -> random.Random:
    return random.Random(_seed_from_uuid(seed_uuid, namespace))


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _compute_transition(
    current_status: str,
    order_type: str,
    simulated_elapsed_seconds: float,
    rng: random.Random,
    *,
    order_id: uuid.UUID | None = None,
    avg_prep_time_min: float | None = None,
    distance_km: float | None = None,
) -> tuple[str | None, str | None]:
    status = str(current_status).upper()
    normalized_order_type = str(order_type).upper()
    if simulated_elapsed_seconds < 0:
        return None, None

    if status == "PLACED":
        if simulated_elapsed_seconds < 30:
            return None, None

        roll = rng.random()
        if roll < 0.97:
            return "ACCEPTED", None

        cancelled_by = "USER" if rng.random() < 0.5 else "MERCHANT"
        return "CANCELLED", f"cancelled_by={cancelled_by}"

    if status == "ACCEPTED":
        return "PREPARING", None

    if status == "PREPARING":
        prep_time_min = _DEFAULT_PREP_TIME_MIN if avg_prep_time_min is None else avg_prep_time_min
        threshold_seconds = prep_time_min * 60.0 * rng.uniform(0.8, 1.2)
        if simulated_elapsed_seconds < threshold_seconds:
            return None, None

        if rng.random() < 0.98:
            return "READY", None
        return "CANCELLED", "out of stock"

    if status == "READY":
        if normalized_order_type == "DELIVERY":
            if simulated_elapsed_seconds < 60:
                return None, None
            if rng.random() < 0.99:
                return "PICKED_UP", None
            return None, None

        if normalized_order_type == "PICKUP":
            if simulated_elapsed_seconds < 60:
                return None, None

            if order_id is not None:
                is_no_show = _seeded_rng(order_id, "pickup_no_show").random() < 0.05
                if is_no_show and simulated_elapsed_seconds > 3600:
                    return "CANCELLED", "no-show"
                if is_no_show:
                    return None, None

                sample_ready_delay_seconds = _seeded_rng(
                    order_id,
                    "pickup_ready_delay_minutes",
                ).uniform(60.0, 1800.0)
                if simulated_elapsed_seconds >= sample_ready_delay_seconds:
                    return "DELIVERED", None
                return None, None

            if simulated_elapsed_seconds >= 3600:
                return "CANCELLED", "no-show"
            if simulated_elapsed_seconds >= 60 and rng.random() < 0.95:
                return "DELIVERED", None
            return None, None

        return None, None

    if status == "PICKED_UP":
        return "IN_TRANSIT", None

    if status == "IN_TRANSIT":
        distance = _DEFAULT_DISTANCE_KM if distance_km is None else distance_km
        if order_id is None:
            travel_factor = rng.uniform(0.8, 1.5)
        else:
            travel_factor = _seeded_rng(order_id, "in_transit_factor").uniform(0.8, 1.5)
        threshold_seconds = distance * 2.0 * 60.0 * travel_factor
        if simulated_elapsed_seconds < threshold_seconds:
            return None, None

        if rng.random() < 0.98:
            return "DELIVERED", None
        return "FAILED", "address wrong"

    return None, None


def _cancelled_by_from_reason(
    *,
    reason: str | None,
    current_status: str,
    order_type: str,
) -> str | None:
    normalized_current_status = current_status.upper()
    if reason is not None and reason.startswith("cancelled_by="):
        maybe_by = reason.split("=", 1)[1]
        if maybe_by in {"USER", "MERCHANT"}:
            return maybe_by

    if normalized_current_status == "PLACED":
        return None
    if normalized_current_status == "PREPARING":
        return "MERCHANT"
    if normalized_current_status == "READY" and order_type.upper() == "PICKUP":
        return "USER"
    return None


async def _fetch_nonterminal_orders(conn: asyncpg.Connection) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT order_id, placed_at, order_status, order_type,
               store_id, store_city, delivery_distance_km, driver_id
        FROM orders
        WHERE order_status NOT IN ('DELIVERED', 'CANCELLED', 'REFUNDED', 'FAILED')
          AND placed_at > NOW() - INTERVAL '7 days'
        LIMIT $1
        """,
        _MAX_ORDERS_PER_POLL,
    )
    return [dict(row) for row in rows]


async def _fetch_last_state_times(
    conn: asyncpg.Connection,
    order_ids: list[uuid.UUID],
) -> dict[uuid.UUID, datetime]:
    if not order_ids:
        return {}

    rows = await conn.fetch(
        """
        SELECT order_id, MAX(created_at) AS last_event_at
        FROM order_events
        WHERE order_id = ANY($1)
          AND order_placed_at > NOW() - INTERVAL '7 days'
        GROUP BY order_id
        """,
        order_ids,
    )
    result: dict[uuid.UUID, datetime] = {}
    for row in rows:
        row_order_id = _coerce_uuid(row["order_id"])
        if row_order_id is None:
            continue
        last_event_at = row["last_event_at"]
        if isinstance(last_event_at, datetime):
            result[row_order_id] = last_event_at
    return result


async def _fetch_store_prep_times(
    conn: asyncpg.Connection,
    store_ids: list[uuid.UUID],
) -> dict[uuid.UUID, float]:
    if not store_ids:
        return {}

    rows = await conn.fetch(
        "SELECT store_id, avg_prep_time_min FROM stores WHERE store_id = ANY($1)",
        store_ids,
    )
    result: dict[uuid.UUID, float] = {}
    for row in rows:
        store_id = _coerce_uuid(row["store_id"])
        if store_id is None:
            continue
        prep_time = _coerce_float(row["avg_prep_time_min"])
        if prep_time is None:
            prep_time = _DEFAULT_PREP_TIME_MIN
        result[store_id] = prep_time
    return result


async def _pick_driver(
    conn: asyncpg.Connection,
    store_city: str | None,
) -> uuid.UUID | None:
    if store_city is None:
        return None

    row = await conn.fetchrow(
        """
        SELECT driver_id FROM drivers
        WHERE status = 'ACTIVE' AND home_city = $1
        ORDER BY -log(random()) / (COALESCE(completed_deliveries, 0) + 1)
        LIMIT 1
        """,
        store_city,
    )
    return _coerce_uuid(row["driver_id"]) if row is not None else None


async def advance_order(
    conn: asyncpg.Connection,
    order: Mapping[str, Any],
    *,
    last_state_at: datetime,
    rng: random.Random,
    simulation_time_compression: int,
    store_avg_prep_time_min_by_store_id: Mapping[uuid.UUID, float] | None = None,
    now: datetime | None = None,
) -> bool:
    order_id = _coerce_uuid(order["order_id"])
    if order_id is None:
        raise ValueError("order_id missing or invalid")

    placed_at = order.get("placed_at")
    if not isinstance(placed_at, datetime):
        raise ValueError(f"placed_at missing or invalid for order {order_id}")

    current_status = str(order["order_status"])
    order_type = str(order["order_type"])
    store_id = _coerce_uuid(order.get("store_id"))
    city = order.get("store_city")
    store_city = city if isinstance(city, str) else None
    distance_km = _coerce_float(order.get("delivery_distance_km"))

    if now is None:
        now = _now_utc()
    elapsed_seconds = (now - last_state_at).total_seconds()
    if elapsed_seconds < 0:
        elapsed_seconds = 0.0

    if elapsed_seconds == 0:
        return False

    simulated_elapsed = elapsed_seconds * simulation_time_compression

    avg_prep_time_min: float | None = None
    if (
        current_status.upper() == "PREPARING"
        and store_id is not None
        and store_avg_prep_time_min_by_store_id is not None
    ):
        avg_prep_time_min = store_avg_prep_time_min_by_store_id.get(store_id)

    next_status, reason = _compute_transition(
        current_status=current_status,
        order_type=order_type,
        order_id=order_id,
        simulated_elapsed_seconds=simulated_elapsed,
        rng=rng,
        avg_prep_time_min=avg_prep_time_min,
        distance_km=distance_km,
    )
    if next_status is None:
        return False

    event_type = _EVENT_BY_STATE.get(next_status, "ORDER_STATE_CHANGED")
    status_timestamp = _STATUS_TIMESTAMP_BY_STATE.get(next_status)
    is_terminal = next_status in _TERMINAL_STATES

    update_values: list[Any] = [next_status, order_id, placed_at]
    update_parts: list[str] = ["order_status = $1"]
    next_param = 4

    if status_timestamp is not None:
        update_parts.append(f"{status_timestamp} = NOW()")

    if is_terminal:
        update_parts.append("terminal_state_reached_at = NOW()")

    driver_id: uuid.UUID | None = None
    if next_status == "ACCEPTED" and order_type.upper() == "DELIVERY":
        driver_id = await _pick_driver(conn, store_city)
        if driver_id is None:
            logger.info(
                json.dumps(
                    {
                        "event": "lifecycle_no_driver",
                        "order_id": str(order_id),
                        "store_city": store_city,
                    },
                )
            )
            return False

    if next_status == "CANCELLED":
        if reason is not None:
            update_parts.append(f"cancellation_reason = ${next_param}")
            update_values.append(reason)
            next_param += 1
        else:
            update_parts.append("cancellation_reason = NULL")

        cancelled_by = _cancelled_by_from_reason(
            reason=reason,
            current_status=current_status,
            order_type=order_type,
        )
        if cancelled_by is not None:
            update_parts.append(f"cancelled_by = ${next_param}")
            update_values.append(cancelled_by)
            next_param += 1
        else:
            update_parts.append("cancelled_by = NULL")

    if driver_id is not None:
        update_parts.append(f"driver_id = ${next_param}")
        update_values.append(driver_id)

    update_sql = (
        f"UPDATE orders SET {', '.join(update_parts)} WHERE order_id = $2 AND placed_at = $3"
    )

    event_actor_type = "SYSTEM"
    event_actor_id: uuid.UUID | None = None
    if next_status in {"PICKED_UP", "IN_TRANSIT", "DELIVERED", "FAILED"}:
        event_actor_type = "DRIVER"
        event_actor_id = (
            driver_id if driver_id is not None else _coerce_uuid(order.get("driver_id"))
        )

    event_payload: dict[str, Any] = {
        "from_status": current_status,
        "to_status": next_status,
    }
    if reason is not None:
        event_payload["reason"] = reason
    if status_timestamp is not None:
        event_payload["status_timestamp_field"] = status_timestamp

    async with conn.transaction():
        await conn.execute(update_sql, *update_values)
        await conn.execute(
            """
            INSERT INTO order_events (
                order_id, order_placed_at, event_type, event_data,
                actor_type, actor_id, created_at
            ) VALUES ($1, $2, $3, $4::jsonb, $5, $6, NOW())
            """,
            order_id,
            placed_at,
            event_type,
            json.dumps(event_payload),
            event_actor_type,
            event_actor_id,
        )

    logger.info(
        json.dumps(
            {
                "event": "order_advanced",
                "order_id": str(order_id),
                "from_status": current_status,
                "to_status": next_status,
            },
        )
    )

    return True


async def _safe_advance_one_order(
    pool: asyncpg.Pool,
    order: Mapping[str, Any],
    last_state_at: datetime,
    rng: random.Random,
    store_avg_prep_time_min_by_store_id: Mapping[uuid.UUID, float],
    simulation_time_compression: int,
) -> bool:
    try:
        async with pool.acquire() as conn:
            return await advance_order(
                conn,
                order,
                last_state_at=last_state_at,
                rng=rng,
                simulation_time_compression=simulation_time_compression,
                store_avg_prep_time_min_by_store_id=store_avg_prep_time_min_by_store_id,
            )
    except Exception as exc:
        order_id = order.get("order_id")
        logger.error(
            json.dumps(
                {
                    "event": "order_advance_failed",
                    "order_id": str(order_id),
                    "error": str(exc),
                },
            )
        )
        return False


async def run_once(
    pool: asyncpg.Pool,
    *,
    rng: random.Random,
    simulation_time_compression: int | None = None,
) -> int:
    if simulation_time_compression is None:
        simulation_time_compression = SIMULATION_TIME_COMPRESSION

    async with pool.acquire() as conn:
        orders = await _fetch_nonterminal_orders(conn)
        if not orders:
            return 0

        order_ids = [
            order_id
            for order_id in [_coerce_uuid(order.get("order_id")) for order in orders]
            if order_id is not None
        ]
        last_state_at_by_order = await _fetch_last_state_times(conn, order_ids)
        store_ids = [
            store_id
            for store_id in [_coerce_uuid(order.get("store_id")) for order in orders]
            if store_id is not None
        ]
        store_avg_prep_time_min_by_store_id = await _fetch_store_prep_times(
            conn,
            store_ids,
        )

    tasks: list[asyncio.Task[bool]] = []
    total_advanced = 0
    for start in range(0, len(orders), _CHUNK_SIZE):
        chunk = orders[start : start + _CHUNK_SIZE]
        for order in chunk:
            order_id = _coerce_uuid(order.get("order_id"))
            if order_id is None:
                continue
            last_state_at = last_state_at_by_order.get(order_id, _now_utc())
            order_rng = random.Random(rng.randint(0, 2**63 - 1))
            tasks.append(
                asyncio.create_task(
                    _safe_advance_one_order(
                        pool,
                        order,
                        last_state_at=last_state_at,
                        rng=order_rng,
                        store_avg_prep_time_min_by_store_id=(store_avg_prep_time_min_by_store_id),
                        simulation_time_compression=simulation_time_compression,
                    )
                )
            )

        if tasks:
            results = await asyncio.gather(*tasks)
            total_advanced += sum(1 for result in results if result)
            tasks = []

    return total_advanced


async def main() -> None:
    rng = random.Random()
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=5,
        max_size=20,
    )
    try:
        while True:
            try:
                await run_once(pool=pool, rng=rng)
            except Exception:
                logger.exception("order_lifecycle_failed")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
