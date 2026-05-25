from __future__ import annotations

import datetime
from typing import Any, Callable, Dict, Awaitable, TypedDict, cast
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
        self.calls: list[tuple[str, str, object]] = []
        self.zadd_calls: list[tuple[str, dict[str, object]]] = []
        self.hset_calls: list[tuple[str, dict[str, object]]] = []
        self.expire_calls: list[tuple[str, int]] = []
        self.zrangebyscore_calls: list[tuple[str, int]] = []
        self.zcount_calls: list[tuple[str, int]] = []
        self.zremrangebyscore_calls: list[tuple[str, object, object]] = []

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

    def zremrangebyscore(self, key: str, min_score: object, max_score: object) -> _FakePipeline:
        self.calls.append(("zremrangebyscore", key, min_score))
        self.zremrangebyscore_calls.append((key, min_score, max_score))
        return self

    def hset(self, key: str, mapping: dict[str, object]) -> _FakePipeline:
        self.calls.append(("hset", key, mapping))
        self.hset_calls.append((key, mapping))
        return self

    def expire(self, key: str, seconds: int) -> _FakePipeline:
        self.calls.append(("expire", key, seconds))
        self.expire_calls.append((key, seconds))
        return self

    async def execute(self) -> list[object]:
        return self.execute_result


class _FakeRedis:
    def __init__(
        self,
        pipeline_results: list[list[object]],
        scan_results: list[tuple[int, list[str]]] | None = None,
        zrevrange_result: list[list[tuple[str, object]]] | None = None,
        sismember_result: bool | list[bool] | None = None,
    ) -> None:
        self._pipeline_results = pipeline_results
        self.pipeline_calls: list[_FakePipeline] = []
        self.scan_calls: list[tuple[int, str, int]] = []
        self.scan_results = scan_results if scan_results is not None else [(0, [])]
        self.zrevrange_results = zrevrange_result if zrevrange_result is not None else []
        self.sismember_result: bool | list[bool] = (
            sismember_result if sismember_result is not None else False
        )
        self.sismember_args: list[tuple[str, str]] = []
        self.sadd_calls: list[tuple[str, str]] = []
        self.expire_calls: list[tuple[str, int]] = []
        self.zrevrange_calls: list[tuple[str, int, int, bool, bool]] = []

    def pipeline(self) -> _FakePipeline:
        result = self._pipeline_results.pop(0) if self._pipeline_results else []
        pipe = _FakePipeline(result)
        self.pipeline_calls.append(pipe)
        return pipe

    async def sismember(self, key: str, value: str) -> bool:
        self.sismember_args.append((key, value))
        if isinstance(self.sismember_result, list):
            if not self.sismember_result:
                return False
            return self.sismember_result.pop(0)
        return self.sismember_result

    async def zrevrange(
        self,
        key: str,
        start: int,
        stop: int,
        desc: bool = False,
        withscores: bool = False,
    ) -> list[tuple[str, object]]:
        self.zrevrange_calls.append((key, start, stop, desc, withscores))
        if self.zrevrange_results:
            return self.zrevrange_results.pop(0)
        return []

    async def sadd(self, key: str, value: str) -> int:
        self.sadd_calls.append((key, value))
        return 1

    async def expire(self, key: str, ttl: int) -> bool:
        self.expire_calls.append((key, ttl))
        return True

    async def scan(self, cursor: int, match: str, count: int) -> tuple[int, list[str]]:
        self.scan_calls.append((cursor, match, count))
        if self.scan_results:
            return self.scan_results.pop(0)
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


def _build_full_order_kwargs() -> _OrderKwargs:
    values = _build_order_kwargs()
    values["device_id"] = uuid4()
    values["payment_method_id"] = uuid4()
    values["delivery_address_id"] = uuid4()
    return values


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
            [None, None, None],
            [
                ["a:120", "b:30", "bad"],
                ["x:300", "y:40", "z:abc"],
                2,
                3,
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

    user_hset_calls = fake_redis.pipeline_calls[2].hset_calls
    assert len(fake_redis.pipeline_calls) == 3
    hset_call = user_hset_calls[0]
    user_mapping = hset_call[1]
    assert user_mapping["spend_1h_pence"] == 150
    assert user_mapping["spend_24h_pence"] == 340
    assert "last_order_age_minutes" not in user_mapping
    user_stream_key = f"fs:user:{row_values['user_id']}:stream"
    assert fake_redis.pipeline_calls[2].expire_calls == [
        (user_stream_key, aggregator.TWENTY_FOUR_HOURS_SECONDS),
    ]


@pytest.mark.asyncio
async def test_write_user_stream_aggregates_uses_previous_order_age() -> None:
    row_values = _build_order_kwargs()
    row = _build_order_row(**row_values)
    order = aggregator._coerce_order_context(row)
    now_ts = aggregator._utcnow_ts()
    previous_score = now_ts - 600

    fake_redis = _FakeRedis(
        pipeline_results=[
            [None, None, None],
            [
                ["a:120", "b:30", "bad"],
                ["x:300", "y:40", "z:abc"],
                2,
                3,
                2,
                0,
            ],
            [
                1,
            ],
        ],
        zrevrange_result=[[("order-current", now_ts), ("order-previous", previous_score)]],
    )

    await aggregator.write_user_stream_aggregates(
        redis_conn=cast(AsyncRedis[str], fake_redis), order=order, now_ts=now_ts
    )

    user_hset_calls = fake_redis.pipeline_calls[2].hset_calls
    user_mapping = user_hset_calls[0][1]
    assert user_mapping["last_order_age_minutes"] == 10
    assert fake_redis.zrevrange_calls[0][0] == f"fs:user:{row_values['user_id']}:orders_zset"
    user_stream_key = f"fs:user:{row_values['user_id']}:stream"
    assert fake_redis.pipeline_calls[2].expire_calls == [
        (user_stream_key, aggregator.TWENTY_FOUR_HOURS_SECONDS),
    ]
    assert fake_redis.zrevrange_calls[0] == (
        f"fs:user:{row_values['user_id']}:orders_zset",
        1,
        1,
        False,
        True,
    )


@pytest.mark.asyncio
async def test_write_user_stream_aggregates_omits_last_order_age_with_only_current_order() -> None:
    row_values = _build_order_kwargs()
    row = _build_order_row(**row_values)
    order = aggregator._coerce_order_context(row)
    now_ts = aggregator._utcnow_ts()

    fake_redis = _FakeRedis(
        pipeline_results=[
            [None, None, None],
            [
                ["a:120", "b:30", "bad"],
                ["x:300", "y:40", "z:abc"],
                2,
                3,
                2,
                0,
            ],
            [
                1,
            ],
        ],
        zrevrange_result=[[("order-current", now_ts)]],
    )

    await aggregator.write_user_stream_aggregates(
        redis_conn=cast(AsyncRedis[str], fake_redis), order=order, now_ts=now_ts
    )

    user_hset_calls = fake_redis.pipeline_calls[2].hset_calls
    user_mapping = user_hset_calls[0][1]
    assert "last_order_age_minutes" not in user_mapping


@pytest.mark.asyncio
@patch("feature_store.aggregator._utcnow_ts", return_value=1_700_000_000)
@patch("feature_store.aggregator.update_features_for_order")
async def test_backup_poll_once_skips_processed_orders(
    update_features_for_order: AsyncMock,
    _utcnow_ts: AsyncMock,
) -> None:
    timestamp = 1_700_000_000
    kwargs = _build_order_kwargs()
    row = _build_order_row(**kwargs)
    conn = AsyncMock()
    conn.fetch.return_value = [row]
    pool = AsyncMock()
    pool_acquire = AsyncMock()
    pool_acquire.__aenter__.return_value = conn
    pool.acquire.return_value = pool_acquire

    fake_redis = _FakeRedis(pipeline_results=[], sismember_result=True)

    await aggregator.run_backup_poll_once(
        pool=pool,
        redis_conn=cast(AsyncRedis[str], fake_redis),
        metrics=aggregator.Metrics(errors=[0]),
    )

    _utcnow_ts.assert_called_once_with()
    assert fake_redis.sismember_args == [
        (aggregator.processed_bucket_key(timestamp), str(kwargs["order_id"]))
    ]
    update_features_for_order.assert_not_awaited()


@pytest.mark.asyncio
@patch("feature_store.aggregator._utcnow_ts", return_value=1_700_000_000)
@patch("feature_store.aggregator.update_features_for_order")
async def test_backup_poll_once_skips_previous_bucket_processed_orders(
    update_features_for_order: AsyncMock,
    _utcnow_ts: AsyncMock,
) -> None:
    kwargs = _build_order_kwargs()
    row = _build_order_row(**kwargs)
    conn = AsyncMock()
    conn.fetch.return_value = [row]
    pool = AsyncMock()
    pool_acquire = AsyncMock()
    pool_acquire.__aenter__.return_value = conn
    pool.acquire.return_value = pool_acquire

    fake_redis = _FakeRedis(
        pipeline_results=[],
        sismember_result=[False, True],
    )

    await aggregator.run_backup_poll_once(
        pool=pool,
        redis_conn=cast(AsyncRedis[str], fake_redis),
        metrics=aggregator.Metrics(errors=[0]),
    )

    now_ts = 1_700_000_000
    assert fake_redis.sismember_args == [
        (aggregator.processed_bucket_key(now_ts), str(kwargs["order_id"])),
        (
            aggregator.processed_bucket_key(now_ts - aggregator.ORDER_TTL_SECONDS),
            str(kwargs["order_id"]),
        ),
    ]
    _utcnow_ts.assert_called_once_with()
    update_features_for_order.assert_not_awaited()


@pytest.mark.asyncio
@patch("feature_store.aggregator._utcnow_ts", return_value=1_700_000_000)
async def test_mark_order_processed_buckets_keys_and_ttl() -> None:
    now_ts = 1_700_000_000
    expected_bucket = aggregator.processed_bucket_key(now_ts)
    order_id = str(uuid4())

    fake_redis = _FakeRedis(pipeline_results=[])

    await aggregator._mark_order_processed(
        redis_conn=cast(AsyncRedis[str], fake_redis), order_id=order_id
    )

    assert fake_redis.sadd_calls == [(expected_bucket, order_id)]
    assert fake_redis.expire_calls == [(expected_bucket, aggregator.PROCESSED_ORDERS_TTL_SECONDS)]


@pytest.mark.parametrize(
    "writer,stream_key_fn,write_pipeline_results,read_results",
    [
        (
            aggregator.write_user_stream_aggregates,
            lambda kwargs: f"fs:user:{kwargs['user_id']}:stream",
            [None, None, None, None],
            [
                ["a:120", "b:30", "bad"],
                ["x:300", "y:40", "z:abc"],
                2,
                3,
                2,
                0,
            ],
        ),
        (
            aggregator.write_device_stream_aggregates,
            lambda kwargs: f"fs:device:{kwargs['device_id']}:stream",
            [None, None, None, None, None, None, None],
            [2, 3, 4, 4],
        ),
        (
            aggregator.write_payment_stream_aggregates,
            lambda kwargs: f"fs:payment:{kwargs['payment_method_id']}:stream",
            [None, None, None, None, None],
            [2, 3, 4],
        ),
        (
            aggregator.write_ip_stream_aggregates,
            lambda kwargs: f"fs:ip:{kwargs['ip_address']}:stream",
            [None, None, None, None, None, None, None],
            [2, 3, 4, 4],
        ),
        (
            aggregator.write_store_stream_aggregates,
            lambda kwargs: f"fs:store:{kwargs['store_id']}:stream",
            [None, None, None, None, None, None, None],
            [2, 3, 4, 4],
        ),
        (
            aggregator.write_address_stream_aggregates,
            lambda kwargs: f"fs:address:{kwargs['delivery_address_id']}:stream",
            [None, None, None, None],
            [2, 3],
        ),
    ],
)
@pytest.mark.asyncio
async def test_write_stream_aggregates_set_stream_ttl(
    writer: Callable[..., Awaitable[None]],
    stream_key_fn: Callable[[_OrderKwargs], str],
    write_pipeline_results: list[object],
    read_results: list[object],
) -> None:
    row_values = _build_full_order_kwargs()
    row = _build_order_row(**row_values)
    order = aggregator._coerce_order_context(row)

    fake_redis = _FakeRedis(
        pipeline_results=[
            write_pipeline_results,
            read_results,
            [1],
        ],
    )

    await writer(redis_conn=cast(AsyncRedis[str], fake_redis), order=order, now_ts=1_700_000_000)

    stream_key = stream_key_fn(row_values)
    assert fake_redis.pipeline_calls[2].expire_calls == [
        (stream_key, aggregator.TWENTY_FOUR_HOURS_SECONDS),
    ]


@patch("feature_store.aggregator._utcnow_ts", return_value=1_700_000_000)
@patch("feature_store.aggregator._refresh_user_stream_aggregates", new=AsyncMock())
@patch("feature_store.aggregator._refresh_device_stream_aggregates", new=AsyncMock())
@patch("feature_store.aggregator._refresh_payment_stream_aggregates", new=AsyncMock())
@patch("feature_store.aggregator._refresh_ip_stream_aggregates", new=AsyncMock())
@patch("feature_store.aggregator._refresh_store_stream_aggregates", new=AsyncMock())
@patch("feature_store.aggregator._refresh_address_stream_aggregates", new=AsyncMock())
@pytest.mark.asyncio
async def test_trim_order_zsets_once_refreshes_trimmed_stream_entities(
    _refresh_address_stream_aggregates: AsyncMock,
    _refresh_store_stream_aggregates: AsyncMock,
    _refresh_ip_stream_aggregates: AsyncMock,
    _refresh_payment_stream_aggregates: AsyncMock,
    _refresh_device_stream_aggregates: AsyncMock,
    _refresh_user_stream_aggregates: AsyncMock,
    _utcnow_ts: AsyncMock,
) -> None:
    del _utcnow_ts

    fake_redis = _FakeRedis(
        pipeline_results=[
            [1],
            [1],
            [1],
            [1],
            [1],
            [1],
            [1],
            [1],
        ],
        scan_results=[
            (1, ["fs:user:111:orders_zset"]),
            (0, ["fs:user:111:spend_zset"]),
            (0, ["fs:store:444:stores_zset"]),
            (0, ["fs:payment:333:payments_zset"]),
            (1, ["fs:device:222:users_zset"]),
            (0, ["fs:address:999:users_zset"]),
            (0, ["fs:ip:2001:db8::1:devices_zset"]),
            (0, ["fs:store:444:cards_1h_zset"]),
        ],
    )

    await aggregator.trim_order_zsets_once(
        redis_conn=cast(AsyncRedis[str], fake_redis),
        metrics=aggregator.Metrics(errors=[0]),
    )

    assert _refresh_user_stream_aggregates.await_count == 1
    assert _refresh_user_stream_aggregates.await_args is not None
    assert _refresh_user_stream_aggregates.await_args.kwargs["user_id"] == "111"

    assert _refresh_device_stream_aggregates.await_count == 1
    assert _refresh_device_stream_aggregates.await_args is not None
    assert _refresh_device_stream_aggregates.await_args.kwargs["device_id"] == "222"

    assert _refresh_payment_stream_aggregates.await_count == 1
    assert _refresh_payment_stream_aggregates.await_args is not None
    assert _refresh_payment_stream_aggregates.await_args.kwargs["payment_id"] == "333"

    assert _refresh_ip_stream_aggregates.await_count == 1
    assert _refresh_ip_stream_aggregates.await_args is not None
    assert _refresh_ip_stream_aggregates.await_args.kwargs["ip_key"] == "2001:db8::1"

    assert _refresh_store_stream_aggregates.await_count == 1
    assert _refresh_store_stream_aggregates.await_args is not None
    assert _refresh_store_stream_aggregates.await_args.kwargs["store_id"] == "444"

    assert _refresh_address_stream_aggregates.await_count == 1
    assert _refresh_address_stream_aggregates.await_args is not None
    assert _refresh_address_stream_aggregates.await_args.kwargs["address_id"] == "999"

    assert _refresh_user_stream_aggregates.await_args.kwargs["now_ts"] == 1_700_000_000
    assert _refresh_device_stream_aggregates.await_args.kwargs["now_ts"] == 1_700_000_000
    assert _refresh_payment_stream_aggregates.await_args.kwargs["now_ts"] == 1_700_000_000
    assert _refresh_ip_stream_aggregates.await_args.kwargs["now_ts"] == 1_700_000_000
    assert _refresh_store_stream_aggregates.await_args.kwargs["now_ts"] == 1_700_000_000
    assert _refresh_address_stream_aggregates.await_args.kwargs["now_ts"] == 1_700_000_000


@pytest.mark.asyncio
async def test_trim_order_zsets_once_scans_all_relevant_zsets() -> None:
    fake_redis = _FakeRedis(
        pipeline_results=[],
        scan_results=[
            (0, ["fs:user:111:orders_zset"]),
            (0, ["fs:user:111:spend_zset"]),
            (0, ["fs:user:111:stores_zset"]),
            (0, ["fs:user:111:payments_zset"]),
            (0, ["fs:user:111:users_zset"]),
            (0, ["fs:user:111:devices_zset"]),
            (0, ["fs:user:111:cards_1h_zset"]),
        ],
    )

    await aggregator.trim_order_zsets_once(
        redis_conn=cast(AsyncRedis[str], fake_redis),
        metrics=aggregator.Metrics(errors=[0]),
    )

    expected_matches = [f"fs:*{suffix}" for suffix in aggregator.CLEANUP_ZSET_SUFFIXES]
    assert [call[1] for call in fake_redis.scan_calls] == expected_matches
    assert all(call[2] == 5000 for call in fake_redis.scan_calls)
    assert [
        key
        for pipe in fake_redis.pipeline_calls
        for call in pipe.zremrangebyscore_calls
        for key in [call[0]]
    ] == [
        "fs:user:111:orders_zset",
        "fs:user:111:spend_zset",
        "fs:user:111:stores_zset",
        "fs:user:111:payments_zset",
        "fs:user:111:users_zset",
        "fs:user:111:devices_zset",
        "fs:user:111:cards_1h_zset",
    ]


def test_extract_entity_from_zset_key_matches_cleanup_suffix() -> None:
    assert aggregator._extract_entity_from_zset_key(
        key="fs:user:abc-123:orders_zset",
        suffix=":orders_zset",
    ) == ("user", "abc-123")
