"""Chronological timestamp synthesis for bulk order generation.

Approach (option a for feature aggregator):
    Stop the feature_aggregator container before running bulk. NOTIFY triggers
    fire into void; Redis stays empty until aggregator restarts and rebuilds.
    Phase 5 training reads Postgres directly, so empty Redis is fine. A
    separate "warm Redis from Postgres" packet is required before Phase 6
    scoring eval.
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import (  # Python 3.8 backport; zoneinfo is 3.9+.
        ZoneInfo,
    )

LONDON_TZ = ZoneInfo("Europe/London")


def _as_london_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=LONDON_TZ)
    return value.astimezone(LONDON_TZ)


def synthesize_chronological_timestamps(
    *,
    window_start: datetime,
    window_end: datetime,
    rate_multiplier: float,
    rng: random.Random,
) -> list[datetime]:
    """Return timestamps sampled from a Poisson process using current_rate().

    Samples inter-arrival times via rng.expovariate(current_rate(ts, ...)) so
    the temporal distribution (hourly, day-of-week, per-day lognormal) matches
    the live simulator exactly. Same seed and window returns the same list.
    """
    from simulator.generator import current_rate  # Lazy import avoids generator import cycles.

    day_multiplier_cache: dict[date, float] = {}
    timestamps: list[datetime] = []
    cursor = _as_london_aware(window_start)
    window_end_tz = _as_london_aware(window_end)

    while cursor < window_end_tz:
        rate = current_rate(
            now=cursor,
            multiplier=rate_multiplier,
            day_multiplier_cache=day_multiplier_cache,
        )
        if rate <= 0.0:
            cursor += timedelta(seconds=1.0)
            continue

        interval = rng.expovariate(rate)
        cursor += timedelta(seconds=interval)
        if cursor < window_end_tz:
            timestamps.append(cursor)

    return timestamps
