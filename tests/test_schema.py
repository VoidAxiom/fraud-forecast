"""Schema-level invariants from Phase 1-B's migrations on the LIVE primary postgres."""

from __future__ import annotations

import datetime as dt
import contextlib
import os
import uuid
from typing import Generator

import pytest
import shared.db
from sqlalchemy import text
from sqlalchemy.engine import Engine

EXPECTED_BASE_TABLES = {
    "users",
    "user_addresses",
    "devices",
    "user_devices",
    "sessions",
    "payment_methods",
    "merchants",
    "stores",
    "store_hours",
    "menu_items",
    "drivers",
    "promotions",
    "orders",
    "order_items",
    "order_events",
    "fraud_decisions",
    "chargebacks",
    "orders_archive",
    "order_items_archive",
    "order_events_archive",
}


@contextlib.contextmanager
def _scoring_env_override(url: str) -> Generator[None, None, None]:
    """Set DATABASE_URL_SCORING to url and flush engine cache; restore on exit."""
    prior = os.environ.get("DATABASE_URL_SCORING")
    os.environ["DATABASE_URL_SCORING"] = url
    shared.db._engines.pop("scoring", None)
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("DATABASE_URL_SCORING", None)
        else:
            os.environ["DATABASE_URL_SCORING"] = prior
        shared.db._engines.pop("scoring", None)


def test_all_20_tables_exist(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        rows = set(
            conn.execute(
                text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
              AND table_name NOT SIMILAR TO '%_p_____'
              AND table_name != 'alembic_version'
        """)
            )
            .scalars()
            .all()
        )
    assert EXPECTED_BASE_TABLES.issubset(rows), f"missing: {EXPECTED_BASE_TABLES - rows}"


def test_53_weekly_partitions_per_partitioned_parent(db_engine: Engine) -> None:
    parents = [
        "orders",
        "orders_archive",
        "order_items",
        "order_items_archive",
        "order_events",
        "order_events_archive",
    ]
    with db_engine.connect() as conn:
        for parent in parents:
            count = conn.execute(
                text("SELECT count(*) FROM pg_class WHERE relname LIKE :pat AND relkind = 'r'"),
                {"pat": f"{parent}_p_%"},
            ).scalar()
            assert count == 53, f"{parent} has {count} partitions, expected 53"


def test_three_custom_roles_exist(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        roles = set(
            conn.execute(
                text(
                    "SELECT rolname FROM pg_roles WHERE rolname IN "
                    "('scoring_user', 'simulator_user', 'analyst_user')"
                )
            )
            .scalars()
            .all()
        )
    assert roles == {"scoring_user", "simulator_user", "analyst_user"}


def test_scoring_user_cannot_delete() -> None:
    from sqlalchemy.exc import ProgrammingError

    from shared.db import get_engine

    scoring_url = "postgresql://scoring_user:scoring_dev_password@postgres:5432/fraud_platform"
    with _scoring_env_override(scoring_url):
        scoring = get_engine("scoring")
        with pytest.raises(ProgrammingError) as exc_info:  # noqa: SIM117
            with scoring.connect() as conn:
                conn.execute(text("DELETE FROM users WHERE 1 = 0"))
        assert "permission denied" in str(exc_info.value).lower()


def test_scoring_user_cannot_update_total_pence() -> None:
    from sqlalchemy.exc import ProgrammingError

    from shared.db import get_engine

    scoring_url = "postgresql://scoring_user:scoring_dev_password@postgres:5432/fraud_platform"
    with _scoring_env_override(scoring_url):
        scoring = get_engine("scoring")
        with pytest.raises(ProgrammingError) as exc_info:  # noqa: SIM117
            with scoring.connect() as conn:
                conn.execute(text("UPDATE orders SET total_pence = 0 WHERE order_id IS NULL"))
        assert "permission denied" in str(exc_info.value).lower()


def test_scoring_user_can_update_fraud_columns() -> None:
    from shared.db import get_engine

    scoring_url = "postgresql://scoring_user:scoring_dev_password@postgres:5432/fraud_platform"
    with _scoring_env_override(scoring_url):
        scoring = get_engine("scoring")
        with scoring.connect() as conn:
            result = conn.execute(
                text("""
                UPDATE orders SET fraud_score = 0.5, fraud_score_version = 'v1',
                    fraud_decision = 'REVIEW', fraud_rules_triggered = ARRAY[]::varchar[],
                    updated_at = now()
                WHERE order_id IS NULL
            """)
            )
            assert result.rowcount == 0


def test_pgcrypto_gen_random_uuid(db_engine: Engine) -> None:
    with db_engine.connect() as conn:
        val = conn.execute(text("SELECT gen_random_uuid()")).scalar()
    assert isinstance(val, uuid.UUID)


def test_ensure_future_partitions_idempotent(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(text("SELECT ensure_future_partitions(2)"))
        conn.execute(text("SELECT ensure_future_partitions(2)"))
    # Both calls returned without error — that is the idempotency assertion.


def test_partition_routing(db_engine: Engine) -> None:
    """Insert across 2 weeks; verify rows land in correct child partitions via tableoid."""
    now = dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=dt.timezone.utc)
    wk1, wk2 = now, now + dt.timedelta(weeks=1)
    oid1, oid2 = uuid.uuid4(), uuid.uuid4()
    user_id, store_id, merchant_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    with db_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (user_id, email, password_hash) VALUES (:u, :e, 'p')"),
            {"u": user_id, "e": f"partition-test-{user_id}@example.com"},
        )
        conn.execute(
            text(
                "INSERT INTO merchants (merchant_id, legal_name, brand_name) VALUES (:m, 'PT', 'PT')"
            ),
            {"m": merchant_id},
        )
        conn.execute(
            text(
                """
            INSERT INTO stores (store_id, merchant_id, store_name, address_line_1,
                                city, postcode, latitude, longitude)
            VALUES (:s, :m, 'PT', '1 X', 'London', 'SW1A 1AA', 51.5074, -0.1278)
        """
            ),
            {"s": store_id, "m": merchant_id},
        )
        for oid, placed_at in [(oid1, wk1), (oid2, wk2)]:
            conn.execute(
                text("""
                INSERT INTO orders (order_id, placed_at, order_number, order_status,
                                    order_channel, order_type, user_id,
                                    user_account_age_days, user_total_orders_lifetime,
                                    user_total_orders_30d, user_total_spend_lifetime_pence,
                                    user_email, user_email_domain, store_id, merchant_id,
                                    store_city, store_latitude, store_longitude,
                                    item_count, unique_item_count, subtotal_pence,
                                    total_pence, payment_type)
                VALUES (:oid, :pa, :on, 'PLACED', 'WEB', 'DELIVERY', :u, 1, 0, 0, 0,
                        :em, 'example.com', :s, :m, 'London', 51.5074, -0.1278,
                        1, 1, 1000, 1000, 'CREDIT_CARD')
            """),
                {
                    "oid": oid,
                    "pa": placed_at,
                    "on": f"PT-{str(oid)[:8]}",
                    "u": user_id,
                    "em": f"partition-test-{user_id}@example.com",
                    "s": store_id,
                    "m": merchant_id,
                },
            )
        for oid, placed_at in [(oid1, wk1), (oid2, wk2)]:
            iso_y, iso_w, _ = placed_at.isocalendar()
            tableoid_text = conn.execute(
                text(
                    "SELECT (orders.tableoid::regclass)::text FROM orders "
                    "WHERE order_id = :oid AND placed_at = :pa"
                ),
                {"oid": oid, "pa": placed_at},
            ).scalar()
            assert tableoid_text is not None
            assert f"_p_{iso_y}_{iso_w:02d}" in tableoid_text, (
                f"order landed in {tableoid_text}, expected ...{iso_y}_{iso_w:02d}"
            )
        # Cleanup
        conn.execute(
            text("DELETE FROM orders WHERE order_id IN (:o1, :o2)"), {"o1": oid1, "o2": oid2}
        )
        conn.execute(text("DELETE FROM stores WHERE store_id = :s"), {"s": store_id})
        conn.execute(text("DELETE FROM merchants WHERE merchant_id = :m"), {"m": merchant_id})
        conn.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": user_id})


def test_citext_case_insensitive_email(db_engine: Engine) -> None:
    user1, user2 = uuid.uuid4(), uuid.uuid4()
    # Use a unique suffix to avoid conflicts with pre-existing rows.
    suffix = str(uuid.uuid4())[:8]
    email_upper = f"CITEXT-Test-{suffix}@example.com"
    email_lower = f"citext-test-{suffix}@example.com"
    from sqlalchemy.exc import IntegrityError

    try:
        with db_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (user_id, email, password_hash) VALUES (:u, :e, 'p')"),
                {"u": user1, "e": email_upper},
            )
            with pytest.raises(IntegrityError):
                conn.execute(
                    text("INSERT INTO users (user_id, email, password_hash) VALUES (:u, :e, 'p')"),
                    {"u": user2, "e": email_lower},
                )
    finally:
        with db_engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": user1})
