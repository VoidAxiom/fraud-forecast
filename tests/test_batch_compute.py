from __future__ import annotations

import datetime
import logging
import sys
from typing import Any, Mapping

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

import feature_store.batch_compute as batch_compute
import pytest


class _FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self._index = 0

    def fetchmany(self, size: int) -> list[dict[str, object]]:
        start = self._index
        end = self._index + size
        self._index = end
        return self._rows[start:end]


class _FakeConnection:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: object,
    ) -> None:
        pass

    def execution_options(self, **_: Any) -> _FakeConnection:
        return self

    def execute(self, statement: object, params: dict[str, object] | None = None) -> _FakeResult:
        _ = statement
        _ = params
        return _FakeResult(self._rows)


class _FakeEngine:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def connect(self) -> _FakeConnection:
        return _FakeConnection(self._rows)


class _FakePipeline:
    def __init__(self, store: dict[str, dict[str, str]]) -> None:
        self._store = store
        self._pending: list[tuple[str, dict[str, str]]] = []
        self.expirations: dict[str, int] = {}

    def hset(self, key: str, mapping: Mapping[str, Any]) -> _FakePipeline:
        self._pending.append((key, dict(mapping)))
        return self

    def expire(self, key: str, ttl: int) -> _FakePipeline:
        self.expirations[key] = ttl
        return self

    def execute(self) -> list[int]:
        for key, mapping in self._pending:
            self._store.setdefault(key, {})
            self._store[key].update({k: str(v) for k, v in mapping.items()})
        self._pending.clear()
        return [1]


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, dict[str, str]] = {}
        self.pipelines: list[_FakePipeline] = []

    def pipeline(self, transaction: bool = False) -> _FakePipeline:
        _ = transaction
        pipeline = _FakePipeline(self.store)
        self.pipelines.append(pipeline)
        return pipeline

    def close(self) -> None:
        return None


class _FailingConnection(_FakeConnection):
    def execute(self, statement: object, params: dict[str, object] | None = None) -> _FakeResult:
        _ = statement
        _ = params
        raise RuntimeError("intentional connection failure")


class _FailingEngine(_FakeEngine):
    def connect(self) -> _FakeConnection:
        return _FailingConnection([])


def test_compute_user_batch_features_writes_expected_payload() -> None:
    now = datetime.datetime(2026, 5, 25, 12, 0, tzinfo=ZoneInfo("Europe/London"))
    user_id = "00000000-0000-0000-0000-000000000001"
    rows = [
        {
            "user_id": user_id,
            "created_at": datetime.datetime(
                2026,
                5,
                20,
                12,
                tzinfo=ZoneInfo("Europe/London"),
            ),
            "lifetime_order_count": 4,
            "lifetime_spend_pence": 1000,
            "avg_order_value_pence": 250,
            "lifetime_chargeback_count": 1,
            "unique_devices_used": 2,
            "unique_payment_methods_used": 1,
            "unique_delivery_addresses": 3,
            "distinct_cities_ordered_from": 2,
            "last_placed_at": datetime.datetime(
                2026,
                5,
                24,
                8,
                tzinfo=ZoneInfo("Europe/London"),
            ),
        }
    ]
    engine = _FakeEngine(rows)
    redis_client = _FakeRedis()
    expected_key = f"fs:user:{user_id}:batch"
    original_now = batch_compute._now
    batch_compute._now = lambda: now
    try:
        batch_compute.compute_user_batch_features(engine, redis_client)
    finally:
        batch_compute._now = original_now

    assert expected_key in redis_client.store
    payload = redis_client.store[expected_key]

    assert payload["lifetime_order_count"] == "4"
    assert payload["lifetime_spend_pence"] == "1000"
    assert payload["avg_order_value_pence"] == "250"
    assert payload["lifetime_chargeback_count"] == "1"
    assert payload["lifetime_refund_count"] == "0"
    assert payload["lifetime_chargeback_rate"] == "0.25"
    assert payload["unique_devices_used"] == "2"
    assert payload["account_age_days"] == "5"
    assert payload["days_since_last_order"] == "1"
    assert payload["distinct_cities_ordered_from"] == "2"
    assert redis_client.pipelines[0].expirations[expected_key] == 172_800


def test_compute_device_batch_features_zero_orders_still_writes_zero_rate() -> None:
    now = datetime.datetime(2026, 5, 25, 12, 0, tzinfo=ZoneInfo("Europe/London"))
    device_id = "00000000-0000-0000-0000-000000000002"
    rows = [
        {
            "device_id": device_id,
            "lifetime_order_count": 0,
            "lifetime_chargeback_count": 3,
            "unique_users_lifetime": 0,
            "distinct_payment_methods_lifetime": 0,
            "first_seen_at": datetime.datetime(
                2026,
                5,
                18,
                12,
                tzinfo=ZoneInfo("Europe/London"),
            ),
        }
    ]
    engine = _FakeEngine(rows)
    redis_client = _FakeRedis()
    original_now = batch_compute._now
    batch_compute._now = lambda: now
    try:
        batch_compute.compute_device_batch_features(engine, redis_client)
    finally:
        batch_compute._now = original_now

    expected_key = f"fs:device:{device_id}:batch"
    payload = redis_client.store[expected_key]
    assert payload["lifetime_order_count"] == "0"
    assert payload["lifetime_chargeback_rate"] == "0.0"
    assert payload["first_seen_days_ago"] == "7"
    assert payload["unique_users_lifetime"] == "0"


def test_run_batch_dispatches_all_entity_computes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    engine = object()
    redis_client = _FakeRedis()
    monkeypatch.setattr(batch_compute, "get_engine", lambda role="app": engine)
    monkeypatch.setattr(
        batch_compute.redis.Redis,
        "from_url",
        lambda url, decode_responses=True: redis_client,
    )
    monkeypatch.setattr(batch_compute, "compute_user_batch_features", lambda engine, r: calls.append("user"))
    monkeypatch.setattr(batch_compute, "compute_device_batch_features", lambda engine, r: calls.append("device"))
    monkeypatch.setattr(
        batch_compute,
        "compute_payment_batch_features",
        lambda engine, r: calls.append("payment"),
    )
    monkeypatch.setattr(
        batch_compute,
        "compute_ip_batch_features",
        lambda engine, r: calls.append("ip"),
    )
    monkeypatch.setattr(
        batch_compute,
        "compute_store_batch_features",
        lambda engine, r: calls.append("store"),
    )
    monkeypatch.setattr(
        batch_compute,
        "compute_merchant_batch_features",
        lambda engine, r: calls.append("merchant"),
    )
    monkeypatch.setattr(
        batch_compute,
        "compute_email_domain_batch_features",
        lambda engine, r: calls.append("email_domain"),
    )

    batch_compute.run_batch()

    assert calls == [
        "user",
        "device",
        "payment",
        "ip",
        "store",
        "merchant",
        "email_domain",
    ]


def test_main_once_runs_batch_once(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(sys, "argv", ["batch_compute", "--once"])
    monkeypatch.setattr(batch_compute, "run_batch", lambda: called.append("run_batch"))

    batch_compute.main()

    assert called == ["run_batch"]


def test_main_serve_starts_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeTrigger:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class _FakeScheduler:
        def __init__(self, timezone: str) -> None:
            self.timezone = timezone
            self.jobs: list[dict[str, Any]] = []
            self.started = False

        def add_job(self, func: object, trigger: _FakeTrigger) -> None:
            self.jobs.append({"func": func, "trigger": trigger})

        def start(self) -> None:
            self.started = True

    _scheduler = _FakeScheduler(timezone=batch_compute.EUROPE_LONDON)
    sys_argv = sys.argv.copy()
    monkeypatch.setattr(sys, "argv", ["batch_compute", "--serve"])
    monkeypatch.setattr(batch_compute, "BlockingScheduler", lambda timezone: _scheduler)
    monkeypatch.setattr(batch_compute, "CronTrigger", _FakeTrigger)
    try:
        batch_compute.main()
    finally:
        monkeypatch.setattr(sys, "argv", sys_argv)

    assert _scheduler.started
    assert len(_scheduler.jobs) == 1
    assert isinstance(_scheduler.jobs[0]["trigger"], _FakeTrigger)
    assert _scheduler.jobs[0]["trigger"].kwargs["hour"] == 2
    assert _scheduler.jobs[0]["trigger"].kwargs["minute"] == 0
    assert _scheduler.jobs[0]["trigger"].kwargs["timezone"] == batch_compute.EUROPE_LONDON


def test_compute_store_entity_logs_errors() -> None:
    fake_redis = _FakeRedis()
    engine = _FailingEngine([])

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    batch_compute.LOG.addHandler(handler)
    try:
        batch_compute.compute_store_batch_features(engine, fake_redis)
    finally:
        batch_compute.LOG.removeHandler(handler)

    assert any(
        record.levelno >= logging.ERROR and record.__dict__.get("entity") == "store"
        for record in records
    )
