"""Base clients for the Redis feature store.

Provides a synchronous and asynchronous API for loading aggregate feature payloads
for a single order context.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from uuid import UUID

import redis
import redis.asyncio as aioredis
import redis.asyncio.client as aioredis_client

from feature_store.schema import FeatureSet


def _to_int(val: object, default: int = 0) -> int:
    """Convert a Redis value (str, None, or already int) to int."""
    if val is None:
        return default
    return int(str(val))


def _to_int_or_none(val: object) -> int | None:
    """Convert Redis optional numeric values, including blank values, to int."""
    if val is None or val == "":
        return None
    return int(str(val))


def _to_float_or_none(val: object) -> float | None:
    """Convert Redis optional numeric values, including blank values, to float."""
    if val is None or val == "":
        return None
    return float(str(val))


@dataclass(frozen=True)
class UserFeatures:
    """User feature payload used by scoring consumers (Phase 6).

    first_order_at_epoch and last_order_at_epoch are reserved for future
    extension; in P4 the batch schema (spec/PHASE_4.md) omits these fields so
    they remain at 0.
    """
    orders_1h: int = 0
    orders_24h: int = 0
    lifetime_total_orders: int = 0
    lifetime_total_spend_pence: int = 0
    avg_order_value_pence: int = 0
    chargeback_count: int = 0
    refund_count: int = 0
    first_order_at_epoch: int = 0  # reserved; spec/PHASE_4.md batch schema omits this field
    last_order_at_epoch: int = 0   # reserved; spec/PHASE_4.md batch schema omits this field


@dataclass(frozen=True)
class DeviceFeatures:
    orders_1h: int = 0
    unique_users_count: int = 0


@dataclass(frozen=True)
class IPFeatures:
    orders_1h: int = 0


def _build_feature_set(
    results: list[object],
    user_id: UUID,
    device_id: UUID | None,
    payment_method_id: UUID | None,
    ip_address: str,
    store_id: UUID,
    merchant_id: UUID,
    delivery_address_id: UUID | None,
    email_domain: str,
    latency_ms: float,
) -> FeatureSet:
    """Assemble a full Phase-6 `FeatureSet` from a pipeline result array."""

    def _result_hash(index: int) -> dict[str, str]:
        raw = results[index]
        return raw if isinstance(raw, dict) else {}

    idx = 0
    user_stream_key = f"fs:user:{user_id}:stream"
    user_stream = _result_hash(idx)
    idx += 1

    user_batch_key = f"fs:user:{user_id}:batch"
    user_batch = _result_hash(idx)
    idx += 1

    if device_id is not None:
        device_stream_key = f"fs:device:{device_id}:stream"
        device_stream = _result_hash(idx)
        idx += 1
        device_batch_key = f"fs:device:{device_id}:batch"
        device_batch = _result_hash(idx)
        idx += 1
    else:
        device_stream = {}
        device_batch = {}
        device_stream_key = None
        device_batch_key = None

    if payment_method_id is not None:
        payment_stream_key = f"fs:payment:{payment_method_id}:stream"
        payment_stream = _result_hash(idx)
        idx += 1
        payment_batch_key = f"fs:payment:{payment_method_id}:batch"
        payment_batch = _result_hash(idx)
        idx += 1
    else:
        payment_stream = {}
        payment_batch = {}
        payment_stream_key = None
        payment_batch_key = None

    ip_stream_key = f"fs:ip:{ip_address}:stream"
    ip_stream = _result_hash(idx)
    idx += 1

    ip_batch_key = f"fs:ip:{ip_address}:batch"
    ip_batch = _result_hash(idx)
    idx += 1

    store_stream_key = f"fs:store:{store_id}:stream"
    store_stream = _result_hash(idx)
    idx += 1

    store_batch_key = f"fs:store:{store_id}:batch"
    store_batch = _result_hash(idx)
    idx += 1

    merchant_batch_key = f"fs:merchant:{merchant_id}:batch"
    merchant_batch = _result_hash(idx)
    idx += 1

    email_domain_batch_key = f"fs:email_domain:{email_domain}:batch"
    email_domain_batch = _result_hash(idx)
    idx += 1

    if delivery_address_id is not None:
        address_stream_key = f"fs:address:{delivery_address_id}:stream"
        address_stream = _result_hash(idx)
    else:
        address_stream = {}
        address_stream_key = None

    missing_features: list[str] = []
    for key, payload in (
        (user_stream_key, user_stream),
        (user_batch_key, user_batch),
        (device_stream_key, device_stream),
        (device_batch_key, device_batch),
        (payment_stream_key, payment_stream),
        (payment_batch_key, payment_batch),
        (ip_stream_key, ip_stream),
        (ip_batch_key, ip_batch),
        (store_stream_key, store_stream),
        (store_batch_key, store_batch),
        (merchant_batch_key, merchant_batch),
        (email_domain_batch_key, email_domain_batch),
        (address_stream_key, address_stream),
    ):
        if key is not None and payload == {}:
            missing_features.append(key)

    return FeatureSet(
        user_orders_1h=_to_int(user_stream.get("orders_1h")),
        user_orders_24h=_to_int(user_stream.get("orders_24h")),
        user_spend_1h_pence=_to_int(user_stream.get("spend_1h_pence")),
        user_spend_24h_pence=_to_int(user_stream.get("spend_24h_pence")),
        user_unique_stores_24h=_to_int(user_stream.get("unique_stores_24h")),
        user_unique_payment_methods_24h=_to_int(user_stream.get("unique_payment_methods_24h")),
        user_last_order_age_minutes=_to_int_or_none(user_stream.get("last_order_age_minutes")),

        user_lifetime_order_count=_to_int_or_none(user_batch.get("lifetime_order_count")),
        user_lifetime_spend_pence=_to_int_or_none(user_batch.get("lifetime_spend_pence")),
        user_avg_order_value_pence=_to_int_or_none(user_batch.get("avg_order_value_pence")),
        user_lifetime_chargeback_count=_to_int_or_none(user_batch.get("lifetime_chargeback_count")),
        user_lifetime_refund_count=_to_int_or_none(user_batch.get("lifetime_refund_count")),
        user_lifetime_chargeback_rate=_to_float_or_none(user_batch.get("lifetime_chargeback_rate")),
        user_unique_devices_used=_to_int_or_none(user_batch.get("unique_devices_used")),
        user_unique_payment_methods_used=_to_int_or_none(user_batch.get("unique_payment_methods_used")),
        user_unique_delivery_addresses=_to_int_or_none(user_batch.get("unique_delivery_addresses")),
        user_account_age_days=_to_int_or_none(user_batch.get("account_age_days")),
        user_days_since_last_order=_to_int_or_none(user_batch.get("days_since_last_order")),
        user_distinct_cities_ordered_from=_to_int_or_none(user_batch.get("distinct_cities_ordered_from")),

        device_orders_1h=_to_int(device_stream.get("orders_1h")),
        device_orders_24h=_to_int(device_stream.get("orders_24h")),
        device_unique_users_24h=_to_int(device_stream.get("unique_users_24h")),
        device_unique_payment_methods_24h=_to_int(device_stream.get("unique_payment_methods_24h")),

        device_lifetime_order_count=_to_int_or_none(device_batch.get("lifetime_order_count")),
        device_lifetime_chargeback_rate=_to_float_or_none(device_batch.get("lifetime_chargeback_rate")),
        device_unique_users_lifetime=_to_int_or_none(device_batch.get("unique_users_lifetime")),
        device_first_seen_days_ago=_to_int_or_none(device_batch.get("first_seen_days_ago")),
        device_distinct_payment_methods_lifetime=_to_int_or_none(device_batch.get("distinct_payment_methods_lifetime")),

        payment_orders_1h=_to_int(payment_stream.get("orders_1h")),
        payment_orders_24h=_to_int(payment_stream.get("orders_24h")),
        payment_unique_users_24h=_to_int(payment_stream.get("unique_users_24h")),
        payment_decline_count_24h=_to_int(payment_stream.get("decline_count_24h")),

        payment_lifetime_order_count=_to_int_or_none(payment_batch.get("lifetime_order_count")),
        payment_lifetime_chargeback_count=_to_int_or_none(payment_batch.get("lifetime_chargeback_count")),
        payment_lifetime_chargeback_rate=_to_float_or_none(payment_batch.get("lifetime_chargeback_rate")),
        payment_unique_users_lifetime=_to_int_or_none(payment_batch.get("unique_users_lifetime")),
        payment_distinct_delivery_addresses_lifetime=_to_int_or_none(
            payment_batch.get("distinct_delivery_addresses_lifetime")
        ),

        ip_orders_1h=_to_int(ip_stream.get("orders_1h")),
        ip_orders_24h=_to_int(ip_stream.get("orders_24h")),
        ip_unique_users_24h=_to_int(ip_stream.get("unique_users_24h")),
        ip_unique_devices_24h=_to_int(ip_stream.get("unique_devices_24h")),

        ip_lifetime_order_count=_to_int_or_none(ip_batch.get("lifetime_order_count")),
        ip_unique_users_lifetime=_to_int_or_none(ip_batch.get("unique_users_lifetime")),
        ip_chargeback_rate=_to_float_or_none(ip_batch.get("chargeback_rate")),
        ip_first_seen_days_ago=_to_int_or_none(ip_batch.get("first_seen_days_ago")),

        store_orders_1h=_to_int(store_stream.get("orders_1h")),
        store_orders_24h=_to_int(store_stream.get("orders_24h")),
        store_unique_users_24h=_to_int(store_stream.get("unique_users_24h")),
        store_unique_cards_1h=_to_int(store_stream.get("unique_cards_1h")),

        store_avg_order_value_pence=_to_int_or_none(store_batch.get("avg_order_value_pence")),
        store_chargeback_rate=_to_float_or_none(store_batch.get("chargeback_rate")),
        store_unique_cards_30d=_to_int_or_none(store_batch.get("unique_cards_30d")),
        store_total_orders_30d=_to_int_or_none(store_batch.get("total_orders_30d")),

        merchant_chargeback_rate=_to_float_or_none(merchant_batch.get("chargeback_rate")),
        merchant_total_stores=_to_int_or_none(merchant_batch.get("total_stores")),

        email_domain_chargeback_rate=_to_float_or_none(email_domain_batch.get("chargeback_rate")),
        email_domain_total_orders=_to_int_or_none(email_domain_batch.get("total_orders")),

        address_orders_24h=_to_int(address_stream.get("orders_24h")),
        address_unique_users_24h=_to_int(address_stream.get("unique_users_24h")),

        feature_fetch_latency_ms=latency_ms,
        missing_features=missing_features,
    )


class FeatureStoreClient:
    """Synchronous feature-store client."""

    def __init__(self, redis_url: str) -> None:
        self._r: redis.Redis[str] = redis.Redis.from_url(redis_url, decode_responses=True)

    def close(self) -> None:
        self._r.close()

    def get_features(
        self,
        user_id: UUID,
        device_id: UUID | None,
        payment_method_id: UUID | None,
        ip_address: str,
        store_id: UUID,
        merchant_id: UUID,
        delivery_address_id: UUID | None,
        email_domain: str,
    ) -> FeatureSet:
        """Read all relevant hashes for an order context in one pipeline round-trip."""
        uid = str(user_id)
        start = time.perf_counter()

        pipe = self._r.pipeline(transaction=False)
        pipe.hgetall(f"fs:user:{uid}:stream")
        pipe.hgetall(f"fs:user:{uid}:batch")
        if device_id is not None:
            did = str(device_id)
            pipe.hgetall(f"fs:device:{did}:stream")
            pipe.hgetall(f"fs:device:{did}:batch")
        if payment_method_id is not None:
            pid = str(payment_method_id)
            pipe.hgetall(f"fs:payment:{pid}:stream")
            pipe.hgetall(f"fs:payment:{pid}:batch")
        pipe.hgetall(f"fs:ip:{ip_address}:stream")
        pipe.hgetall(f"fs:ip:{ip_address}:batch")
        sid = str(store_id)
        pipe.hgetall(f"fs:store:{sid}:stream")
        pipe.hgetall(f"fs:store:{sid}:batch")
        mid = str(merchant_id)
        pipe.hgetall(f"fs:merchant:{mid}:batch")
        pipe.hgetall(f"fs:email_domain:{email_domain}:batch")
        if delivery_address_id is not None:
            did2 = str(delivery_address_id)
            pipe.hgetall(f"fs:address:{did2}:stream")

        results = pipe.execute()
        latency_ms = (time.perf_counter() - start) * 1000
        return _build_feature_set(
            results=results,
            user_id=user_id,
            device_id=device_id,
            payment_method_id=payment_method_id,
            ip_address=ip_address,
            store_id=store_id,
            merchant_id=merchant_id,
            delivery_address_id=delivery_address_id,
            email_domain=email_domain,
            latency_ms=latency_ms,
        )


class AsyncFeatureStoreClient:
    def __init__(self, url: str | None = None) -> None:
        resolved = url if url is not None else os.environ.get("REDIS_URL", "redis://redis:6379/0")
        self._r: aioredis.Redis[str] = aioredis.from_url(resolved, decode_responses=True)

    async def close(self) -> None:
        await self._r.close()

    async def get_user_features(self, user_id: UUID) -> UserFeatures:
        uid = str(user_id)
        pipe: aioredis_client.Pipeline[str] = self._r.pipeline(transaction=False)
        pipe.hgetall(f"fs:user:{uid}:stream")
        pipe.hgetall(f"fs:user:{uid}:batch")
        stream_raw, batch_raw = await pipe.execute()
        stream: dict[str, str] = stream_raw if isinstance(stream_raw, dict) else {}
        batch: dict[str, str] = batch_raw if isinstance(batch_raw, dict) else {}
        return UserFeatures(
            orders_1h=_to_int(stream.get("orders_1h")),
            orders_24h=_to_int(stream.get("orders_24h")),
            lifetime_total_orders=_to_int(batch.get("lifetime_order_count")),
            lifetime_total_spend_pence=_to_int(batch.get("lifetime_spend_pence")),
            avg_order_value_pence=_to_int(batch.get("avg_order_value_pence")),
            chargeback_count=_to_int(batch.get("lifetime_chargeback_count")),
            refund_count=_to_int(batch.get("lifetime_refund_count")),
            first_order_at_epoch=0,  # not in spec/PHASE_4.md batch schema; P4-B does not write this field
            last_order_at_epoch=0,   # not in spec/PHASE_4.md batch schema; P4-B does not write this field
        )

    async def get_device_features(self, device_id: UUID) -> DeviceFeatures:
        did = str(device_id)
        pipe: aioredis_client.Pipeline[str] = self._r.pipeline(transaction=False)
        pipe.hgetall(f"fs:device:{did}:stream")
        results = await pipe.execute()
        stream_raw = results[0]
        stream: dict[str, str] = stream_raw if isinstance(stream_raw, dict) else {}
        return DeviceFeatures(
            orders_1h=_to_int(stream.get("orders_1h")),
            unique_users_count=_to_int(stream.get("unique_users_24h")),
        )

    async def get_ip_features(self, ip: str) -> IPFeatures:
        pipe: aioredis_client.Pipeline[str] = self._r.pipeline(transaction=False)
        pipe.hgetall(f"fs:ip:{ip}:stream")
        results = await pipe.execute()
        stream_raw = results[0]
        stream: dict[str, str] = stream_raw if isinstance(stream_raw, dict) else {}
        return IPFeatures(orders_1h=_to_int(stream.get("orders_1h")))

    async def get_features(
        self,
        user_id: UUID,
        device_id: UUID | None,
        payment_method_id: UUID | None,
        ip_address: str,
        store_id: UUID,
        merchant_id: UUID,
        delivery_address_id: UUID | None,
        email_domain: str,
    ) -> FeatureSet:
        """Read all relevant hashes for an order context in one pipeline round-trip."""
        uid = str(user_id)
        start = time.perf_counter()

        pipe: aioredis_client.Pipeline[str] = self._r.pipeline(transaction=False)
        pipe.hgetall(f"fs:user:{uid}:stream")
        pipe.hgetall(f"fs:user:{uid}:batch")
        if device_id is not None:
            did = str(device_id)
            pipe.hgetall(f"fs:device:{did}:stream")
            pipe.hgetall(f"fs:device:{did}:batch")
        if payment_method_id is not None:
            pid = str(payment_method_id)
            pipe.hgetall(f"fs:payment:{pid}:stream")
            pipe.hgetall(f"fs:payment:{pid}:batch")
        pipe.hgetall(f"fs:ip:{ip_address}:stream")
        pipe.hgetall(f"fs:ip:{ip_address}:batch")
        sid = str(store_id)
        pipe.hgetall(f"fs:store:{sid}:stream")
        pipe.hgetall(f"fs:store:{sid}:batch")
        mid = str(merchant_id)
        pipe.hgetall(f"fs:merchant:{mid}:batch")
        pipe.hgetall(f"fs:email_domain:{email_domain}:batch")
        if delivery_address_id is not None:
            did2 = str(delivery_address_id)
            pipe.hgetall(f"fs:address:{did2}:stream")

        results = await pipe.execute()
        latency_ms = (time.perf_counter() - start) * 1000
        return _build_feature_set(
            results=results,
            user_id=user_id,
            device_id=device_id,
            payment_method_id=payment_method_id,
            ip_address=ip_address,
            store_id=store_id,
            merchant_id=merchant_id,
            delivery_address_id=delivery_address_id,
            email_domain=email_domain,
            latency_ms=latency_ms,
        )


# Module-level singleton for re-use by long-running services
_default_client: AsyncFeatureStoreClient | None = None


def get_default_client() -> AsyncFeatureStoreClient:
    global _default_client
    if _default_client is None:
        _default_client = AsyncFeatureStoreClient()
    return _default_client


async def close_default_client() -> None:
    """Close and reset the module-level default feature-store client."""
    global _default_client
    if _default_client is not None:
        await _default_client.close()
    _default_client = None
