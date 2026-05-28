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
    delivery_address_uuid: UUID
    device_uuid: UUID
    preferred_store_ids: list[UUID]


RESELLER_ACCOUNTS: list[ResellerAccount] = []


def _create_account(rng: random.Random, store_id_pool: list[UUID]) -> ResellerAccount:
    preferred_store_count = rng.randint(1, min(3, len(store_id_pool)))
    return ResellerAccount(
        account_id=UUID(int=rng.getrandbits(128)),
        reseller_address={
            "lat": rng.uniform(51.45, 51.55),
            "lon": rng.uniform(-0.20, 0.05),
            "postcode": f"E1{rng.randint(1, 9)}{rng.randint(1, 9)} "
            f"{rng.randint(1, 9)}{chr(rng.randint(65, 90))}{rng.randint(1, 9)}",
            "city": "London",
        },
        delivery_address_uuid=UUID(int=rng.getrandbits(128)),
        device_uuid=UUID(int=rng.getrandbits(128)),
        preferred_store_ids=[rng.choice(store_id_pool) for _ in range(preferred_store_count)],
    )


def init_reseller_accounts(
    rng: random.Random, store_id_pool: list[UUID], n: int = 50
) -> None:
    if not store_id_pool:
        raise ValueError("store_id_pool must contain at least one store_id")

    RESELLER_ACCOUNTS.clear()
    for _ in range(n):
        RESELLER_ACCOUNTS.append(_create_account(rng, store_id_pool))


def _sample_per_item_price_pence(
    rng: random.Random, mean_pence: int = 1000, sigma: float = 0.35
) -> int:
    mu = math.log(mean_pence) - (sigma ** 2) / 2
    return max(200, int(rng.lognormvariate(mu, sigma)))


@register("reseller", 0.05)
async def generate_reseller_fraud(
    ctx: FraudPatternContext,
) -> tuple[dict[str, Any], GroundTruth]:
    if not RESELLER_ACCOUNTS:
        raise RuntimeError(
            "RESELLER_ACCOUNTS is empty — call "
            "init_reseller_accounts(rng, store_id_pool) explicitly"
        )

    account = ctx.rng.choice(RESELLER_ACCOUNTS)
    item_count = ctx.rng.randint(10, 25)
    order_total_pence = sum(_sample_per_item_price_pence(ctx.rng) for _ in range(item_count))

    preferred_stores = account.preferred_store_ids
    if preferred_stores and ctx.rng.random() < 0.70:
        store_id: UUID = ctx.rng.choice(preferred_stores)
    else:
        if not preferred_stores:
            raise RuntimeError("reseller account has no preferred_store_ids")
        store_id = ctx.rng.choice(preferred_stores)

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
        "delivery_address_id": account.delivery_address_uuid,
        "delivery_address_snapshot": account.reseller_address,
        "payment_method": "RESELLER_OWN_CARD",
        "device_id": account.device_uuid,
        "user_id": account.account_id,
        "store_id": store_id,
    }

    return order_dict, GroundTruth(
        order_id=order_id,
        is_fraud=True,
        fraud_category="reseller",
        pattern_notes=pattern_notes,
        ring_id=None,
    )
