from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True)
class VATLineItem:
    line_total_pence: int
    is_hot_food: bool


VAT_RATE_STANDARD = Decimal("0.20")
VAT_RATE_ZERO_RATED = Decimal("0.00")


def pounds_to_pence(amount: Decimal) -> int:
    """Convert £-amount (Decimal) to integer pence, half-up rounded."""
    if not isinstance(amount, Decimal):
        raise TypeError("pounds_to_pence requires Decimal, not float — money never uses floats")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def pence_to_pounds_str(pence: int) -> str:
    """Format an integer-pence amount as '£12.50' / '£0.05' etc."""
    sign = "-" if pence < 0 else ""
    abs_pence = abs(pence)
    pounds, p = divmod(abs_pence, 100)
    return f"{sign}£{pounds}.{p:02d}"


def calculate_vat(items: list[VATLineItem]) -> int:
    """Sum VAT across line items. Hot food = 20%, cold food = 0%. Half-up rounding per line."""
    total = 0
    for item in items:
        rate = VAT_RATE_STANDARD if item.is_hot_food else VAT_RATE_ZERO_RATED
        vat = (Decimal(item.line_total_pence) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        total += int(vat)
    return total


def calculate_total(
    subtotal_pence: int,
    vat_pence: int,
    delivery_fee_pence: int,
    service_fee_pence: int,
    tip_pence: int,
    discount_pence: int,
) -> int:
    """Order total in pence. Discount is positive and subtracted."""
    return (
        subtotal_pence + vat_pence + delivery_fee_pence + service_fee_pence + tip_pence
        - discount_pence
    )
