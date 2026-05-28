from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from shared.money import VATLineItem, calculate_total, calculate_vat

from simulator.cart_builder import Cart, UserProfile, build_realistic_cart
from simulator.fraud_patterns import GroundTruth, generate_fraud_order
from simulator.fraud_patterns.account_takeover import _IP_COUNTRY_RESOLUTION, _OTHER_ISO2_POOL
from simulator.fraud_patterns.collusive_merchant import init_collusive_stores
from simulator.fraud_patterns.promo_abuse import init_rings as init_promo_abuse_rings
from simulator.fraud_patterns.reseller import init_reseller_accounts
from simulator.fraud_patterns.stolen_card import FraudPatternContext
from simulator.fraud_patterns.triangulation import init_accounts as init_triangulation_accounts
from simulator.user_picker import WeightedUserPicker

FORCE_PEAK = os.getenv("SIMULATION_FORCE_PEAK", "false").lower() in {"true", "1", "yes"}

logger = logging.getLogger(__name__)


def _parse_fraud_rate(raw: str | None, default: float = 0.02) -> float:
    if not raw:
        return default

    try:
        rate = float(raw)
    except ValueError:
        logger.warning(
            "invalid_fraud_injection_rate raw=%r falling_back_to=%s",
            raw,
            default,
        )
        return default

    return max(0.0, min(1.0, rate))


DATABASE_URL_SIMULATOR = os.environ.get(
    "DATABASE_URL_SIMULATOR",
    "postgresql://simulator_user:simulator_dev_password@postgres:5432/fraud_platform",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
FRAUD_INJECTION_RATE = _parse_fraud_rate(os.getenv("FRAUD_INJECTION_RATE"))

LONDON_TZ = ZoneInfo("Europe/London")
_FALLBACK_OTHER_CARD_COUNTRY = _OTHER_ISO2_POOL[0]
_FRAUD_FK_SENTINELS = {"VICTIM_SAVED", "ABUSER_SAVED"}
_SYNTHETIC_FRAUD_RING_MERCHANT_ID = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
_SYNTHETIC_FRAUD_RING_STORE_CITY = "FRAUD_RING"
_SYNTHETIC_FRAUD_RING_STORE_COUNTRY = "GB"
_SYNTHETIC_FRAUD_RING_STORE_LATITUDE = 0.0
_SYNTHETIC_FRAUD_RING_STORE_LONGITUDE = 0.0

_UK_ISP_PREFIXES: list[str] = [
    "80.0",
    "82.0",
    "86.0",
    "88.0",
    "90.0",
    "92.0",
    "94.0",
    "5.64",
    "5.65",
    "193.0",
    "194.0",
    "195.0",
    "109.144",
    "109.145",
    "109.146",
]


def _resolve_card_country(raw: str | None) -> str:
    if not raw:
        return "GB"

    if len(raw) == 2 and raw.isalpha():
        return raw.upper()

    if raw in _IP_COUNTRY_RESOLUTION:
        resolved = _IP_COUNTRY_RESOLUTION[raw] or _FALLBACK_OTHER_CARD_COUNTRY
    elif raw == "foreign_other":
        resolved = _FALLBACK_OTHER_CARD_COUNTRY
    else:
        resolved = raw

    normalized = resolved.upper()
    if len(normalized) == 2:
        return normalized

    logger.warning(
        "invalid_fraud_card_country raw=%r falling_back_to=GB",
        raw,
    )
    return "GB"


def _fraud_uuid_override(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, str) and value in _FRAUD_FK_SENTINELS:
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _fraud_user_id_override(fraud_dict: dict[str, Any]) -> uuid.UUID | None:
    user_id_override = fraud_dict.get("user_id") or fraud_dict.get("victim_user_id")
    return _fraud_uuid_override(user_id_override)


@dataclass(frozen=True)
class GeneratorConfig:
    orders_per_second: int = 1
    simulation_time_compression: int = 1
    scoring_enabled: bool = False


@dataclass(frozen=True)
class _MenuItemForCart:
    item_id: uuid.UUID
    item_name: str
    category: str
    price_pence: int
    is_hot_food: bool


def load_config_from_env() -> GeneratorConfig:
    orders_per_second_raw = os.getenv("ORDERS_PER_SECOND", "1")
    simulation_time_compression_raw = os.getenv("SIMULATION_TIME_COMPRESSION", "1")
    scoring_enabled_raw = os.getenv("SCORING_ENABLED", "false").strip().lower()

    try:
        orders_per_second = int(orders_per_second_raw)
    except ValueError:
        orders_per_second = 1

    try:
        simulation_time_compression = int(simulation_time_compression_raw)
    except ValueError:
        simulation_time_compression = 1

    if orders_per_second < 1:
        orders_per_second = 1
    if simulation_time_compression < 1:
        simulation_time_compression = 1

    scoring_enabled = scoring_enabled_raw in {"1", "true", "yes", "on", "y", "t"}
    return GeneratorConfig(
        orders_per_second=orders_per_second,
        simulation_time_compression=simulation_time_compression,
        scoring_enabled=scoring_enabled,
    )


async def load_stores_by_city(pool: asyncpg.Pool) -> dict[str, list[dict[str, Any]]]:
    rows = await pool.fetch(
        """
        SELECT store_id, store_name, city, latitude, longitude, cuisine_types, price_tier,
               accepts_in_store, accepts_delivery, accepts_pickup, is_active, delivery_radius_km,
               merchant_id, country
        FROM stores
        WHERE is_active = true
        """,
    )

    stores_by_city: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        city = str(row["city"]) if row["city"] is not None else ""
        stores_by_city.setdefault(city, []).append(dict(row))
    return stores_by_city


async def load_store_hours(pool: asyncpg.Pool) -> dict[uuid.UUID, list[dict[str, Any]]]:
    rows = await pool.fetch(
        """
        SELECT store_id, day_of_week, open_time, close_time
        FROM store_hours
        ORDER BY store_id, day_of_week, open_time
        """,
    )

    store_hours_by_store_id: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for row in rows:
        store_id = row["store_id"]
        store_hours_by_store_id.setdefault(store_id, []).append(dict(row))
    return store_hours_by_store_id


async def load_active_promos(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        """
        SELECT promo_id, promo_code, promo_type, discount_amount_pence, discount_percent,
               min_order_pence, max_redemptions_per_user, valid_from, valid_until, is_targeted,
               (promo_type = 'NEW_USER') as is_new_user_only
        FROM promotions
        WHERE (valid_until IS NULL OR valid_until > NOW())
          AND (valid_from IS NULL OR valid_from <= NOW())
        """,
    )
    return [dict(row) for row in rows]


def _is_new_user_only_promo(promo: dict[str, Any]) -> bool:
    is_new_user_only = promo.get("is_new_user_only")
    if isinstance(is_new_user_only, bool):
        return is_new_user_only

    promo_type = promo.get("promo_type")
    return isinstance(promo_type, str) and promo_type.upper() == "NEW_USER"


async def _promo_redemption_count(
    conn: asyncpg.Connection,
    user_id: uuid.UUID,
    promo_code: str,
) -> int:
    redemptions = await conn.fetchval(
        """
        SELECT COUNT(*)
        FROM (
            SELECT 1 FROM orders WHERE user_id=$1 AND promo_code=$2
            UNION ALL
            SELECT 1 FROM orders_archive WHERE user_id=$1 AND promo_code=$2
        ) AS promo_redemptions
        """,
        user_id,
        promo_code,
    )
    return int(redemptions)


async def load_user_data(
    conn: asyncpg.Connection,
    user_id: uuid.UUID,
) -> dict[str, Any]:
    user_row = await conn.fetchrow(
        "SELECT user_id, email, phone, risk_tier, signup_postcode, created_at "
        "FROM users WHERE user_id=$1",
        user_id,
    )
    if user_row is None:
        raise RuntimeError(f"user not found: {user_id}")

    address_rows = await conn.fetch(
        "SELECT address_id, label, city, postcode, latitude, longitude, is_default, address_type "
        "FROM user_addresses WHERE user_id=$1 ORDER BY is_default DESC",
        user_id,
    )
    payment_rows = await conn.fetch(
        "SELECT payment_method_id, payment_type, card_bin, card_last_four, card_brand, "
        "card_funding_type, card_issuer_country, is_digital_native_bank, is_default, "
        "billing_address_id, unique_users_count "
        "FROM payment_methods WHERE user_id=$1 AND status='ACTIVE' ORDER BY is_default DESC",
        user_id,
    )
    device_rows = await conn.fetch(
        "SELECT d.device_id, d.device_type, d.platform, d.os_version, d.app_version, "
        "d.browser_name, d.browser_version, d.unique_users_count "
        "FROM devices d JOIN user_devices ud ON d.device_id=ud.device_id "
        "WHERE ud.user_id=$1 ORDER BY ud.last_used_at DESC LIMIT 5",
        user_id,
    )

    addresses = [dict(row) for row in address_rows]
    payment_methods = [dict(row) for row in payment_rows]
    devices = [dict(row) for row in device_rows]

    default_address = next((address for address in addresses if address.get("is_default")), None)
    if default_address is None and addresses:
        default_address = addresses[0]

    default_payment = next(
        (payment for payment in payment_methods if payment.get("is_default")),
        None,
    )
    if default_payment is None and payment_methods:
        default_payment = payment_methods[0]

    return {
        "user": dict(user_row),
        "addresses": addresses,
        "payment_methods": payment_methods,
        "devices": devices,
        "default_address": default_address,
        "default_payment": default_payment,
    }


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return earth_radius_km * 2 * math.asin(math.sqrt(a))


def _default_user_location(
    user_data: dict[str, Any],
) -> tuple[float | None, float | None, str | None]:
    default_address = user_data.get("default_address")
    if not isinstance(default_address, dict):
        return None, None, None

    city = default_address.get("city")
    lat = default_address.get("latitude")
    lon = default_address.get("longitude")
    if lat is None or lon is None:
        return None, None, city if isinstance(city, str) else None

    return float(lat), float(lon), city if isinstance(city, str) else None


def pick_store_for_user(
    rng: random.Random,
    user_data: dict[str, Any],
    stores_by_city: dict[str, list[dict[str, Any]]],
    store_hours_by_store_id: dict[uuid.UUID, list[dict[str, Any]]],
) -> dict[str, Any]:
    default_lat, default_lon, user_city = _default_user_location(user_data)

    candidate_map: dict[uuid.UUID, dict[str, Any]] = {}
    if user_city is not None and user_city in stores_by_city:
        for store in stores_by_city[user_city]:
            candidate_map[store["store_id"]] = store

    if default_lat is not None and default_lon is not None:
        for store_list in stores_by_city.values():
            for store in store_list:
                store_lat = store.get("latitude")
                store_lon = store.get("longitude")
                if store_lat is None or store_lon is None:
                    continue
                distance = haversine_km(
                    default_lat,
                    default_lon,
                    float(store_lat),
                    float(store_lon),
                )
                if distance <= 15.0:
                    candidate_map[store["store_id"]] = store

    if not candidate_map:
        for store_list in stores_by_city.values():
            for store in store_list:
                candidate_map[store["store_id"]] = store

    stores = list(candidate_map.values())
    if FORCE_PEAK:
        weekday = 5
        prev_weekday = 4
        current_time = (
            datetime.now(tz=LONDON_TZ).replace(hour=19, minute=0, second=0, microsecond=0).time()
        )
    else:
        now = datetime.now(tz=LONDON_TZ)
        weekday = now.isoweekday() % 7
        prev_weekday = (weekday - 1) % 7
        current_time = now.time()
    open_stores = []
    for store in stores:
        store_id = store["store_id"]
        for row in store_hours_by_store_id.get(store_id, []):
            open_t = row["open_time"]
            close_t = row["close_time"]
            dow = row["day_of_week"]
            if close_t < open_t:
                if dow == weekday and current_time >= open_t:
                    open_stores.append(store)
                    break
                if dow == prev_weekday and current_time <= close_t:
                    open_stores.append(store)
                    break
            elif dow == weekday and open_t <= current_time <= close_t:
                open_stores.append(store)
                break

    if open_stores:
        stores = open_stores
    elif not stores:
        raise RuntimeError("no active stores available")
    else:
        raise RuntimeError("no stores in current open-hours window")

    weights: list[float] = []
    for store in stores:
        store_lat = store.get("latitude")
        store_lon = store.get("longitude")
        if (
            default_lat is not None
            and default_lon is not None
            and store_lat is not None
            and store_lon is not None
        ):
            distance_km = haversine_km(default_lat, default_lon, float(store_lat), float(store_lon))
        else:
            distance_km = 0.0
        weights.append(1.0 / (distance_km + 0.1))

    return rng.choices(stores, weights=weights, k=1)[0]


def pick_channel_for_user(rng: random.Random, user_devices: list[dict[str, Any]]) -> str:
    has_ios = any((device.get("platform") or "").lower() == "ios" for device in user_devices)
    has_android = any(
        (device.get("platform") or "").lower() == "android" for device in user_devices
    )

    if has_ios:
        roll = rng.random()
        if roll < 0.80:
            return "IOS_APP"
        if roll < 0.95:
            return "MOBILE_WEB"
        return "DESKTOP_WEB"

    if has_android:
        roll = rng.random()
        if roll < 0.80:
            return "ANDROID_APP"
        if roll < 0.95:
            return "MOBILE_WEB"
        return "DESKTOP_WEB"

    return "DESKTOP_WEB" if rng.random() < 0.60 else "MOBILE_WEB"


def _random_uk_ip(rng: random.Random) -> str:
    prefix = rng.choice(_UK_ISP_PREFIXES)
    parts = prefix.split(".")
    if len(parts) == 2:
        return f"{parts[0]}.{parts[1]}.{rng.randint(0, 255)}.{rng.randint(0, 255)}"

    return f"{parts[0]}.{parts[1]}.{parts[2]}.{rng.randint(0, 255)}"


def pick_device_and_ip(
    rng: random.Random,
    user_devices: list[dict[str, Any]],
    user_city: str | None,
) -> tuple[dict[str, Any], str]:
    _ = user_city

    if user_devices and rng.random() < 0.92:
        device = dict(rng.choice(user_devices))
    else:
        device = {
            "device_id": uuid.uuid4(),
            "device_type": "MOBILE_APP",
            "platform": rng.choice(["iOS", "Android"]),
            "os_version": "17.0",
            "app_version": "4.31.0",
            "browser_name": None,
            "browser_version": None,
            "unique_users_count": 1,
        }

    return device, _random_uk_ip(rng)


def generate_order_number(rng: random.Random) -> str:
    _ = rng
    order_year = datetime.now(tz=LONDON_TZ).year
    suffix = base64.b32encode(uuid.uuid4().bytes).decode()[0:10]
    return f"JE-{order_year}-{suffix}"


def compute_pricing(
    cart_subtotal_pence: int,
    distance_km: float,
    rng: random.Random,
    order_type: str = "DELIVERY",
) -> tuple[int, int, int, int]:
    if order_type == "DELIVERY":
        delivery_fee = 250 + min(249, int(distance_km * 20))
        if delivery_fee < 250:
            delivery_fee = 250
        elif delivery_fee > 499:
            delivery_fee = 499
    else:
        delivery_fee = 0

    service_fee = min(250, int(cart_subtotal_pence * 0.10))

    tip = 0
    if rng.random() < 0.20:
        tip = round(cart_subtotal_pence * rng.uniform(0.05, 0.15))

    return delivery_fee, service_fee, tip, 0


async def apply_promo(
    conn: asyncpg.Connection,
    user_id: uuid.UUID,
    rng: random.Random,
    is_first_order: bool,
    eligible_promos: list[dict[str, Any]],
    subtotal_pence: int,
) -> dict[str, Any] | None:
    eligible_promos = [
        promo for promo in eligible_promos if (promo.get("min_order_pence") or 0) <= subtotal_pence
    ]

    if not eligible_promos:
        return None

    filtered_promos: list[dict[str, Any]] = []
    for promo in eligible_promos:
        promo_code = promo.get("promo_code")
        if not isinstance(promo_code, str):
            continue
        max_redemptions_per_user = promo.get("max_redemptions_per_user")
        if max_redemptions_per_user is None:
            filtered_promos.append(promo)
            continue

        # Read-then-apply check; race condition under concurrency is bounded by the
        # 100-task semaphore + this being a local-dev simulator (single-process, no
        # multi-instance). Production would use SELECT FOR UPDATE or unique-constraint
        # enforcement. See follow-up packet for atomic enforcement if needed.
        already_redeemed = await _promo_redemption_count(
            conn=conn,
            user_id=user_id,
            promo_code=promo_code,
        )
        if already_redeemed < int(max_redemptions_per_user):
            filtered_promos.append(promo)

    eligible_promos = filtered_promos
    if not eligible_promos:
        return None

    if not is_first_order:
        eligible_promos = [promo for promo in eligible_promos if not _is_new_user_only_promo(promo)]
        if not eligible_promos:
            return None

    if is_first_order:
        if rng.random() >= 0.80:
            return None
        for promo in eligible_promos:
            if promo.get("promo_code") == "WELCOME10":
                return promo
        return None

    if rng.random() >= 0.05:
        return None

    return rng.choice(eligible_promos)


def _promo_discount(promo: dict[str, Any] | None, subtotal_pence: int) -> int:
    if promo is None:
        return 0

    discount_amount = promo.get("discount_amount_pence")
    if discount_amount is not None:
        return min(int(discount_amount), subtotal_pence)

    discount_percent = promo.get("discount_percent")
    if discount_percent is None:
        return 0

    discount_percent_decimal = Decimal(str(discount_percent))
    discount = int(
        (discount_percent_decimal * Decimal(subtotal_pence) / Decimal("100")).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )
    return min(discount, subtotal_pence)


async def _read_user_order_metrics(
    conn: asyncpg.Connection,
    user_id: uuid.UUID,
) -> tuple[int, int, int]:
    row = await conn.fetchrow(
        """
        SELECT
          (COALESCE((SELECT COUNT(*) FROM orders WHERE user_id=$1), 0)
           + COALESCE((SELECT COUNT(*) FROM orders_archive WHERE user_id=$1), 0)) AS total_orders,
          (COALESCE((SELECT COUNT(*) FROM orders WHERE user_id=$1
           AND placed_at >= NOW() - INTERVAL '30 days'), 0)
           + COALESCE((SELECT COUNT(*) FROM orders_archive WHERE user_id=$1
           AND placed_at >= NOW() - INTERVAL '30 days'), 0)) AS total_30d,
          (COALESCE(
            (SELECT COALESCE(SUM(total_pence), 0) FROM orders WHERE user_id=$1), 0)
           + COALESCE(
            (SELECT COALESCE(SUM(total_pence), 0) FROM orders_archive WHERE user_id=$1), 0))
           AS total_spend
        """,
        user_id,
    )
    if row is None:
        return 0, 0, 0

    return int(row["total_orders"]), int(row["total_30d"]), int(row["total_spend"])


async def _is_new_payment_method(
    conn: asyncpg.Connection,
    user_id: uuid.UUID,
    payment_method_id: uuid.UUID,
) -> bool:
    previous_uses = await conn.fetchval(
        """
        SELECT EXISTS(
            SELECT 1 FROM orders WHERE user_id=$1 AND payment_method_id=$2
            UNION ALL
            SELECT 1 FROM orders_archive WHERE user_id=$1 AND payment_method_id=$2
        )
        """,
        user_id,
        payment_method_id,
    )
    return not bool(previous_uses)


async def _is_new_delivery_address(
    conn: asyncpg.Connection,
    user_id: uuid.UUID,
    address_id: uuid.UUID,
) -> bool:
    previous_uses = await conn.fetchval(
        """
        SELECT EXISTS(
            SELECT 1 FROM orders WHERE user_id=$1 AND delivery_address_id=$2
            UNION ALL
            SELECT 1 FROM orders_archive WHERE user_id=$1 AND delivery_address_id=$2
        )
        """,
        user_id,
        address_id,
    )
    return not bool(previous_uses)


async def _insert_ephemeral_payment_method(
    conn: asyncpg.Connection,
    user_id: uuid.UUID,
    rng: random.Random,
) -> dict[str, Any]:
    card_bin = f"{rng.randint(100000, 999999):06d}"
    card_last_four = f"{rng.randint(1000, 9999):04d}"

    row = await conn.fetchrow(
        """
        INSERT INTO payment_methods (
            user_id, payment_type, card_bin, card_last_four, card_brand,
            card_funding_type, card_issuer_country, is_digital_native_bank
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING payment_method_id, payment_type, card_bin, card_last_four, card_brand,
                  card_funding_type, card_issuer_country, is_digital_native_bank,
                  unique_users_count
        """,
        user_id,
        "CREDIT_CARD",
        card_bin,
        card_last_four,
        rng.choice(["VISA", "MASTERCARD", "AMEX"]),
        rng.choice(["DEBIT", "CREDIT"]),
        "GB",
        False,
    )
    if row is None:
        raise RuntimeError("payment method insert returned no row")

    return dict(row)


async def _load_menu_items(
    conn: asyncpg.Connection,
    store_id: uuid.UUID,
) -> list[_MenuItemForCart]:
    rows = await conn.fetch(
        "SELECT item_id, item_name, category, price_pence, is_hot_food "
        "FROM menu_items WHERE store_id=$1 AND is_available=true",
        store_id,
    )

    return [
        _MenuItemForCart(
            item_id=row["item_id"],
            item_name=row["item_name"],
            category=row["category"] or "",
            price_pence=int(row["price_pence"]),
            is_hot_food=bool(row["is_hot_food"]),
        )
        for row in rows
    ]


def _select_order_type(rng: random.Random, store: dict[str, Any]) -> str:
    roll = rng.random()

    if roll < 0.75:
        if bool(store.get("accepts_delivery", True)):
            return "DELIVERY"
        if bool(store.get("accepts_pickup", True)):
            return "PICKUP"
        if bool(store.get("accepts_in_store", True)):
            return "DINE_IN"
        raise RuntimeError("no eligible order type for store")

    if roll < 0.95:
        if bool(store.get("accepts_pickup", True)):
            return "PICKUP"
        if bool(store.get("accepts_delivery", True)):
            return "DELIVERY"
        if bool(store.get("accepts_in_store", True)):
            return "DINE_IN"
        raise RuntimeError("no eligible order type for store")

    if bool(store.get("accepts_in_store", True)):
        return "DINE_IN"
    if bool(store.get("accepts_delivery", True)):
        return "DELIVERY"
    if bool(store.get("accepts_pickup")):
        return "PICKUP"
    raise RuntimeError("no eligible order type for store")


def _select_delivery_address(
    rng: random.Random,
    default_address: dict[str, Any] | None,
    addresses: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not addresses:
        return None

    selected_default = default_address if default_address is not None else addresses[0]
    others = [
        address
        for address in addresses
        if address.get("address_id") != selected_default.get("address_id")
    ]

    roll = rng.random()
    if roll < 0.80:
        return selected_default
    if roll < 0.95 and others:
        return rng.choice(others)
    return None


def _distance_km_for_delivery(
    store: dict[str, Any],
    delivery_address: dict[str, Any] | None,
) -> float:
    if delivery_address is None:
        return 0.0

    store_lat = store.get("latitude")
    store_lon = store.get("longitude")
    if store_lat is None or store_lon is None:
        return 0.0

    delivery_lat = delivery_address.get("latitude")
    delivery_lon = delivery_address.get("longitude")
    if delivery_lat is None or delivery_lon is None:
        return 0.0

    return haversine_km(
        float(store_lat),
        float(store_lon),
        float(delivery_lat),
        float(delivery_lon),
    )


def _address_to_json(addr: dict[str, Any]) -> dict[str, Any]:
    """Convert asyncpg-returned address dict to JSON-serializable form."""
    result: dict[str, Any] = {}
    for key, val in addr.items():
        if isinstance(val, uuid.UUID):
            result[key] = str(val)
        elif isinstance(val, Decimal):
            result[key] = float(val)
        elif isinstance(val, (datetime, date)):
            result[key] = val.isoformat()
        else:
            result[key] = val
    return result


def _build_snapshot(
    user: dict[str, Any],
    store: dict[str, Any],
    cart: Cart,
    delivery_address: dict[str, Any] | None,
    payment_method: dict[str, Any],
    device: dict[str, Any],
    ip_address: str,
    order_type: str,
    order_channel: str,
    promo: dict[str, Any] | None,
    applied_discount: int,
    pricing_tuple: tuple[int, int, int, int],
    user_total_orders_lifetime: int,
    user_total_orders_30d: int,
    user_total_spend_lifetime_pence: int,
    is_new_address: bool | None,
    is_new_payment_method: bool,
    rng: random.Random,
) -> dict[str, Any]:
    delivery_fee_pence, service_fee_pence, tip_pence, _ = pricing_tuple
    delivery_distance_km = _distance_km_for_delivery(store, delivery_address)

    user_created_at = user.get("created_at")
    placed_at = datetime.now(tz=LONDON_TZ)
    if user_created_at is not None:
        user_account_age_days = (placed_at - user_created_at.astimezone(LONDON_TZ)).days
    else:
        user_account_age_days = 0

    email = str(user["email"])
    user_email_domain = email.split("@", 1)[1] if "@" in email else "unknown"

    vat_items = [
        VATLineItem(
            line_total_pence=item.qty * item.unit_price_pence,
            is_hot_food=item.is_hot_food,
        )
        for item in cart.items
    ] + [
        VATLineItem(
            line_total_pence=delivery_fee_pence,
            is_hot_food=True,
        ),
        VATLineItem(
            line_total_pence=service_fee_pence,
            is_hot_food=True,
        ),
    ]
    vat_pence = calculate_vat(vat_items)

    subtotal_pence = cart.subtotal_pence
    total_pence = calculate_total(
        subtotal_pence,
        vat_pence,
        delivery_fee_pence,
        service_fee_pence,
        tip_pence,
        applied_discount,
    )

    return {
        "order_channel": order_channel,
        "order_type": order_type,
        "user_id": user["user_id"],
        "user_account_age_days": max(user_account_age_days, 0),
        "user_total_orders_lifetime": user_total_orders_lifetime,
        "user_total_orders_30d": user_total_orders_30d,
        "user_total_spend_lifetime_pence": user_total_spend_lifetime_pence,
        "user_email": email,
        "user_email_domain": user_email_domain,
        "user_phone": user.get("phone"),
        "user_risk_tier_at_order": user.get("risk_tier"),
        "is_guest_checkout": False,
        "store_id": store["store_id"],
        "merchant_id": store["merchant_id"],
        "store_city": store["city"],
        "store_country": store.get("country", "GB"),
        "store_latitude": float(store["latitude"]),
        "store_longitude": float(store["longitude"]),
        "delivery_address_id": delivery_address["address_id"] if delivery_address else None,
        "delivery_address_snapshot": json.dumps(_address_to_json(delivery_address))
        if delivery_address
        else None,
        "delivery_latitude": (
            float(delivery_address["latitude"])
            if (delivery_address is not None and delivery_address.get("latitude") is not None)
            else None
        ),
        "delivery_longitude": (
            float(delivery_address["longitude"])
            if (delivery_address is not None and delivery_address.get("longitude") is not None)
            else None
        ),
        "delivery_distance_km": delivery_distance_km,
        "delivery_address_type": delivery_address.get("address_type") if delivery_address else None,
        "is_new_delivery_address": is_new_address,
        "delivery_address_use_count": (
            delivery_address.get("times_used") if delivery_address else None
        ),
        "item_count": cart.item_count,
        "unique_item_count": cart.unique_item_count,
        "subtotal_pence": subtotal_pence,
        "vat_pence": vat_pence,
        "delivery_fee_pence": delivery_fee_pence,
        "service_fee_pence": service_fee_pence,
        "tip_pence": tip_pence,
        "discount_pence": applied_discount,
        "total_pence": total_pence,
        "promo_id": promo["promo_id"] if promo else None,
        "promo_code": promo["promo_code"] if promo else None,
        "is_first_order_for_user": user_total_orders_lifetime == 0,
        "is_new_user_promo": (
            promo is not None
            and isinstance(promo.get("promo_code"), str)
            and promo["promo_code"].startswith("WELCOME")
        ),
        "payment_method_id": payment_method["payment_method_id"],
        "payment_type": payment_method["payment_type"],
        "card_bin": payment_method.get("card_bin"),
        "card_last_four": payment_method.get("card_last_four"),
        "card_brand": payment_method.get("card_brand"),
        "card_funding_type": payment_method.get("card_funding_type"),
        "card_issuer_country": payment_method.get("card_issuer_country"),
        "is_digital_native_bank": payment_method.get("is_digital_native_bank"),
        "is_new_payment_method": is_new_payment_method,
        "session_id": None,
        "device_id": device.get("device_id"),
        "device_type": device.get("device_type"),
        "platform": device.get("platform"),
        "os_version": device.get("os_version"),
        "app_version": device.get("app_version"),
        "browser_name": device.get("browser_name"),
        "browser_version": device.get("browser_version"),
        "ip_address": ip_address,
        "ip_country": "GB",
        "ip_city": user.get("signup_postcode"),
        "ip_is_proxy": False,
        "ip_is_vpn": False,
        "ip_is_tor": False,
        "ip_is_hosting": False,
        "device_user_count": int(device.get("unique_users_count", 0)),
        "payment_user_count": int(payment_method.get("unique_users_count", 0)),
        "ip_to_delivery_distance_km": 0.0,
        "billing_to_delivery_distance_km": 0.0,
        "time_to_checkout_seconds": rng.randint(18, 95),
        "cart_modifications_count": 0,
    }


def _event_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": "ORDER_PLACED",
        "order_id": str(snapshot["order_id"]),
        "order_number": snapshot["order_number"],
        "user_id": str(snapshot["user_id"]),
        "store_id": str(snapshot["store_id"]),
        "order_channel": snapshot["order_channel"],
        "order_type": snapshot["order_type"],
    }


async def insert_order(
    conn: asyncpg.Connection,
    snapshot: dict[str, Any],
    cart: Cart,
    placed_at: datetime,
    *,
    is_fraud: bool = False,
    fraud_category: str | None = None,
    pattern_notes: str | None = None,
    ring_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, datetime]:
    order_row = {
        "order_id": uuid.uuid4(),
        "placed_at": placed_at,
        "order_status": "PLACED",
    }
    order_row.update(snapshot)

    order_columns = [
        "order_id",
        "order_number",
        "order_status",
        "order_channel",
        "order_type",
        "placed_at",
        "user_id",
        "user_account_age_days",
        "user_total_orders_lifetime",
        "user_total_orders_30d",
        "user_total_spend_lifetime_pence",
        "user_email",
        "user_email_domain",
        "user_phone",
        "user_risk_tier_at_order",
        "is_guest_checkout",
        "store_id",
        "merchant_id",
        "store_city",
        "store_country",
        "store_latitude",
        "store_longitude",
        "delivery_address_id",
        "delivery_address_snapshot",
        "delivery_latitude",
        "delivery_longitude",
        "delivery_distance_km",
        "delivery_address_type",
        "is_new_delivery_address",
        "delivery_address_use_count",
        "item_count",
        "unique_item_count",
        "subtotal_pence",
        "vat_pence",
        "delivery_fee_pence",
        "service_fee_pence",
        "tip_pence",
        "discount_pence",
        "total_pence",
        "currency",
        "promo_id",
        "promo_code",
        "is_first_order_for_user",
        "is_new_user_promo",
        "payment_method_id",
        "payment_type",
        "card_bin",
        "card_last_four",
        "card_brand",
        "card_funding_type",
        "card_issuer_country",
        "is_digital_native_bank",
        "is_new_payment_method",
        "avs_result",
        "cvv_result",
        "session_id",
        "device_id",
        "device_type",
        "platform",
        "os_version",
        "app_version",
        "browser_name",
        "browser_version",
        "ip_address",
        "ip_country",
        "ip_city",
        "ip_is_proxy",
        "ip_is_vpn",
        "ip_is_tor",
        "ip_is_hosting",
        "device_user_count",
        "payment_user_count",
        "ip_to_delivery_distance_km",
        "billing_to_delivery_distance_km",
        "time_to_checkout_seconds",
        "cart_modifications_count",
    ]

    order_values = [
        order_row["order_id"],
        order_row["order_number"],
        order_row["order_status"],
        order_row["order_channel"],
        order_row["order_type"],
        order_row["placed_at"],
        order_row["user_id"],
        order_row["user_account_age_days"],
        order_row["user_total_orders_lifetime"],
        order_row["user_total_orders_30d"],
        order_row["user_total_spend_lifetime_pence"],
        order_row["user_email"],
        order_row["user_email_domain"],
        order_row["user_phone"],
        order_row["user_risk_tier_at_order"],
        order_row["is_guest_checkout"],
        order_row["store_id"],
        order_row["merchant_id"],
        order_row["store_city"],
        order_row["store_country"],
        order_row["store_latitude"],
        order_row["store_longitude"],
        order_row["delivery_address_id"],
        order_row["delivery_address_snapshot"],
        order_row["delivery_latitude"],
        order_row["delivery_longitude"],
        order_row["delivery_distance_km"],
        order_row["delivery_address_type"],
        order_row["is_new_delivery_address"],
        order_row["delivery_address_use_count"],
        order_row["item_count"],
        order_row["unique_item_count"],
        order_row["subtotal_pence"],
        order_row["vat_pence"],
        order_row["delivery_fee_pence"],
        order_row["service_fee_pence"],
        order_row["tip_pence"],
        order_row["discount_pence"],
        order_row["total_pence"],
        "GBP",
        order_row["promo_id"],
        order_row["promo_code"],
        order_row["is_first_order_for_user"],
        order_row["is_new_user_promo"],
        order_row["payment_method_id"],
        order_row["payment_type"],
        order_row["card_bin"],
        order_row["card_last_four"],
        order_row["card_brand"],
        order_row["card_funding_type"],
        order_row["card_issuer_country"],
        order_row["is_digital_native_bank"],
        order_row["is_new_payment_method"],
        order_row.get("avs_result"),
        order_row.get("cvv_result"),
        order_row["session_id"],
        order_row["device_id"],
        order_row["device_type"],
        order_row["platform"],
        order_row["os_version"],
        order_row["app_version"],
        order_row["browser_name"],
        order_row["browser_version"],
        order_row["ip_address"],
        order_row["ip_country"],
        order_row["ip_city"],
        order_row["ip_is_proxy"],
        order_row["ip_is_vpn"],
        order_row["ip_is_tor"],
        order_row["ip_is_hosting"],
        order_row["device_user_count"],
        order_row["payment_user_count"],
        order_row["ip_to_delivery_distance_km"],
        order_row["billing_to_delivery_distance_km"],
        order_row["time_to_checkout_seconds"],
        order_row["cart_modifications_count"],
    ]

    placeholder_sql = ", ".join(f"${idx}" for idx in range(1, len(order_columns) + 1))
    insert_order_sql = (
        f"INSERT INTO orders ({', '.join(order_columns)}) "
        f"VALUES ({placeholder_sql}) RETURNING order_id, placed_at"
    )

    async with conn.transaction():
        inserted = await conn.fetchrow(insert_order_sql, *order_values)
        if inserted is None:
            raise RuntimeError("order insert returned no row")

        order_id = inserted["order_id"]
        placed_at = inserted["placed_at"]

        if cart.items:
            item_rows = [
                (
                    uuid.uuid4(),
                    order_id,
                    placed_at,
                    item.item_id,
                    item.name,
                    item.qty,
                    item.unit_price_pence,
                    item.line_total_pence,
                    item.is_hot_food,
                    None,
                    None,
                )
                for item in cart.items
            ]
            await conn.executemany(
                """
                INSERT INTO order_items (
                    order_item_id, order_id, order_placed_at, item_id, item_name_snapshot,
                    quantity, unit_price_pence, line_total_pence, is_hot_food, modifiers,
                    special_instructions
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                item_rows,
            )

        await conn.execute(
            """
            INSERT INTO order_events (
                order_id, order_placed_at, event_type, event_data, actor_type, created_at
            )
            VALUES ($1, $2, 'ORDER_PLACED', $3::jsonb, 'SIMULATOR', NOW())
            """,
            order_id,
            placed_at,
            json.dumps(_event_payload({**snapshot, "order_id": str(order_id)})),
        )

        await conn.execute(
            """
            INSERT INTO simulator_ground_truth (
                order_id, is_fraud, fraud_category, pattern_notes, ring_id
            )
            VALUES ($1, $2, $3, $4, $5)
            """,
            order_id,
            is_fraud,
            fraud_category,
            pattern_notes,
            ring_id,
        )

    return order_id, placed_at


async def notify_order_placed(conn: asyncpg.Connection, order_id: uuid.UUID) -> None:
    await conn.execute("SELECT pg_notify('order_placed', $1)", str(order_id))


async def _read_runtime_rate(
    redis_conn: aioredis.Redis[Any],
    fallback: int,
) -> int:
    raw = await redis_conn.get("simulator:rate_per_second")
    if raw is None:
        return fallback

    if isinstance(raw, bytes):
        raw = raw.decode()

    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return fallback

    return parsed if parsed > 0 else fallback


def _apply_fraud_order_attrs(
    snapshot: dict[str, Any],
    fraud_dict: dict[str, Any],
) -> None:
    """Propagate fraud-pattern overrides onto the order snapshot (synchronous).

    Fields propagated:
    - order_total_pence -> total_pence
    - user_id/victim_user_id, store_id, device_id, payment_method_id,
      delivery_address_id (FK columns only; sentinel values
      "VICTIM_SAVED"/"ABUSER_SAVED" are skipped - the legit-path value is
      retained)
      FK values are coerced to uuid.UUID regardless of source type.
    - card_country -> card_issuer_country (ISO-2 normalised)
    - card_funding_type, is_digital_native_bank
    - ip_country (explicit), ip_type (vpn/foreign -> ip_is_* flags)
    - address_type -> delivery_address_type (DELIVERY orders only)
    - avs_result, cvv_result (orders table columns)

    Fields intentionally NOT propagated here (generator-owned):
    - order_id, placed_at, order_number: generator-owned identity/time fields
    - is_night_order, variant, is_high_end_cart, is_new_device: control-plane
      fields consumed upstream by generate_fraud_order, not stored on orders
    - Denormalized FK-derived fields (user email/city/platform/card fields):
      handled by _apply_fraud_identity_overrides (async, DB-backed).
    """
    if "order_total_pence" in fraud_dict:
        # DESIGN: fraud_dict.total_pence is the canonical fraud amount; subtotal/vat/fees
        # from legit cart are kept since ML training uses total_pence directly and
        # doesn't reconcile against components. The "inconsistency" is intentional -
        # ML reads top-line total_pence + per-entity features, not arithmetic identity.
        snapshot["total_pence"] = int(fraud_dict["order_total_pence"])

    user_id_override = _fraud_user_id_override(fraud_dict)
    if user_id_override is not None:
        snapshot["user_id"] = user_id_override

    for key in ("store_id", "device_id", "payment_method_id", "delivery_address_id"):
        override = _fraud_uuid_override(fraud_dict.get(key))
        if override is not None:
            snapshot[key] = override

    if "avs_result" in fraud_dict:
        snapshot["avs_result"] = fraud_dict["avs_result"]
    if "cvv_result" in fraud_dict:
        snapshot["cvv_result"] = fraud_dict["cvv_result"]

    if "card_country" in fraud_dict:
        raw_card_country = fraud_dict["card_country"]
        # Fraud patterns may emit ISO-2 codes or long-form sentinels; persist ISO-2 only.
        snapshot["card_issuer_country"] = _resolve_card_country(
            None if raw_card_country is None else str(raw_card_country)
        )
    if "card_funding_type" in fraud_dict:
        snapshot["card_funding_type"] = fraud_dict["card_funding_type"]
    if "is_digital_native_bank" in fraud_dict:
        snapshot["is_digital_native_bank"] = fraud_dict["is_digital_native_bank"]

    has_explicit_ip_country = "ip_country" in fraud_dict
    if has_explicit_ip_country:
        raw_ip_country = fraud_dict["ip_country"]
        ip_country = "" if raw_ip_country is None else str(raw_ip_country)
        snapshot["ip_country"] = (
            ip_country.upper() if len(ip_country) == 2 and ip_country.isalpha() else "GB"
        )

    ip_type = fraud_dict.get("ip_type")
    if ip_type == "vpn":
        if not has_explicit_ip_country:
            snapshot["ip_country"] = "GB"
        snapshot["ip_is_vpn"] = True
        snapshot["ip_is_proxy"] = False
        snapshot["ip_is_tor"] = False
        snapshot["ip_is_hosting"] = False
    elif ip_type == "foreign":
        if not has_explicit_ip_country:
            snapshot["ip_country"] = "XX"
        snapshot["ip_is_vpn"] = False
        snapshot["ip_is_proxy"] = False

    if "address_type" in fraud_dict and snapshot.get("order_type") == "DELIVERY":
        snapshot["delivery_address_type"] = fraud_dict["address_type"]


async def _apply_fraud_identity_overrides(
    conn: asyncpg.Connection,
    snapshot: dict[str, Any],
    fraud_dict: dict[str, Any],
) -> None:
    """Fetch denormalized fields for any identity FKs the fraud pattern overrides.

    Called after _apply_fraud_order_attrs has stamped the FK columns.  Looks
    up the row for each FK that was actually overridden and back-fills the
    denormalized snapshot columns that insert_order writes. Rows that do not
    exist in the DB (synthetic fraud UUIDs) receive explicit synthetic/null
    derived values so the fraud FK is not mixed with legit-path attributes.
    """
    user_id = _fraud_user_id_override(fraud_dict)
    if user_id is not None:
        row = await conn.fetchrow(
            "SELECT email, phone, risk_tier, created_at FROM users WHERE user_id = $1",
            user_id,
        )
        if row is not None:
            email = str(row["email"])
            snapshot["user_email"] = email
            snapshot["user_email_domain"] = email.split("@", 1)[1] if "@" in email else "unknown"
            snapshot["user_phone"] = row["phone"]
            snapshot["user_risk_tier_at_order"] = row["risk_tier"]
        else:
            synthetic_email = f"{snapshot['user_id']}@fraud.test"
            snapshot["user_email"] = synthetic_email
            snapshot["user_email_domain"] = "fraud.test"
            snapshot["user_phone"] = None
            snapshot["user_risk_tier_at_order"] = None
            snapshot["user_account_age_days"] = 0
            snapshot["user_total_orders_lifetime"] = 0
            snapshot["user_total_orders_30d"] = 0
            snapshot["user_total_spend_lifetime_pence"] = 0
            snapshot["is_first_order_for_user"] = True

    store_id = _fraud_uuid_override(fraud_dict.get("store_id"))
    if store_id is not None:
        row = await conn.fetchrow(
            "SELECT merchant_id, city, country, latitude, longitude "
            "FROM stores WHERE store_id = $1",
            store_id,
        )
        if row is not None:
            snapshot["merchant_id"] = row["merchant_id"]
            snapshot["store_city"] = row["city"]
            snapshot["store_country"] = row["country"] if row["country"] is not None else "GB"
            snapshot["store_latitude"] = float(row["latitude"])
            snapshot["store_longitude"] = float(row["longitude"])
        else:
            snapshot["merchant_id"] = _SYNTHETIC_FRAUD_RING_MERCHANT_ID
            snapshot["store_city"] = _SYNTHETIC_FRAUD_RING_STORE_CITY
            snapshot["store_country"] = _SYNTHETIC_FRAUD_RING_STORE_COUNTRY
            snapshot["store_latitude"] = _SYNTHETIC_FRAUD_RING_STORE_LATITUDE
            snapshot["store_longitude"] = _SYNTHETIC_FRAUD_RING_STORE_LONGITUDE

    device_id = _fraud_uuid_override(fraud_dict.get("device_id"))
    if device_id is not None:
        row = await conn.fetchrow(
            "SELECT device_type, platform, os_version, app_version, browser_name, browser_version "
            "FROM devices WHERE device_id = $1",
            device_id,
        )
        if row is not None:
            snapshot["device_type"] = row["device_type"]
            snapshot["platform"] = row["platform"]
            snapshot["os_version"] = row["os_version"]
            snapshot["app_version"] = row["app_version"]
            snapshot["browser_name"] = row["browser_name"]
            snapshot["browser_version"] = row["browser_version"]
        else:
            snapshot["device_type"] = None
            snapshot["platform"] = None
            snapshot["os_version"] = None
            snapshot["app_version"] = None
            snapshot["browser_name"] = None
            snapshot["browser_version"] = None

    pm_id = _fraud_uuid_override(fraud_dict.get("payment_method_id"))
    if pm_id is not None:
        row = await conn.fetchrow(
            """SELECT payment_type, card_bin, card_last_four, card_brand,
                      card_funding_type, card_issuer_country, is_digital_native_bank
               FROM payment_methods WHERE payment_method_id = $1""",
            pm_id,
        )
        if row is not None:
            snapshot["payment_type"] = row["payment_type"]
            snapshot["card_bin"] = row["card_bin"]
            snapshot["card_last_four"] = row["card_last_four"]
            snapshot["card_brand"] = row["card_brand"]
            snapshot["card_funding_type"] = row["card_funding_type"]
            snapshot["card_issuer_country"] = row["card_issuer_country"]
            snapshot["is_digital_native_bank"] = row["is_digital_native_bank"]
        else:
            card_issuer_country = (
                snapshot.get("card_issuer_country") if "card_country" in fraud_dict else None
            )
            snapshot["card_bin"] = None
            snapshot["card_last_four"] = None
            snapshot["card_brand"] = None
            snapshot["payment_type"] = snapshot.get("payment_type") or "ACCOUNT_CREDIT"
            snapshot["card_issuer_country"] = card_issuer_country
            snapshot["card_issuer_bank"] = None

    addr_id = _fraud_uuid_override(fraud_dict.get("delivery_address_id"))
    if addr_id is not None:
        row = await conn.fetchrow(
            "SELECT latitude, longitude, address_type FROM user_addresses WHERE address_id = $1",
            addr_id,
        )
        if row is not None:
            if row["latitude"] is not None:
                snapshot["delivery_latitude"] = float(row["latitude"])
            if row["longitude"] is not None:
                snapshot["delivery_longitude"] = float(row["longitude"])
            if row["address_type"] is not None:
                snapshot["delivery_address_type"] = row["address_type"]
        else:
            snapshot["delivery_latitude"] = None
            snapshot["delivery_longitude"] = None
            snapshot["delivery_address_snapshot"] = json.dumps({"city": "FRAUD_RING"})
            snapshot["delivery_city"] = "FRAUD_RING"


async def create_one_order(
    pool: asyncpg.Pool,
    user_picker: WeightedUserPicker,
    stores_by_city: dict[str, list[dict[str, Any]]],
    store_hours_by_store_id: dict[uuid.UUID, list[dict[str, Any]]],
    promos: list[dict[str, Any]],
    rng: random.Random,
    scoring_enabled: bool,
) -> None:
    async with pool.acquire() as conn:
        user_id = user_picker.pick(rng)
        fraud_roll = rng.random()
        fraud_rate = _parse_fraud_rate(os.getenv("FRAUD_INJECTION_RATE"), FRAUD_INJECTION_RATE)
        is_fraud_order = fraud_roll < fraud_rate
        user_data = await load_user_data(conn, user_id)
        user = user_data["user"]

        store = pick_store_for_user(rng, user_data, stores_by_city, store_hours_by_store_id)
        order_type = _select_order_type(rng, store)
        if order_type == "DINE_IN" and not bool(store.get("accepts_in_store")):
            raise RuntimeError("DINE_IN selected but store does not accept in-store orders")
        order_channel = pick_channel_for_user(rng, user_data["devices"])

        device, ip_address = pick_device_and_ip(
            rng,
            user_data["devices"],
            user_data["default_address"].get("city")
            if isinstance(user_data.get("default_address"), dict)
            else None,
        )

        delivery_address = None
        if order_type == "DELIVERY":
            delivery_address = _select_delivery_address(
                rng,
                user_data["default_address"],
                user_data["addresses"],
            )

        payment_methods = user_data["payment_methods"]
        roll = rng.random()
        if payment_methods and roll < 0.85:
            payment_method = payment_methods[0]
        elif payment_methods and roll < 0.95:
            payment_method = rng.choice(payment_methods)
        else:
            payment_method = await _insert_ephemeral_payment_method(conn, user_id, rng)

        is_new_payment_method = await _is_new_payment_method(
            conn,
            user_id,
            payment_method["payment_method_id"],
        )

        menu_items = await _load_menu_items(conn, store["store_id"])
        if not menu_items:
            raise RuntimeError(f"no active menu items for store: {store['store_id']}")

        user_profile = UserProfile(
            user_id=user_id,
            preferred_cuisines=store.get("cuisine_types"),
        )
        cart = build_realistic_cart(store["store_id"], user_profile, menu_items, rng=rng)

        (
            user_total_orders_lifetime,
            user_total_orders_30d,
            user_total_spend_lifetime_pence,
        ) = await _read_user_order_metrics(conn, user_id)
        is_first_order_for_user = user_total_orders_lifetime == 0

        promo = await apply_promo(
            conn,
            user_id,
            rng,
            is_first_order_for_user,
            promos,
            cart.subtotal_pence,
        )
        applied_discount = _promo_discount(promo, cart.subtotal_pence)

        distance_km = _distance_km_for_delivery(store, delivery_address)
        pricing_tuple = compute_pricing(cart.subtotal_pence, distance_km, rng, order_type)

        snapshot = _build_snapshot(
            user=user,
            store=store,
            cart=cart,
            delivery_address=delivery_address,
            payment_method=payment_method,
            device=device,
            ip_address=ip_address,
            order_type=order_type,
            order_channel=order_channel,
            promo=promo,
            applied_discount=applied_discount,
            pricing_tuple=pricing_tuple,
            user_total_orders_lifetime=user_total_orders_lifetime,
            user_total_orders_30d=user_total_orders_30d,
            user_total_spend_lifetime_pence=user_total_spend_lifetime_pence,
            is_new_address=(
                await _is_new_delivery_address(
                    conn,
                    user_id,
                    delivery_address["address_id"],
                )
                if delivery_address is not None
                else None
            ),
            is_new_payment_method=is_new_payment_method,
            rng=rng,
        )

        snapshot["order_number"] = generate_order_number(rng)

        attempts = 0
        placed_at = datetime.now(tz=LONDON_TZ)
        # DESIGN: placed_at stays wall-clock (generator-owned). Patterns like stolen_card
        # that set 2-5am timestamps for time-of-day fraud signal are ignored - real-time
        # simulator pacing conflict (overriding to past timestamps breaks partition window,
        # aggregator NOTIFY consistency, and lifecycle daemon assumptions). v1 limitation;
        # see <P3-K1 follow-up issue> for the future fraud-time-shift mode.
        fraud_order_dict: dict[str, Any] | None = None
        fraud_ground_truth: GroundTruth | None = None
        if is_fraud_order:
            ctx = FraudPatternContext(now=placed_at, rng=rng)
            fraud_order_dict, fraud_ground_truth = await generate_fraud_order(ctx)

        if is_fraud_order and fraud_order_dict is not None:
            _apply_fraud_order_attrs(snapshot, fraud_order_dict)
            await _apply_fraud_identity_overrides(conn, snapshot, fraud_order_dict)

        while True:
            try:
                snapshot["order_number"] = generate_order_number(rng)
                if fraud_ground_truth is None:
                    order_id, _ = await insert_order(conn, snapshot, cart, placed_at)
                else:
                    order_id, _ = await insert_order(
                        conn,
                        snapshot,
                        cart,
                        placed_at,
                        is_fraud=True,
                        fraud_category=fraud_ground_truth.fraud_category,
                        pattern_notes=fraud_ground_truth.pattern_notes,
                        ring_id=fraud_ground_truth.ring_id,
                    )
                break
            except asyncpg.UniqueViolationError:
                attempts += 1
                if attempts >= 5:
                    raise

        if scoring_enabled:
            pass

        await notify_order_placed(conn, uuid.UUID(str(order_id)))


async def main() -> None:
    config = load_config_from_env()

    pool = await asyncpg.create_pool(
        DATABASE_URL_SIMULATOR,
        min_size=10,
        max_size=50,
    )
    redis_conn = aioredis.from_url(REDIS_URL)

    try:
        stores_by_city = await load_stores_by_city(pool)
        store_hours_by_store_id = await load_store_hours(pool)
        promos = await load_active_promos(pool)

        user_picker = WeightedUserPicker(pool, redis_conn)
        await user_picker.refresh()

        semaphore = asyncio.Semaphore(100)
        rng = random.Random(42)
        # Initialize fraud-pattern state (callers-must-init per CLAUDE.md).
        init_collusive_stores(rng)
        init_promo_abuse_rings(rng)
        init_reseller_accounts(
            rng,
            store_id_pool=sorted(
                [s["store_id"] for stores in stores_by_city.values() for s in stores],
                key=str,
            ),
        )
        init_triangulation_accounts(rng)
        rng_lock = asyncio.Lock()
        stats_lock = asyncio.Lock()

        attempt_counter = 0
        window_orders = 0
        window_errors = 0
        window_create_ms = 0.0
        effective_orders_per_second = config.orders_per_second

        async def generate_one() -> None:
            nonlocal window_orders
            nonlocal window_errors
            nonlocal window_create_ms
            nonlocal effective_orders_per_second

            start = time.perf_counter()

            try:
                async with rng_lock:
                    order_rng = random.Random(rng.randint(0, 2**63 - 1))

                try:
                    await create_one_order(
                        pool=pool,
                        user_picker=user_picker,
                        stores_by_city=stores_by_city,
                        store_hours_by_store_id=store_hours_by_store_id,
                        promos=promos,
                        rng=order_rng,
                        scoring_enabled=config.scoring_enabled,
                    )
                except Exception:
                    logger.exception("order_gen_failed")
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    async with stats_lock:
                        window_orders += 1
                        window_errors += 1
                        window_create_ms += elapsed_ms
                    return

                elapsed_ms = (time.perf_counter() - start) * 1000
                async with stats_lock:
                    window_orders += 1
                    window_create_ms += elapsed_ms

                    if window_orders >= 1000:
                        logger.info(
                            json.dumps(
                                {
                                    "event": "throughput_report",
                                    "orders_1min": window_orders,
                                    "errors_1min": window_errors,
                                    "avg_create_ms": (
                                        0.0
                                        if window_orders == 0
                                        else round(window_create_ms / max(window_orders, 1), 3)
                                    ),
                                }
                            )
                        )
                        window_orders = 0
                        window_errors = 0
                        window_create_ms = 0.0
            finally:
                semaphore.release()

        while True:
            await semaphore.acquire()
            asyncio.create_task(generate_one())
            attempt_counter += 1

            if attempt_counter % 100 == 0:
                effective_orders_per_second = await _read_runtime_rate(
                    redis_conn,
                    config.orders_per_second,
                )

            sleep_for = 1.0 / max(effective_orders_per_second, 1)
            await asyncio.sleep(sleep_for)
    finally:
        await pool.close()
        await redis_conn.close()


if __name__ == "__main__":
    asyncio.run(main())
