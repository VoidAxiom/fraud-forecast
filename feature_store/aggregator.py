from __future__ import annotations

import asyncio
import datetime
import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable, cast
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
PROCESSED_ORDERS_KEY_PREFIX = "fs:processed_orders"
CLEANUP_ZSET_SUFFIXES = (
    ":orders_zset",
    ":spend_zset",
    ":stores_zset",
    ":payments_zset",
    ":users_zset",
    ":devices_zset",
    ":cards_1h_zset",
)
PROCESSED_ORDERS_TTL_SECONDS = ORDER_TTL_SECONDS + 300

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


def _last_order_age_minutes(last_scores: list[tuple[str, object]], now_ts: int) -> int | None:
    if not last_scores:
        return None
    _, raw_score = last_scores[0]
    if isinstance(raw_score, (int, float, bytes, bytearray, str)):
        last_order_ts = int(raw_score)
    else:
        return None
    return max(0, now_ts - last_order_ts) // 60


def processed_bucket_key(now_ts: int) -> str:
    return f"{PROCESSED_ORDERS_KEY_PREFIX}:{(now_ts // ORDER_TTL_SECONDS) * ORDER_TTL_SECONDS}"


async def write_user_stream_aggregates(
    redis_conn: AsyncRedis[str],
    order: _OrderContext,
    now_ts: int,
) -> None:
    user_id = str(order.user_id)
    order_id = str(order.order_id)
    store_id = str(order.store_id)
    orders_zset_key = f"fs:user:{user_id}:orders_zset"
    stores_zset_key = f"fs:user:{user_id}:stores_zset"
    payments_zset_key = f"fs:user:{user_id}:payments_zset"
    spend_zset_key = f"fs:user:{user_id}:spend_zset"
    write_pipe = redis_conn.pipeline()
    write_pipe.zadd(orders_zset_key, {order_id: now_ts})
    if order.store_id is not None:
        write_pipe.zadd(stores_zset_key, {store_id: now_ts})
    if order.payment_method_id is not None:
        write_pipe.zadd(payments_zset_key, {str(order.payment_method_id): now_ts})
    write_pipe.zadd(spend_zset_key, {f"{order_id}:{order.total_pence}": now_ts})
    await write_pipe.execute()
    await _refresh_user_stream_aggregates(
        redis_conn=redis_conn,
        user_id=user_id,
        now_ts=now_ts,
    )


async def _refresh_user_stream_aggregates(
    redis_conn: AsyncRedis[str],
    user_id: str,
    now_ts: int,
) -> None:
    stream_key = f"fs:user:{user_id}:stream"
    orders_zset_key = f"fs:user:{user_id}:orders_zset"
    stores_zset_key = f"fs:user:{user_id}:stores_zset"
    payments_zset_key = f"fs:user:{user_id}:payments_zset"
    spend_zset_key = f"fs:user:{user_id}:spend_zset"
    previous_orders = await redis_conn.zrevrange(
        orders_zset_key,
        0,
        0,
        withscores=True,
    )
    last_order_age_minutes = _last_order_age_minutes(
        cast(list[tuple[str, object]], previous_orders),
        now_ts,
    )

    read_pipe = redis_conn.pipeline()
    read_pipe.zrangebyscore(spend_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    read_pipe.zrangebyscore(spend_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    read_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    read_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    read_pipe.zcount(stores_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    read_pipe.zcount(payments_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)

    results = await read_pipe.execute()

    spend_1h = _sum_spend_from_members(_stringify_members(results[0] if len(results) > 0 else None))
    spend_24h = _sum_spend_from_members(
        _stringify_members(results[1] if len(results) > 1 else None),
    )
    orders_1h = _safe_int(results[2] if len(results) > 2 else None)
    orders_24h = _safe_int(results[3] if len(results) > 3 else None)
    unique_stores_24h = _safe_int(results[4] if len(results) > 4 else None)
    unique_payment_methods_24h = _safe_int(results[5] if len(results) > 5 else None)

    persist_pipe = redis_conn.pipeline()
    stream_mapping: dict[str | bytes, bytes | float | int | str] = {
        "orders_1h": orders_1h,
        "orders_24h": orders_24h,
        "spend_1h_pence": spend_1h,
        "spend_24h_pence": spend_24h,
        "unique_stores_24h": unique_stores_24h,
        "unique_payment_methods_24h": unique_payment_methods_24h,
        "updated_at": now_ts,
    }
    if last_order_age_minutes is not None:
        stream_mapping["last_order_age_minutes"] = last_order_age_minutes

    persist_pipe.hset(
        stream_key,
        mapping=stream_mapping,
    )
    persist_pipe.expire(stream_key, TWENTY_FOUR_HOURS_SECONDS)
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
    await write_pipe.execute()

    read_pipe = redis_conn.pipeline()
    read_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    read_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    read_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    read_pipe.zcount(payments_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)

    results = await read_pipe.execute()
    orders_1h = _coerce_int(results[0] if len(results) > 0 else None)
    orders_24h = _coerce_int(results[1] if len(results) > 1 else None)
    unique_users_24h = _coerce_int(results[2] if len(results) > 2 else None)
    unique_payment_methods_24h = _coerce_int(
        results[3] if len(results) > 3 else None,
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
    persist_pipe.expire(stream_key, TWENTY_FOUR_HOURS_SECONDS)
    await persist_pipe.execute()


async def _refresh_device_stream_aggregates(
    redis_conn: AsyncRedis[str],
    device_id: str,
    now_ts: int,
) -> None:
    stream_key = f"fs:device:{device_id}:stream"
    orders_zset_key = f"fs:device:{device_id}:orders_zset"
    users_zset_key = f"fs:device:{device_id}:users_zset"
    payments_zset_key = f"fs:device:{device_id}:payments_zset"

    read_pipe = redis_conn.pipeline()
    read_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    orders_1h_index = 0
    read_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    orders_24h_index = 1
    read_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    users_24h_index = 2
    read_pipe.zcount(payments_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    payment_methods_24h_index = 3

    results = await read_pipe.execute()
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
    persist_pipe.expire(stream_key, TWENTY_FOUR_HOURS_SECONDS)
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
    persist_pipe.expire(stream_key, TWENTY_FOUR_HOURS_SECONDS)
    await persist_pipe.execute()


async def _refresh_payment_stream_aggregates(
    redis_conn: AsyncRedis[str],
    payment_id: str,
    now_ts: int,
) -> None:
    stream_key = f"fs:payment:{payment_id}:stream"
    orders_zset_key = f"fs:payment:{payment_id}:orders_zset"
    users_zset_key = f"fs:payment:{payment_id}:users_zset"

    read_pipe = redis_conn.pipeline()
    read_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    orders_1h_index = 0
    read_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    orders_24h_index = 1
    read_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    users_24h_index = 2

    results = await read_pipe.execute()
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
    persist_pipe.expire(stream_key, TWENTY_FOUR_HOURS_SECONDS)
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
    await write_pipe.execute()

    read_pipe = redis_conn.pipeline()
    read_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    read_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    read_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    read_pipe.zcount(devices_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)

    results = await read_pipe.execute()
    orders_1h = _coerce_int(results[0] if len(results) > 0 else None)
    orders_24h = _coerce_int(results[1] if len(results) > 1 else None)
    unique_users_24h = _coerce_int(results[2] if len(results) > 2 else None)
    unique_devices_24h = _coerce_int(results[3] if len(results) > 3 else None)

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
    persist_pipe.expire(stream_key, TWENTY_FOUR_HOURS_SECONDS)
    await persist_pipe.execute()


async def _refresh_ip_stream_aggregates(
    redis_conn: AsyncRedis[str],
    ip_key: str,
    now_ts: int,
) -> None:
    stream_key = f"fs:ip:{ip_key}:stream"
    orders_zset_key = f"fs:ip:{ip_key}:orders_zset"
    users_zset_key = f"fs:ip:{ip_key}:users_zset"
    devices_zset_key = f"fs:ip:{ip_key}:devices_zset"

    read_pipe = redis_conn.pipeline()
    read_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    orders_1h_index = 0
    read_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    orders_24h_index = 1
    read_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    users_24h_index = 2
    read_pipe.zcount(devices_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    devices_24h_index = 3

    results = await read_pipe.execute()
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
    persist_pipe.expire(stream_key, TWENTY_FOUR_HOURS_SECONDS)
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
    await write_pipe.execute()

    read_pipe = redis_conn.pipeline()
    read_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    read_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    read_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    read_pipe.zcount(cards_1h_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)

    results = await read_pipe.execute()
    orders_1h = _coerce_int(results[0] if len(results) > 0 else None)
    orders_24h = _coerce_int(results[1] if len(results) > 1 else None)
    unique_users_24h = _coerce_int(results[2] if len(results) > 2 else None)
    unique_cards_1h = _coerce_int(results[3] if len(results) > 3 else None)

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
    persist_pipe.expire(stream_key, TWENTY_FOUR_HOURS_SECONDS)
    await persist_pipe.execute()


async def _refresh_store_stream_aggregates(
    redis_conn: AsyncRedis[str],
    store_id: str,
    now_ts: int,
) -> None:
    stream_key = f"fs:store:{store_id}:stream"
    orders_zset_key = f"fs:store:{store_id}:orders_zset"
    users_zset_key = f"fs:store:{store_id}:users_zset"
    cards_1h_zset_key = f"fs:store:{store_id}:cards_1h_zset"

    read_pipe = redis_conn.pipeline()
    read_pipe.zcount(orders_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    orders_1h_index = 0
    read_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    orders_24h_index = 1
    read_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    users_24h_index = 2
    read_pipe.zcount(cards_1h_zset_key, now_ts - ONE_HOUR_SECONDS, now_ts)
    cards_1h_index = 3

    results = await read_pipe.execute()
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
    persist_pipe.expire(stream_key, TWENTY_FOUR_HOURS_SECONDS)
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
    persist_pipe.expire(stream_key, TWENTY_FOUR_HOURS_SECONDS)
    await persist_pipe.execute()


async def _refresh_address_stream_aggregates(
    redis_conn: AsyncRedis[str],
    address_id: str,
    now_ts: int,
) -> None:
    stream_key = f"fs:address:{address_id}:stream"
    orders_zset_key = f"fs:address:{address_id}:orders_zset"
    users_zset_key = f"fs:address:{address_id}:users_zset"

    read_pipe = redis_conn.pipeline()
    read_pipe.zcount(orders_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    orders_24h_index = 0
    read_pipe.zcount(users_zset_key, now_ts - TWENTY_FOUR_HOURS_SECONDS, now_ts)
    users_24h_index = 1

    results = await read_pipe.execute()
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
    persist_pipe.expire(stream_key, TWENTY_FOUR_HOURS_SECONDS)
    await persist_pipe.execute()


async def _mark_order_processed(redis_conn: AsyncRedis[str], order_id: str) -> None:
    processed_bucket = processed_bucket_key(_utcnow_ts())
    await redis_conn.sadd(processed_bucket, order_id)
    await redis_conn.expire(processed_bucket, PROCESSED_ORDERS_TTL_SECONDS)


async def _was_order_processed(redis_conn: AsyncRedis[str], order_id: str, now_ts: int) -> bool:
    current_bucket = processed_bucket_key(now_ts)
    processed = bool(await redis_conn.sismember(current_bucket, order_id))
    if processed:
        return True
    previous_bucket = processed_bucket_key(now_ts - ORDER_TTL_SECONDS)
    return bool(await redis_conn.sismember(previous_bucket, order_id))


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
        now_ts = _utcnow_ts()
        try:
            processed = await _was_order_processed(
                redis_conn=redis_conn, order_id=order_id_str, now_ts=now_ts
            )
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


def _strip_prefix(value: str, prefix: str) -> str:
    return value[len(prefix) :] if value.startswith(prefix) else value


def _strip_suffix(value: str, suffix: str) -> str:
    return value[: -len(suffix)] if value.endswith(suffix) else value


def _extract_entity_from_zset_key(key: str, suffix: str) -> tuple[str, str] | None:
    key_prefix = "fs:"
    suffix_token = suffix
    if not key.startswith(key_prefix) or not key.endswith(suffix_token):
        return None
    entity_fragment = _strip_prefix(key, key_prefix)
    entity_fragment = _strip_suffix(entity_fragment, suffix_token)
    entity_type, _, entity_id = entity_fragment.partition(":")
    if not entity_id:
        return None
    return entity_type, entity_id


async def trim_order_zsets_once(redis_conn: AsyncRedis[str], metrics: Metrics) -> None:
    del metrics
    now_ts = _utcnow_ts()
    cutoff = str(now_ts - TWENTY_FOUR_HOURS_SECONDS)
    refresh_required_by_type: dict[str, set[str]] = {
        "user": set(),
        "device": set(),
        "payment": set(),
        "ip": set(),
        "store": set(),
        "address": set(),
    }

    for suffix in CLEANUP_ZSET_SUFFIXES:
        cursor = 0
        while True:
            cursor, keys = await redis_conn.scan(cursor=cursor, match=f"fs:*{suffix}", count=5000)
            if keys:
                pipe = redis_conn.pipeline()
                for key in keys:
                    pipe.zremrangebyscore(key, "-inf", cutoff)
                trimmed_counts = await pipe.execute()

                for key, trimmed_count in zip(keys, trimmed_counts):
                    if _safe_int(trimmed_count) <= 0:
                        continue
                    entity = _extract_entity_from_zset_key(key=key, suffix=suffix)
                    if entity is None:
                        continue
                    entity_type, entity_id = entity
                    if entity_type in refresh_required_by_type:
                        refresh_required_by_type[entity_type].add(entity_id)
            if cursor == 0:
                break

    for user_id in refresh_required_by_type["user"]:
        await _refresh_user_stream_aggregates(
            redis_conn=redis_conn,
            user_id=user_id,
            now_ts=now_ts,
        )
    for device_id in refresh_required_by_type["device"]:
        await _refresh_device_stream_aggregates(
            redis_conn=redis_conn,
            device_id=device_id,
            now_ts=now_ts,
        )
    for payment_id in refresh_required_by_type["payment"]:
        await _refresh_payment_stream_aggregates(
            redis_conn=redis_conn,
            payment_id=payment_id,
            now_ts=now_ts,
        )
    for ip_key in refresh_required_by_type["ip"]:
        await _refresh_ip_stream_aggregates(
            redis_conn=redis_conn,
            ip_key=ip_key,
            now_ts=now_ts,
        )
    for store_id in refresh_required_by_type["store"]:
        await _refresh_store_stream_aggregates(
            redis_conn=redis_conn,
            store_id=store_id,
            now_ts=now_ts,
        )
    for address_id in refresh_required_by_type["address"]:
        await _refresh_address_stream_aggregates(
            redis_conn=redis_conn,
            address_id=address_id,
            now_ts=now_ts,
        )


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
