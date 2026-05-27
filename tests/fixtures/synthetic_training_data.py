from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple

import numpy as np
import pandas as pd

# The packet requires Python 3.8-compatible typing names here.
# ruff: noqa: UP006

REFERENCE_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)

STRING_VALUES: Dict[str, Tuple[str, ...]] = {
    "order_channel": ("app", "web"),
    "order_type": ("delivery", "pickup"),
    "payment_type": ("card", "wallet"),
    "card_brand": ("visa", "mastercard", "amex"),
    "card_funding_type": ("debit", "credit"),
    "device_type": ("mobile", "desktop", "tablet"),
    "platform": ("ios", "android", "web"),
    "merchant_category": ("quick_service", "grocery", "restaurant"),
    "delivery_address_type": ("home", "work", ""),
    "cancellation_reason": ("", "customer_cancelled", "store_closed"),
    "card_bin": ("400000", "510000", "370000"),
    "card_issuer_bank": ("barclays", "hsbc", "lloyds"),
    "ip_country": ("GB", "IE", "FR"),
    "store_city": ("London", "Manchester", "Birmingham"),
    "browser_name": ("Chrome", "Safari", "Firefox"),
    "user_email_domain": ("example.com", "mail.test", "delivery.test"),
    "card_issuer_country": ("GB", "IE", "FR"),
}

BOOLEAN_COLUMNS: Tuple[str, ...] = (
    "is_first_order_for_user",
    "is_new_payment_method",
    "is_new_delivery_address",
    "is_guest_checkout",
    "is_digital_native_bank",
    "ip_is_proxy",
    "ip_is_vpn",
    "ip_is_tor",
    "ip_is_hosting",
)


def make_synthetic_df(n_rows: int = 10000, n_fraud: int = 200, seed: int = 42) -> pd.DataFrame:
    if n_rows < 0:
        raise ValueError("n_rows must be non-negative")
    if n_fraud < 0 or n_fraud > n_rows:
        raise ValueError("n_fraud must be between 0 and n_rows")

    rng = np.random.default_rng(seed)
    start_date = REFERENCE_DATE - timedelta(days=90)
    if n_rows == 0:
        placed_at = []
    else:
        step_seconds = (90 * 24 * 60 * 60) / float(n_rows)
        placed_at = [
            start_date + timedelta(seconds=step_seconds * row_index) for row_index in range(n_rows)
        ]

    is_fraud = np.zeros(n_rows, dtype=bool)
    is_fraud[:n_fraud] = True

    data: Dict[str, object] = {
        "placed_at": placed_at,
        "user_account_age_days": rng.integers(0, 3650, size=n_rows),
        "user_lifetime_order_count": rng.integers(0, 500, size=n_rows),
        "user_lifetime_chargeback_rate": rng.uniform(0.0, 0.08, size=n_rows),
        "user_orders_1h_at_order_time": rng.integers(0, 8, size=n_rows),
        "user_orders_24h_at_order_time": rng.integers(0, 20, size=n_rows),
        "user_spend_24h_pence": rng.integers(0, 25000, size=n_rows),
        "device_lifetime_order_count": rng.integers(0, 500, size=n_rows),
        "device_unique_users_lifetime": rng.integers(1, 30, size=n_rows),
        "payment_lifetime_chargeback_rate": rng.uniform(0.0, 0.1, size=n_rows),
        "ip_unique_users_24h": rng.integers(1, 40, size=n_rows),
        "store_chargeback_rate": rng.uniform(0.0, 0.06, size=n_rows),
        "merchant_chargeback_rate": rng.uniform(0.0, 0.05, size=n_rows),
        "email_domain_chargeback_rate": rng.uniform(0.0, 0.04, size=n_rows),
        "subtotal_pence": rng.integers(500, 12000, size=n_rows),
        "total_pence": rng.integers(800, 15000, size=n_rows),
        "item_count": rng.integers(1, 12, size=n_rows),
        "delivery_distance_km": rng.uniform(0.1, 15.0, size=n_rows),
        "ip_to_delivery_distance_km": rng.uniform(0.0, 500.0, size=n_rows),
        "billing_to_delivery_distance_km": rng.uniform(0.0, 120.0, size=n_rows),
        "time_to_checkout_seconds": rng.integers(20, 1800, size=n_rows),
        "is_fraud": is_fraud,
        "gt_is_fraud": is_fraud,
        "fraud_category": np.where(is_fraud, "stolen_card", ""),
    }

    for column, values in STRING_VALUES.items():
        data[column] = rng.choice(values, size=n_rows)

    for column in BOOLEAN_COLUMNS:
        data[column] = rng.choice(np.array([False, True]), size=n_rows, p=[0.9, 0.1])

    return pd.DataFrame(data)
