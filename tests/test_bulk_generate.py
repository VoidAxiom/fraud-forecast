from __future__ import annotations

import asyncio
import os
import random
import uuid
from contextlib import ExitStack
from datetime import datetime, time, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

asyncpg = pytest.importorskip("asyncpg")

from simulator.bulk_generate import (  # noqa: E402
    BulkRunConfig,
    _parse_end_at,
    _rate_multiplier_from_env,
    bulk_generate,
)
from simulator.cart_builder import Cart  # noqa: E402
from simulator.generator import DATABASE_URL_SIMULATOR, LONDON_TZ, generate_order  # noqa: E402
from simulator.timestamps import synthesize_chronological_timestamps  # noqa: E402


_BULK_DAYS = 0.001
_BULK_RATE_MULTIPLIER = 0.001


class _FixedUserPicker:
    def __init__(self, user_id: uuid.UUID) -> None:
        self._user_id = user_id

    def pick(self, _rng: random.Random) -> uuid.UUID:
        return self._user_id


class _EmptyRedis:
    async def smembers(self, _key: str) -> set[bytes]:
        return set()


def _fixed_end_at() -> datetime:
    return datetime(2026, 5, 1, 19, 0, 0, tzinfo=LONDON_TZ)


def _bulk_config(*, day_offset: int, seed: int, force: bool = False) -> BulkRunConfig:
    target_date = datetime.now(tz=LONDON_TZ).date() + timedelta(days=day_offset)
    end_at = datetime.combine(target_date, time(19, 0), tzinfo=LONDON_TZ)
    return BulkRunConfig(
        days=_BULK_DAYS,
        end_at=end_at,
        seed=seed,
        force=force,
        rate_multiplier=_BULK_RATE_MULTIPLIER,
    )


def _window_start(config: BulkRunConfig) -> datetime:
    return config.end_at - timedelta(days=config.days)


async def _create_pool_or_skip() -> asyncpg.Pool:
    try:
        pool = await asyncpg.create_pool(DATABASE_URL_SIMULATOR, min_size=2, max_size=5)
    except Exception as exc:
        pytest.skip(f"requires live DB: {exc}")
    return pool


async def _require_seed_data(pool: asyncpg.Pool) -> None:
    active_user_count = await pool.fetchval(
        """
        SELECT COUNT(*)
        FROM users
        WHERE account_status = 'ACTIVE'
        """
    )
    store_count = await pool.fetchval("SELECT COUNT(*) FROM stores")
    menu_item_count = await pool.fetchval("SELECT COUNT(*) FROM menu_items WHERE is_available")

    if int(active_user_count or 0) == 0:
        pytest.skip("requires seeded active users")
    if int(store_count or 0) == 0:
        pytest.skip("requires seeded stores")
    if int(menu_item_count or 0) == 0:
        pytest.skip("requires seeded available menu items")


async def _cleanup_window(
    pool: asyncpg.Pool,
    *,
    window_start: datetime,
    window_end: datetime,
) -> None:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT order_id
            FROM orders
            WHERE placed_at >= $1
              AND placed_at <= $2
            """,
            window_start,
            window_end,
        )
        order_ids = [row["order_id"] for row in rows]
        if not order_ids:
            return

        await conn.execute("DELETE FROM chargebacks WHERE order_id = ANY($1::uuid[])", order_ids)
        await conn.execute(
            "DELETE FROM sim.simulator_ground_truth WHERE order_id = ANY($1::uuid[])",
            order_ids,
        )
        await conn.execute("DELETE FROM order_events WHERE order_id = ANY($1::uuid[])", order_ids)
        await conn.execute("DELETE FROM order_items WHERE order_id = ANY($1::uuid[])", order_ids)
        await conn.execute("DELETE FROM orders WHERE order_id = ANY($1::uuid[])", order_ids)


async def _fetch_orders_in_window(
    pool: asyncpg.Pool,
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[asyncpg.Record]:
    rows = await pool.fetch(
        """
        SELECT order_id, placed_at, user_id, user_total_orders_lifetime
        FROM orders
        WHERE placed_at >= $1
          AND placed_at < $2
        ORDER BY placed_at, order_id
        """,
        window_start,
        window_end,
    )
    return list(rows)


async def _noop_ensure_fraud_pattern_state(
    _pool: asyncpg.Pool,
    _rng: random.Random,
    _stores_by_city: dict[str, list[dict[str, Any]]],
) -> None:
    return None


async def _noop_write_bulk_metadata(
    _pool: asyncpg.Pool,
    *,
    run_id: str,
    value: dict[str, Any],
) -> None:
    return None


async def _noop_maybe_emit_chargeback(
    _order_id: uuid.UUID,
    _conn: asyncpg.Connection,
    *,
    now: datetime,
) -> None:
    return None


async def _fake_ephemeral_payment_method(
    _conn: asyncpg.Connection,
    user_id: uuid.UUID,
    _rng: random.Random,
) -> dict[str, Any]:
    return {
        "payment_method_id": uuid.uuid5(uuid.NAMESPACE_DNS, f"bulk-test-payment-{user_id}"),
        "payment_type": "CREDIT_CARD",
        "card_bin": "411111",
        "card_last_four": "1111",
        "card_brand": "VISA",
        "card_funding_type": "CREDIT",
        "card_issuer_country": "GB",
        "is_digital_native_bank": False,
        "unique_users_count": 1,
    }


async def _run_bulk_with_test_patches(
    config: BulkRunConfig,
    pool: asyncpg.Pool,
) -> dict[str, Any]:
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"FRAUD_INJECTION_RATE": "0.0"}))
        stack.enter_context(
            patch(
                "simulator.bulk_generate.ensure_fraud_pattern_state",
                _noop_ensure_fraud_pattern_state,
            )
        )
        stack.enter_context(
            patch("simulator.bulk_generate._write_bulk_metadata", _noop_write_bulk_metadata)
        )
        stack.enter_context(
            patch("simulator.bulk_generate.maybe_emit_chargeback", _noop_maybe_emit_chargeback)
        )
        stack.enter_context(
            patch(
                "simulator.generator._insert_ephemeral_payment_method",
                _fake_ephemeral_payment_method,
            )
        )
        return await bulk_generate(config, pool, redis_conn=_EmptyRedis())


def test_bulk_run_config_validates_days() -> None:
    with pytest.raises(ValueError, match="days must be greater than 0"):
        BulkRunConfig(days=0.0, end_at=_fixed_end_at(), seed=42)


def test_bulk_run_config_validates_rate_multiplier() -> None:
    with pytest.raises(ValueError, match="rate_multiplier must be greater than 0"):
        BulkRunConfig(days=1.0, end_at=_fixed_end_at(), seed=42, rate_multiplier=0.0)


def test_parse_end_at_none() -> None:
    before = datetime.now(tz=LONDON_TZ)
    parsed = _parse_end_at(None)
    after = datetime.now(tz=LONDON_TZ)

    assert before <= parsed <= after
    assert parsed.tzinfo is not None


def test_parse_end_at_iso_z() -> None:
    parsed = _parse_end_at("2026-05-01T12:00:00Z")

    assert parsed.tzinfo is not None
    assert parsed.astimezone(timezone.utc) == datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert parsed == datetime(2026, 5, 1, 13, 0, 0, tzinfo=LONDON_TZ)


def test_rate_multiplier_from_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BULK_RATE_MULTIPLIER", raising=False)
    monkeypatch.delenv("LIVE_RATE_MULTIPLIER", raising=False)

    assert _rate_multiplier_from_env() == 1.0


def test_rate_multiplier_from_env_bulk_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BULK_RATE_MULTIPLIER", "0.5")
    monkeypatch.delenv("LIVE_RATE_MULTIPLIER", raising=False)

    assert _rate_multiplier_from_env() == 0.5


def test_rate_multiplier_from_env_live_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BULK_RATE_MULTIPLIER", raising=False)
    monkeypatch.setenv("LIVE_RATE_MULTIPLIER", "0.3")

    assert _rate_multiplier_from_env() == 0.3


def test_rate_multiplier_bulk_takes_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BULK_RATE_MULTIPLIER", "0.5")
    monkeypatch.setenv("LIVE_RATE_MULTIPLIER", "0.3")

    assert _rate_multiplier_from_env() == 0.5


def test_generate_order_uses_placed_at_timestamp() -> None:
    async def _run() -> None:
        injected_now = datetime(2026, 5, 1, 19, 0, 0, tzinfo=timezone.utc)
        user_id = uuid.UUID(int=1)
        store_id = uuid.UUID(int=2)
        payment_method_id = uuid.UUID(int=3)
        device_id = uuid.UUID(int=4)
        user_data = {
            "user": {"user_id": user_id},
            "addresses": [],
            "default_address": {"city": "London"},
            "devices": [{"device_id": device_id}],
            "payment_methods": [{"payment_method_id": payment_method_id}],
        }
        store = {
            "store_id": store_id,
            "accepts_in_store": False,
        }
        cart = Cart(store_id=store_id, items=[])
        captured_snapshot_placed_at: list[datetime] = []
        captured_insert_placed_at: list[datetime] = []

        async def fake_load_user_data(
            _conn: object,
            _user_id: uuid.UUID,
        ) -> dict[str, object]:
            return user_data

        async def fake_is_new_payment_method(
            _conn: object,
            _user_id: uuid.UUID,
            _payment_method_id: uuid.UUID,
        ) -> bool:
            return False

        async def fake_load_menu_items(
            _conn: object,
            _store_id: uuid.UUID,
        ) -> list[object]:
            return [object()]

        async def fake_read_user_order_metrics(
            _conn: object,
            _user_id: uuid.UUID,
        ) -> tuple[int, int, int]:
            return 0, 0, 0

        async def fake_apply_promo(
            _conn: object,
            _user_id: uuid.UUID,
            _rng: random.Random,
            _is_first_order_for_user: bool,
            _promos: list[dict[str, object]],
            _subtotal_pence: int,
        ) -> None:
            return None

        def fake_build_snapshot(**kwargs: object) -> dict[str, object]:
            placed_at = kwargs["placed_at"]
            assert isinstance(placed_at, datetime)
            captured_snapshot_placed_at.append(placed_at)
            return {}

        async def fake_insert_order(
            _conn: object,
            _snapshot: dict[str, object],
            _cart: Cart,
            placed_at: datetime,
            *,
            is_fraud: bool = False,
            fraud_category: str | None = None,
            pattern_notes: str | None = None,
            ring_id: uuid.UUID | None = None,
        ) -> tuple[uuid.UUID, datetime]:
            assert not is_fraud
            assert fraud_category is None
            assert pattern_notes is None
            assert ring_id is None
            captured_insert_placed_at.append(placed_at)
            return uuid.UUID(int=99), placed_at

        async def fake_notify_order_placed(_conn: object, _order_id: uuid.UUID) -> None:
            return None

        rng = random.Random(123)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"FRAUD_INJECTION_RATE": "0.0"}))
            stack.enter_context(patch("simulator.generator.load_user_data", fake_load_user_data))
            stack.enter_context(
                patch("simulator.generator.pick_store_for_user", return_value=store)
            )
            stack.enter_context(
                patch("simulator.generator._select_order_type", return_value="PICKUP")
            )
            stack.enter_context(
                patch("simulator.generator.pick_channel_for_user", return_value="WEB")
            )
            stack.enter_context(
                patch(
                    "simulator.generator.pick_device_and_ip",
                    return_value=({"device_id": device_id}, "81.2.3.4"),
                )
            )
            stack.enter_context(
                patch("simulator.generator._is_new_payment_method", fake_is_new_payment_method)
            )
            stack.enter_context(patch("simulator.generator._load_menu_items", fake_load_menu_items))
            stack.enter_context(
                patch("simulator.generator.build_realistic_cart", return_value=cart)
            )
            stack.enter_context(
                patch("simulator.generator._read_user_order_metrics", fake_read_user_order_metrics)
            )
            stack.enter_context(patch("simulator.generator.apply_promo", fake_apply_promo))
            stack.enter_context(
                patch("simulator.generator.compute_pricing", return_value=(0, 0, 0, 0))
            )
            stack.enter_context(patch("simulator.generator._build_snapshot", fake_build_snapshot))
            stack.enter_context(
                patch(
                    "simulator.generator.generate_order_number",
                    return_value="JE-2026-AAAAAAAAAA",
                )
            )
            stack.enter_context(patch("simulator.generator.insert_order", fake_insert_order))
            stack.enter_context(
                patch("simulator.generator.notify_order_placed", fake_notify_order_placed)
            )

            order_id = await generate_order(
                rng,
                object(),
                now=injected_now,
                user_picker=_FixedUserPicker(user_id),
                stores_by_city={"London": [store]},
                store_hours_by_store_id={store_id: []},
                promos=[],
                scoring_enabled=False,
            )

        assert order_id == uuid.UUID(int=99)
        assert captured_snapshot_placed_at == [injected_now]
        assert captured_insert_placed_at == [injected_now]

    asyncio.run(_run())


def test_bulk_generate_reproducibility() -> None:
    config = BulkRunConfig(
        days=_BULK_DAYS,
        end_at=_fixed_end_at(),
        seed=292,
        rate_multiplier=_BULK_RATE_MULTIPLIER,
    )
    window_start = _window_start(config)

    first = synthesize_chronological_timestamps(
        window_start=window_start,
        window_end=config.end_at,
        rate_multiplier=config.rate_multiplier,
        rng=random.Random(config.seed),
    )
    second = synthesize_chronological_timestamps(
        window_start=window_start,
        window_end=config.end_at,
        rate_multiplier=config.rate_multiplier,
        rng=random.Random(config.seed),
    )

    assert first == second


@pytest.mark.slow
def test_bulk_generate_small_produces_orders() -> None:
    async def _run() -> None:
        pool = await _create_pool_or_skip()
        config = _bulk_config(day_offset=14, seed=292101)
        window_start = _window_start(config)
        try:
            await _require_seed_data(pool)
            await _cleanup_window(pool, window_start=window_start, window_end=config.end_at)

            result = await _run_bulk_with_test_patches(config, pool)
            rows = await _fetch_orders_in_window(
                pool,
                window_start=window_start,
                window_end=config.end_at,
            )

            assert int(result["orders_generated"]) == len(rows)
            assert len(rows) > 0
            assert all(
                window_start <= row["placed_at"].astimezone(LONDON_TZ) < config.end_at
                for row in rows
            )
        finally:
            try:
                await _cleanup_window(pool, window_start=window_start, window_end=config.end_at)
            finally:
                await pool.close()

    asyncio.run(_run())


@pytest.mark.slow
def test_bulk_generate_aggregate_consistency() -> None:
    async def _run() -> None:
        pool = await _create_pool_or_skip()
        config = _bulk_config(day_offset=15, seed=292102)
        window_start = _window_start(config)
        try:
            await _require_seed_data(pool)
            await _cleanup_window(pool, window_start=window_start, window_end=config.end_at)

            await _run_bulk_with_test_patches(config, pool)
            rows = await _fetch_orders_in_window(
                pool,
                window_start=window_start,
                window_end=config.end_at,
            )
            violations = await pool.fetch(
                """
                SELECT
                    o.order_id,
                    o.placed_at,
                    o.user_id,
                    o.user_total_orders_lifetime AS snapshot,
                    (
                        SELECT COUNT(*)
                        FROM orders o2
                        WHERE o2.user_id = o.user_id
                          AND o2.placed_at < o.placed_at
                    ) AS actual_prior_count
                FROM orders o
                WHERE o.placed_at >= $1
                  AND o.placed_at < $2
                  AND o.user_total_orders_lifetime <> (
                      SELECT COUNT(*)
                      FROM orders o2
                      WHERE o2.user_id = o.user_id
                        AND o2.placed_at < o.placed_at
                  )
                ORDER BY o.placed_at, o.order_id
                """,
                window_start,
                config.end_at,
            )

            assert len(rows) > 0
            assert len(violations) == 0
        finally:
            try:
                await _cleanup_window(pool, window_start=window_start, window_end=config.end_at)
            finally:
                await pool.close()

    asyncio.run(_run())


@pytest.mark.slow
def test_bulk_generate_rebulk_refused() -> None:
    async def _run() -> None:
        pool = await _create_pool_or_skip()
        config = _bulk_config(day_offset=16, seed=292103)
        window_start = _window_start(config)
        try:
            await _require_seed_data(pool)
            await _cleanup_window(pool, window_start=window_start, window_end=config.end_at)

            result = await _run_bulk_with_test_patches(config, pool)
            assert int(result["orders_generated"]) > 0

            with pytest.raises(RuntimeError, match="orders table has .* Use --force"):
                await _run_bulk_with_test_patches(config, pool)
        finally:
            try:
                await _cleanup_window(pool, window_start=window_start, window_end=config.end_at)
            finally:
                await pool.close()

    asyncio.run(_run())


@pytest.mark.slow
def test_bulk_generate_force_succeeds() -> None:
    async def _run() -> None:
        pool = await _create_pool_or_skip()
        config = _bulk_config(day_offset=17, seed=292104)
        force_config = _bulk_config(day_offset=17, seed=292104, force=True)
        window_start = _window_start(config)
        try:
            await _require_seed_data(pool)
            await _cleanup_window(pool, window_start=window_start, window_end=config.end_at)

            first = await _run_bulk_with_test_patches(config, pool)
            assert int(first["orders_generated"]) > 0

            second = await _run_bulk_with_test_patches(force_config, pool)
            assert int(second["orders_generated"]) > 0
        finally:
            try:
                await _cleanup_window(pool, window_start=window_start, window_end=config.end_at)
            finally:
                await pool.close()

    asyncio.run(_run())
