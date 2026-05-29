from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import math
import multiprocessing
import os
import random
import string
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg2
from faker import Faker  # type: ignore[import]
from shared.uk_data import (
    CARD_BRANDS,
    CUISINE_WEIGHTS,
    DISPOSABLE_DOMAIN_RATE,
    DISPOSABLE_EMAIL_DOMAINS,
    EMAIL_DOMAINS,
    POS_SYSTEMS,
    UK_CARD_ISSUERS,
    UK_CITIES,
    UK_POSTCODE_AREAS,
    random_uk_postcode,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_SIMULATOR_DB_URL = (
    "postgresql://simulator_user:simulator_dev_password@postgres:5432/fraud_platform"
)
_UK_CHAIN_NAMES = [
    "Nando's",
    "Wagamama",
    "Pret A Manger",
    "Greggs",
    "Pizza Express",
    "Itsu",
    "Leon",
    "Honest Burgers",
    "Dishoom",
    "Five Guys UK",
    "Yo! Sushi",
    "Pho",
    "Wasabi",
    "Tortilla",
    "Subway UK",
    "KFC UK",
    "McDonald's UK",
    "Burger King UK",
    "Costa Coffee",
    "Caffè Nero",
]
_UK_CITY_DATA = {
    city: (weight, latitude, longitude, county)
    for city, weight, latitude, longitude, county in UK_CITIES
}
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
_RNG_CHAIN_SET = set(_UK_CHAIN_NAMES)
_CITY_POSTCODE_AREAS = UK_POSTCODE_AREAS
_CITY_NAMES = [city for city, *_ in UK_CITIES]
_CITY_WEIGHTS = [weight for _, weight, *_ in UK_CITIES]
_CUISINE_NAMES = list(CUISINE_WEIGHTS.keys())
_CUISINE_WEIGHTS = list(CUISINE_WEIGHTS.values())
_POS_SYSTEM_NAMES = [name for name, _ in POS_SYSTEMS]
_POS_SYSTEM_WEIGHTS = [weight for _, weight in POS_SYSTEMS]
_timings: dict[str, tuple[int, float]] = {}
rng = random.Random()
fake = Faker("en_GB")

_merchant_store_allocs: list[tuple[str, int]] = []
_store_ids: list[str] = []
_store_cuisines: dict[str, list[str]] = {}
_store_price_tiers: dict[str, int] = {}
_user_ids: list[str] = []
_USER_CARD_BRANDS = CARD_BRANDS
_USER_DISPOSABLE_DOMAIN_RATE = DISPOSABLE_DOMAIN_RATE
_USER_DISPOSABLE_EMAIL_DOMAINS = DISPOSABLE_EMAIL_DOMAINS
_USER_EMAIL_DOMAINS = EMAIL_DOMAINS
_USER_UK_CARD_ISSUERS = UK_CARD_ISSUERS

_STORE_HOUR_PATTERNS: list[str] = ["24h", "early", "standard", "late", "lunch"]
_STORE_HOUR_WEIGHTS: list[float] = [0.05, 0.12, 0.58, 0.18, 0.07]
_STORE_HOUR_WINDOWS: dict[str, tuple[str, str]] = {
    "24h": ("00:00:00", "23:59:59"),
    "early": ("06:00:00", "22:00:00"),
    "standard": ("11:00:00", "23:00:00"),
    "late": ("17:00:00", "04:00:00"),
    "lunch": ("11:00:00", "15:00:00"),
}

CUISINE_MENU_TEMPLATES: dict[str, list[tuple[str, str, bool]]] = {
    "Indian": [
        ("Chicken Tikka Masala", "MAIN", True),
        ("Lamb Rogan Josh", "MAIN", True),
        ("Vegetable Biryani", "MAIN", True),
        ("Garlic Naan", "SIDE", True),
        ("Pilau Rice", "SIDE", True),
        ("Onion Bhaji", "STARTER", True),
        ("Samosa", "STARTER", True),
        ("Tandoori Chicken", "MAIN", True),
        ("Saag Aloo", "SIDE", True),
        ("Mango Chutney", "SIDE", False),
        ("Raita", "SIDE", False),
        ("Mango Lassi", "DRINK", False),
        ("Gulab Jamun", "DESSERT", False),
    ],
    "Chinese": [
        ("Sweet & Sour Chicken", "MAIN", True),
        ("Beef Chow Mein", "MAIN", True),
        ("Dim Sum Selection", "STARTER", True),
        ("Spring Rolls", "STARTER", True),
        ("Egg Fried Rice", "SIDE", True),
        ("Prawn Crackers", "SIDE", False),
        ("Char Siu Pork", "MAIN", True),
        ("Wonton Soup", "STARTER", True),
        ("Jasmine Tea", "DRINK", False),
        ("Fortune Cookie", "DESSERT", False),
        ("Sesame Prawn Toast", "STARTER", True),
        ("Crispy Duck", "MAIN", True),
    ],
    "Italian": [
        ("Spaghetti Bolognese", "MAIN", True),
        ("Lasagne", "MAIN", True),
        ("Penne Arrabbiata", "MAIN", True),
        ("Bruschetta", "STARTER", True),
        ("Garlic Bread", "SIDE", True),
        ("Tiramisu", "DESSERT", False),
        ("Panna Cotta", "DESSERT", False),
        ("Caprese Salad", "STARTER", False),
        ("Minestrone Soup", "STARTER", True),
        ("Sparkling Water", "DRINK", False),
        ("Lemonade", "DRINK", False),
    ],
    "Pizza": [
        ("Margherita", "MAIN", True),
        ("Pepperoni", "MAIN", True),
        ("Hawaiian", "MAIN", True),
        ("Quattro Stagioni", "MAIN", True),
        ("BBQ Chicken Pizza", "MAIN", True),
        ("Veggie Supreme", "MAIN", True),
        ("Garlic Bread", "SIDE", True),
        ("Dough Balls", "STARTER", True),
        ("Caesar Salad", "STARTER", False),
        ("Tiramisu", "DESSERT", False),
        ("Coca-Cola", "DRINK", False),
        ("Lemonade", "DRINK", False),
    ],
    "Kebab": [
        ("Doner Kebab", "MAIN", True),
        ("Mixed Shish", "MAIN", True),
        ("Chicken Shish", "MAIN", True),
        ("Lamb Doner Wrap", "MAIN", True),
        ("Falafel Wrap", "MAIN", True),
        ("Chips", "SIDE", True),
        ("Halloumi Wrap", "MAIN", True),
        ("Garlic Sauce", "SIDE", False),
        ("Chilli Sauce", "SIDE", False),
        ("Salad Box", "SIDE", False),
        ("Canned Drink", "DRINK", False),
    ],
    "Turkish": [
        ("Adana Kebab", "MAIN", True),
        ("Iskender Kebab", "MAIN", True),
        ("Chicken Shish", "MAIN", True),
        ("Lahmacun", "MAIN", True),
        ("Pide", "MAIN", True),
        ("Hummus", "STARTER", False),
        ("Ezme Salad", "STARTER", False),
        ("Baklava", "DESSERT", False),
        ("Ayran", "DRINK", False),
        ("Chips", "SIDE", True),
    ],
    "Fish & Chips": [
        ("Cod & Chips", "MAIN", True),
        ("Haddock & Chips", "MAIN", True),
        ("Battered Sausage", "MAIN", True),
        ("Mushy Peas", "SIDE", True),
        ("Curry Sauce", "SIDE", True),
        ("Pickled Egg", "SIDE", False),
        ("Saveloy", "MAIN", True),
        ("Scampi & Chips", "MAIN", True),
        ("Bread & Butter", "SIDE", False),
        ("Canned Drink", "DRINK", False),
    ],
    "Burger": [
        ("Classic Cheeseburger", "MAIN", True),
        ("Double Smash Burger", "MAIN", True),
        ("Crispy Chicken Burger", "MAIN", True),
        ("Veggie Burger", "MAIN", True),
        ("Hot Dog", "MAIN", True),
        ("Fries", "SIDE", True),
        ("Onion Rings", "SIDE", True),
        ("Coleslaw", "SIDE", False),
        ("Milkshake", "DRINK", False),
        ("Canned Drink", "DRINK", False),
        ("Brownie", "DESSERT", False),
    ],
    "American": [
        ("BBQ Ribs", "MAIN", True),
        ("Mac & Cheese", "MAIN", True),
        ("Buffalo Wings", "STARTER", True),
        ("Pulled Pork Slider", "MAIN", True),
        ("Sweet Potato Fries", "SIDE", True),
        ("Corn on the Cob", "SIDE", True),
        ("Caesar Salad", "STARTER", False),
        ("Root Beer Float", "DRINK", False),
        ("Chocolate Brownie", "DESSERT", False),
    ],
    "Thai": [
        ("Pad Thai", "MAIN", True),
        ("Green Curry", "MAIN", True),
        ("Red Curry", "MAIN", True),
        ("Tom Yum Soup", "STARTER", True),
        ("Spring Rolls", "STARTER", True),
        ("Jasmine Rice", "SIDE", True),
        ("Som Tam Salad", "STARTER", False),
        ("Mango Sticky Rice", "DESSERT", False),
        ("Thai Iced Tea", "DRINK", False),
    ],
    "Japanese": [
        ("Chicken Ramen", "MAIN", True),
        ("Tonkotsu Ramen", "MAIN", True),
        ("Gyoza", "STARTER", True),
        ("Edamame", "STARTER", False),
        ("Miso Soup", "STARTER", True),
        ("Teriyaki Chicken", "MAIN", True),
        ("Katsu Curry", "MAIN", True),
        ("Green Tea", "DRINK", False),
        ("Matcha Ice Cream", "DESSERT", False),
    ],
    "Sushi": [
        ("Salmon Nigiri", "MAIN", False),
        ("Tuna Maki", "MAIN", False),
        ("California Roll", "MAIN", False),
        ("Dragon Roll", "MAIN", False),
        ("Spicy Tuna Roll", "MAIN", False),
        ("Edamame", "STARTER", False),
        ("Miso Soup", "STARTER", True),
        ("Gyoza", "STARTER", True),
        ("Matcha Ice Cream", "DESSERT", False),
        ("Green Tea", "DRINK", False),
        ("Seaweed Salad", "STARTER", False),
    ],
    "Caribbean": [
        ("Jerk Chicken", "MAIN", True),
        ("Curry Goat", "MAIN", True),
        ("Ackee & Saltfish", "MAIN", True),
        ("Rice & Peas", "SIDE", True),
        ("Plantain", "SIDE", True),
        ("Coleslaw", "SIDE", False),
        ("Rum Punch", "DRINK", False),
        ("Ginger Beer", "DRINK", False),
        ("Festival Dumplings", "SIDE", True),
        ("Bread Pudding", "DESSERT", False),
    ],
    "Lebanese": [
        ("Mixed Mezze Platter", "STARTER", False),
        ("Hummus", "STARTER", False),
        ("Falafel", "STARTER", True),
        ("Shawarma", "MAIN", True),
        ("Kafta", "MAIN", True),
        ("Fattoush Salad", "STARTER", False),
        ("Tabbouleh", "STARTER", False),
        ("Pita Bread", "SIDE", True),
        ("Mint Lemonade", "DRINK", False),
        ("Baklava", "DESSERT", False),
    ],
    "Polish": [
        ("Pierogi", "MAIN", True),
        ("Bigos", "MAIN", True),
        ("Żurek Soup", "STARTER", True),
        ("Kielbasa Sausage", "MAIN", True),
        ("Placki Ziemniaczane", "MAIN", True),
        ("Kapusniak", "STARTER", True),
        ("Cucumber Salad", "SIDE", False),
        ("Kompot", "DRINK", False),
        ("Makowiec", "DESSERT", False),
    ],
    "British": [
        ("Fish & Chips", "MAIN", True),
        ("Bangers & Mash", "MAIN", True),
        ("Cottage Pie", "MAIN", True),
        ("Sunday Roast", "MAIN", True),
        ("Ploughman's Lunch", "MAIN", False),
        ("Scotch Egg", "STARTER", True),
        ("Prawn Cocktail", "STARTER", False),
        ("Sticky Toffee Pudding", "DESSERT", True),
        ("English Breakfast Tea", "DRINK", False),
        ("Cornish Pasty", "MAIN", True),
    ],
    "Pub": [
        ("Steak & Ale Pie", "MAIN", True),
        ("Fish & Chips", "MAIN", True),
        ("Ploughman's Lunch", "MAIN", False),
        ("Jacket Potato", "MAIN", True),
        ("Scampi & Chips", "MAIN", True),
        ("Garlic Mushrooms", "STARTER", True),
        ("Caesar Salad", "STARTER", False),
        ("Chips", "SIDE", True),
        ("Soft Drink", "DRINK", False),
        ("Apple Crumble", "DESSERT", True),
    ],
    "Vietnamese": [
        ("Pho Bo", "MAIN", True),
        ("Pho Ga", "MAIN", True),
        ("Banh Mi", "MAIN", False),
        ("Goi Cuon (Fresh Spring Rolls)", "STARTER", False),
        ("Bun Cha", "MAIN", True),
        ("Com Tam", "MAIN", True),
        ("Jasmine Tea", "DRINK", False),
        ("Cà Phê Sữa Đá", "DRINK", False),
        ("Chè", "DESSERT", False),
    ],
    "Other": [
        ("House Special", "MAIN", True),
        ("Seasonal Salad", "STARTER", False),
        ("Soup of the Day", "STARTER", True),
        ("Chef's Pasta", "MAIN", True),
        ("Grilled Chicken", "MAIN", True),
        ("Soft Drink", "DRINK", False),
        ("Water", "DRINK", False),
        ("Ice Cream", "DESSERT", False),
        ("Garlic Bread", "SIDE", True),
        ("Chips", "SIDE", True),
    ],
}

_CUISINE_PRICE_RANGES: dict[str, dict[str, tuple[int, int]]] = {
    "MAIN": {
        "Fish & Chips": (800, 1400),
        "Burger": (700, 1200),
        "American": (800, 1500),
        "Kebab": (600, 1100),
        "Turkish": (800, 1300),
        "British": (900, 1500),
        "Pub": (900, 1600),
        "Pizza": (900, 1600),
        "Italian": (1000, 1800),
        "Indian": (1000, 1700),
        "Chinese": (900, 1500),
        "Thai": (900, 1500),
        "Caribbean": (900, 1500),
        "Lebanese": (800, 1400),
        "Polish": (800, 1400),
        "Vietnamese": (800, 1400),
        "Japanese": (1200, 2200),
        "Sushi": (1200, 2500),
        "Other": (900, 1600),
    },
    "STARTER": {
        "default": (400, 900),
    },
    "SIDE": {
        "default": (200, 600),
    },
    "DRINK": {
        "default": (150, 350),
    },
    "DESSERT": {
        "default": (400, 900),
    },
}

# Imported modules used for forward compatibility with downstream slices.
_SEED_SKELETON_BUFFER = io.StringIO()
_SIMULATOR_NAMESPACE = uuid.UUID(int=0)
_WORKER_COUNT_HINT = multiprocessing.cpu_count()
_DEFAULT_SCALE_SQRT = math.sqrt(1.0)
_TODAY = date.today()
_ONE_DAY = timedelta(days=0)
_SCRIPT_STARTED_AT = time.time()
_SEED_BASE_NOW = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_DRIVER_EMAIL_DOMAINS = ["gmail.com", "outlook.com", "hotmail.com", "yahoo.co.uk", "icloud.com"]
_PROMOS_DATA: list[tuple[str, str, str, str, str, str, int, int | None, bool]] = [
    # (code, type, discount_pence_or_empty, discount_pct_or_empty, min_order, max_per_user, valid_from_offset_days, valid_until_offset_days_or_None, is_targeted)
    ("WELCOME10", "NEW_USER", "", "10.00", "1500", "1", -3650, None, False),
    ("NEWUK20", "NEW_USER", "", "20.00", "2000", "1", -3650, None, False),
    ("FREEDEL", "FREE_DELIVERY", "", "", "1000", "5", -3650, None, False),
    ("SUMMER24", "PERCENT_OFF", "", "15.00", "1500", "2", -365, None, False),
    ("MIDWEEK", "PERCENT_OFF", "", "10.00", "1200", "3", -3650, None, False),
    ("SUNDAYROAST", "PERCENT_OFF", "", "15.00", "2500", "2", -365, 365, False),
    ("FRIDAY5", "POUND_OFF", "500", "", "2000", "1", -365, 365, False),
    ("LUNCHTIME", "PERCENT_OFF", "", "20.00", "800", "1", -3650, None, False),
    ("BIRTHDAY", "PERCENT_OFF", "", "25.00", "1500", "1", -3650, None, True),
    ("FIRSTORDER", "NEW_USER", "", "15.00", "1000", "1", -3650, None, False),
    ("STUDENTUK", "PERCENT_OFF", "", "10.00", "1000", "5", -3650, None, True),
    ("NEWNHS", "PERCENT_OFF", "", "20.00", "1500", "2", -3650, None, True),
    ("LOYALTY5", "POUND_OFF", "500", "", "2500", "1", -3650, None, False),
    ("VEGGIE20", "PERCENT_OFF", "", "20.00", "1200", "2", -3650, None, False),
    ("FREESHIP", "FREE_DELIVERY", "", "", "500", "10", -3650, None, False),
    ("WINTER10", "PERCENT_OFF", "", "10.00", "1500", "2", -365, 180, False),
    ("EASTER15", "PERCENT_OFF", "", "15.00", "2000", "1", -365, 60, False),
    ("XMAS25", "PERCENT_OFF", "", "25.00", "3000", "1", -365, 30, False),
    ("NEWYEAR", "PERCENT_OFF", "", "20.00", "2000", "1", -365, 7, False),
    ("GROUPORDER", "PERCENT_OFF", "", "10.00", "5000", "3", -3650, None, False),
    ("REFER100", "REFERRAL", "1000", "", "0", "1", -3650, None, False),
    ("REFER101", "REFERRAL", "1000", "", "0", "1", -3650, None, False),
    ("REFER102", "REFERRAL", "1000", "", "0", "1", -3650, None, False),
    ("REFER103", "REFERRAL", "1000", "", "0", "1", -3650, None, False),
    ("REFER104", "REFERRAL", "1000", "", "0", "1", -3650, None, False),
    ("REFER105", "REFERRAL", "1000", "", "0", "1", -3650, None, False),
    ("REFER106", "REFERRAL", "1000", "", "0", "1", -3650, None, False),
    ("REFER107", "REFERRAL", "1000", "", "0", "1", -3650, None, False),
    ("REFER108", "REFERRAL", "1000", "", "0", "1", -3650, None, False),
    ("REFER109", "REFERRAL", "1000", "", "0", "1", -3650, None, False),
]


def _poisson_sample(lam: float) -> int:
    """Knuth Poisson sampler (stdlib only)."""
    lam_tail = math.exp(-lam)
    k = 0
    p = 1.0
    while p > lam_tail:
        k += 1
        p *= rng.random()
    return k - 1


def main() -> None:
    parser = argparse.ArgumentParser(description="UK-localised seed loader")
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Scale factor; 0.1 = 10% of full scale for dev iteration",
    )
    parser.add_argument(
        "--skip",
        nargs="*",
        default=[],
        help="Entities to skip (e.g. --skip users devices)",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error(f"--workers must be >= 1, got {args.workers}")

    skip_entities: list[str] = args.skip or []
    seed_everything(args.seed)
    apply_session_tunings()
    if "merchants" not in skip_entities:
        seed_merchants(args.scale)
    if "stores" not in skip_entities:
        seed_stores(args.scale)
    if "store_hours" not in skip_entities:
        seed_store_hours()
    if "menu_items" not in skip_entities:
        seed_menu_items(args.scale)
    if "drivers" not in skip_entities:
        seed_drivers(args.scale)
    if "users" not in skip_entities:
        seed_users_parallel(args.scale, args.workers)
    if "promotions" not in skip_entities:
        seed_promotions()
    if "devices" not in skip_entities:
        seed_devices(args.scale)
    vacuum_analyze_all()
    print_summary()


def seed_everything(seed_value: int) -> None:
    random.seed(seed_value)
    Faker.seed(seed_value)
    print(f"[seed] seed={seed_value}")


def apply_session_tunings() -> None:
    pass  # Tunings now applied in _get_conn() for each connection.


def _get_conn() -> Any:
    db_url = _coalesce_database_url(
        os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_SIMULATOR")
    )
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SET synchronous_commit = OFF;")  # type: ignore[attr-defined]
        cur.execute("SET work_mem = '256MB';")  # type: ignore[attr-defined]
        cur.execute("SET maintenance_work_mem = '1GB';")  # type: ignore[attr-defined]
    conn.commit()
    return conn


def _coalesce_database_url(database_url: str | None) -> str:
    if database_url is None:
        return _DEFAULT_SIMULATOR_DB_URL
    return database_url


def _random_uk_ip() -> str:
    prefix = rng.choice(_UK_ISP_PREFIXES)
    return f"{prefix}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"


def seed_merchants(scale: float) -> None:
    start = time.time()
    rng.seed(random.random())
    _merchant_store_allocs.clear()

    now = _SEED_BASE_NOW
    target_merchants = max(1, int(5000 * scale))
    target_total_stores = max(1, int(15000 * scale))

    category_names = ["QSR", "CASUAL_DINING", "FINE_DINING", "DARK_KITCHEN", "CONVENIENCE"]
    category_weights = [60, 20, 5, 10, 5]

    raw_allocs: list[tuple[str, int]] = []
    merchant_data: list[tuple[str, str, str, str, str, str, str, str, str]] = []

    for _ in range(target_merchants):
        merchant_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        merchant_category = rng.choices(category_names, weights=category_weights, k=1)[0]
        chain_roll = rng.random()
        if chain_roll < 0.8:
            merchant_type = "independent"
            legal_base = fake.company()
            while legal_base in _RNG_CHAIN_SET:
                legal_base = fake.company()
            brand_name = legal_base
            raw_store_count = 1
        elif chain_roll < 0.95:
            merchant_type = "small_chain"
            brand_name = rng.choice(_UK_CHAIN_NAMES)
            raw_store_count = rng.randint(2, 5)
        else:
            merchant_type = "large_chain"
            brand_name = rng.choice(_UK_CHAIN_NAMES)
            raw_store_count = rng.randint(10, 50)

        legal_name = f"{brand_name} Ltd" if merchant_type != "independent" else f"{brand_name} Ltd"
        brand_name_trunc = brand_name[:255]
        legal_name_trunc = legal_name[:255]
        companies_house_no = f"0{rng.randint(1000000, 9999999)}"
        vat_number = f"GB{rng.randint(100000000, 999999999)}"
        onboarded_at = (now - timedelta(days=rng.randint(365, 1095))).replace(microsecond=0)
        onboarded_at_text = onboarded_at.strftime("%Y-%m-%d %H:%M:%S+00")

        raw_allocs.append((merchant_id, raw_store_count))
        merchant_data.append(
            (
                merchant_id,
                legal_name_trunc,
                brand_name_trunc,
                merchant_category,
                companies_house_no,
                vat_number,
                "ACTIVE",
                onboarded_at_text,
                "STANDARD",
            )
        )

    raw_total = sum(count for _, count in raw_allocs)
    scale_ratio = target_total_stores / raw_total
    scaled_allocs: list[tuple[str, int, float]] = []
    for merchant_id, raw_count in raw_allocs:
        scaled = raw_count * scale_ratio
        scaled_count = max(1, int(scaled))
        scaled_allocs.append((merchant_id, scaled_count, scaled - scaled_count))

    current_total = sum(count for _, count, _ in scaled_allocs)
    delta = target_total_stores - current_total
    if delta > 0:
        order = sorted(
            range(len(scaled_allocs)),
            key=lambda idx: scaled_allocs[idx][2],
            reverse=True,
        )
        cursor = 0
        while delta > 0:
            idx = order[cursor % len(order)]
            merchant_id, current_count, remainder = scaled_allocs[idx]
            scaled_allocs[idx] = (merchant_id, current_count + 1, remainder)
            delta -= 1
            cursor += 1
    elif delta < 0:
        order = sorted(
            range(len(scaled_allocs)),
            key=lambda idx: scaled_allocs[idx][2],
        )
        cursor = 0
        while delta < 0 and cursor < len(order) * 3:
            idx = order[cursor % len(order)]
            merchant_id, current_count, remainder = scaled_allocs[idx]
            if current_count > 1:
                scaled_allocs[idx] = (merchant_id, current_count - 1, remainder)
                delta += 1
            cursor += 1

    # Preserve target count as much as possible under minimum-one per merchant.
    _merchant_store_allocs.extend(
        (merchant_id, max(1, count)) for merchant_id, count, _ in scaled_allocs
    )

    conn = _get_conn()
    try:
        buf = io.StringIO()
        writer = csv.writer(buf)
        for (
            merchant_id,
            legal_name,
            brand_name,
            merchant_category,
            companies_house_no,
            vat_number,
            status,
            onboarded_at_text,
            risk_tier,
        ) in merchant_data:
            writer.writerow(
                [
                    merchant_id,
                    legal_name,
                    brand_name,
                    merchant_category,
                    companies_house_no,
                    vat_number,
                    status,
                    onboarded_at_text,
                    risk_tier,
                ]
            )
        buf.seek(0)
        with conn.cursor() as cur:
            cur.copy_expert(
                "COPY merchants (merchant_id, legal_name, brand_name, merchant_category, companies_house_no, vat_number, status, onboarded_at, risk_tier) FROM STDIN WITH (FORMAT csv)",
                buf,
            )
        conn.commit()
        _timings["merchants"] = (target_merchants, time.time() - start)
    finally:
        conn.close()


def _pg_array_literal(elements: list[str]) -> str:
    """Serialize a list of strings to a PostgreSQL array literal."""

    def quote(s: str) -> str:
        needs_quote = not s or any(c in s for c in (" ", ",", "{", "}", "\\", '"'))
        if needs_quote:
            return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return s

    return "{" + ",".join(quote(e) for e in elements) + "}"


def seed_stores(scale: float) -> None:
    start = time.time()
    rng.seed(random.random())
    _store_ids.clear()
    _store_cuisines.clear()
    _store_price_tiers.clear()

    if not _merchant_store_allocs:
        logger.warning("No merchant allocations available; run seed_merchants first.")
        _timings["stores"] = (0, time.time() - start)
        return

    now = _SEED_BASE_NOW
    conn = _get_conn()
    try:
        merchant_id_texts = [merchant_id for merchant_id, _ in _merchant_store_allocs]
        merchant_brand_by_id: dict[str, str] = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT merchant_id::text, brand_name FROM merchants WHERE merchant_id::text = ANY(%s)",
                (merchant_id_texts,),
            )
            for row in cur.fetchall():
                if len(row) == 2:
                    merchant_brand_by_id[str(row[0])] = row[1]

        buf = io.StringIO()
        writer = csv.writer(buf)
        for merchant_id, store_count in _merchant_store_allocs:
            merchant_brand = merchant_brand_by_id.get(merchant_id, fake.company())
            is_chain = merchant_brand in _RNG_CHAIN_SET
            for _ in range(store_count):
                store_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
                city = rng.choices(_CITY_NAMES, weights=_CITY_WEIGHTS, k=1)[0]
                if city not in _CITY_POSTCODE_AREAS:
                    city = rng.choice(list(_CITY_POSTCODE_AREAS))
                _, lat, lon, county = _UK_CITY_DATA[city]
                cuisine_count = rng.choices([1, 2, 3], weights=[50, 35, 15], k=1)[0]
                cuisines = []
                seeded_candidates = rng.choices(_CUISINE_NAMES, weights=_CUISINE_WEIGHTS, k=3)
                for cuisine in seeded_candidates:
                    if cuisine not in cuisines:
                        cuisines.append(cuisine)
                    if len(cuisines) >= cuisine_count:
                        break
                while len(cuisines) < cuisine_count:
                    extra = rng.choices(_CUISINE_NAMES, weights=_CUISINE_WEIGHTS, k=1)[0]
                    if extra not in cuisines:
                        cuisines.append(extra)

                cuisine_types = _pg_array_literal(cuisines)
                price_tier = int(rng.choices([1, 2, 3, 4], weights=[30, 45, 20, 5], k=1)[0])
                accepts_cash = rng.random() < 0.05
                accepts_in_store = rng.random() < 0.75
                accepts_delivery = rng.random() < 0.92
                accepts_pickup = rng.random() < 0.88
                is_verified = rng.random() < 0.95
                created_at = (now - timedelta(days=rng.randint(365, 730))).replace(microsecond=0)
                pos_system = rng.choices(_POS_SYSTEM_NAMES, weights=_POS_SYSTEM_WEIGHTS, k=1)[0]

                avg_prep_time = 20
                first_cuisine = cuisines[0]
                if first_cuisine == "Pizza":
                    avg_prep_time = 18
                elif first_cuisine == "Indian":
                    avg_prep_time = 25
                elif first_cuisine == "Sushi":
                    avg_prep_time = 30
                elif first_cuisine == "Burger":
                    avg_prep_time = 12
                elif first_cuisine == "Chinese":
                    avg_prep_time = 20
                elif first_cuisine == "Fish & Chips" or first_cuisine == "Kebab":
                    avg_prep_time = 15
                elif first_cuisine == "Turkish":
                    avg_prep_time = 20
                elif first_cuisine == "Thai":
                    avg_prep_time = 22
                elif first_cuisine == "Japanese":
                    avg_prep_time = 25
                elif first_cuisine == "Caribbean":
                    avg_prep_time = 20
                elif first_cuisine == "British" or first_cuisine == "Pub":
                    avg_prep_time = 18
                elif first_cuisine == "Vietnamese" or first_cuisine == "Lebanese":
                    avg_prep_time = 20
                elif first_cuisine == "Polish":
                    avg_prep_time = 18
                elif first_cuisine == "Italian":
                    avg_prep_time = 20
                elif first_cuisine == "American":
                    avg_prep_time = 15

                store_name = (
                    f"{merchant_brand} {city}" if is_chain else f"{fake.company()} {city}"
                )[:255]
                writer.writerow(
                    [
                        store_id,
                        merchant_id,
                        store_name,
                        f"SC{rng.randint(10000, 99999)}",
                        cuisine_types,
                        price_tier,
                        fake.street_address()[:255],
                        "",
                        city,
                        county,
                        random_uk_postcode(city, rng=rng),
                        "GB",
                        float(lat + rng.gauss(0, 0.02)),
                        float(lon + rng.gauss(0, 0.02)),
                        "Europe/London",
                        f"+44 {rng.randint(1000000000, 9999999999)}",
                        pos_system,
                        "API",
                        round(rng.uniform(3.0, 8.0), 2),
                        avg_prep_time,
                        accepts_cash,
                        accepts_in_store,
                        accepts_delivery,
                        accepts_pickup,
                        True,
                        is_verified,
                        created_at.strftime("%Y-%m-%d %H:%M:%S+00"),
                        0.0,
                    ]
                )
                _store_ids.append(store_id)
                _store_cuisines[store_id] = cuisines
                _store_price_tiers[store_id] = price_tier

        buf.seek(0)
        with conn.cursor() as cur:
            cur.copy_expert(
                "COPY stores (store_id, merchant_id, store_name, store_code, cuisine_types, price_tier, address_line_1, address_line_2, city, county, postcode, country, latitude, longitude, timezone, phone, pos_system, pos_integration_type, delivery_radius_km, avg_prep_time_min, accepts_cash, accepts_in_store, accepts_delivery, accepts_pickup, is_active, is_verified, created_at, risk_score) FROM STDIN WITH (FORMAT csv)",
                buf,
            )
        conn.commit()
        _timings["stores"] = (len(_store_ids), time.time() - start)
    finally:
        conn.close()


def seed_store_hours() -> None:
    start = time.time()
    rng.seed(random.random())
    store_count = len(_store_ids)
    if store_count == 0:
        _timings["store_hours"] = (0, 0.0)
        return
    conn = _get_conn()
    try:
        buf = io.StringIO()
        writer = csv.writer(buf)
        row_count = 0
        for store_id in _store_ids:
            pattern = rng.choices(_STORE_HOUR_PATTERNS, weights=_STORE_HOUR_WEIGHTS, k=1)[0]
            open_time, close_time = _STORE_HOUR_WINDOWS[pattern]

            closed_day = None
            if pattern != "24h" and rng.random() < 0.10:
                closed_day = rng.choice([0, 1])

            for day_of_week in range(7):
                if day_of_week == closed_day:
                    continue
                writer.writerow([store_id, day_of_week, open_time, close_time])
                row_count += 1
        buf.seek(0)
        with conn.cursor() as cur:
            cur.copy_expert(
                "COPY store_hours (store_id, day_of_week, open_time, close_time) FROM STDIN WITH (FORMAT csv)",
                buf,
            )
        conn.commit()
        _timings["store_hours"] = (row_count, time.time() - start)
    finally:
        conn.close()


def _compute_price(category: str, cuisine: str, price_tier: int) -> int:
    cat_map: dict[str, tuple[int, int]] = _CUISINE_PRICE_RANGES.get(category, {})
    price_range = cat_map.get(cuisine, cat_map.get("default", (500, 1000)))
    lo, hi = price_range
    tier_mult = {1: 0.80, 2: 1.00, 3: 1.30, 4: 1.70}.get(price_tier, 1.00)
    noise = 1.0 + rng.uniform(-0.20, 0.20)
    raw = rng.randint(lo, hi) * tier_mult * noise
    return max(50, int(round(raw / 10.0)) * 10)


def seed_menu_items(scale: float) -> None:
    start = time.time()
    rng.seed(random.random())
    if not _store_ids:
        _timings["menu_items"] = (0, time.time() - start)
        return
    n_stores = len(_store_ids)
    target_item_count = max(int(80000 * scale), n_stores * 4)
    # Pre-allocate item counts per store, summing to target_item_count.
    base = target_item_count // n_stores
    remainder = target_item_count - base * n_stores
    per_store: list[int] = [base] * n_stores
    for i in rng.sample(range(n_stores), k=remainder):
        per_store[i] += 1
    # Clamp to [4, 24] per store while keeping total; any overflow handled by trimming.
    per_store = [max(4, min(24, c)) for c in per_store]
    logger.debug("Menu item target count: %d", target_item_count)

    conn = _get_conn()
    total_items = 0
    try:
        buf = io.StringIO()
        writer = csv.writer(buf)
        for idx, store_id in enumerate(_store_ids):
            store_cuisines = _store_cuisines.get(store_id, [])
            cuisines = store_cuisines if store_cuisines else ["Other"]
            cuisine = cuisines[0]
            price_tier = _store_price_tiers.get(store_id, 2)
            item_count = per_store[idx]

            templates = CUISINE_MENU_TEMPLATES.get(cuisine, CUISINE_MENU_TEMPLATES["Other"])
            if len(templates) < item_count:
                sampled_templates = rng.choices(templates, k=item_count)
            else:
                sampled_templates = rng.sample(templates, k=item_count)

            for item_name, category, is_hot_food in sampled_templates:
                price_pence = _compute_price(category, cuisine, price_tier)
                created_at = (_SEED_BASE_NOW - timedelta(days=rng.randint(300, 700))).replace(
                    microsecond=0
                )
                writer.writerow(
                    [
                        str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                        store_id,
                        item_name,
                        category,
                        price_pence,
                        is_hot_food,
                        True,
                        created_at.strftime("%Y-%m-%d %H:%M:%S+00"),
                    ]
                )
                total_items += 1

        buf.seek(0)
        with conn.cursor() as cur:
            cur.copy_expert(
                "COPY menu_items (item_id, store_id, item_name, category, price_pence, is_hot_food, is_available, created_at) FROM STDIN WITH (FORMAT csv)",
                buf,
            )
        conn.commit()
        _timings["menu_items"] = (total_items, time.time() - start)
    finally:
        conn.close()


def _lognormal_sample(mu_log: float, sigma: float) -> float:
    return math.exp(rng.gauss(mu_log, sigma))


def seed_drivers(scale: float) -> None:
    start = time.time()
    rng.seed(random.random())
    target = max(1, int(2000 * scale))
    now = _SEED_BASE_NOW
    conn = _get_conn()
    try:
        buf = io.StringIO()
        writer = csv.writer(buf)
        vehicle_types = ["BIKE", "EBIKE", "SCOOTER", "CAR", "WALK"]
        vehicle_weights = [35, 25, 20, 18, 2]
        for driver_idx in range(target):
            driver_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            first_name = fake.first_name()[:100]
            last_name = fake.last_name()[:100]
            domain = rng.choice(_DRIVER_EMAIL_DOMAINS)
            email_base = f"{first_name.lower()}.{last_name.lower()}".replace("'", "").replace(
                " ", ""
            )
            email = f"{email_base}{driver_idx}{rng.randint(1, 9999)}@{domain}"
            email = email[:254]  # max email length
            phone = f"+44 7{rng.randint(100000000, 999999999)}"
            vehicle_type = rng.choices(vehicle_types, weights=vehicle_weights, k=1)[0]
            if vehicle_type in ("CAR", "SCOOTER"):
                letters2 = "".join(rng.choices(string.ascii_uppercase, k=2))
                digits2 = "".join(str(rng.randint(0, 9)) for _ in range(2))
                letters3 = "".join(rng.choices(string.ascii_uppercase, k=3))
                licence_plate = f"{letters2}{digits2} {letters3}"
            else:
                licence_plate = ""
            onboarded_at = (now - timedelta(days=rng.randint(30, 1000))).replace(microsecond=0)
            rating = round(max(3.0, min(5.0, rng.gauss(4.7, 0.3))), 2)
            completed = int(min(5000, max(0, _lognormal_sample(4.6, 1.0))))
            home_city = rng.choices(_CITY_NAMES, weights=_CITY_WEIGHTS, k=1)[0]
            writer.writerow(
                [
                    driver_id,
                    first_name,
                    last_name,
                    email,
                    phone,
                    vehicle_type,
                    licence_plate,
                    onboarded_at.strftime("%Y-%m-%d %H:%M:%S+00"),
                    "ACTIVE",
                    f"{rating:.2f}",
                    completed,
                    home_city,
                    "0.0",
                ]
            )
        buf.seek(0)
        with conn.cursor() as cur:
            cur.copy_expert(
                "COPY drivers (driver_id, first_name, last_name, email, phone, vehicle_type, "
                "licence_plate, onboarded_at, status, rating, completed_deliveries, home_city, "
                "risk_score) FROM STDIN WITH (FORMAT csv)",
                buf,
            )
        conn.commit()
        _timings["drivers"] = (target, time.time() - start)
    finally:
        conn.close()


def _user_worker(
    worker_idx: int,
    start_idx: int,
    end_idx: int,
    seed_value: int,
    db_url: str,
) -> tuple[int, int, int]:
    """
    Worker for parallelised user seeding.
    Returns (users_written, addresses_written, payments_written).
    """
    import csv as _csv
    import io as _io
    import random as _random
    import uuid as _uuid
    from datetime import date as _date
    from datetime import datetime as _datetime
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    import psycopg2 as _psycopg2
    from faker import Faker as _Faker
    from shared.uk_data import (
        CARD_BRANDS,
        DISPOSABLE_DOMAIN_RATE,
        DISPOSABLE_EMAIL_DOMAINS,
        EMAIL_DOMAINS,
        UK_CARD_ISSUERS,
        UK_CITIES,
        random_uk_postcode,
    )

    _wrng = _random.Random(seed_value + worker_idx * 1000003)
    _Faker.seed(seed_value + worker_idx)
    _fake = _Faker("en_GB")

    city_names = [c[0] for c in UK_CITIES]
    city_weights = [c[1] for c in UK_CITIES]
    city_lat_lon = {c[0]: (c[2], c[3]) for c in UK_CITIES}

    email_domains = [d for d, _ in EMAIL_DOMAINS]
    email_weights = [w for _, w in EMAIL_DOMAINS]
    disposable_domains = DISPOSABLE_EMAIL_DOMAINS

    card_issuer_names = [i[0] for i in UK_CARD_ISSUERS]
    card_issuer_bins = [i[1] for i in UK_CARD_ISSUERS]
    card_issuer_funding = [i[2] for i in UK_CARD_ISSUERS]
    card_issuer_digital = [i[3] for i in UK_CARD_ISSUERS]
    card_issuer_weights = [i[4] for i in UK_CARD_ISSUERS]
    card_brands = [b for b, _ in CARD_BRANDS]
    card_brand_weights = [w for _, w in CARD_BRANDS]

    uk_isp_prefixes = [
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

    base_now = _datetime(2025, 1, 1, 0, 0, 0, tzinfo=_tz.utc)
    sim_now = base_now

    account_statuses = ["ACTIVE", "SUSPENDED", "BANNED", "DELETED"]
    account_status_weights = [95, 3, 1, 1]
    risk_tiers = ["TRUSTED", "STANDARD", "ELEVATED", "HIGH_RISK"]
    risk_tier_weights = [10, 80, 8, 2]
    referral_sources = ["ORGANIC", "GOOGLE_ADS", "FB_ADS", "REFERRAL", "TV", "OTHER"]
    referral_source_weights = [40, 25, 15, 10, 5, 5]
    address_types = ["RESIDENTIAL", "COMMERCIAL", "STUDENT_HALL", "HOTEL"]
    address_type_weights = [85, 8, 5, 2]
    payment_types = ["CREDIT_CARD", "DEBIT_CARD", "PAYPAL", "APPLE_PAY", "GOOGLE_PAY", "GIFT_CARD"]
    payment_type_weights = [35, 35, 15, 10, 4, 1]

    password_hash = "$2b$10$SIMULATED_HASH_DO_NOT_USE_IN_PROD"

    user_buf = _io.StringIO()
    addr_buf = _io.StringIO()
    pay_buf = _io.StringIO()
    user_writer = _csv.writer(user_buf)
    addr_writer = _csv.writer(addr_buf)
    pay_writer = _csv.writer(pay_buf)

    users_written = 0
    addresses_written = 0
    payments_written = 0

    referral_pool: list[tuple[_datetime, str]] = []
    for _ in range(start_idx, end_idx):
        user_id = str(_uuid.UUID(int=_wrng.getrandbits(128), version=4))

        first_name = _fake.first_name()[:100]
        last_name = _fake.last_name()[:100]

        if _wrng.random() < DISPOSABLE_DOMAIN_RATE:
            domain = _wrng.choice(disposable_domains)
        else:
            domain = _wrng.choices(email_domains, weights=email_weights, k=1)[0]
        _clean_name = (
            first_name.lower().replace("'", "").replace(" ", "")
            + "."
            + last_name.lower().replace("'", "").replace(" ", "")
        )
        email_local = f"{_clean_name}{worker_idx}{_wrng.randint(1, 999999)}"
        email_local = "".join(
            c for c in email_local if (c.isascii() and c.isalnum()) or c in "._-"
        )[:100]
        email = f"{email_local}@{domain}"

        phone = ""
        phone_verified_at = ""
        if _wrng.random() < 0.90:
            phone = f"+44 7{_wrng.randint(100000000, 999999999)}"
            phone_verified_at = ""

        age_years = int(max(18, min(75, _wrng.gauss(35, 12))))
        dob = _date(
            sim_now.year - age_years, _wrng.randint(1, 12), _wrng.randint(1, 28)
        ).isoformat()

        account_status = _wrng.choices(account_statuses, weights=account_status_weights, k=1)[0]
        risk_tier = _wrng.choices(risk_tiers, weights=risk_tier_weights, k=1)[0]
        referral_source = _wrng.choices(referral_sources, weights=referral_source_weights, k=1)[0]
        referred_by = ""

        exp_days = int(min(1500, max(1, _wrng.expovariate(1.0 / 400.0))))
        created_at = (sim_now - _td(days=exp_days)).replace(microsecond=0)
        referral_pool.append((created_at, user_id))

        if _wrng.random() < 0.20 and len(referral_pool) > 1:
            idx = _wrng.randrange(len(referral_pool) - 1)
            if referral_pool[idx][0] < created_at:
                referred_by = referral_pool[idx][1]

        created_at_str = created_at.strftime("%Y-%m-%d %H:%M:%S+00")
        if phone and _wrng.random() < 0.85:
            phone_verified_at = (created_at + _td(days=_wrng.randint(0, 30))).strftime(
                "%Y-%m-%d %H:%M:%S+00"
            )

        prefix = _wrng.choice(uk_isp_prefixes)
        signup_ip = f"{prefix}.{_wrng.randint(0, 255)}.{_wrng.randint(1, 254)}"

        signup_country = (
            "GB" if _wrng.random() < 0.97 else _wrng.choice(["US", "DE", "FR", "IE", "AU"])
        )
        city = _wrng.choices(city_names, weights=city_weights, k=1)[0]
        signup_postcode = random_uk_postcode(city, rng=_wrng)

        email_verified_at = ""
        if _wrng.random() < 0.80:
            delta_hours = _wrng.uniform(0, 24)
            email_verified_at = (created_at + _td(hours=delta_hours)).strftime(
                "%Y-%m-%d %H:%M:%S+00"
            )

        user_writer.writerow(
            [
                user_id,
                email,
                email_verified_at,
                phone,
                phone_verified_at,
                password_hash,
                first_name,
                last_name,
                dob,
                account_status,
                risk_tier,
                referral_source,
                referred_by,
                signup_ip,
                "",
                signup_country,
                signup_postcode,
                "",
                created_at_str,
                created_at_str,
                "",
            ]
        )
        users_written += 1

        n_addr = _wrng.choices([1, 2, 3], weights=[60, 30, 10], k=1)[0]
        lat, lon = city_lat_lon.get(city, (51.5074, -0.1278))
        addr_ids: list[str] = []
        for a_idx in range(n_addr):
            addr_id = str(_uuid.UUID(int=_wrng.getrandbits(128), version=4))
            addr_ids.append(addr_id)
            addr_city = city
            addr_postcode = random_uk_postcode(addr_city, rng=_wrng)
            addr_lat = lat + _wrng.gauss(0, 0.02)
            addr_lon = lon + _wrng.gauss(0, 0.02)
            addr_type = _wrng.choices(address_types, weights=address_type_weights, k=1)[0]
            is_default = a_idx == 0
            addr_writer.writerow(
                [
                    addr_id,
                    user_id,
                    f"{'Home' if a_idx == 0 else 'Address ' + str(a_idx + 1)}",
                    _fake.street_address()[:255],
                    "",
                    addr_city,
                    "",
                    addr_postcode,
                    "GB",
                    f"{addr_lat:.7f}",
                    f"{addr_lon:.7f}",
                    is_default,
                    addr_type,
                    "",
                    "0",
                    created_at_str,
                    created_at_str,
                ]
            )
            addresses_written += 1

        n_pay = _wrng.choices([1, 2, 3], weights=[20, 60, 20], k=1)[0]
        for p_idx in range(n_pay):
            pay_id = str(_uuid.UUID(int=_wrng.getrandbits(128), version=4))
            pay_type = _wrng.choices(payment_types, weights=payment_type_weights, k=1)[0]
            is_default_pay = p_idx == 0 and _wrng.random() < 0.70

            card_bin = ""
            card_last_four = ""
            card_brand = ""
            card_funding = ""
            card_issuer_country = ""
            card_issuer_bank = ""
            is_digital = False
            avs_result = ""
            cvv_result = ""
            exp_month = ""
            exp_year = ""
            billing_addr_id = ""

            if pay_type in ("CREDIT_CARD", "DEBIT_CARD", "PREPAID_CARD"):
                funding_type_for_pay = {
                    "CREDIT_CARD": "CREDIT",
                    "DEBIT_CARD": "DEBIT",
                    "PREPAID_CARD": "PREPAID",
                }
                target_funding = funding_type_for_pay[pay_type]
                eligible_issuers = [
                    idx
                    for idx, funding in enumerate(card_issuer_funding)
                    if funding == target_funding
                ]
                if eligible_issuers:
                    issuer_weights = [card_issuer_weights[idx] for idx in eligible_issuers]
                    issuer_idx = eligible_issuers[
                        _wrng.choices(range(len(eligible_issuers)), weights=issuer_weights, k=1)[0]
                    ]
                else:
                    issuer_idx = _wrng.choices(
                        range(len(card_issuer_names)), weights=card_issuer_weights, k=1
                    )[0]
                card_bin = _wrng.choice(card_issuer_bins[issuer_idx])
                card_last_four = "".join(str(_wrng.randint(0, 9)) for _ in range(4))
                card_funding = target_funding
                is_digital = card_issuer_digital[issuer_idx]
                card_issuer_bank = card_issuer_names[issuer_idx]
                if card_bin.startswith(("4",)):
                    card_brand = "VISA"
                elif card_bin.startswith(("5",)):
                    card_brand = "MASTERCARD"
                elif card_bin.startswith(("3",)):
                    card_brand = "AMEX"
                else:
                    card_brand = _wrng.choices(card_brands, weights=card_brand_weights, k=1)[0]
                card_issuer_country = (
                    "GB" if _wrng.random() < 0.88 else _wrng.choice(["US", "DE", "FR", "IE"])
                )
                avs_result = "MATCH" if _wrng.random() < 0.95 else "PARTIAL"
                cvv_result = "MATCH" if _wrng.random() < 0.99 else "NO_MATCH"
                exp_month_val = _wrng.randint(1, 12)
                exp_year_val = sim_now.year + _wrng.randint(1, 4)
                exp_month = str(exp_month_val)
                exp_year = str(exp_year_val)
                if addr_ids and _wrng.random() < 0.90:
                    billing_addr_id = addr_ids[0]

            pay_writer.writerow(
                [
                    pay_id,
                    user_id,
                    pay_type,
                    f"tok_{pay_id[:8]}" if pay_type in ("CREDIT_CARD", "DEBIT_CARD") else "",
                    card_bin,
                    card_last_four,
                    card_brand,
                    card_funding,
                    card_issuer_country,
                    card_issuer_bank,
                    is_digital,
                    exp_month,
                    exp_year,
                    billing_addr_id,
                    avs_result,
                    cvv_result,
                    is_default_pay,
                    "ACTIVE",
                    "0",
                    "1",
                    created_at_str,
                    "",
                ]
            )
            payments_written += 1

    conn = _psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SET synchronous_commit = OFF;")  # type: ignore[attr-defined]
            cur.execute("SET work_mem = '256MB';")  # type: ignore[attr-defined]
        conn.commit()
        user_buf.seek(0)
        with conn.cursor() as cur:
            cur.copy_expert(  # type: ignore[attr-defined]
                "COPY users (user_id, email, email_verified_at, phone, phone_verified_at, "
                "password_hash, first_name, last_name, date_of_birth, account_status, risk_tier, "
                "referral_source, referred_by_user_id, signup_ip, signup_device_id, signup_country, "
                "signup_postcode, signup_user_agent, created_at, updated_at, last_login_at) "
                "FROM STDIN WITH (FORMAT csv)",
                user_buf,
            )
        conn.commit()
        addr_buf.seek(0)
        with conn.cursor() as cur:
            cur.copy_expert(  # type: ignore[attr-defined]
                "COPY user_addresses (address_id, user_id, label, address_line_1, address_line_2, "
                "city, county, postcode, country, latitude, longitude, is_default, address_type, "
                "delivery_instructions, times_used, created_at, first_used_at) "
                "FROM STDIN WITH (FORMAT csv)",
                addr_buf,
            )
        conn.commit()
        pay_buf.seek(0)
        with conn.cursor() as cur:
            cur.copy_expert(  # type: ignore[attr-defined]
                "COPY payment_methods (payment_method_id, user_id, payment_type, card_token, "
                "card_bin, card_last_four, card_brand, card_funding_type, card_issuer_country, "
                "card_issuer_bank, is_digital_native_bank, card_exp_month, card_exp_year, "
                "billing_address_id, avs_result, cvv_result, is_default, status, times_used, "
                "unique_users_count, created_at, last_used_at) "
                "FROM STDIN WITH (FORMAT csv)",
                pay_buf,
            )
        conn.commit()
    finally:
        conn.close()

    return (users_written, addresses_written, payments_written)


def seed_users_parallel(scale: float, workers: int) -> None:
    global _user_ids
    start = time.time()
    total_users = max(1, int(1_000_000 * scale))
    db_url = _coalesce_database_url(
        os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_SIMULATOR")
    )
    global_seed = int(random.random() * 1_000_000)
    chunk_size = (total_users + workers - 1) // workers
    chunks = []
    for w in range(workers):
        s = w * chunk_size
        e = min(s + chunk_size, total_users)
        if s < e:
            chunks.append((w, s, e, global_seed, db_url))

    logger.info("Seeding %d users with %d workers...", total_users, len(chunks))
    _user_ids = []

    t_users = t_addrs = t_pays = 0
    if workers > 1:
        with multiprocessing.Pool(processes=len(chunks)) as pool:
            results = pool.starmap(_user_worker, chunks)
    else:
        results = [_user_worker(*c) for c in chunks]

    for u, a, p in results:
        t_users += u
        t_addrs += a
        t_pays += p

    elapsed = time.time() - start
    _timings["users"] = (t_users, elapsed)
    _timings["user_addresses"] = (t_addrs, elapsed)
    _timings["payment_methods"] = (t_pays, elapsed)
    logger.info(
        "Users: %d | Addresses: %d | Payment methods: %d in %.1fs (%.0f/s)",
        t_users,
        t_addrs,
        t_pays,
        elapsed,
        t_users / max(elapsed, 0.001),
    )


def seed_promotions() -> None:
    start = time.time()
    conn = _get_conn()
    try:
        buf = io.StringIO()
        writer = csv.writer(buf)
        created_at = _SEED_BASE_NOW.strftime("%Y-%m-%d %H:%M:%S+00")
        for (
            code,
            promo_type,
            disc_pence,
            disc_pct,
            min_order,
            max_per_user,
            vf_offset,
            vu_offset,
            is_targeted,
        ) in _PROMOS_DATA:
            promo_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            valid_from = (_SEED_BASE_NOW + timedelta(days=vf_offset)).strftime(
                "%Y-%m-%d %H:%M:%S+00"
            )
            valid_until = (
                (_SEED_BASE_NOW + timedelta(days=vu_offset)).strftime("%Y-%m-%d %H:%M:%S+00")
                if vu_offset is not None
                else ""
            )
            writer.writerow(
                [
                    promo_id,
                    code,
                    promo_type,
                    disc_pence,
                    disc_pct,
                    min_order,
                    max_per_user,
                    valid_from,
                    valid_until,
                    is_targeted,
                    created_at,
                ]
            )
        buf.seek(0)
        with conn.cursor() as cur:
            cur.copy_expert(
                "COPY promotions (promo_id, promo_code, promo_type, discount_amount_pence, "
                "discount_percent, min_order_pence, max_redemptions_per_user, valid_from, "
                "valid_until, is_targeted, created_at) FROM STDIN WITH (FORMAT csv)",
                buf,
            )
        conn.commit()
        _timings["promotions"] = (len(_PROMOS_DATA), time.time() - start)
    finally:
        conn.close()


def seed_devices(scale: float = 1.0) -> None:
    start = time.time()
    rng.seed(random.random())
    n_devices = max(1, int(300_000 * scale))

    device_types_dist = [
        ("MOBILE_APP", "iOS", 50),
        ("MOBILE_APP", "Android", 35),
        ("MOBILE_WEB", "", 10),
        ("DESKTOP_WEB", "", 4),
        ("TABLET", "", 1),
    ]
    type_choices: list[tuple[str, str]] = []
    for dtype, platform, weight in device_types_dist:
        type_choices.extend([(dtype, platform)] * weight)

    app_versions = ["4.30.1", "4.31.0", "4.32.1", "4.33.0", "4.34.2", "4.35.0"]
    ios_browsers = ["Safari", "Chrome", "Firefox"]
    android_browsers = ["Chrome", "Firefox", "Samsung"]
    desktop_browsers = ["Chrome", "Safari", "Firefox", "Edge"]

    now = _SEED_BASE_NOW
    conn = _get_conn()
    try:
        # ---- devices ----
        device_ids: list[str] = []
        buf = io.StringIO()
        writer = csv.writer(buf)
        device_first_seen_days: dict[str, int] = {}

        for idx in range(n_devices):
            device_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            device_ids.append(device_id)
            dtype, platform = rng.choice(type_choices)

            app_version = ""
            browser_name = ""
            browser_version = ""
            os_version = ""
            screen_res = ""

            if dtype == "MOBILE_APP" and platform == "iOS":
                app_version = rng.choice(app_versions)
                os_version = f"iOS {rng.randint(14, 17)}.{rng.randint(0, 5)}"
                screen_res = rng.choice(["390x844", "414x896", "375x667", "428x926"])
            elif dtype == "MOBILE_APP" and platform == "Android":
                app_version = rng.choice(app_versions)
                os_version = f"Android {rng.randint(10, 14)}"
                screen_res = rng.choice(["360x800", "390x844", "412x915", "360x780"])
            elif dtype == "MOBILE_WEB":
                browser_name = rng.choice(ios_browsers + android_browsers)
                browser_version = f"{rng.randint(100, 120)}.0"
                platform = rng.choice(["iOS", "Android"])
                os_version = (
                    f"iOS {rng.randint(14, 17)}"
                    if platform == "iOS"
                    else f"Android {rng.randint(10, 14)}"
                )
            elif dtype == "DESKTOP_WEB":
                browser_name = rng.choice(desktop_browsers)
                browser_version = f"{rng.randint(100, 120)}.0"
                platform = rng.choice(["Windows", "macOS", "Linux"])
                os_version = platform
                screen_res = rng.choice(["1920x1080", "2560x1440", "1440x900", "1280x720"])
            else:
                app_version = rng.choice(app_versions)
                platform = rng.choice(["iOS", "Android"])
                os_version = (
                    f"iPadOS {rng.randint(14, 17)}"
                    if platform == "iOS"
                    else f"Android {rng.randint(10, 14)}"
                )
                screen_res = rng.choice(["768x1024", "1024x1366", "810x1080"])

            first_seen_days = rng.randint(0, 1200)
            fp_raw = f"{device_id}:{dtype}:{platform}:{idx}"
            device_first_seen_days[device_id] = first_seen_days
            fingerprint = hashlib.sha256(fp_raw.encode()).hexdigest()
            first_seen = (now - timedelta(days=first_seen_days)).strftime("%Y-%m-%d %H:%M:%S+00")

            is_rooted = rng.random() < 0.01
            is_emulator = rng.random() < 0.005
            is_vpn = rng.random() < 0.05

            writer.writerow(
                [
                    device_id,
                    fingerprint,
                    dtype,
                    platform,
                    os_version,
                    app_version,
                    browser_name,
                    browser_version,
                    screen_res,
                    "Europe/London",
                    "en-GB",
                    is_rooted,
                    is_emulator,
                    is_vpn,
                    first_seen,
                    first_seen,
                    "1",
                    "0.0",
                ]
            )

        buf.seek(0)
        with conn.cursor() as cur:
            cur.copy_expert(
                "COPY devices (device_id, device_fingerprint, device_type, platform, os_version, "
                "app_version, browser_name, browser_version, screen_resolution, timezone, language, "
                "is_rooted_jailbroken, is_emulator, is_vpn_detected, first_seen_at, last_seen_at, "
                "unique_users_count, risk_score) FROM STDIN WITH (FORMAT csv)",
                buf,
            )
        conn.commit()
        _timings["devices"] = (n_devices, time.time() - start)
        logger.info("Devices seeded: %d", n_devices)

        # ---- user IDs from DB ----
        with conn.cursor() as cur:
            cur.execute("SELECT user_id::text, created_at FROM users ORDER BY created_at, user_id")
            rows = cur.fetchall()
            user_ids_db = [row[0] for row in rows]
            user_created_at: dict[str, datetime] = {str(row[0]): row[1] for row in rows}

        if not user_ids_db:
            logger.warning("No users found in DB — skipping user_devices seeding")
            _timings["user_devices"] = (0, time.time() - start)
            return

        # ---- Plant 50 shared family devices ----
        n_shared = min(50, len(device_ids))
        shared_device_indices = rng.sample(range(len(device_ids)), n_shared)
        family_device_ids = [device_ids[i] for i in shared_device_indices]
        logger.info(
            "Planted %d shared family devices for Phase 3 awareness",
            n_shared,
        )
        logger.info("Family device IDs: %s", json.dumps(family_device_ids))

        # ---- user_devices ----
        seen_ud_pairs: set[tuple[str, str]] = set()
        ud_buf = io.StringIO()
        ud_writer = csv.writer(ud_buf)
        ud_count = 0
        n_device_pool = len(device_ids)

        for user_id in user_ids_db:
            n_devs = rng.choices([1, 2, 3, 4], weights=[80, 15, 4, 1], k=1)[0]
            if n_devs == 4:
                n_devs += rng.randint(0, 1)

            for didx in rng.sample(range(n_device_pool), min(n_devs, n_device_pool)):
                device_id = device_ids[didx]
                d_age_days = device_first_seen_days[device_id]
                device_first_seen_ts = now - timedelta(days=d_age_days)
                user_ts = user_created_at[user_id]
                earliest = max(device_first_seen_ts, user_ts)
                total_window = int((now - earliest).total_seconds())
                offset_secs = rng.randint(0, total_window) if total_window > 0 else 0
                first_used = (earliest + timedelta(seconds=offset_secs)).strftime(
                    "%Y-%m-%d %H:%M:%S+00"
                )
                pair = (user_id, device_id)
                if pair in seen_ud_pairs:
                    continue
                seen_ud_pairs.add(pair)
                ud_writer.writerow(
                    [
                        user_id,
                        device_id,
                        first_used,
                        first_used,
                        rng.randint(1, 50),
                        False,
                    ]
                )
                ud_count += 1

        # Add shared family links (5-10 users per shared device)
        extra_users = rng.sample(user_ids_db, min(500, len(user_ids_db)))
        for fdi in family_device_ids:
            n_family_users = rng.randint(5, 10)
            family_users = rng.sample(extra_users, min(n_family_users, len(extra_users)))
            for fu_id in family_users:
                d_age_days = device_first_seen_days[fdi]
                device_first_seen_ts = now - timedelta(days=d_age_days)
                user_ts = user_created_at.get(fu_id, device_first_seen_ts)
                earliest = max(device_first_seen_ts, user_ts)
                total_window = int((now - earliest).total_seconds())
                offset_secs = rng.randint(0, total_window) if total_window > 0 else 0
                first_used = (earliest + timedelta(seconds=offset_secs)).strftime(
                    "%Y-%m-%d %H:%M:%S+00"
                )
                pair = (fu_id, fdi)
                if pair in seen_ud_pairs:
                    continue
                seen_ud_pairs.add(pair)
                ud_writer.writerow(
                    [
                        fu_id,
                        fdi,
                        first_used,
                        first_used,
                        rng.randint(1, 20),
                        True,
                    ]
                )
                ud_count += 1

        ud_buf.seek(0)
        with conn.cursor() as cur:
            # psycopg2 copy_expert doesn't support ON CONFLICT, so use a temp table.
            cur.execute("CREATE TEMP TABLE _ud_import (LIKE user_devices INCLUDING ALL)")
            cur.copy_expert(
                "COPY _ud_import (user_id, device_id, first_used_at, last_used_at, "
                "session_count, is_trusted) FROM STDIN WITH (FORMAT csv)",
                ud_buf,
            )
            cur.execute(
                "INSERT INTO user_devices SELECT * FROM _ud_import "
                "ON CONFLICT (user_id, device_id) DO NOTHING"
            )
            cur.execute("DROP TABLE _ud_import")
        conn.commit()

        # Update unique_users_count.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET unique_users_count = COALESCE(subq.cnt, 0) "
                "FROM (SELECT d.device_id, COUNT(ud.user_id) AS cnt "
                "      FROM devices d LEFT JOIN user_devices ud USING (device_id) "
                "      GROUP BY d.device_id) subq "
                "WHERE devices.device_id = subq.device_id"
            )
        conn.commit()

        _timings["user_devices"] = (ud_count, time.time() - start)
        logger.info("User-device links seeded: %d", ud_count)
    finally:
        conn.close()


def vacuum_analyze_all() -> None:
    try:
        conn = _get_conn()
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "VACUUM ANALYZE merchants, stores, store_hours, menu_items, drivers, users, "
                    "user_addresses, payment_methods, devices, user_devices, promotions;"
                )
            logger.info("VACUUM ANALYZE complete")
        except psycopg2.Error as exc:
            logger.warning(
                "VACUUM ANALYZE skipped (insufficient privileges — run manually as a superuser): %s",
                exc,
            )
        finally:
            conn.close()
    except psycopg2.Error as exc:
        logger.warning(
            "VACUUM ANALYZE skipped (insufficient privileges — run manually as a superuser): %s",
            exc,
        )


def print_summary() -> None:
    print("=== SEEDING COMPLETE ===")
    print("Entity                   Rows   Elapsed (s)")
    if not _timings:
        return
    for entity, (row_count, elapsed_secs) in sorted(_timings.items()):
        print(f"{entity:<24}{row_count:6d}{elapsed_secs:12.4f}")


if __name__ == "__main__":
    main()
