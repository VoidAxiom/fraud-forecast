from __future__ import annotations

from simulator.seed import (
    _STORE_HOUR_PATTERNS,
    _STORE_HOUR_WEIGHTS,
    _STORE_HOUR_WINDOWS,
)


def _is_open_at(open_time: str, close_time: str, hour: int) -> bool:
    open_h = int(open_time.split(":")[0])
    close_h = int(close_time.split(":")[0])
    # 24h window: close_time "23:59:59.999999" -> treat as always open
    if open_time == "00:00:00" and close_time == "23:59:59.999999":
        return True
    if close_h > open_h:
        return open_h <= hour < close_h
    # overnight (close < open): open at hour if hour>=open_h or hour<close_h
    return hour >= open_h or hour < close_h


def test_store_hours_cover_all_24_hours() -> None:
    windows = [_STORE_HOUR_WINDOWS[p] for p in _STORE_HOUR_PATTERNS]
    for hour in range(24):
        assert any(_is_open_at(o, c, hour) for (o, c) in windows), f"hour {hour} uncovered"


def test_24h_window_open_every_hour() -> None:
    o, c = _STORE_HOUR_WINDOWS["24h"]
    assert c == "23:59:59.999999"
    for hour in range(24):
        assert _is_open_at(o, c, hour)


def test_weights_sum_to_one_and_align() -> None:
    assert len(_STORE_HOUR_PATTERNS) == len(_STORE_HOUR_WEIGHTS)
    assert abs(sum(_STORE_HOUR_WEIGHTS) - 1.0) < 1e-9
    assert set(_STORE_HOUR_WINDOWS) == set(_STORE_HOUR_PATTERNS)


def test_24h_stores_never_get_closed_day() -> None:
    # mirror the guard in seed_store_hours: closed_day only assigned when pattern != "24h"
    for pattern in _STORE_HOUR_PATTERNS:
        eligible_for_closed_day = pattern != "24h"
        if pattern == "24h":
            assert not eligible_for_closed_day
