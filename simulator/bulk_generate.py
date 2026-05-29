"""Async bulk order generator for simulator backfills.

Stop the feature_aggregator container before running bulk generation. The
generator still emits NOTIFY messages, but with no listener they are discarded;
Redis can be rebuilt from Postgres in a later packet before scoring eval.

Bulk generation uses the app role via DATABASE_URL_BULK -> DATABASE_URL rather
than simulator_user because it needs DELETE on sim.simulator_ground_truth for
the --force path and SELECT on sim.simulator_ground_truth when chargebacks are
emitted.

KNOWN LIMITATION (v1, tracked in VOI-324): bulk-generated orders use the
current state of promos and aggregate windows (NOW()-relative), not the
historical state at each order's placed_at. For Phase 5 v1 evaluation
this is acceptable since promos are mostly static seed data; for
publication-quality training data, VOI-324 fixes this.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import asyncpg  # type: ignore[import]  # asyncpg 0.28 lacks type stubs in the tool env.
import redis.asyncio as aioredis

from simulator.chargebacks import (
    bulk_chargeback_received_at,
    bulk_emit_chargeback,
    bulk_refund_issued_at,
)
from simulator.fraud_patterns.collusive_merchant import init_collusive_stores
from simulator.fraud_patterns.promo_abuse import init_rings_from_db as init_promo_abuse_rings
from simulator.fraud_patterns.reseller import init_reseller_accounts
from simulator.fraud_patterns.triangulation import init_accounts as init_triangulation_accounts
from simulator.generator import (
    LONDON_TZ,
    REDIS_URL,
    generate_order,
    load_active_promos,
    load_store_hours,
    load_stores_by_city,
)
from simulator.lifecycle import _TERMINAL_STATES, advance_lifecycle
from simulator.timestamps import synthesize_chronological_timestamps
from simulator.user_picker import WeightedUserPicker

logger = logging.getLogger(__name__)

_UNPLACEABLE_ORDER_MESSAGES: tuple[str, ...] = (
    "no stores in current open-hours window",
    "no eligible order type for store",
)


def _is_unplaceable_order_error(exc: RuntimeError) -> bool:
    message = str(exc)
    return any(fragment in message for fragment in _UNPLACEABLE_ORDER_MESSAGES)


def _should_abort_run(orders_generated: int, skipped_unplaceable: int, total: int) -> bool:
    """True when a run looks systematically broken: produced zero orders,
    or skipped more than 5% of attempted timestamps."""
    if orders_generated == 0:
        return True
    return skipped_unplaceable > 0.05 * total


DATABASE_URL_BULK: str = os.environ.get(
    "DATABASE_URL_BULK",
    os.environ.get(
        "DATABASE_URL",
        "postgresql://app:app_dev_password@postgres:5432/fraud_platform",
    ),
)

_BULK_LIFECYCLE_STEP_SECONDS = 3600


@dataclass(frozen=True)
class BulkRunConfig:
    days: float
    end_at: datetime
    seed: int
    force: bool = False
    rate_multiplier: float = 0.05

    def __post_init__(self) -> None:
        if self.days <= 0.0:
            raise ValueError("days must be greater than 0")
        if self.rate_multiplier <= 0.0:
            raise ValueError("rate_multiplier must be greater than 0")


def _as_london_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=LONDON_TZ)
    return value.astimezone(LONDON_TZ)


def _store_id_pool(stores_by_city: dict[str, list[dict[str, Any]]]) -> list[uuid.UUID]:
    store_ids: list[uuid.UUID] = []
    for stores in stores_by_city.values():
        for store in stores:
            raw_store_id = store.get("store_id")
            if isinstance(raw_store_id, uuid.UUID):
                store_ids.append(raw_store_id)
            elif raw_store_id is not None:
                store_ids.append(uuid.UUID(str(raw_store_id)))
    return sorted(store_ids, key=str)


async def ensure_fraud_pattern_state(
    pool: asyncpg.Pool,
    rng: random.Random,
    stores_by_city: dict[str, list[dict[str, Any]]],
) -> None:
    """Initialize fraud-pattern backing state the same way live generation does."""
    store_ids = _store_id_pool(stores_by_city)

    async with pool.acquire() as collusive_conn:
        await init_collusive_stores(
            rng,
            collusive_conn,
            store_pool=store_ids,
            n=10,
        )
    async with pool.acquire() as promo_conn:
        await init_promo_abuse_rings(rng, promo_conn)
    async with pool.acquire() as reseller_conn:
        await init_reseller_accounts(
            rng,
            reseller_conn,
            store_id_pool=store_ids,
        )
    async with pool.acquire() as tri_conn:
        await init_triangulation_accounts(rng, tri_conn)


async def _existing_order_count(
    pool: asyncpg.Pool,
    *,
    window_start: datetime,
    window_end: datetime,
) -> int:
    count_raw = await pool.fetchval(
        """
        SELECT (
            SELECT COUNT(*)
            FROM orders
            WHERE placed_at >= $1
              AND placed_at < $2
        ) + (
            SELECT COUNT(*)
            FROM orders_archive
            WHERE placed_at >= $1
              AND placed_at < $2
        )
        """,
        window_start,
        window_end,
    )
    return int(count_raw or 0)


async def _write_bulk_metadata(
    pool: asyncpg.Pool,
    *,
    run_id: str,
    value: dict[str, Any],
) -> None:
    metadata_key = f"bulk_{run_id}" if run_id.startswith("window_") else f"bulk_run_{run_id}"
    await pool.execute(
        """
        INSERT INTO sim.simulator_meta (key, value)
        VALUES ($1, $2::jsonb)
        ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value,
            updated_at = NOW()
        """,
        metadata_key,
        json.dumps(value),
    )


async def bulk_generate(
    config: BulkRunConfig,
    pool: asyncpg.Pool,
    redis_conn: Optional[aioredis.Redis[Any]] = None,  # noqa: UP045
) -> dict[str, Any]:
    """Generate historical orders in chronological order for the configured window."""
    window_end = _as_london_aware(config.end_at)
    window_start = window_end - timedelta(days=config.days)
    window_start_epoch = int(window_start.timestamp())
    bulk_window_key = f"bulk_window_{config.seed}_{window_start_epoch}"
    existing_count = await _existing_order_count(
        pool,
        window_start=window_start,
        window_end=window_end,
    )
    if existing_count > 0 and not config.force:
        raise RuntimeError(
            f"orders table has {existing_count} rows in target window "
            f"[{window_start}, {window_end}]. Use --force to overwrite."
        )

    if config.force:
        async with pool.acquire() as conn, conn.transaction():
            deletable_tracked_pm_ids: list[uuid.UUID] = []
            metadata_rows = await conn.fetch(
                """
                SELECT value
                FROM sim.simulator_meta
                WHERE key = $1
                """,
                bulk_window_key,
            )
            if not metadata_rows:
                logger.warning("bulk_force_metadata_missing key=%s", bulk_window_key)
            else:
                metadata_value = metadata_rows[0]["value"]
                prior_metadata: dict[str, Any] = {}
                if isinstance(metadata_value, str):
                    parsed_metadata = json.loads(metadata_value)
                    if isinstance(parsed_metadata, dict):
                        prior_metadata = parsed_metadata
                elif isinstance(metadata_value, dict):
                    prior_metadata = metadata_value

                tracked_ephemeral_pm_ids: list[uuid.UUID] = []
                raw_ephemeral_pm_ids = prior_metadata.get("ephemeral_pm_ids")
                if isinstance(raw_ephemeral_pm_ids, list):
                    tracked_ephemeral_pm_ids = sorted(
                        uuid.UUID(str(payment_method_id))
                        for payment_method_id in raw_ephemeral_pm_ids
                    )

                if tracked_ephemeral_pm_ids:
                    deletable_tracked_pm_rows = await conn.fetch(
                        """
                        SELECT tracked_pm.payment_method_id
                        FROM UNNEST($1::uuid[]) AS tracked_pm(payment_method_id)
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM orders
                            WHERE payment_method_id = ANY($1::uuid[])
                              AND payment_method_id = tracked_pm.payment_method_id
                              AND (placed_at < $2 OR placed_at >= $3)
                            UNION ALL
                            SELECT 1
                            FROM orders_archive
                            WHERE payment_method_id = ANY($1::uuid[])
                              AND payment_method_id = tracked_pm.payment_method_id
                              AND (placed_at < $2 OR placed_at >= $3)
                        )
                        """,
                        tracked_ephemeral_pm_ids,
                        window_start,
                        window_end,
                    )
                    for row in deletable_tracked_pm_rows:
                        payment_method_id = row["payment_method_id"]
                        if isinstance(payment_method_id, uuid.UUID):
                            deletable_tracked_pm_ids.append(payment_method_id)
                        else:
                            deletable_tracked_pm_ids.append(uuid.UUID(str(payment_method_id)))

            await conn.execute(
                """
                UPDATE sim.fraud_promo_rings r
                SET created_user_ids = COALESCE(
                    (
                        SELECT ARRAY_AGG(DISTINCT u.user_id)
                        FROM (
                            SELECT o.user_id
                            FROM sim.simulator_ground_truth sgt
                            JOIN orders o ON o.order_id = sgt.order_id
                            WHERE sgt.fraud_category = 'promo_abuse'
                              AND sgt.ring_id = r.ring_id
                              AND (o.placed_at < $1 OR o.placed_at >= $2)
                            UNION
                            SELECT oa.user_id
                            FROM sim.simulator_ground_truth sgt
                            JOIN orders_archive oa ON oa.order_id = sgt.order_id
                            WHERE sgt.fraud_category = 'promo_abuse'
                              AND sgt.ring_id = r.ring_id
                              AND (oa.placed_at < $1 OR oa.placed_at >= $2)
                        ) u
                    ),
                    ARRAY[]::uuid[]
                )
                """,
                window_start,
                window_end,
            )
            await conn.execute(
                """
                DELETE FROM chargebacks
                WHERE order_id IN (
                    SELECT order_id
                    FROM orders
                    WHERE placed_at >= $1
                      AND placed_at < $2
                    UNION ALL
                    SELECT order_id
                    FROM orders_archive
                    WHERE placed_at >= $1
                      AND placed_at < $2
                )
                """,
                window_start,
                window_end,
            )
            await conn.execute(
                """
                DELETE FROM sim.simulator_ground_truth
                WHERE order_id IN (
                    SELECT order_id
                    FROM orders
                    WHERE placed_at >= $1
                      AND placed_at < $2
                    UNION ALL
                    SELECT order_id
                    FROM orders_archive
                    WHERE placed_at >= $1
                      AND placed_at < $2
                )
                """,
                window_start,
                window_end,
            )
            await conn.execute(
                """
                DELETE FROM order_events
                WHERE order_id IN (
                    SELECT order_id
                    FROM orders
                    WHERE placed_at >= $1
                      AND placed_at < $2
                    UNION ALL
                    SELECT order_id
                    FROM orders_archive
                    WHERE placed_at >= $1
                      AND placed_at < $2
                )
                """,
                window_start,
                window_end,
            )
            await conn.execute(
                """
                DELETE FROM order_items
                WHERE order_id IN (
                    SELECT order_id
                    FROM orders
                    WHERE placed_at >= $1
                      AND placed_at < $2
                    UNION ALL
                    SELECT order_id
                    FROM orders_archive
                    WHERE placed_at >= $1
                      AND placed_at < $2
                )
                """,
                window_start,
                window_end,
            )
            await conn.execute(
                """
                DELETE FROM order_items_archive
                WHERE order_id IN (
                    SELECT order_id
                    FROM orders
                    WHERE placed_at >= $1
                      AND placed_at < $2
                    UNION ALL
                    SELECT order_id
                    FROM orders_archive
                    WHERE placed_at >= $1
                      AND placed_at < $2
                )
                """,
                window_start,
                window_end,
            )
            await conn.execute(
                """
                DELETE FROM order_events_archive
                WHERE order_id IN (
                    SELECT order_id
                    FROM orders
                    WHERE placed_at >= $1
                      AND placed_at < $2
                    UNION ALL
                    SELECT order_id
                    FROM orders_archive
                    WHERE placed_at >= $1
                      AND placed_at < $2
                )
                """,
                window_start,
                window_end,
            )
            await conn.execute(
                """
                DELETE FROM refunds
                WHERE order_id IN (
                    SELECT order_id
                    FROM orders
                    WHERE placed_at >= $1
                      AND placed_at < $2
                    UNION ALL
                    SELECT order_id
                    FROM orders_archive
                    WHERE placed_at >= $1
                      AND placed_at < $2
                )
                """,
                window_start,
                window_end,
            )
            await conn.execute(
                """
                DELETE FROM fraud_decisions
                WHERE order_id IN (
                    SELECT order_id
                    FROM orders
                    WHERE placed_at >= $1
                      AND placed_at < $2
                    UNION ALL
                    SELECT order_id
                    FROM orders_archive
                    WHERE placed_at >= $1
                      AND placed_at < $2
                )
                """,
                window_start,
                window_end,
            )
            await conn.execute(
                """
                DELETE FROM orders
                WHERE placed_at >= $1
                  AND placed_at < $2
                """,
                window_start,
                window_end,
            )
            await conn.execute(
                """
                DELETE FROM orders_archive
                WHERE placed_at >= $1
                  AND placed_at < $2
                """,
                window_start,
                window_end,
            )
            if deletable_tracked_pm_ids:
                await conn.execute(
                    "DELETE FROM payment_methods WHERE payment_method_id = ANY($1::uuid[])",
                    deletable_tracked_pm_ids,
                )

    rng = random.Random(config.seed)
    # Mix window start into seed so runs over different windows with
    # the same seed don't collide on sim.simulator_ground_truth.order_id.
    _window_seed = int(window_start.timestamp()) & 0x7FFFFFFFFFFFFFFF
    rng.seed(config.seed ^ _window_seed)
    # Use a separate RNG for fraud-state bootstrap so its presence/absence
    # does not shift bulk generation draws (fixes PRRT_kwDOSmPsa86Fdm5d).
    bootstrap_rng = random.Random(config.seed ^ 0xC0DE_B007)
    run_id = f"bulk_{datetime.now(tz=LONDON_TZ).strftime('%Y%m%d_%H%M%S')}_{config.seed}"

    resolved_redis: aioredis.Redis[Any]
    owns_redis = False
    if redis_conn is None:
        resolved_redis = aioredis.from_url(REDIS_URL)
        owns_redis = True
    else:
        resolved_redis = redis_conn

    try:
        stores_by_city = await load_stores_by_city(pool)
        store_hours_by_store_id = await load_store_hours(pool)
        promos = await load_active_promos(pool)

        user_picker = WeightedUserPicker(pool, resolved_redis)
        await user_picker.refresh()
        await ensure_fraud_pattern_state(pool, bootstrap_rng, stores_by_city)

        timestamps = synthesize_chronological_timestamps(
            window_start=window_start,
            window_end=window_end,
            rate_multiplier=config.rate_multiplier,
            rng=rng,
        )

        started_at = datetime.now(tz=LONDON_TZ)
        started_monotonic = time.perf_counter()
        processed = 0
        total = len(timestamps)
        bulk_pm_ids: set[uuid.UUID] = set()
        skipped_unplaceable = 0
        for ts in timestamps:
            order_rng = random.Random(rng.randint(0, 2**63 - 1))
            bulk_ephemeral_pm_id = uuid.UUID(
                bytes=bytes(order_rng.getrandbits(8) for _ in range(16))
            )
            order_id_bytes = bytes(rng.getrandbits(8) for _ in range(16))
            bulk_order_id = uuid.UUID(bytes=order_id_bytes)
            try:
                async with pool.acquire() as conn:
                    order_id = await generate_order(
                        order_rng,
                        conn,
                        now=ts,
                        user_picker=user_picker,
                        stores_by_city=stores_by_city,
                        store_hours_by_store_id=store_hours_by_store_id,
                        promos=promos,
                        scoring_enabled=False,
                        order_id_override=bulk_order_id,
                        ephemeral_pm_id_override=bulk_ephemeral_pm_id,
                        bulk_pm_tracker=bulk_pm_ids,
                    )
                    for lifecycle_iter in range(20):
                        lifecycle_now = min(
                            ts
                            + timedelta(
                                seconds=(lifecycle_iter + 1) * _BULK_LIFECYCLE_STEP_SECONDS
                            ),
                            window_end,
                        )
                        status_before = await conn.fetchval(
                            "SELECT order_status FROM orders WHERE order_id = $1",
                            order_id,
                        )
                        if status_before in _TERMINAL_STATES:
                            break
                        await advance_lifecycle(order_id, conn, now=lifecycle_now)
                    order_row = await conn.fetchrow(
                        """
                        SELECT o.delivered_at, gt.is_fraud, gt.fraud_category
                        FROM (
                            SELECT delivered_at FROM orders WHERE order_id = $1
                            UNION ALL
                            SELECT delivered_at FROM orders_archive WHERE order_id = $1
                        ) o,
                        sim.simulator_ground_truth gt
                        WHERE gt.order_id = $1
                        LIMIT 1
                        """,
                        order_id,
                    )
                    if order_row is not None and order_row["delivered_at"] is not None:
                        cb_time = bulk_chargeback_received_at(
                            order_id=order_id,
                            delivered_at=order_row["delivered_at"],
                            is_fraud=bool(order_row["is_fraud"]),
                            fraud_category=(
                                str(order_row["fraud_category"])
                                if order_row["fraud_category"]
                                else None
                            ),
                            window_end=window_end,
                        )
                        if cb_time is not None:
                            await bulk_emit_chargeback(order_id, conn, received_at=cb_time)
                    if (
                        order_row is not None
                        and order_row["delivered_at"] is not None
                        and str(order_row["fraud_category"]) == "refund_abuse"
                    ):
                        refund_time = bulk_refund_issued_at(
                            order_id=order_id,
                            delivered_at=order_row["delivered_at"],
                            window_end=window_end,
                        )
                        if refund_time is not None:
                            await conn.execute(
                                """
INSERT INTO refunds (order_id, order_placed_at, amount_pence, reason, initiated_by, issued_at)
SELECT $1, o.placed_at, o.total_pence, 'order_quality_complaint', 'USER', $2
FROM (
    SELECT placed_at, total_pence FROM orders WHERE order_id = $1
    UNION ALL
    SELECT placed_at, total_pence FROM orders_archive WHERE order_id = $1
) o
ON CONFLICT (order_id) DO NOTHING
""",
                                order_id,
                                refund_time,
                            )
            except RuntimeError as exc:
                if not _is_unplaceable_order_error(exc):
                    raise
                skipped_unplaceable += 1
                logger.info(
                    json.dumps(
                        {
                            "event": "bulk_skip_unplaceable",
                            "reason": str(exc),
                            "skipped_unplaceable": skipped_unplaceable,
                        }
                    )
                )
                continue

            processed += 1
            if processed % 1000 == 0:
                logger.info(
                    json.dumps(
                        {
                            "event": "bulk_progress",
                            "processed": processed,
                            "total": total,
                        }
                    )
                )

        completed_at = datetime.now(tz=LONDON_TZ)
        elapsed_seconds = time.perf_counter() - started_monotonic
        metadata: dict[str, Any] = {
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "seed": config.seed,
            "rate_multiplier": config.rate_multiplier,
            "orders_generated": processed,
            "timestamps_synthesized": total,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "ephemeral_pm_ids": [str(pm_id) for pm_id in sorted(bulk_pm_ids)],
            "skipped_unplaceable": skipped_unplaceable,
        }
        await _write_bulk_metadata(pool, run_id=run_id, value=metadata)
        await _write_bulk_metadata(
            pool,
            run_id=f"window_{config.seed}_{window_start_epoch}",
            value=metadata,
        )

        if _should_abort_run(processed, skipped_unplaceable, total):
            raise RuntimeError(
                f"bulk run aborted: orders_generated={processed} "
                f"skipped_unplaceable={skipped_unplaceable} of total={total} "
                f"(exceeds 5% skip ceiling or produced zero orders)"
            )

        result: dict[str, Any] = {
            "run_id": run_id,
            "orders_generated": processed,
            "skipped_unplaceable": skipped_unplaceable,
            "timestamps_synthesized": total,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "seed": config.seed,
            "rate_multiplier": config.rate_multiplier,
            "elapsed_seconds": round(elapsed_seconds, 3),
        }
        return result
    finally:
        if owns_redis:
            await resolved_redis.close()


def _parse_end_at(raw: Optional[str]) -> datetime:  # noqa: UP045
    if raw is None:
        return datetime.now(tz=LONDON_TZ)

    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return _as_london_aware(parsed)


def _rate_multiplier_from_env() -> float:
    env_name = "BULK_RATE_MULTIPLIER"
    raw = os.environ.get(env_name)
    if raw is None:
        return 0.05
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid_rate_multiplier env=%s raw=%r", env_name, raw)
    return 0.05


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic simulator bulk generation.")
    parser.add_argument("--days", type=float, required=True)
    parser.add_argument("--end-at", default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--rate-multiplier", type=float, default=None)
    return parser


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    args = _build_parser().parse_args()
    rate_multiplier = (
        _rate_multiplier_from_env() if args.rate_multiplier is None else float(args.rate_multiplier)
    )
    config = BulkRunConfig(
        days=float(args.days),
        end_at=_parse_end_at(args.end_at if isinstance(args.end_at, str) else None),
        seed=int(args.seed),
        force=bool(args.force),
        rate_multiplier=rate_multiplier,
    )

    pool: asyncpg.Pool = await asyncpg.create_pool(
        DATABASE_URL_BULK,
        min_size=2,
        max_size=10,
    )
    redis_conn: aioredis.Redis[Any] = aioredis.from_url(REDIS_URL)
    try:
        result = await bulk_generate(config, pool, redis_conn)
        print(json.dumps(result, sort_keys=True))
    finally:
        await redis_conn.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
