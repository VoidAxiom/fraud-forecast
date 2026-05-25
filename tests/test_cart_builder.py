from __future__ import annotations

import random
from dataclasses import dataclass
from uuid import UUID

from simulator.cart_builder import UserProfile, build_realistic_cart


@dataclass
class MenuItemFixture:
    item_id: UUID
    item_name: str
    category: str
    price_pence: int
    is_hot_food: bool


def _build_menu_items() -> list[MenuItemFixture]:
    rng = random.Random(7)
    store_items: list[MenuItemFixture] = []

    for idx in range(8):
        store_items.append(
            MenuItemFixture(
                item_id=UUID(int=rng.getrandbits(128), version=4),
                item_name=f"Main {idx + 1}",
                category="MAIN",
                price_pence=900 + idx * 100,
                is_hot_food=True,
            )
        )

    for idx in range(6):
        store_items.append(
            MenuItemFixture(
                item_id=UUID(int=rng.getrandbits(128), version=4),
                item_name=f"Side {idx + 1}",
                category="SIDE",
                price_pence=350 + idx * 50,
                is_hot_food=False,
            )
        )

    for idx in range(4):
        store_items.append(
            MenuItemFixture(
                item_id=UUID(int=rng.getrandbits(128), version=4),
                item_name=f"Drink {idx + 1}",
                category="DRINK",
                price_pence=250 + idx * 25,
                is_hot_food=False,
            )
        )

    for idx in range(2):
        store_items.append(
            MenuItemFixture(
                item_id=UUID(int=rng.getrandbits(128), version=4),
                item_name=f"Other {idx + 1}",
                category="OTHER",
                price_pence=500,
                is_hot_food=False,
            )
        )

    return store_items


def test_cart_builder_distribution() -> None:
    rng = random.Random(42)
    menu_items = _build_menu_items()
    user_profile = UserProfile(user_id=UUID(int=99), preferred_cuisines=["ITALIAN", "INDIAN"])
    carts = [build_realistic_cart(UUID(int=0), user_profile, menu_items, rng) for _ in range(1000)]

    main_ids = {item.item_id for item in menu_items if item.category == "MAIN"}
    carts_with_main = sum(
        1 for cart in carts if any(item.item_id in main_ids for item in cart.items)
    )

    assert carts_with_main / len(carts) >= 0.90

    item_counts = [cart.item_count for cart in carts]
    mean_item_count = sum(item_counts) / len(item_counts)
    assert 2.5 <= mean_item_count <= 3.5
    assert all(1 <= count <= 8 for count in item_counts)
    assert all(len(cart.items) >= 1 for cart in carts)


def test_cart_builder_repeat_qty() -> None:
    rng = random.Random(7)
    menu_items = _build_menu_items()
    user_profile = UserProfile(user_id=UUID(int=99))
    carts = [build_realistic_cart(UUID(int=0), user_profile, menu_items, rng) for _ in range(500)]

    carts_with_repeats = sum(1 for cart in carts if any(item.qty >= 2 for item in cart.items))

    assert carts_with_repeats / len(carts) >= 0.40


def test_cart_builder_subtotal() -> None:
    rng = random.Random(99)
    menu_items = _build_menu_items()
    user_profile = UserProfile(user_id=UUID(int=99))
    carts = [build_realistic_cart(UUID(int=0), user_profile, menu_items, rng) for _ in range(10)]

    for cart in carts:
        assert cart.subtotal_pence == sum(
            item.qty * item.unit_price_pence for item in cart.items
        )
        assert cart.item_count == sum(item.qty for item in cart.items)
        assert cart.unique_item_count == len({item.item_id for item in cart.items})


def test_cart_builder_deterministic() -> None:
    menu_items = _build_menu_items()
    store_id = UUID(int=1)
    user_profile = UserProfile(user_id=UUID(int=99))

    rng = random.Random(123)
    first_cart = build_realistic_cart(store_id, user_profile, menu_items, rng)

    rng = random.Random(123)
    second_cart = build_realistic_cart(store_id, user_profile, menu_items, rng)

    assert first_cart == second_cart
