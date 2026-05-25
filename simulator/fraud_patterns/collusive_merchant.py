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

from simulator.fraud_patterns import GroundTruth, register
from simulator.fraud_patterns.stolen_card import FraudPatternContext, _weighted_choice

# Module-level set of collusive store IDs — populated at simulator startup
# via init_collusive_stores(). Until initialized, the generator raises.
COLLUSIVE_STORES: set[UUID] = set()


def init_collusive_stores(rng: random.Random, n: int = 10) -> None:
    """Generate n placeholder store UUIDs as the collusive store pool.

    Must be called at simulator startup (and in tests) before generating
    collusive_merchant orders. Phase 2-C resolves to real seeded store_ids.
    """
    COLLUSIVE_STORES.clear()
    while len(COLLUSIVE_STORES) < n:
        COLLUSIVE_STORES.add(UUID(int=rng.getrandbits(128)))


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
            "COLLUSIVE_STORES is empty — call init_collusive_stores(rng) at simulator startup"
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
