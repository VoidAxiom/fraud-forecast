# PHASE 1: Foundation — Database, Schema, Migrations, Archival

## Goal of This Phase

Stand up the entire data infrastructure. By the end of this phase, you can run `docker-compose up`, the database initialises, all tables exist with proper partitioning, the SQLAlchemy ORM is wired up, and an archival job runs nightly to move terminal orders from hot to cold storage.

**Do not build the order generator, fraud detection, or any ML code in this phase.** Phase 1 is purely infrastructure.

## Context Recap

We are building a fraud detection platform for a UK food delivery company at 10x normal scale (1M users, 15K stores, 50 orders/sec). Stack: PostgreSQL 12, Redis 6, Python 3.8, SQLAlchemy 1.4, Alembic, Docker Compose. Currency is GBP stored as integer pence. Country is `GB`. Timezone is `Europe/London`. All monetary fields are `BIGINT`.

## Deliverables

1. `docker-compose.yml` with postgres, redis services
2. `infra/postgres/init.sql` for one-time extension setup
3. `db/alembic.ini` + Alembic environment configured
4. `db/migrations/001_initial_schema.py` — all 19 tables
5. `db/migrations/002_create_partitions.py` — weekly partitions for next 6 months
6. `db/migrations/003_create_roles.py` — `scoring_user` Postgres role with restricted permissions (used in Phase 3)
7. `shared/models.py` — full SQLAlchemy ORM
8. `shared/db.py` — session factory and engine config
9. `shared/enums.py` — Python enums matching DB CHECK constraints
10. `shared/money.py` — GBP/pence helpers and VAT calculator
11. `shared/uk_data.py` — UK constants (postcodes, BINs, cities, etc. — used by Phase 2; just define the structures here)
12. `archival/archiver.py` — nightly hot→cold mover
13. `Makefile` with: `up`, `down`, `reset`, `migrate`, `psql`, `redis-cli`, `test`
14. `.env.example` with all required variables documented
15. `tests/test_schema.py` and `tests/test_archiver.py`

## Detailed Specifications

### docker-compose.yml

Services:

**`postgres`**
- Image: `postgres:12-alpine`
- Environment: `POSTGRES_DB=fraud_platform`, `POSTGRES_USER=app`, `POSTGRES_PASSWORD=app_dev_password`
- Port: `5432:5432`
- Volume: named volume `pg_data` mounted at `/var/lib/postgresql/data`
- Volume: `./infra/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro`
- Healthcheck: `pg_isready -U app -d fraud_platform`, interval 5s, retries 10
- Shared memory: `shm_size: 1gb` (needed for sustained insert load)
- Command override to tune for write-heavy workload:
  ```
  command: postgres
    -c shared_buffers=512MB
    -c effective_cache_size=2GB
    -c maintenance_work_mem=256MB
    -c wal_buffers=16MB
    -c synchronous_commit=off
    -c max_wal_size=4GB
    -c checkpoint_completion_target=0.9
    -c random_page_cost=1.1
  ```
  (`synchronous_commit=off` is acceptable for this simulation — a real prod system would weigh durability vs throughput differently)

**`redis`**
- Image: `redis:6-alpine`
- Port: `6379:6379`
- Volume: `redis_data:/data`
- Volume: `./infra/redis/redis.conf:/usr/local/etc/redis/redis.conf:ro`
- Command: `redis-server /usr/local/etc/redis/redis.conf`
- Config: maxmemory 512mb, maxmemory-policy `allkeys-lru`, appendonly no (simulation, not durability)

**`archiver`** (defined now, exercised by tests)
- Build: `./` with a Dockerfile that installs Python 3.8 deps
- Depends on: postgres healthcheck
- Environment: `DATABASE_URL=postgresql://app:app_dev_password@postgres:5432/fraud_platform`
- Command: `python -m archival.archiver`
- Restart: `unless-stopped`

Define both named volumes (`pg_data`, `redis_data`) and a custom network `fraud_net`.

### infra/postgres/init.sql

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS btree_gin;
-- A bare `SET timezone = ...` only affects the init script's own session;
-- subsequent connections revert to UTC. Use `ALTER DATABASE` so the
-- setting persists across connections.
ALTER DATABASE fraud_platform SET timezone = 'Europe/London';
```

### Alembic Setup

- `alembic.ini` with `sqlalchemy.url = postgresql://app:app_dev_password@localhost:5432/fraud_platform` (override via env var in env.py)
- `db/migrations/env.py` reads `DATABASE_URL` from environment, falls back to alembic.ini
- All migrations are pure SQL via `op.execute(...)` blocks (no autogenerate). This is intentional — partitioning and DDL specifics don't round-trip through autogenerate cleanly.

### Migration 001: Full Schema

Create the following 19 tables exactly. Column comments matter — preserve them. All UUIDs default to `gen_random_uuid()`. All TIMESTAMPTZ defaults to `NOW()` where indicated.

```sql
-- ============================================================
-- USERS
-- ============================================================
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

-- ============================================================
-- USER ADDRESSES (UK)
-- ============================================================
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

-- ============================================================
-- DEVICES
-- ============================================================
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

-- ============================================================
-- USER ↔ DEVICE (many-to-many)
-- ============================================================
CREATE TABLE user_devices (
    user_id        UUID NOT NULL REFERENCES users(user_id),
    device_id      UUID NOT NULL REFERENCES devices(device_id),
    first_used_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    session_count  INTEGER DEFAULT 1,
    is_trusted     BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (user_id, device_id)
);

-- ============================================================
-- SESSIONS
-- ============================================================
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

-- ============================================================
-- PAYMENT METHODS (UK card mix)
-- ============================================================
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

-- ============================================================
-- MERCHANTS
-- ============================================================
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

-- ============================================================
-- STORES (individual restaurant locations)
-- ============================================================
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

-- ============================================================
-- STORE HOURS
-- ============================================================
CREATE TABLE store_hours (
    store_id     UUID NOT NULL REFERENCES stores(store_id),
    day_of_week  SMALLINT NOT NULL,                       -- 0=Sun, 6=Sat
    open_time    TIME NOT NULL,
    close_time   TIME NOT NULL,
    PRIMARY KEY (store_id, day_of_week, open_time)
);

-- ============================================================
-- MENU ITEMS
-- ============================================================
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

-- ============================================================
-- DRIVERS
-- ============================================================
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

-- ============================================================
-- PROMOTIONS
-- ============================================================
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

-- ============================================================
-- ORDERS (HOT) — partitioned weekly on placed_at
-- ============================================================
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

-- ============================================================
-- ORDER ITEMS
-- ============================================================
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

-- ============================================================
-- ORDER EVENTS (append-only event log)
-- ============================================================
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

-- ============================================================
-- FRAUD DECISIONS (audit log)
-- ============================================================
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

-- ============================================================
-- CHARGEBACKS
-- ============================================================
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

-- ============================================================
-- ORDERS ARCHIVE (COLD) — same schema as orders
-- ============================================================
CREATE TABLE orders_archive (LIKE orders INCLUDING ALL) PARTITION BY RANGE (placed_at);
CREATE TABLE order_items_archive (LIKE order_items INCLUDING ALL) PARTITION BY RANGE (order_placed_at);
CREATE TABLE order_events_archive (LIKE order_events INCLUDING ALL) PARTITION BY RANGE (order_placed_at);
```

### Migration 002: Weekly Partitions

Programmatically create weekly partitions for `orders`, `orders_archive`, `order_items`, `order_items_archive`, `order_events`, `order_events_archive`. Generate partitions for 26 weeks back and 26 weeks forward from the migration date. Naming: `orders_p_YYYY_WW` (e.g. `orders_p_2026_21`).

Example partition creation logic (write in Python in the migration):
```python
from datetime import date, timedelta
def week_partitions(parent, start_date, num_weeks):
    statements = []
    for i in range(num_weeks):
        wk_start = start_date + timedelta(weeks=i)
        wk_end = wk_start + timedelta(weeks=1)
        name = f"{parent}_p_{wk_start.strftime('%Y_%U')}"
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {parent} "
            f"FOR VALUES FROM ('{wk_start.isoformat()}') TO ('{wk_end.isoformat()}');"
        )
    return statements
```

Apply to all six partitioned parents. Include `partition_date_column` arg if needed (`placed_at` vs `order_placed_at`). Also include a "future partition maintenance" function (Phase 7 will schedule it) — define it as a Postgres function:

```sql
CREATE OR REPLACE FUNCTION ensure_future_partitions(weeks_ahead INTEGER DEFAULT 8)
RETURNS void AS $$
-- Creates any missing weekly partitions for next N weeks for all 6 partitioned tables
-- Idempotent (uses CREATE TABLE IF NOT EXISTS via dynamic SQL)
$$ LANGUAGE plpgsql;
```

### Migration 003: Postgres Roles

```sql
CREATE ROLE scoring_user WITH LOGIN PASSWORD 'scoring_dev_password';
GRANT CONNECT ON DATABASE fraud_platform TO scoring_user;
GRANT USAGE ON SCHEMA public TO scoring_user;
-- Read access to most tables
GRANT SELECT ON ALL TABLES IN SCHEMA public TO scoring_user;
-- Write access ONLY to fraud_decisions and orders (for score updates)
GRANT INSERT ON fraud_decisions TO scoring_user;
GRANT UPDATE (fraud_score, fraud_score_version, fraud_decision, fraud_rules_triggered, updated_at) 
    ON orders TO scoring_user;
GRANT USAGE, SELECT ON SEQUENCE fraud_decisions_decision_id_seq TO scoring_user;
-- Phase 3 will REVOKE select on simulator_ground_truth (created in Phase 3)
```

Also create `simulator_user` with full DML, and a read-only `analyst_user` for the dashboard.

### shared/models.py

Full SQLAlchemy 1.4 ORM declarative models matching every table above. Use `sqlalchemy.dialects.postgresql.UUID`, `INET`, `JSONB`, `CITEXT` (custom type — use `sqlalchemy_utils.CITEXTType` or define a TypeDecorator). For partitioned tables, declare the model normally but document at the top of the class that PK includes the partition key.

Example structure for one table:
```python
class User(Base):
    __tablename__ = 'users'
    user_id = Column(UUID(as_uuid=True), primary_key=True, server_default=text('gen_random_uuid()'))
    email = Column(CITEXTType, unique=True, nullable=False)
    # ... etc
    
    addresses = relationship('UserAddress', back_populates='user')
    devices = relationship('Device', secondary='user_devices', back_populates='users')
```

Include relationships only where they'll be used in Phase 2+ (don't over-engineer — the Order model is mostly snapshot fields, not relationships).

### shared/db.py

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
import os

def get_engine(role: str = 'app'):
    """role: 'app' | 'scoring' | 'simulator' | 'analyst'"""
    # Look up DATABASE_URL_{ROLE} env var, fall back to DATABASE_URL
    ...

SessionLocal = sessionmaker(...)

@contextmanager
def get_session(role: str = 'app') -> Session:
    ...
```

Connection pool: pool_size=20, max_overflow=10, pool_pre_ping=True, pool_recycle=3600. For the simulator (Phase 2), we'll bump this higher.

### shared/enums.py

Define Python `enum.Enum` classes for every CHECK constraint in the schema (OrderStatus, OrderChannel, OrderType, AccountStatus, RiskTier, FraudDecision, etc.). Provide a helper `enum_values(EnumClass) -> list[str]` for inserting random values in seed.

### shared/money.py

```python
def pounds_to_pence(amount: float) -> int: ...
def pence_to_pounds_str(pence: int) -> str:  # '£12.50'
    ...

def calculate_vat(subtotal_pence: int, items: list[dict]) -> int:
    """
    UK VAT: 20% on hot food, 0% on cold food (zero-rated).
    items: [{'line_total_pence': int, 'is_hot_food': bool}, ...]
    Returns total VAT in pence (rounded half-up).
    """
    ...

def calculate_total(subtotal_pence, vat_pence, delivery_fee_pence, 
                    service_fee_pence, tip_pence, discount_pence) -> int:
    return subtotal_pence + vat_pence + delivery_fee_pence + service_fee_pence + tip_pence - discount_pence
```

VAT on delivery_fee and service_fee is also 20%; bake that into how generator.py builds totals in Phase 2. Document this clearly.

### shared/uk_data.py

Define module-level constants. Phase 2 imports these; Phase 1 just defines them.

```python
UK_CITIES = [
    # (name, population_weight, lat, lon, county)
    ('London', 0.35, 51.5074, -0.1278, 'Greater London'),
    ('Birmingham', 0.08, 52.4862, -1.8904, 'West Midlands'),
    ('Manchester', 0.08, 53.4808, -2.2426, 'Greater Manchester'),
    ('Glasgow', 0.06, 55.8642, -4.2518, 'Glasgow City'),
    ('Leeds', 0.06, 53.8008, -1.5491, 'West Yorkshire'),
    ('Liverpool', 0.05, 53.4084, -2.9916, 'Merseyside'),
    ('Bristol', 0.05, 51.4545, -2.5879, 'Bristol'),
    ('Edinburgh', 0.05, 55.9533, -3.1883, 'City of Edinburgh'),
    ('Sheffield', 0.04, 53.3811, -1.4701, 'South Yorkshire'),
    ('Newcastle upon Tyne', 0.04, 54.9783, -1.6178, 'Tyne and Wear'),
    # ... 10 more smaller cities making up remaining 14%
]

UK_POSTCODE_AREAS = {
    'London': ['E', 'EC', 'N', 'NW', 'SE', 'SW', 'W', 'WC'],
    'Birmingham': ['B'],
    'Manchester': ['M'],
    # ... etc
}

CUISINE_WEIGHTS = {
    'Indian': 0.15, 'Chinese': 0.12, 'Italian': 0.08, 'Pizza': 0.07,
    'Kebab': 0.06, 'Turkish': 0.04, 'Fish & Chips': 0.08,
    'Burger': 0.06, 'American': 0.04, 'Thai': 0.05, 'Japanese': 0.03,
    'Sushi': 0.02, 'Caribbean': 0.03, 'Lebanese': 0.03,
    'Polish': 0.02, 'British': 0.03, 'Pub': 0.02, 'Vietnamese': 0.02, 
    'Other': 0.05,
}

POS_SYSTEMS = [
    ('Lightspeed', 0.15), ('Square', 0.15), ('Epos Now', 0.12),
    ('Toast', 0.08), ('Clover', 0.08), ('Vita Mojo', 0.05),
    ('Deliveroo Tablet', 0.10), ('Uber Eats Tablet', 0.10), ('In-House', 0.17),
]

CARD_BRANDS = [('VISA', 0.45), ('MASTERCARD', 0.35), ('AMEX', 0.08), 
               ('MAESTRO', 0.05), ('OTHER', 0.07)]

# UK BIN ranges by issuer (real BIN prefixes; lookup-able)
UK_CARD_ISSUERS = [
    # (issuer_name, list_of_bin_prefixes, funding_type, is_digital_native, weight)
    ('Barclays', ['492181', '492182', '492183', '492184', '492185'], 'DEBIT', False, 0.15),
    ('HSBC UK', ['453978', '453979', '465859', '465860'], 'DEBIT', False, 0.12),
    ('Lloyds Bank', ['454313', '454314', '454742', '454743'], 'DEBIT', False, 0.10),
    ('NatWest', ['465902', '465903', '465904', '465905'], 'DEBIT', False, 0.08),
    ('Santander UK', ['491880', '491881', '511234'], 'DEBIT', False, 0.07),
    ('Halifax', ['454742', '465925'], 'DEBIT', False, 0.05),
    ('Nationwide', ['492930', '492931'], 'DEBIT', False, 0.05),
    ('Monzo Bank', ['535522', '535523', '539923'], 'DEBIT', True, 0.10),
    ('Revolut', ['516842', '535410', '537956'], 'DEBIT', True, 0.08),
    ('Starling Bank', ['548175', '548176'], 'DEBIT', True, 0.05),
    ('Chase UK', ['483138'], 'DEBIT', True, 0.03),
    ('American Express UK', ['374622', '374623', '378282'], 'CREDIT', False, 0.05),
    # ... a few more, including some prepaid issuers
    ('Foreign EU', ['435060', '424519'], 'DEBIT', False, 0.04),
    ('Foreign US', ['414709', '424519'], 'CREDIT', False, 0.03),
]

EMAIL_DOMAINS = [
    ('gmail.com', 0.35), ('outlook.com', 0.12), ('hotmail.com', 0.15),
    ('yahoo.co.uk', 0.08), ('icloud.com', 0.10), ('btinternet.com', 0.05),
    ('googlemail.com', 0.03), ('live.co.uk', 0.02), ('hotmail.co.uk', 0.05),
]
DISPOSABLE_EMAIL_DOMAINS = [
    'mailinator.com', 'guerrillamail.com', 'tempmail.io', 
    'throwawaymail.com', '10minutemail.com', 'mailsac.com',
]
DISPOSABLE_DOMAIN_RATE = 0.05  # 5% of users get one

PAYMENT_GATEWAYS = ['ADYEN', 'STRIPE', 'BRAINTREE', 'WORLDPAY', 'CHECKOUT_COM']

# Generate a valid-format UK postcode
def random_uk_postcode(city: str) -> str:
    """Returns something like 'SW1A 1AA' or 'M1 1AE'"""
    ...

UK_BANK_HOLIDAYS_2026 = [
    date(2026, 1, 1),   # New Year
    date(2026, 4, 3),   # Good Friday
    date(2026, 4, 6),   # Easter Monday
    date(2026, 5, 4),   # Early May
    date(2026, 5, 25),  # Spring
    date(2026, 8, 31),  # Summer
    date(2026, 12, 25), # Christmas
    date(2026, 12, 28), # Boxing (substitute day)
]
```

### archival/archiver.py

Long-running daemon. Uses APScheduler `BackgroundScheduler`. Schedule: every day at 03:00 Europe/London.

Algorithm:
```
Loop until no rows moved:
    BEGIN TRANSACTION
    SELECT order_id, placed_at FROM orders 
        WHERE order_status IN ('DELIVERED','CANCELLED','REFUNDED','FAILED')
          AND terminal_state_reached_at < NOW() - INTERVAL '48 hours'
        LIMIT 10000
        FOR UPDATE SKIP LOCKED
    INSERT INTO orders_archive SELECT * FROM orders WHERE (order_id, placed_at) IN (...)
    INSERT INTO order_items_archive SELECT * FROM order_items WHERE (order_id, order_placed_at) IN (...)
    INSERT INTO order_events_archive SELECT * FROM order_events WHERE (order_id, order_placed_at) IN (...)
    DELETE FROM order_events WHERE (order_id, order_placed_at) IN (...)
    DELETE FROM order_items WHERE (order_id, order_placed_at) IN (...)
    DELETE FROM orders WHERE (order_id, placed_at) IN (...)
    COMMIT
    Log: {moved_count, duration_ms}
```

Use `psycopg2` directly (not SQLAlchemy ORM) for performance. Batch size configurable via env `ARCHIVE_BATCH_SIZE` (default 10000). Total batch cap per run: env `ARCHIVE_MAX_BATCHES` (default 500 — i.e. up to 5M rows per nightly run, enough for 50 ord/sec × 86400s × 1.2 buffer).

Structured logging via Python `logging` + `python-json-logger`. Log every batch with fields: `event=archive_batch`, `moved`, `duration_ms`, `batch_num`.

Also expose a one-shot mode: `python -m archival.archiver --once` runs one full pass and exits. Used by Phase 1 tests.

### Makefile

```make
.PHONY: up down reset migrate psql redis-cli test logs

up:
	docker-compose up -d postgres redis
	./scripts/wait_for_postgres.sh
	$(MAKE) migrate

down:
	docker-compose down

reset:
	docker-compose down -v
	$(MAKE) up

migrate:
	docker-compose run --rm app alembic upgrade head

psql:
	docker-compose exec postgres psql -U app -d fraud_platform

redis-cli:
	docker-compose exec redis redis-cli

test:
	pytest tests/ -v

logs:
	docker-compose logs -f --tail=100
```

### .env.example

Document every env var the system reads. Group by subsystem. Example:
```
# === Database ===
DATABASE_URL=postgresql://app:app_dev_password@localhost:5432/fraud_platform
DATABASE_URL_SCORING=postgresql://scoring_user:scoring_dev_password@localhost:5432/fraud_platform
DATABASE_URL_SIMULATOR=postgresql://app:app_dev_password@localhost:5432/fraud_platform

# === Redis ===
REDIS_URL=redis://localhost:6379/0

# === Archival ===
ARCHIVE_BATCH_SIZE=10000
ARCHIVE_MAX_BATCHES=500
ARCHIVE_SCHEDULE_HOUR=3
ARCHIVE_SCHEDULE_TZ=Europe/London

# === (Phase 2+ vars — placeholders) ===
ORDERS_PER_SECOND=50
FRAUD_RATE=0.02
SIMULATION_TIME_COMPRESSION=60
SCORING_ENABLED=false
SCORING_SERVICE_URL=http://scoring_service:8000
```

### Tests

**tests/conftest.py** — fixtures: `db_engine`, `db_session`, `clean_db` (truncates all tables between tests).

**tests/test_schema.py**
- All 19 tables exist
- All indexes exist
- All CHECK constraints reject invalid values
- Partitioning works: insert into `orders` with placed_at across two weeks lands in correct partitions (query `pg_class`)
- The 3 roles exist with the expected permissions (use `has_table_privilege`)
- `gen_random_uuid()` works (pgcrypto installed)
- `citext` works (case-insensitive email uniqueness)

**tests/test_archiver.py**
- Insert 100 orders: 50 are DELIVERED >48h ago, 30 are DELIVERED <48h ago, 20 are PLACED.
- Run archiver in `--once` mode.
- Assert: 50 rows now in `orders_archive`, 50 rows remain in `orders` (30 recent terminal + 20 active).
- Insert 10000 archivable orders; run archiver; assert all moved in a reasonable time (<30s).
- Confirm order_items and order_events for archived orders moved as well.

## Acceptance Criteria for Phase 1

- `make reset` brings up a clean stack from nothing in <60 seconds
- `make migrate` runs all three migrations cleanly with no warnings
- All tests in `tests/test_schema.py` and `tests/test_archiver.py` pass
- `psql` shows all 19 tables and the partitioned children for 6 months back/forward
- Connecting as `scoring_user` and trying `DELETE FROM orders` fails with permission denied
- Connecting as `scoring_user` and trying `UPDATE orders SET total_pence = 0` fails (only the 4 fraud columns are writable)
- A 10K-row archival pass completes in <30 seconds
- All Python code passes `mypy --strict` on the shared/ module

## Out of Scope for Phase 1

- The `simulator_ground_truth` table (Phase 3)
- Any seeding of data (Phase 2)
- Any order generation (Phase 2)
- Anything Redis-related beyond bringing up the service (Phase 4)
- Anything ML (Phases 5-6)
