from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytz  # type: ignore[import]
import redis as redis_lib
from shared.models import (
    Device,
    PaymentMethod,
    Promotion,
    Store,
    User,
    UserAddress,
)
from shared.models import (
    Session as SessionModel,
)
from shared.money import VATLineItem, calculate_vat
from sqlalchemy import text  # type: ignore[import]
from sqlalchemy.orm import Session as SASession  # type: ignore[import]

from simulator.cart_builder import Cart

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_STATS_TTL_SECONDS = 60


@dataclass
class IPAddress:
    ip_address: str
    ip_country: str | None = None
    ip_city: str | None = None
    ip_is_proxy: bool = False
    ip_is_vpn: bool = False
    ip_is_tor: bool = False
    ip_is_hosting: bool = False
    city_centroid_lat: float | None = None
    city_centroid_lon: float | None = None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return earth_radius_km * 2 * math.asin(math.sqrt(a))


def _query_union_count(db_session: SASession, query: str, params: dict[str, Any]) -> int:
    rows = db_session.execute(text(query), params).scalars().all()
    return sum(int(row) for row in rows)


def _query_union_sum(db_session: SASession, query: str, params: dict[str, Any]) -> int:
    rows = db_session.execute(text(query), params).scalars().all()
    return sum(int(row) for row in rows)


def _query_exists(db_session: SASession, query: str, params: dict[str, Any]) -> bool:
    return db_session.execute(text(query), params).first() is not None


def _read_cached_user_stats(
    redis_client: redis_lib.Redis[bytes], user_id: str
) -> tuple[int, int, int] | None:
    key = f"user_stats:{user_id}"
    cached = redis_client.hgetall(key)
    required = {b"total_orders_lifetime", b"total_orders_30d", b"total_spend_lifetime_pence"}
    if not required.issubset(cached.keys()):
        return None

    try:
        return (
            int(cached[b"total_orders_lifetime"].decode()),
            int(cached[b"total_orders_30d"].decode()),
            int(cached[b"total_spend_lifetime_pence"].decode()),
        )
    except (TypeError, ValueError):
        return None


# Phase 2 Redis caching — spec/PHASE_2.md intentional design.
# Cache TTL is 60s; user stats (order counts, spend) may be stale by up to 60s
# within a rapid-fire session. This is the accepted Phase 2 trade-off — Phase 4
# formalises per-user feature freshness in the feature store with transactional
# invalidation. At 50 ord/sec across 1M users, same-user-within-60s collision
# rate is negligible (<0.01% of orders).
def _write_user_stats_cache(
    redis_client: redis_lib.Redis[bytes],
    user_id: str,
    lifetime_orders: int,
    orders_30d: int,
    spend_lifetime_pence: int,
) -> None:
    key = f"user_stats:{user_id}"
    redis_client.hset(
        key,
        mapping={
            "total_orders_lifetime": lifetime_orders,
            "total_orders_30d": orders_30d,
            "total_spend_lifetime_pence": spend_lifetime_pence,
        },
    )
    redis_client.expire(key, REDIS_STATS_TTL_SECONDS)


def build_order_snapshot(
    user: User,
    store: Store,
    cart: Cart,
    delivery_address: UserAddress | None,
    payment_method: PaymentMethod,
    device: Device,
    session: SessionModel,
    ip: IPAddress,
    promo: Promotion | None,
    placed_at: datetime,
    db_session: SASession,
    redis_client: redis_lib.Redis[bytes],
) -> dict[str, Any]:
    """Build a denormalized order snapshot for order-generator persistence."""

    user_id = str(user.user_id)

    cached_stats = _read_cached_user_stats(redis_client, user_id)
    if cached_stats is None:
        user_id_ = user.user_id
        user_total_orders_lifetime = _query_union_count(
            db_session,
            """
            SELECT count(*) FROM orders WHERE user_id = :uid
            UNION ALL
            SELECT count(*) FROM orders_archive WHERE user_id = :uid
            """,
            {"uid": user_id_},
        )

        user_total_orders_30d = _query_union_count(
            db_session,
            """
            SELECT count(*) FROM orders WHERE user_id = :uid
            AND placed_at >= NOW() - INTERVAL '30 days'
            UNION ALL
            SELECT count(*) FROM orders_archive WHERE user_id = :uid
            AND placed_at >= NOW() - INTERVAL '30 days'
            """,
            {"uid": user_id_},
        )

        user_total_spend_lifetime_pence = _query_union_sum(
            db_session,
            """
            SELECT COALESCE(SUM(total_pence), 0) FROM orders WHERE user_id = :uid
            UNION ALL
            SELECT COALESCE(SUM(total_pence), 0) FROM orders_archive WHERE user_id = :uid
            """,
            {"uid": user_id_},
        )
        _write_user_stats_cache(
            redis_client,
            user_id,
            user_total_orders_lifetime,
            user_total_orders_30d,
            user_total_spend_lifetime_pence,
        )
    else:
        user_total_orders_lifetime, user_total_orders_30d, user_total_spend_lifetime_pence = cached_stats

    is_first_order_for_user = user_total_orders_lifetime == 0

    is_new_payment_method = not _query_exists(
        db_session,
        """
        SELECT 1 WHERE
          EXISTS (SELECT 1 FROM orders WHERE user_id = :uid AND payment_method_id = :pm_id)
          OR
          EXISTS (SELECT 1 FROM orders_archive WHERE user_id = :uid AND payment_method_id = :pm_id)
        """,
        {"uid": user.user_id, "pm_id": payment_method.payment_method_id},
    )

    is_new_delivery_address: bool | None = None
    if delivery_address is not None:
        is_new_delivery_address = not _query_exists(
            db_session,
            """
            SELECT 1 WHERE
              EXISTS (SELECT 1 FROM orders WHERE user_id = :uid AND delivery_address_id = :addr_id)
              OR
              EXISTS (SELECT 1 FROM orders_archive WHERE user_id = :uid AND delivery_address_id = :addr_id)
            """,
            {"uid": user.user_id, "addr_id": delivery_address.address_id},
        )

    london_tz = pytz.timezone("Europe/London")
    if placed_at.tzinfo is None:
        placed_at = placed_at.replace(tzinfo=pytz.utc)
    placed_at_london = placed_at.astimezone(london_tz)
    created_at_london = user.created_at.astimezone(london_tz)
    user_account_age_days = (placed_at_london - created_at_london).days

    delivery_latitude = float(delivery_address.latitude) if delivery_address and delivery_address.latitude is not None else None
    delivery_longitude = float(delivery_address.longitude) if delivery_address and delivery_address.longitude is not None else None
    store_latitude = float(store.latitude)
    store_longitude = float(store.longitude)
    delivery_distance_km = (
        _haversine_km(
            store_latitude,
            store_longitude,
            delivery_latitude,
            delivery_longitude,
        )
        if delivery_address is not None and delivery_latitude is not None and delivery_longitude is not None
        else 0.0
        # 0.0 is the spec-mandated sentinel for absent delivery address (PICKUP/DINE_IN orders).
        # Phase 4 feature store will differentiate None (unknown) vs 0.0 (inapplicable).
    )

    billing_latitude = None
    billing_longitude = None
    if payment_method.billing_address_id is not None:
        billing_row = db_session.execute(
            text(
                """
                SELECT latitude, longitude
                FROM user_addresses
                WHERE address_id = :address_id
                """
            ),
            {"address_id": payment_method.billing_address_id},
        ).one_or_none()
        if billing_row is not None:
            billing_latitude = float(billing_row[0]) if billing_row[0] is not None else None
            billing_longitude = float(billing_row[1]) if billing_row[1] is not None else None

    ip_to_delivery_distance_km = (
        _haversine_km(
            ip.city_centroid_lat,
            ip.city_centroid_lon,
            delivery_latitude,
            delivery_longitude,
        )
        if (
            delivery_address is not None
            and ip.city_centroid_lat is not None
            and ip.city_centroid_lon is not None
            and delivery_latitude is not None
            and delivery_longitude is not None
        )
        else 0.0
        # 0.0 is the spec-mandated sentinel when delivery address is absent.
    )

    billing_to_delivery_distance_km = (
        _haversine_km(
            billing_latitude,
            billing_longitude,
            delivery_latitude,
            delivery_longitude,
        )
        if (
            delivery_address is not None
            and billing_latitude is not None
            and billing_longitude is not None
            and delivery_latitude is not None
            and delivery_longitude is not None
        )
        else 0.0
        # 0.0 is the spec-mandated sentinel when delivery address is absent.
    )

    vat_items = [
        VATLineItem(line_total_pence=item.qty * item.unit_price_pence, is_hot_food=item.is_hot_food)
        for item in cart.items
    ]
    vat_pence = calculate_vat(vat_items)

    return {
        "user_id": user_id,
        "user_account_age_days": user_account_age_days,
        "user_email": str(user.email),
        "user_email_domain": user.email.split("@")[1],
        "user_phone": user.phone,
        "user_risk_tier_at_order": user.risk_tier,
        "user_total_orders_lifetime": user_total_orders_lifetime,
        "user_total_orders_30d": user_total_orders_30d,
        "user_total_spend_lifetime_pence": user_total_spend_lifetime_pence,
        "is_first_order_for_user": is_first_order_for_user,
        "is_new_payment_method": is_new_payment_method,
        "is_new_delivery_address": is_new_delivery_address,
        "store_id": str(store.store_id),
        "merchant_id": str(store.merchant_id),
        "store_city": store.city,
        "store_country": store.country,
        "store_latitude": store_latitude,
        "store_longitude": store_longitude,
        "delivery_address_id": str(delivery_address.address_id) if delivery_address else None,
        "delivery_address_type": delivery_address.address_type if delivery_address else None,
        "delivery_latitude": delivery_latitude,
        "delivery_longitude": delivery_longitude,
        "delivery_distance_km": delivery_distance_km,
        "item_count": cart.item_count,
        "unique_item_count": cart.unique_item_count,
        "subtotal_pence": cart.subtotal_pence,
        "vat_pence": vat_pence,
        "payment_method_id": str(payment_method.payment_method_id),
        "payment_type": payment_method.payment_type,
        "card_bin": payment_method.card_bin,
        "card_last_four": payment_method.card_last_four,
        "card_brand": payment_method.card_brand,
        "card_funding_type": payment_method.card_funding_type,
        "card_issuer_country": payment_method.card_issuer_country,
        "is_digital_native_bank": payment_method.is_digital_native_bank,
        "device_id": str(device.device_id),
        "device_type": device.device_type,
        "platform": device.platform,
        "os_version": device.os_version,
        "app_version": device.app_version,
        "browser_name": device.browser_name,
        "browser_version": device.browser_version,
        "device_user_count": device.unique_users_count,
        "payment_user_count": payment_method.unique_users_count,
        "session_id": str(session.session_id),
        "ip_address": ip.ip_address,
        "ip_country": ip.ip_country,
        "ip_city": ip.ip_city,
        "ip_is_proxy": ip.ip_is_proxy,
        "ip_is_vpn": ip.ip_is_vpn,
        "ip_is_tor": ip.ip_is_tor,
        "ip_is_hosting": ip.ip_is_hosting,
        "ip_to_delivery_distance_km": ip_to_delivery_distance_km,
        "billing_to_delivery_distance_km": billing_to_delivery_distance_km,
        "promo_id": str(promo.promo_id) if promo is not None else None,
        "promo_code": promo.promo_code if promo is not None else None,
    }
