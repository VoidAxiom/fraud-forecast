"""Unit tests for fraud-pattern eager-write entity helpers."""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from simulator.fraud_patterns._entity_factory import create_fresh_device


def _mock_conn() -> Any:
    return SimpleNamespace(execute=AsyncMock())


def _execute_values(conn: Any) -> tuple[Any, ...]:
    conn.execute.assert_awaited_once()
    query, *values = conn.execute.await_args.args
    assert "INSERT INTO devices" in query
    return tuple(values)


def test_create_fresh_device_inserts_realistic_device_row() -> None:
    conn = _mock_conn()
    now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    device_id = asyncio.run(create_fresh_device(random.Random(42), conn, now))
    values = _execute_values(conn)

    assert isinstance(device_id, uuid.UUID)
    assert values[0] == device_id
    assert isinstance(values[0], uuid.UUID)
    assert len(values[1]) == 64
    int(values[1], 16)
    assert values[2] in {"MOBILE_APP", "MOBILE_WEB", "DESKTOP_WEB", "TABLET"}
    assert values[3] in {"iOS", "Android", "Windows", "macOS", "Linux"}
    assert values[9] == "Europe/London"
    assert values[10] == "en-GB"
    assert values[16] == 1


def test_create_fresh_device_honours_ios_platform_bias() -> None:
    conn = _mock_conn()
    now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    device_id = asyncio.run(
        create_fresh_device(random.Random(7), conn, now, platform_bias="iOS")
    )
    values = _execute_values(conn)

    assert isinstance(device_id, uuid.UUID)
    assert values[2] == "MOBILE_APP"
    assert values[3] == "iOS"
    assert str(values[4]).startswith("iOS ")
    assert values[5] != ""
    assert values[6] == ""


def test_create_fresh_device_returns_uuid_not_string() -> None:
    conn = _mock_conn()
    now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
    device_id = asyncio.run(create_fresh_device(random.Random(99), conn, now))

    assert isinstance(device_id, uuid.UUID)
    assert not isinstance(device_id, str)


def test_create_fresh_device_allows_seeded_replay_conflict() -> None:
    first_conn = _mock_conn()
    replay_conn = _mock_conn()
    now = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

    first_device_id = asyncio.run(
        create_fresh_device(random.Random(275), first_conn, now)
    )
    replay_device_id = asyncio.run(
        create_fresh_device(random.Random(275), replay_conn, now)
    )
    first_values = _execute_values(first_conn)
    replay_values = _execute_values(replay_conn)
    first_query = first_conn.execute.await_args.args[0]
    replay_query = replay_conn.execute.await_args.args[0]

    assert first_device_id == replay_device_id
    assert first_values[0] == first_device_id
    assert replay_values[0] == replay_device_id
    assert "ON CONFLICT (device_id) DO NOTHING" in first_query
    assert "ON CONFLICT (device_id) DO NOTHING" in replay_query
