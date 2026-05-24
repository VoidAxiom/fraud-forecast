"""Feature set schema used by phase 6 scoring feature lookups."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FeatureSet:
    """Aggregated, flat feature payload loaded from feature-store hashes."""

    # User streaming fields
    user_orders_1h: int = 0
    user_orders_24h: int = 0
    user_spend_1h_pence: int = 0
    user_spend_24h_pence: int = 0
    user_unique_stores_24h: int = 0
    user_unique_payment_methods_24h: int = 0
    user_last_order_age_minutes: int | None = None

    # User batch fields
    user_lifetime_order_count: int | None = None
    user_lifetime_spend_pence: int | None = None
    user_avg_order_value_pence: int | None = None
    user_lifetime_chargeback_count: int | None = None
    user_lifetime_refund_count: int | None = None
    user_lifetime_chargeback_rate: float | None = None
    user_unique_devices_used: int | None = None
    user_unique_payment_methods_used: int | None = None
    user_unique_delivery_addresses: int | None = None
    user_account_age_days: int | None = None
    user_days_since_last_order: int | None = None
    user_distinct_cities_ordered_from: int | None = None

    # Device streaming fields
    device_orders_1h: int = 0
    device_orders_24h: int = 0
    device_unique_users_24h: int = 0
    device_unique_payment_methods_24h: int = 0

    # Device batch fields
    device_lifetime_order_count: int | None = None
    device_lifetime_chargeback_rate: float | None = None
    device_unique_users_lifetime: int | None = None
    device_first_seen_days_ago: int | None = None
    device_distinct_payment_methods_lifetime: int | None = None

    # Payment streaming fields
    payment_orders_1h: int = 0
    payment_orders_24h: int = 0
    payment_unique_users_24h: int = 0
    payment_decline_count_24h: int = 0

    # Payment batch fields
    payment_lifetime_order_count: int | None = None
    payment_lifetime_chargeback_count: int | None = None
    payment_lifetime_chargeback_rate: float | None = None
    payment_unique_users_lifetime: int | None = None
    payment_distinct_delivery_addresses_lifetime: int | None = None

    # IP streaming fields
    ip_orders_1h: int = 0
    ip_orders_24h: int = 0
    ip_unique_users_24h: int = 0
    ip_unique_devices_24h: int = 0

    # IP batch fields
    ip_lifetime_order_count: int | None = None
    ip_unique_users_lifetime: int | None = None
    ip_chargeback_rate: float | None = None
    ip_first_seen_days_ago: int | None = None

    # Store streaming fields
    store_orders_1h: int = 0
    store_orders_24h: int = 0
    store_unique_users_24h: int = 0
    store_unique_cards_1h: int = 0

    # Store batch fields
    store_avg_order_value_pence: int | None = None
    store_chargeback_rate: float | None = None
    store_unique_cards_30d: int | None = None
    store_total_orders_30d: int | None = None

    # Merchant batch fields
    merchant_chargeback_rate: float | None = None
    merchant_total_stores: int | None = None

    # Email domain batch fields
    email_domain_chargeback_rate: float | None = None
    email_domain_total_orders: int | None = None

    # Address streaming fields
    address_orders_24h: int = 0
    address_unique_users_24h: int = 0

    # Meta fields
    feature_fetch_latency_ms: float = 0.0
    missing_features: list[str] = field(default_factory=list)
