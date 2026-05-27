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
dev_user_first_seen AS (
    SELECT
        device_id,
        user_id,
        MIN(placed_at) AS first_seen
    FROM all_orders
    WHERE device_id IS NOT NULL
      AND user_id IS NOT NULL
    GROUP BY device_id, user_id
),
dev_first_seen_counts AS (
    SELECT
        device_id,
        first_seen,
        COUNT(*) AS first_seen_user_count
    FROM dev_user_first_seen
    GROUP BY device_id, first_seen
),
dev_first_seen_cumulative AS (
    SELECT
        device_id,
        first_seen,
        COALESCE(
            SUM(first_seen_user_count) OVER (
                PARTITION BY device_id
                ORDER BY first_seen
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            0
        ) AS cumulative_distinct_users,
        first_seen_user_count
    FROM dev_first_seen_counts
),
dev_unique_users AS (
    SELECT
        dfs.device_id,
        dfs.user_id,
        dfs.first_seen,
        dfsc.cumulative_distinct_users
    FROM dev_user_first_seen dfs
    JOIN dev_first_seen_cumulative dfsc ON dfsc.device_id = dfs.device_id
        AND dfsc.first_seen = dfs.first_seen
),
dev_order_unique_users AS (
    SELECT
        o2.order_id,
        COALESCE(
            MAX(dfsc.cumulative_distinct_users + dfsc.first_seen_user_count),
            0
        ) AS device_unique_users_lifetime
    FROM all_orders o2
    LEFT JOIN dev_first_seen_cumulative dfsc ON dfsc.device_id = o2.device_id
        AND dfsc.first_seen < o2.placed_at
    GROUP BY o2.order_id
),
ip_24h_distinct AS (
    SELECT
        anchors.ip_address,
        anchors.placed_at AS anchor_placed_at,
        COUNT(DISTINCT previous_orders.user_id) AS ip_unique_users_24h
    FROM (
        SELECT DISTINCT
            ip_address,
            placed_at
        FROM all_orders
        WHERE ip_address IS NOT NULL
    ) anchors
    JOIN all_orders previous_orders ON previous_orders.ip_address = anchors.ip_address
        AND previous_orders.placed_at >= anchors.placed_at - INTERVAL '24 hours'
        AND previous_orders.placed_at < anchors.placed_at
    GROUP BY anchors.ip_address, anchors.placed_at
),
chargeback_event_times AS (
    SELECT
        o2.order_id,
        o2.payment_method_id,
        o2.store_id,
        o2.merchant_id,
        o2.user_email_domain,
        GREATEST(o2.placed_at, cb.received_at) AS event_time
    FROM all_orders o2
    JOIN chargebacks cb ON cb.order_id = o2.order_id
),
payment_cb_events AS (
    SELECT
        payment_method_id,
        event_time,
        SUM(order_count) AS order_count,
        SUM(chargeback_count) AS chargeback_count
    FROM (
        SELECT
            o2.payment_method_id,
            o2.placed_at AS event_time,
            COUNT(*) AS order_count,
            0 AS chargeback_count
        FROM all_orders o2
        WHERE o2.payment_method_id IS NOT NULL
        GROUP BY o2.payment_method_id, o2.placed_at
        UNION ALL
        SELECT
            cbe.payment_method_id,
            cbe.event_time,
            0 AS order_count,
            COUNT(*) AS chargeback_count
        FROM chargeback_event_times cbe
        WHERE cbe.payment_method_id IS NOT NULL
        GROUP BY cbe.payment_method_id, cbe.event_time
    ) payment_events
    GROUP BY payment_method_id, event_time
),
payment_cb_windows AS (
    SELECT
        payment_method_id,
        event_time,
        SUM(chargeback_count) OVER (
            PARTITION BY payment_method_id
            ORDER BY event_time
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS prior_chargebacks,
        SUM(order_count) OVER (
            PARTITION BY payment_method_id
            ORDER BY event_time
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS prior_orders
    FROM payment_cb_events
),
payment_cb_rates AS (
    SELECT
        o2.order_id,
        CASE
            WHEN o2.payment_method_id IS NULL THEN NULL
            ELSE pcw.prior_chargebacks::DOUBLE PRECISION / NULLIF(pcw.prior_orders, 0)
        END AS payment_lifetime_chargeback_rate
    FROM all_orders o2
    LEFT JOIN payment_cb_windows pcw ON pcw.payment_method_id = o2.payment_method_id
        AND pcw.event_time = o2.placed_at
),
store_cb_events AS (
    SELECT
        store_id,
        event_time,
        SUM(order_count) AS order_count,
        SUM(chargeback_count) AS chargeback_count
    FROM (
        SELECT
            o2.store_id,
            o2.placed_at AS event_time,
            COUNT(*) AS order_count,
            0 AS chargeback_count
        FROM all_orders o2
        WHERE o2.store_id IS NOT NULL
        GROUP BY o2.store_id, o2.placed_at
        UNION ALL
        SELECT
            cbe.store_id,
            cbe.event_time,
            0 AS order_count,
            COUNT(*) AS chargeback_count
        FROM chargeback_event_times cbe
        WHERE cbe.store_id IS NOT NULL
        GROUP BY cbe.store_id, cbe.event_time
    ) store_events
    GROUP BY store_id, event_time
),
store_cb_windows AS (
    SELECT
        store_id,
        event_time,
        SUM(chargeback_count) OVER (
            PARTITION BY store_id
            ORDER BY event_time
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS prior_chargebacks,
        SUM(order_count) OVER (
            PARTITION BY store_id
            ORDER BY event_time
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS prior_orders
    FROM store_cb_events
),
store_cb_rates AS (
    SELECT
        o2.order_id,
        CASE
            WHEN o2.store_id IS NULL THEN NULL
            ELSE scw.prior_chargebacks::DOUBLE PRECISION / NULLIF(scw.prior_orders, 0)
        END AS store_chargeback_rate
    FROM all_orders o2
    LEFT JOIN store_cb_windows scw ON scw.store_id = o2.store_id
        AND scw.event_time = o2.placed_at
),
merchant_cb_events AS (
    SELECT
        merchant_id,
        event_time,
        SUM(order_count) AS order_count,
        SUM(chargeback_count) AS chargeback_count
    FROM (
        SELECT
            o2.merchant_id,
            o2.placed_at AS event_time,
            COUNT(*) AS order_count,
            0 AS chargeback_count
        FROM all_orders o2
        WHERE o2.merchant_id IS NOT NULL
        GROUP BY o2.merchant_id, o2.placed_at
        UNION ALL
        SELECT
            cbe.merchant_id,
            cbe.event_time,
            0 AS order_count,
            COUNT(*) AS chargeback_count
        FROM chargeback_event_times cbe
        WHERE cbe.merchant_id IS NOT NULL
        GROUP BY cbe.merchant_id, cbe.event_time
    ) merchant_events
    GROUP BY merchant_id, event_time
),
merchant_cb_windows AS (
    SELECT
        merchant_id,
        event_time,
        SUM(chargeback_count) OVER (
            PARTITION BY merchant_id
            ORDER BY event_time
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS prior_chargebacks,
        SUM(order_count) OVER (
            PARTITION BY merchant_id
            ORDER BY event_time
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS prior_orders
    FROM merchant_cb_events
),
merchant_cb_rates AS (
    SELECT
        o2.order_id,
        CASE
            WHEN o2.merchant_id IS NULL THEN NULL
            ELSE mcw.prior_chargebacks::DOUBLE PRECISION / NULLIF(mcw.prior_orders, 0)
        END AS merchant_chargeback_rate
    FROM all_orders o2
    LEFT JOIN merchant_cb_windows mcw ON mcw.merchant_id = o2.merchant_id
        AND mcw.event_time = o2.placed_at
),
email_domain_cb_events AS (
    SELECT
        user_email_domain,
        event_time,
        SUM(order_count) AS order_count,
        SUM(chargeback_count) AS chargeback_count
    FROM (
        SELECT
            o2.user_email_domain,
            o2.placed_at AS event_time,
            COUNT(*) AS order_count,
            0 AS chargeback_count
        FROM all_orders o2
        WHERE o2.user_email_domain IS NOT NULL
        GROUP BY o2.user_email_domain, o2.placed_at
        UNION ALL
        SELECT
            cbe.user_email_domain,
            cbe.event_time,
            0 AS order_count,
            COUNT(*) AS chargeback_count
        FROM chargeback_event_times cbe
        WHERE cbe.user_email_domain IS NOT NULL
        GROUP BY cbe.user_email_domain, cbe.event_time
    ) email_domain_events
    GROUP BY user_email_domain, event_time
),
email_domain_cb_windows AS (
    SELECT
        user_email_domain,
        event_time,
        SUM(chargeback_count) OVER (
            PARTITION BY user_email_domain
            ORDER BY event_time
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS prior_chargebacks,
        SUM(order_count) OVER (
            PARTITION BY user_email_domain
            ORDER BY event_time
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
        ) AS prior_orders
    FROM email_domain_cb_events
),
email_domain_cb_rates AS (
    SELECT
        o2.order_id,
        CASE
            WHEN o2.user_email_domain IS NULL THEN NULL
            ELSE ecw.prior_chargebacks::DOUBLE PRECISION / NULLIF(ecw.prior_orders, 0)
        END AS email_domain_chargeback_rate
    FROM all_orders o2
    LEFT JOIN email_domain_cb_windows ecw ON ecw.user_email_domain = o2.user_email_domain
        AND ecw.event_time = o2.placed_at
),
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
            RANGE BETWEEN INTERVAL '1 hour' PRECEDING AND CURRENT ROW
            EXCLUDE CURRENT ROW
        ) AS user_orders_1h_at_order_time,
        COUNT(*) OVER (
            PARTITION BY o.user_id
            ORDER BY o.placed_at
            RANGE BETWEEN INTERVAL '24 hours' PRECEDING AND CURRENT ROW
            EXCLUDE CURRENT ROW
        ) AS user_orders_24h_at_order_time,
        COALESCE(
            SUM(o.total_pence) OVER (
                PARTITION BY o.user_id
                ORDER BY o.placed_at
                RANGE BETWEEN INTERVAL '24 hours' PRECEDING AND CURRENT ROW
                EXCLUDE CURRENT ROW
            ),
            0
        )::BIGINT AS user_spend_24h_pence,
        CASE
            WHEN o.device_id IS NULL THEN 0
            ELSE COUNT(*) OVER (
                PARTITION BY o.device_id
                ORDER BY o.placed_at
                RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                EXCLUDE CURRENT ROW
            )
        END AS device_lifetime_order_count,
        CASE
            WHEN o.device_id IS NULL THEN 0
            ELSE COALESCE(dou.device_unique_users_lifetime, 0)
        END AS device_unique_users_lifetime,
        COALESCE(pcr.payment_lifetime_chargeback_rate, 0.0)
            AS payment_lifetime_chargeback_rate,
        CASE
            WHEN o.ip_address IS NULL THEN 0
            ELSE COALESCE(ip24.ip_unique_users_24h, 0)
        END AS ip_unique_users_24h,
        COALESCE(scr.store_chargeback_rate, 0.0) AS store_chargeback_rate,
        COALESCE(mcr.merchant_chargeback_rate, 0.0) AS merchant_chargeback_rate,
        COALESCE(ecr.email_domain_chargeback_rate, 0.0)
            AS email_domain_chargeback_rate,
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
    LEFT JOIN dev_unique_users duc ON duc.device_id = o.device_id
        AND duc.user_id = o.user_id
    LEFT JOIN dev_order_unique_users dou ON dou.order_id = o.order_id
    LEFT JOIN ip_24h_distinct ip24 ON ip24.ip_address = o.ip_address
        AND ip24.anchor_placed_at = o.placed_at
    LEFT JOIN payment_cb_rates pcr ON pcr.order_id = o.order_id
    LEFT JOIN store_cb_rates scr ON scr.order_id = o.order_id
    LEFT JOIN merchant_cb_rates mcr ON mcr.order_id = o.order_id
    LEFT JOIN email_domain_cb_rates ecr ON ecr.order_id = o.order_id
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
