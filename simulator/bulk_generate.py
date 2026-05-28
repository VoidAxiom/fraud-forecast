"""Async bulk order generator for simulator backfills.

Stop the feature_aggregator container before running bulk generation. The
generator still emits NOTIFY messages, but with no listener they are discarded;
Redis can be rebuilt from Postgres in a later packet before scoring eval.
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

from simulator.chargebacks import maybe_emit_chargeback
from simulator.fraud_patterns.collusive_merchant import init_collusive_stores
from simulator.fraud_patterns.promo_abuse import init_rings_from_db as init_promo_abuse_rings
from simulator.fraud_patterns.reseller import init_reseller_accounts
from simulator.fraud_patterns.triangulation import init_accounts as init_triangulation_accounts
from simulator.generator import (
    DATABASE_URL_SIMULATOR,
    LONDON_TZ,
    REDIS_URL,
    generate_order,
    load_active_promos,
    load_store_hours,
    load_stores_by_city,
)
from simulator.lifecycle import advance_lifecycle
from simulator.timestamps import synthesize_chronological_timestamps
from simulator.user_picker import WeightedUserPicker

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BulkRunConfig:
    days: float
    end_at: datetime
    seed: int
    force: bool = False
    rate_multiplier: float = 1.0

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
        SELECT COUNT(*)
        FROM orders
        WHERE placed_at >= $1
          AND placed_at <= $2
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
    await pool.execute(
        """
        INSERT INTO sim.simulator_meta (key, value)
        VALUES ($1, $2::jsonb)
        ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value,
            updated_at = NOW()
        """,
        f"bulk_run_{run_id}",
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

    rng = random.Random(config.seed)
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
        await ensure_fraud_pattern_state(pool, rng, stores_by_city)

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
        for ts in timestamps:
            order_rng = random.Random(rng.randint(0, 2**63 - 1))
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
                )
                await advance_lifecycle(order_id, conn, now=window_end)
                await maybe_emit_chargeback(order_id, conn, now=window_end)

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
        }
        await _write_bulk_metadata(pool, run_id=run_id, value=metadata)

        result: dict[str, Any] = {
            "run_id": run_id,
            "orders_generated": processed,
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
    for env_name in ("BULK_RATE_MULTIPLIER", "LIVE_RATE_MULTIPLIER"):
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        try:
            return float(raw)
        except ValueError:
            logger.warning("invalid_rate_multiplier env=%s raw=%r", env_name, raw)
    return 1.0


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
        DATABASE_URL_SIMULATOR,
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
