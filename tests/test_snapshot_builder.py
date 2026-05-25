from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
import redis as redis_lib
from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.models import Device, PaymentMethod, Session as SessionModel, Store, User, UserAddress
from simulator.cart_builder import Cart, CartItem
from simulator.snapshot_builder import IPAddress, build_order_snapshot


@pytest.fixture
def redis_client() -> redis_lib.Redis[bytes]:
    client = redis_lib.Redis.from_url("redis://redis:6379/0")
    pre_existing: set[bytes] = set(client.keys(b"user_stats:*"))
    try:
        yield client
    finally:
        post_keys: set[bytes] = set(client.keys(b"user_stats:*"))
        new_keys = post_keys - pre_existing
        if new_keys:
            client.delete(*new_keys)


def _insert_user(
    db_session: Session,
    user_id: UUID,
    created_at: datetime,
) -> User:
    email = f"snapshot-{user_id}@example.com"
    db_session.execute(
        text(
            """
            INSERT INTO users (
                user_id, email, password_hash, created_at
            ) VALUES (:uid, :email, 'pw', :created_at)
            """
        ),
        {
            "uid": user_id,
            "email": email,
            "created_at": created_at,
        },
    )
    result = db_session.query(User).filter_by(user_id=user_id).one()
    return result


def _insert_merchant(db_session: Session, merchant_id: UUID) -> UUID:
    db_session.execute(
        text(
            """
            INSERT INTO merchants (merchant_id, legal_name, brand_name)
            VALUES (:merchant_id, 'Merchant', 'Merchant')
            """
        ),
        {"merchant_id": merchant_id},
    )
    return merchant_id


def _insert_store(
    db_session: Session,
    store_id: UUID,
    merchant_id: UUID,
    latitude: float,
    longitude: float,
) -> Store:
    db_session.execute(
        text(
            """
            INSERT INTO stores (
                store_id, merchant_id, store_name, address_line_1, city, postcode,
                latitude, longitude
            ) VALUES (
                :store_id, :merchant_id, 'Store', '1 Test Road', 'London', 'SW1A 1AA',
                :latitude, :longitude
            )
            """
        ),
        {
            "store_id": store_id,
            "merchant_id": merchant_id,
            "latitude": latitude,
            "longitude": longitude,
        },
    )
    return db_session.query(Store).filter_by(store_id=store_id).one()


def _insert_menu_items(db_session: Session, store_id: UUID) -> list[UUID]:
    item_ids: list[UUID] = [uuid4() for _ in range(3)]
    db_session.execute(
        text(
            """
            INSERT INTO menu_items (
                item_id, store_id, item_name, category, price_pence, is_hot_food
            ) VALUES
            (:item1, :store_id, 'Item 1', 'MAIN', 1000, true),
            (:item2, :store_id, 'Item 2', 'SIDE', 500, false),
            (:item3, :store_id, 'Item 3', 'DRINK', 300, false)
            """
        ),
        {
            "store_id": store_id,
            "item1": item_ids[0],
            "item2": item_ids[1],
            "item3": item_ids[2],
        },
    )
    return item_ids


def _insert_user_address(
    db_session: Session,
    address_id: UUID,
    user_id: UUID,
    latitude: float,
    longitude: float,
    address_type: str = "RESIDENTIAL",
) -> UserAddress:
    db_session.execute(
        text(
            """
            INSERT INTO user_addresses (
                address_id, user_id, address_line_1, city, postcode,
                latitude, longitude, address_type
            ) VALUES (
                :address_id, :user_id, '1 Delivery Lane', 'London', 'SW1A 1AA',
                :latitude, :longitude, :address_type
            )
            """
        ),
        {
            "address_id": address_id,
            "user_id": user_id,
            "latitude": latitude,
            "longitude": longitude,
            "address_type": address_type,
        },
    )
    return db_session.query(UserAddress).filter_by(address_id=address_id).one()


def _insert_device(
    db_session: Session,
    device_id: UUID,
) -> Device:
    db_session.execute(
        text(
            """
            INSERT INTO devices (
                device_id, device_fingerprint, device_type, platform, os_version,
                app_version, browser_name, browser_version
            ) VALUES (
                :device_id, :device_fingerprint, 'MOBILE_APP', 'iOS', '16.5', '5.0',
                'Safari', '17'
            )
            """
        ),
        {
            "device_id": device_id,
            "device_fingerprint": f"fp-{device_id}",
        },
    )
    return db_session.query(Device).filter_by(device_id=device_id).one()


def _insert_payment_method(
    db_session: Session,
    payment_method_id: UUID,
    user_id: UUID,
    billing_address_id: UUID | None,
    unique_users_count: int = 3,
) -> PaymentMethod:
    db_session.execute(
        text(
            """
            INSERT INTO payment_methods (
                payment_method_id, user_id, payment_type, card_bin, card_last_four,
                card_brand, card_funding_type, card_issuer_country, is_digital_native_bank,
                billing_address_id, unique_users_count
            ) VALUES (
                :payment_method_id, :user_id, 'CREDIT_CARD', '123456', '1111',
                'VISA', 'DEBIT', 'GB', false, :billing_address_id, :unique_users_count
            )
            """
        ),
        {
            "payment_method_id": payment_method_id,
            "user_id": user_id,
            "billing_address_id": billing_address_id,
            "unique_users_count": unique_users_count,
        },
    )
    return db_session.query(PaymentMethod).filter_by(payment_method_id=payment_method_id).one()


def _insert_session(
    db_session: Session,
    session_id: UUID,
    user_id: UUID,
    device_id: UUID,
) -> SessionModel:
    db_session.execute(
        text(
            """
            INSERT INTO sessions (
                session_id, user_id, device_id, ip_address, ip_country, ip_city
            ) VALUES (
                :session_id, :user_id, :device_id, '127.0.0.1', 'GB', 'London'
            )
            """
        ),
        {
            "session_id": session_id,
            "user_id": user_id,
            "device_id": device_id,
        },
    )
    return db_session.query(SessionModel).filter_by(session_id=session_id).one()


def _insert_order(
    db_session: Session,
    *,
    order_id: UUID,
    merchant_id: UUID,
    store_id: UUID,
    user: User,
    payment_method_id: UUID,
    delivery_address_id: UUID,
    placed_at: datetime,
    total_pence: int = 1500,
) -> None:
    db_session.execute(
        text(
            """
            INSERT INTO orders (
                order_id, placed_at, order_number, order_status, order_channel, order_type,
                user_id, user_account_age_days, user_total_orders_lifetime, user_total_orders_30d,
                user_total_spend_lifetime_pence, user_email, user_email_domain, store_id,
                merchant_id, store_city, store_country, store_latitude, store_longitude,
                delivery_address_id, delivery_latitude, delivery_longitude, delivery_address_type,
                item_count, unique_item_count, subtotal_pence, vat_pence, delivery_fee_pence,
                service_fee_pence, tip_pence, discount_pence, total_pence, currency, payment_type,
                payment_method_id
            ) VALUES (
                :order_id, :placed_at, :order_number, 'PLACED', 'WEB', 'DELIVERY',
                :user_id, 1, 0, 0, :total_spend, :user_email, :user_email_domain,
                :store_id, :merchant_id, :store_city, :store_country, 51.5074, -0.1278,
                :delivery_address_id, 51.5000, -0.1000, 'RESIDENTIAL',
                1, 1, 1000, 200, 0, 0, 0, 0, :total_pence, 'GBP', 'CREDIT_CARD',
                :payment_method_id
            )
            """
        ),
        {
            "order_id": order_id,
            "placed_at": placed_at,
            "order_number": f"SNAP-{str(order_id)[:8]}",
            "user_id": user.user_id,
            "total_spend": total_pence,
            "user_email": user.email,
            "user_email_domain": user.email.split("@")[1],
            "store_id": store_id,
            "merchant_id": merchant_id,
            "store_city": "London",
            "store_country": "GB",
            "delivery_address_id": delivery_address_id,
            "total_pence": total_pence,
            "payment_method_id": payment_method_id,
        },
    )


def _build_cart(store_id: UUID) -> Cart:
    items = [
        CartItem(item_id=uuid4(), name="Item 1", qty=1, unit_price_pence=1000, is_hot_food=True),
        CartItem(item_id=uuid4(), name="Item 2", qty=1, unit_price_pence=500, is_hot_food=False),
    ]
    return Cart(store_id=store_id, items=items)


def _build_haversine_cart(store_id: UUID) -> Cart:
    items = [
        CartItem(item_id=uuid4(), name="Item 1", qty=1, unit_price_pence=500, is_hot_food=False),
        CartItem(item_id=uuid4(), name="Item 2", qty=1, unit_price_pence=700, is_hot_food=True),
        CartItem(item_id=uuid4(), name="Item 3", qty=1, unit_price_pence=900, is_hot_food=True),
    ]
    return Cart(store_id=store_id, items=items)


def _cleanup_test_rows(
    db_session: Session,
    *,
    user_id: UUID,
    merchant_id: UUID,
    store_id: UUID,
    device_id: UUID,
    session_id: UUID,
    payment_method_id: UUID,
    order_ids: list[UUID] | None = None,
    address_ids: list[UUID] | None = None,
    menu_item_ids: list[UUID] | None = None,
) -> None:
    if order_ids:
        for order_id in order_ids:
            db_session.execute(text("DELETE FROM orders WHERE order_id = :oid"), {"oid": order_id})
    if menu_item_ids:
        for item_id in menu_item_ids:
            db_session.execute(text("DELETE FROM menu_items WHERE item_id = :iid"), {"iid": item_id})
    db_session.execute(
        text("DELETE FROM payment_methods WHERE payment_method_id = :pmid"),
        {"pmid": payment_method_id},
    )
    if address_ids:
        for address_id in address_ids:
            db_session.execute(text("DELETE FROM user_addresses WHERE address_id = :aid"), {"aid": address_id})
    db_session.execute(text("DELETE FROM sessions WHERE session_id = :sid"), {"sid": session_id})
    db_session.execute(text("DELETE FROM devices WHERE device_id = :did"), {"did": device_id})
    db_session.execute(text("DELETE FROM stores WHERE store_id = :sid"), {"sid": store_id})
    db_session.execute(text("DELETE FROM merchants WHERE merchant_id = :mid"), {"mid": merchant_id})
    db_session.execute(text("DELETE FROM users WHERE user_id = :uid"), {"uid": user_id})


def test_snapshot_brand_new_user(db_session: Session, redis_client: redis_lib.Redis[bytes]) -> None:
    user_id = uuid4()
    merchant_id = uuid4()
    store_id = uuid4()
    device_id = uuid4()
    session_id = uuid4()
    payment_method_id = uuid4()
    delivery_address_id = uuid4()

    created_at = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
    placed_at = datetime(2026, 5, 24, 12, 30, 0, tzinfo=timezone.utc)

    user = _insert_user(db_session, user_id=user_id, created_at=created_at)
    _insert_merchant(db_session, merchant_id=merchant_id)
    store = _insert_store(db_session, store_id=store_id, merchant_id=merchant_id, latitude=51.5074, longitude=-0.1278)
    menu_item_ids = _insert_menu_items(db_session, store_id=store_id)
    delivery_address = _insert_user_address(
        db_session,
        address_id=delivery_address_id,
        user_id=user_id,
        latitude=51.5000,
        longitude=-0.1000,
    )
    device = _insert_device(db_session, device_id=device_id)
    payment_method = _insert_payment_method(
        db_session,
        payment_method_id=payment_method_id,
        user_id=user_id,
        billing_address_id=None,
        unique_users_count=7,
    )
    session = _insert_session(
        db_session,
        session_id=session_id,
        user_id=user_id,
        device_id=device_id,
    )
    cart = _build_cart(store_id=store_id)
    ip = IPAddress(
        ip_address="1.2.3.4",
        ip_country="GB",
        ip_city="London",
        ip_is_proxy=True,
        city_centroid_lat=51.4800,
        city_centroid_lon=-0.1500,
    )

    try:
        snapshot = build_order_snapshot(
            user=user,
            store=store,
            cart=cart,
            delivery_address=delivery_address,
            payment_method=payment_method,
            device=device,
            session=session,
            ip=ip,
            promo=None,
            placed_at=placed_at,
            db_session=db_session,
            redis_client=redis_client,
        )

        assert snapshot["is_first_order_for_user"] is True
        assert snapshot["user_total_orders_lifetime"] == 0
        assert snapshot["user_total_spend_lifetime_pence"] == 0
        assert snapshot["is_new_payment_method"] is True
        assert snapshot["is_new_delivery_address"] is True
    finally:
        _cleanup_test_rows(
            db_session,
            user_id=user_id,
            merchant_id=merchant_id,
            store_id=store_id,
            device_id=device_id,
            session_id=session_id,
            payment_method_id=payment_method_id,
            address_ids=[delivery_address_id],
            menu_item_ids=menu_item_ids,
        )


def test_snapshot_after_one_order(db_session: Session, redis_client: redis_lib.Redis[bytes]) -> None:
    user_id = uuid4()
    merchant_id = uuid4()
    store_id = uuid4()
    device_id = uuid4()
    session_id = uuid4()
    payment_method_id = uuid4()
    delivery_address_id = uuid4()
    order_id = uuid4()

    created_at = datetime(2026, 5, 24, 10, 0, 0, tzinfo=timezone.utc)
    placed_at = datetime(2026, 5, 24, 11, 0, 0, tzinfo=timezone.utc)

    user = _insert_user(db_session, user_id=user_id, created_at=created_at)
    _insert_merchant(db_session, merchant_id=merchant_id)
    store = _insert_store(db_session, store_id=store_id, merchant_id=merchant_id, latitude=51.5074, longitude=-0.1278)
    menu_item_ids = _insert_menu_items(db_session, store_id=store_id)
    delivery_address = _insert_user_address(
        db_session,
        address_id=delivery_address_id,
        user_id=user_id,
        latitude=51.5000,
        longitude=-0.1000,
    )
    device = _insert_device(db_session, device_id=device_id)
    payment_method = _insert_payment_method(
        db_session,
        payment_method_id=payment_method_id,
        user_id=user_id,
        billing_address_id=delivery_address_id,
        unique_users_count=4,
    )
    session = _insert_session(
        db_session,
        session_id=session_id,
        user_id=user_id,
        device_id=device_id,
    )
    _insert_order(
        db_session,
        order_id=order_id,
        merchant_id=merchant_id,
        store_id=store_id,
        user=user,
        payment_method_id=payment_method_id,
        delivery_address_id=delivery_address_id,
        placed_at=placed_at,
        total_pence=1500,
    )

    cart = _build_cart(store_id=store_id)
    ip = IPAddress(
        ip_address="1.2.3.4",
        ip_country="GB",
        ip_city="London",
        ip_is_proxy=False,
        city_centroid_lat=51.4800,
        city_centroid_lon=-0.1500,
    )

    try:
        snapshot = build_order_snapshot(
            user=user,
            store=store,
            cart=cart,
            delivery_address=delivery_address,
            payment_method=payment_method,
            device=device,
            session=session,
            ip=ip,
            promo=None,
            placed_at=placed_at,
            db_session=db_session,
            redis_client=redis_client,
        )

        assert snapshot["is_first_order_for_user"] is False
        assert snapshot["user_total_orders_lifetime"] == 1
        assert snapshot["user_total_spend_lifetime_pence"] == 1500
        assert snapshot["is_new_payment_method"] is False
        assert snapshot["is_new_delivery_address"] is False
    finally:
        _cleanup_test_rows(
            db_session,
            user_id=user_id,
            merchant_id=merchant_id,
            store_id=store_id,
            device_id=device_id,
            session_id=session_id,
            payment_method_id=payment_method_id,
            order_ids=[order_id],
            address_ids=[delivery_address_id],
            menu_item_ids=menu_item_ids,
        )


def test_snapshot_haversine_distances(
    db_session: Session, redis_client: redis_lib.Redis[bytes]
) -> None:
    user_id = uuid4()
    merchant_id = uuid4()
    store_id = uuid4()
    device_id = uuid4()
    session_id = uuid4()
    payment_method_id = uuid4()
    delivery_address_id = uuid4()
    billing_address_id = uuid4()

    created_at = datetime(2026, 5, 24, 9, 0, 0, tzinfo=timezone.utc)
    placed_at = datetime(2026, 5, 24, 12, 30, 0, tzinfo=timezone.utc)

    user = _insert_user(db_session, user_id=user_id, created_at=created_at)
    _insert_merchant(db_session, merchant_id=merchant_id)
    store = _insert_store(
        db_session,
        store_id=store_id,
        merchant_id=merchant_id,
        latitude=51.5074,
        longitude=-0.1278,
    )
    menu_item_ids = _insert_menu_items(db_session, store_id=store_id)
    delivery_address = _insert_user_address(
        db_session,
        address_id=delivery_address_id,
        user_id=user_id,
        latitude=51.5000,
        longitude=-0.1000,
    )
    _insert_user_address(
        db_session,
        address_id=billing_address_id,
        user_id=user_id,
        latitude=51.5200,
        longitude=-0.1100,
        address_type="RESIDENTIAL",
    )
    device = _insert_device(db_session, device_id=device_id)
    payment_method = _insert_payment_method(
        db_session,
        payment_method_id=payment_method_id,
        user_id=user_id,
        billing_address_id=billing_address_id,
        unique_users_count=12,
    )
    session = _insert_session(
        db_session,
        session_id=session_id,
        user_id=user_id,
        device_id=device_id,
    )
    cart = _build_haversine_cart(store_id=store_id)
    ip = IPAddress(
        ip_address="9.9.9.9",
        ip_country="GB",
        ip_city="London",
        ip_is_proxy=True,
        city_centroid_lat=51.4800,
        city_centroid_lon=-0.1500,
    )

    try:
        snapshot = build_order_snapshot(
            user=user,
            store=store,
            cart=cart,
            delivery_address=delivery_address,
            payment_method=payment_method,
            device=device,
            session=session,
            ip=ip,
            promo=None,
            placed_at=placed_at,
            db_session=db_session,
            redis_client=redis_client,
        )

        assert 1.9 <= snapshot["delivery_distance_km"] <= 2.3
        assert 3.8 <= snapshot["ip_to_delivery_distance_km"] <= 4.4
        assert 2.1 <= snapshot["billing_to_delivery_distance_km"] <= 2.6
    finally:
        _cleanup_test_rows(
            db_session,
            user_id=user_id,
            merchant_id=merchant_id,
            store_id=store_id,
            device_id=device_id,
            session_id=session_id,
            payment_method_id=payment_method_id,
            address_ids=[delivery_address_id, billing_address_id],
            menu_item_ids=menu_item_ids,
        )


def test_snapshot_vat_per_item(db_session: Session, redis_client: redis_lib.Redis[bytes]) -> None:
    user_id = uuid4()
    merchant_id = uuid4()
    store_id = uuid4()
    device_id = uuid4()
    session_id = uuid4()
    payment_method_id = uuid4()
    delivery_address_id = uuid4()

    user = _insert_user(
        db_session,
        user_id=user_id,
        created_at=datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc),
    )
    placed_at = datetime(2026, 5, 24, 12, 30, 0, tzinfo=timezone.utc)
    _insert_merchant(db_session, merchant_id=merchant_id)
    store = _insert_store(db_session, store_id=store_id, merchant_id=merchant_id, latitude=51.0, longitude=0.0)
    menu_item_ids = _insert_menu_items(db_session, store_id=store_id)
    delivery_address = _insert_user_address(
        db_session,
        address_id=delivery_address_id,
        user_id=user_id,
        latitude=51.0,
        longitude=0.0,
    )
    device = _insert_device(db_session, device_id=device_id)
    payment_method = _insert_payment_method(
        db_session,
        payment_method_id=payment_method_id,
        user_id=user_id,
        billing_address_id=None,
    )
    session = _insert_session(db_session, session_id=session_id, user_id=user_id, device_id=device_id)

    cart = Cart(
        store_id=store_id,
        items=[
            CartItem(
                item_id=uuid4(),
                name="Hot 1",
                qty=1,
                unit_price_pence=1000,
                is_hot_food=True,
            ),
            CartItem(
                item_id=uuid4(),
                name="Hot 2",
                qty=1,
                unit_price_pence=1000,
                is_hot_food=True,
            ),
            CartItem(
                item_id=uuid4(),
                name="Cold",
                qty=1,
                unit_price_pence=500,
                is_hot_food=False,
            ),
        ],
    )
    ip = IPAddress(
        ip_address="8.8.8.8",
        ip_country="GB",
        ip_city="London",
        city_centroid_lat=51.0,
        city_centroid_lon=0.0,
    )

    try:
        snapshot = build_order_snapshot(
            user=user,
            store=store,
            cart=cart,
            delivery_address=delivery_address,
            payment_method=payment_method,
            device=device,
            session=session,
            ip=ip,
            promo=None,
            placed_at=placed_at,
            db_session=db_session,
            redis_client=redis_client,
        )

        assert snapshot["vat_pence"] == 400
    finally:
        _cleanup_test_rows(
            db_session,
            user_id=user_id,
            merchant_id=merchant_id,
            store_id=store_id,
            device_id=device_id,
            session_id=session_id,
            payment_method_id=payment_method_id,
            address_ids=[delivery_address_id],
            menu_item_ids=menu_item_ids,
        )


def test_snapshot_redis_cache_hit(db_session: Session, redis_client: redis_lib.Redis[bytes]) -> None:
    user_id = uuid4()
    merchant_id = uuid4()
    store_id = uuid4()
    device_id = uuid4()
    session_id = uuid4()
    payment_method_id = uuid4()
    delivery_address_id = uuid4()

    user = _insert_user(
        db_session,
        user_id=user_id,
        created_at=datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc),
    )
    placed_at = datetime(2026, 5, 24, 12, 30, 0, tzinfo=timezone.utc)
    _insert_merchant(db_session, merchant_id=merchant_id)
    store = _insert_store(db_session, store_id=store_id, merchant_id=merchant_id, latitude=51.5074, longitude=-0.1278)
    menu_item_ids = _insert_menu_items(db_session, store_id=store_id)
    delivery_address = _insert_user_address(
        db_session,
        address_id=delivery_address_id,
        user_id=user_id,
        latitude=51.5000,
        longitude=-0.1000,
    )
    device = _insert_device(db_session, device_id=device_id)
    payment_method = _insert_payment_method(
        db_session,
        payment_method_id=payment_method_id,
        user_id=user_id,
        billing_address_id=None,
    )
    session = _insert_session(db_session, session_id=session_id, user_id=user_id, device_id=device_id)
    cart = _build_cart(store_id=store_id)
    ip = IPAddress(ip_address="1.1.1.1", ip_country="GB", ip_city="London")

    try:
        first_snapshot = build_order_snapshot(
            user=user,
            store=store,
            cart=cart,
            delivery_address=delivery_address,
            payment_method=payment_method,
            device=device,
            session=session,
            ip=ip,
            promo=None,
            placed_at=placed_at,
            db_session=db_session,
            redis_client=redis_client,
        )

        cache_key = f"user_stats:{user.user_id}"
        cached = redis_client.hgetall(cache_key)
        assert cached == {
            b"total_orders_lifetime": b"0",
            b"total_orders_30d": b"0",
            b"total_spend_lifetime_pence": b"0",
        } or (
            b"total_orders_lifetime" in cached
            and b"total_orders_30d" in cached
            and b"total_spend_lifetime_pence" in cached
        )

        second_snapshot = build_order_snapshot(
            user=user,
            store=store,
            cart=cart,
            delivery_address=delivery_address,
            payment_method=payment_method,
            device=device,
            session=session,
            ip=ip,
            promo=None,
            placed_at=placed_at,
            db_session=db_session,
            redis_client=redis_client,
        )
        assert first_snapshot["user_total_orders_lifetime"] == second_snapshot["user_total_orders_lifetime"]
    finally:
        _cleanup_test_rows(
            db_session,
            user_id=user_id,
            merchant_id=merchant_id,
            store_id=store_id,
            device_id=device_id,
            session_id=session_id,
            payment_method_id=payment_method_id,
            address_ids=[delivery_address_id],
            menu_item_ids=menu_item_ids,
        )
