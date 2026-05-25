from __future__ import annotations

import asyncio
import datetime
import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import UUID

import asyncpg  # type: ignore[import]
import redis.asyncio as aioredis
from backports.zoneinfo import ZoneInfo
from pythonjsonlogger import jsonlogger
from redis.asyncio import Redis as AsyncRedis

LOGGER_NAME = "feature_store.aggregator"
LOGGER = logging.getLogger(LOGGER_NAME)
LONDON_TZ = ZoneInfo("Europe/London")
DATABASE_URL = os.environ.get(
    "DATABASE_URL_APP",
    "postgresql://app:app_dev_password@postgres:5432/fraud_platform",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
ONE_HOUR_SECONDS = 60 * 60
TWENTY_FOUR_HOURS_SECONDS = 24 * 60 * 60
BACKUP_POLL_SECONDS = 30
CLEANUP_POLL_SECONDS = 60
ORDER_TTL_SECONDS = 600
PROCESSED_ORDERS_KEY = "fs:processed_orders"

_FETCH_ORDER_SQL = """
SELECT
    order_id,
    user_id,
    store_id,
    merchant_id,
    device_id,
    ip_address,
    payment_method_id,
    delivery_address_id,
    total_pence,
    placed_at,
    user_email_domain
FROM orders
WHERE order_id = $1
"""

_BACKUP_POLL_SQL = """
SELECT
    order_id,
    user_id,
    store_id,
    merchant_id,
    device_id,
    ip_address,
    payment_method_id,
    delivery_address_id,
    total_pence,
    placed_at,
    user_email_domain
FROM orders
WHERE placed_at >= NOW() - INTERVAL '60 seconds'
"""


def _configure_logging() -> None:
    if LOGGER.handlers:
        return

    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(  # type: ignore[no-untyped-call]
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level"},
    )
    handler.setFormatter(formatter)
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


def _utcnow_ts() -> int:
    return int(datetime.datetime.now(tz=LONDON_TZ).timestamp())


@dataclass
class Metrics:
    errors: list[int]


@dataclass(frozen=True)
class _OrderContext:
    order_id: UUID
    user_id: UUID
    store_id: UUID
    merchant_id: UUID
    device_id: UUID | None
    ip_address: str
    payment_method_id: UUID | None
    delivery_address_id: UUID | None
    total_pence: int
    placed_at_ts: int
    user_email_domain: str


def _coerce_uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise TypeError(f"expected UUID for {field}, got {type(value)!r}")


def _coerce_uuid_or_none(value: object, field: str) -> UUID | None:
    if value is None:
        return None
    return _coerce_uuid(value, field)


def _coerce_order_context(row: asyncpg.Record) -> _OrderContext:
    placed_at = row["placed_at"]
    if not isinstance(placed_at, datetime.datetime):
        raise TypeError(f"expected datetime for placed_at, got {type(placed_at)!r}")
    if placed_at.tzinfo is None:
        placed_at = placed_at.replace(tzinfo=LONDON_TZ)
    return _OrderContext(
        order_id=_coerce_uuid(row["order_id"], "order_id"),
        user_id=_coerce_uuid(row["user_id"], "user_id"),
        store_id=_coerce_uuid(row["store_id"], "store_id"),
        merchant_id=_coerce_uuid(row["merchant_id"], "merchant_id"),
        device_id=_coerce_uuid_or_none(row["device_id"], "device_id"),
        ip_address=str(row["ip_address"]),
        payment_method_id=_coerce_uuid_or_none(row["payment_method_id"], "payment_method_id"),
        delivery_address_id=_coerce_uuid_or_none(row["delivery_address_id"], "delivery_address_id"),
        total_pence=int(row["total_pence"]),
        placed_at_ts=int(placed_at.timestamp()),
        user_email_domain=str(row["user_email_domain"]),
    )


def _safe_int(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    if isinstance(value, (bytes, bytearray)):
        return int(value)
    return 0


def _stringify_members(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    parsed: list[str] = []
    for value in values:
        if value is not None:
            parsed.append(str(value))
    return parsed


def _sum_spend_from_members(values: Iterable[str]) -> int:
    total = 0
    for value in values:
        _, _, pence = str(value).rpartition(":")
        if pence == "":
            continue
        try:
            total += int(pence)
        except ValueError:
            continue
    return total


def _coerce_int(value: object) -> int:
    return _safe_int(value)


async def write_user_stream_aggregates(
    redis_conn: AsyncRedis[str],
    order: _OrderContext,
    now_ts: int,
) -> None:
    user_id = str(order.user_id)
    order_id = str(order.order_id)
    store_id = str(order.store_id)
    stream_key = f"fs:user:{user_id}:stream"
    orders_zset_key = f"fs:user:{user_id}:orders_zset"
    stores_zset_key = f"fs:user:{user_id}:stores_zset"
    payments_zset_key = f"fs:user:{user_id}:payments_zset"
    spend_zset_key = f"fs:user:{user_id}:spend_zset"

    write_pipe = redis_conn.pipeline()
    write_pipe.zadd(orders_zset_key, {order_id: now_ts})

    result_cursor = 1
    if order.store_id is not None:
        write_pipe.zadd(stores_zset_key, {store_id: now_ts})
        result_cursor += 1
    if order.payment_method_id is not None:
        write_pipe.zadd(payments_zset_key, {str(order.payment_method_id): now_ts})
        result_cursor += 1

    write_pipe.zadd(spend_zset_key, {f"{order_id}:{order.total_pence}": now_ts})

    write_pipe.zrangebyscore(spend_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    spend_1h_index = result_cursor
    result_cursor += 1
    write_pipe.zrangebyscore(spend_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    spend_24h_index = result_cursor
    result_cursor += 1
    write_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    orders_1h_index = result_cursor
    result_cursor += 1
    write_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    orders_24h_index = result_cursor
    result_cursor += 1
    write_pipe.zcount(stores_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    stores_24h_index = result_cursor
    result_cursor += 1
    write_pipe.zcount(payments_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    payments_24h_index = result_cursor
    result_cursor += 1

    results = await write_pipe.execute()

    spend_1h = _sum_spend_from_members(
        _stringify_members(results[spend_1h_index] if len(results) > spend_1h_index else None),
    )
    spend_24h = _sum_spend_from_members(
        _stringify_members(results[spend_24h_index] if len(results) > spend_24h_index else None),
    )
    orders_1h = _safe_int(results[orders_1h_index] if len(results) > orders_1h_index else None)
    orders_24h = _safe_int(results[orders_24h_index] if len(results) > orders_24h_index else None)
    unique_stores_24h = _safe_int(
        results[stores_24h_index] if len(results) > stores_24h_index else None,
    )
    unique_payment_methods_24h = _safe_int(
        results[payments_24h_index] if len(results) > payments_24h_index else None,
    )

    age_minutes = max(0, (now_ts - order.placed_at_ts) // 60)
    persist_pipe = redis_conn.pipeline()
    persist_pipe.hset(
        stream_key,
        mapping={
            "orders_1h": orders_1h,
            "orders_24h": orders_24h,
            "spend_1h_pence": spend_1h,
            "spend_24h_pence": spend_24h,
            "unique_stores_24h": unique_stores_24h,
            "unique_payment_methods_24h": unique_payment_methods_24h,
            "last_order_age_minutes": age_minutes,
            "updated_at": now_ts,
        },
    )
    await persist_pipe.execute()


async def write_device_stream_aggregates(
    redis_conn: AsyncRedis[str],
    order: _OrderContext,
    now_ts: int,
) -> None:
    if order.device_id is None:
        return

    device_id = str(order.device_id)
    order_id = str(order.order_id)
    stream_key = f"fs:device:{device_id}:stream"
    orders_zset_key = f"fs:device:{device_id}:orders_zset"
    users_zset_key = f"fs:device:{device_id}:users_zset"
    payments_zset_key = f"fs:device:{device_id}:payments_zset"

    write_pipe = redis_conn.pipeline()
    write_pipe.zadd(orders_zset_key, {order_id: now_ts})
    write_pipe.zadd(users_zset_key, {str(order.user_id): now_ts})
    if order.payment_method_id is not None:
        write_pipe.zadd(payments_zset_key, {str(order.payment_method_id): now_ts})
    write_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    orders_1h_index = 3 if order.payment_method_id is None else 4
    write_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    orders_24h_index = 4 if order.payment_method_id is None else 5
    write_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    users_24h_index = 5 if order.payment_method_id is None else 6
    write_pipe.zcount(payments_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    payment_methods_24h_index = 6 if order.payment_method_id is None else 7

    results = await write_pipe.execute()
    orders_1h = _coerce_int(results[orders_1h_index] if len(results) > orders_1h_index else None)
    orders_24h = _coerce_int(results[orders_24h_index] if len(results) > orders_24h_index else None)
    unique_users_24h = _coerce_int(
        results[users_24h_index] if len(results) > users_24h_index else None
    )
    unique_payment_methods_24h = _coerce_int(
        results[payment_methods_24h_index] if len(results) > payment_methods_24h_index else None,
    )

    persist_pipe = redis_conn.pipeline()
    persist_pipe.hset(
        stream_key,
        mapping={
            "orders_1h": orders_1h,
            "orders_24h": orders_24h,
            "unique_users_24h": unique_users_24h,
            "unique_payment_methods_24h": unique_payment_methods_24h,
            "updated_at": now_ts,
        },
    )
    await persist_pipe.execute()


async def write_payment_stream_aggregates(
    redis_conn: AsyncRedis[str],
    order: _OrderContext,
    now_ts: int,
) -> None:
    if order.payment_method_id is None:
        return

    payment_id = str(order.payment_method_id)
    order_id = str(order.order_id)
    stream_key = f"fs:payment:{payment_id}:stream"
    orders_zset_key = f"fs:payment:{payment_id}:orders_zset"
    users_zset_key = f"fs:payment:{payment_id}:users_zset"

    write_pipe = redis_conn.pipeline()
    write_pipe.zadd(orders_zset_key, {order_id: now_ts})
    write_pipe.zadd(users_zset_key, {str(order.user_id): now_ts})
    write_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    orders_1h_index = 2
    write_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    orders_24h_index = 3
    write_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    users_24h_index = 4

    results = await write_pipe.execute()
    orders_1h = _coerce_int(results[orders_1h_index] if len(results) > orders_1h_index else None)
    orders_24h = _coerce_int(results[orders_24h_index] if len(results) > orders_24h_index else None)
    unique_users_24h = _coerce_int(
        results[users_24h_index] if len(results) > users_24h_index else None
    )

    persist_pipe = redis_conn.pipeline()
    persist_pipe.hset(
        stream_key,
        mapping={
            "orders_1h": orders_1h,
            "orders_24h": orders_24h,
            "unique_users_24h": unique_users_24h,
            "decline_count_24h": 0,
            "updated_at": now_ts,
        },
    )
    await persist_pipe.execute()


async def write_ip_stream_aggregates(
    redis_conn: AsyncRedis[str],
    order: _OrderContext,
    now_ts: int,
) -> None:
    ip_key = str(order.ip_address)
    order_id = str(order.order_id)
    stream_key = f"fs:ip:{ip_key}:stream"
    orders_zset_key = f"fs:ip:{ip_key}:orders_zset"
    users_zset_key = f"fs:ip:{ip_key}:users_zset"
    devices_zset_key = f"fs:ip:{ip_key}:devices_zset"

    write_pipe = redis_conn.pipeline()
    write_pipe.zadd(orders_zset_key, {order_id: now_ts})
    write_pipe.zadd(users_zset_key, {str(order.user_id): now_ts})
    if order.device_id is not None:
        write_pipe.zadd(devices_zset_key, {str(order.device_id): now_ts})
    write_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    orders_1h_index = 3 if order.device_id is None else 4
    write_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    orders_24h_index = 4 if order.device_id is None else 5
    write_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    users_24h_index = 5 if order.device_id is None else 6
    write_pipe.zcount(devices_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    devices_24h_index = 6 if order.device_id is None else 7

    results = await write_pipe.execute()
    orders_1h = _coerce_int(results[orders_1h_index] if len(results) > orders_1h_index else None)
    orders_24h = _coerce_int(results[orders_24h_index] if len(results) > orders_24h_index else None)
    unique_users_24h = _coerce_int(
        results[users_24h_index] if len(results) > users_24h_index else None
    )
    unique_devices_24h = _coerce_int(
        results[devices_24h_index] if len(results) > devices_24h_index else None
    )

    persist_pipe = redis_conn.pipeline()
    persist_pipe.hset(
        stream_key,
        mapping={
            "orders_1h": orders_1h,
            "orders_24h": orders_24h,
            "unique_users_24h": unique_users_24h,
            "unique_devices_24h": unique_devices_24h,
            "updated_at": now_ts,
        },
    )
    await persist_pipe.execute()


async def write_store_stream_aggregates(
    redis_conn: AsyncRedis[str],
    order: _OrderContext,
    now_ts: int,
) -> None:
    store_id = str(order.store_id)
    order_id = str(order.order_id)
    stream_key = f"fs:store:{store_id}:stream"
    orders_zset_key = f"fs:store:{store_id}:orders_zset"
    users_zset_key = f"fs:store:{store_id}:users_zset"
    cards_1h_zset_key = f"fs:store:{store_id}:cards_1h_zset"

    write_pipe = redis_conn.pipeline()
    write_pipe.zadd(orders_zset_key, {order_id: now_ts})
    write_pipe.zadd(users_zset_key, {str(order.user_id): now_ts})
    if order.payment_method_id is not None:
        write_pipe.zadd(cards_1h_zset_key, {str(order.payment_method_id): now_ts})
    write_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    orders_1h_index = 3 if order.payment_method_id is None else 4
    write_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    orders_24h_index = 4 if order.payment_method_id is None else 5
    write_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    users_24h_index = 5 if order.payment_method_id is None else 6
    write_pipe.zcount(cards_1h_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    cards_1h_index = 6 if order.payment_method_id is None else 7

    results = await write_pipe.execute()
    orders_1h = _coerce_int(results[orders_1h_index] if len(results) > orders_1h_index else None)
    orders_24h = _coerce_int(results[orders_24h_index] if len(results) > orders_24h_index else None)
    unique_users_24h = _coerce_int(
        results[users_24h_index] if len(results) > users_24h_index else None
    )
    unique_cards_1h = _coerce_int(
        results[cards_1h_index] if len(results) > cards_1h_index else None
    )

    persist_pipe = redis_conn.pipeline()
    persist_pipe.hset(
        stream_key,
        mapping={
            "orders_1h": orders_1h,
            "orders_24h": orders_24h,
            "unique_users_24h": unique_users_24h,
            "unique_cards_1h": unique_cards_1h,
            "updated_at": now_ts,
        },
    )
    await persist_pipe.execute()


async def write_address_stream_aggregates(
    redis_conn: AsyncRedis[str],
    order: _OrderContext,
    now_ts: int,
) -> None:
    if order.delivery_address_id is None:
        return

    address_id = str(order.delivery_address_id)
    order_id = str(order.order_id)
    stream_key = f"fs:address:{address_id}:stream"
    orders_zset_key = f"fs:address:{address_id}:orders_zset"
    users_zset_key = f"fs:address:{address_id}:users_zset"

    write_pipe = redis_conn.pipeline()
    write_pipe.zadd(orders_zset_key, {order_id: now_ts})
    write_pipe.zadd(users_zset_key, {str(order.user_id): now_ts})
    write_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    orders_24h_index = 2
    write_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    users_24h_index = 3

    results = await write_pipe.execute()
    orders_24h = _coerce_int(results[orders_24h_index] if len(results) > orders_24h_index else None)
    unique_users_24h = _coerce_int(
        results[users_24h_index] if len(results) > users_24h_index else None
    )

    persist_pipe = redis_conn.pipeline()
    persist_pipe.hset(
        stream_key,
        mapping={
            "orders_24h": orders_24h,
            "unique_users_24h": unique_users_24h,
            "updated_at": now_ts,
        },
    )
    await persist_pipe.execute()


async def _mark_order_processed(redis_conn: AsyncRedis[str], order_id: str) -> None:
    await redis_conn.sadd(PROCESSED_ORDERS_KEY, order_id)
    await redis_conn.expire(PROCESSED_ORDERS_KEY, ORDER_TTL_SECONDS)


async def update_features_for_order(
    pool: asyncpg.Pool,
    redis_conn: AsyncRedis[str],
    order_id: UUID,
) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_FETCH_ORDER_SQL, order_id)

    if row is None:
        LOGGER.warning(
            "order_row_missing", extra={"event": "order_row_missing", "order_id": str(order_id)}
        )
        return False

    now_ts = _utcnow_ts()
    order = _coerce_order_context(row)

    await write_user_stream_aggregates(redis_conn=redis_conn, order=order, now_ts=now_ts)
    await write_device_stream_aggregates(redis_conn=redis_conn, order=order, now_ts=now_ts)
    await write_payment_stream_aggregates(redis_conn=redis_conn, order=order, now_ts=now_ts)
    await write_ip_stream_aggregates(redis_conn=redis_conn, order=order, now_ts=now_ts)
    await write_store_stream_aggregates(redis_conn=redis_conn, order=order, now_ts=now_ts)
    await write_address_stream_aggregates(redis_conn=redis_conn, order=order, now_ts=now_ts)
    await _mark_order_processed(redis_conn=redis_conn, order_id=str(order_id))
    return True


async def run_backup_poll_once(
    pool: asyncpg.Pool,
    redis_conn: AsyncRedis[str],
    metrics: Metrics,
) -> None:
    async with pool.acquire() as conn:
        rows = await conn.fetch(_BACKUP_POLL_SQL)

    for row in rows:
        order_id = _coerce_uuid(row["order_id"], "order_id")
        order_id_str = str(order_id)
        try:
            processed = await redis_conn.sismember(PROCESSED_ORDERS_KEY, order_id_str)
            if processed:
                LOGGER.info(
                    "order_already_processed",
                    extra={"event": "backup_order_skipped", "order_id": order_id_str},
                )
                continue
            await update_features_for_order(pool=pool, redis_conn=redis_conn, order_id=order_id)
        except Exception as exc:  # noqa: BLE001
            del exc
            metrics.errors[0] += 1
            LOGGER.exception(
                "backup_poll_update_failed",
                extra={"event": "backup_poll_update_failed", "order_id": order_id_str},
            )


async def run_backup_poll_loop(
    pool: asyncpg.Pool,
    redis_conn: AsyncRedis[str],
    metrics: Metrics,
) -> None:
    while True:
        try:
            await run_backup_poll_once(pool=pool, redis_conn=redis_conn, metrics=metrics)
        except Exception as exc:  # noqa: BLE001
            del exc
            metrics.errors[0] += 1
            LOGGER.exception("backup_poll_loop_failed")
        await asyncio.sleep(BACKUP_POLL_SECONDS)


async def trim_order_zsets_once(redis_conn: AsyncRedis[str], metrics: Metrics) -> None:
    del metrics
    cutoff = str(_utcnow_ts() - TWENTY_FOUR_HOURS_SECONDS)
    cursor = 0
    while True:
        cursor, keys = await redis_conn.scan(cursor=cursor, match="fs:*:orders_zset", count=5000)
        if keys:
            pipe = redis_conn.pipeline()
            for key in keys:
                pipe.zremrangebyscore(key, "-inf", cutoff)
            await pipe.execute()
        if cursor == 0:
            break


async def run_cleanup_loop(redis_conn: AsyncRedis[str], metrics: Metrics) -> None:
    while True:
        try:
            await trim_order_zsets_once(redis_conn=redis_conn, metrics=metrics)
        except Exception as exc:  # noqa: BLE001
            del exc
            metrics.errors[0] += 1
            LOGGER.exception("cleanup_loop_failed")
        await asyncio.sleep(CLEANUP_POLL_SECONDS)


def _on_order_placed(
    pool: asyncpg.Pool,
    redis_conn: AsyncRedis[str],
    metrics: Metrics,
    connection: Any,
    pid: int,
    channel: str,
    payload: str,
) -> None:
    del connection
    del pid
    del channel
    asyncio.ensure_future(
        _on_order_placed_async(
            pool=pool,
            redis_conn=redis_conn,
            metrics=metrics,
            payload=payload,
        ),
    )


async def _on_order_placed_async(
    pool: asyncpg.Pool,
    redis_conn: AsyncRedis[str],
    metrics: Metrics,
    payload: str,
) -> None:
    try:
        order_id = UUID(payload.strip())
        updated = await update_features_for_order(
            pool=pool, redis_conn=redis_conn, order_id=order_id
        )
        if updated:
            LOGGER.info(
                "order_features_updated",
                extra={"event": "order_features_updated", "order_id": payload},
            )
    except ValueError:
        metrics.errors[0] += 1
        LOGGER.exception(
            "invalid_order_id", extra={"event": "invalid_order_id", "payload": payload}
        )
    except Exception as exc:
        metrics.errors[0] += 1
        del exc
        LOGGER.exception(
            "notify_handler_failed", extra={"event": "notify_handler_failed", "payload": payload}
        )


async def main() -> None:
    _configure_logging()

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=20)
    redis_conn = aioredis.from_url(REDIS_URL, decode_responses=True)
    metrics = Metrics(errors=[0])

    listener_conn = await asyncpg.connect(DATABASE_URL)

    def listener_callback(connection: Any, pid: int, channel: str, payload: str) -> None:
        _on_order_placed(
            pool=pool,
            redis_conn=redis_conn,
            metrics=metrics,
            connection=connection,
            pid=pid,
            channel=channel,
            payload=payload,
        )

    try:
        await listener_conn.add_listener("order_placed", listener_callback)
        await listener_conn.execute("LISTEN order_placed")
        LOGGER.info(
            "feature_aggregator_started",
            extra={"event": "feature_aggregator_started", "service": LOGGER_NAME},
        )
        backup_task = asyncio.create_task(
            run_backup_poll_loop(pool=pool, redis_conn=redis_conn, metrics=metrics)
        )
        cleanup_task = asyncio.create_task(run_cleanup_loop(redis_conn=redis_conn, metrics=metrics))

        try:
            while True:
                await asyncio.sleep(60 * 60)
        finally:
            for task in (backup_task, cleanup_task):
                task.cancel()
            await asyncio.gather(backup_task, cleanup_task, return_exceptions=True)
    finally:
        await listener_conn.remove_listener("order_placed", listener_callback)
        await listener_conn.close()
        await pool.close()
        await redis_conn.close()


def main_sync() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
