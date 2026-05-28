from __future__ import annotations

import asyncio
import math
import os
import random
import sys
import unittest.mock
import uuid
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from typing import Any
from unittest.mock import AsyncMock

import asyncpg

import pytest

if sys.version_info >= (3, 9):
    from zoneinfo import ZoneInfo
else:
    from backports.zoneinfo import ZoneInfo
import shared.db

from sqlalchemy import create_engine as _create_engine, exc as sqlalchemy_exc, text
from sqlalchemy.engine import Connection, Engine  # type: ignore[import]

from simulator.fraud_patterns import GroundTruth, generate_fraud_order
from simulator.fraud_patterns.account_takeover import generate_account_takeover_fraud
from simulator.fraud_patterns.collusive_merchant import (
    COLLUSIVE_STORES,
    generate_collusive_merchant_fraud,
    init_collusive_stores,
)
from simulator.fraud_patterns.promo_abuse import (
    generate_promo_abuse_fraud,
    init_rings,
    PROMO_ABUSE_RINGS,
)
import simulator.chargebacks
from simulator.fraud_patterns.refund_abuse import generate_refund_abuse_fraud
from simulator.fraud_patterns.reseller import (
    generate_reseller_fraud,
    init_reseller_accounts,
)
from simulator.fraud_patterns.stolen_card import FraudPatternContext, generate_stolen_card_fraud
from simulator.fraud_patterns.triangulation import (
    generate_triangulation_fraud,
    init_accounts,
)


class _FakeConn:
    def __init__(self) -> None:
        self._rows: list[dict[str, uuid.UUID]] = []

    async def fetch(self, query: str) -> list[dict[str, uuid.UUID]]:
        _ = query
        return self._rows

    async def executemany(self, query: str, args: list[tuple[uuid.UUID, ...]]) -> None:
        _ = query
        existing = {row["store_id"] for row in self._rows}
        for (store_id,) in args:
            normalized_store_id = (
                store_id if isinstance(store_id, uuid.UUID) else uuid.UUID(str(store_id))
            )
            if normalized_store_id not in existing:
                self._rows.append({"store_id": normalized_store_id})
                existing.add(normalized_store_id)


_SCORING_URL: str = "postgresql://scoring_user:scoring_dev_password@postgres:5432/fraud_platform"

LONDON_TZ_TEST: ZoneInfo = ZoneInfo("Europe/London")

_ORDER_INSERT_SQL = text(
    """
    INSERT INTO orders (
        order_id, order_number, order_status, order_channel, order_type,
        placed_at, user_id,
        user_account_age_days, user_total_orders_lifetime, user_total_orders_30d,
        user_total_spend_lifetime_pence, user_email, user_email_domain,
        store_id, merchant_id, store_city, store_country,
        store_latitude, store_longitude,
        item_count, unique_item_count,
        subtotal_pence, vat_pence, delivery_fee_pence,
        service_fee_pence, tip_pence, discount_pence,
        total_pence, payment_type
    ) VALUES (
        :order_id, :order_number, 'DELIVERED', 'WEB', 'DELIVERY',
        :placed_at, :user_id,
        30, 5, 2,
        10000, :user_email, 'test.example.com',
        :store_id, :merchant_id, 'London', 'GB',
        51.5074, -0.1278,
        1, 1,
        1000, 0, 0,
        0, 0, 0,
        :total_pence, 'CREDIT_CARD'
    )
    """
)

_GROUND_TRUTH_INSERT_SQL = text(
    """
    INSERT INTO sim.simulator_ground_truth (
        order_id,
        is_fraud,
        fraud_category,
        pattern_notes,
        ring_id
    ) VALUES (
        :order_id,
        :is_fraud,
        :fraud_category,
        :pattern_notes,
        :ring_id
    )
    """
)


def _ctx(seed: int | float) -> FraudPatternContext:
    return FraudPatternContext(
        now=datetime.now(tz=LONDON_TZ_TEST),
        rng=random.Random(seed),
    )


def _init_triangulation_accounts_for_test(rng: random.Random, n: int = 30) -> None:
    conn = AsyncMock()
    conn.fetchval.return_value = 0
    conn.execute.return_value = "DELETE 0"
    conn.executemany.return_value = None
    asyncio.run(init_accounts(rng, conn, n=n))


def _mock_reseller_conn() -> unittest.mock.AsyncMock:
    """Stateful mock connection for empty DB reseller account initialization."""
    conn = unittest.mock.AsyncMock()
    rows: list[dict[str, Any]] = []

    async def _fetch(_query: str) -> list[dict[str, Any]]:
        return list(rows)

    async def _executemany(
        _query: str,
        values: list[Any],
    ) -> None:
        import json as _json

        for v in values:
            (
                account_id,
                reseller_address_json,
                delivery_address_uuid,
                device_uuid,
                preferred_store_ids,
            ) = v
            rows.append(
                {
                    "account_id": account_id,
                    "reseller_address": _json.loads(reseller_address_json)
                    if isinstance(reseller_address_json, str)
                    else reseller_address_json,
                    "delivery_address_uuid": delivery_address_uuid,
                    "device_uuid": device_uuid,
                    "preferred_store_ids": list(preferred_store_ids),
                }
            )

    conn.fetch.side_effect = _fetch
    conn.executemany.side_effect = _executemany
    conn.execute.return_value = None
    txn_mock = unittest.mock.AsyncMock()
    txn_mock.__aenter__ = unittest.mock.AsyncMock(return_value=txn_mock)
    txn_mock.__aexit__ = unittest.mock.AsyncMock(return_value=None)
    conn.transaction = unittest.mock.Mock()
    conn.transaction.return_value = txn_mock
    return conn


def _assert_uuid(value: object, field_name: str) -> uuid.UUID:
    assert isinstance(value, uuid.UUID), f"{field_name} must be a UUID"
    return value


def _safe_int(value: object, field_name: str) -> int:
    assert isinstance(value, int), f"{field_name} must be int, got {type(value)}"
    return value


def _safe_str(value: object, field_name: str) -> str:
    assert isinstance(value, str), f"{field_name} must be str, got {type(value)}"
    return value


def _extract_user_id(order_dict: dict[str, object]) -> uuid.UUID:
    user_id = order_dict.get("user_id")
    if isinstance(user_id, uuid.UUID):
        return user_id
    victim_user_id = order_dict.get("victim_user_id")
    if isinstance(victim_user_id, uuid.UUID):
        return victim_user_id
    return uuid.uuid4()


def _insert_minimal_order_row(
    conn: Connection,
    *,
    order_id: uuid.UUID,
    placed_at: datetime,
    user_id: uuid.UUID,
    store_id: uuid.UUID,
    merchant_id: uuid.UUID,
    order_number: str,
    total_pence: int,
) -> None:
    conn.execute(
        _ORDER_INSERT_SQL,
        {
            "order_id": order_id,
            "order_number": order_number,
            "placed_at": placed_at,
            "user_id": user_id,
            "user_email": f"gt-{order_id.hex}@test.example.com",
            "store_id": store_id,
            "merchant_id": merchant_id,
            "total_pence": total_pence,
        },
    )


def test_stolen_card_signals_present() -> None:
    ctx = _ctx(seed=0)
    avs_match_count: int = 0
    non_gb_count: int = 0
    order_totals: list[int] = []

    for _ in range(100):
        order_dict, _gt = asyncio.run(generate_stolen_card_fraud(ctx))
        avs_result = _safe_str(order_dict["avs_result"], "avs_result")
        card_country = _safe_str(order_dict["card_country"], "card_country")
        order_total = _safe_int(order_dict["order_total_pence"], "order_total_pence")

        if avs_result == "NO_MATCH":
            avs_match_count += 1
        if card_country != "GB":
            non_gb_count += 1
        order_totals.append(order_total)

    assert avs_match_count / 100 >= 0.60
    assert non_gb_count / 100 >= 0.70
    assert sum(order_totals) / len(order_totals) >= 5000


def test_account_takeover_signals_present() -> None:
    ctx = _ctx(seed=0)
    non_gb_count: int = 0
    normal_totals: list[int] = []
    high_totals: list[int] = []

    for _ in range(100):
        order_dict, _gt = asyncio.run(generate_account_takeover_fraud(ctx))
        device_id = order_dict["device_id"]
        assert isinstance(device_id, uuid.UUID)
        assert device_id is not None

        ip_country = _safe_str(order_dict["ip_country"], "ip_country")
        if ip_country != "GB":
            non_gb_count += 1

        order_value_mode = _safe_str(order_dict["order_value_mode"], "order_value_mode")
        order_total = _safe_int(order_dict["order_total_pence"], "order_total_pence")
        if order_value_mode == "normal":
            normal_totals.append(order_total)
        elif order_value_mode == "high_value":
            high_totals.append(order_total)
        else:
            raise AssertionError(f"unexpected order_value_mode: {order_value_mode}")

    assert non_gb_count / 100 >= 0.70
    assert normal_totals and high_totals
    normal_mean = sum(normal_totals) / len(normal_totals)
    high_mean = sum(high_totals) / len(high_totals)
    assert normal_mean < 5000
    assert high_mean > 5000
    assert high_mean > normal_mean * 2


def test_promo_abuse_signals_present() -> None:
    init_rings(rng=random.Random(7), n_rings=50)
    ctx = _ctx(seed=0)

    for _ in range(100):
        order_dict, _gt = asyncio.run(generate_promo_abuse_fraud(ctx))
        promo = _safe_str(order_dict["promo"], "promo")
        order_total = _safe_int(order_dict["order_total_pence"], "order_total_pence")

        assert promo == "WELCOME10"
        assert 2000 <= order_total <= 2500


def test_refund_abuse_signals_present() -> None:
    ctx = _ctx(seed=0)

    for _ in range(100):
        order_dict, gt = asyncio.run(generate_refund_abuse_fraud(ctx))
        is_new_device = order_dict["is_new_device"]
        assert is_new_device is False
        assert gt.pattern_notes is not None
        assert "_refund_abuser_filter=refunds_lifetime__gte_3" in gt.pattern_notes
        avs_result = _safe_str(order_dict["avs_result"], "avs_result")

        assert avs_result == "MATCH"


def test_collusive_merchant_signals_present() -> None:
    asyncio.run(
        init_collusive_stores(
            random.Random(7),
            _FakeConn(),
            [uuid.UUID(int=i + 1) for i in range(20)],
            n=10,
        )
    )
    ctx = _ctx(seed=0)
    avs_match_count: int = 0
    cvv_match_count: int = 0

    for _ in range(100):
        order_dict, _gt = asyncio.run(generate_collusive_merchant_fraud(ctx))
        store_id = _assert_uuid(order_dict["store_id"], "store_id")
        assert store_id in COLLUSIVE_STORES

        avs_result = _safe_str(order_dict["avs_result"], "avs_result")
        cvv_result = _safe_str(order_dict["cvv_result"], "cvv_result")

        if avs_result == "MATCH":
            avs_match_count += 1
        if cvv_result == "MATCH":
            cvv_match_count += 1

    assert avs_match_count / 100 >= 0.75
    assert cvv_match_count / 100 >= 0.75


def test_triangulation_signals_present() -> None:
    _init_triangulation_accounts_for_test(rng=random.Random(7), n=30)
    ctx = _ctx(seed=0)
    delivery_address_ids: set[uuid.UUID] = set()

    for _ in range(100):
        order_dict, _gt = asyncio.run(generate_triangulation_fraud(ctx))
        is_new_device = order_dict["is_new_device"]
        assert is_new_device is False
        delivery_address_id = _assert_uuid(
            order_dict["delivery_address_id"],
            "delivery_address_id",
        )
        delivery_address_ids.add(delivery_address_id)

    assert len(delivery_address_ids) / 100 >= 0.80


def test_reseller_signals_present() -> None:
    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(7),
            conn=_mock_reseller_conn(),
            store_id_pool=[uuid.UUID(int=i + 1) for i in range(5)],
            n=50,
        )
    )
    ctx = _ctx(seed=0)
    delivery_address_ids: set[uuid.UUID] = set()

    for _ in range(100):
        order_dict, _gt = asyncio.run(generate_reseller_fraud(ctx))
        item_count = _safe_int(order_dict["item_count"], "item_count")
        assert 10 <= item_count <= 25
        delivery_address_id = _assert_uuid(order_dict["delivery_address_id"], "delivery_address_id")
        delivery_address_ids.add(delivery_address_id)

    assert len(delivery_address_ids) <= 50


def test_stolen_card_ground_truth_recorded(db_engine: Engine) -> None:
    rows: list[uuid.UUID] = []
    min_placed_at: datetime | None = None
    expected = "stolen_card"

    def _make_rows() -> list[tuple[uuid.UUID, datetime, GroundTruth, uuid.UUID, int]]:
        generated: list[tuple[uuid.UUID, datetime, GroundTruth, uuid.UUID, int]] = []
        rng = random.Random(11)
        for _ in range(10):
            ctx = _ctx(seed=rng.randint(0, 999_999))
            order_dict, gt = asyncio.run(generate_stolen_card_fraud(ctx))
            order_id = _assert_uuid(order_dict["order_id"], "order_id")
            placed_at = _safe_datetime(order_dict.get("placed_at", ctx.now))
            total_pence = _safe_int(order_dict.get("order_total_pence", 1000), "order_total_pence")
            user_id = _extract_user_id(order_dict)
            generated.append((order_id, placed_at, gt, user_id, total_pence))
        return generated

    generated_rows = _make_rows()
    try:
        for order_id, placed_at, _gt, user_id, total_pence in generated_rows:
            rows.append(order_id)
            min_placed_at = placed_at if min_placed_at is None else min(min_placed_at, placed_at)
            with db_engine.begin() as conn:
                _insert_minimal_order_row(
                    conn,
                    order_id=order_id,
                    placed_at=placed_at,
                    user_id=user_id,
                    store_id=uuid.uuid4(),
                    merchant_id=uuid.uuid4(),
                    order_number=f"STO-{order_id.hex[:12]}",
                    total_pence=total_pence,
                )
                conn.execute(
                    _GROUND_TRUTH_INSERT_SQL,
                    {
                        "order_id": order_id,
                        "is_fraud": _gt.is_fraud,
                        "fraud_category": _gt.fraud_category,
                        "pattern_notes": _gt.pattern_notes,
                        "ring_id": _gt.ring_id,
                    },
                )

        with db_engine.connect() as conn:
            found = {
                row[0]: row[1]
                for row in conn.execute(
                    text(
                        "SELECT order_id, fraud_category FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"
                    ),
                    {"ids": rows},
                ).fetchall()
            }

        assert len(found) == len(rows)
        for order_id in rows:
            assert found[order_id] == expected
    finally:
        with db_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"),
                {"ids": rows},
            )
            if min_placed_at is not None:
                conn.execute(
                    text(
                        "DELETE FROM orders WHERE order_id = ANY(:ids) AND placed_at >= :min_placed_at"
                    ),
                    {"ids": rows, "min_placed_at": min_placed_at},
                )


def _safe_datetime(value: object) -> datetime:
    assert isinstance(value, datetime), f"placed_at must be datetime, got {type(value)}"
    return value


def _percentile(values: list[float], quantile: float) -> float:
    assert values
    assert 0.0 <= quantile <= 1.0
    ordered = sorted(values)
    index = int(quantile * (len(ordered) - 1))
    return ordered[index]


def test_account_takeover_ground_truth_recorded(db_engine: Engine) -> None:
    rows: list[uuid.UUID] = []
    min_placed_at: datetime | None = None
    expected = "account_takeover"
    rng = random.Random(11)

    generated: list[tuple[uuid.UUID, datetime, GroundTruth, uuid.UUID, int]] = []
    for _ in range(10):
        ctx = _ctx(seed=rng.randint(0, 999_999))
        order_dict, gt = asyncio.run(generate_account_takeover_fraud(ctx))
        order_id = _assert_uuid(order_dict["order_id"], "order_id")
        placed_at = _safe_datetime(order_dict.get("placed_at", ctx.now))
        total_pence = _safe_int(order_dict.get("order_total_pence", 1000), "order_total_pence")
        user_id = _extract_user_id(order_dict)
        generated.append((order_id, placed_at, gt, user_id, total_pence))

    try:
        for order_id, placed_at, gt, user_id, total_pence in generated:
            rows.append(order_id)
            min_placed_at = placed_at if min_placed_at is None else min(min_placed_at, placed_at)
            with db_engine.begin() as conn:
                _insert_minimal_order_row(
                    conn,
                    order_id=order_id,
                    placed_at=placed_at,
                    user_id=user_id,
                    store_id=uuid.uuid4(),
                    merchant_id=uuid.uuid4(),
                    order_number=f"ATO-{order_id.hex[:12]}",
                    total_pence=total_pence,
                )
                conn.execute(
                    _GROUND_TRUTH_INSERT_SQL,
                    {
                        "order_id": order_id,
                        "is_fraud": gt.is_fraud,
                        "fraud_category": gt.fraud_category,
                        "pattern_notes": gt.pattern_notes,
                        "ring_id": gt.ring_id,
                    },
                )

        with db_engine.connect() as conn:
            found = {
                row[0]: row[1]
                for row in conn.execute(
                    text(
                        "SELECT order_id, fraud_category FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"
                    ),
                    {"ids": rows},
                ).fetchall()
            }

        assert len(found) == len(rows)
        for order_id in rows:
            assert found[order_id] == expected
    finally:
        with db_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"),
                {"ids": rows},
            )
            if min_placed_at is not None:
                conn.execute(
                    text(
                        "DELETE FROM orders WHERE order_id = ANY(:ids) AND placed_at >= :min_placed_at"
                    ),
                    {"ids": rows, "min_placed_at": min_placed_at},
                )


def test_promo_abuse_ground_truth_recorded(db_engine: Engine) -> None:
    init_rings(rng=random.Random(7), n_rings=50)
    rows: list[uuid.UUID] = []
    min_placed_at: datetime | None = None
    expected = "promo_abuse"
    rng = random.Random(11)
    generated: list[tuple[uuid.UUID, datetime, GroundTruth, uuid.UUID, int]] = []

    for _ in range(10):
        ctx = _ctx(seed=rng.randint(0, 999_999))
        order_dict, gt = asyncio.run(generate_promo_abuse_fraud(ctx))
        order_id = _assert_uuid(order_dict["order_id"], "order_id")
        placed_at = _safe_datetime(order_dict.get("placed_at", ctx.now))
        total_pence = _safe_int(order_dict.get("order_total_pence", 1000), "order_total_pence")
        user_id = _extract_user_id(order_dict)
        generated.append((order_id, placed_at, gt, user_id, total_pence))

    try:
        for order_id, placed_at, gt, user_id, total_pence in generated:
            rows.append(order_id)
            min_placed_at = placed_at if min_placed_at is None else min(min_placed_at, placed_at)
            with db_engine.begin() as conn:
                _insert_minimal_order_row(
                    conn,
                    order_id=order_id,
                    placed_at=placed_at,
                    user_id=user_id,
                    store_id=uuid.uuid4(),
                    merchant_id=uuid.uuid4(),
                    order_number=f"PRA-{order_id.hex[:12]}",
                    total_pence=total_pence,
                )
                conn.execute(
                    _GROUND_TRUTH_INSERT_SQL,
                    {
                        "order_id": order_id,
                        "is_fraud": gt.is_fraud,
                        "fraud_category": gt.fraud_category,
                        "pattern_notes": gt.pattern_notes,
                        "ring_id": gt.ring_id,
                    },
                )

        with db_engine.connect() as conn:
            found = {
                row[0]: row[1]
                for row in conn.execute(
                    text(
                        "SELECT order_id, fraud_category FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"
                    ),
                    {"ids": rows},
                ).fetchall()
            }

        assert len(found) == len(rows)
        for order_id in rows:
            assert found[order_id] == expected
    finally:
        with db_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"),
                {"ids": rows},
            )
            if min_placed_at is not None:
                conn.execute(
                    text(
                        "DELETE FROM orders WHERE order_id = ANY(:ids) AND placed_at >= :min_placed_at"
                    ),
                    {"ids": rows, "min_placed_at": min_placed_at},
                )


def test_refund_abuse_ground_truth_recorded(db_engine: Engine) -> None:
    rows: list[uuid.UUID] = []
    min_placed_at: datetime | None = None
    expected = "refund_abuse"
    rng = random.Random(11)

    generated: list[tuple[uuid.UUID, datetime, GroundTruth, uuid.UUID, int]] = []
    for _ in range(10):
        ctx = _ctx(seed=rng.randint(0, 999_999))
        order_dict, gt = asyncio.run(generate_refund_abuse_fraud(ctx))
        order_id = _assert_uuid(order_dict["order_id"], "order_id")
        placed_at = _safe_datetime(order_dict.get("placed_at", ctx.now))
        total_pence = _safe_int(order_dict.get("order_total_pence", 1000), "order_total_pence")
        user_id = _extract_user_id(order_dict)
        generated.append((order_id, placed_at, gt, user_id, total_pence))

    try:
        for order_id, placed_at, gt, user_id, total_pence in generated:
            rows.append(order_id)
            min_placed_at = placed_at if min_placed_at is None else min(min_placed_at, placed_at)
            with db_engine.begin() as conn:
                _insert_minimal_order_row(
                    conn,
                    order_id=order_id,
                    placed_at=placed_at,
                    user_id=user_id,
                    store_id=uuid.uuid4(),
                    merchant_id=uuid.uuid4(),
                    order_number=f"REF-{order_id.hex[:12]}",
                    total_pence=total_pence,
                )
                conn.execute(
                    _GROUND_TRUTH_INSERT_SQL,
                    {
                        "order_id": order_id,
                        "is_fraud": gt.is_fraud,
                        "fraud_category": gt.fraud_category,
                        "pattern_notes": gt.pattern_notes,
                        "ring_id": gt.ring_id,
                    },
                )

        with db_engine.connect() as conn:
            found = {
                row[0]: row[1]
                for row in conn.execute(
                    text(
                        "SELECT order_id, fraud_category FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"
                    ),
                    {"ids": rows},
                ).fetchall()
            }

        assert len(found) == len(rows)
        for order_id in rows:
            assert found[order_id] == expected
    finally:
        with db_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"),
                {"ids": rows},
            )
            if min_placed_at is not None:
                conn.execute(
                    text(
                        "DELETE FROM orders WHERE order_id = ANY(:ids) AND placed_at >= :min_placed_at"
                    ),
                    {"ids": rows, "min_placed_at": min_placed_at},
                )


def test_collusive_merchant_ground_truth_recorded(db_engine: Engine) -> None:
    asyncio.run(
        init_collusive_stores(
            random.Random(7),
            _FakeConn(),
            [uuid.UUID(int=i + 1) for i in range(20)],
            n=10,
        )
    )
    rows: list[uuid.UUID] = []
    min_placed_at: datetime | None = None
    expected = "collusive_merchant"
    rng = random.Random(11)

    generated: list[tuple[uuid.UUID, datetime, GroundTruth, uuid.UUID, int]] = []
    for _ in range(10):
        ctx = _ctx(seed=rng.randint(0, 999_999))
        order_dict, gt = asyncio.run(generate_collusive_merchant_fraud(ctx))
        order_id = _assert_uuid(order_dict["order_id"], "order_id")
        placed_at = _safe_datetime(order_dict.get("placed_at", ctx.now))
        total_pence = _safe_int(order_dict.get("order_total_pence", 1000), "order_total_pence")
        user_id = _extract_user_id(order_dict)
        generated.append((order_id, placed_at, gt, user_id, total_pence))

    try:
        for order_id, placed_at, gt, user_id, total_pence in generated:
            rows.append(order_id)
            min_placed_at = placed_at if min_placed_at is None else min(min_placed_at, placed_at)
            with db_engine.begin() as conn:
                _insert_minimal_order_row(
                    conn,
                    order_id=order_id,
                    placed_at=placed_at,
                    user_id=user_id,
                    store_id=uuid.uuid4(),
                    merchant_id=uuid.uuid4(),
                    order_number=f"COL-{order_id.hex[:12]}",
                    total_pence=total_pence,
                )
                conn.execute(
                    _GROUND_TRUTH_INSERT_SQL,
                    {
                        "order_id": order_id,
                        "is_fraud": gt.is_fraud,
                        "fraud_category": gt.fraud_category,
                        "pattern_notes": gt.pattern_notes,
                        "ring_id": gt.ring_id,
                    },
                )

        with db_engine.connect() as conn:
            found = {
                row[0]: row[1]
                for row in conn.execute(
                    text(
                        "SELECT order_id, fraud_category FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"
                    ),
                    {"ids": rows},
                ).fetchall()
            }

        assert len(found) == len(rows)
        for order_id in rows:
            assert found[order_id] == expected
    finally:
        with db_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"),
                {"ids": rows},
            )
            if min_placed_at is not None:
                conn.execute(
                    text(
                        "DELETE FROM orders WHERE order_id = ANY(:ids) AND placed_at >= :min_placed_at"
                    ),
                    {"ids": rows, "min_placed_at": min_placed_at},
                )


def test_triangulation_ground_truth_recorded(db_engine: Engine) -> None:
    _init_triangulation_accounts_for_test(rng=random.Random(7), n=30)
    rows: list[uuid.UUID] = []
    min_placed_at: datetime | None = None
    expected = "triangulation"
    rng = random.Random(11)

    generated: list[tuple[uuid.UUID, datetime, GroundTruth, uuid.UUID, int]] = []
    for _ in range(10):
        ctx = _ctx(seed=rng.randint(0, 999_999))
        order_dict, gt = asyncio.run(generate_triangulation_fraud(ctx))
        order_id = _assert_uuid(order_dict["order_id"], "order_id")
        placed_at = _safe_datetime(order_dict.get("placed_at", ctx.now))
        total_pence = _safe_int(order_dict.get("order_total_pence", 1000), "order_total_pence")
        user_id = _extract_user_id(order_dict)
        generated.append((order_id, placed_at, gt, user_id, total_pence))

    try:
        for order_id, placed_at, gt, user_id, total_pence in generated:
            rows.append(order_id)
            min_placed_at = placed_at if min_placed_at is None else min(min_placed_at, placed_at)
            with db_engine.begin() as conn:
                _insert_minimal_order_row(
                    conn,
                    order_id=order_id,
                    placed_at=placed_at,
                    user_id=user_id,
                    store_id=uuid.uuid4(),
                    merchant_id=uuid.uuid4(),
                    order_number=f"TRI-{order_id.hex[:12]}",
                    total_pence=total_pence,
                )
                conn.execute(
                    _GROUND_TRUTH_INSERT_SQL,
                    {
                        "order_id": order_id,
                        "is_fraud": gt.is_fraud,
                        "fraud_category": gt.fraud_category,
                        "pattern_notes": gt.pattern_notes,
                        "ring_id": gt.ring_id,
                    },
                )

        with db_engine.connect() as conn:
            found = {
                row[0]: row[1]
                for row in conn.execute(
                    text(
                        "SELECT order_id, fraud_category FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"
                    ),
                    {"ids": rows},
                ).fetchall()
            }

        assert len(found) == len(rows)
        for order_id in rows:
            assert found[order_id] == expected
    finally:
        with db_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"),
                {"ids": rows},
            )
            if min_placed_at is not None:
                conn.execute(
                    text(
                        "DELETE FROM orders WHERE order_id = ANY(:ids) AND placed_at >= :min_placed_at"
                    ),
                    {"ids": rows, "min_placed_at": min_placed_at},
                )


def test_reseller_ground_truth_recorded(db_engine: Engine) -> None:
    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(7),
            conn=_mock_reseller_conn(),
            store_id_pool=[uuid.UUID(int=i + 1) for i in range(5)],
            n=50,
        )
    )
    rows: list[uuid.UUID] = []
    min_placed_at: datetime | None = None
    expected = "reseller"
    rng = random.Random(11)

    generated: list[tuple[uuid.UUID, datetime, GroundTruth, uuid.UUID, int]] = []
    for _ in range(10):
        ctx = _ctx(seed=rng.randint(0, 999_999))
        order_dict, gt = asyncio.run(generate_reseller_fraud(ctx))
        order_id = _assert_uuid(order_dict["order_id"], "order_id")
        placed_at = _safe_datetime(order_dict.get("placed_at", ctx.now))
        total_pence = _safe_int(order_dict.get("order_total_pence", 1000), "order_total_pence")
        user_id = _extract_user_id(order_dict)
        generated.append((order_id, placed_at, gt, user_id, total_pence))

    try:
        for order_id, placed_at, gt, user_id, total_pence in generated:
            rows.append(order_id)
            min_placed_at = placed_at if min_placed_at is None else min(min_placed_at, placed_at)
            with db_engine.begin() as conn:
                _insert_minimal_order_row(
                    conn,
                    order_id=order_id,
                    placed_at=placed_at,
                    user_id=user_id,
                    store_id=uuid.uuid4(),
                    merchant_id=uuid.uuid4(),
                    order_number=f"RES-{order_id.hex[:12]}",
                    total_pence=total_pence,
                )
                conn.execute(
                    _GROUND_TRUTH_INSERT_SQL,
                    {
                        "order_id": order_id,
                        "is_fraud": gt.is_fraud,
                        "fraud_category": gt.fraud_category,
                        "pattern_notes": gt.pattern_notes,
                        "ring_id": gt.ring_id,
                    },
                )

        with db_engine.connect() as conn:
            found = {
                row[0]: row[1]
                for row in conn.execute(
                    text(
                        "SELECT order_id, fraud_category FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"
                    ),
                    {"ids": rows},
                ).fetchall()
            }

        assert len(found) == len(rows)
        for order_id in rows:
            assert found[order_id] == expected
    finally:
        with db_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"),
                {"ids": rows},
            )
            if min_placed_at is not None:
                conn.execute(
                    text(
                        "DELETE FROM orders WHERE order_id = ANY(:ids) AND placed_at >= :min_placed_at"
                    ),
                    {"ids": rows, "min_placed_at": min_placed_at},
                )


def test_fraud_distribution() -> None:
    init_rings(rng=random.Random(42), n_rings=50)
    asyncio.run(
        init_collusive_stores(
            random.Random(42),
            _FakeConn(),
            [uuid.UUID(int=i + 1) for i in range(20)],
            n=10,
        )
    )
    _init_triangulation_accounts_for_test(rng=random.Random(42), n=30)
    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(42),
            conn=_mock_reseller_conn(),
            store_id_pool=[uuid.UUID(int=i + 1) for i in range(5)],
            n=50,
        )
    )

    ctx: FraudPatternContext = _ctx(42)
    category_counts: dict[str, int] = {
        "stolen_card": 0,
        "account_takeover": 0,
        "promo_abuse": 0,
        "refund_abuse": 0,
        "collusive_merchant": 0,
        "triangulation": 0,
        "reseller": 0,
    }

    for _ in range(1000):
        _order_dict, gt = asyncio.run(generate_fraud_order(ctx))
        category = gt.fraud_category
        assert category is not None
        category_counts[category] += 1

    expected_ranges: dict[str, tuple[int, int]] = {
        "stolen_card": (270, 330),
        "account_takeover": (170, 230),
        "promo_abuse": (220, 280),
        "refund_abuse": (70, 130),
        "collusive_merchant": (20, 80),
        "triangulation": (20, 80),
        "reseller": (20, 80),
    }

    for category, (minimum, maximum) in expected_ranges.items():
        assert category_counts[category] > 0
        assert minimum <= category_counts[category] <= maximum


def test_promo_abuse_ring_consistency() -> None:
    init_rings(rng=random.Random(7), n_rings=50)
    ctx: FraudPatternContext = _ctx(7)

    orders_by_ring: dict[uuid.UUID, list[dict[str, object]]] = {}
    for _ in range(100):
        order_dict, gt = asyncio.run(generate_promo_abuse_fraud(ctx))
        ring_id = gt.ring_id
        assert ring_id is not None
        orders_by_ring.setdefault(ring_id, []).append(order_dict)

    ring_by_id = {ring.ring_id: ring for ring in PROMO_ABUSE_RINGS}
    rings_with_multiple_orders = 0

    for ring_id, orders in orders_by_ring.items():
        if len(orders) < 2:
            continue

        rings_with_multiple_orders += 1
        ring = ring_by_id[ring_id]
        ring_base_lat = ring.base_address["lat"]
        ring_base_lon = ring.base_address["lon"]
        assert isinstance(ring_base_lat, float)
        assert isinstance(ring_base_lon, float)

        ring_payment_signatures: set[tuple[str, str, str]] = {
            (payment["card_bin"], payment["last4"], payment["funding"])
            for payment in ring.payment_pool
        }

        device_ids: set[uuid.UUID] = set()
        for order in orders:
            device_ids.add(_assert_uuid(order["device_id"], "device_id"))

            payment = order["payment"]
            assert isinstance(payment, dict)
            card_bin = _safe_str(payment.get("card_bin"), "card_bin")
            last4 = _safe_str(payment.get("last4"), "last4")
            funding = _safe_str(payment.get("funding"), "funding")
            assert (card_bin, last4, funding) in ring_payment_signatures

            delivery_lat = order["delivery_lat"]
            assert isinstance(delivery_lat, float)
            delivery_lon = order["delivery_lon"]
            assert isinstance(delivery_lon, float)
            assert abs(delivery_lat - ring_base_lat) <= 0.0045
            assert abs(delivery_lon - ring_base_lon) <= 0.0045

        assert len(device_ids) == 1

    assert rings_with_multiple_orders > 0


def test_collusive_store_concentration() -> None:
    asyncio.run(
        init_collusive_stores(
            random.Random(7),
            _FakeConn(),
            [uuid.UUID(int=i + 1) for i in range(20)],
            n=10,
        )
    )
    init_rings(rng=random.Random(7), n_rings=50)
    _init_triangulation_accounts_for_test(rng=random.Random(7), n=30)
    asyncio.run(
        init_reseller_accounts(
            rng=random.Random(7),
            conn=_mock_reseller_conn(),
            store_id_pool=[uuid.UUID(int=i + 1) for i in range(5)],
            n=50,
        )
    )
    ctx: FraudPatternContext = _ctx(7)

    orders_by_store: dict[uuid.UUID, int] = {}
    fraud_orders_by_store: dict[uuid.UUID, int] = {}
    normal_order_count = 9000
    order_ids: list[uuid.UUID] = []
    fraud_order_ids: list[uuid.UUID] = []
    engine = shared.db.get_engine("app")

    try:
        with engine.begin() as conn:
            for _ in range(1000):
                order_dict, gt = asyncio.run(generate_fraud_order(ctx))
                store_id_raw = order_dict.get("store_id")
                if store_id_raw is None:
                    # Some patterns are not bound to a concrete top-level store_id.
                    # They are irrelevant for the collusive-vs-normal store concentration
                    # comparison, so skip them entirely here.
                    continue

                store_id = _assert_uuid(store_id_raw, "store_id")
                order_id = _assert_uuid(order_dict["order_id"], "order_id")
                user_id = _extract_user_id(order_dict)
                placed_at = _safe_datetime(
                    order_dict.get("placed_at", datetime.now(tz=LONDON_TZ_TEST))
                )
                total_pence = _safe_int(
                    order_dict.get("order_total_pence", 1000),
                    "order_total_pence",
                )

                order_ids.append(order_id)
                fraud_order_ids.append(order_id)
                orders_by_store[store_id] = orders_by_store.get(store_id, 0) + 1
                if gt.is_fraud:
                    fraud_orders_by_store[store_id] = fraud_orders_by_store.get(store_id, 0) + 1

                if gt.fraud_category == "collusive_merchant":
                    assert store_id in COLLUSIVE_STORES

                _insert_minimal_order_row(
                    conn,
                    order_id=order_id,
                    placed_at=placed_at,
                    user_id=user_id,
                    store_id=store_id,
                    merchant_id=uuid.uuid5(
                        uuid.NAMESPACE_DNS,
                        f"collusive-store-concentration-merchant-{order_id.hex}",
                    ),
                    order_number=f"COL-FR-{order_id.hex[:12]}",
                    total_pence=total_pence,
                )
                conn.execute(
                    _GROUND_TRUTH_INSERT_SQL,
                    {
                        "order_id": order_id,
                        "is_fraud": gt.is_fraud,
                        "fraud_category": gt.fraud_category,
                        "pattern_notes": gt.pattern_notes,
                        "ring_id": gt.ring_id,
                    },
                )

            for i in range(normal_order_count):
                order_id = uuid.uuid5(
                    uuid.NAMESPACE_DNS,
                    f"collusive-store-concentration-legit-{i}",
                )
                store_id = uuid.uuid5(
                    uuid.NAMESPACE_DNS,
                    f"collusive-store-concentration-store-{i}",
                )
                order_ids.append(order_id)
                orders_by_store[store_id] = orders_by_store.get(store_id, 0) + 1

                _insert_minimal_order_row(
                    conn,
                    order_id=order_id,
                    placed_at=datetime.now(timezone.utc),
                    user_id=uuid.uuid5(
                        uuid.NAMESPACE_DNS,
                        f"collusive-store-concentration-user-{i}",
                    ),
                    store_id=store_id,
                    merchant_id=uuid.uuid5(
                        uuid.NAMESPACE_DNS,
                        f"collusive-store-concentration-merchant-legit-{i}",
                    ),
                    order_number=f"COL-LG-{i:05d}-{order_id.hex[:6]}",
                    total_pence=1000,
                )

        collusive_store_ids = {
            store_id for store_id in orders_by_store if store_id in COLLUSIVE_STORES
        }
        normal_store_ids = {
            store_id for store_id in orders_by_store if store_id not in COLLUSIVE_STORES
        }
        assert collusive_store_ids
        assert normal_store_ids

        collusive_order_total = sum(orders_by_store[store_id] for store_id in collusive_store_ids)
        normal_order_total = sum(orders_by_store[store_id] for store_id in normal_store_ids)
        collusive_fraud_total = sum(
            fraud_orders_by_store.get(store_id, 0) for store_id in collusive_store_ids
        )
        normal_fraud_total = sum(
            fraud_orders_by_store.get(store_id, 0) for store_id in normal_store_ids
        )

        assert collusive_order_total > 0
        assert normal_order_total > 0

        collusive_store_rate = collusive_fraud_total / collusive_order_total
        normal_store_rate = normal_fraud_total / normal_order_total
        assert collusive_store_rate >= normal_store_rate * 10.0
    finally:
        if not order_ids:
            return

        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM chargebacks WHERE order_id = ANY(:ids)"),
                {"ids": order_ids},
            )
            conn.execute(
                text("DELETE FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"),
                {"ids": fraud_order_ids},
            )
            conn.execute(
                text("DELETE FROM orders_archive WHERE order_id = ANY(:ids)"),
                {"ids": order_ids},
            )
            conn.execute(
                text("DELETE FROM orders WHERE order_id = ANY(:ids)"),
                {"ids": order_ids},
            )


def test_scoring_user_cannot_read_ground_truth() -> None:
    engine = _create_engine(_SCORING_URL)

    with pytest.raises(sqlalchemy_exc.ProgrammingError) as exc_info:
        with engine.connect() as conn:
            conn.execute(text("SELECT * FROM sim.simulator_ground_truth LIMIT 1"))

    engine.dispose()

    message = str(exc_info.value).lower()
    assert "permission denied" in message or "insufficientprivilege" in message


def test_scoring_user_cannot_join_to_ground_truth() -> None:
    engine = _create_engine(_SCORING_URL)

    with pytest.raises(sqlalchemy_exc.ProgrammingError) as exc_info:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT o.order_id FROM orders o "
                    "JOIN sim.simulator_ground_truth gt ON o.order_id = gt.order_id "
                    "LIMIT 1"
                )
            )

    engine.dispose()

    message = str(exc_info.value).lower()
    assert "permission denied" in message or "insufficientprivilege" in message


def _chargeback_database_url() -> str:
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://app:app_dev_password@postgres:5432/fraud_platform"
    )
    os.environ["DATABASE_URL"] = database_url
    simulator.chargebacks.DATABASE_URL = database_url
    return database_url


def _insert_order_for_chargeback_test(
    conn: Connection,
    *,
    order_id: uuid.UUID,
    placed_at: datetime,
    delivered_at: datetime,
    user_id: uuid.UUID,
    store_id: uuid.UUID,
    merchant_id: uuid.UUID,
    order_number: str,
    fraud_category: str | None,
    is_fraud: bool = True,
) -> None:
    _insert_minimal_order_row(
        conn,
        order_id=order_id,
        placed_at=placed_at,
        user_id=user_id,
        store_id=store_id,
        merchant_id=merchant_id,
        order_number=order_number,
        total_pence=10000,
    )
    conn.execute(
        text(
            "UPDATE orders "
            "SET delivered_at = :delivered_at "
            "WHERE order_id = :order_id "
            "AND placed_at = :placed_at"
        ),
        {"order_id": order_id, "placed_at": placed_at, "delivered_at": delivered_at},
    )
    conn.execute(
        _GROUND_TRUTH_INSERT_SQL,
        {
            "order_id": order_id,
            "is_fraud": is_fraud,
            "fraud_category": fraud_category,
            "pattern_notes": "chargeback test row",
            "ring_id": None,
        },
    )


def test_chargeback_rates() -> None:
    order_ids: list[uuid.UUID] = []
    engine = shared.db.get_engine("app")
    _chargeback_database_url()
    now = datetime.now(timezone.utc)
    delivered_at = now - timedelta(days=59, minutes=30)
    placed_at = delivered_at - timedelta(days=1)
    fraud_order_counts: dict[str, int] = {
        "stolen_card": 200,
        "account_takeover": 200,
        "triangulation": 200,
        "collusive_merchant": 200,
        "refund_abuse": 200,
        "reseller": 200,
        "promo_abuse": 200,
    }
    expected_rates: dict[str, float] = {
        category: simulator.chargebacks._chargeback_probability(True, category)
        for category in fraud_order_counts
    }
    expected_rates["legit"] = simulator.chargebacks._chargeback_probability(False, None)
    inserted_counts: dict[str, int] = {category: 0 for category in fraud_order_counts}
    inserted_counts["legit"] = 0

    try:
        with engine.begin() as conn:
            for category, count in fraud_order_counts.items():
                for i in range(count):
                    order_id = uuid.uuid5(
                        uuid.NAMESPACE_DNS,
                        f"chargeback-rate-{category}-{i}",
                    )
                    order_ids.append(order_id)
                    _insert_order_for_chargeback_test(
                        conn,
                        order_id=order_id,
                        placed_at=placed_at,
                        delivered_at=delivered_at,
                        user_id=uuid.uuid5(
                            uuid.NAMESPACE_DNS,
                            f"chargeback-user-rate-{category}-{i}",
                        ),
                        store_id=uuid.uuid5(
                            uuid.NAMESPACE_DNS,
                            f"chargeback-store-rate-{category}-{i}",
                        ),
                        merchant_id=uuid.uuid5(
                            uuid.NAMESPACE_DNS,
                            f"chargeback-merchant-rate-{category}-{i}",
                        ),
                        order_number=f"CBR-{category[:3].upper()}-{i:03d}-{order_id.hex[:8]}",
                        fraud_category=category,
                    )
                    inserted_counts[category] += 1

            for i in range(200):
                order_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"chargeback-rate-legit-{i:05d}")
                order_ids.append(order_id)
                _insert_order_for_chargeback_test(
                    conn,
                    order_id=order_id,
                    placed_at=placed_at,
                    delivered_at=delivered_at,
                    user_id=uuid.uuid5(
                        uuid.NAMESPACE_DNS,
                        f"chargeback-user-rate-legit-{i}",
                    ),
                    store_id=uuid.uuid5(
                        uuid.NAMESPACE_DNS,
                        f"chargeback-store-rate-legit-{i}",
                    ),
                    merchant_id=uuid.uuid5(
                        uuid.NAMESPACE_DNS,
                        f"chargeback-merchant-rate-legit-{i}",
                    ),
                    order_number=f"CBR-LG-{i:05d}-{order_id.hex[:6]}",
                    fraud_category=None,
                    is_fraud=False,
                )
                inserted_counts["legit"] += 1

        asyncio.run(simulator.chargebacks.run_once())

        with engine.connect() as conn:
            chargeback_rows = conn.execute(
                text(
                    "SELECT COALESCE(gt.fraud_category, 'legit') AS fraud_category, COUNT(*)\n"
                    "FROM chargebacks cb\n"
                    "JOIN sim.simulator_ground_truth gt USING (order_id)\n"
                    "WHERE cb.order_id = ANY(:ids)\n"
                    "GROUP BY COALESCE(gt.fraud_category, 'legit')"
                ),
                {"ids": order_ids},
            ).fetchall()

        chargeback_counts: dict[str, int] = {category: 0 for category in inserted_counts}
        for category, count in chargeback_rows:
            chargeback_counts[str(category)] = int(count)

        for category, expected_rate in expected_rates.items():
            observed = chargeback_counts[category] / inserted_counts[category]
            tolerance = 2.576 * math.sqrt(
                (expected_rate * (1.0 - expected_rate)) / inserted_counts[category]
            )
            assert abs(observed - expected_rate) <= tolerance
    finally:
        if not order_ids:
            return

        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM chargebacks WHERE order_id = ANY(:ids)"),
                {"ids": order_ids},
            )
            conn.execute(
                text("DELETE FROM sim.simulator_ground_truth WHERE order_id = ANY(:ids)"),
                {"ids": order_ids},
            )
            conn.execute(
                text("DELETE FROM orders_archive WHERE order_id = ANY(:ids)"),
                {"ids": order_ids},
            )
            conn.execute(
                text("DELETE FROM orders WHERE order_id = ANY(:ids)"),
                {"ids": order_ids},
            )


def test_chargeback_timing() -> None:
    rng = random.Random(11)
    thresholds_days: list[float] = [
        simulator.chargebacks._days_to_chargeback_threshold(rng, True) for _ in range(500)
    ]
    assert thresholds_days

    normal_dist = NormalDist()
    mu = math.log(14) - 0.7**2 / 2
    sigma = 0.7
    expected_p10 = math.exp(mu + sigma * normal_dist.inv_cdf(0.10))
    expected_p50 = math.exp(mu + sigma * normal_dist.inv_cdf(0.50))
    expected_p90 = math.exp(mu + sigma * normal_dist.inv_cdf(0.90))

    p10 = _percentile(thresholds_days, 0.10)
    p50 = _percentile(thresholds_days, 0.50)
    p90 = _percentile(thresholds_days, 0.90)

    # These values come from a lognormal(mean days, sigma) with
    # mu=ln(14)-0.7²/2, sigma=0.7 and are sampled through the
    # production threshold helper so the timing model is tested directly.
    assert abs(p10 - expected_p10) <= 1.5
    assert abs(p50 - expected_p50) <= 1.5
    assert abs(p90 - expected_p90) <= 1.5
    assert max(thresholds_days) <= 59.0


def test_chargeback_on_archived_order() -> None:
    candidate_order_id: uuid.UUID | None = None
    for idx in range(200):
        proposed = uuid.uuid5(uuid.NAMESPACE_DNS, f"archived-chargeback-{idx}")
        rng = random.Random(int(proposed.bytes[:8].hex(), 16))
        days_to_chargeback = simulator.chargebacks._days_to_chargeback_threshold(rng, True)
        if 45.0 >= days_to_chargeback and rng.random() < 0.60:
            candidate_order_id = proposed
            break

    assert candidate_order_id is not None
    order_id = candidate_order_id
    placed_at = datetime.now(timezone.utc) - timedelta(days=46)
    delivered_at = placed_at + timedelta(days=1)
    engine = shared.db.get_engine("app")
    database_url = _chargeback_database_url()

    async def _run_once_against_archived_order() -> None:
        pool = await asyncpg.create_pool(database_url)
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO orders_archive SELECT * FROM orders WHERE order_id = $1 AND placed_at = $2",
                    order_id,
                    placed_at,
                )
                await conn.execute(
                    "DELETE FROM orders WHERE order_id = $1 AND placed_at = $2",
                    order_id,
                    placed_at,
                )
            await simulator.chargebacks.generate_chargebacks(pool)
        finally:
            await pool.close()

    try:
        with engine.begin() as conn:
            _insert_order_for_chargeback_test(
                conn,
                order_id=order_id,
                placed_at=placed_at,
                delivered_at=delivered_at,
                user_id=uuid.uuid5(uuid.NAMESPACE_DNS, "archived-chargeback-user"),
                store_id=uuid.uuid5(uuid.NAMESPACE_DNS, "archived-chargeback-store"),
                merchant_id=uuid.uuid5(uuid.NAMESPACE_DNS, "archived-chargeback-merchant"),
                order_number=f"CBA-{order_id.hex[:12]}",
                fraud_category="stolen_card",
            )

        asyncio.run(_run_once_against_archived_order())

        with engine.connect() as conn:
            chargeback_count = conn.execute(
                text("SELECT COUNT(*) FROM chargebacks WHERE order_id = :order_id"),
                {"order_id": order_id},
            ).scalar_one()

            archived_received = conn.execute(
                text(
                    "SELECT chargeback_received_at "
                    "FROM orders_archive "
                    "WHERE order_id = :order_id"
                    "  AND placed_at = :placed_at"
                ),
                {"order_id": order_id, "placed_at": placed_at},
            ).scalar_one_or_none()

        assert chargeback_count == 1
        assert archived_received is not None
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM chargebacks WHERE order_id = :order_id"),
                {"order_id": order_id},
            )
            conn.execute(
                text("DELETE FROM sim.simulator_ground_truth WHERE order_id = :order_id"),
                {"order_id": order_id},
            )
            conn.execute(
                text("DELETE FROM orders_archive WHERE order_id = :order_id"),
                {"order_id": order_id},
            )
            conn.execute(
                text("DELETE FROM orders WHERE order_id = :order_id"),
                {"order_id": order_id},
            )
