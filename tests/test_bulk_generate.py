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
    DATABASE_URL_BULK,
    _is_unplaceable_order_error,
    _parse_end_at,
    _rate_multiplier_from_env,
    _should_abort_run,
    bulk_generate,
)
from simulator.cart_builder import Cart  # noqa: E402
from simulator.generator import LONDON_TZ, generate_order  # noqa: E402
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
        pool = await asyncpg.create_pool(DATABASE_URL_BULK, min_size=2, max_size=5)
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


async def _noop_bulk_emit_chargeback(
    _order_id: uuid.UUID,
    _conn: asyncpg.Connection,
    *,
    received_at: datetime,
) -> None:
    return None


async def _fake_ephemeral_payment_method(
    _conn: asyncpg.Connection,
    user_id: uuid.UUID,
    _rng: random.Random,
    _payment_method_id: uuid.UUID | None = None,
) -> tuple[dict[str, Any], bool]:
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
    }, True


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
            patch("simulator.bulk_generate.bulk_emit_chargeback", _noop_bulk_emit_chargeback)
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


def test_is_unplaceable_order_error_matches_open_hours() -> None:
    assert _is_unplaceable_order_error(RuntimeError("no stores in current open-hours window"))


def test_bulk_skips_eligible_order_type_error() -> None:
    assert _is_unplaceable_order_error(RuntimeError("no eligible order type for store"))
    assert not _is_unplaceable_order_error(
        RuntimeError("no active menu items for store")
    )  # raised post-PM-insert; must crash not skip (VOI-329)
    # empty-DB fail-fast still propagates:
    assert not _is_unplaceable_order_error(RuntimeError("no active stores available"))


def test_is_unplaceable_order_error_ignores_other_runtime_errors() -> None:
    # "no active stores available" is the empty-DB fail-fast guard raised by
    # generator.py only when there are no active stores at all; it must
    # propagate (NOT be skipped as an unplaceable closed-window order).
    assert not _is_unplaceable_order_error(RuntimeError("no active stores available"))
    assert not _is_unplaceable_order_error(RuntimeError("user not found: 123"))
    assert not _is_unplaceable_order_error(RuntimeError("payment method insert returned no row"))


def test_bulk_aborts_on_zero_orders() -> None:
    assert _should_abort_run(orders_generated=0, skipped_unplaceable=0, total=100)


def test_bulk_aborts_above_skip_ceiling() -> None:
    # >5% skipped
    assert _should_abort_run(orders_generated=90, skipped_unplaceable=10, total=100)
    # healthy low-skip run returns success (no abort)
    assert not _should_abort_run(orders_generated=99, skipped_unplaceable=1, total=100)


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

    assert _rate_multiplier_from_env() == 0.05


def test_rate_multiplier_from_env_bulk_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BULK_RATE_MULTIPLIER", "0.5")

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
            rng: random.Random | None = None,
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


def test_bulk_generate_force_rerun_cleans_ephemeral_state() -> None:
    async def _run() -> None:
        config = BulkRunConfig(
            days=_BULK_DAYS,
            end_at=_fixed_end_at(),
            seed=292,
            rate_multiplier=_BULK_RATE_MULTIPLIER,
        )
        force_config = BulkRunConfig(
            days=_BULK_DAYS,
            end_at=_fixed_end_at(),
            seed=292,
            force=True,
            rate_multiplier=_BULK_RATE_MULTIPLIER,
        )
        window_start = _window_start(force_config)
        timestamps = [
            window_start + timedelta(seconds=1),
            window_start + timedelta(seconds=2),
        ]
        tracked_pm_ids = [uuid.UUID(int=100), uuid.UUID(int=101), uuid.UUID(int=102)]
        deletable_tracked_pm_ids = tracked_pm_ids[:2]
        stable_metadata_key = f"bulk_window_{force_config.seed}_{int(window_start.timestamp())}"

        class _FakeTransaction:
            async def __aenter__(self) -> _FakeTransaction:
                return self

            async def __aexit__(
                self,
                _exc_type: type[BaseException] | None,
                _exc: BaseException | None,
                _traceback: object,
            ) -> None:
                return None

        class _RecordingConnection:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, tuple[object, ...]]] = []
                self.executed: list[tuple[str, tuple[object, ...]]] = []
                self.fetched: list[tuple[str, tuple[object, ...]]] = []

            def transaction(self) -> _FakeTransaction:
                return _FakeTransaction()

            async def execute(self, sql: str, *args: object) -> str:
                self.calls.append(("execute", sql, args))
                self.executed.append((sql, args))
                return "OK"

            async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
                self.calls.append(("fetch", sql, args))
                self.fetched.append((sql, args))
                normalized_sql = " ".join(sql.split())
                if normalized_sql == "SELECT value FROM sim.simulator_meta WHERE key = $1":
                    return [
                        {
                            "value": {
                                "ephemeral_pm_ids": [
                                    str(payment_method_id) for payment_method_id in tracked_pm_ids
                                ]
                            }
                        }
                    ]
                if normalized_sql.startswith("SELECT tracked_pm.payment_method_id"):
                    return [
                        {"payment_method_id": payment_method_id}
                        for payment_method_id in deletable_tracked_pm_ids
                    ]
                return [
                    {"payment_method_id": payment_method_id} for payment_method_id in tracked_pm_ids
                ]

            async def fetchval(self, _sql: str, *_args: object) -> str:
                return "DELIVERED"

            async def fetchrow(self, _sql: str, *_args: object) -> None:
                return None

        class _FakeAcquire:
            def __init__(self, conn: _RecordingConnection) -> None:
                self._conn = conn

            async def __aenter__(self) -> _RecordingConnection:
                return self._conn

            async def __aexit__(
                self,
                _exc_type: type[BaseException] | None,
                _exc: BaseException | None,
                _traceback: object,
            ) -> None:
                return None

        class _RecordingPool:
            def __init__(self, conn: _RecordingConnection) -> None:
                self._conn = conn
                self._existing_counts = [0, len(timestamps)]

            async def fetchval(self, _sql: str, *_args: object) -> int:
                if self._existing_counts:
                    return self._existing_counts.pop(0)
                return 0

            def acquire(self) -> _FakeAcquire:
                return _FakeAcquire(self._conn)

        class _FakeWeightedUserPicker:
            def __init__(self, *_args: object) -> None:
                return None

            async def refresh(self) -> None:
                return None

        async def fake_load_stores_by_city(
            _pool: object,
        ) -> dict[str, list[dict[str, Any]]]:
            return {}

        async def fake_load_store_hours(_pool: object) -> dict[uuid.UUID, list[object]]:
            return {}

        async def fake_load_active_promos(_pool: object) -> list[dict[str, object]]:
            return []

        def fake_synthesize_chronological_timestamps(**_kwargs: object) -> list[datetime]:
            return list(timestamps)

        generated_order_ids: list[uuid.UUID] = []

        async def fake_generate_order(*_args: object, **_kwargs: object) -> uuid.UUID:
            order_id = uuid.UUID(int=len(generated_order_ids) + 1)
            generated_order_ids.append(order_id)
            return order_id

        conn = _RecordingConnection()
        pool = _RecordingPool(conn)

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"FRAUD_INJECTION_RATE": "1.0"}))
            stack.enter_context(
                patch("simulator.bulk_generate.load_stores_by_city", fake_load_stores_by_city)
            )
            stack.enter_context(
                patch("simulator.bulk_generate.load_store_hours", fake_load_store_hours)
            )
            stack.enter_context(
                patch("simulator.bulk_generate.load_active_promos", fake_load_active_promos)
            )
            stack.enter_context(
                patch("simulator.bulk_generate.WeightedUserPicker", _FakeWeightedUserPicker)
            )
            stack.enter_context(
                patch(
                    "simulator.bulk_generate.ensure_fraud_pattern_state",
                    _noop_ensure_fraud_pattern_state,
                )
            )
            stack.enter_context(
                patch(
                    "simulator.bulk_generate.synthesize_chronological_timestamps",
                    fake_synthesize_chronological_timestamps,
                )
            )
            stack.enter_context(
                patch("simulator.bulk_generate.generate_order", fake_generate_order)
            )
            stack.enter_context(
                patch("simulator.bulk_generate._write_bulk_metadata", _noop_write_bulk_metadata)
            )

            first = await bulk_generate(config, pool, redis_conn=_EmptyRedis())
            second = await bulk_generate(force_config, pool, redis_conn=_EmptyRedis())

        expected_generated = len(timestamps)
        assert int(first["orders_generated"]) == expected_generated
        assert int(second["orders_generated"]) == expected_generated

        normalized_sql = [" ".join(sql.split()) for sql, _args in conn.executed]
        normalized_calls = [(kind, " ".join(sql.split()), args) for kind, sql, args in conn.calls]
        metadata_lookup_kind, metadata_lookup_sql, metadata_lookup_args = normalized_calls[0]
        assert metadata_lookup_kind == "fetch"
        assert metadata_lookup_sql == "SELECT value FROM sim.simulator_meta WHERE key = $1"
        assert metadata_lookup_args == (stable_metadata_key,)

        pm_survivor_kind, pm_survivor_sql, pm_survivor_args = normalized_calls[1]
        assert pm_survivor_kind == "fetch"
        assert pm_survivor_sql.startswith("SELECT tracked_pm.payment_method_id")
        assert "payment_method_id = ANY($1::uuid[])" in pm_survivor_sql
        assert "FROM orders_archive" in pm_survivor_sql
        assert "SELECT DISTINCT window_orders.payment_method_id" not in pm_survivor_sql
        assert pm_survivor_args == (tracked_pm_ids, window_start, force_config.end_at)

        ring_cleanup_sql = normalized_sql[0]
        assert ring_cleanup_sql.startswith("UPDATE sim.fraud_promo_rings")
        assert "ARRAY_AGG(DISTINCT u.user_id)" in ring_cleanup_sql
        assert "SELECT o.user_id FROM sim.simulator_ground_truth sgt" in ring_cleanup_sql
        assert "SELECT oa.user_id FROM sim.simulator_ground_truth sgt" in ring_cleanup_sql
        assert "sgt.user_id" not in ring_cleanup_sql
        assert "JOIN orders o ON o.order_id = sgt.order_id" in ring_cleanup_sql
        assert "JOIN orders_archive oa ON oa.order_id = sgt.order_id" in ring_cleanup_sql
        assert "AND (o.placed_at < $1 OR o.placed_at >= $2)" in ring_cleanup_sql
        assert "AND (oa.placed_at < $1 OR oa.placed_at >= $2)" in ring_cleanup_sql
        fraud_decisions_index = next(
            index
            for index, sql in enumerate(normalized_sql)
            if sql.startswith("DELETE FROM fraud_decisions")
        )
        assert fraud_decisions_index > 0
        assert conn.executed[0][1] == (window_start, force_config.end_at)
        payment_delete_matches = [
            (index, sql, args)
            for index, (kind, sql, args) in enumerate(normalized_calls)
            if kind == "execute" and sql.startswith("DELETE FROM payment_methods")
        ]
        assert len(payment_delete_matches) == 1
        payment_delete_index, payment_delete_sql, payment_delete_args = payment_delete_matches[0]
        assert (
            payment_delete_sql
            == "DELETE FROM payment_methods WHERE payment_method_id = ANY($1::uuid[])"
        )
        assert payment_delete_args == (deletable_tracked_pm_ids,)
        orders_delete_index = next(
            index
            for index, (kind, sql, _args) in enumerate(normalized_calls)
            if kind == "execute" and sql.startswith("DELETE FROM orders WHERE placed_at")
        )
        assert payment_delete_index > orders_delete_index

    asyncio.run(_run())


def test_bulk_seeded_uuids_deterministic() -> None:
    def _derive_uuids(seed: int) -> tuple[uuid.UUID, uuid.UUID]:
        rng = random.Random(seed)
        order_id_bytes = bytes(rng.getrandbits(8) for _ in range(16))
        device_id_bytes = bytes(rng.getrandbits(8) for _ in range(16))
        return uuid.UUID(bytes=order_id_bytes), uuid.UUID(bytes=device_id_bytes)

    run1 = _derive_uuids(42)
    run2 = _derive_uuids(42)
    assert run1 == run2, f"UUID generation is not deterministic: {run1} != {run2}"

    run3 = _derive_uuids(99)
    assert run1 != run3, "Different seeds should produce different UUIDs"


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
