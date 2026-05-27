"""SQL-backed training data loader for the Phase 5 XGBoost pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

import pandas as pd
from shared.db import get_engine
from sqlalchemy import text  # type: ignore[import]  # SQLAlchemy 1.4 has no type stubs

# The packet requires Python 3.8-compatible typing names here.
# ruff: noqa: UP006, UP045


@dataclass
class TrainingDataConfig:
    start_date: datetime
    end_date: datetime
    label_finalisation_buffer_days: int = 45
    max_rows: Optional[int] = None


_REQUIRED_COLUMNS: Tuple[str, ...] = (
    "user_account_age_days",
    "user_lifetime_order_count",
    "user_lifetime_chargeback_rate",
    "user_orders_1h_at_order_time",
    "user_orders_24h_at_order_time",
    "user_spend_24h_pence",
    "device_lifetime_order_count",
    "device_unique_users_lifetime",
    "payment_lifetime_chargeback_rate",
    "ip_unique_users_24h",
    "store_chargeback_rate",
    "merchant_chargeback_rate",
    "email_domain_chargeback_rate",
    "subtotal_pence",
    "total_pence",
    "item_count",
    "delivery_distance_km",
    "ip_to_delivery_distance_km",
    "billing_to_delivery_distance_km",
    "time_to_checkout_seconds",
    "order_channel",
    "order_type",
    "payment_type",
    "card_brand",
    "card_funding_type",
    "device_type",
    "platform",
    "merchant_category",
    "delivery_address_type",
    "cancellation_reason",
    "card_bin",
    "card_issuer_bank",
    "ip_country",
    "store_city",
    "browser_name",
    "user_email_domain",
    "is_first_order_for_user",
    "is_new_payment_method",
    "is_new_delivery_address",
    "is_guest_checkout",
    "is_digital_native_bank",
    "ip_is_proxy",
    "ip_is_vpn",
    "ip_is_tor",
    "ip_is_hosting",
    "is_fraud",
)

_TRAINING_SQL = """
WITH all_orders AS (
    SELECT * FROM orders
    UNION ALL
    SELECT * FROM orders_archive
),
-- There is no chargebacks_archive table in the schema, so use live chargebacks.
order_features AS (
    SELECT
        o.order_id,
        o.placed_at,
        o.user_id,
        o.store_id,
        o.merchant_id,
        o.device_id,
        COALESCE(CAST(o.ip_address AS VARCHAR), '') AS ip_address,
        COALESCE(CAST(o.fraud_outcome AS VARCHAR), 'UNKNOWN') AS fraud_outcome,
        COALESCE(o.user_total_orders_lifetime, 0) AS user_total_orders_lifetime,
        COALESCE(o.user_total_orders_30d, 0) AS user_total_orders_30d,
        COALESCE(o.user_total_spend_lifetime_pence, 0) AS user_total_spend_lifetime_pence,
        COALESCE(o.user_avg_order_value_pence, 0) AS user_avg_order_value_pence,
        COALESCE(o.user_chargebacks_lifetime, 0) AS user_chargebacks_lifetime,
        COALESCE(o.user_refunds_lifetime, 0) AS user_refunds_lifetime,
        COALESCE(CAST(o.user_risk_tier_at_order AS VARCHAR), 'UNKNOWN') AS user_risk_tier_at_order,
        COALESCE(CAST(o.store_country AS VARCHAR), 'GB') AS store_country,
        COALESCE(o.store_latitude, 0.0)::DOUBLE PRECISION AS store_latitude,
        COALESCE(o.store_longitude, 0.0)::DOUBLE PRECISION AS store_longitude,
        COALESCE(CAST(o.order_status AS VARCHAR), 'UNKNOWN') AS order_status,
        COALESCE(CAST(o.card_issuer_country AS VARCHAR), 'UNKNOWN') AS card_issuer_country,

        COALESCE(o.user_account_age_days, 0) AS user_account_age_days,
        COALESCE(o.user_total_orders_lifetime, 0) AS user_lifetime_order_count,
        (
            COALESCE(o.user_chargebacks_lifetime, 0)::DOUBLE PRECISION
            / (COALESCE(o.user_total_orders_lifetime, 0) + 1)
        ) AS user_lifetime_chargeback_rate,
        COUNT(*) OVER (
            PARTITION BY o.user_id
            ORDER BY o.placed_at
            RANGE BETWEEN INTERVAL '1 hour' PRECEDING AND INTERVAL '1 second' PRECEDING
        ) AS user_orders_1h_at_order_time,
        COUNT(*) OVER (
            PARTITION BY o.user_id
            ORDER BY o.placed_at
            RANGE BETWEEN INTERVAL '24 hours' PRECEDING AND INTERVAL '1 second' PRECEDING
        ) AS user_orders_24h_at_order_time,
        COALESCE(
            SUM(o.total_pence) OVER (
                PARTITION BY o.user_id
                ORDER BY o.placed_at
                RANGE BETWEEN INTERVAL '24 hours' PRECEDING AND INTERVAL '1 second' PRECEDING
            ),
            0
        )::BIGINT AS user_spend_24h_pence,
        CASE
            WHEN o.device_id IS NULL THEN 0
            ELSE COUNT(*) OVER (
                PARTITION BY o.device_id
                ORDER BY o.placed_at
                RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL '1 second' PRECEDING
            )
        END AS device_lifetime_order_count,
        (
            SELECT COUNT(DISTINCT o2.user_id)
            FROM all_orders o2
            WHERE o2.device_id = o.device_id
              AND o2.placed_at < o.placed_at
        ) AS device_unique_users_lifetime,
        COALESCE(
            CAST((
                SELECT COUNT(*)
                FROM chargebacks cb
                JOIN all_orders o2 ON cb.order_id = o2.order_id
                WHERE o2.payment_method_id = o.payment_method_id
                  AND o2.placed_at < o.placed_at
                  AND cb.received_at < o.placed_at
            ) AS DOUBLE PRECISION) / NULLIF((
                SELECT COUNT(*)
                FROM all_orders o2
                WHERE o2.payment_method_id = o.payment_method_id
                  AND o2.placed_at < o.placed_at
            ), 0),
            0.0
        ) AS payment_lifetime_chargeback_rate,
        (
            SELECT COUNT(DISTINCT o2.user_id)
            FROM all_orders o2
            WHERE o2.ip_address = o.ip_address
              AND o2.placed_at >= o.placed_at - INTERVAL '24 hours'
              AND o2.placed_at < o.placed_at
        ) AS ip_unique_users_24h,
        COALESCE(
            CAST((
                SELECT COUNT(*)
                FROM chargebacks cb
                JOIN all_orders o2 ON cb.order_id = o2.order_id
                WHERE o2.store_id = o.store_id
                  AND o2.placed_at < o.placed_at
                  AND cb.received_at < o.placed_at
            ) AS DOUBLE PRECISION) / NULLIF((
                SELECT COUNT(*)
                FROM all_orders o2
                WHERE o2.store_id = o.store_id
                  AND o2.placed_at < o.placed_at
            ), 0),
            0.0
        ) AS store_chargeback_rate,
        COALESCE(
            CAST((
                SELECT COUNT(*)
                FROM chargebacks cb
                JOIN all_orders o2 ON cb.order_id = o2.order_id
                WHERE o2.merchant_id = o.merchant_id
                  AND o2.placed_at < o.placed_at
                  AND cb.received_at < o.placed_at
            ) AS DOUBLE PRECISION) / NULLIF((
                SELECT COUNT(*)
                FROM all_orders o2
                WHERE o2.merchant_id = o.merchant_id
                  AND o2.placed_at < o.placed_at
            ), 0),
            0.0
        ) AS merchant_chargeback_rate,
        COALESCE(
            CAST((
                SELECT COUNT(*)
                FROM chargebacks cb
                JOIN all_orders o2 ON cb.order_id = o2.order_id
                WHERE o2.user_email_domain = o.user_email_domain
                  AND o2.placed_at < o.placed_at
                  AND cb.received_at < o.placed_at
            ) AS DOUBLE PRECISION) / NULLIF((
                SELECT COUNT(*)
                FROM all_orders o2
                WHERE o2.user_email_domain = o.user_email_domain
                  AND o2.placed_at < o.placed_at
            ), 0),
            0.0
        ) AS email_domain_chargeback_rate,
        COALESCE(o.subtotal_pence, 0) AS subtotal_pence,
        COALESCE(o.total_pence, 0) AS total_pence,
        COALESCE(o.item_count, 0) AS item_count,
        COALESCE(o.delivery_distance_km, 0.0)::DOUBLE PRECISION AS delivery_distance_km,
        COALESCE(o.ip_to_delivery_distance_km, 0.0)::DOUBLE PRECISION
            AS ip_to_delivery_distance_km,
        COALESCE(o.billing_to_delivery_distance_km, 0.0)::DOUBLE PRECISION
            AS billing_to_delivery_distance_km,
        COALESCE(o.time_to_checkout_seconds, 0) AS time_to_checkout_seconds,

        COALESCE(CAST(o.order_channel AS VARCHAR), 'UNKNOWN') AS order_channel,
        COALESCE(CAST(o.order_type AS VARCHAR), 'UNKNOWN') AS order_type,
        COALESCE(CAST(o.payment_type AS VARCHAR), 'UNKNOWN') AS payment_type,
        COALESCE(CAST(o.card_brand AS VARCHAR), 'UNKNOWN') AS card_brand,
        COALESCE(CAST(o.card_funding_type AS VARCHAR), 'UNKNOWN') AS card_funding_type,
        COALESCE(CAST(o.device_type AS VARCHAR), 'UNKNOWN') AS device_type,
        COALESCE(CAST(o.platform AS VARCHAR), 'UNKNOWN') AS platform,
        CAST('UNKNOWN' AS VARCHAR) AS merchant_category,
        COALESCE(CAST(o.delivery_address_type AS VARCHAR), 'UNKNOWN') AS delivery_address_type,
        COALESCE(CAST(o.cancellation_reason AS VARCHAR), 'UNKNOWN') AS cancellation_reason,
        COALESCE(CAST(o.card_bin AS VARCHAR), 'UNKNOWN') AS card_bin,
        CAST('UNKNOWN' AS VARCHAR) AS card_issuer_bank,
        COALESCE(CAST(o.ip_country AS VARCHAR), 'UNKNOWN') AS ip_country,
        COALESCE(CAST(o.store_city AS VARCHAR), 'UNKNOWN') AS store_city,
        COALESCE(CAST(o.browser_name AS VARCHAR), 'UNKNOWN') AS browser_name,
        COALESCE(CAST(o.user_email_domain AS VARCHAR), 'UNKNOWN') AS user_email_domain,

        COALESCE(o.is_first_order_for_user, FALSE) AS is_first_order_for_user,
        COALESCE(o.is_new_payment_method, FALSE) AS is_new_payment_method,
        COALESCE(o.is_new_delivery_address, FALSE) AS is_new_delivery_address,
        COALESCE(o.is_guest_checkout, FALSE) AS is_guest_checkout,
        COALESCE(o.is_digital_native_bank, FALSE) AS is_digital_native_bank,
        COALESCE(o.ip_is_proxy, FALSE) AS ip_is_proxy,
        COALESCE(o.ip_is_vpn, FALSE) AS ip_is_vpn,
        COALESCE(o.ip_is_tor, FALSE) AS ip_is_tor,
        COALESCE(o.ip_is_hosting, FALSE) AS ip_is_hosting,

        COALESCE(gt.is_fraud, FALSE) AS is_fraud,
        COALESCE(gt.is_fraud, FALSE) AS gt_is_fraud,
        COALESCE(CAST(gt.fraud_category AS VARCHAR), 'LEGIT') AS fraud_category
    FROM all_orders o
    LEFT JOIN simulator_ground_truth gt ON gt.order_id = o.order_id
)
SELECT *
FROM order_features
WHERE placed_at >= :start_date
  AND placed_at < :end_date
  AND placed_at < (
      NOW() - (CAST(:label_finalisation_buffer_days AS INTEGER) * INTERVAL '1 day')
  )
ORDER BY placed_at ASC, order_id ASC
"""


def load_training_data(config: TrainingDataConfig) -> pd.DataFrame:
    """
    Returns a DataFrame with order context, point-in-time training features, and labels.
    """
    if config.end_date <= config.start_date:
        raise ValueError("end_date must be after start_date")
    if config.label_finalisation_buffer_days < 0:
        raise ValueError("label_finalisation_buffer_days must be non-negative")
    if config.max_rows is not None and config.max_rows < 0:
        raise ValueError("max_rows must be non-negative when provided")

    query_text = _TRAINING_SQL
    params: Dict[str, object] = {
        "start_date": config.start_date,
        "end_date": config.end_date,
        "label_finalisation_buffer_days": config.label_finalisation_buffer_days,
    }
    if config.max_rows is not None:
        query_text = f"{query_text}\nLIMIT :max_rows"
        params["max_rows"] = config.max_rows

    engine = get_engine(role="training")
    with engine.connect() as conn:
        df: pd.DataFrame = pd.read_sql_query(text(query_text), conn, params=params)

    missing_columns: List[str] = [
        column for column in _REQUIRED_COLUMNS if column not in df.columns
    ]
    if missing_columns:
        raise RuntimeError(
            "Training query did not return required columns: " + ", ".join(missing_columns)
        )

    run_id = uuid4().hex
    output_dir = Path("ml/data")
    output_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(output_dir / f"training_{run_id}.parquet", index=False)

    return df


__all__ = ["TrainingDataConfig", "load_training_data"]
