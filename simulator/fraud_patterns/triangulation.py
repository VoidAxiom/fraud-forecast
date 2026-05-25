"""Triangulation fraud pattern for Phase 3 simulator behavior.

Scenario: Fraudster sells food orders cheaply on a side channel (e.g. Telegram).
Customer pays fraudster directly, fraudster places real order to customer's address
using a stolen card. The signal: a fraudster account places orders to many different
new addresses with many different cards, using a consistent device.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from simulator.fraud_patterns import GroundTruth, register
from simulator.fraud_patterns.stolen_card import FraudPatternContext

# Pool of triangulation fraudster accounts (stateful across invocations).
TRIANGULATION_ACCOUNTS: list[TriangulationAccount] = []

# Lognormal params for order value: mean ~£30, sigma=0.3
_ORDER_VALUE_MEAN_GBP = 30.0
_ORDER_VALUE_SIGMA = 0.3
_ORDER_VALUE_MU = math.log(_ORDER_VALUE_MEAN_GBP) - (_ORDER_VALUE_SIGMA ** 2) / 2

# Foreign-bias card countries (same stolen-card pattern from P3-B)
_OTHER_ISO2_POOL: list[str] = ["BR", "MX", "PL", "TR", "ZA", "AR", "PH", "PK", "ID", "TH"]
_CARD_COUNTRIES = [
    ("GB", 0.20),
    ("US", 0.20),
    ("RU", 0.12),
    ("NG", 0.10),
    ("IN", 0.08),
    ("CN", 0.06),
    ("DE", 0.05),
    ("FR", 0.05),
    ("BR", 0.014),
    ("MX", 0.014),
    ("PL", 0.014),
    ("TR", 0.014),
    ("ZA", 0.014),
    ("AR", 0.014),
    ("PH", 0.014),
    ("PK", 0.014),
    ("ID", 0.014),
    ("TH", 0.014),
]

# AVS: sophisticated variant — often MATCH (fraudster knows the billing addr)
_AVS_MATCH_PROB = 0.60

# Card funding: often prepaid / credit
_CARD_FUNDING_CHOICES = [("PREPAID", 0.40), ("CREDIT", 0.40), ("DEBIT", 0.20)]


@dataclass
class TriangulationAccount:
    account_id: UUID
    device_id: UUID
    cards_used_count: int = 0
    delivery_addresses_used_count: int = 0


def _uuid_from_rng(rng: random.Random) -> UUID:
    return UUID(int=rng.getrandbits(128))


def _weighted_choice(rng: random.Random, choices: list[tuple[str, float]]) -> str:
    total = sum(w for _, w in choices)
    draw = rng.random() * total
    cumulative = 0.0
    for value, weight in choices:
        cumulative += weight
        if draw < cumulative:
            return value
    return choices[-1][0]


def _new_stolen_card(rng: random.Random) -> dict[str, str]:
    """Generate a stolen-pattern card placeholder."""
    country = _weighted_choice(rng, _CARD_COUNTRIES)
    funding = _weighted_choice(rng, _CARD_FUNDING_CHOICES)
    avs = "MATCH" if rng.random() < _AVS_MATCH_PROB else "NO_MATCH"
    return {
        "card_bin": f"{rng.randint(100000, 999999):06d}",
        "payment_method_id": str(_uuid_from_rng(rng)),
        "card_country": country,
        "card_funding_type": funding,
        "avs_result": avs,
    }


def _new_delivery_address(rng: random.Random) -> UUID:
    """Generate a new residential delivery address UUID (Phase 2-C resolves to real seeded address)."""
    return _uuid_from_rng(rng)


def init_accounts(rng: random.Random, n: int = 30) -> None:
    """Initialize the pool of triangulation fraudster accounts.

    Must be called at simulator startup (and in tests) before dispatch.
    P3-B no-default convention: no implicit fallback.
    """
    TRIANGULATION_ACCOUNTS.clear()
    for _ in range(n):
        TRIANGULATION_ACCOUNTS.append(
            TriangulationAccount(
                account_id=_uuid_from_rng(rng),
                device_id=_uuid_from_rng(rng),
            )
        )


@register("triangulation", 0.05)
async def generate_triangulation_fraud(
    ctx: FraudPatternContext,
) -> tuple[dict[str, Any], GroundTruth]:
    """Generate a triangulation fraud order.

    Signal: consistent device_id per fraudster account, but many distinct
    delivery addresses and many distinct stolen cards over time.
    """
    if not TRIANGULATION_ACCOUNTS:
        raise RuntimeError(
            "TRIANGULATION_ACCOUNTS is empty — call init_accounts(rng) at simulator startup"
        )

    # Pick a fraudster account
    account = ctx.rng.choice(TRIANGULATION_ACCOUNTS)

    # Consistent device (the fraudster's own device)
    device_id = account.device_id

    # New stolen card for this transaction
    card = _new_stolen_card(ctx.rng)
    account.cards_used_count += 1

    # New delivery address (the customer who paid the fraudster)
    delivery_address_id = _new_delivery_address(ctx.rng)
    account.delivery_addresses_used_count += 1

    # Realistic order value ~£25-40 (lognormal mean ~£30, sigma=0.3)
    order_value_gbp = math.exp(ctx.rng.gauss(_ORDER_VALUE_MU, _ORDER_VALUE_SIGMA))
    order_total_pence = max(2500, min(4000, int(order_value_gbp * 100)))

    order_id = _uuid_from_rng(ctx.rng)

    # ip_country: ISO-2, consistent with card country (sophisticated fraudster)
    ip_country = card["card_country"]

    order_dict: dict[str, Any] = {
        "order_id": order_id,
        "order_total_pence": order_total_pence,
        "user_id": account.account_id,
        "device_id": device_id,
        "delivery_address_id": delivery_address_id,
        "card_bin": card["card_bin"],
        "payment_method_id": card["payment_method_id"],
        "card_country": card["card_country"],
        "card_funding_type": card["card_funding_type"],
        "avs_result": card["avs_result"],
        "ip_country": ip_country,
        "address_type": "RESIDENTIAL_NEW",
        "is_new_device": False,  # fraudster reuses their own device
    }

    pattern_notes = (
        f"fraudster_account_id={account.account_id}, "
        f"n_addresses_used={account.delivery_addresses_used_count}, "
        f"n_cards_used={account.cards_used_count}"
    )

    gt = GroundTruth(
        order_id=order_id,
        is_fraud=True,
        fraud_category="triangulation",
        pattern_notes=pattern_notes,
        ring_id=None,
    )

    return order_dict, gt
