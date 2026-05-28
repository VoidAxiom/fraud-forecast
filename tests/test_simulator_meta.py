from __future__ import annotations

import asyncio
import json
import os

import asyncpg

from simulator.generator import write_simulator_epoch


DATABASE_URL_SIMULATOR = os.environ.get(
    "DATABASE_URL_SIMULATOR",
    "postgresql://simulator_user:simulator_dev_password@postgres:5432/fraud_platform",
)


async def _cleanup(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM sim.simulator_meta WHERE key LIKE 'simulator_epoch_%'")


async def _write_epoch(pool: asyncpg.Pool) -> str:
    async with pool.acquire() as conn:
        return await write_simulator_epoch(conn)


def test_epoch_increments() -> None:
    async def _run() -> None:
        conn = await asyncpg.connect(DATABASE_URL_SIMULATOR)
        try:
            await _cleanup(conn)

            key1 = await write_simulator_epoch(conn)
            key2 = await write_simulator_epoch(conn)

            row1 = await conn.fetchrow(
                "SELECT value FROM sim.simulator_meta WHERE key = $1",
                key1,
            )
            row2 = await conn.fetchrow(
                "SELECT value FROM sim.simulator_meta WHERE key = $1",
                key2,
            )

            assert row1 is not None
            assert row2 is not None
            assert json.loads(row1["value"])["epoch_num"] == 1
            assert json.loads(row2["value"])["epoch_num"] == 2
        finally:
            await _cleanup(conn)
            await conn.close()

    asyncio.run(_run())


def test_epoch_value_shape() -> None:
    async def _run() -> None:
        conn = await asyncpg.connect(DATABASE_URL_SIMULATOR)
        try:
            await _cleanup(conn)

            key = await write_simulator_epoch(conn)
            row = await conn.fetchrow(
                "SELECT value FROM sim.simulator_meta WHERE key = $1",
                key,
            )

            assert row is not None
            value = json.loads(row["value"])
            assert "started_at" in value
            assert "git_commit" in value
            assert "rng_seed" in value
            assert "epoch_num" in value
        finally:
            await _cleanup(conn)
            await conn.close()

    asyncio.run(_run())


def test_concurrent_no_pk_collision() -> None:
    async def _run() -> None:
        pool = await asyncpg.create_pool(DATABASE_URL_SIMULATOR, min_size=5, max_size=10)
        try:
            await pool.execute("DELETE FROM sim.simulator_meta WHERE key LIKE 'simulator_epoch_%'")

            await asyncio.gather(*(_write_epoch(pool) for _ in range(5)))
            count = await pool.fetchval(
                "SELECT count(*) FROM sim.simulator_meta WHERE key LIKE 'simulator_epoch_%'",
            )

            assert count == 5
        finally:
            await pool.execute("DELETE FROM sim.simulator_meta WHERE key LIKE 'simulator_epoch_%'")
            await pool.close()

    asyncio.run(_run())
