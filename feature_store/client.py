"""Base async client for the Redis feature store.

Returns typed feature bundles with cold-start defaults. Pipelined reads
to minimise round-trips. P4-D will extend this with the full
`assemble_feature_vector(order_snapshot)` API used by Phase 6 scoring.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

import redis.asyncio as aioredis
import redis.asyncio.client as aioredis_client


def _to_int(val: object, default: int = 0) -> int:
    """Convert a Redis value (str, None, or already int) to int."""
    if val is None:
        return default
    return int(str(val))


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


class FeatureStoreClient:
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


# Module-level singleton for re-use by long-running services
_default_client: FeatureStoreClient | None = None


def get_default_client() -> FeatureStoreClient:
    global _default_client
    if _default_client is None:
        _default_client = FeatureStoreClient()
    return _default_client


async def close_default_client() -> None:
    """Close and reset the module-level default feature-store client."""
    global _default_client
    if _default_client is not None:
        await _default_client.close()
    _default_client = None
