from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional, cast

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import]
from psycopg2.extensions import connection as PsycopgConnection
from psycopg2.extensions import cursor as PsycopgCursor
from pythonjsonlogger import jsonlogger

from shared.db import get_engine

LOG = logging.getLogger("archiver")


def configure_logging() -> None:
    """Configure structured JSON logging for the archiver."""
    if not LOG.handlers:
        handler = logging.StreamHandler()
        formatter = jsonlogger.JsonFormatter(  # type: ignore[no-untyped-call]
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "ts", "levelname": "level"},
        )
        handler.setFormatter(formatter)
        LOG.addHandler(handler)

    LOG.setLevel(logging.INFO)
    LOG.propagate = False


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable with a fallback default."""
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


@contextmanager
def _raw_connection() -> Iterator[PsycopgConnection]:
    """Yield a raw psycopg2 connection from SQLAlchemy."""
    engine = get_engine("app")
    conn = cast(PsycopgConnection, engine.raw_connection())
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_ARCHIVE_SQL = """
WITH terminal_orders AS (
  SELECT order_id, placed_at FROM orders
  WHERE order_status IN ('DELIVERED','CANCELLED','REFUNDED','FAILED')
    AND terminal_state_reached_at < NOW() - INTERVAL '48 hours'
  LIMIT %(batch_size)s FOR UPDATE SKIP LOCKED
),
archived_events AS (
  DELETE FROM order_events e
  USING terminal_orders t
  WHERE e.order_id = t.order_id AND e.order_placed_at = t.placed_at
  RETURNING e.*
),
ins_events AS (
  INSERT INTO order_events_archive SELECT * FROM archived_events
  RETURNING 1
),
archived_items AS (
  DELETE FROM order_items i
  USING terminal_orders t
  WHERE i.order_id = t.order_id AND i.order_placed_at = t.placed_at
  RETURNING i.*
),
ins_items AS (
  INSERT INTO order_items_archive SELECT * FROM archived_items
  RETURNING 1
),
archived_orders AS (
  DELETE FROM orders o
  USING terminal_orders t
  WHERE o.order_id = t.order_id AND o.placed_at = t.placed_at
  RETURNING o.*
),
ins_orders AS (
  INSERT INTO orders_archive SELECT * FROM archived_orders
  RETURNING 1
)
SELECT
  (SELECT count(*) FROM ins_events),
  (SELECT count(*) FROM ins_items),
  (SELECT count(*) FROM ins_orders);
"""


def _archive_one_batch(batch_size: int, batch_num: int) -> int:
    """Move one batch of terminal orders from hot tables into archive tables."""
    start_ms = time.perf_counter()
    with _raw_connection() as conn:
        cur: PsycopgCursor = conn.cursor()
        try:
            cur.execute(_ARCHIVE_SQL, {"batch_size": batch_size})
            row = cur.fetchone()
        finally:
            cur.close()
        conn.commit()

    if row is None:
        moved = 0
    else:
        moved = int(row[2])

    LOG.info(
        "Archive batch complete",
        extra={
            "event": "archive_batch",
            "batch_num": batch_num,
            "moved": moved,
            "duration_ms": int((time.perf_counter() - start_ms) * 1000),
        },
    )
    return moved


def run_once(batch_size: int, max_batches: int) -> int:
    """Run archival passes until no rows are moved or max batches is reached."""
    batches = 0
    moved_total = 0

    for batch_num in range(1, max_batches + 1):
        moved = _archive_one_batch(batch_size=batch_size, batch_num=batch_num)
        batches = batch_num
        moved_total += moved
        if moved == 0:
            break

    LOG.info(
        "Archive run complete",
        extra={
            "event": "archive_run_complete",
            "batches": batches,
            "moved_total": moved_total,
        },
    )
    return moved_total


def _parse_stop_signal(signum: int, frame: Optional[object]) -> None:
    """Signal handler that flags daemon shutdown."""
    _ = signum
    _ = frame
    LOG.info(
        "Received SIGTERM, stopping",
        extra={"event": "archiver_shutdown"},
    )
    _STOP_EVENT.set()


_STOP_EVENT = threading.Event()


def main() -> int:
    """Entrypoint for the archiver process."""
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run a single archival cycle and exit.")
    args = parser.parse_args()

    batch_size = _env_int("ARCHIVE_BATCH_SIZE", 10000)
    max_batches = _env_int("ARCHIVE_MAX_BATCHES", 500)

    if args.once:
        run_once(batch_size=batch_size, max_batches=max_batches)
        return 0

    schedule_hour = _env_int("ARCHIVE_SCHEDULE_HOUR", 3)
    schedule_tz = os.getenv("ARCHIVE_SCHEDULE_TZ", "Europe/London")
    schedule = f"{schedule_hour:02d}:00 {schedule_tz}"

    LOG.info(
        "Archiver started",
        extra={
            "event": "archiver_started",
            "schedule": schedule,
            "batch_size": batch_size,
            "max_batches": max_batches,
        },
    )

    signal.signal(signal.SIGTERM, _parse_stop_signal)
    scheduler = BackgroundScheduler(timezone=schedule_tz)
    scheduler.add_job(
        run_once,
        trigger=CronTrigger(hour=schedule_hour, timezone=schedule_tz),
        args=[batch_size, max_batches],
    )
    scheduler.start()
    try:
        _STOP_EVENT.wait()
    finally:
        scheduler.shutdown(wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
