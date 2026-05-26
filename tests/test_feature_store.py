from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Union, cast
from uuid import UUID, uuid4

import pytest

from shared.db import get_engine

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
        feature_fetch_latency_ms: float
        missing_features: list[str]
        user_lifetime_order_count: int
        user_orders_1h: int
        user_orders_24h: int

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


class _AsyncConnection(Protocol):
    async def execute(self, query: str, *args: object) -> str: ...

    async def fetchval(self, query: str, *args: object) -> object: ...


class _AsyncDirectConnection(_AsyncConnection, Protocol):
    async def close(self) -> None: ...


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


def _scan_keys(redis_client: _SyncRedis, pattern: str) -> list[str]:
    return [str(key) for key in redis_client.scan_iter(match=pattern)]


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
    pg_conn = cast(_AsyncDirectConnection, await asyncpg.connect(DATABASE_URL))
    redis_sync: _SyncRedis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        snapshot = _snapshot_hashes(redis_sync, "fs:user:*:batch")
        try:
            user_id_raw = await pg_conn.fetchval("SELECT user_id FROM users LIMIT 1")
            if user_id_raw is None:
                assert True
                return

            _to_uuid(user_id_raw)
            engine = get_engine("app")
            batch_compute.compute_user_batch_features(engine, redis_sync)

            batch_keys = _scan_keys(redis_sync, "fs:user:*:batch")
            assert any("lifetime_order_count" in redis_sync.hgetall(key) for key in batch_keys)
        finally:
            _restore_hashes(redis_sync, "fs:user:*:batch", snapshot)
    finally:
        await pg_conn.close()
        redis_sync.close()


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

    client = FeatureStoreClient(REDIS_URL)
    try:
        feature_set = client.get_features(
            user_id=user_id,
            device_id=None,
            payment_method_id=None,
            ip_address="192.168.1.100",
            store_id=store_id,
            merchant_id=merchant_id,
            delivery_address_id=None,
            email_domain="example.com",
        )
    finally:
        client.close()

    assert feature_set.user_orders_1h == 0
    assert feature_set.user_orders_24h == 0
    assert feature_set.user_lifetime_order_count == 0
    assert len(feature_set.missing_features) > 0
    assert user_stream_key in feature_set.missing_features


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
async def test_stream_throughput() -> None:
    user_id = uuid4()
    store_id = uuid4()
    merchant_id = uuid4()
    placed_at = _now_london()
    order_ids = [uuid4() for _ in range(10)]
    order_rows = [
        _minimal_order_row(
            user_id=user_id,
            store_id=store_id,
            merchant_id=merchant_id,
            order_id=order_id,
            placed_at=placed_at,
            order_number=f"FS-{order_id.hex[:12]}",
        )
        for order_id in order_ids
    ]
    metrics = aggregator.Metrics(errors=[0])
    ip_address = str(order_rows[0]["ip_address"])
    pg_pool = cast(
        _AsyncPool,
        await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5),
    )
    redis_async: _AsyncRedis = aioredis.from_url(REDIS_URL, decode_responses=True)

    try:
        try:
            await _insert_order_graph(pg_pool, user_id, merchant_id, store_id, order_rows)
            for order_id in order_ids:
                updated = await aggregator.update_features_for_order(
                    pool=pg_pool,
                    redis_conn=redis_async,
                    order_id=order_id,
                )
                assert updated is True

            assert metrics.errors[0] == 0
            assert (
                await _redis_hget_int(redis_async, f"fs:user:{user_id}:stream", "orders_1h") == 10
            )
        finally:
            await _delete_order_graph(pg_pool, user_id, merchant_id, store_id, order_ids)
            await _delete_redis_keys(
                redis_async, _order_context_keys(user_id, store_id, ip_address)
            )
            await _remove_processed_order_ids(redis_async, order_ids)
    finally:
        await redis_async.close()
        await pg_pool.close()
