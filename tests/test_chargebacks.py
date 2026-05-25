"""Unit tests for simulator/chargebacks.py helpers."""
from __future__ import annotations

import uuid
from simulator.chargebacks import _chargeback_age_allowed, _refund_age_allowed, _refund_due_at_hours


def test_chargeback_age_allowed_within_window() -> None:
    assert _chargeback_age_allowed(0.0)
    assert _chargeback_age_allowed(60.0)


def test_chargeback_age_allowed_excludes_older_than_60_days() -> None:
    assert not _chargeback_age_allowed(60.000_000_1)
    assert not _chargeback_age_allowed(61.0)


def test_refund_age_allowed_within_window() -> None:
    assert _refund_age_allowed(0.0)
    assert _refund_age_allowed(120.0)


def test_refund_age_allowed_excludes_older_than_120_hours() -> None:
    assert not _refund_age_allowed(120.000_000_1)
    assert not _refund_age_allowed(121.0)


def test_refund_due_at_hours_range() -> None:
    """_refund_due_at_hours returns values in [0, 120] for all order_ids."""
    delays = [_refund_due_at_hours(uuid.uuid4()) for _ in range(1000)]
    assert all(0.0 <= d <= 120.0 for d in delays), "delay out of [0, 120] range"


def test_refund_due_at_hours_distribution() -> None:
    """Delays should be roughly uniform: mean near 60h, spanning the full range."""
    delays = [_refund_due_at_hours(uuid.uuid4()) for _ in range(1000)]
    mean_h = sum(delays) / len(delays)
    assert 45.0 <= mean_h <= 75.0, f"mean {mean_h:.1f}h not near 60h — distribution skewed"
    assert min(delays) < 10.0, "no delays under 10h in 1000 samples — lower tail missing"
    assert max(delays) > 110.0, "no delays over 110h in 1000 samples — upper tail missing"


def test_refund_due_at_hours_deterministic() -> None:
    """Same order_id must yield the same delay on every call."""
    oid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    d1 = _refund_due_at_hours(oid)
    d2 = _refund_due_at_hours(oid)
    assert d1 == d2, "delay is not deterministic for the same order_id"


def test_refund_due_at_hours_different_orders_differ() -> None:
    """Different order_ids should (with overwhelming probability) yield different delays."""
    delays = {_refund_due_at_hours(uuid.uuid4()) for _ in range(100)}
    assert len(delays) > 90, "too many collisions — RNG not seeded by order_id"
