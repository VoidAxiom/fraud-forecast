"""Create the initial fraud-forecast schema."""

from alembic import op


# revision identifiers, used by Alembic.
revision = '001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE users (
    user_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email                CITEXT UNIQUE NOT NULL,
    email_verified_at    TIMESTAMPTZ,
    phone                VARCHAR(20),                    -- +44 format
    phone_verified_at    TIMESTAMPTZ,
    password_hash        VARCHAR(255) NOT NULL,          -- store bcrypt hash; for sim use a placeholder
    first_name           VARCHAR(100),
    last_name            VARCHAR(100),
    date_of_birth        DATE,
    account_status       VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    risk_tier            VARCHAR(20) NOT NULL DEFAULT 'STANDARD',
    referral_source      VARCHAR(50),                    -- ORGANIC, GOOGLE_ADS, FB_ADS, REFERRAL, etc.
    referred_by_user_id  UUID REFERENCES users(user_id),
    signup_ip            INET,
    signup_device_id     UUID,                           -- soft FK; populated after devices exist
    signup_country       CHAR(2) DEFAULT 'GB',
    signup_postcode      VARCHAR(10),
    signup_user_agent    TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at        TIMESTAMPTZ,
    CONSTRAINT users_status_check CHECK (account_status IN ('ACTIVE','SUSPENDED','BANNED','DELETED')),
    CONSTRAINT users_tier_check CHECK (risk_tier IN ('TRUSTED','STANDARD','ELEVATED','HIGH_RISK'))
);
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_phone ON users(phone);
CREATE INDEX idx_users_signup_ip ON users(signup_ip);
CREATE INDEX idx_users_signup_device ON users(signup_device_id);
CREATE INDEX idx_users_referred_by ON users(referred_by_user_id);
CREATE INDEX idx_users_created ON users(created_at);
"""
    )
    op.execute(
        """
CREATE TABLE user_addresses (
    address_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL REFERENCES users(user_id),
    label                   VARCHAR(50),                 -- 'Home', 'Work', 'Mum'
    address_line_1          VARCHAR(255) NOT NULL,       -- e.g. "10 Downing Street"
    address_line_2          VARCHAR(255),                -- e.g. "Flat 2"
    city                    VARCHAR(100) NOT NULL,
    county                  VARCHAR(100),                -- e.g. "Greater London", "West Midlands"
    postcode                VARCHAR(10) NOT NULL,        -- UK format
    country                 CHAR(2) NOT NULL DEFAULT 'GB',
    latitude                DECIMAL(10, 7),
    longitude               DECIMAL(10, 7),
    is_default              BOOLEAN DEFAULT FALSE,
    address_type            VARCHAR(20) DEFAULT 'RESIDENTIAL',
    delivery_instructions   TEXT,
    times_used              INTEGER DEFAULT 0,
    first_used_at           TIMESTAMPTZ,
    last_used_at            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT addresses_type_check CHECK (address_type IN ('RESIDENTIAL','COMMERCIAL','HOTEL','STUDENT_HALL','OTHER'))
);
CREATE INDEX idx_addresses_user ON user_addresses(user_id);
CREATE INDEX idx_addresses_geo ON user_addresses(latitude, longitude);
CREATE INDEX idx_addresses_postcode ON user_addresses(postcode);
"""
    )
    op.execute(
        """
CREATE TABLE devices (
    device_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_fingerprint   VARCHAR(255) UNIQUE NOT NULL,
    device_type          VARCHAR(20) NOT NULL,
    platform             VARCHAR(20),                    -- iOS, Android, Windows, macOS, Linux
    os_version           VARCHAR(50),
    app_version          VARCHAR(50),
    browser_name         VARCHAR(50),
    browser_version      VARCHAR(50),
    screen_resolution    VARCHAR(20),
    timezone             VARCHAR(50),
    language             VARCHAR(20),
    is_rooted_jailbroken BOOLEAN,
    is_emulator          BOOLEAN,
    is_vpn_detected      BOOLEAN,
    first_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    unique_users_count   INTEGER DEFAULT 1,
    risk_score           DECIMAL(5,4) DEFAULT 0.0,
    CONSTRAINT devices_type_check CHECK (device_type IN ('MOBILE_APP','MOBILE_WEB','DESKTOP_WEB','TABLET'))
);
CREATE INDEX idx_devices_fingerprint ON devices(device_fingerprint);
"""
    )
    op.execute(
        """
CREATE TABLE user_devices (
    user_id        UUID NOT NULL REFERENCES users(user_id),
    device_id      UUID NOT NULL REFERENCES devices(device_id),
    first_used_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_count  INTEGER DEFAULT 1,
    is_trusted     BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (user_id, device_id)
);
"""
    )
    op.execute(
        """
CREATE TABLE sessions (
    session_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(user_id),
    device_id       UUID NOT NULL REFERENCES devices(device_id),
    ip_address      INET NOT NULL,
    ip_country      CHAR(2),
    ip_region       VARCHAR(100),
    ip_city         VARCHAR(100),
    ip_isp          VARCHAR(255),
    ip_is_proxy     BOOLEAN DEFAULT FALSE,
    ip_is_tor       BOOLEAN DEFAULT FALSE,
    ip_is_hosting   BOOLEAN DEFAULT FALSE,
    user_agent      TEXT,
    referrer        TEXT,
    auth_method     VARCHAR(20),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    is_active       BOOLEAN DEFAULT TRUE
);
CREATE INDEX idx_sessions_user ON sessions(user_id);
CREATE INDEX idx_sessions_device ON sessions(device_id);
CREATE INDEX idx_sessions_ip ON sessions(ip_address);
CREATE INDEX idx_sessions_started ON sessions(started_at);
"""
    )
    op.execute(
        """
CREATE TABLE payment_methods (
    payment_method_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                  UUID NOT NULL REFERENCES users(user_id),
    payment_type             VARCHAR(20) NOT NULL,       -- CREDIT_CARD, DEBIT_CARD, PAYPAL, APPLE_PAY, GOOGLE_PAY, GIFT_CARD, ACCOUNT_CREDIT
    card_token               VARCHAR(255),
    card_bin                 VARCHAR(8),
    card_last_four           VARCHAR(4),
    card_brand               VARCHAR(20),                -- VISA, MASTERCARD, AMEX, MAESTRO, OTHER
    card_funding_type        VARCHAR(20),                -- CREDIT, DEBIT, PREPAID
    card_issuer_country      CHAR(2),
    card_issuer_bank         VARCHAR(255),               -- e.g. 'MONZO BANK LIMITED'
    is_digital_native_bank   BOOLEAN DEFAULT FALSE,      -- Monzo, Revolut, Starling, Chase UK
    card_exp_month           SMALLINT,
    card_exp_year            SMALLINT,
    billing_address_id       UUID REFERENCES user_addresses(address_id),
    avs_result               VARCHAR(10),                -- MATCH, PARTIAL, NO_MATCH, UNAVAILABLE
    cvv_result               VARCHAR(10),
    is_default               BOOLEAN DEFAULT FALSE,
    status                   VARCHAR(20) DEFAULT 'ACTIVE',
    times_used               INTEGER DEFAULT 0,
    unique_users_count       INTEGER DEFAULT 1,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at             TIMESTAMPTZ
);
CREATE INDEX idx_payment_user ON payment_methods(user_id);
CREATE INDEX idx_payment_bin ON payment_methods(card_bin);
CREATE INDEX idx_payment_token ON payment_methods(card_token);
"""
    )
    op.execute(
        """
CREATE TABLE merchants (
    merchant_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    legal_name         VARCHAR(255) NOT NULL,
    brand_name         VARCHAR(255) NOT NULL,
    merchant_category  VARCHAR(50),                      -- QSR, CASUAL_DINING, FINE_DINING, GROCERY, CONVENIENCE, DARK_KITCHEN
    companies_house_no VARCHAR(20),                      -- UK Companies House number
    vat_number         VARCHAR(20),                      -- UK VAT registration
    status             VARCHAR(20) DEFAULT 'ACTIVE',
    onboarded_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    risk_tier          VARCHAR(20) DEFAULT 'STANDARD'
);
"""
    )
    op.execute(
        """
CREATE TABLE stores (
    store_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id           UUID NOT NULL REFERENCES merchants(merchant_id),
    store_name            VARCHAR(255) NOT NULL,
    store_code            VARCHAR(50),
    cuisine_types         VARCHAR(255)[],
    price_tier            SMALLINT,                      -- 1-4 (£ to ££££)
    address_line_1        VARCHAR(255) NOT NULL,
    address_line_2        VARCHAR(255),
    city                  VARCHAR(100) NOT NULL,
    county                VARCHAR(100),
    postcode              VARCHAR(10) NOT NULL,
    country               CHAR(2) NOT NULL DEFAULT 'GB',
    latitude              DECIMAL(10,7) NOT NULL,
    longitude             DECIMAL(10,7) NOT NULL,
    timezone              VARCHAR(50) NOT NULL DEFAULT 'Europe/London',
    phone                 VARCHAR(20),
    pos_system            VARCHAR(50),
    pos_integration_type  VARCHAR(20),                   -- API, TABLET, EMAIL
    delivery_radius_km    DECIMAL(5,2),
    avg_prep_time_min     SMALLINT,
    accepts_cash          BOOLEAN DEFAULT FALSE,         -- UK most are card-only on platform
    accepts_in_store      BOOLEAN DEFAULT TRUE,
    accepts_delivery      BOOLEAN DEFAULT TRUE,
    accepts_pickup        BOOLEAN DEFAULT TRUE,
    is_active             BOOLEAN DEFAULT TRUE,
    is_verified           BOOLEAN DEFAULT FALSE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    risk_score            DECIMAL(5,4) DEFAULT 0.0
);
CREATE INDEX idx_stores_merchant ON stores(merchant_id);
CREATE INDEX idx_stores_geo ON stores(latitude, longitude);
CREATE INDEX idx_stores_city ON stores(city);
CREATE INDEX idx_stores_postcode ON stores(postcode);
"""
    )
    op.execute(
        """
CREATE TABLE store_hours (
    store_id     UUID NOT NULL REFERENCES stores(store_id),
    day_of_week  SMALLINT NOT NULL,                       -- 0=Sun, 6=Sat
    open_time    TIME NOT NULL,
    close_time   TIME NOT NULL,
    PRIMARY KEY (store_id, day_of_week, open_time)
);
"""
    )
    op.execute(
        """
CREATE TABLE menu_items (
    item_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id         UUID NOT NULL REFERENCES stores(store_id),
    item_name        VARCHAR(255) NOT NULL,
    category         VARCHAR(100),
    price_pence      BIGINT NOT NULL,                    -- GBP in pence
    is_hot_food      BOOLEAN NOT NULL DEFAULT TRUE,      -- VAT-relevant
    is_available     BOOLEAN DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_menu_store ON menu_items(store_id);
"""
    )
    op.execute(
        """
CREATE TABLE drivers (
    driver_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    first_name           VARCHAR(100),
    last_name            VARCHAR(100),
    email                CITEXT UNIQUE,
    phone                VARCHAR(20),
    vehicle_type         VARCHAR(20),                    -- CAR, BIKE, SCOOTER, EBIKE, WALK
    licence_plate        VARCHAR(20),                    -- UK spelling
    onboarded_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status               VARCHAR(20) DEFAULT 'ACTIVE',
    rating               DECIMAL(3,2),
    completed_deliveries INTEGER DEFAULT 0,
    home_city            VARCHAR(100),
    risk_score           DECIMAL(5,4) DEFAULT 0.0
);
"""
    )
    op.execute(
        """
CREATE TABLE promotions (
    promo_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    promo_code               VARCHAR(50) UNIQUE,
    promo_type               VARCHAR(30),                -- NEW_USER, REFERRAL, PERCENT_OFF, POUND_OFF, FREE_DELIVERY, BOGO
    discount_amount_pence    BIGINT,
    discount_percent         DECIMAL(5,2),
    min_order_pence          BIGINT,
    max_redemptions_per_user INTEGER,
    valid_from               TIMESTAMPTZ,
    valid_until              TIMESTAMPTZ,
    is_targeted              BOOLEAN DEFAULT FALSE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
    )
    op.execute(
        """
CREATE TABLE orders (
    -- Identifiers
    order_id                          UUID NOT NULL DEFAULT gen_random_uuid(),
    order_number                      VARCHAR(20) NOT NULL,    -- e.g. 'JE-2026-A8K3L9'
    -- Order metadata
    order_status                      VARCHAR(30) NOT NULL,
    order_channel                     VARCHAR(30) NOT NULL,    -- WEB, IOS_APP, ANDROID_APP, IN_STORE_POS, PHONE
    order_type                        VARCHAR(20) NOT NULL,    -- DELIVERY, PICKUP, DINE_IN
    placed_at                         TIMESTAMPTZ NOT NULL,
    accepted_at                       TIMESTAMPTZ,
    ready_at                          TIMESTAMPTZ,
    picked_up_at                      TIMESTAMPTZ,
    delivered_at                      TIMESTAMPTZ,
    cancelled_at                      TIMESTAMPTZ,
    cancellation_reason               VARCHAR(100),
    cancelled_by                      VARCHAR(20),             -- USER, MERCHANT, DRIVER, SYSTEM, FRAUD
    terminal_state_reached_at         TIMESTAMPTZ,
    -- Customer snapshot
    user_id                           UUID NOT NULL,
    user_account_age_days             INTEGER NOT NULL,
    user_total_orders_lifetime        INTEGER NOT NULL,
    user_total_orders_30d             INTEGER NOT NULL,
    user_total_spend_lifetime_pence   BIGINT NOT NULL,
    user_avg_order_value_pence        BIGINT,
    user_chargebacks_lifetime         INTEGER DEFAULT 0,
    user_refunds_lifetime             INTEGER DEFAULT 0,
    user_email                        CITEXT NOT NULL,
    user_email_domain                 VARCHAR(100) NOT NULL,
    user_phone                        VARCHAR(20),
    user_risk_tier_at_order           VARCHAR(20),
    is_guest_checkout                 BOOLEAN DEFAULT FALSE,
    -- Store
    store_id                          UUID NOT NULL,
    merchant_id                       UUID NOT NULL,
    store_city                        VARCHAR(100) NOT NULL,
    store_country                     CHAR(2) NOT NULL DEFAULT 'GB',
    store_latitude                    DECIMAL(10,7) NOT NULL,
    store_longitude                   DECIMAL(10,7) NOT NULL,
    -- Delivery
    delivery_address_id               UUID,
    delivery_address_snapshot         JSONB,
    delivery_latitude                 DECIMAL(10,7),
    delivery_longitude                DECIMAL(10,7),
    delivery_distance_km              DECIMAL(6,2),
    delivery_address_type             VARCHAR(20),
    is_new_delivery_address           BOOLEAN,
    delivery_address_use_count        INTEGER,
    driver_id                         UUID,
    -- Items & pricing (all in pence)
    item_count                        INTEGER NOT NULL,
    unique_item_count                 INTEGER NOT NULL,
    subtotal_pence                    BIGINT NOT NULL,
    vat_pence                         BIGINT NOT NULL DEFAULT 0,   -- 20% UK VAT (hot food + fees)
    delivery_fee_pence                BIGINT NOT NULL DEFAULT 0,
    service_fee_pence                 BIGINT NOT NULL DEFAULT 0,
    tip_pence                         BIGINT NOT NULL DEFAULT 0,
    discount_pence                    BIGINT NOT NULL DEFAULT 0,
    total_pence                       BIGINT NOT NULL,
    currency                          CHAR(3) NOT NULL DEFAULT 'GBP',
    -- Promo
    promo_id                          UUID,
    promo_code                        VARCHAR(50),
    is_first_order_for_user           BOOLEAN DEFAULT FALSE,
    is_new_user_promo                 BOOLEAN DEFAULT FALSE,
    -- Payment
    payment_method_id                 UUID,
    payment_type                      VARCHAR(20) NOT NULL,
    card_bin                          VARCHAR(8),
    card_last_four                    VARCHAR(4),
    card_brand                        VARCHAR(20),
    card_funding_type                 VARCHAR(20),
    card_issuer_country               CHAR(2),
    is_digital_native_bank            BOOLEAN,
    is_new_payment_method             BOOLEAN,
    payment_authorized_at             TIMESTAMPTZ,
    payment_captured_at               TIMESTAMPTZ,
    payment_gateway                   VARCHAR(50),             -- ADYEN, STRIPE, BRAINTREE, WORLDPAY
    payment_gateway_txn_id            VARCHAR(255),
    avs_result                        VARCHAR(10),
    cvv_result                        VARCHAR(10),
    threeds_status                    VARCHAR(20),
    -- Device / session / network
    session_id                        UUID,
    device_id                         UUID,
    device_type                       VARCHAR(20),
    platform                          VARCHAR(20),
    os_version                        VARCHAR(50),
    app_version                       VARCHAR(50),
    browser_name                      VARCHAR(50),
    browser_version                   VARCHAR(50),
    ip_address                        INET,
    ip_country                        CHAR(2),
    ip_city                           VARCHAR(100),
    ip_is_proxy                       BOOLEAN,
    ip_is_vpn                         BOOLEAN,
    ip_is_tor                         BOOLEAN,
    ip_is_hosting                     BOOLEAN,
    device_user_count                 INTEGER,
    payment_user_count                INTEGER,
    -- Distance signals
    ip_to_delivery_distance_km        DECIMAL(8,2),
    billing_to_delivery_distance_km   DECIMAL(8,2),
    -- Behavioral
    time_to_checkout_seconds          INTEGER,
    cart_modifications_count          INTEGER,
    -- Fraud system output
    fraud_score                       DECIMAL(5,4),
    fraud_score_version               VARCHAR(50),
    fraud_decision                    VARCHAR(20),             -- APPROVE, REVIEW, DECLINE
    fraud_rules_triggered             VARCHAR(100)[],
    fraud_reviewed_at                 TIMESTAMPTZ,
    fraud_reviewed_by                 VARCHAR(100),
    fraud_outcome                     VARCHAR(20),             -- LEGIT, FRAUD, CHARGEBACK, REFUND_ABUSE, PROMO_ABUSE
    fraud_outcome_set_at              TIMESTAMPTZ,
    chargeback_received_at            TIMESTAMPTZ,
    chargeback_amount_pence           BIGINT,
    -- Audit
    created_at                        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (order_id, placed_at)                          -- composite PK required for partitioning
) PARTITION BY RANGE (placed_at);

CREATE UNIQUE INDEX idx_orders_order_number ON orders(order_number, placed_at);
CREATE INDEX idx_orders_user ON orders(user_id, placed_at DESC);
CREATE INDEX idx_orders_store ON orders(store_id, placed_at DESC);
CREATE INDEX idx_orders_device ON orders(device_id);
CREATE INDEX idx_orders_payment ON orders(payment_method_id);
CREATE INDEX idx_orders_ip ON orders(ip_address);
CREATE INDEX idx_orders_status ON orders(order_status);
CREATE INDEX idx_orders_terminal ON orders(terminal_state_reached_at)
    WHERE order_status IN ('DELIVERED','CANCELLED','REFUNDED','FAILED');
CREATE INDEX idx_orders_fraud_review ON orders(fraud_decision)
    WHERE fraud_decision = 'REVIEW';
"""
    )
    op.execute(
        """
CREATE TABLE order_items (
    order_item_id        UUID NOT NULL DEFAULT gen_random_uuid(),
    order_id             UUID NOT NULL,
    order_placed_at      TIMESTAMPTZ NOT NULL,                 -- needed for FK to partitioned parent
    item_id              UUID REFERENCES menu_items(item_id),
    item_name_snapshot   VARCHAR(255) NOT NULL,
    quantity             INTEGER NOT NULL,
    unit_price_pence     BIGINT NOT NULL,
    line_total_pence     BIGINT NOT NULL,
    is_hot_food          BOOLEAN NOT NULL DEFAULT TRUE,
    modifiers            JSONB,
    special_instructions TEXT,
    PRIMARY KEY (order_item_id, order_placed_at)
) PARTITION BY RANGE (order_placed_at);
CREATE INDEX idx_order_items_order ON order_items(order_id);
"""
    )
    op.execute(
        """
CREATE TABLE order_events (
    event_id        BIGSERIAL NOT NULL,
    order_id        UUID NOT NULL,
    order_placed_at TIMESTAMPTZ NOT NULL,
    event_type      VARCHAR(50) NOT NULL,
    event_data      JSONB,
    actor_type      VARCHAR(20),
    actor_id        UUID,
    ip_address      INET,
    device_id       UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id, order_placed_at)
) PARTITION BY RANGE (order_placed_at);
CREATE INDEX idx_order_events_order ON order_events(order_id, created_at);
CREATE INDEX idx_order_events_type ON order_events(event_type);
"""
    )
    op.execute(
        """
CREATE TABLE fraud_decisions (
    decision_id        BIGSERIAL PRIMARY KEY,
    order_id           UUID NOT NULL,
    order_placed_at    TIMESTAMPTZ NOT NULL,
    model_version      VARCHAR(50) NOT NULL,
    score              DECIMAL(5,4) NOT NULL,
    decision           VARCHAR(20) NOT NULL,
    features_snapshot  JSONB NOT NULL,
    rules_triggered    VARCHAR(100)[],
    latency_ms         INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_fraud_decisions_order ON fraud_decisions(order_id);
CREATE INDEX idx_fraud_decisions_model ON fraud_decisions(model_version);
CREATE INDEX idx_fraud_decisions_created ON fraud_decisions(created_at);
"""
    )
    op.execute(
        """
CREATE TABLE chargebacks (
    chargeback_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id         UUID NOT NULL,
    order_placed_at  TIMESTAMPTZ NOT NULL,
    reason_code      VARCHAR(20),
    reason_category  VARCHAR(50),                                -- FRAUD, NOT_AS_DESCRIBED, NOT_RECEIVED, DUPLICATE, OTHER
    amount_pence     BIGINT NOT NULL,
    received_at      TIMESTAMPTZ NOT NULL,
    resolved_at      TIMESTAMPTZ,
    resolution       VARCHAR(20),                                -- LOST, WON, ACCEPTED
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_chargebacks_order ON chargebacks(order_id);
"""
    )
    op.execute(
        """
CREATE TABLE orders_archive (LIKE orders INCLUDING ALL) PARTITION BY RANGE (placed_at);
CREATE TABLE order_items_archive (LIKE order_items INCLUDING ALL) PARTITION BY RANGE (order_placed_at);
CREATE TABLE order_events_archive (LIKE order_events INCLUDING ALL) PARTITION BY RANGE (order_placed_at);
"""
    )


def downgrade() -> None:
    pass
