from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import pytest

from simulator.timestamps import LONDON_TZ, synthesize_chronological_timestamps


pytest.importorskip("asyncpg")


def _window() -> tuple[datetime, datetime]:
    window_start = datetime(2026, 5, 1, 18, 0, 0, tzinfo=LONDON_TZ)
    return window_start, window_start + timedelta(hours=1)


def test_timestamps_are_sorted() -> None:
    window_start, window_end = _window()

    timestamps = synthesize_chronological_timestamps(
        window_start=window_start,
        window_end=window_end,
        rate_multiplier=0.001,
        rng=random.Random(42),
    )

    assert timestamps == sorted(timestamps)


def test_timestamps_within_window() -> None:
    window_start, window_end = _window()

    timestamps = synthesize_chronological_timestamps(
        window_start=window_start,
        window_end=window_end,
        rate_multiplier=0.001,
        rng=random.Random(42),
    )

    assert timestamps
    assert all(window_start <= timestamp < window_end for timestamp in timestamps)


def test_timestamps_deterministic() -> None:
    window_start, window_end = _window()

    first = synthesize_chronological_timestamps(
        window_start=window_start,
        window_end=window_end,
        rate_multiplier=0.001,
        rng=random.Random(292),
    )
    second = synthesize_chronological_timestamps(
        window_start=window_start,
        window_end=window_end,
        rate_multiplier=0.001,
        rng=random.Random(292),
    )

    assert first == second


def test_timestamps_follow_hourly_pattern() -> None:
    window_start = datetime(2026, 5, 1, 0, 0, 0, tzinfo=LONDON_TZ)
    window_end = window_start + timedelta(days=2)

    timestamps = synthesize_chronological_timestamps(
        window_start=window_start,
        window_end=window_end,
        rate_multiplier=0.0001,
        rng=random.Random(42),
    )

    peak_count = sum(
        1 for timestamp in timestamps if 18 <= timestamp.astimezone(LONDON_TZ).hour <= 21
    )
    trough_count = sum(
        1 for timestamp in timestamps if 2 <= timestamp.astimezone(LONDON_TZ).hour <= 5
    )

    assert peak_count > 0
    assert peak_count > trough_count * 5


def test_day_multiplier_caching(monkeypatch: pytest.MonkeyPatch) -> None:
    observed_cache_ids: set[int] = set()
    observed_values_by_date: dict[date, set[float]] = {}

    def fake_current_rate(
        *,
        now: datetime,
        multiplier: float,
        day_multiplier_cache: dict[date, float],
    ) -> float:
        assert multiplier == 1.0
        current_date = now.astimezone(LONDON_TZ).date()
        day_multiplier = day_multiplier_cache.get(current_date)
        if day_multiplier is None:
            day_multiplier = float(len(day_multiplier_cache) + 1)
            day_multiplier_cache[current_date] = day_multiplier

        observed_cache_ids.add(id(day_multiplier_cache))
        observed_values_by_date.setdefault(current_date, set()).add(day_multiplier)
        return 100.0

    monkeypatch.setattr("simulator.generator.current_rate", fake_current_rate)
    window_start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=LONDON_TZ)
    window_end = window_start + timedelta(seconds=1)

    timestamps = synthesize_chronological_timestamps(
        window_start=window_start,
        window_end=window_end,
        rate_multiplier=1.0,
        rng=random.Random(42),
    )

    assert timestamps
    assert len(observed_cache_ids) == 1
    assert observed_values_by_date == {window_start.date(): {1.0}}


def test_timestamps_empty_for_zero_window() -> None:
    window_start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=LONDON_TZ)

    timestamps = synthesize_chronological_timestamps(
        window_start=window_start,
        window_end=window_start,
        rate_multiplier=1.0,
        rng=random.Random(42),
    )

    assert timestamps == []


def test_timestamps_rate_multiplier_scales_count() -> None:
    window_start, window_end = _window()

    lower_rate = synthesize_chronological_timestamps(
        window_start=window_start,
        window_end=window_end,
        rate_multiplier=0.0005,
        rng=random.Random(42),
    )
    higher_rate = synthesize_chronological_timestamps(
        window_start=window_start,
        window_end=window_end,
        rate_multiplier=0.002,
        rng=random.Random(42),
    )

    assert len(higher_rate) > len(lower_rate)
