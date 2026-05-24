"""Shared pytest fixtures.

All DB-touching tests run against the LIVE primary postgres (single
environment per CLAUDE.md "Scope"). Tests are responsible for cleaning
up any rows they insert."""

from __future__ import annotations

import datetime
import random
from collections.abc import Iterator
from typing import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from shared.db import get_engine, get_session


@pytest.fixture(scope="session")
def db_engine() -> Engine:
    return get_engine("app")


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    with get_session("app") as session:
        yield session


@pytest.fixture
def clean_db(db_engine: Engine) -> Callable[[list[str]], None]:
    def _clean(tables: list[str]) -> None:
        with db_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE"))

    return _clean


@pytest.fixture
def seeded_random() -> random.Random:
    return random.Random(42)


@pytest.fixture
def mock_clock() -> datetime.datetime:
    return datetime.datetime(2026, 5, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)
