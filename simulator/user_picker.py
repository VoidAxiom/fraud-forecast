"""Weighted user selection helper for simulator order generation.

This helper keeps a sliding window of active users loaded from PostgreSQL and
applies a weighted pick strategy that prefers recent activity and Redis-defined
activity tiers.

If Redis sets ``simulator:heavy_users`` or ``simulator:very_active_users`` are
absent/empty, the picker falls back gracefully:

- no ``simulator:very_active_users`` entries => use the base 60/30/10 split
  (recency/heavy/uniform) for all draws;
- no ``simulator:heavy_users`` entries => heavy tier draws fall back to uniform.

The distribution assumes P2-A seeded ``simulator:heavy_users`` and
``simulator:very_active_users``; these sets drive the 30% and 5% branches when
present.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from typing import Any, List, Optional, Set

import asyncpg
import redis.asyncio as aioredis


class WeightedUserPicker:
    """Selects user IDs with recency and Redis-driven weighted distribution."""

    _SELECT_ACTIVE_USERS = """
        SELECT user_id FROM users WHERE account_status = 'ACTIVE'
        ORDER BY last_login_at DESC NULLS LAST
        LIMIT 100000
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        redis: aioredis.Redis[Any],
        refresh_every: int = 300_000,
    ) -> None:
        self._pool = pool
        self._redis = redis
        self._refresh_every = refresh_every
        self._pick_count = 0
        self._buffer: List[uuid.UUID] = []
        self._buffer_set: Set[uuid.UUID] = set()
        self._heavy_users_cache: List[uuid.UUID] = []
        self._very_active_users_cache: List[uuid.UUID] = []

    async def refresh_redis_caches(self) -> None:
        """Refresh heavy and very-active Redis user caches."""
        heavy_raw = await self._redis.smembers("simulator:heavy_users")
        self._heavy_users_cache = [
            uuid.UUID(member.decode()) for member in sorted(heavy_raw)
        ]

        very_active_raw = await self._redis.smembers("simulator:very_active_users")
        self._very_active_users_cache = [
            uuid.UUID(member.decode()) for member in sorted(very_active_raw)
        ]

    async def refresh(self) -> None:
        """Refresh the in-memory recency-sorted active user buffer."""
        records = await self._pool.fetch(self._SELECT_ACTIVE_USERS)
        self._buffer = [self._extract_user_id(record) for record in records]
        self._buffer_set = set(self._buffer)
        await self.refresh_redis_caches()

    def pick(self, rng: random.Random) -> uuid.UUID:
        """Return one active user id according to the weighted sampling strategy."""
        self._pick_count += 1
        if self._pick_count >= self._refresh_every:
            asyncio.ensure_future(self.refresh())
            self._pick_count = 0

        if not self._buffer:
            raise RuntimeError("user buffer is empty; call refresh() before pick()")

        very_active_users = self._very_active_users_cache
        if very_active_users and rng.random() < 0.05:
            pick = self._pick_very_active_user(rng, very_active_users)
            if pick is not None and pick in self._buffer_set:
                return pick

        # Split 60/30/10 across recency/heavy/uniform for all remaining draws.
        branch_roll = rng.random()
        if branch_roll < 0.60:
            return self._pick_recency_user(rng)

        heavy_users = self._heavy_users_cache
        if branch_roll < 0.90:
            if heavy_users:
                pick = self._pick_heavy_user(rng, heavy_users)
                if pick is not None and pick in self._buffer_set:
                    return pick
                return self._pick_uniform_user(rng)
            return self._pick_uniform_user(rng)

        return self._pick_uniform_user(rng)

    @staticmethod
    def _extract_user_id(record: Any) -> uuid.UUID:
        if isinstance(record, asyncpg.Record):
            user_id = record["user_id"]
        else:
            user_id = getattr(record, "user_id")
        if not isinstance(user_id, uuid.UUID):
            raise TypeError(f"expected user_id as UUID, got {type(user_id)}")
        return user_id

    def _pick_very_active_user(
        self,
        rng: random.Random,
        users: List[uuid.UUID],
    ) -> Optional[uuid.UUID]:
        if not users:
            return None
        user_id = rng.choice(users)
        if user_id in self._buffer_set:
            return user_id
        return None

    def _pick_heavy_user(
        self,
        rng: random.Random,
        users: List[uuid.UUID],
    ) -> Optional[uuid.UUID]:
        if not users:
            return None
        user_id = rng.choice(users)
        if user_id in self._buffer_set:
            return user_id
        return None

    def _pick_recency_user(self, rng: random.Random) -> uuid.UUID:
        recency_window = max(1, int(len(self._buffer) * 0.60))
        idx = rng.randrange(recency_window)
        return self._buffer[idx]

    def _pick_uniform_user(self, rng: random.Random) -> uuid.UUID:
        idx = rng.randrange(len(self._buffer))
        return self._buffer[idx]
