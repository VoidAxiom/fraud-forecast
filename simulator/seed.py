from __future__ import annotations

import argparse
import io
import logging
import math
import multiprocessing
import os
import random
import time
import uuid
from datetime import date, timedelta
from typing import Any

import numpy as np
import psycopg2  # type: ignore
from faker import Faker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_SIMULATOR_DB_URL = (
    "postgresql://simulator_user:simulator_dev_password@postgres:5432/fraud_platform"
)
_timings: dict[str, tuple[int, float]] = {}

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
    logger.info("TODO: seed_merchants")
    return


def seed_stores(scale: float) -> None:
    logger.info("TODO: seed_stores")
    return


def seed_store_hours() -> None:
    logger.info("TODO: seed_store_hours")
    return


def seed_menu_items(scale: float) -> None:
    logger.info("TODO: seed_menu_items")
    return


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
