from __future__ import annotations

import asyncio
import datetime
import logging
import math
import os
import random
import sys
import uuid
from typing import Any

import asyncpg  # type: ignore[import]

LOGGER: logging.Logger = logging.getLogger(__name__)
DATABASE_URL: str | None = os.environ.get("DATABASE_URL")

_CHARGEBACK_CANDIDATES_SQL = """
SELECT
  o.order_id,
  o.placed_at AS order_placed_at,
  o.delivered_at,
  o.total_pence,
  gt.is_fraud,
  gt.fraud_category
FROM orders o
JOIN simulator_ground_truth gt USING (order_id)
WHERE o.delivered_at IS NOT NULL
  AND o.delivered_at >= NOW() - INTERVAL '90 days'
  AND o.chargeback_received_at IS NULL
  AND o.fraud_outcome IS NULL
UNION ALL
SELECT
  o.order_id,
  o.placed_at AS order_placed_at,
  o.delivered_at,
  o.total_pence,
  gt.is_fraud,
  gt.fraud_category
FROM orders_archive o
JOIN simulator_ground_truth gt USING (order_id)
WHERE o.delivered_at >= NOW() - INTERVAL '90 days'
  AND o.chargeback_received_at IS NULL
  AND o.fraud_outcome IS NULL
LIMIT 10000
"""

_CHARGEBACK_INSERT_SQL = """
INSERT INTO chargebacks (order_id, order_placed_at, reason_code, reason_category, amount_pence, received_at)
VALUES ($1, $2, 'CB001', $3, $4, NOW())
ON CONFLICT DO NOTHING
"""

_CHARGEBACK_ORDERS_UPDATE_SQL = """
UPDATE orders
SET chargeback_received_at = NOW(),
    chargeback_amount_pence = $3,
    fraud_outcome = $4
WHERE order_id = $1
  AND placed_at = $2
"""

_CHARGEBACK_ARCHIVE_UPDATE_SQL = """
UPDATE orders_archive
SET chargeback_received_at = NOW(),
    chargeback_amount_pence = $3,
    fraud_outcome = $4
WHERE order_id = $1
  AND placed_at = $2
"""

_REFUND_CANDIDATES_SQL = """
SELECT
  o.order_id,
  o.placed_at AS order_placed_at,
  o.delivered_at,
  o.total_pence
FROM orders o
JOIN simulator_ground_truth gt USING (order_id)
WHERE o.delivered_at IS NOT NULL
  AND gt.fraud_category = 'refund_abuse'
UNION ALL
SELECT
  o.order_id,
  o.placed_at AS order_placed_at,
  o.delivered_at,
  o.total_pence
FROM orders_archive o
JOIN simulator_ground_truth gt USING (order_id)
WHERE o.delivered_at IS NOT NULL
  AND gt.fraud_category = 'refund_abuse'
LIMIT 10000
"""

_REFUND_INSERT_SQL = """
INSERT INTO refunds (order_id, order_placed_at, amount_pence, reason, initiated_by, issued_at)
VALUES ($1, $2, $3, 'order_quality_complaint', 'USER', NOW())
ON CONFLICT (order_id) DO NOTHING
"""

_FINALIZE_ORDERS_SQL = """
UPDATE orders
SET fraud_outcome = 'LEGIT'
WHERE delivered_at < NOW() - INTERVAL '60 days'
  AND fraud_outcome IS NULL
  AND delivered_at IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = orders.order_id)
"""

_FINALIZE_ARCHIVE_SQL = """
UPDATE orders_archive
SET fraud_outcome = 'LEGIT'
WHERE delivered_at < NOW() - INTERVAL '60 days'
  AND fraud_outcome IS NULL
  AND delivered_at IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = orders_archive.order_id)
"""

_FINALIZE_REFUND_ABUSE_ORDERS_SQL = """
UPDATE orders
SET fraud_outcome = 'REFUND_ABUSE'
WHERE delivered_at < NOW() - INTERVAL '60 days'
  AND fraud_outcome IS NULL
  AND delivered_at IS NOT NULL
  AND EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = orders.order_id)
"""

_FINALIZE_REFUND_ABUSE_ARCHIVE_SQL = """
UPDATE orders_archive
SET fraud_outcome = 'REFUND_ABUSE'
WHERE delivered_at < NOW() - INTERVAL '60 days'
  AND fraud_outcome IS NULL
  AND delivered_at IS NOT NULL
  AND EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = orders_archive.order_id)
"""


def _coerce_order_id(raw_order_id: object) -> uuid.UUID:
    if isinstance(raw_order_id, uuid.UUID):
        return raw_order_id
    return uuid.UUID(str(raw_order_id))


def _chargeback_probability(is_fraud: bool, fraud_category: str | None) -> float:
    if not is_fraud:
        return 0.002

    if fraud_category in {
        "stolen_card",
        "account_takeover",
        "triangulation",
        "collusive_merchant",
    }:
        return 0.60
    if fraud_category == "refund_abuse":
        return 0.30
    if fraud_category == "promo_abuse":
        return 0.05
    if fraud_category == "reseller":
        return 0.10
    return 0.0


def _days_to_chargeback_threshold(rng: random.Random, is_fraud: bool) -> float:
    if is_fraud:
        return rng.lognormvariate(math.log(14), 0.7)
    return rng.lognormvariate(math.log(30), 0.8)


async def generate_chargebacks(pool: Any) -> None:
    async with pool.acquire() as conn:
        candidate_rows = await conn.fetch(_CHARGEBACK_CANDIDATES_SQL)
        now = datetime.datetime.now(datetime.timezone.utc)

        for row in candidate_rows:
            order_id = _coerce_order_id(row["order_id"])
            order_placed_at = row["order_placed_at"]
            delivered_at = row["delivered_at"]
            total_pence = int(row["total_pence"])
            is_fraud = bool(row["is_fraud"])
            fraud_category = row["fraud_category"]

            rng = random.Random(int(order_id.bytes[:8].hex(), 16))
            days_to_chargeback = _days_to_chargeback_threshold(rng, is_fraud)
            delivered_age_days = (now - delivered_at).total_seconds() / 86400
            chargeback_probability = _chargeback_probability(is_fraud, fraud_category)
            should_chargeback_now = (
                delivered_age_days >= days_to_chargeback
            ) and (rng.random() < chargeback_probability)

            if not should_chargeback_now:
                continue

            reason_category = "FRAUD" if is_fraud else "OTHER"
            fraud_outcome = "CHARGEBACK" if is_fraud else "LEGIT"

            await conn.execute(
                _CHARGEBACK_INSERT_SQL,
                order_id,
                order_placed_at,
                reason_category,
                total_pence,
            )
            await conn.execute(
                _CHARGEBACK_ORDERS_UPDATE_SQL,
                order_id,
                order_placed_at,
                total_pence,
                fraud_outcome,
            )
            await conn.execute(
                _CHARGEBACK_ARCHIVE_UPDATE_SQL,
                order_id,
                order_placed_at,
                total_pence,
                fraud_outcome,
            )


async def generate_refunds(pool: Any) -> None:
    async with pool.acquire() as conn:
        candidate_rows = await conn.fetch(_REFUND_CANDIDATES_SQL)
        now = datetime.datetime.now(datetime.timezone.utc)

        for row in candidate_rows:
            order_id = _coerce_order_id(row["order_id"])
            order_placed_at = row["order_placed_at"]
            delivered_at = row["delivered_at"]
            total_pence = int(row["total_pence"])

            delivered_age_hours = (now - delivered_at).total_seconds() / 3600
            if not (0 <= delivered_age_hours <= 120):
                continue

            await conn.execute(_REFUND_INSERT_SQL, order_id, order_placed_at, total_pence)


async def finalize_stale_labels(pool: Any) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_FINALIZE_ORDERS_SQL)
        await conn.execute(_FINALIZE_ARCHIVE_SQL)
        await conn.execute(_FINALIZE_REFUND_ABUSE_ORDERS_SQL)
        await conn.execute(_FINALIZE_REFUND_ABUSE_ARCHIVE_SQL)


def _get_database_url() -> str:
    if DATABASE_URL is None:
        raise RuntimeError("Missing DATABASE_URL environment variable")
    return DATABASE_URL


async def run_once() -> None:
    pool = await asyncpg.create_pool(_get_database_url(), min_size=2, max_size=5)
    try:
        await generate_chargebacks(pool)
        await generate_refunds(pool)
        await finalize_stale_labels(pool)
    finally:
        await pool.close()


async def run_daemon() -> None:
    interval_min = int(os.environ.get("CHARGEBACK_DAEMON_INTERVAL_MIN", "60"))
    while True:
        try:
            await run_once()
        except Exception:
            LOGGER.exception("chargeback_daemon_iteration_failed")
        await asyncio.sleep(interval_min * 60)


if __name__ == "__main__":
    if "--once" in sys.argv:
        asyncio.run(run_once())
    else:
        asyncio.run(run_daemon())
