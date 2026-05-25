from __future__ import annotations

import datetime
from typing import Any, Dict, TypedDict, cast
from uuid import UUID, uuid4
from unittest.mock import AsyncMock, patch
from backports.zoneinfo import ZoneInfo
from redis.asyncio import Redis as AsyncRedis

import pytest

import feature_store.aggregator as aggregator


class _FakeRecord(Dict[str, Any]):
    def __getitem__(self, key: str) -> object:
        return super().__getitem__(key)


class _OrderKwargs(TypedDict):
    order_id: UUID
    user_id: UUID
    store_id: UUID
    merchant_id: UUID
    device_id: UUID | None
    ip_address: str
    payment_method_id: UUID | None
    delivery_address_id: UUID | None
    total_pence: int
    placed_at: datetime.datetime
    user_email_domain: str


class _FakePipeline:
    def __init__(self, execute_result: list[object]) -> None:
        self.execute_result = execute_result
        self.calls: list[tuple[str, str, str | int | dict[str, object]]] = []
        self.zadd_calls: list[tuple[str, dict[str, object]]] = []
        self.hset_calls: list[tuple[str, dict[str, object]]] = []
        self.zrangebyscore_calls: list[tuple[str, int]] = []
        self.zcount_calls: list[tuple[str, int]] = []

    def zadd(self, key: str, mapping: dict[str, object]) -> _FakePipeline:
        self.calls.append(("zadd", key, mapping))
        self.zadd_calls.append((key, mapping))
        return self

    def zrangebyscore(self, key: str, min_score: int, max_score: int) -> _FakePipeline:
        del max_score
        self.calls.append(("zrangebyscore", key, min_score))
        self.zrangebyscore_calls.append((key, min_score))
        return self

    def zcount(self, key: str, min_score: int, max_score: int) -> _FakePipeline:
        del max_score
        self.calls.append(("zcount", key, min_score))
        self.zcount_calls.append((key, min_score))
        return self

    def hset(self, key: str, mapping: dict[str, object]) -> _FakePipeline:
        self.calls.append(("hset", key, mapping))
        self.hset_calls.append((key, mapping))
        return self

    async def execute(self) -> list[object]:
        return self.execute_result


class _FakeRedis:
    def __init__(self, pipeline_results: list[list[object]]) -> None:
        self._pipeline_results = pipeline_results
        self.pipeline_calls: list[_FakePipeline] = []
        self.sismember_result = False
        self.sismember_args: list[tuple[str, str]] = []
        self.sadd_calls: list[str] = []

    def pipeline(self) -> _FakePipeline:
        result = self._pipeline_results.pop(0)
        pipe = _FakePipeline(result)
        self.pipeline_calls.append(pipe)
        return pipe

    async def sismember(self, key: str, value: str) -> bool:
        self.sismember_args.append((key, value))
        return self.sismember_result

    async def sadd(self, key: str, value: str) -> int:
        self.sadd_calls.append(value)
        return 1

    async def expire(self, key: str, ttl: int) -> bool:
        del key
        del ttl
        return True

    async def scan(self, cursor: int, match: str, count: int) -> tuple[int, list[str]]:
        del cursor
        del match
        del count
        return 0, []


def _build_order_row(**values: object) -> _FakeRecord:
    return _FakeRecord(
        {
            "order_id": values["order_id"],
            "user_id": values["user_id"],
            "store_id": values["store_id"],
            "merchant_id": values["merchant_id"],
            "device_id": values.get("device_id"),
            "ip_address": values["ip_address"],
            "payment_method_id": values.get("payment_method_id"),
            "delivery_address_id": values.get("delivery_address_id"),
            "total_pence": values["total_pence"],
            "placed_at": values["placed_at"],
            "user_email_domain": values["user_email_domain"],
        },
    )


def _build_order_kwargs() -> _OrderKwargs:
    now = datetime.datetime(2026, 5, 24, 12, 0, 0, tzinfo=ZoneInfo("Europe/London"))
    return {
        "order_id": uuid4(),
        "user_id": uuid4(),
        "store_id": uuid4(),
        "merchant_id": uuid4(),
        "device_id": None,
        "ip_address": "192.168.1.1",
        "payment_method_id": None,
        "delivery_address_id": None,
        "total_pence": 1250,
        "placed_at": now,
        "user_email_domain": "example.com",
    }


@pytest.mark.asyncio
@patch("feature_store.aggregator.write_device_stream_aggregates", new=AsyncMock())
@patch("feature_store.aggregator.write_payment_stream_aggregates", new=AsyncMock())
@patch("feature_store.aggregator.write_ip_stream_aggregates", new=AsyncMock())
@patch("feature_store.aggregator.write_store_stream_aggregates", new=AsyncMock())
@patch("feature_store.aggregator.write_address_stream_aggregates", new=AsyncMock())
async def test_update_features_for_order_updates_user_orders_zset() -> None:
    kwargs = _build_order_kwargs()
    order_id: UUID = kwargs["order_id"]
    row = _build_order_row(**kwargs)
    conn = AsyncMock()
    conn.fetchrow.return_value = row
    pool = AsyncMock()
    pool_acquire = AsyncMock()
    pool_acquire.__aenter__.return_value = conn
    pool.acquire.return_value = pool_acquire

    fake_redis = _FakeRedis(
        pipeline_results=[
            [
                None,
                None,
                ["order:1", "order:2"],
                ["order:3", "order:4"],
                2,
                3,
                4,
                4,
                2,
                4,
                0,
            ],
            [1],
        ],
    )

    await aggregator.update_features_for_order(
        pool=pool, redis_conn=cast(AsyncRedis[str], fake_redis), order_id=order_id
    )

    user_key = f"fs:user:{kwargs['user_id']}:orders_zset"
    user_calls = fake_redis.pipeline_calls[0].zadd_calls
    assert any(call[0] == user_key for call in user_calls)
    assert any(str(order_id) in call[1] for call in user_calls if call[0] == user_key)


@pytest.mark.asyncio
async def test_write_user_stream_aggregates_parses_spend_members() -> None:
    row_values = _build_order_kwargs()
    row = _build_order_row(**row_values)
    order = aggregator._coerce_order_context(row)
    now_ts = aggregator._utcnow_ts()

    fake_redis = _FakeRedis(
        pipeline_results=[
            [
                None,
                None,
                None,
                ["a:120", "b:30", "bad"],
                ["x:300", "y:40", "z:abc"],
                2,
                3,
                2,
                2,
                0,
            ],
            [
                1,
            ],
        ],
    )

    await aggregator.write_user_stream_aggregates(
        redis_conn=cast(AsyncRedis[str], fake_redis), order=order, now_ts=now_ts
    )

    user_hset_calls = fake_redis.pipeline_calls[1].hset_calls
    hset_call = user_hset_calls[0]
    user_mapping = hset_call[1]
    assert user_mapping["spend_1h_pence"] == 150
    assert user_mapping["spend_24h_pence"] == 340


@pytest.mark.asyncio
@patch("feature_store.aggregator.update_features_for_order")
async def test_backup_poll_once_skips_processed_orders(
    update_features_for_order: AsyncMock,
) -> None:
    kwargs = _build_order_kwargs()
    row = _build_order_row(**kwargs)
    conn = AsyncMock()
    conn.fetch.return_value = [row]
    pool = AsyncMock()
    pool_acquire = AsyncMock()
    pool_acquire.__aenter__.return_value = conn
    pool.acquire.return_value = pool_acquire

    fake_redis = _FakeRedis(pipeline_results=[])
    fake_redis.sismember_result = True

    await aggregator.run_backup_poll_once(
        pool=pool,
        redis_conn=cast(AsyncRedis[str], fake_redis),
        metrics=aggregator.Metrics(errors=[0]),
    )

    update_features_for_order.assert_not_awaited()
