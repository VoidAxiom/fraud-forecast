"""Reseller fraud pattern for Phase 3 simulator behavior."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from simulator.fraud_patterns import GroundTruth, register
from simulator.fraud_patterns.stolen_card import FraudPatternContext


@dataclass
class ResellerAccount:
    account_id: UUID
    reseller_address: dict[str, Any]
    preferred_store_ids: list[UUID]


RESELLER_ACCOUNTS: list[ResellerAccount] = []


def _create_account(rng: random.Random) -> ResellerAccount:
    return ResellerAccount(
        account_id=UUID(int=rng.getrandbits(128)),
        reseller_address={
            "lat": rng.uniform(51.45, 51.55),
            "lon": rng.uniform(-0.20, 0.05),
            "postcode": f"E1{rng.randint(1, 9)}{rng.randint(1, 9)} "
            f"{rng.randint(1, 9)}{chr(rng.randint(65, 90))}{rng.randint(1, 9)}",
            "city": "London",
        },
        preferred_store_ids=[UUID(int=rng.getrandbits(128)) for _ in range(rng.randint(1, 3))],
    )


def init_reseller_accounts(rng: random.Random, n: int = 50) -> None:
    RESELLER_ACCOUNTS.clear()
    for _ in range(n):
        RESELLER_ACCOUNTS.append(_create_account(rng))


def _sample_order_total_pence(rng: random.Random, mean_pence: int = 15000, sigma: float = 0.3) -> int:
    mu = math.log(mean_pence) - (sigma ** 2) / 2
    return max(5000, int(rng.lognormvariate(mu, sigma)))


@register("reseller", 0.05)
async def generate_reseller_fraud(
    ctx: FraudPatternContext,
) -> tuple[dict[str, Any], GroundTruth]:
    if not RESELLER_ACCOUNTS:
        raise RuntimeError(
            "RESELLER_ACCOUNTS is empty — call init_reseller_accounts(rng) explicitly"
        )

    account = ctx.rng.choice(RESELLER_ACCOUNTS)
    item_count = ctx.rng.randint(10, 25)
    order_total_pence = _sample_order_total_pence(ctx.rng)

    preferred_stores = account.preferred_store_ids
    if preferred_stores and ctx.rng.random() < 0.70:
        store_id: UUID | str = ctx.rng.choice(preferred_stores)
    else:
        store_id = "RESELLER_OTHER_STORE"

    order_id: UUID = UUID(int=ctx.rng.getrandbits(128))
    pattern_notes: str = (
        f"reseller_account_id={account.account_id}, "
        f"item_count={item_count}, "
        "delivery_addr_stable=true"
    )

    order_dict: dict[str, Any] = {
        "order_id": order_id,
        "order_total_pence": order_total_pence,
        "item_count": item_count,
        "delivery_address_id": account.reseller_address,
        "payment_method": "RESELLER_OWN_CARD",
        "device_id": "RESELLER_OWN_DEVICE",
        "store_id": store_id,
    }

    return order_dict, GroundTruth(
        order_id=order_id,
        is_fraud=True,
        fraud_category="reseller",
        pattern_notes=pattern_notes,
        ring_id=None,
    )
