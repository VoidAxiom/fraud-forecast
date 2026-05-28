"""Collusive-merchant fraud pattern for Phase 3 simulator behavior.

Spec: spec/PHASE_3.md § "Pattern 5: Collusive Merchant"

A small set of merchants are in on a card-cycling scheme. Stolen cards are run
through legitimate-looking orders at these merchants; the merchant gets paid;
the merchant passes kickback to the fraudster.
"""

from __future__ import annotations

import math
import random
from typing import Any
from uuid import UUID

import asyncpg

from simulator.fraud_patterns import GroundTruth, register
from simulator.fraud_patterns.stolen_card import FraudPatternContext, _weighted_choice

# Module-level set of collusive store IDs — populated at simulator startup
# via init_collusive_stores(). Until initialized, the generator raises.
COLLUSIVE_STORES: set[UUID] = set()


async def _fetch_collusive_store_ids(conn: asyncpg.Connection) -> set[UUID]:
    rows = await conn.fetch(
        """
        SELECT store_id
        FROM sim.fraud_collusive_stores
        ORDER BY store_id
        """
    )

    store_ids: set[UUID] = set()
    for row in rows:
        raw_store_id: Any = row["store_id"]
        if isinstance(raw_store_id, UUID):
            store_ids.add(raw_store_id)
        else:
            store_ids.add(UUID(str(raw_store_id)))
    return store_ids


async def init_collusive_stores(
    rng: random.Random,
    conn: asyncpg.Connection,
    store_pool: list[UUID],
    n: int = 10,
) -> None:
    """Initialize the collusive store pool from persistent simulator state."""
    existing_ids = await _fetch_collusive_store_ids(conn)

    if len(existing_ids) >= n:
        COLLUSIVE_STORES.clear()
        COLLUSIVE_STORES.update(existing_ids)
        return

    needed = n - len(existing_ids)
    candidates = sorted(
        {store_id for store_id in store_pool if store_id not in existing_ids},
        key=str,
    )
    if len(candidates) < needed:
        raise ValueError(
            f"store_pool has {len(candidates)} available entries, need {needed}"
        )

    selected = rng.sample(candidates, k=needed)
    await conn.executemany(
        """
        INSERT INTO sim.fraud_collusive_stores (store_id)
        VALUES ($1)
        ON CONFLICT (store_id) DO NOTHING
        """,
        [(store_id,) for store_id in selected],
    )
    existing_ids = await _fetch_collusive_store_ids(conn)

    COLLUSIVE_STORES.clear()
    COLLUSIVE_STORES.update(existing_ids)


@register("collusive_merchant", 0.05)
async def generate_collusive_merchant_fraud(
    ctx: FraudPatternContext,
) -> tuple[dict[str, Any], GroundTruth]:
    """Generate a single collusive-merchant fraud order.

    Spec: Pattern 5 generation logic:
    - store: picked from COLLUSIVE_STORES (10 stores) via ctx.rng
    - card: stolen-pattern (foreign-biased, often prepaid)
    - AVS/CVV: usually MATCH (80%+) — merchant ignores mismatches
    - order value: lognormal mean ~£20 (2000 pence), sigma=0.3
    - pattern_notes = f"store_id={store_id}"
    """
    if not COLLUSIVE_STORES:
        raise RuntimeError(
            "COLLUSIVE_STORES is empty — call init_collusive_stores(rng, conn, store_pool) at simulator startup"
        )

    store_id = ctx.rng.choice(sorted(COLLUSIVE_STORES))

    card_country = _weighted_choice(
        ctx.rng,
        [
            ("US", 0.20),
            ("RU", 0.15),
            ("NG", 0.10),
            ("IN", 0.08),
            ("CN", 0.07),
            ("GB", 0.15),
            ("foreign_other", 0.25),
        ],
    )
    card_funding_type = _weighted_choice(
        ctx.rng,
        [("PREPAID", 0.50), ("CREDIT", 0.35), ("DEBIT", 0.15)],
    )

    avs_result = "MATCH" if ctx.rng.random() < 0.80 else "NO_MATCH"
    cvv_result = "MATCH" if ctx.rng.random() < 0.80 else "NO_MATCH"

    sigma = 0.3
    mu = math.log(2000) - (sigma**2) / 2
    order_total_pence: int = max(500, int(math.exp(ctx.rng.gauss(mu, sigma))))

    _order_id = UUID(int=ctx.rng.getrandbits(128))

    order_dict: dict[str, Any] = {
        "order_id": _order_id,
        "store_id": store_id,
        "order_total_pence": order_total_pence,
        "card_country": card_country,
        "card_funding_type": card_funding_type,
        "avs_result": avs_result,
        "cvv_result": cvv_result,
    }

    gt = GroundTruth(
        order_id=_order_id,
        is_fraud=True,
        fraud_category="collusive_merchant",
        pattern_notes=f"store_id={store_id}",
        ring_id=store_id,
    )

    return order_dict, gt


# No auto-init at module-import time. Call init_collusive_stores(rng, conn, store_pool=pool)
# explicitly at simulator startup.
