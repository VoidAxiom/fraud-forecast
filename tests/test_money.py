"""Pure-function tests for shared/money.py — no DB."""

from __future__ import annotations

from decimal import Decimal

import pytest

from shared.money import (
    VATLineItem,
    calculate_total,
    calculate_vat,
    pence_to_pounds_str,
    pounds_to_pence,
)


def test_pounds_to_pence_exact() -> None:
    assert pounds_to_pence(Decimal("12.50")) == 1250


def test_pounds_to_pence_half_up_rounding() -> None:
    assert pounds_to_pence(Decimal("12.505")) == 1251


def test_pounds_to_pence_rejects_float() -> None:
    with pytest.raises(TypeError):
        pounds_to_pence(12.50)  # type: ignore[arg-type]


def test_pence_to_pounds_str() -> None:
    assert pence_to_pounds_str(1250) == "£12.50"
    assert pence_to_pounds_str(5) == "£0.05"
    assert pence_to_pounds_str(0) == "£0.00"


def test_calculate_vat_hot_only() -> None:
    assert calculate_vat([VATLineItem(line_total_pence=1000, is_hot_food=True)]) == 200


def test_calculate_vat_cold_only() -> None:
    assert calculate_vat([VATLineItem(line_total_pence=1000, is_hot_food=False)]) == 0


def test_calculate_vat_mixed() -> None:
    items = [
        VATLineItem(line_total_pence=1000, is_hot_food=True),
        VATLineItem(line_total_pence=500, is_hot_food=False),
    ]
    assert calculate_vat(items) == 200


def test_calculate_vat_half_up_at_pence_boundary() -> None:
    # 5p × 20% = 1.0p → rounds to 1
    # 2p × 20% = 0.4p → rounds to 0
    assert calculate_vat([VATLineItem(line_total_pence=5, is_hot_food=True)]) == 1
    assert calculate_vat([VATLineItem(line_total_pence=2, is_hot_food=True)]) == 0


def test_calculate_total() -> None:
    # 1000 + 200 + 300 + 100 + 50 - 150 = 1500
    assert (
        calculate_total(
            subtotal_pence=1000,
            vat_pence=200,
            delivery_fee_pence=300,
            service_fee_pence=100,
            tip_pence=50,
            discount_pence=150,
        )
        == 1500
    )
