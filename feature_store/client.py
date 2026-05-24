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

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")


def _to_int(val: object, default: int = 0) -> int:
    """Convert a Redis value (str, None, or already int) to int."""
    if val is None:
        return default
    return int(str(val))


@dataclass(frozen=True)
class UserFeatures:
    orders_1h: int = 0
    orders_24h: int = 0
    lifetime_total_orders: int = 0
    lifetime_total_spend_pence: int = 0
    avg_order_value_pence: int = 0
    chargeback_count: int = 0
    refund_count: int = 0
    first_order_at_epoch: int = 0
    last_order_at_epoch: int = 0


@dataclass(frozen=True)
class DeviceFeatures:
    orders_1h: int = 0
    unique_users_count: int = 1


@dataclass(frozen=True)
class IPFeatures:
    orders_1h: int = 0


class FeatureStoreClient:
    def __init__(self, url: str = REDIS_URL) -> None:
        self._r: aioredis.Redis[str] = aioredis.from_url(url, decode_responses=True)

    async def close(self) -> None:
        await self._r.close()

    async def get_user_features(self, user_id: UUID) -> UserFeatures:
        uid = str(user_id)
        pipe: aioredis_client.Pipeline[str] = self._r.pipeline(transaction=False)
        pipe.get(f"velocity:user:{uid}:orders_1h")
        pipe.get(f"velocity:user:{uid}:orders_24h")
        pipe.hgetall(f"user:{uid}:lifetime_stats")
        results: list[object] = await pipe.execute()
        o1h, o24h, stats_raw = results[0], results[1], results[2]
        stats: dict[str, str] = stats_raw if isinstance(stats_raw, dict) else {}
        return UserFeatures(
            orders_1h=_to_int(o1h),
            orders_24h=_to_int(o24h),
            lifetime_total_orders=int(stats.get("total_orders", "0")),
            lifetime_total_spend_pence=int(stats.get("total_spend_pence", "0")),
            avg_order_value_pence=int(stats.get("avg_order_value_pence", "0")),
            chargeback_count=int(stats.get("chargeback_count", "0")),
            refund_count=int(stats.get("refund_count", "0")),
            first_order_at_epoch=int(stats.get("first_order_at_epoch", "0")),
            last_order_at_epoch=int(stats.get("last_order_at_epoch", "0")),
        )

    async def get_device_features(self, device_id: UUID) -> DeviceFeatures:
        did = str(device_id)
        pipe: aioredis_client.Pipeline[str] = self._r.pipeline(transaction=False)
        pipe.get(f"velocity:device:{did}:orders_1h")
        pipe.get(f"device:{did}:unique_users_count")
        results: list[object] = await pipe.execute()
        o1h, uu = results[0], results[1]
        return DeviceFeatures(
            orders_1h=_to_int(o1h),
            unique_users_count=_to_int(uu, default=1),
        )

    async def get_ip_features(self, ip: str) -> IPFeatures:
        o1h = await self._r.get(f"velocity:ip:{ip}:orders_1h")
        return IPFeatures(orders_1h=_to_int(o1h))


# Module-level singleton for re-use by long-running services
_default_client: FeatureStoreClient | None = None


def get_default_client() -> FeatureStoreClient:
    global _default_client
    if _default_client is None:
        _default_client = FeatureStoreClient()
    return _default_client
