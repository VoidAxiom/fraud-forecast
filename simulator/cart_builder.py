from __future__ import annotations

import random
from dataclasses import dataclass
from math import exp
from typing import Protocol
from uuid import UUID


@dataclass
class UserProfile:
    user_id: UUID
    preferred_cuisines: list[str] | None = None


class MenuItemLike(Protocol):
    item_id: UUID
    item_name: str
    category: str
    price_pence: int
    is_hot_food: bool


@dataclass
class CartItem:
    item_id: UUID
    name: str
    qty: int
    unit_price_pence: int
    is_hot_food: bool

    @property
    def line_total_pence(self) -> int:
        return self.qty * self.unit_price_pence


@dataclass
class Cart:
    store_id: UUID
    items: list[CartItem]

    @property
    def subtotal_pence(self) -> int:
        return sum(item.line_total_pence for item in self.items)

    @property
    def item_count(self) -> int:
        return sum(item.qty for item in self.items)

    @property
    def unique_item_count(self) -> int:
        return len({item.item_id for item in self.items})


def _sample_item_count(rng: random.Random) -> int:
    lam = 2.5
    threshold = exp(-lam)
    p = 1.0
    k = 0
    while p > threshold:
        p *= rng.random()
        k += 1
    return max(1, min(8, k - 1))


def _sample_item_qty(rng: random.Random) -> int:
    return 2 if rng.random() < 0.5 else 1


def build_realistic_cart(
    store_id: UUID,
    user_profile: UserProfile,
    menu_items: list[MenuItemLike],
    rng: random.Random | None = None,
) -> Cart:
    if rng is None:
        rng = random.Random()

    if not menu_items:
        raise ValueError("menu_items must not be empty")

    main_item_ids = {item.item_id for item in menu_items if item.category == "MAIN"}
    target_item_count = _sample_item_count(rng)
    raw_items: list[tuple[MenuItemLike, int]] = []

    category_rules: list[tuple[str, float]] = [
        ("MAIN", 0.70),
        ("SIDE", 0.40),
        ("DRINK", 0.30),
    ]

    for category, probability in category_rules:
        if rng.random() < probability:
            candidates = [item for item in menu_items if item.category == category]
            if not candidates:
                continue
            raw_items.append((rng.choice(candidates), 1))

    while len(raw_items) < target_item_count:
        raw_items.append((rng.choice(menu_items), _sample_item_qty(rng)))

    merged_items: dict[UUID, CartItem] = {}
    ordered_item_ids: list[UUID] = []
    for menu_item, qty in raw_items:
        existing_item = merged_items.get(menu_item.item_id)
        if existing_item is None:
            merged_items[menu_item.item_id] = CartItem(
                item_id=menu_item.item_id,
                name=menu_item.item_name,
                qty=qty,
                unit_price_pence=menu_item.price_pence,
                is_hot_food=menu_item.is_hot_food,
            )
            ordered_item_ids.append(menu_item.item_id)
            continue
        existing_item.qty += qty

    if not main_item_ids:
        return Cart(
            store_id=store_id,
            items=[merged_items[item_id] for item_id in ordered_item_ids],
        )

    if not any(item_id in main_item_ids for item_id in merged_items):
        main_candidates = [item for item in menu_items if item.category == "MAIN"]
        forced_main = rng.choice(main_candidates)
        existing_main = merged_items.get(forced_main.item_id)
        if existing_main is None:
            merged_items[forced_main.item_id] = CartItem(
                item_id=forced_main.item_id,
                name=forced_main.item_name,
                qty=1,
                unit_price_pence=forced_main.price_pence,
                is_hot_food=forced_main.is_hot_food,
            )
            ordered_item_ids.append(forced_main.item_id)
        else:
            existing_main.qty += 1

    total_qty = sum(item.qty for item in merged_items.values())
    if total_qty > 8:
        overflow = total_qty - 8
        # Trim non-MAIN items first to protect the main-item guarantee
        for item_id in reversed(ordered_item_ids):
            if overflow <= 0:
                break
            if item_id in main_item_ids:
                continue
            current_item = merged_items[item_id]
            if current_item.qty > 1:
                reduction = min(current_item.qty - 1, overflow)
                current_item.qty -= reduction
                overflow -= reduction
            elif len(merged_items) > 1:
                del merged_items[item_id]
                ordered_item_ids.remove(item_id)
                overflow -= 1
        # If still overflowing, trim from any item (including MAIN)
        if overflow > 0:
            for item_id in reversed(ordered_item_ids):
                if overflow <= 0:
                    break
                current_item = merged_items[item_id]
                if current_item.qty > 1:
                    reduction = min(current_item.qty - 1, overflow)
                    current_item.qty -= reduction
                    overflow -= reduction
                elif len(merged_items) > 1:
                    del merged_items[item_id]
                    ordered_item_ids.remove(item_id)
                    overflow -= 1

    return Cart(
        store_id=store_id,
        items=[merged_items[item_id] for item_id in ordered_item_ids],
    )
