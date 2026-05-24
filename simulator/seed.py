from __future__ import annotations

import argparse
import csv
import io
import logging
import math
import multiprocessing
import os
import random
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import psycopg2  # type: ignore
from faker import Faker
from shared.uk_data import (
    CUISINE_WEIGHTS,
    POS_SYSTEMS,
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
_UK_CITY_DATA = {city: (weight, latitude, longitude, county) for city, weight, latitude, longitude, county in UK_CITIES}
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
        seed_devices()
    vacuum_analyze_all()
    print_summary()


def seed_everything(seed_value: int) -> None:
    random.seed(seed_value)
    np.random.seed(seed_value)
    Faker.seed(seed_value)
    print(f"[seed] seed={seed_value}")


def apply_session_tunings() -> None:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SET synchronous_commit = OFF;")
            cur.execute("SET work_mem = '256MB';")
            cur.execute("SET maintenance_work_mem = '1GB';")
        conn.commit()
    finally:
        conn.close()


def _get_conn() -> Any:
    db_url = _coalesce_database_url(
        os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_SIMULATOR")
    )
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    return conn


def _coalesce_database_url(database_url: str | None) -> str:
    if database_url is None:
        return _DEFAULT_SIMULATOR_DB_URL
    return database_url


def seed_merchants(scale: float) -> None:
    start = time.time()
    rng.seed(random.random())
    _merchant_store_allocs.clear()

    now = datetime.now(timezone.utc)
    target_merchants = max(1, int(5000 * scale))
    target_total_stores = max(1, int(15000 * scale))

    category_names = ["QSR", "CASUAL_DINING", "FINE_DINING", "DARK_KITCHEN", "CONVENIENCE"]
    category_weights = [60, 20, 5, 10, 5]

    raw_allocs: list[tuple[str, int]] = []
    merchant_data: list[tuple[str, str, str, str, str, str, str, str, str]] = []

    for _ in range(target_merchants):
        merchant_id = str(uuid.uuid4())
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

    now = datetime.now(timezone.utc)
    conn = _get_conn()
    try:
        merchant_ids = [uuid.UUID(merchant_id) for merchant_id, _ in _merchant_store_allocs]
        merchant_brand_by_id: dict[str, str] = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT merchant_id::text, brand_name FROM merchants WHERE merchant_id = ANY(%s)",
                (merchant_ids,),
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
                store_id = str(uuid.uuid4())
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

                cuisine_types = "{" + ",".join(cuisines) + "}"
                price_tier = int(np.random.choice([1, 2, 3, 4], p=[0.30, 0.45, 0.20, 0.05]))
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
                        float(lat + np.random.normal(0, 0.02)),
                        float(lon + np.random.normal(0, 0.02)),
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
            pattern = rng.choices(["standard", "late", "lunch"], weights=[0.90, 0.05, 0.05], k=1)[0]
            if pattern == "standard":
                open_time = "11:00:00"
                close_time = "23:00:00"
            elif pattern == "late":
                open_time = "17:00:00"
                close_time = "03:00:00"
            else:
                open_time = "11:00:00"
                close_time = "15:00:00"

            closed_day = None
            if rng.random() < 0.10:
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
    target_item_count = int(80000 * scale)
    scale_factor = scale
    logger.debug(
        "Menu item target hint: %d (scale=%0.2f)",
        target_item_count,
        scale_factor,
    )
    now = datetime.now(timezone.utc)
    if not _store_ids:
        _timings["menu_items"] = (0, time.time() - start)
        return

    conn = _get_conn()
    total_items = 0
    try:
        buf = io.StringIO()
        writer = csv.writer(buf)
        for store_id in _store_ids:
            store_cuisines = _store_cuisines.get(store_id, [])
            cuisines = store_cuisines if store_cuisines else ["Other"]
            cuisine = cuisines[0]
            price_tier = _store_price_tiers.get(store_id, 2)
            raw_item_count = int(np.random.poisson(lam=6))
            item_count = min(12, max(4, raw_item_count))

            templates = CUISINE_MENU_TEMPLATES.get(
                cuisine, CUISINE_MENU_TEMPLATES["Other"]
            )
            if len(templates) < item_count:
                sampled_templates = rng.choices(templates, k=item_count)
            else:
                sampled_templates = rng.sample(templates, k=item_count)

            for item_name, category, is_hot_food in sampled_templates:
                price_pence = _compute_price(category, cuisine, price_tier)
                created_at = (
                    now - timedelta(days=rng.randint(300, 700))
                ).replace(microsecond=0)
                writer.writerow(
                    [
                        str(uuid.uuid4()),
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


def seed_drivers(scale: float) -> None:
    logger.info("TODO: seed_drivers")
    return


def seed_users_parallel(scale: float, workers: int) -> None:
    logger.info("TODO: seed_users_parallel")
    return


def seed_promotions() -> None:
    logger.info("TODO: seed_promotions")
    return


def seed_devices() -> None:
    logger.info("TODO: seed_devices")
    return


def vacuum_analyze_all() -> None:
    conn = _get_conn()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "VACUUM ANALYZE merchants, stores, store_hours, menu_items, drivers, users, "
                "user_addresses, payment_methods, devices, user_devices, promotions;"
            )
    finally:
        conn.close()


def print_summary() -> None:
    print("=== SEEDING COMPLETE ===")
    print("Entity                   Rows   Elapsed (s)")
    if not _timings:
        return
    for entity, (row_count, elapsed_secs) in sorted(_timings.items()):
        print(f"{entity:<24}{row_count:6d}{elapsed_secs:12.4f}")


if __name__ == "__main__":
    main()
