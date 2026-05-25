from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, cast

import redis
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from pythonjsonlogger import jsonlogger
from shared.db import get_engine
from sqlalchemy import text  # type: ignore[import]  # SQLAlchemy 1.4 has no type stubs
from sqlalchemy.engine import Engine  # type: ignore[import]  # SQLAlchemy 1.4 has no type stubs

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo

REDIS_URL_DEFAULT = "redis://redis:6379/0"
STREAM_CHUNK_SIZE = 10_000
BATCH_TTL_SECONDS = 172_800
EUROPE_LONDON = "Europe/London"
LOG = logging.getLogger("batch_compute")


def _configure_logging() -> None:
    """Set up JSON logging once."""
    if LOG.handlers:
        return
    handler = logging.StreamHandler()
    formatter = jsonlogger.JsonFormatter(  # type: ignore[no-untyped-call]
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level"},
    )
    handler.setFormatter(formatter)
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False


def _now() -> datetime:
    """Return the current datetime in London time."""
    return datetime.now(ZoneInfo(EUROPE_LONDON))


def _to_int(row: Mapping[str, Any], key: str, default: int = 0) -> int:
    if key not in row:
        return default
    value = row[key]
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    return int(value)


def _to_str(row: Mapping[str, object], key: str) -> str | None:
    if key not in row:
        return None
    value = row[key]
    if value is None:
        return None
    return str(value)


def _to_datetime(row: Mapping[str, object], key: str) -> datetime | None:
    if key not in row:
        return None
    value = row[key]
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return None


def _days_ago(now: datetime, when: datetime | None) -> int:
    if when is None:
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(EUROPE_LONDON))
    return max((now - when).days, 0)


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 6)


def _fetch_rows(result: Any) -> list[Mapping[str, object]]:
    rows = result.fetchmany(STREAM_CHUNK_SIZE)
    return [cast(Mapping[str, object], row) for row in rows]


def compute_user_batch_features(engine: Engine, r: redis.Redis[str]) -> None:
    """Compute all user-level batch features."""
    now = _now()
    query = text(
        """
        WITH all_orders AS (
          SELECT order_id, user_id, placed_at, total_pence, device_id,
                 payment_method_id, delivery_address_id, store_city
          FROM orders
          UNION ALL
          SELECT order_id, user_id, placed_at, total_pence, device_id,
                 payment_method_id, delivery_address_id, store_city
          FROM orders_archive
        )
        SELECT
          o.user_id,
          u.created_at,
          COUNT(o.order_id) AS lifetime_order_count,
          COALESCE(SUM(o.total_pence), 0) AS lifetime_spend_pence,
          COALESCE(ROUND(AVG(o.total_pence)), 0)::BIGINT AS avg_order_value_pence,
          COALESCE(COUNT(cb.order_id), 0) AS lifetime_chargeback_count,
          COALESCE(COUNT(r.refund_id), 0) AS lifetime_refund_count,
          COUNT(DISTINCT o.device_id) AS unique_devices_used,
          COUNT(DISTINCT o.payment_method_id) AS unique_payment_methods_used,
          COUNT(DISTINCT o.delivery_address_id) AS unique_delivery_addresses,
          COUNT(DISTINCT o.store_city) AS distinct_cities_ordered_from,
          MAX(o.placed_at) AS last_placed_at
        FROM all_orders AS o
        JOIN users AS u ON u.user_id = o.user_id
        LEFT JOIN chargebacks AS cb ON cb.order_id = o.order_id
        LEFT JOIN refunds AS r ON r.order_id = o.order_id
        GROUP BY o.user_id, u.created_at
        """
    )

    try:
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(query)
            while True:
                rows = _fetch_rows(result)
                if not rows:
                    break

                pipe = r.pipeline(transaction=False)
                for row in rows:
                    user_id = _to_str(row, "user_id")
                    if user_id is None:
                        continue

                    order_count = _to_int(row, "lifetime_order_count")
                    chargeback_count = _to_int(row, "lifetime_chargeback_count")
                    spend = _to_int(row, "lifetime_spend_pence")
                    avg_order_value = _to_int(row, "avg_order_value_pence")
                    account_age_days = _days_ago(now, _to_datetime(row, "created_at"))
                    days_since_last_order = _days_ago(
                        now,
                        _to_datetime(row, "last_placed_at"),
                    )
                    chargeback_rate = _rate(chargeback_count, order_count)

                    pipe.hset(
                        f"fs:user:{user_id}:batch",
                        mapping={
                            "lifetime_order_count": order_count,
                            "lifetime_spend_pence": spend,
                            "avg_order_value_pence": avg_order_value,
                            "lifetime_chargeback_count": chargeback_count,
                            "lifetime_refund_count": _to_int(
                                row,
                                "lifetime_refund_count",
                            ),
                            "lifetime_chargeback_rate": chargeback_rate,
                            "unique_devices_used": _to_int(row, "unique_devices_used"),
                            "unique_payment_methods_used": _to_int(
                                row,
                                "unique_payment_methods_used",
                            ),
                            "unique_delivery_addresses": _to_int(
                                row,
                                "unique_delivery_addresses",
                            ),
                            "account_age_days": account_age_days,
                            "days_since_last_order": days_since_last_order,
                            "distinct_cities_ordered_from": _to_int(
                                row,
                                "distinct_cities_ordered_from",
                            ),
                        },
                    )
                    pipe.expire(f"fs:user:{user_id}:batch", BATCH_TTL_SECONDS)

                pipe.execute()
    except Exception as exc:  # pragma: no cover - exception-path is validated by unit tests
        LOG.exception("batch_compute_entity_error", extra={"entity": "user", "error": str(exc)})
        raise


def compute_device_batch_features(engine: Engine, r: redis.Redis[str]) -> None:
    """Compute all device-level batch features."""
    now = _now()
    query = text(
        """
        WITH all_orders AS (
          SELECT order_id, user_id, device_id, payment_method_id
          FROM orders
          UNION ALL
          SELECT order_id, user_id, device_id, payment_method_id
          FROM orders_archive
        )
        SELECT
          o.device_id,
          COUNT(o.order_id) AS lifetime_order_count,
          COALESCE(COUNT(cb.order_id), 0) AS lifetime_chargeback_count,
          COUNT(DISTINCT o.user_id) AS unique_users_lifetime,
          COUNT(DISTINCT o.payment_method_id) AS distinct_payment_methods_lifetime,
          d.first_seen_at
        FROM all_orders AS o
        LEFT JOIN devices AS d ON d.device_id = o.device_id
        LEFT JOIN chargebacks AS cb ON cb.order_id = o.order_id
        WHERE o.device_id IS NOT NULL
        GROUP BY o.device_id, d.first_seen_at
        """
    )

    try:
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(query)
            while True:
                rows = _fetch_rows(result)
                if not rows:
                    break

                pipe = r.pipeline(transaction=False)
                for row in rows:
                    device_id = _to_str(row, "device_id")
                    if device_id is None:
                        continue

                    order_count = _to_int(row, "lifetime_order_count")
                    chargeback_count = _to_int(row, "lifetime_chargeback_count")
                    rate = _rate(chargeback_count, order_count)
                    first_seen_days_ago = _days_ago(
                        now,
                        _to_datetime(row, "first_seen_at"),
                    )

                    pipe.hset(
                        f"fs:device:{device_id}:batch",
                        mapping={
                            "lifetime_order_count": order_count,
                            "lifetime_chargeback_rate": rate,
                            "unique_users_lifetime": _to_int(
                                row,
                                "unique_users_lifetime",
                            ),
                            "first_seen_days_ago": first_seen_days_ago,
                            "distinct_payment_methods_lifetime": _to_int(
                                row,
                                "distinct_payment_methods_lifetime",
                            ),
                        },
                    )
                    pipe.expire(f"fs:device:{device_id}:batch", BATCH_TTL_SECONDS)
                pipe.execute()
    except Exception as exc:  # pragma: no cover - exception-path is validated by unit tests
        LOG.exception("batch_compute_entity_error", extra={"entity": "device", "error": str(exc)})
        raise


def compute_payment_batch_features(engine: Engine, r: redis.Redis[str]) -> None:
    """Compute all payment-method-level batch features."""
    query = text(
        """
        WITH all_orders AS (
          SELECT order_id, payment_method_id, user_id, delivery_address_id
          FROM orders
          UNION ALL
          SELECT order_id, payment_method_id, user_id, delivery_address_id
          FROM orders_archive
        )
        SELECT
          o.payment_method_id,
          COUNT(o.order_id) AS lifetime_order_count,
          COALESCE(COUNT(cb.order_id), 0) AS lifetime_chargeback_count,
          COUNT(DISTINCT o.user_id) AS unique_users_lifetime,
          COUNT(DISTINCT o.delivery_address_id) AS distinct_delivery_addresses_lifetime
        FROM all_orders AS o
        LEFT JOIN chargebacks AS cb ON cb.order_id = o.order_id
        WHERE o.payment_method_id IS NOT NULL
        GROUP BY o.payment_method_id
        """
    )
    try:
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(query)
            while True:
                rows = _fetch_rows(result)
                if not rows:
                    break

                pipe = r.pipeline(transaction=False)
                for row in rows:
                    payment_method_id = _to_str(row, "payment_method_id")
                    if payment_method_id is None:
                        continue

                    order_count = _to_int(row, "lifetime_order_count")
                    chargeback_count = _to_int(row, "lifetime_chargeback_count")
                    rate = _rate(chargeback_count, order_count)

                    pipe.hset(
                        f"fs:payment:{payment_method_id}:batch",
                        mapping={
                            "lifetime_order_count": order_count,
                            "lifetime_chargeback_count": chargeback_count,
                            "lifetime_chargeback_rate": rate,
                            "unique_users_lifetime": _to_int(
                                row,
                                "unique_users_lifetime",
                            ),
                            "distinct_delivery_addresses_lifetime": _to_int(
                                row,
                                "distinct_delivery_addresses_lifetime",
                            ),
                        },
                    )
                    pipe.expire(f"fs:payment:{payment_method_id}:batch", BATCH_TTL_SECONDS)
                pipe.execute()
    except Exception as exc:  # pragma: no cover - exception-path is validated by unit tests
        LOG.exception("batch_compute_entity_error", extra={"entity": "payment", "error": str(exc)})
        raise


def compute_ip_batch_features(engine: Engine, r: redis.Redis[str]) -> None:
    """Compute all IP-level batch features."""
    now = _now()
    query = text(
        """
        WITH all_orders AS (
          SELECT order_id, ip_address, placed_at, user_id
          FROM orders
          UNION ALL
          SELECT order_id, ip_address, placed_at, user_id
          FROM orders_archive
        )
        SELECT
          o.ip_address,
          COUNT(o.order_id) AS lifetime_order_count,
          COUNT(DISTINCT o.user_id) AS unique_users_lifetime,
          COALESCE(COUNT(cb.order_id), 0) AS chargeback_count,
          MIN(o.placed_at) AS first_placed_at
        FROM all_orders AS o
        LEFT JOIN chargebacks AS cb ON cb.order_id = o.order_id
        WHERE o.ip_address IS NOT NULL
        GROUP BY o.ip_address
        """
    )

    try:
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(query)
            while True:
                rows = _fetch_rows(result)
                if not rows:
                    break

                pipe = r.pipeline(transaction=False)
                for row in rows:
                    ip_address = _to_str(row, "ip_address")
                    if ip_address is None:
                        continue

                    order_count = _to_int(row, "lifetime_order_count")
                    chargeback_count = _to_int(row, "chargeback_count")
                    rate = _rate(chargeback_count, order_count)
                    first_seen_days_ago = _days_ago(
                        now,
                        _to_datetime(row, "first_placed_at"),
                    )

                    pipe.hset(
                        f"fs:ip:{ip_address}:batch",
                        mapping={
                            "lifetime_order_count": order_count,
                            "unique_users_lifetime": _to_int(row, "unique_users_lifetime"),
                            "chargeback_rate": rate,
                            "first_seen_days_ago": first_seen_days_ago,
                        },
                    )
                    pipe.expire(f"fs:ip:{ip_address}:batch", BATCH_TTL_SECONDS)
                pipe.execute()
    except Exception as exc:  # pragma: no cover - exception-path is validated by unit tests
        LOG.exception("batch_compute_entity_error", extra={"entity": "ip", "error": str(exc)})
        raise


def compute_store_batch_features(engine: Engine, r: redis.Redis[str]) -> None:
    """Compute all store-level batch features."""
    now = _now()
    thirty_days_ago = now - timedelta(days=30)
    query = text(
        """
        WITH all_orders AS (
          SELECT order_id, store_id, total_pence, payment_method_id, placed_at
          FROM orders
          UNION ALL
          SELECT order_id, store_id, total_pence, payment_method_id, placed_at
          FROM orders_archive
        )
        SELECT
          o.store_id,
          COALESCE(COUNT(o.order_id), 0) AS total_orders,
          COALESCE(SUM(o.total_pence), 0) AS lifetime_spend_pence,
          SUM(CASE WHEN o.placed_at >= :thirty_days_ago THEN 1 ELSE 0 END) AS total_orders_30d,
          COUNT(
            DISTINCT CASE WHEN o.placed_at >= :thirty_days_ago THEN o.payment_method_id END
          ) AS unique_cards_30d,
          COALESCE(COUNT(cb.order_id), 0) AS chargeback_count
        FROM all_orders AS o
        LEFT JOIN chargebacks AS cb ON cb.order_id = o.order_id
        GROUP BY o.store_id
        """
    )

    try:
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(
                query,
                {"thirty_days_ago": thirty_days_ago},
            )
            while True:
                rows = _fetch_rows(result)
                if not rows:
                    break

                pipe = r.pipeline(transaction=False)
                for row in rows:
                    store_id = _to_str(row, "store_id")
                    if store_id is None:
                        continue

                    total_orders = _to_int(row, "total_orders")
                    total_spend = _to_int(row, "lifetime_spend_pence")
                    avg_order_value = total_spend // total_orders if total_orders > 0 else 0
                    chargeback_count = _to_int(row, "chargeback_count")
                    chargeback_rate = _rate(chargeback_count, total_orders)

                    pipe.hset(
                        f"fs:store:{store_id}:batch",
                        mapping={
                            "avg_order_value_pence": avg_order_value,
                            "chargeback_rate": chargeback_rate,
                            "unique_cards_30d": _to_int(row, "unique_cards_30d"),
                            "total_orders_30d": _to_int(row, "total_orders_30d"),
                        },
                    )
                    pipe.expire(f"fs:store:{store_id}:batch", BATCH_TTL_SECONDS)
                pipe.execute()
    except Exception as exc:  # pragma: no cover - exception-path is validated by unit tests
        LOG.exception("batch_compute_entity_error", extra={"entity": "store", "error": str(exc)})
        raise


def compute_merchant_batch_features(engine: Engine, r: redis.Redis[str]) -> None:
    """Compute all merchant-level batch features."""
    query = text(
        """
        WITH all_orders AS (
          SELECT o.merchant_id, o.order_id, o.store_id
          FROM orders o
          UNION ALL
          SELECT o.merchant_id, o.order_id, o.store_id
          FROM orders_archive o
        ),
        merchant_orders AS (
          SELECT merchant_id, COUNT(order_id) AS total_orders
          FROM all_orders
          GROUP BY merchant_id
        ),
        merchant_chargebacks AS (
          SELECT ao.merchant_id, COUNT(cb.order_id) AS chargeback_count
          FROM all_orders ao
          LEFT JOIN chargebacks cb ON cb.order_id = ao.order_id
          GROUP BY ao.merchant_id
        )
        SELECT
          ao.merchant_id,
          COALESCE(mo.total_orders, 0) AS total_orders,
          COALESCE(mc.chargeback_count, 0) AS lifetime_chargeback_count,
          COUNT(DISTINCT ao.store_id) AS total_stores
        FROM all_orders AS ao
        LEFT JOIN merchant_orders mo ON mo.merchant_id = ao.merchant_id
        LEFT JOIN merchant_chargebacks mc ON mc.merchant_id = ao.merchant_id
        GROUP BY ao.merchant_id, mo.total_orders, mc.chargeback_count
        """
    )

    try:
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(query)
            while True:
                rows = _fetch_rows(result)
                if not rows:
                    break

                pipe = r.pipeline(transaction=False)
                for row in rows:
                    merchant_id = _to_str(row, "merchant_id")
                    if merchant_id is None:
                        continue

                    total_orders = _to_int(row, "total_orders")
                    chargeback_count = _to_int(row, "lifetime_chargeback_count")
                    chargeback_rate = _rate(chargeback_count, total_orders)

                    pipe.hset(
                        f"fs:merchant:{merchant_id}:batch",
                        mapping={
                            "chargeback_rate": chargeback_rate,
                            "total_stores": _to_int(row, "total_stores"),
                        },
                    )
                    pipe.expire(f"fs:merchant:{merchant_id}:batch", BATCH_TTL_SECONDS)
                pipe.execute()
    except Exception as exc:  # pragma: no cover - exception-path is validated by unit tests
        LOG.exception("batch_compute_entity_error", extra={"entity": "merchant", "error": str(exc)})
        raise


def compute_email_domain_batch_features(engine: Engine, r: redis.Redis[str]) -> None:
    """Compute all email-domain batch features."""
    # Lifetime aggregation is intentional per spec/PHASE_4.md feature catalog.
    # No rolling window is specified for email_domain batch features.
    query = text(
        """
        WITH all_orders AS (
          SELECT order_id, user_email_domain
          FROM orders
          UNION ALL
          SELECT order_id, user_email_domain
          FROM orders_archive
        )
        SELECT
          o.user_email_domain AS email_domain,
          COUNT(o.order_id) AS total_orders,
          COALESCE(COUNT(cb.order_id), 0) AS chargeback_count
        FROM all_orders AS o
        LEFT JOIN chargebacks AS cb ON cb.order_id = o.order_id
        GROUP BY o.user_email_domain
        """
    )

    try:
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(query)
            while True:
                rows = _fetch_rows(result)
                if not rows:
                    break

                pipe = r.pipeline(transaction=False)
                for row in rows:
                    email_domain = _to_str(row, "email_domain")
                    if email_domain is None:
                        continue

                    total_orders = _to_int(row, "total_orders")
                    chargeback_count = _to_int(row, "chargeback_count")
                    chargeback_rate = _rate(chargeback_count, total_orders)

                    pipe.hset(
                        f"fs:email_domain:{email_domain}:batch",
                        mapping={
                            "chargeback_rate": chargeback_rate,
                            "total_orders": total_orders,
                        },
                    )
                    pipe.expire(f"fs:email_domain:{email_domain}:batch", BATCH_TTL_SECONDS)
                pipe.execute()
    except Exception as exc:  # pragma: no cover - exception-path is validated by unit tests
        LOG.exception(
            "batch_compute_entity_error", extra={"entity": "email_domain", "error": str(exc)}
        )
        raise


def run_batch() -> None:
    """Compute and write all feature-store batch aggregates."""
    _configure_logging()
    start = time.perf_counter()
    LOG.info(
        "batch_compute_start",
        extra={"event": "batch_compute_start"},
    )

    engine = get_engine("app")
    redis_client: redis.Redis[str] = redis.Redis.from_url(
        os.getenv("REDIS_URL", REDIS_URL_DEFAULT),
        decode_responses=True,
    )
    failures: list[tuple[str, Exception]] = []

    try:
        for entity_name, compute in (
            ("user", compute_user_batch_features),
            ("device", compute_device_batch_features),
            ("payment", compute_payment_batch_features),
            ("ip", compute_ip_batch_features),
            ("store", compute_store_batch_features),
            ("merchant", compute_merchant_batch_features),
            ("email_domain", compute_email_domain_batch_features),
        ):
            try:
                compute(engine=engine, r=redis_client)
            except Exception as exc:  # pragma: no cover - validated by unit tests
                LOG.exception(
                    "batch_compute_entity_failed",
                    extra={"entity": entity_name, "error": str(exc)},
                )
                failures.append((entity_name, exc))
    finally:
        redis_client.close()

    duration_s = time.perf_counter() - start
    if failures:
        LOG.error(
            "batch_compute_partial",
            extra={
                "event": "batch_compute_partial",
                "duration_s": round(duration_s, 3),
                "failed_entities": [entity_name for entity_name, _ in failures],
            },
        )
        raise RuntimeError(
            "batch_compute had "
            f"{len(failures)} entity failures: "
            f"{[entity_name for entity_name, _ in failures]}"
        )

    LOG.info(
        "batch_compute_done",
        extra={
            "event": "batch_compute_done",
            "duration_s": round(duration_s, 3),
        },
    )


def _run_scheduler(
    scheduler_factory: type[BackgroundScheduler] | type[BlockingScheduler],
) -> None:
    scheduler = scheduler_factory(timezone=EUROPE_LONDON)
    scheduler.add_job(
        run_batch,
        trigger=CronTrigger(hour=2, minute=0, timezone=EUROPE_LONDON),
    )
    scheduler.start()


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for batch compute."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--daemon",
        "--serve",
        action="store_true",
        help="Run the APScheduler daemon (daily 02:00 Europe/London).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single batch and exit.",
    )
    return parser


def main() -> None:
    """CLI entry point for one-shot or scheduled batch compute."""
    _configure_logging()
    parser = _build_parser()
    args = parser.parse_args()

    if args.once:
        run_batch()
        return
    if not args.daemon:
        run_batch()
        return

    _run_scheduler(BlockingScheduler)


if __name__ == "__main__":
    _configure_logging()
    main()
