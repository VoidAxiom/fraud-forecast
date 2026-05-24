"""Tests for the extended FeatureSet / get_features API (P4-D).

Runs inside the compose stack against the live Redis service.
Each test manages its own keys — no shared state.
"""
from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import redis

from feature_store.client import AsyncFeatureStoreClient, FeatureStoreClient

REDIS_URL = "redis://redis:6379/0"


def _always_queried_keys(
    user_id: str,
    ip_address: str,
    store_id: str,
    merchant_id: str,
    email_domain: str,
) -> list[str]:
    return [
        f"fs:user:{user_id}:stream",
        f"fs:user:{user_id}:batch",
        f"fs:ip:{ip_address}:stream",
        f"fs:ip:{ip_address}:batch",
        f"fs:store:{store_id}:stream",
        f"fs:store:{store_id}:batch",
        f"fs:merchant:{merchant_id}:batch",
        f"fs:email_domain:{email_domain}:batch",
    ]


@pytest.fixture(scope="function")
def redis_client() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def test_get_features_cold_start(redis_client: redis.Redis) -> None:
    user_id = uuid4()
    store_id = uuid4()
    merchant_id = uuid4()
    ip_address = "192.168.100.1"
    email_domain = "example.com"
    device_id = None
    payment_method_id = None
    delivery_address_id = None

    client = FeatureStoreClient(REDIS_URL)
    try:
        fs = client.get_features(
            user_id=user_id,
            device_id=device_id,
            payment_method_id=payment_method_id,
            ip_address=ip_address,
            store_id=store_id,
            merchant_id=merchant_id,
            delivery_address_id=delivery_address_id,
            email_domain=email_domain,
        )

        assert fs.user_orders_1h == 0
        assert fs.user_orders_24h == 0
        assert fs.user_lifetime_order_count is None
        assert fs.device_orders_1h == 0

        expected_missing = set(
            _always_queried_keys(
                str(user_id),
                ip_address,
                str(store_id),
                str(merchant_id),
                email_domain,
            )
        )
        assert len(fs.missing_features) == 8
        assert set(fs.missing_features) == expected_missing
        assert fs.feature_fetch_latency_ms > 0.0
    finally:
        client.close()


def test_get_features_with_populated_user(redis_client: redis.Redis) -> None:
    user_id = uuid4()
    store_id = uuid4()
    merchant_id = uuid4()
    ip_address = "192.168.100.2"
    email_domain = "example.com"
    user_stream_key = f"fs:user:{user_id}:stream"
    user_batch_key = f"fs:user:{user_id}:batch"

    redis_client.hset(
        user_stream_key,
        mapping={
            "orders_1h": "5",
            "orders_24h": "12",
            "spend_1h_pence": "3000",
            "spend_24h_pence": "8500",
        },
    )
    redis_client.hset(
        user_batch_key,
        mapping={
            "lifetime_order_count": "200",
            "lifetime_chargeback_rate": "0.02",
        },
    )

    client = FeatureStoreClient(REDIS_URL)
    try:
        fs = client.get_features(
            user_id=user_id,
            device_id=None,
            payment_method_id=None,
            ip_address=ip_address,
            store_id=store_id,
            merchant_id=merchant_id,
            delivery_address_id=None,
            email_domain=email_domain,
        )

        assert fs.user_orders_1h == 5
        assert fs.user_orders_24h == 12
        assert fs.user_spend_1h_pence == 3000
        assert fs.user_spend_24h_pence == 8500
        assert fs.user_lifetime_order_count == 200
        assert fs.user_lifetime_chargeback_rate is not None
        assert abs(fs.user_lifetime_chargeback_rate - 0.02) < 1e-9
        assert user_stream_key not in fs.missing_features
        assert user_batch_key not in fs.missing_features
    finally:
        client.close()
        redis_client.delete(user_stream_key, user_batch_key)


def test_get_features_with_partial_data(redis_client: redis.Redis) -> None:
    user_id = uuid4()
    store_id = uuid4()
    merchant_id = uuid4()
    ip_address = "192.168.100.3"
    email_domain = "example.com"
    user_stream_key = f"fs:user:{user_id}:stream"
    user_batch_key = f"fs:user:{user_id}:batch"

    redis_client.hset(
        user_stream_key,
        mapping={"orders_1h": "3"},
    )

    client = FeatureStoreClient(REDIS_URL)
    try:
        fs = client.get_features(
            user_id=user_id,
            device_id=None,
            payment_method_id=None,
            ip_address=ip_address,
            store_id=store_id,
            merchant_id=merchant_id,
            delivery_address_id=None,
            email_domain=email_domain,
        )

        assert fs.user_orders_1h == 3
        assert fs.user_lifetime_order_count is None
        assert fs.user_lifetime_chargeback_rate is None
        assert user_batch_key in fs.missing_features
        assert user_stream_key not in fs.missing_features
    finally:
        client.close()
        redis_client.delete(user_stream_key, user_batch_key)


def test_get_features_under_10ms_p99(redis_client: redis.Redis) -> None:
    user_id = uuid4()
    store_id = uuid4()
    merchant_id = uuid4()
    ip_address = "192.168.100.4"
    email_domain = "example.com"

    user_stream_key = f"fs:user:{user_id}:stream"
    user_batch_key = f"fs:user:{user_id}:batch"
    ip_stream_key = f"fs:ip:{ip_address}:stream"
    store_stream_key = f"fs:store:{store_id}:stream"

    redis_client.hset(
        user_stream_key,
        mapping={"orders_1h": "1"},
    )
    redis_client.hset(
        user_batch_key,
        mapping={"lifetime_order_count": "10"},
    )
    redis_client.hset(
        ip_stream_key,
        mapping={"orders_1h": "2"},
    )
    redis_client.hset(
        store_stream_key,
        mapping={"orders_1h": "4"},
    )

    client = FeatureStoreClient(REDIS_URL)
    try:
        latencies: list[float] = []
        for _ in range(100):
            fs = client.get_features(
                user_id=user_id,
                device_id=None,
                payment_method_id=None,
                ip_address=ip_address,
                store_id=store_id,
                merchant_id=merchant_id,
                delivery_address_id=None,
                email_domain=email_domain,
            )
            latencies.append(fs.feature_fetch_latency_ms)

        assert len(latencies) == 100
        sorted_latencies = sorted(latencies)
        p99 = sorted_latencies[98]
        assert p99 < 25.0, f"p99={p99:.2f}ms > 25ms"
    finally:
        client.close()
        redis_client.delete(
            user_stream_key,
            user_batch_key,
            ip_stream_key,
            store_stream_key,
        )


def test_async_get_features_round_trip(redis_client: redis.Redis) -> None:
    async def _inner() -> None:
        user_id = uuid4()
        store_id = uuid4()
        merchant_id = uuid4()
        ip_address = "192.168.100.5"
        email_domain = "example.com"
        user_stream_key = f"fs:user:{user_id}:stream"

        redis_client.hset(
            user_stream_key,
            mapping={"orders_1h": "7"},
        )

        client = AsyncFeatureStoreClient(REDIS_URL)
        try:
            fs = await client.get_features(
                user_id=user_id,
                device_id=None,
                payment_method_id=None,
                ip_address=ip_address,
                store_id=store_id,
                merchant_id=merchant_id,
                delivery_address_id=None,
                email_domain=email_domain,
            )
            assert fs.user_orders_1h == 7
            assert fs.feature_fetch_latency_ms > 0.0
        finally:
            await client.close()
            redis_client.delete(user_stream_key)

    asyncio.run(_inner())


def test_optional_entity_ids(redis_client: redis.Redis) -> None:
    user_id = uuid4()
    store_id = uuid4()
    merchant_id = uuid4()
    ip_address = "192.168.100.6"
    email_domain = "example.com"
    user_stream_key = f"fs:user:{user_id}:stream"

    client = FeatureStoreClient(REDIS_URL)
    try:
        fs = client.get_features(
            user_id=user_id,
            device_id=None,
            payment_method_id=None,
            ip_address=ip_address,
            store_id=store_id,
            merchant_id=merchant_id,
            delivery_address_id=None,
            email_domain=email_domain,
        )

        assert fs.device_orders_1h == 0
        assert fs.device_lifetime_order_count is None
        assert fs.payment_orders_1h == 0
        assert fs.payment_lifetime_order_count is None
        assert fs.address_orders_24h == 0

        assert not any(key.startswith("fs:device:") for key in fs.missing_features)
        assert not any(key.startswith("fs:payment:") for key in fs.missing_features)
        assert not any(key.startswith("fs:address:") for key in fs.missing_features)

        expected_missing = set(
            _always_queried_keys(
                str(user_id),
                ip_address,
                str(store_id),
                str(merchant_id),
                email_domain,
            )
        )
        assert len(fs.missing_features) == 8
        assert set(fs.missing_features) == expected_missing
    finally:
        client.close()
        redis_client.delete(user_stream_key)
