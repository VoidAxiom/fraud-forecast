"""Reseller fraud pattern for Phase 3 simulator behavior."""

from __future__ import annotations

import json
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from simulator.fraud_patterns import GroundTruth, register
from simulator.fraud_patterns.stolen_card import FraudPatternContext

if TYPE_CHECKING:
    import asyncpg


@dataclass
class ResellerAccount:
    account_id: UUID
    reseller_address: dict[str, Any]
    delivery_address_uuid: UUID
    device_uuid: UUID
    preferred_store_ids: list[UUID]


RESELLER_ACCOUNTS: list[ResellerAccount] = []
RESELLER_STORE_POOL: list[UUID] = []

_SELECT_RESELLER_ACCOUNTS_SQL = """
SELECT
    account_id,
    reseller_address,
    delivery_address_uuid,
    device_uuid,
    preferred_store_ids
FROM sim.fraud_reseller_accounts
ORDER BY created_at, account_id
"""

_INSERT_RESELLER_ACCOUNTS_SQL = """
INSERT INTO sim.fraud_reseller_accounts (
    account_id,
    reseller_address,
    delivery_address_uuid,
    device_uuid,
    preferred_store_ids
) VALUES ($1, $2::jsonb, $3, $4, $5)
ON CONFLICT (account_id) DO NOTHING
"""


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


def _account_from_row(row: Mapping[str, Any]) -> ResellerAccount:
    account_id = row["account_id"]
    if not isinstance(account_id, UUID):
        raise TypeError("fraud_reseller_accounts.account_id must be a UUID")

    reseller_address_raw = row["reseller_address"]
    if isinstance(reseller_address_raw, str):
        reseller_address_value = json.loads(reseller_address_raw)
    else:
        reseller_address_value = reseller_address_raw
    if not isinstance(reseller_address_value, dict):
        raise TypeError("fraud_reseller_accounts.reseller_address must be a dict")
    reseller_address: dict[str, Any] = {}
    for key, value in reseller_address_value.items():
        if not isinstance(key, str):
            raise TypeError("fraud_reseller_accounts.reseller_address keys must be str")
        reseller_address[key] = value

    delivery_address_uuid = row["delivery_address_uuid"]
    if not isinstance(delivery_address_uuid, UUID):
        raise TypeError("fraud_reseller_accounts.delivery_address_uuid must be a UUID")

    device_uuid = row["device_uuid"]
    if not isinstance(device_uuid, UUID):
        raise TypeError("fraud_reseller_accounts.device_uuid must be a UUID")

    preferred_store_ids_raw = row["preferred_store_ids"]
    if not isinstance(preferred_store_ids_raw, list):
        raise TypeError("fraud_reseller_accounts.preferred_store_ids must be a list")
    preferred_store_ids: list[UUID] = []
    for store_id in preferred_store_ids_raw:
        if not isinstance(store_id, UUID):
            raise TypeError("fraud_reseller_accounts.preferred_store_ids must contain UUIDs")
        preferred_store_ids.append(store_id)

    return ResellerAccount(
        account_id=account_id,
        reseller_address=reseller_address,
        delivery_address_uuid=delivery_address_uuid,
        device_uuid=device_uuid,
        preferred_store_ids=preferred_store_ids,
    )


def _insert_args(account: ResellerAccount) -> tuple[UUID, str, UUID, UUID, list[UUID]]:
    return (
        account.account_id,
        json.dumps(account.reseller_address),
        account.delivery_address_uuid,
        account.device_uuid,
        account.preferred_store_ids,
    )


async def init_reseller_accounts(
    rng: random.Random,
    conn: asyncpg.Connection,
    store_id_pool: list[UUID],
    n: int = 50,
) -> None:
    if not store_id_pool:
        raise ValueError("store_id_pool must contain at least one store_id")

    RESELLER_ACCOUNTS.clear()
    RESELLER_STORE_POOL.clear()
    RESELLER_STORE_POOL.extend(store_id_pool)

    # Always generate exactly n accounts so the shared simulator RNG advances
    # identically on cold starts, partial warm starts, and full warm starts.
    all_accounts = [_create_account(rng, store_id_pool) for _ in range(n)]

    async with conn.transaction():
        await conn.execute("LOCK TABLE sim.fraud_reseller_accounts IN SHARE ROW EXCLUSIVE MODE")
        rows = await conn.fetch(_SELECT_RESELLER_ACCOUNTS_SQL)
        if len(rows) < n:
            existing_ids = {row["account_id"] for row in rows}
            new_accounts = [
                account for account in all_accounts if account.account_id not in existing_ids
            ]
            await conn.executemany(
                _INSERT_RESELLER_ACCOUNTS_SQL,
                [_insert_args(account) for account in new_accounts],
            )
            rows = await conn.fetch(_SELECT_RESELLER_ACCOUNTS_SQL)

    RESELLER_ACCOUNTS.extend(_account_from_row(row) for row in rows)


def _sample_per_item_price_pence(
    rng: random.Random, mean_pence: int = 1000, sigma: float = 0.35
) -> int:
    mu = math.log(mean_pence) - (sigma**2) / 2
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
        if not RESELLER_STORE_POOL:
            raise RuntimeError(
                "RESELLER_STORE_POOL is empty — call "
                "init_reseller_accounts(rng, store_id_pool) explicitly"
            )
        preferred_set = set(preferred_stores)
        non_preferred_pool = [sid for sid in RESELLER_STORE_POOL if sid not in preferred_set]
        store_id = ctx.rng.choice(non_preferred_pool or RESELLER_STORE_POOL)

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
