from __future__ import annotations

import sqlalchemy.types as sqltypes

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    Time,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import RelationshipProperty, relationship

Base = declarative_base()


class CITEXTType(sqltypes.TypeDecorator):
    """Maps to PostgreSQL CITEXT (case-insensitive text). Postgres-side enforcement by migration."""

    impl = sqltypes.String
    cache_ok = True


class User(Base):
    __tablename__ = "users"

    user_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    email = Column(CITEXTType(), unique=True, nullable=False)
    email_verified_at = Column(DateTime(timezone=True))
    phone = Column(String(20))
    phone_verified_at = Column(DateTime(timezone=True))
    password_hash = Column(String(255), nullable=False)
    first_name = Column(String(100))
    last_name = Column(String(100))
    date_of_birth = Column(Date)
    account_status = Column(String(20), nullable=False, server_default=text("'ACTIVE'"))
    risk_tier = Column(String(20), nullable=False, server_default=text("'STANDARD'"))
    referral_source = Column(String(50))
    referred_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"))
    signup_ip = Column(INET)
    signup_device_id = Column(UUID(as_uuid=True))
    signup_country = Column(String(2), server_default=text("'GB'"))
    signup_postcode = Column(String(10))
    signup_user_agent = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    last_login_at = Column(DateTime(timezone=True))

    addresses: RelationshipProperty = relationship("UserAddress", back_populates="user")
    devices: RelationshipProperty = relationship("Device", secondary="user_devices", back_populates="users")

    __table_args__ = (
        CheckConstraint("account_status IN ('ACTIVE','SUSPENDED','BANNED','DELETED')", name="users_status_check"),
        CheckConstraint("risk_tier IN ('TRUSTED','STANDARD','ELEVATED','HIGH_RISK')", name="users_tier_check"),
    )


class UserAddress(Base):
    __tablename__ = "user_addresses"

    address_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    label = Column(String(50))
    address_line_1 = Column(String(255), nullable=False)
    address_line_2 = Column(String(255))
    city = Column(String(100), nullable=False)
    county = Column(String(100))
    postcode = Column(String(10), nullable=False)
    country = Column(String(2), nullable=False, server_default=text("'GB'"))
    latitude = Column(Numeric(10, 7))
    longitude = Column(Numeric(10, 7))
    is_default = Column(Boolean, server_default=text("false"))
    address_type = Column(String(20), server_default=text("'RESIDENTIAL'"))
    delivery_instructions = Column(Text)
    times_used = Column(Integer, server_default=text("0"))
    first_used_at = Column(DateTime(timezone=True))
    last_used_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    user: RelationshipProperty = relationship("User", back_populates="addresses")

    __table_args__ = (
        CheckConstraint(
            "address_type IN ('RESIDENTIAL','COMMERCIAL','HOTEL','STUDENT_HALL','OTHER')",
            name="addresses_type_check",
        ),
    )


class Device(Base):
    __tablename__ = "devices"

    device_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    device_fingerprint = Column(String(255), unique=True, nullable=False)
    device_type = Column(String(20), nullable=False)
    platform = Column(String(20))
    os_version = Column(String(50))
    app_version = Column(String(50))
    browser_name = Column(String(50))
    browser_version = Column(String(50))
    screen_resolution = Column(String(20))
    timezone = Column(String(50))
    language = Column(String(20))
    is_rooted_jailbroken = Column(Boolean)
    is_emulator = Column(Boolean)
    is_vpn_detected = Column(Boolean)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    unique_users_count = Column(Integer, server_default=text("1"))
    risk_score = Column(Numeric(5, 4), server_default=text("0.0"))

    users: RelationshipProperty = relationship("User", secondary="user_devices", back_populates="devices")

    __table_args__ = (
        CheckConstraint(
            "device_type IN ('MOBILE_APP','MOBILE_WEB','DESKTOP_WEB','TABLET')",
            name="devices_type_check",
        ),
    )


class UserDevice(Base):
    __tablename__ = "user_devices"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.device_id"), nullable=False)
    first_used_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    last_used_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    session_count = Column(Integer, server_default=text("1"))
    is_trusted = Column(Boolean, server_default=text("false"))

    __table_args__ = (PrimaryKeyConstraint("user_id", "device_id"),)


class Session(Base):
    __tablename__ = "sessions"

    session_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"))
    device_id = Column(UUID(as_uuid=True), ForeignKey("devices.device_id"), nullable=False)
    ip_address = Column(INET, nullable=False)
    ip_country = Column(String(2))
    ip_region = Column(String(100))
    ip_city = Column(String(100))
    ip_isp = Column(String(255))
    ip_is_proxy = Column(Boolean, server_default=text("false"))
    ip_is_tor = Column(Boolean, server_default=text("false"))
    ip_is_hosting = Column(Boolean, server_default=text("false"))
    user_agent = Column(Text)
    referrer = Column(Text)
    auth_method = Column(String(20))
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    ended_at = Column(DateTime(timezone=True))
    is_active = Column(Boolean, server_default=text("true"))


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    payment_method_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False)
    payment_type = Column(String(20), nullable=False)
    card_token = Column(String(255))
    card_bin = Column(String(8))
    card_last_four = Column(String(4))
    card_brand = Column(String(20))
    card_funding_type = Column(String(20))
    card_issuer_country = Column(String(2))
    card_issuer_bank = Column(String(255))
    is_digital_native_bank = Column(Boolean, server_default=text("false"))
    card_exp_month = Column(SmallInteger)
    card_exp_year = Column(SmallInteger)
    billing_address_id = Column(UUID(as_uuid=True), ForeignKey("user_addresses.address_id"))
    avs_result = Column(String(10))
    cvv_result = Column(String(10))
    is_default = Column(Boolean, server_default=text("false"))
    status = Column(String(20), server_default=text("'ACTIVE'"))
    times_used = Column(Integer, server_default=text("0"))
    unique_users_count = Column(Integer, server_default=text("1"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    last_used_at = Column(DateTime(timezone=True))


class Merchant(Base):
    __tablename__ = "merchants"

    merchant_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    legal_name = Column(String(255), nullable=False)
    brand_name = Column(String(255), nullable=False)
    merchant_category = Column(String(50))
    companies_house_no = Column(String(20))
    vat_number = Column(String(20))
    status = Column(String(20), server_default=text("'ACTIVE'"))
    onboarded_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    risk_tier = Column(String(20), server_default=text("'STANDARD'"))

    stores: RelationshipProperty = relationship("Store", back_populates="merchant")


class Store(Base):
    __tablename__ = "stores"

    store_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    merchant_id = Column(UUID(as_uuid=True), ForeignKey("merchants.merchant_id"), nullable=False)
    store_name = Column(String(255), nullable=False)
    store_code = Column(String(50))
    cuisine_types = Column(ARRAY(String(255)))
    price_tier = Column(SmallInteger)
    address_line_1 = Column(String(255), nullable=False)
    address_line_2 = Column(String(255))
    city = Column(String(100), nullable=False)
    county = Column(String(100))
    postcode = Column(String(10), nullable=False)
    country = Column(String(2), nullable=False, server_default=text("'GB'"))
    latitude = Column(Numeric(10, 7), nullable=False)
    longitude = Column(Numeric(10, 7), nullable=False)
    timezone = Column(String(50), nullable=False, server_default=text("'Europe/London'"))
    phone = Column(String(20))
    pos_system = Column(String(50))
    pos_integration_type = Column(String(20))
    delivery_radius_km = Column(Numeric(5, 2))
    avg_prep_time_min = Column(SmallInteger)
    accepts_cash = Column(Boolean, server_default=text("false"))
    accepts_in_store = Column(Boolean, server_default=text("true"))
    accepts_delivery = Column(Boolean, server_default=text("true"))
    accepts_pickup = Column(Boolean, server_default=text("true"))
    is_active = Column(Boolean, server_default=text("true"))
    is_verified = Column(Boolean, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    risk_score = Column(Numeric(5, 4), server_default=text("0.0"))

    merchant: RelationshipProperty = relationship("Merchant", back_populates="stores")
    menu_items: RelationshipProperty = relationship("MenuItem", back_populates="store")
    hours: RelationshipProperty = relationship("StoreHour", back_populates="store")


class StoreHour(Base):
    __tablename__ = "store_hours"

    store_id = Column(UUID(as_uuid=True), ForeignKey("stores.store_id"), nullable=False)
    day_of_week = Column(SmallInteger, nullable=False)
    open_time = Column(Time, nullable=False)
    close_time = Column(Time, nullable=False)

    store: RelationshipProperty = relationship("Store", back_populates="hours")

    __table_args__ = (PrimaryKeyConstraint("store_id", "day_of_week", "open_time"),)


class MenuItem(Base):
    __tablename__ = "menu_items"

    item_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    store_id = Column(UUID(as_uuid=True), ForeignKey("stores.store_id"), nullable=False)
    item_name = Column(String(255), nullable=False)
    category = Column(String(100))
    price_pence = Column(BigInteger, nullable=False)
    is_hot_food = Column(Boolean, nullable=False, server_default=text("true"))
    is_available = Column(Boolean, server_default=text("true"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    store: RelationshipProperty = relationship("Store", back_populates="menu_items")


class Driver(Base):
    __tablename__ = "drivers"

    driver_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    first_name = Column(String(100))
    last_name = Column(String(100))
    email = Column(CITEXTType(), unique=True)
    phone = Column(String(20))
    vehicle_type = Column(String(20))
    licence_plate = Column(String(20))
    onboarded_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    status = Column(String(20), server_default=text("'ACTIVE'"))
    rating = Column(Numeric(3, 2))
    completed_deliveries = Column(Integer, server_default=text("0"))
    home_city = Column(String(100))
    risk_score = Column(Numeric(5, 4), server_default=text("0.0"))


class Promotion(Base):
    __tablename__ = "promotions"

    promo_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    promo_code = Column(String(50), unique=True)
    promo_type = Column(String(30))
    discount_amount_pence = Column(BigInteger)
    discount_percent = Column(Numeric(5, 2))
    min_order_pence = Column(BigInteger)
    max_redemptions_per_user = Column(Integer)
    valid_from = Column(DateTime(timezone=True))
    valid_until = Column(DateTime(timezone=True))
    is_targeted = Column(Boolean, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))


class Order(Base):
    """Partitioned by `placed_at`; composite PK (`order_id`, `placed_at`)."""

    order_id = Column(UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()"))
    order_number = Column(String(20), nullable=False)
    order_status = Column(String(30), nullable=False)
    order_channel = Column(String(30), nullable=False)
    order_type = Column(String(20), nullable=False)
    placed_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True))
    ready_at = Column(DateTime(timezone=True))
    picked_up_at = Column(DateTime(timezone=True))
    delivered_at = Column(DateTime(timezone=True))
    cancelled_at = Column(DateTime(timezone=True))
    cancellation_reason = Column(String(100))
    cancelled_by = Column(String(20))
    terminal_state_reached_at = Column(DateTime(timezone=True))
    user_id = Column(UUID(as_uuid=True), nullable=False)
    user_account_age_days = Column(Integer, nullable=False)
    user_total_orders_lifetime = Column(Integer, nullable=False)
    user_total_orders_30d = Column(Integer, nullable=False)
    user_total_spend_lifetime_pence = Column(BigInteger, nullable=False)
    user_avg_order_value_pence = Column(BigInteger)
    user_chargebacks_lifetime = Column(Integer, server_default=text("0"))
    user_refunds_lifetime = Column(Integer, server_default=text("0"))
    user_email = Column(CITEXTType(), nullable=False)
    user_email_domain = Column(String(100), nullable=False)
    user_phone = Column(String(20))
    user_risk_tier_at_order = Column(String(20))
    is_guest_checkout = Column(Boolean, server_default=text("false"))
    store_id = Column(UUID(as_uuid=True), nullable=False)
    merchant_id = Column(UUID(as_uuid=True), nullable=False)
    store_city = Column(String(100), nullable=False)
    store_country = Column(String(2), nullable=False, server_default=text("'GB'"))
    store_latitude = Column(Numeric(10, 7), nullable=False)
    store_longitude = Column(Numeric(10, 7), nullable=False)
    delivery_address_id = Column(UUID(as_uuid=True))
    delivery_address_snapshot = Column(JSONB)
    delivery_latitude = Column(Numeric(10, 7))
    delivery_longitude = Column(Numeric(10, 7))
    delivery_distance_km = Column(Numeric(6, 2))
    delivery_address_type = Column(String(20))
    is_new_delivery_address = Column(Boolean)
    delivery_address_use_count = Column(Integer)
    driver_id = Column(UUID(as_uuid=True))
    item_count = Column(Integer, nullable=False)
    unique_item_count = Column(Integer, nullable=False)
    subtotal_pence = Column(BigInteger, nullable=False)
    vat_pence = Column(BigInteger, nullable=False, server_default=text("0"))
    delivery_fee_pence = Column(BigInteger, nullable=False, server_default=text("0"))
    service_fee_pence = Column(BigInteger, nullable=False, server_default=text("0"))
    tip_pence = Column(BigInteger, nullable=False, server_default=text("0"))
    discount_pence = Column(BigInteger, nullable=False, server_default=text("0"))
    total_pence = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, server_default=text("'GBP'"))
    promo_id = Column(UUID(as_uuid=True))
    promo_code = Column(String(50))
    is_first_order_for_user = Column(Boolean, server_default=text("false"))
    is_new_user_promo = Column(Boolean, server_default=text("false"))
    payment_method_id = Column(UUID(as_uuid=True))
    payment_type = Column(String(20), nullable=False)
    card_bin = Column(String(8))
    card_last_four = Column(String(4))
    card_brand = Column(String(20))
    card_funding_type = Column(String(20))
    card_issuer_country = Column(String(2))
    is_digital_native_bank = Column(Boolean)
    is_new_payment_method = Column(Boolean)
    payment_authorized_at = Column(DateTime(timezone=True))
    payment_captured_at = Column(DateTime(timezone=True))
    payment_gateway = Column(String(50))
    payment_gateway_txn_id = Column(String(255))
    avs_result = Column(String(10))
    cvv_result = Column(String(10))
    threeds_status = Column(String(20))
    session_id = Column(UUID(as_uuid=True))
    device_id = Column(UUID(as_uuid=True))
    device_type = Column(String(20))
    platform = Column(String(20))
    os_version = Column(String(50))
    app_version = Column(String(50))
    browser_name = Column(String(50))
    browser_version = Column(String(50))
    ip_address = Column(INET)
    ip_country = Column(String(2))
    ip_city = Column(String(100))
    ip_is_proxy = Column(Boolean)
    ip_is_vpn = Column(Boolean)
    ip_is_tor = Column(Boolean)
    ip_is_hosting = Column(Boolean)
    device_user_count = Column(Integer)
    payment_user_count = Column(Integer)
    ip_to_delivery_distance_km = Column(Numeric(8, 2))
    billing_to_delivery_distance_km = Column(Numeric(8, 2))
    time_to_checkout_seconds = Column(Integer)
    cart_modifications_count = Column(Integer)
    fraud_score = Column(Numeric(5, 4))
    fraud_score_version = Column(String(50))
    fraud_decision = Column(String(20))
    fraud_rules_triggered = Column(ARRAY(String(100)))
    fraud_reviewed_at = Column(DateTime(timezone=True))
    fraud_reviewed_by = Column(String(100))
    fraud_outcome = Column(String(20))
    fraud_outcome_set_at = Column(DateTime(timezone=True))
    chargeback_received_at = Column(DateTime(timezone=True))
    chargeback_amount_pence = Column(BigInteger)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    __tablename__ = "orders"

    __table_args__ = (
        PrimaryKeyConstraint("order_id", "placed_at"),
    )


class OrdersArchive(Base):
    """Archive table. Same schema as `orders`/`order_items`/`order_events`. Rows moved here by the archiver daemon after 48h in terminal state."""

    order_id = Column(UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()"))
    order_number = Column(String(20), nullable=False)
    order_status = Column(String(30), nullable=False)
    order_channel = Column(String(30), nullable=False)
    order_type = Column(String(20), nullable=False)
    placed_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True))
    ready_at = Column(DateTime(timezone=True))
    picked_up_at = Column(DateTime(timezone=True))
    delivered_at = Column(DateTime(timezone=True))
    cancelled_at = Column(DateTime(timezone=True))
    cancellation_reason = Column(String(100))
    cancelled_by = Column(String(20))
    terminal_state_reached_at = Column(DateTime(timezone=True))
    user_id = Column(UUID(as_uuid=True), nullable=False)
    user_account_age_days = Column(Integer, nullable=False)
    user_total_orders_lifetime = Column(Integer, nullable=False)
    user_total_orders_30d = Column(Integer, nullable=False)
    user_total_spend_lifetime_pence = Column(BigInteger, nullable=False)
    user_avg_order_value_pence = Column(BigInteger)
    user_chargebacks_lifetime = Column(Integer, server_default=text("0"))
    user_refunds_lifetime = Column(Integer, server_default=text("0"))
    user_email = Column(CITEXTType(), nullable=False)
    user_email_domain = Column(String(100), nullable=False)
    user_phone = Column(String(20))
    user_risk_tier_at_order = Column(String(20))
    is_guest_checkout = Column(Boolean, server_default=text("false"))
    store_id = Column(UUID(as_uuid=True), nullable=False)
    merchant_id = Column(UUID(as_uuid=True), nullable=False)
    store_city = Column(String(100), nullable=False)
    store_country = Column(String(2), nullable=False, server_default=text("'GB'"))
    store_latitude = Column(Numeric(10, 7), nullable=False)
    store_longitude = Column(Numeric(10, 7), nullable=False)
    delivery_address_id = Column(UUID(as_uuid=True))
    delivery_address_snapshot = Column(JSONB)
    delivery_latitude = Column(Numeric(10, 7))
    delivery_longitude = Column(Numeric(10, 7))
    delivery_distance_km = Column(Numeric(6, 2))
    delivery_address_type = Column(String(20))
    is_new_delivery_address = Column(Boolean)
    delivery_address_use_count = Column(Integer)
    driver_id = Column(UUID(as_uuid=True))
    item_count = Column(Integer, nullable=False)
    unique_item_count = Column(Integer, nullable=False)
    subtotal_pence = Column(BigInteger, nullable=False)
    vat_pence = Column(BigInteger, nullable=False, server_default=text("0"))
    delivery_fee_pence = Column(BigInteger, nullable=False, server_default=text("0"))
    service_fee_pence = Column(BigInteger, nullable=False, server_default=text("0"))
    tip_pence = Column(BigInteger, nullable=False, server_default=text("0"))
    discount_pence = Column(BigInteger, nullable=False, server_default=text("0"))
    total_pence = Column(BigInteger, nullable=False)
    currency = Column(String(3), nullable=False, server_default=text("'GBP'"))
    promo_id = Column(UUID(as_uuid=True))
    promo_code = Column(String(50))
    is_first_order_for_user = Column(Boolean, server_default=text("false"))
    is_new_user_promo = Column(Boolean, server_default=text("false"))
    payment_method_id = Column(UUID(as_uuid=True))
    payment_type = Column(String(20), nullable=False)
    card_bin = Column(String(8))
    card_last_four = Column(String(4))
    card_brand = Column(String(20))
    card_funding_type = Column(String(20))
    card_issuer_country = Column(String(2))
    is_digital_native_bank = Column(Boolean)
    is_new_payment_method = Column(Boolean)
    payment_authorized_at = Column(DateTime(timezone=True))
    payment_captured_at = Column(DateTime(timezone=True))
    payment_gateway = Column(String(50))
    payment_gateway_txn_id = Column(String(255))
    avs_result = Column(String(10))
    cvv_result = Column(String(10))
    threeds_status = Column(String(20))
    session_id = Column(UUID(as_uuid=True))
    device_id = Column(UUID(as_uuid=True))
    device_type = Column(String(20))
    platform = Column(String(20))
    os_version = Column(String(50))
    app_version = Column(String(50))
    browser_name = Column(String(50))
    browser_version = Column(String(50))
    ip_address = Column(INET)
    ip_country = Column(String(2))
    ip_city = Column(String(100))
    ip_is_proxy = Column(Boolean)
    ip_is_vpn = Column(Boolean)
    ip_is_tor = Column(Boolean)
    ip_is_hosting = Column(Boolean)
    device_user_count = Column(Integer)
    payment_user_count = Column(Integer)
    ip_to_delivery_distance_km = Column(Numeric(8, 2))
    billing_to_delivery_distance_km = Column(Numeric(8, 2))
    time_to_checkout_seconds = Column(Integer)
    cart_modifications_count = Column(Integer)
    fraud_score = Column(Numeric(5, 4))
    fraud_score_version = Column(String(50))
    fraud_decision = Column(String(20))
    fraud_rules_triggered = Column(ARRAY(String(100)))
    fraud_reviewed_at = Column(DateTime(timezone=True))
    fraud_reviewed_by = Column(String(100))
    fraud_outcome = Column(String(20))
    fraud_outcome_set_at = Column(DateTime(timezone=True))
    chargeback_received_at = Column(DateTime(timezone=True))
    chargeback_amount_pence = Column(BigInteger)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    __tablename__ = "orders_archive"

    __table_args__ = (
        PrimaryKeyConstraint("order_id", "placed_at"),
    )


class OrderItem(Base):
    """Partitioned by `order_placed_at`; composite PK (`order_item_id`, `order_placed_at`)."""

    order_item_id = Column(UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()"))
    order_id = Column(UUID(as_uuid=True), nullable=False)
    order_placed_at = Column(DateTime(timezone=True), nullable=False)
    item_id = Column(UUID(as_uuid=True), ForeignKey("menu_items.item_id"))
    item_name_snapshot = Column(String(255), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price_pence = Column(BigInteger, nullable=False)
    line_total_pence = Column(BigInteger, nullable=False)
    is_hot_food = Column(Boolean, nullable=False, server_default=text("true"))
    modifiers = Column(JSONB)
    special_instructions = Column(Text)

    __tablename__ = "order_items"

    __table_args__ = (
        PrimaryKeyConstraint("order_item_id", "order_placed_at"),
    )


class OrderItemsArchive(Base):
    """Archive table. Same schema as `orders`/`order_items`/`order_events`. Rows moved here by the archiver daemon after 48h in terminal state."""

    order_item_id = Column(UUID(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()"))
    order_id = Column(UUID(as_uuid=True), nullable=False)
    order_placed_at = Column(DateTime(timezone=True), nullable=False)
    item_id = Column(UUID(as_uuid=True), ForeignKey("menu_items.item_id"))
    item_name_snapshot = Column(String(255), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price_pence = Column(BigInteger, nullable=False)
    line_total_pence = Column(BigInteger, nullable=False)
    is_hot_food = Column(Boolean, nullable=False, server_default=text("true"))
    modifiers = Column(JSONB)
    special_instructions = Column(Text)

    __tablename__ = "order_items_archive"

    __table_args__ = (
        PrimaryKeyConstraint("order_item_id", "order_placed_at"),
    )


class OrderEvent(Base):
    """Partitioned by `order_placed_at`; composite PK (`event_id`, `order_placed_at`)."""

    event_id = Column(BigInteger, nullable=False, autoincrement=True)
    order_id = Column(UUID(as_uuid=True), nullable=False)
    order_placed_at = Column(DateTime(timezone=True), nullable=False)
    event_type = Column(String(50), nullable=False)
    event_data = Column(JSONB)
    actor_type = Column(String(20))
    actor_id = Column(UUID(as_uuid=True))
    ip_address = Column(INET)
    device_id = Column(UUID(as_uuid=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    __tablename__ = "order_events"

    __table_args__ = (
        PrimaryKeyConstraint("event_id", "order_placed_at"),
    )


class OrderEventsArchive(Base):
    """Archive table. Same schema as `orders`/`order_items`/`order_events`. Rows moved here by the archiver daemon after 48h in terminal state."""

    event_id = Column(BigInteger, nullable=False, autoincrement=True)
    order_id = Column(UUID(as_uuid=True), nullable=False)
    order_placed_at = Column(DateTime(timezone=True), nullable=False)
    event_type = Column(String(50), nullable=False)
    event_data = Column(JSONB)
    actor_type = Column(String(20))
    actor_id = Column(UUID(as_uuid=True))
    ip_address = Column(INET)
    device_id = Column(UUID(as_uuid=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    __tablename__ = "order_events_archive"

    __table_args__ = (
        PrimaryKeyConstraint("event_id", "order_placed_at"),
    )


class FraudDecision(Base):
    __tablename__ = "fraud_decisions"

    decision_id = Column(BigInteger, primary_key=True, autoincrement=True)
    order_id = Column(UUID(as_uuid=True), nullable=False)
    order_placed_at = Column(DateTime(timezone=True), nullable=False)
    model_version = Column(String(50), nullable=False)
    score = Column(Numeric(5, 4), nullable=False)
    decision = Column(String(20), nullable=False)
    features_snapshot = Column(JSONB, nullable=False)
    rules_triggered = Column(ARRAY(String(100)))
    latency_ms = Column(Integer)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))


class Chargeback(Base):
    __tablename__ = "chargebacks"

    chargeback_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    order_id = Column(UUID(as_uuid=True), nullable=False)
    order_placed_at = Column(DateTime(timezone=True), nullable=False)
    reason_code = Column(String(20))
    reason_category = Column(String(50))
    amount_pence = Column(BigInteger, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    resolved_at = Column(DateTime(timezone=True))
    resolution = Column(String(20))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
