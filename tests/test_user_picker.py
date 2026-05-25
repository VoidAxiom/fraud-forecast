from __future__ import annotations

import asyncio
import random
import uuid
from unittest.mock import AsyncMock, patch

from simulator.user_picker import WeightedUserPicker


class _FakeRecord:
    def __init__(self, user_id: uuid.UUID) -> None:
        self.user_id = user_id


def _make_redis_mock(very_active: list[uuid.UUID], heavy: list[uuid.UUID]) -> AsyncMock:
    redis = AsyncMock()

    async def smembers(key: str) -> set[bytes]:
        if key == "simulator:very_active_users":
            return {str(user_id).encode() for user_id in very_active}
        if key == "simulator:heavy_users":
            return {str(user_id).encode() for user_id in heavy}
        return set()

    redis.smembers.side_effect = smembers
    return redis


def _make_empty_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.smembers.return_value = set()
    return redis


def _make_pool_with_rows(rows: list[_FakeRecord]) -> AsyncMock:
    pool = AsyncMock()
    pool.fetch.return_value = rows
    return pool


def test_user_picker_buffer_initialization() -> None:
    buffer_users = [uuid.uuid4() for _ in range(1000)]
    pool = _make_pool_with_rows([_FakeRecord(user_id=user_id) for user_id in buffer_users])
    redis_mock = _make_empty_redis()
    picker = WeightedUserPicker(pool=pool, redis=redis_mock)

    asyncio.run(picker.refresh())

    assert len(picker._buffer) == 1000
    assert redis_mock.smembers.await_count == 2


def test_user_picker_weighted_distribution() -> None:
    rng = random.Random(42)
    buffer_users = [uuid.UUID(int=i) for i in range(1000)]
    very_active_users = buffer_users[:50]
    heavy_users = buffer_users[600:800]

    picker = WeightedUserPicker(
        pool=_make_pool_with_rows([]),
        redis=_make_empty_redis(),
        refresh_every=1_000_000,
    )
    picker._very_active_users_cache = very_active_users
    picker._heavy_users_cache = heavy_users
    picker._buffer = buffer_users
    picker._buffer_set = set(buffer_users)

    with patch.object(
        picker,
        "_pick_very_active_user",
        wraps=picker._pick_very_active_user,
    ) as very_active_calls:
        with patch.object(
            picker,
            "_pick_recency_user",
            wraps=picker._pick_recency_user,
        ) as recency_calls:
            with patch.object(
                picker,
                "_pick_heavy_user",
                wraps=picker._pick_heavy_user,
            ) as heavy_calls:
                with patch.object(
                    picker,
                    "_pick_uniform_user",
                    wraps=picker._pick_uniform_user,
                ) as uniform_calls:
                    for _ in range(2000):
                        picker.pick(rng)

                    very_active_count = very_active_calls.call_count
                    recency_count = recency_calls.call_count
                    heavy_count = heavy_calls.call_count
                    uniform_count = uniform_calls.call_count
    assert 70 <= very_active_count <= 130

    remaining = 2000 - very_active_count
    expected_recency = remaining * 0.60
    expected_heavy = remaining * 0.30
    expected_uniform = remaining * 0.10

    assert expected_recency * 0.85 <= recency_count <= expected_recency * 1.15
    assert expected_heavy * 0.85 <= heavy_count <= expected_heavy * 1.15
    assert expected_uniform * 0.85 <= uniform_count <= expected_uniform * 1.15


def test_user_picker_refresh_threshold() -> None:
    rng = random.Random(42)
    buffer_users = [uuid.UUID(int=i) for i in range(1000)]
    picker = WeightedUserPicker(
        pool=AsyncMock(),
        redis=_make_empty_redis(),
        refresh_every=10,
    )
    picker._buffer = buffer_users
    picker._buffer_set = set(buffer_users)

    with patch("simulator.user_picker.asyncio.ensure_future"):
        for _ in range(11):
            picker.pick(rng)

    assert picker._pick_count == 1


def test_user_picker_fallback_when_redis_sets_empty() -> None:
    rng = random.Random(42)
    buffer_users = [uuid.UUID(int=i) for i in range(1000)]
    picker = WeightedUserPicker(pool=AsyncMock(), redis=_make_empty_redis())
    picker._very_active_users_cache = []
    picker._heavy_users_cache = []
    picker._buffer = buffer_users
    picker._buffer_set = set(buffer_users)

    allowed = set(buffer_users)
    for _ in range(1000):
        picked = picker.pick(rng)
        assert picked in allowed


def test_user_picker_redis_tier_validates_buffer() -> None:
    """Redis tier picks that are not in the active buffer fall through to next tier."""
    rng = random.Random(0)
    buffer_users = [uuid.UUID(int=i) for i in range(100)]
    stale_very_active = [uuid.UUID(int=i + 1000) for i in range(10)]

    picker = WeightedUserPicker(pool=AsyncMock(), redis=_make_empty_redis())
    picker._buffer = buffer_users
    picker._buffer_set = set(buffer_users)
    picker._very_active_users_cache = stale_very_active
    picker._heavy_users_cache = []
    allowed = set(buffer_users)

    with patch.object(rng, "random", return_value=0.0):
        for _ in range(500):
            picked = picker.pick(rng)
            assert picked in allowed, f"Picked {picked!r} not in buffer"
