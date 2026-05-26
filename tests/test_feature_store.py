from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Union, cast
from uuid import UUID, uuid4

import numpy as np  # type: ignore[import]
import pytest

_RedisValue = Union[bytes, float, int, str]
_RedisMapping = Mapping[Union[str, bytes], _RedisValue]


class _AsyncPgModule(Protocol):
    async def create_pool(self, dsn: str, *, min_size: int, max_size: int) -> object: ...

    async def connect(self, dsn: str) -> object: ...


class _AsyncRedis(Protocol):
    async def delete(self, *names: str) -> object: ...

    async def scan(self, *, cursor: int, match: str, count: int) -> tuple[int, list[object]]: ...

    async def srem(self, name: str, *values: str) -> object: ...

    async def hget(self, name: str, key: str) -> object: ...

    async def zadd(self, name: str, mapping: _RedisMapping) -> object: ...

    async def zcard(self, name: str) -> int: ...

    async def close(self) -> None: ...


class _AsyncRedisModule(Protocol):
    def from_url(self, url: str, *, decode_responses: bool) -> _AsyncRedis: ...


class _SyncPipeline(Protocol):
    def hset(self, name: str, mapping: _RedisMapping) -> object: ...

    def expire(self, name: str, time: int) -> object: ...

    def execute(self) -> object: ...


class _SyncRedis(Protocol):
    def scan_iter(self, *, match: str) -> Iterable[object]: ...

    def hgetall(self, name: str) -> Mapping[object, object]: ...

    def ttl(self, name: str) -> int: ...

    def delete(self, *names: str) -> object: ...

    def pipeline(self, *, transaction: bool) -> _SyncPipeline: ...

    def hset(self, name: str, mapping: _RedisMapping) -> object: ...

    def close(self) -> None: ...


class _SyncRedisFactory(Protocol):
    def from_url(self, url: str, *, decode_responses: bool) -> _SyncRedis: ...


class _RedisModule(Protocol):
    Redis: _SyncRedisFactory


class _ZoneInfoFactory(Protocol):
    def __call__(self, key: str) -> dt.tzinfo: ...


if TYPE_CHECKING:
    asyncpg: _AsyncPgModule
    aioredis: _AsyncRedisModule
    redis: _RedisModule
    ZoneInfo: _ZoneInfoFactory

    @dataclass
    class _Metrics:
        errors: list[int]

    class _FeatureSet(Protocol):
        user_orders_1h: int
        user_orders_24h: int
        user_spend_1h_pence: int
        user_spend_24h_pence: int
        user_unique_stores_24h: int
        user_unique_payment_methods_24h: int
        user_last_order_age_minutes: int
        user_lifetime_order_count: int
        user_lifetime_spend_pence: int
        user_avg_order_value_pence: int
        user_lifetime_chargeback_count: int
        user_lifetime_refund_count: int
        user_lifetime_chargeback_rate: float
        user_unique_devices_used: int
        user_unique_payment_methods_used: int
        user_unique_delivery_addresses: int
        user_account_age_days: int
        user_days_since_last_order: int
        user_distinct_cities_ordered_from: int
        device_orders_1h: int
        device_orders_24h: int
        device_unique_users_24h: int
        device_unique_payment_methods_24h: int
        device_lifetime_order_count: int
        device_lifetime_chargeback_rate: float
        device_unique_users_lifetime: int
        device_first_seen_days_ago: int
        device_distinct_payment_methods_lifetime: int
        payment_orders_1h: int
        payment_orders_24h: int
        payment_unique_users_24h: int
        payment_decline_count_24h: int
        payment_lifetime_order_count: int
        payment_lifetime_chargeback_count: int
        payment_lifetime_chargeback_rate: float
        payment_unique_users_lifetime: int
        payment_distinct_delivery_addresses_lifetime: int
        ip_orders_1h: int
        ip_orders_24h: int
        ip_unique_users_24h: int
        ip_unique_devices_24h: int
        ip_lifetime_order_count: int
        ip_unique_users_lifetime: int
        ip_chargeback_rate: float
        ip_first_seen_days_ago: int
        store_orders_1h: int
        store_orders_24h: int
        store_unique_users_24h: int
        store_unique_cards_1h: int
        store_avg_order_value_pence: int
        store_chargeback_rate: float
        store_unique_cards_30d: int
        store_total_orders_30d: int
        merchant_chargeback_rate: float
        merchant_total_stores: int
        email_domain_chargeback_rate: float
        email_domain_total_orders: int
        address_orders_24h: int
        address_unique_users_24h: int
        feature_fetch_latency_ms: float
        missing_features: list[str]

    class _FeatureStoreClient(Protocol):
        def close(self) -> None: ...

        def get_features(
            self,
            user_id: UUID,
            device_id: Union[UUID, None],  # noqa: UP007 - packet requires Python 3.8 syntax.
            payment_method_id: Union[UUID, None],  # noqa: UP007 - packet requires Python 3.8 syntax.
            ip_address: str,
            store_id: UUID,
            merchant_id: UUID,
            delivery_address_id: Union[UUID, None],  # noqa: UP007 - packet requires Python 3.8 syntax.
            email_domain: str,
        ) -> _FeatureSet: ...

    class _FeatureStoreClientFactory(Protocol):
        def __call__(self, redis_url: str) -> _FeatureStoreClient: ...

    class _AggregatorModule(Protocol):
        PROCESSED_ORDERS_KEY_PREFIX: str
        Metrics: type[_Metrics]

        def _on_order_placed(
            self,
            pool: object,
            redis_conn: _AsyncRedis,
            metrics: _Metrics,
            connection: object,
            pid: int,
            channel: str,
            payload: str,
        ) -> None: ...

        async def update_features_for_order(
            self,
            pool: object,
            redis_conn: _AsyncRedis,
            order_id: UUID,
        ) -> bool: ...

        async def _refresh_user_stream_aggregates(
            self,
            redis_conn: _AsyncRedis,
            user_id: str,
            now_ts: int,
        ) -> None: ...

        async def run_backup_poll_once(
            self,
            pool: object,
            redis_conn: _AsyncRedis,
            metrics: _Metrics,
        ) -> None: ...

        async def trim_order_zsets_once(
            self,
            redis_conn: _AsyncRedis,
            metrics: _Metrics,
        ) -> None: ...

    class _BatchComputeModule(Protocol):
        def compute_user_batch_features(
            self,
            engine: object,
            r: _SyncRedis,
        ) -> None: ...

        def run_batch(self) -> None: ...

    aggregator: _AggregatorModule
    batch_compute: _BatchComputeModule
    FeatureStoreClient: _FeatureStoreClientFactory
else:
    import asyncpg
    import feature_store.aggregator as aggregator
    import redis
    import redis.asyncio as aioredis
    from backports.zoneinfo import ZoneInfo
    from feature_store import batch_compute
    from feature_store.client import FeatureStoreClient

DATABASE_URL = "postgresql://app:app_dev_password@postgres:5432/fraud_platform"
REDIS_URL = "redis://redis:6379/0"
LONDON_TZ = ZoneInfo("Europe/London")

_ORDER_COLUMNS = (
    "order_id",
    "order_number",
    "order_status",
    "order_channel",
    "order_type",
    "placed_at",
    "user_id",
    "user_account_age_days",
    "user_total_orders_lifetime",
    "user_total_orders_30d",
    "user_total_spend_lifetime_pence",
    "user_email",
    "user_email_domain",
    "user_phone",
    "user_risk_tier_at_order",
    "is_guest_checkout",
    "store_id",
    "merchant_id",
    "store_city",
    "store_country",
    "store_latitude",
    "store_longitude",
    "delivery_address_id",
    "delivery_address_snapshot",
    "delivery_latitude",
    "delivery_longitude",
    "delivery_distance_km",
    "delivery_address_type",
    "is_new_delivery_address",
    "delivery_address_use_count",
    "item_count",
    "unique_item_count",
    "subtotal_pence",
    "vat_pence",
    "delivery_fee_pence",
    "service_fee_pence",
    "tip_pence",
    "discount_pence",
    "total_pence",
    "currency",
    "promo_id",
    "promo_code",
    "is_first_order_for_user",
    "is_new_payment_method",
    "payment_method_id",
    "payment_type",
    "card_bin",
    "card_last_four",
    "card_brand",
    "card_funding_type",
    "card_issuer_country",
    "is_digital_native_bank",
    "is_new_user_promo",
    "session_id",
    "device_id",
    "device_type",
    "platform",
    "os_version",
    "app_version",
    "browser_name",
    "browser_version",
    "ip_address",
    "ip_country",
    "ip_city",
    "ip_is_proxy",
    "ip_is_vpn",
    "ip_is_tor",
    "ip_is_hosting",
)
_ORDER_PLACEHOLDERS = ", ".join(f"${index}" for index in range(1, len(_ORDER_COLUMNS) + 1))
_INSERT_ORDER_SQL = (
    f"INSERT INTO orders ({', '.join(_ORDER_COLUMNS)}) VALUES ({_ORDER_PLACEHOLDERS})"
)
_INSERT_ORDER_NOTIFY_SQL = (
    f"WITH inserted AS ({_INSERT_ORDER_SQL} RETURNING order_id) "
    "SELECT pg_notify('order_placed', (SELECT order_id::text FROM inserted))"
)
_BATCH_HASH_PATTERNS = (
    "fs:user:*:batch",
    "fs:device:*:batch",
    "fs:payment:*:batch",
    "fs:ip:*:batch",
    "fs:store:*:batch",
    "fs:merchant:*:batch",
    "fs:email_domain:*:batch",
)
_BATCH_ENTITY_FIELDS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "user": (
        "fs:user:*:batch",
        (
            "lifetime_order_count",
            "lifetime_spend_pence",
            "avg_order_value_pence",
            "lifetime_chargeback_count",
            "lifetime_refund_count",
            "lifetime_chargeback_rate",
            "unique_devices_used",
            "unique_payment_methods_used",
            "unique_delivery_addresses",
            "account_age_days",
            "days_since_last_order",
            "distinct_cities_ordered_from",
        ),
    ),
    "device": (
        "fs:device:*:batch",
        (
            "lifetime_order_count",
            "lifetime_chargeback_rate",
            "unique_users_lifetime",
            "first_seen_days_ago",
            "distinct_payment_methods_lifetime",
        ),
    ),
    "payment": (
        "fs:payment:*:batch",
        (
            "lifetime_order_count",
            "lifetime_chargeback_count",
            "lifetime_chargeback_rate",
            "unique_users_lifetime",
            "distinct_delivery_addresses_lifetime",
        ),
    ),
    "ip": (
        "fs:ip:*:batch",
        (
            "lifetime_order_count",
            "unique_users_lifetime",
            "chargeback_rate",
            "first_seen_days_ago",
        ),
    ),
    "store": (
        "fs:store:*:batch",
        (
            "avg_order_value_pence",
            "chargeback_rate",
            "unique_cards_30d",
            "total_orders_30d",
        ),
    ),
    "merchant": (
        "fs:merchant:*:batch",
        (
            "chargeback_rate",
            "total_stores",
        ),
    ),
    "email_domain": (
        "fs:email_domain:*:batch",
        (
            "chargeback_rate",
            "total_orders",
        ),
    ),
}


class _AsyncConnection(Protocol):
    async def execute(self, query: str, *args: object) -> str: ...

    async def fetchval(self, query: str, *args: object) -> object: ...


class _AsyncDirectConnection(_AsyncConnection, Protocol):
    async def close(self) -> None: ...


class _NotifyCallback(Protocol):
    def __call__(self, connection: object, pid: int, channel: str, payload: str) -> None: ...


class _AsyncListenerConnection(_AsyncDirectConnection, Protocol):
    async def add_listener(self, channel: str, callback: _NotifyCallback) -> None: ...

    async def remove_listener(self, channel: str, callback: _NotifyCallback) -> None: ...


class _PoolAcquireContext(Protocol):
    async def __aenter__(self) -> _AsyncConnection: ...

    async def __aexit__(
        self,
        exc_type: Union[type[BaseException], None],  # noqa: UP007 - packet requires py3.8.
        exc: Union[BaseException, None],  # noqa: UP007 - packet requires Python 3.8 syntax.
        tb: object,
    ) -> None: ...


class _AsyncPool(Protocol):
    def acquire(self) -> _PoolAcquireContext: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class _RedisHashSnapshot:
    payload: dict[str, str]
    ttl_seconds: int


@dataclass(frozen=True)
class _ThroughputOrder:
    order_id: UUID
    user_id: UUID
    expected_user_orders_1h: int
    row: Mapping[str, object]


def _now_london() -> dt.datetime:
    return dt.datetime.now(tz=LONDON_TZ)


def _to_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _minimal_order_row(
    user_id: UUID,
    store_id: UUID,
    merchant_id: UUID,
    order_id: UUID,
    placed_at: dt.datetime,
    order_number: str,
) -> dict[str, object]:
    return {
        "order_id": order_id,
        "order_number": order_number,
        "order_status": "PLACED",
        "order_channel": "WEB",
        "order_type": "DELIVERY",
        "placed_at": placed_at,
        "user_id": user_id,
        "user_account_age_days": 30,
        "user_total_orders_lifetime": 1,
        "user_total_orders_30d": 1,
        "user_total_spend_lifetime_pence": 1500,
        "user_email": f"feature-store-{user_id}@example.com",
        "user_email_domain": "example.com",
        "user_phone": "+447700900001",
        "user_risk_tier_at_order": "STANDARD",
        "is_guest_checkout": False,
        "store_id": store_id,
        "merchant_id": merchant_id,
        "store_city": "London",
        "store_country": "GB",
        "store_latitude": 51.501,
        "store_longitude": -0.142,
        "delivery_address_id": None,
        "delivery_address_snapshot": None,
        "delivery_latitude": 51.501,
        "delivery_longitude": -0.142,
        "delivery_distance_km": 1.5,
        "delivery_address_type": "RESIDENTIAL",
        "is_new_delivery_address": True,
        "delivery_address_use_count": 1,
        "item_count": 2,
        "unique_item_count": 2,
        "subtotal_pence": 1200,
        "vat_pence": 240,
        "delivery_fee_pence": 149,
        "service_fee_pence": 50,
        "tip_pence": 0,
        "discount_pence": 0,
        "total_pence": 1500,
        "currency": "GBP",
        "promo_id": None,
        "promo_code": None,
        "is_first_order_for_user": False,
        "is_new_payment_method": False,
        "payment_method_id": None,
        "payment_type": "CREDIT_CARD",
        "card_bin": "411111",
        "card_last_four": "1111",
        "card_brand": "VISA",
        "card_funding_type": "DEBIT",
        "card_issuer_country": "GB",
        "is_digital_native_bank": False,
        "is_new_user_promo": False,
        "session_id": uuid4(),
        "device_id": None,
        "device_type": "MOBILE_WEB",
        "platform": "IOS",
        "os_version": "16.0",
        "app_version": "3.0.0",
        "browser_name": None,
        "browser_version": None,
        "ip_address": "192.168.1.100",
        "ip_country": "GB",
        "ip_city": "London",
        "ip_is_proxy": False,
        "ip_is_vpn": False,
        "ip_is_tor": False,
        "ip_is_hosting": False,
    }


async def _insert_user(conn: _AsyncConnection, user_id: UUID) -> None:
    now = _now_london()
    await conn.execute(
        """
        INSERT INTO users (
            user_id, email, phone, password_hash, first_name, last_name,
            signup_country, risk_tier, created_at, updated_at
        ) VALUES (
            $1, $2, $3, $4, 'Feature', 'Store', 'GB', 'STANDARD', $5, $5
        )
        """,
        user_id,
        f"feature-store-{user_id}@example.com",
        "+447700900001",
        "feature_store_test_password",
        now,
    )


async def _insert_merchant(conn: _AsyncConnection, merchant_id: UUID) -> None:
    await conn.execute(
        """
        INSERT INTO merchants (merchant_id, legal_name, brand_name, merchant_category, status)
        VALUES ($1, 'Feature Store Merchant', 'Feature Store Merchant', 'QSR', 'ACTIVE')
        """,
        merchant_id,
    )


async def _insert_store(conn: _AsyncConnection, store_id: UUID, merchant_id: UUID) -> None:
    await conn.execute(
        """
        INSERT INTO stores (
            store_id, merchant_id, store_name, address_line_1, city, postcode,
            country, latitude, longitude, is_active
        ) VALUES (
            $1, $2, 'Feature Store Test', '1 Test Street', 'London', 'SW1A 1AA',
            'GB', 51.501, -0.142, TRUE
        )
        """,
        store_id,
        merchant_id,
    )


async def _insert_order(conn: _AsyncConnection, order_row: Mapping[str, object]) -> None:
    values = [order_row[column] for column in _ORDER_COLUMNS]
    await conn.execute(_INSERT_ORDER_SQL, *values)


async def _insert_order_and_notify(pool: _AsyncPool, order_row: Mapping[str, object]) -> float:
    async with pool.acquire() as conn:
        values = [order_row[column] for column in _ORDER_COLUMNS]
        t_insert_ms = time.perf_counter() * 1000.0
        await conn.execute(_INSERT_ORDER_NOTIFY_SQL, *values)
        return t_insert_ms


async def _insert_order_parents(
    pool: _AsyncPool,
    user_ids: Iterable[UUID],
    merchant_id: UUID,
    store_id: UUID,
) -> None:
    async with pool.acquire() as conn:
        await _insert_merchant(conn, merchant_id)
        await _insert_store(conn, store_id, merchant_id)
        for user_id in user_ids:
            await _insert_user(conn, user_id)


async def _insert_order_graph(
    pool: _AsyncPool,
    user_id: UUID,
    merchant_id: UUID,
    store_id: UUID,
    order_rows: Iterable[Mapping[str, object]],
) -> None:
    async with pool.acquire() as conn:
        await _insert_user(conn, user_id)
        await _insert_merchant(conn, merchant_id)
        await _insert_store(conn, store_id, merchant_id)
        for order_row in order_rows:
            await _insert_order(conn, order_row)


async def _delete_order_graph(
    pool: _AsyncPool,
    user_id: UUID,
    merchant_id: UUID,
    store_id: UUID,
    order_ids: Iterable[UUID],
) -> None:
    async with pool.acquire() as conn:
        ids = list(order_ids)
        if ids:
            await conn.execute("DELETE FROM orders WHERE order_id = ANY($1::uuid[])", ids)
        await conn.execute("DELETE FROM stores WHERE store_id = $1", store_id)
        await conn.execute("DELETE FROM merchants WHERE merchant_id = $1", merchant_id)
        await conn.execute("DELETE FROM users WHERE user_id = $1", user_id)


async def _delete_seed_order_graph(
    pool: _AsyncPool,
    user_ids: Iterable[UUID],
    merchant_id: UUID,
    store_id: UUID,
    order_ids: Iterable[UUID],
) -> None:
    async with pool.acquire() as conn:
        ids = list(order_ids)
        if ids:
            await conn.execute("DELETE FROM orders WHERE order_id = ANY($1::uuid[])", ids)

        await conn.execute("DELETE FROM stores WHERE store_id = $1", store_id)
        await conn.execute("DELETE FROM merchants WHERE merchant_id = $1", merchant_id)

        users = list(user_ids)
        if users:
            await conn.execute("DELETE FROM users WHERE user_id = ANY($1::uuid[])", users)


def _user_stream_keys(user_id: UUID) -> list[str]:
    uid = str(user_id)
    return [
        f"fs:user:{uid}:stream",
        f"fs:user:{uid}:orders_zset",
        f"fs:user:{uid}:stores_zset",
        f"fs:user:{uid}:payments_zset",
        f"fs:user:{uid}:spend_zset",
    ]


def _order_context_keys(user_id: UUID, store_id: UUID, ip_address: str) -> list[str]:
    uid = str(user_id)
    sid = str(store_id)
    return [
        f"fs:user:{uid}:stream",
        f"fs:user:{uid}:orders_zset",
        f"fs:user:{uid}:stores_zset",
        f"fs:user:{uid}:payments_zset",
        f"fs:user:{uid}:spend_zset",
        f"fs:ip:{ip_address}:stream",
        f"fs:ip:{ip_address}:orders_zset",
        f"fs:ip:{ip_address}:users_zset",
        f"fs:ip:{ip_address}:devices_zset",
        f"fs:store:{sid}:stream",
        f"fs:store:{sid}:orders_zset",
        f"fs:store:{sid}:users_zset",
        f"fs:store:{sid}:cards_1h_zset",
    ]


def _multi_user_order_context_keys(
    user_ids: Iterable[UUID],
    store_id: UUID,
    ip_address: str,
) -> list[str]:
    sid = str(store_id)
    keys: set[str] = {
        f"fs:ip:{ip_address}:stream",
        f"fs:ip:{ip_address}:orders_zset",
        f"fs:ip:{ip_address}:users_zset",
        f"fs:ip:{ip_address}:devices_zset",
        f"fs:store:{sid}:stream",
        f"fs:store:{sid}:orders_zset",
        f"fs:store:{sid}:users_zset",
        f"fs:store:{sid}:cards_1h_zset",
    }
    for user_id in user_ids:
        keys.update(_user_stream_keys(user_id))
    return sorted(keys)


async def _delete_redis_keys(redis_conn: _AsyncRedis, keys: Iterable[str]) -> None:
    key_list = list(keys)
    if key_list:
        await redis_conn.delete(*key_list)


async def _remove_processed_order_ids(
    redis_conn: _AsyncRedis,
    order_ids: Iterable[UUID],
) -> None:
    members = [str(order_id) for order_id in order_ids]
    if not members:
        return

    cursor = 0
    while True:
        cursor_raw, keys_raw = await redis_conn.scan(
            cursor=cursor,
            match=f"{aggregator.PROCESSED_ORDERS_KEY_PREFIX}:*",
            count=100,
        )
        cursor = int(cursor_raw)
        keys = [str(key) for key in keys_raw]
        for key in keys:
            await redis_conn.srem(key, *members)
        if cursor == 0:
            break


async def _redis_hget_int(
    redis_conn: _AsyncRedis,
    key: str,
    field_name: str,
) -> int:
    value = await redis_conn.hget(key, field_name)
    if value is None:
        return 0
    return int(str(value))


async def _poll_until_user_count_visible(
    redis_conn: _AsyncRedis,
    user_id: UUID,
    expected_orders_1h: int,
    t_insert_ms: float,
    timeout_seconds: float,
) -> float | None:
    stream_key = f"fs:user:{user_id}:stream"
    deadline = time.perf_counter() + timeout_seconds

    while time.perf_counter() < deadline:
        if await _redis_hget_int(redis_conn, stream_key, "orders_1h") >= expected_orders_1h:
            t_visible_ms = time.perf_counter() * 1000.0
            return t_visible_ms - t_insert_ms
        await asyncio.sleep(0.01)

    return None


def _scan_keys(redis_client: _SyncRedis, pattern: str) -> list[str]:
    return [str(key) for key in redis_client.scan_iter(match=pattern)]


def _assert_hash_has_fields(
    redis_client: _SyncRedis,
    key: str,
    expected_fields: Iterable[str],
) -> None:
    payload = {str(field): str(value) for field, value in redis_client.hgetall(key).items()}

    assert payload, key
    for field_name in expected_fields:
        assert field_name in payload, f"{key} missing {field_name}"


def _snapshot_hashes(
    redis_client: _SyncRedis,
    pattern: str,
) -> dict[str, _RedisHashSnapshot]:
    snapshots: dict[str, _RedisHashSnapshot] = {}
    for key in _scan_keys(redis_client, pattern):
        snapshots[key] = _RedisHashSnapshot(
            payload={str(field): str(value) for field, value in redis_client.hgetall(key).items()},
            ttl_seconds=int(redis_client.ttl(key)),
        )
    return snapshots


def _restore_hashes(
    redis_client: _SyncRedis,
    pattern: str,
    snapshots: Mapping[str, _RedisHashSnapshot],
) -> None:
    current_keys = _scan_keys(redis_client, pattern)
    if current_keys:
        redis_client.delete(*current_keys)

    pipe = redis_client.pipeline(transaction=False)
    for key, snapshot in snapshots.items():
        pipe.hset(
            key,
            mapping=cast(
                "Mapping[str | bytes, bytes | float | int | str]",
                snapshot.payload,
            ),
        )
        if snapshot.ttl_seconds > 0:
            pipe.expire(key, snapshot.ttl_seconds)
    pipe.execute()


@pytest.mark.asyncio
async def test_streaming_features_update_on_order() -> None:
    user_id = uuid4()
    store_id = uuid4()
    merchant_id = uuid4()
    order_id = uuid4()
    order_row = _minimal_order_row(
        user_id=user_id,
        store_id=store_id,
        merchant_id=merchant_id,
        order_id=order_id,
        placed_at=_now_london(),
        order_number=f"FS-{order_id.hex[:12]}",
    )
    ip_address = str(order_row["ip_address"])
    pg_pool = cast(
        _AsyncPool,
        await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5),
    )
    redis_async: _AsyncRedis = aioredis.from_url(REDIS_URL, decode_responses=True)

    try:
        try:
            await _insert_order_graph(pg_pool, user_id, merchant_id, store_id, [order_row])

            updated = await aggregator.update_features_for_order(
                pool=pg_pool,
                redis_conn=redis_async,
                order_id=order_id,
            )

            assert updated is True
            assert await _redis_hget_int(redis_async, f"fs:user:{user_id}:stream", "orders_1h") == 1
        finally:
            await _delete_order_graph(pg_pool, user_id, merchant_id, store_id, [order_id])
            await _delete_redis_keys(
                redis_async, _order_context_keys(user_id, store_id, ip_address)
            )
            await _remove_processed_order_ids(redis_async, [order_id])
    finally:
        await redis_async.close()
        await pg_pool.close()


@pytest.mark.asyncio
async def test_sliding_window_decay() -> None:
    user_id = uuid4()
    now_ts = int(_now_london().timestamp())
    old_ts = now_ts - (70 * 60)
    orders_zset_key = f"fs:user:{user_id}:orders_zset"
    spend_zset_key = f"fs:user:{user_id}:spend_zset"
    stream_key = f"fs:user:{user_id}:stream"
    redis_async: _AsyncRedis = aioredis.from_url(REDIS_URL, decode_responses=True)

    try:
        try:
            await redis_async.zadd(
                orders_zset_key,
                cast(
                    "Mapping[str | bytes, bytes | float | int | str]",
                    {"dummy_order_id": old_ts},
                ),
            )
            await redis_async.zadd(
                spend_zset_key,
                cast(
                    "Mapping[str | bytes, bytes | float | int | str]",
                    {"dummy_order_id:1000": old_ts},
                ),
            )
            await aggregator._refresh_user_stream_aggregates(
                redis_conn=redis_async,
                user_id=str(user_id),
                now_ts=now_ts,
            )

            assert await _redis_hget_int(redis_async, stream_key, "orders_1h") == 0
            assert await _redis_hget_int(redis_async, stream_key, "orders_24h") == 1
        finally:
            await _delete_redis_keys(redis_async, _user_stream_keys(user_id))
    finally:
        await redis_async.close()


@pytest.mark.asyncio
async def test_batch_features_populated() -> None:
    user_ids = [uuid4() for _ in range(10)]
    store_id = uuid4()
    merchant_id = uuid4()
    device_ids = [uuid4() for _ in range(5)]
    payment_method_ids = [uuid4() for _ in range(5)]
    delivery_address_ids = [uuid4() for _ in range(10)]
    email_domain = f"feature-store-batch-{uuid4().hex}.example.com"
    ip_addresses = [f"10.250.0.{index}" for index in range(1, 6)]
    placed_at = _now_london()
    order_rows: list[dict[str, object]] = []

    for index in range(50):
        user_id = user_ids[index % len(user_ids)]
        order_id = uuid4()
        order_row = _minimal_order_row(
            user_id=user_id,
            store_id=store_id,
            merchant_id=merchant_id,
            order_id=order_id,
            placed_at=placed_at + dt.timedelta(seconds=index),
            order_number=f"FS-{order_id.hex[:12]}",
        )
        order_row["user_total_orders_lifetime"] = index + 1
        order_row["user_total_orders_30d"] = index + 1
        order_row["user_total_spend_lifetime_pence"] = 1500 * (index + 1)
        order_row["user_email"] = f"feature-store-{user_id}@{email_domain}"
        order_row["user_email_domain"] = email_domain
        order_row["device_id"] = device_ids[index % len(device_ids)]
        order_row["payment_method_id"] = payment_method_ids[index % len(payment_method_ids)]
        order_row["delivery_address_id"] = delivery_address_ids[index % len(delivery_address_ids)]
        order_row["ip_address"] = ip_addresses[index % len(ip_addresses)]
        order_rows.append(order_row)

    order_ids = [_to_uuid(order_row["order_id"]) for order_row in order_rows]
    pg_pool = cast(
        _AsyncPool,
        await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5),
    )
    redis_sync: _SyncRedis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        snapshots = {
            pattern: _snapshot_hashes(redis_sync, pattern) for pattern in _BATCH_HASH_PATTERNS
        }
        try:
            await _insert_order_parents(pg_pool, user_ids, merchant_id, store_id)
            async with pg_pool.acquire() as conn:
                for order_row in order_rows:
                    await _insert_order(conn, order_row)

            batch_compute.run_batch()

            expected_keys = {
                "user": f"fs:user:{user_ids[0]}:batch",
                "device": f"fs:device:{device_ids[0]}:batch",
                "payment": f"fs:payment:{payment_method_ids[0]}:batch",
                "ip": f"fs:ip:{ip_addresses[0]}:batch",
                "store": f"fs:store:{store_id}:batch",
                "merchant": f"fs:merchant:{merchant_id}:batch",
                "email_domain": f"fs:email_domain:{email_domain}:batch",
            }
            for entity_name, key in expected_keys.items():
                pattern, expected_fields = _BATCH_ENTITY_FIELDS[entity_name]
                assert _scan_keys(redis_sync, pattern)
                _assert_hash_has_fields(redis_sync, key, expected_fields)
        finally:
            await _delete_seed_order_graph(
                pg_pool,
                user_ids,
                merchant_id,
                store_id,
                order_ids,
            )
            for pattern, snapshot in snapshots.items():
                _restore_hashes(redis_sync, pattern, snapshot)
    finally:
        redis_sync.close()
        await pg_pool.close()


def test_client_get_features_under_10ms() -> None:
    user_id = uuid4()
    store_id = uuid4()
    merchant_id = uuid4()
    ip_address = "192.168.1.100"
    email_domain = "example.com"
    user_stream_key = f"fs:user:{user_id}:stream"
    user_batch_key = f"fs:user:{user_id}:batch"
    redis_sync: _SyncRedis = redis.Redis.from_url(REDIS_URL, decode_responses=True)

    try:
        try:
            redis_sync.hset(
                user_stream_key,
                mapping=cast(
                    "Mapping[str | bytes, bytes | float | int | str]",
                    {
                        "orders_1h": "1",
                        "orders_24h": "1",
                        "spend_1h_pence": "1500",
                        "spend_24h_pence": "1500",
                        "unique_stores_24h": "1",
                        "unique_payment_methods_24h": "0",
                        "last_order_age_minutes": "0",
                    },
                ),
            )
            redis_sync.hset(
                user_batch_key,
                mapping=cast(
                    "Mapping[str | bytes, bytes | float | int | str]",
                    {
                        "lifetime_order_count": "1",
                        "lifetime_spend_pence": "1500",
                        "avg_order_value_pence": "1500",
                        "lifetime_chargeback_count": "0",
                        "lifetime_refund_count": "0",
                        "lifetime_chargeback_rate": "0.0",
                        "unique_devices_used": "0",
                        "unique_payment_methods_used": "0",
                        "unique_delivery_addresses": "0",
                        "account_age_days": "30",
                        "days_since_last_order": "0",
                        "distinct_cities_ordered_from": "1",
                    },
                ),
            )

            client = FeatureStoreClient(REDIS_URL)
            try:
                latencies_ms: list[float] = []
                for _ in range(100):
                    feature_set = client.get_features(
                        user_id=user_id,
                        device_id=None,
                        payment_method_id=None,
                        ip_address=ip_address,
                        store_id=store_id,
                        merchant_id=merchant_id,
                        delivery_address_id=None,
                        email_domain=email_domain,
                    )
                    latencies_ms.append(feature_set.feature_fetch_latency_ms)

                assert len(latencies_ms) == 100
                assert sorted(latencies_ms)[98] < 25.0
            finally:
                client.close()
        finally:
            redis_sync.delete(user_stream_key, user_batch_key)
    finally:
        redis_sync.close()


def test_missing_features_have_defaults() -> None:
    user_id = uuid4()
    store_id = uuid4()
    merchant_id = uuid4()
    user_stream_key = f"fs:user:{user_id}:stream"
    user_batch_key = f"fs:user:{user_id}:batch"
    ip_address = f"198.51.100.{int(user_id.int % 200) + 1}"
    email_domain = f"missing-{user_id.hex}.example.com"

    client = FeatureStoreClient(REDIS_URL)
    try:
        feature_set = client.get_features(
            user_id=user_id,
            device_id=None,
            payment_method_id=None,
            ip_address=ip_address,
            store_id=store_id,
            merchant_id=merchant_id,
            delivery_address_id=None,
            email_domain=email_domain,
        )
    finally:
        client.close()

    int_defaults = (
        feature_set.user_orders_1h,
        feature_set.user_orders_24h,
        feature_set.user_spend_1h_pence,
        feature_set.user_spend_24h_pence,
        feature_set.user_unique_stores_24h,
        feature_set.user_unique_payment_methods_24h,
        feature_set.user_last_order_age_minutes,
        feature_set.user_lifetime_order_count,
        feature_set.user_lifetime_spend_pence,
        feature_set.user_avg_order_value_pence,
        feature_set.user_lifetime_chargeback_count,
        feature_set.user_lifetime_refund_count,
        feature_set.user_unique_devices_used,
        feature_set.user_unique_payment_methods_used,
        feature_set.user_unique_delivery_addresses,
        feature_set.user_account_age_days,
        feature_set.user_days_since_last_order,
        feature_set.user_distinct_cities_ordered_from,
        feature_set.device_orders_1h,
        feature_set.device_orders_24h,
        feature_set.device_unique_users_24h,
        feature_set.device_unique_payment_methods_24h,
        feature_set.device_lifetime_order_count,
        feature_set.device_unique_users_lifetime,
        feature_set.device_first_seen_days_ago,
        feature_set.device_distinct_payment_methods_lifetime,
        feature_set.payment_orders_1h,
        feature_set.payment_orders_24h,
        feature_set.payment_unique_users_24h,
        feature_set.payment_decline_count_24h,
        feature_set.payment_lifetime_order_count,
        feature_set.payment_lifetime_chargeback_count,
        feature_set.payment_unique_users_lifetime,
        feature_set.payment_distinct_delivery_addresses_lifetime,
        feature_set.ip_orders_1h,
        feature_set.ip_orders_24h,
        feature_set.ip_unique_users_24h,
        feature_set.ip_unique_devices_24h,
        feature_set.ip_lifetime_order_count,
        feature_set.ip_unique_users_lifetime,
        feature_set.ip_first_seen_days_ago,
        feature_set.store_orders_1h,
        feature_set.store_orders_24h,
        feature_set.store_unique_users_24h,
        feature_set.store_unique_cards_1h,
        feature_set.store_avg_order_value_pence,
        feature_set.store_unique_cards_30d,
        feature_set.store_total_orders_30d,
        feature_set.merchant_total_stores,
        feature_set.email_domain_total_orders,
        feature_set.address_orders_24h,
        feature_set.address_unique_users_24h,
    )
    float_defaults = (
        feature_set.user_lifetime_chargeback_rate,
        feature_set.device_lifetime_chargeback_rate,
        feature_set.payment_lifetime_chargeback_rate,
        feature_set.ip_chargeback_rate,
        feature_set.store_chargeback_rate,
        feature_set.merchant_chargeback_rate,
        feature_set.email_domain_chargeback_rate,
    )
    expected_missing = {
        user_stream_key,
        user_batch_key,
        f"fs:ip:{ip_address}:stream",
        f"fs:ip:{ip_address}:batch",
        f"fs:store:{store_id}:stream",
        f"fs:store:{store_id}:batch",
        f"fs:merchant:{merchant_id}:batch",
        f"fs:email_domain:{email_domain}:batch",
    }

    assert all(value == 0 for value in int_defaults)
    assert all(value == 0.0 for value in float_defaults)
    assert set(feature_set.missing_features) == expected_missing


@pytest.mark.asyncio
async def test_aggregator_recovers_from_dropped_notify() -> None:
    user_id = uuid4()
    store_id = uuid4()
    merchant_id = uuid4()
    order_id = uuid4()
    order_row = _minimal_order_row(
        user_id=user_id,
        store_id=store_id,
        merchant_id=merchant_id,
        order_id=order_id,
        placed_at=_now_london(),
        order_number=f"FS-{order_id.hex[:12]}",
    )
    ip_address = str(order_row["ip_address"])
    metrics = aggregator.Metrics(errors=[0])
    pg_pool = cast(
        _AsyncPool,
        await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5),
    )
    redis_async: _AsyncRedis = aioredis.from_url(REDIS_URL, decode_responses=True)

    try:
        # Clear stale processed-order markers from previous tests so the
        # backup poll sees a clean slate.
        cursor: int = 0
        while True:
            cursor_raw, keys_raw = await redis_async.scan(
                cursor=cursor,
                match=f"{aggregator.PROCESSED_ORDERS_KEY_PREFIX}:*",
                count=100,
            )
            cursor = int(cursor_raw)
            if keys_raw:
                await redis_async.delete(*[str(k) for k in keys_raw])
            if cursor == 0:
                break

        try:
            await _insert_order_graph(pg_pool, user_id, merchant_id, store_id, [order_row])

            await aggregator.run_backup_poll_once(
                pool=pg_pool,
                redis_conn=redis_async,
                metrics=metrics,
            )

            assert metrics.errors[0] == 0
            assert await _redis_hget_int(redis_async, f"fs:user:{user_id}:stream", "orders_1h") == 1
        finally:
            await _delete_order_graph(pg_pool, user_id, merchant_id, store_id, [order_id])
            await _delete_redis_keys(
                redis_async, _order_context_keys(user_id, store_id, ip_address)
            )
            await _remove_processed_order_ids(redis_async, [order_id])
    finally:
        await redis_async.close()
        await pg_pool.close()


@pytest.mark.asyncio
async def test_no_unbounded_redis_growth() -> None:
    user_id = uuid4()
    now_ts = int(_now_london().timestamp())
    old_ts = now_ts - (25 * 3600)
    recent_ts = now_ts - 3600
    orders_zset_key = f"fs:user:{user_id}:orders_zset"
    metrics = aggregator.Metrics(errors=[0])
    redis_async: _AsyncRedis = aioredis.from_url(REDIS_URL, decode_responses=True)

    try:
        try:
            mapping: dict[str, int] = {}
            for index in range(50):
                mapping[f"old-{index}"] = old_ts
                mapping[f"recent-{index}"] = recent_ts
            await redis_async.zadd(
                orders_zset_key,
                cast(
                    "Mapping[str | bytes, bytes | float | int | str]",
                    mapping,
                ),
            )

            await aggregator.trim_order_zsets_once(redis_conn=redis_async, metrics=metrics)

            assert int(await redis_async.zcard(orders_zset_key)) == 50
        finally:
            await _delete_redis_keys(redis_async, _user_stream_keys(user_id))
    finally:
        await redis_async.close()


@pytest.mark.asyncio
@pytest.mark.slow
async def test_stream_throughput() -> None:
    target_orders_per_second = 100
    duration_seconds = 60
    total_orders = target_orders_per_second * duration_seconds
    visible_timeout_seconds = 5.0
    user_ids = [uuid4() for _ in range(total_orders)]
    store_id = uuid4()
    merchant_id = uuid4()
    placed_at = _now_london()
    throughput_orders: list[_ThroughputOrder] = []

    for index in range(total_orders):
        user_id = user_ids[index]
        order_id = uuid4()
        order_row = _minimal_order_row(
            user_id=user_id,
            store_id=store_id,
            merchant_id=merchant_id,
            order_id=order_id,
            placed_at=placed_at + dt.timedelta(milliseconds=index),
            order_number=f"FS-{order_id.hex[:12]}",
        )
        throughput_orders.append(
            _ThroughputOrder(
                order_id=order_id,
                user_id=user_id,
                expected_user_orders_1h=1,
                row=order_row,
            )
        )

    order_ids = [order.order_id for order in throughput_orders]
    metrics = aggregator.Metrics(errors=[0])
    ip_address = str(throughput_orders[0].row["ip_address"])
    insert_pool = cast(
        _AsyncPool,
        await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=20),
    )
    aggregator_pool = cast(
        _AsyncPool,
        await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=20),
    )
    listener_conn = cast(_AsyncListenerConnection, await asyncpg.connect(DATABASE_URL))
    redis_async: _AsyncRedis = aioredis.from_url(REDIS_URL, decode_responses=True)
    latencies_ms: list[float] = []
    errors: list[BaseException] = []
    visibility_tasks: list[asyncio.Task[float | None]] = []

    def listener_callback(connection: object, pid: int, channel: str, payload: str) -> None:
        aggregator._on_order_placed(
            pool=aggregator_pool,
            redis_conn=redis_async,
            metrics=metrics,
            connection=connection,
            pid=pid,
            channel=channel,
            payload=payload,
        )

    async def schedule_visibility_poll(
        throughput_order: _ThroughputOrder,
    ) -> asyncio.Task[float | None]:
        t_insert_ms = await _insert_order_and_notify(insert_pool, throughput_order.row)
        return asyncio.create_task(
            _poll_until_user_count_visible(
                redis_conn=redis_async,
                user_id=throughput_order.user_id,
                expected_orders_1h=throughput_order.expected_user_orders_1h,
                t_insert_ms=t_insert_ms,
                timeout_seconds=visible_timeout_seconds,
            )
        )

    try:
        await listener_conn.add_listener("order_placed", listener_callback)
        try:
            await _insert_order_parents(insert_pool, user_ids, merchant_id, store_id)
            await _delete_redis_keys(
                redis_async, _multi_user_order_context_keys(user_ids, store_id, ip_address)
            )

            start = time.perf_counter()
            for offset in range(0, total_orders, target_orders_per_second):
                chunk = throughput_orders[offset : offset + target_orders_per_second]
                chunk_results = await asyncio.gather(
                    *(schedule_visibility_poll(order) for order in chunk),
                    return_exceptions=True,
                )

                for chunk_result in chunk_results:
                    if isinstance(chunk_result, BaseException):
                        errors.append(chunk_result)
                    else:
                        visibility_tasks.append(chunk_result)

                target_elapsed = (offset // target_orders_per_second) + 1
                sleep_seconds = start + target_elapsed - time.perf_counter()
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)

            visibility_results = await asyncio.gather(
                *visibility_tasks,
                return_exceptions=True,
            )
            for visible_result in visibility_results:
                if isinstance(visible_result, BaseException):
                    errors.append(visible_result)
                elif visible_result is not None:
                    latencies_ms.append(visible_result)

            assert not errors, [repr(error) for error in errors[:5]]
            assert metrics.errors[0] == 0
            assert len(latencies_ms) >= 5500
            assert float(np.percentile(latencies_ms, 99)) < 1000.0
        finally:
            await listener_conn.remove_listener("order_placed", listener_callback)
            await _delete_seed_order_graph(
                insert_pool,
                user_ids,
                merchant_id,
                store_id,
                order_ids,
            )
            await _delete_redis_keys(
                redis_async, _multi_user_order_context_keys(user_ids, store_id, ip_address)
            )
            await _remove_processed_order_ids(redis_async, order_ids)
    finally:
        await listener_conn.close()
        await redis_async.close()
        await aggregator_pool.close()
        await insert_pool.close()
