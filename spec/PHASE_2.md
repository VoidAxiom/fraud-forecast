# PHASE 2: Seeding & Legitimate Order Simulation

## Goal of This Phase

Populate the database with realistic UK-scale seed data (1M users, 15K stores, etc.), then build a sustained order generation system that produces ~50 legitimate orders/second with full lifecycle progression. **No fraud injection in this phase** — that comes in Phase 3.

## Prerequisites

Phase 1 must be complete and acceptance criteria met. You can rely on: all 19 tables exist, all SQLAlchemy models are in `shared/models.py`, UK constants are in `shared/uk_data.py`, money/VAT helpers exist in `shared/money.py`, archival runs nightly.

## Context Recap

UK food delivery platform, 10x scale. Currency in pence. UK postcodes, GBP card BINs, VAT-correct totals. Read MASTER.md sections "Scale Targets" and "UK Localisation Rules" if not already in context.

## Deliverables

1. `simulator/seed.py` — one-shot seeding, parallelised with multiprocessing
2. `simulator/generator.py` — async order generation loop
3. `simulator/lifecycle.py` — async lifecycle progression loop
4. `simulator/cart_builder.py` — helper that builds carts realistically
5. `simulator/snapshot_builder.py` — computes the denormalized order snapshot fields
6. `scripts/wait_for_postgres.sh` (if not already from Phase 1)
7. `scripts/seed.sh` — entry point that runs `python -m simulator.seed`
8. Updated `docker-compose.yml` to add `simulator` and `lifecycle` services
9. `tests/test_simulator.py` and `tests/test_lifecycle.py`

## Scale Numbers (CRITICAL — these are the seed targets)

| Entity | Count | Notes |
|---|---|---|
| Users | 1,000,000 | Distributed across UK cities by population |
| User Addresses | 1,500,000 | Avg 1.5 per user; some users have 3-4 |
| Devices | 300,000 | Avg 1.5 unique devices per active user |
| User-Device links | ~1,200,000 | Many users share family devices |
| Sessions | 0 | Generated dynamically by order generator |
| Payment Methods | 2,000,000 | Avg 2 per user |
| Merchants | 5,000 | UK-realistic chain + independent mix |
| Stores | 15,000 | Avg 3 stores per merchant |
| Store Hours | ~105,000 | 7 days × 15K stores |
| Menu Items | 80,000 | Avg 5-10 per store |
| Drivers | 2,000 | Distributed by city |
| Promotions | 30 | Including `WELCOME10`, `NEWUK20`, etc. |

**Reproducibility:** Set `random.seed(42)` and `numpy.random.seed(42)` and `Faker.seed(42)` at top of `seed.py`. Print seed values used.

## Detailed Specifications

### simulator/seed.py

This is a big job. Naive sequential `INSERT` of 1M users would take ~30 minutes. Use `COPY FROM STDIN` via `psycopg2.copy_expert` with in-memory CSV buffers — this is 100x faster.

#### Structure

```python
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scale', type=float, default=1.0, 
                        help='Scale factor; 0.1 = 10% scale for dev')
    parser.add_argument('--skip', nargs='*', default=[],
                        help='Skip entities (e.g. --skip users devices)')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    
    seed_everything(args.seed)
    
    # 1. Merchants & Stores (must come before users for store-near-user logic)
    seed_merchants(args.scale)        # 5,000 × scale
    seed_stores(args.scale)           # 15,000 × scale  
    seed_store_hours()
    seed_menu_items(args.scale)
    
    # 2. Drivers
    seed_drivers(args.scale)
    
    # 3. Users + addresses + devices + payment methods (parallelisable across workers)
    seed_users_parallel(args.scale, args.workers)
    
    # 4. Promotions
    seed_promotions()
    
    # 5. Print summary
    print_summary()
```

#### Detailed entity rules

**Merchants (5,000)**
- 80% independent (single-store merchants)
- 15% small chains (2-5 stores)
- 5% large chains (10-50 stores)
- Brand names: mix of realistic UK chain names (`Nando's`, `Wagamama`, `Pret A Manger`, `Greggs`, `Pizza Express`, `Itsu`, `Leon`, `Honest Burgers`, `Dishoom`, `Five Guys UK`) plus generated independents (`Faker.company()` filtered to plausible food-business names)
- `merchant_category`: 60% QSR, 20% CASUAL_DINING, 5% FINE_DINING, 10% DARK_KITCHEN, 5% CONVENIENCE
- `companies_house_no`: 8-digit number with prefix `0` (UK format)
- `vat_number`: `GB` + 9 digits

**Stores (15,000)**
- Each merchant gets stores based on its tier (above). Total = 15,000.
- Each store is assigned to a city using city population weights from `UK_CITIES`. Within a city, lat/lon is sampled by jittering the city centre with normal noise (σ = 0.02° lat/lon ≈ 2.2km).
- Postcode generated using `random_uk_postcode(city)` from `shared/uk_data.py` — postcode prefix must match the city's allowed prefixes (`UK_POSTCODE_AREAS`).
- Cuisine types: 1-3 sampled from `CUISINE_WEIGHTS` (without replacement); first is "primary".
- Price tier: weighted [(1, 0.30), (2, 0.45), (3, 0.20), (4, 0.05)].
- POS system: weighted from `POS_SYSTEMS`.
- `delivery_radius_km`: uniform 3-8 km.
- `avg_prep_time_min`: depends on cuisine (Pizza 18min, Indian 25min, Sushi 30min, Burger 12min, Fish & Chips 15min etc.)
- `accepts_delivery=true` for 92%, `accepts_pickup=true` for 88%, `accepts_in_store=true` for 75%, `accepts_cash=false` for 95%.
- 95% have `is_verified=true`.

**Store Hours**
- For each store, generate 7 rows.
- 90% of stores: open 11:00-23:00 daily
- 5%: late-night kebab/pizza pattern (17:00-03:00)
- 5%: lunch only (11:00-15:00)
- ~10% have a closed day (Mon or Tue)

**Menu Items (80,000)**
- For each store, generate 4-12 items (poisson, λ=6).
- Item names: cuisine-appropriate. Maintain a Python dict of `CUISINE_MENU_TEMPLATES`:
  ```python
  CUISINE_MENU_TEMPLATES = {
      'Indian': ['Chicken Tikka Masala', 'Lamb Rogan Josh', 'Vegetable Biryani', 
                 'Garlic Naan', 'Pilau Rice', 'Onion Bhaji', 'Samosa', 'Tandoori Chicken', ...],
      'Fish & Chips': ['Cod & Chips', 'Haddock & Chips', 'Battered Sausage', 
                       'Mushy Peas', 'Curry Sauce', 'Pickled Egg', 'Saveloy', ...],
      'Kebab': ['Doner Kebab', 'Mixed Shish', 'Chicken Shish', 'Lamb Doner Wrap', 
                'Falafel Wrap', 'Chips', 'Halloumi Wrap', ...],
      'Pizza': ['Margherita', 'Pepperoni', 'Hawaiian', 'Quattro Stagioni', 
                'Garlic Bread', 'Tiramisu', ...],
      # ... etc for every cuisine in CUISINE_WEIGHTS
  }
  ```
- Price: cuisine-and-tier-dependent. E.g. Fish & Chips main = £8-£14 (800-1400 pence), Sushi main = £12-£25, sides £2-£5, drinks £1.50-£3.50. Add ±20% noise per store.
- `is_hot_food`: true for mains and most sides; false for drinks, salads, ice cream, etc.
- `category`: STARTER, MAIN, SIDE, DRINK, DESSERT — inferred from item name templates.

**Drivers (2,000)**
- `home_city` weighted same as users.
- Vehicle: BIKE 35%, EBIKE 25%, SCOOTER 20%, CAR 18%, WALK 2%.
- `licence_plate`: UK format if CAR/SCOOTER (e.g. `AB12 CDE`), null otherwise.
- `rating`: normal(4.7, 0.3), clipped to [3.0, 5.0].
- `completed_deliveries`: lognormal(μ=ln(100), σ=1), clipped to [0, 5000].

**Users (1,000,000) — Parallelised**

Split into 8 worker processes, each generates 125,000 users. Each worker writes to a per-worker CSV buffer and `COPY`s into the DB.

Per user:
- `email`: realistic combo of first/last name + Faker, picking domain from `EMAIL_DOMAINS` weights. **5% of users** get a disposable domain (planted fraud signal).
- `phone`: `+44 7` + 9 digits (UK mobile), 90% populated; 10% null. **85% are verified** (`phone_verified_at` set).
- `password_hash`: literal string `'$2b$10$SIMULATED_HASH_DO_NOT_USE_IN_PROD'`
- `first_name`, `last_name`: Faker `en_GB`
- `date_of_birth`: 18-75 years ago, slight skew younger (mean 35)
- `account_status`: 95% ACTIVE, 3% SUSPENDED, 1% BANNED, 1% DELETED
- `risk_tier`: 10% TRUSTED, 80% STANDARD, 8% ELEVATED, 2% HIGH_RISK
- `created_at`: exponential distribution favoring recency. Spread across last 4 years (since mid-2022). Use `now - exponential(mean=400 days)`, clipped to [1 day ago, 1500 days ago]. This means more recent signups than old ones — realistic platform growth curve.
- `referral_source`: 40% ORGANIC, 25% GOOGLE_ADS, 15% FB_ADS, 10% REFERRAL, 5% TV, 5% other
- 10% of users have `referred_by_user_id` pointing to an earlier-created user
- `signup_ip`: random UK IPv4 (BT, Sky, Virgin Media, EE ranges — maintain a list of UK ISP IP prefix samples)
- `signup_postcode`: matches user's primary city
- `signup_country`: `'GB'` for 97%, foreign for 3%
- `email_verified_at`: 80% verified, set to `created_at + uniform(0, 24h)`

For each user, generate 1-3 addresses (avg 1.5; first is `is_default=true`). Address city == user's primary city. Within city, jitter lat/lon by σ=0.02°. Generate postcode appropriately. `address_type`: 85% RESIDENTIAL, 8% COMMERCIAL (work), 5% STUDENT_HALL, 2% HOTEL.

For each user, generate 1-3 payment methods (avg 2):
- 70% `CREDIT_CARD` or `DEBIT_CARD` from `UK_CARD_ISSUERS` (weighted)
- 15% `PAYPAL`
- 10% `APPLE_PAY` (mostly iOS users)
- 4% `GOOGLE_PAY`
- 1% `GIFT_CARD`
- Card BIN: pick from issuer's BIN list. Last 4: random.
- `card_funding_type`: per issuer (Monzo/Revolut/Starling = DEBIT; Amex = CREDIT; etc.)
- `card_issuer_country`: GB for 88%, foreign for 12%
- `is_digital_native_bank`: from issuer record
- `avs_result`: 95% MATCH (legit users)
- `cvv_result`: 99% MATCH
- `billing_address_id`: 90% set to one of user's addresses, 10% null
- 70% of users have one `is_default=true` payment method
- `card_exp_year/month`: 1-4 years in the future

**Devices (300,000) and User-Device links (~1.2M)**

Devices are partially shared across users (family devices). Algorithm:
1. Generate 300,000 unique devices first (just the `devices` table rows). Distribution:
   - 50% MOBILE_APP / iOS, app_version like '4.32.1'
   - 35% MOBILE_APP / Android
   - 10% MOBILE_WEB
   - 4% DESKTOP_WEB (mostly Chrome/Safari/Firefox/Edge)
   - 1% TABLET
2. Then, for each of the 1M users:
   - 80% get 1 device (their own)
   - 15% get 2 devices
   - 4% get 3 devices
   - 1% get 4-5 devices
   - Average ~1.2 device-user links per user → ~1.2M `user_devices` rows
3. **Critical for Phase 3:** Plant 50 "shared family devices" deliberately by assigning the same device to 5-10 different `user_id`s. Mark these in seeding logs.
4. Update `devices.unique_users_count` after all links inserted.

For device_fingerprint: SHA256 of a synthetic deterministic string so the fingerprint is realistic looking and reproducible.

**Promotions (30)**

```python
PROMOS = [
    # Always-active new user
    ('WELCOME10', 'NEW_USER', None, 10.0, 1500, 1, ...),     # 10% off, min £15
    ('NEWUK20', 'NEW_USER', None, 20.0, 2000, 1, ...),       # 20% off, min £20
    ('FREEDEL', 'FREE_DELIVERY', None, None, 1000, 5, ...),  # free delivery, min £10
    # Time-limited
    ('SUNDAYROAST', 'PERCENT_OFF', None, 15.0, 2500, 2, ...),
    ('FRIDAY5', 'POUND_OFF', 500, None, 2000, 1, ...),       # £5 off
    # Referral codes (10 of these, one per top-referrer-pattern)
    ('REFERRALABC', 'REFERRAL', 1000, None, 0, 1, ...),       # £10 off, signup bonus
    # ... etc
]
```

**Logging** — Use Python `logging` with structured output. At the end, print:
```
=== SEEDING COMPLETE ===
Merchants:        5,000   in 1.2s
Stores:          15,000   in 3.4s
Store Hours:    105,000   in 8.1s
Menu Items:      80,000   in 12.3s
Drivers:          2,000   in 0.8s
Users:        1,000,000   in 145.2s   (8 workers, ~6,900/s)
User Addresses: 1,500,124  in 89.4s
Payment Methods: 1,998,873 in 142.1s
Devices:        300,000   in 18.2s
User-Device Links: 1,201,442 in 65.3s
Promotions:          30   in 0.1s
=== TOTAL: 485.4s ===
```

Target: full 1.0-scale seed completes in **under 15 minutes** on a developer laptop (16GB RAM, 8 cores).

#### Postgres tuning for seed

Before seed, `seed.py` should set session-level:
```sql
SET LOCAL synchronous_commit = OFF;
SET LOCAL work_mem = '256MB';
SET LOCAL maintenance_work_mem = '1GB';
```

After seed, run `VACUUM ANALYZE` on all tables (issue from outside transaction).

### simulator/cart_builder.py

```python
@dataclass
class Cart:
    store_id: UUID
    items: list[CartItem]  # each CartItem has item_id, name, qty, unit_price_pence, is_hot_food
    
    @property
    def subtotal_pence(self) -> int: ...
    @property
    def item_count(self) -> int: ...
    @property
    def unique_item_count(self) -> int: ...

def build_realistic_cart(store_id: UUID, user_profile: UserProfile) -> Cart:
    """
    Realistic cart distribution:
    - Item count: poisson(λ=2.5), clipped to [1, 8]
    - 70% chance a 'MAIN' is included
    - 40% chance to include a SIDE
    - 30% chance to include a DRINK
    - 50% chance for repeat items (qty=2 vs 1)
    Returns Cart with all line_total_pence computed.
    """
```

### simulator/snapshot_builder.py

```python
def build_order_snapshot(
    user: User,
    store: Store,
    cart: Cart,
    delivery_address: UserAddress | None,
    payment_method: PaymentMethod,
    device: Device,
    session: Session,
    ip: IPAddress,
    promo: Promotion | None,
    db_session,
) -> dict:
    """
    Returns a dict matching the orders table columns.
    Performs the necessary DB lookups to populate all snapshot fields:
      - user_account_age_days (computed from user.created_at)
      - user_total_orders_lifetime / 30d (query orders + orders_archive)
      - user_total_spend_lifetime_pence (sum query)
      - is_first_order_for_user (= user_total_orders_lifetime == 0)
      - is_new_payment_method (no orders with this payment_method_id by this user before)
      - is_new_delivery_address (no orders with this address_id before)
      - device_user_count (look up devices.unique_users_count)
      - payment_user_count (look up payment_methods.unique_users_count)
      - ip_to_delivery_distance_km (haversine ip city centroid vs delivery)
      - billing_to_delivery_distance_km (haversine billing vs delivery)
      - delivery_distance_km (haversine store vs delivery)
      - VAT computed correctly per item via money.calculate_vat
    """
```

**Important:** these snapshot queries can be expensive at scale. Cache user stats in Redis (`user_stats:{user_id}` hash, TTL 60s) after first computation. Phase 4 will formalise this as the feature store; Phase 2's caching is a simpler precursor.

### simulator/generator.py

Async event loop using `asyncio` + `asyncpg` for raw speed, NOT SQLAlchemy ORM. Generator runs at configurable rate.

```python
async def main():
    config = load_config_from_env()
    pool = await asyncpg.create_pool(DATABASE_URL_SIMULATOR, min_size=10, max_size=50)
    redis = await aioredis.from_url(REDIS_URL)
    
    # Load static data once (in-memory caches):
    stores_by_city = await load_stores_by_city(pool)         # dict[city] -> list[Store]
    promos = await load_active_promos(pool)
    
    # Sliding-window user picker — keeps a buffer of 100K active user IDs
    user_picker = WeightedUserPicker(pool, refresh_every=300_000)
    
    semaphore = asyncio.Semaphore(100)  # max in-flight order creations
    
    async def generate_one():
        async with semaphore:
            try:
                await create_one_order(pool, redis, user_picker, stores_by_city, promos)
            except Exception as e:
                METRICS.errors.inc()
                logger.exception('order_gen_failed')
    
    # Rate limiter: 50/sec → 1 every 20ms
    interval = 1.0 / config.orders_per_second
    while True:
        asyncio.create_task(generate_one())
        await asyncio.sleep(interval)
```

#### Order creation flow (`create_one_order`)

1. **Pick a user.** `user_picker.pick()` returns a user weighted by:
   - 60% by recency (recent users more likely to order)
   - 30% by activity tier (a tracked "heavy users" set in Redis)
   - 10% uniform random
   - **5% of picks come from a "very active" pool** of ~10K users who place 3-5 orders/day each — this gives the system realistic power-law order distribution
2. **Pick a store.** Filter stores by:
   - In user's primary city OR within 15km of user's default address
   - Currently within open hours (check `store_hours` for current day-of-week and Europe/London time, factoring `SIMULATION_TIME_COMPRESSION`)
   - `is_active = true`
   - Weight by: distance (closer = higher), price tier match to user's past preference (if any), cuisine match
3. **Pick order_type.** 75% DELIVERY, 20% PICKUP, 5% DINE_IN (only at stores with `accepts_in_store`)
4. **Pick channel.** Based on user's most-used device platform: iOS users → IOS_APP 80% / MOBILE_WEB 15% / DESKTOP_WEB 5%. Similar for Android. Some users always use web.
5. **Pick delivery address** (if DELIVERY): 80% user's default, 15% other saved address, 5% null (guest-ish edge case).
6. **Pick payment method:** 85% default, 10% other saved, 5% new card (simulates adding a card during checkout — for legit users, this is also a fraud signal that needs to NOT always be flagged).
7. **Pick device/IP:** 92% one of user's known devices, 8% new device (legit reason: new phone). IP: sampled from a UK-realistic ISP range matching user's city.
8. **Build cart** via `cart_builder`.
9. **Apply promo** if eligible. New user with `is_first_order_for_user=true` gets WELCOME10 with 80% probability. Otherwise random 5% chance of promo.
10. **Compute pricing.** Subtotal → VAT (via money helper) → delivery_fee (250-499 pence based on distance) → service_fee (10% of subtotal, capped at 250 pence) → tip (most UK users don't tip; 20% chance of tip, tip = round(subtotal × uniform(0.05, 0.15)) pence). Discount → total.
11. **Build snapshot** via `snapshot_builder`.
12. **INSERT order** with `order_status='PLACED'`, `placed_at=NOW()`. **INSERT order_items.** **INSERT order_event** (`ORDER_PLACED`).
13. **Call scoring service** if `SCORING_ENABLED=true` (Phase 6 wires this up; Phase 2 stub-checks an env flag and skips).
14. **NOTIFY** on PG channel `order_placed` with the order_id (used by Phase 4 feature aggregator).

#### Time compression

`SIMULATION_TIME_COMPRESSION=60` means 1 real second = 60 simulated seconds. The generator should:
- Use real `NOW()` for `placed_at` — orders happen in real time
- But the **lifecycle advancer** uses compression: a 30-minute simulated prep+delivery happens in 30 real seconds
- The store-hours check uses real wall-clock UK time (so the simulator naturally has more orders during real UK dinner hours unless you override)
- Optional `SIMULATION_FORCE_PEAK=true` env to ignore wall clock and always run at "Friday 7pm" load

#### Order number generation

Format: `JE-YYYY-XXXXXX` where XXXXXX is a base32 random. Must be unique. Use a Postgres sequence or just rely on UUID-derived randomness with retry on conflict.

#### Throughput target

50 orders/sec sustained. With async + connection pool, this should be comfortable. Monitor with structured logging every 1000 orders:
```
{"event": "throughput_report", "orders_1min": 3120, "errors_1min": 2, "avg_create_ms": 18.4}
```

### simulator/lifecycle.py

Separate async daemon. Polls for non-terminal orders every 5 real seconds (= 5 minutes simulated at compression=60). Advances them per these rules:

| From | To | Trigger (simulated time elapsed) | Probability |
|---|---|---|---|
| PLACED | ACCEPTED | 30-180s | 97% |
| PLACED | CANCELLED | any | 3% (cancelled_by=USER or MERCHANT) |
| ACCEPTED | PREPARING | immediate | 100% |
| PREPARING | READY | store.avg_prep_time_min * uniform(0.8, 1.2) | 98% |
| PREPARING | CANCELLED | any | 2% (cancelled_by=MERCHANT, "out of stock") |
| READY (DELIVERY) | PICKED_UP | 1-15 min | 99% |
| READY (PICKUP) | DELIVERED | 1-30 min (customer arrival) | 95% |
| READY (PICKUP) | CANCELLED | >60 min | 5% (no-show) |
| PICKED_UP | IN_TRANSIT | immediate | 100% |
| IN_TRANSIT | DELIVERED | distance × 2 min/km × uniform(0.8, 1.5) | 98% |
| IN_TRANSIT | FAILED | any | 2% (e.g. address wrong) |

Set `terminal_state_reached_at = NOW()` when reaching DELIVERED, CANCELLED, REFUNDED, FAILED. Insert appropriate `order_events` rows for each transition.

Assign a `driver_id` when reaching ACCEPTED for DELIVERY orders: pick driver in same city as store, sample weighted by `completed_deliveries` (active drivers more likely).

### Updated docker-compose.yml

Add services:

**simulator**
- Build: `./` 
- Command: `python -m simulator.generator`
- Depends on: postgres (healthy), redis (healthy)
- Restart: unless-stopped
- Environment: pulled from `.env`

**lifecycle**
- Same image as simulator
- Command: `python -m simulator.lifecycle`
- Depends on: postgres healthy
- Restart: unless-stopped

**Note:** `seed` is NOT a long-running service. Run it on-demand via `make seed` which does `docker-compose run --rm simulator python -m simulator.seed`.

### Updated Makefile

```make
seed:
	docker-compose run --rm simulator python -m simulator.seed --scale 1.0

seed-small:
	docker-compose run --rm simulator python -m simulator.seed --scale 0.1

start-simulation:
	docker-compose up -d simulator lifecycle archiver

stop-simulation:
	docker-compose stop simulator lifecycle
```

### Tests

**tests/test_simulator.py**
- `test_cart_builder`: builds 1000 carts, asserts realistic distribution (90% have ≥1 main, mean item count ~3)
- `test_vat_calculation`: hot food only → 20% VAT; mixed → correctly split; all cold → 0% VAT on items but VAT still on delivery/service fees
- `test_snapshot_builder`: for a brand new user, `is_first_order_for_user=true`, `user_total_orders_lifetime=0`; after one order, `is_first_order_for_user=false` on the next
- `test_order_uniqueness`: 100 concurrent order creations all get unique order_numbers
- `test_throughput`: run generator at 50 ord/sec for 60 seconds; assert ≥2800 orders created, ≥0 errors
- `test_store_hours_respected`: orders only placed at stores within current open hours

**tests/test_lifecycle.py**
- Insert 100 PLACED orders, run lifecycle for 30 simulated minutes; assert most reach DELIVERED, ~3% cancelled, terminal_state_reached_at populated
- Order events table has the full event chain for delivered orders

## Acceptance Criteria for Phase 2

- `make seed` completes full 1.0-scale seed in <15 minutes
- After seed, Postgres counts match the scale table above (±5%)
- `make start-simulation` brings up generator + lifecycle, generator hits 50 ord/sec within 30 seconds
- Run for 1 hour: ≥175,000 orders created, ≥98% of placed orders reach a terminal state within their expected lifecycle time
- VAT is correctly computed across thousands of orders (random audit: sum of line_total + delivery_fee + service_fee + tip - discount + vat = total)
- `user_total_orders_lifetime` snapshot field tracks correctly (a user with 10 prior orders shows 10 on their 11th order)
- No order has `total_pence < 0` or `subtotal_pence == 0`
- Archival continues to work — after 3 days of running, hot table size stays bounded
- All tests pass

## Out of Scope for Phase 2

- Fraud injection (Phase 3)
- Chargebacks (Phase 3)
- Ground truth table (Phase 3)
- Feature store (Phase 4)
- Calling the scoring service (the generator has a `SCORING_ENABLED` flag but it's `false` in Phase 2)
- ML training (Phase 5)
