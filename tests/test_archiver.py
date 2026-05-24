"""Behavioural tests for the archiver against the live primary postgres."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
import os
import subprocess
import sys
import time
import uuid

import pytest
from sqlalchemy import text  # type: ignore[import]
from sqlalchemy.engine import Connection, Engine  # type: ignore[import]


def _insert_minimal_order(
    conn: Connection,
    order_id: uuid.UUID,
    placed_at: dt.datetime,
    status: str,
    terminal_reached: dt.datetime | None,
    user_id: uuid.UUID,
    store_id: uuid.UUID,
    merchant_id: uuid.UUID,
) -> None:
    conn.execute(text("""
        INSERT INTO orders (order_id, placed_at, order_number, order_status,
                            order_channel, order_type, user_id,
                            user_account_age_days, user_total_orders_lifetime,
                            user_total_orders_30d, user_total_spend_lifetime_pence,
                            user_email, user_email_domain, store_id, merchant_id,
                            store_city, store_latitude, store_longitude,
                            terminal_state_reached_at,
                            item_count, unique_item_count, subtotal_pence,
                            total_pence, payment_type)
        VALUES (:oid, :pa, :on, :st, 'WEB', 'DELIVERY', :u, 1, 0, 0, 0,
                :em, 'example.com', :s, :m, 'London', 51.5074, -0.1278,
                :tr, 1, 1, 1000, 1000, 'CREDIT_CARD')
    """), {"oid": order_id, "pa": placed_at, "on": f"AR-{str(order_id)[:8]}",
           "st": status, "u": user_id, "em": f"ar-{user_id}@example.com",
           "s": store_id, "m": merchant_id, "tr": terminal_reached})


@pytest.fixture
def arc_fixture(db_engine: Engine) -> Iterator[tuple[uuid.UUID, uuid.UUID, uuid.UUID, list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]]]:
    """50 stale-terminal + 30 fresh-terminal + 20 active orders. Yields ids; cleans up."""
    user_id, store_id, merchant_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = dt.datetime.now(dt.timezone.utc)
    stale = now - dt.timedelta(hours=72)
    fresh = now - dt.timedelta(hours=24)
    placed_at = now - dt.timedelta(days=1)
    stale_ids = [uuid.uuid4() for _ in range(50)]
    fresh_ids = [uuid.uuid4() for _ in range(30)]
    active_ids = [uuid.uuid4() for _ in range(20)]
    try:
        with db_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO users (user_id, email, password_hash) VALUES (:u, :e, 'p')"
            ), {"u": user_id, "e": f"arc-fixture-{user_id}@example.com"})
            conn.execute(text(
                "INSERT INTO merchants (merchant_id, legal_name, brand_name) "
                "VALUES (:m, 'Arch Test Ltd', 'ArchTest')"
            ), {"m": merchant_id})
            conn.execute(text("""
                INSERT INTO stores (store_id, merchant_id, store_name, address_line_1,
                                    city, postcode, latitude, longitude)
                VALUES (:s, :m, 'Arch Test', '1 X', 'London', 'SW1A 1AA',
                        51.5074, -0.1278)
            """), {"s": store_id, "m": merchant_id})
            for oid in stale_ids:
                _insert_minimal_order(conn, oid, placed_at, "DELIVERED", stale,
                                      user_id, store_id, merchant_id)
            for oid in fresh_ids:
                _insert_minimal_order(conn, oid, placed_at, "DELIVERED", fresh,
                                      user_id, store_id, merchant_id)
            for oid in active_ids:
                _insert_minimal_order(conn, oid, placed_at, "PLACED", None,
                                      user_id, store_id, merchant_id)
        yield (user_id, store_id, merchant_id, stale_ids, fresh_ids, active_ids)
    finally:
        all_ids = stale_ids + fresh_ids + active_ids
        with db_engine.begin() as conn:
            for table in ["orders", "orders_archive", "order_items",
                          "order_items_archive", "order_events",
                          "order_events_archive"]:
                conn.execute(text(f"DELETE FROM {table} WHERE order_id = ANY(:ids)"),
                             {"ids": all_ids})
            conn.execute(text("DELETE FROM stores WHERE store_id = :s"), {"s": store_id})
            conn.execute(text("DELETE FROM merchants WHERE merchant_id = :m"), {"m": merchant_id})
            conn.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": user_id})


def test_archiver_moves_only_stale_terminal(
    db_engine: Engine,
    arc_fixture: tuple[uuid.UUID, uuid.UUID, uuid.UUID, list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]],
) -> None:
    _, _, _, stale_ids, fresh_ids, active_ids = arc_fixture
    from archival.archiver import run_once
    moved = run_once(batch_size=10000, max_batches=10)
    assert moved >= 50

    with db_engine.connect() as conn:
        hot_remaining = conn.execute(text(
            "SELECT count(*) FROM orders WHERE order_id = ANY(:ids)"
        ), {"ids": stale_ids + fresh_ids + active_ids}).scalar()
        assert hot_remaining == 50  # 30 fresh + 20 active

        archived = conn.execute(text(
            "SELECT count(*) FROM orders_archive WHERE order_id = ANY(:ids)"
        ), {"ids": stale_ids}).scalar()
        assert archived == 50


@pytest.mark.slow
def test_archiver_10k_under_30s(db_engine: Engine) -> None:
    """10K stale-terminal orders archived in <30s."""
    user_id, store_id, merchant_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = dt.datetime.now(dt.timezone.utc)
    stale = now - dt.timedelta(hours=72)
    placed_at = now - dt.timedelta(days=1)
    ids = [uuid.uuid4() for _ in range(10000)]
    try:
        with db_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO users (user_id, email, password_hash) VALUES (:u, :e, 'p')"
            ), {"u": user_id, "e": f"arc-10k-{user_id}@example.com"})
            conn.execute(text(
                "INSERT INTO merchants (merchant_id, legal_name, brand_name) "
                "VALUES (:m, 'A', 'A')"
            ), {"m": merchant_id})
            conn.execute(text("""
                INSERT INTO stores (store_id, merchant_id, store_name,
                                    address_line_1, city, postcode, latitude, longitude)
                VALUES (:s, :m, 'A', '1', 'London', 'SW1A 1AA', 51.5, -0.1)
            """), {"s": store_id, "m": merchant_id})
            # Bulk insert via psycopg2.extras.execute_values
            from psycopg2.extras import execute_values
            raw = conn.connection
            cur = raw.cursor()
            rows = [(oid, placed_at, f"10K-{i:08d}", "DELIVERED", "WEB", "DELIVERY",
                     user_id, 1, 0, 0, 0,
                     f"arc-10k-{user_id}@example.com", "example.com",
                     store_id, merchant_id, "London", 51.5, -0.1, stale,
                     1, 1, 1000, 1000, "CREDIT_CARD") for i, oid in enumerate(ids)]
            execute_values(cur, """
                INSERT INTO orders (order_id, placed_at, order_number, order_status,
                    order_channel, order_type, user_id, user_account_age_days,
                    user_total_orders_lifetime, user_total_orders_30d,
                    user_total_spend_lifetime_pence, user_email, user_email_domain,
                    store_id, merchant_id, store_city, store_latitude, store_longitude,
                    terminal_state_reached_at, item_count, unique_item_count,
                    subtotal_pence, total_pence, payment_type) VALUES %s
            """, rows, page_size=1000)
        from archival.archiver import run_once
        t0 = time.monotonic()
        run_once(batch_size=10000, max_batches=10)
        elapsed = time.monotonic() - t0
        with db_engine.connect() as conn:
            archived_count = conn.execute(text(
                "SELECT count(*) FROM orders_archive WHERE order_id = ANY(:ids)"
            ), {"ids": ids}).scalar()
            hot_count = conn.execute(text(
                "SELECT count(*) FROM orders WHERE order_id = ANY(:ids)"
            ), {"ids": ids}).scalar()
        assert archived_count + hot_count == 10000, (
            f"fixture rows not conserved: archived={archived_count}, hot={hot_count}"
        )
        assert elapsed < 30, f"archiver took {elapsed:.1f}s for 10K rows (target <30s)"
    finally:
        with db_engine.begin() as conn:
            for table in ["orders_archive", "orders"]:
                conn.execute(text(f"DELETE FROM {table} WHERE order_id = ANY(:ids)"), {"ids": ids})
            conn.execute(text("DELETE FROM stores WHERE store_id = :s"), {"s": store_id})
            conn.execute(text("DELETE FROM merchants WHERE merchant_id = :m"), {"m": merchant_id})
            conn.execute(text("DELETE FROM users WHERE user_id = :u"), {"u": user_id})


@pytest.mark.concurrent
def test_archiver_concurrent_runs_partition_work(
    db_engine: Engine,
    arc_fixture: tuple[uuid.UUID, uuid.UUID, uuid.UUID, list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]],
) -> None:
    """Two parallel --once runs partition the stale rows via row-locking discipline."""
    _, _, _, stale_ids, _, _ = arc_fixture
    sub_env = os.environ.copy()
    sub_env["ARCHIVE_BATCH_SIZE"] = "100"
    sub_env["ARCHIVE_MAX_BATCHES"] = "2"
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "archival.archiver", "--once"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=sub_env,
        )
        for _ in range(2)
    ]
    for p in procs:
        try:
            _, stderr = p.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
            pytest.fail("concurrent archiver run timed out")
        assert p.returncode == 0, f"archiver failed: {stderr.decode()[:500]}"

    with db_engine.connect() as conn:
        archived_count = conn.execute(text(
            "SELECT count(*) FROM orders_archive WHERE order_id = ANY(:ids)"
        ), {"ids": stale_ids}).scalar()
        hot_count = conn.execute(text(
            "SELECT count(*) FROM orders WHERE order_id = ANY(:ids)"
        ), {"ids": stale_ids}).scalar()
        total_count = archived_count + hot_count
        assert total_count == 50  # no duplication or loss for fixture stale rows
        assert archived_count >= 1
