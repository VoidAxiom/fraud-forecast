# PHASE 3: Fraud Injection & Chargebacks

## Goal of This Phase

Inject realistic, labeled fraudulent orders into the live simulation stream at a configurable rate (~2%). Build the seven fraud pattern generators with enough sophistication that they're not trivially separable from legitimate traffic. Create the ground truth table with strict access controls. Simulate the delayed chargeback feedback loop that ML training will rely on for labels.

## Prerequisites

- Phase 1 and 2 complete.
- Generator running and producing legit orders.
- `scoring_user` role exists (from Phase 1 Migration 003).

## Context Recap

Without realistic, labeled fraud data, no ML model can be trained. Real production fraud detection systems learn from chargebacks (which arrive 1-30 days after a fraudulent order). We simulate that loop. Critically: **the scoring system must never have access to the ground truth** — that would be label leakage in the most literal sense.

## Deliverables

1. `simulator/fraud_patterns.py` — 7 labeled fraud pattern generators
2. `simulator/ground_truth.py` — ground truth recording with access-controlled table
3. `simulator/chargebacks.py` — delayed chargeback generation daemon
4. `db/migrations/004_ground_truth.py` — creates `simulator_ground_truth` table and REVOKES `scoring_user` SELECT on it
5. Modified `simulator/generator.py` — calls fraud injection at `FRAUD_RATE` probability
6. Updated `.env.example` with fraud-related vars
7. `tests/test_fraud_patterns.py` — tests for each pattern + access control test

## The Seven Fraud Patterns

Each pattern is a function that takes the same inputs as `create_one_order` but produces an order with deliberately fraudulent characteristics. The order is otherwise indistinguishable from a legit order at the DB level — the same tables, the same flow. The only marker is in `simulator_ground_truth`.

**Distribution of fraud (when injecting):**
- 30% stolen_card
- 20% account_takeover
- 25% promo_abuse
- 10% refund_abuse
- 5% collusive_merchant
- 5% triangulation
- 5% reseller

**Difficulty calibration:** Each pattern should have a 60-90% "tell rate" — that is, a perfect model should be able to detect 60-90% of instances of that pattern, not 100%. Real fraud is messy. We achieve this by:
1. Sometimes making fraud orders look more legit (e.g. stolen_card with matching AVS — sophisticated fraudster who got the billing address too)
2. Sometimes making legit orders look more suspicious (handled in Phase 2 already: new device, new address, etc.)

### Pattern 1: Stolen Card / Card-Not-Present Fraud (30% of fraud)

**Scenario:** Fraudster has stolen card details from a data breach. They create a new account or use a low-value account, then place a high-value order, often to a hotel or commercial address (so they don't expose their own home).

**Generation logic:**
```python
def generate_stolen_card_fraud(...) -> tuple[Order, GroundTruth]:
    # Variant A (60%): New account, brand new
    # Variant B (30%): Existing low-activity account being used by attacker
    # Variant C (10%): Sophisticated — established account, but new card from foreign country
    
    if variant == 'A':
        user = create_brand_new_user(account_age_hours=uniform(1, 48))
        # New user, never ordered before, using a "new card"
    elif variant == 'B':
        user = pick_low_activity_user(max_orders_lifetime=2)
    else:
        user = pick_established_user(min_orders_lifetime=10)
    
    # Card characteristics — strong signal
    card = generate_card(
        country=weighted_choice([('GB', 0.35), ('US', 0.20), ('RU', 0.10), ('NG', 0.08),
                                 ('IN', 0.07), ('CN', 0.05), ('foreign_other', 0.15)]),
        funding_type=weighted_choice([('CREDIT', 0.6), ('PREPAID', 0.3), ('DEBIT', 0.1)]),
        is_digital_native_bank=False  # almost never digital-native for this pattern
    )
    
    # AVS/CVV: 65% mismatch (clear fraud); 35% match (sophisticated)
    avs = 'NO_MATCH' if random() < 0.65 else 'MATCH'
    cvv = 'NO_MATCH' if random() < 0.40 else 'MATCH'
    
    # Order: usually high value
    order_total_pence = int(normal(mean=6500, sigma=2500))  # avg £65 vs platform avg ~£25
    order_total_pence = max(2000, order_total_pence)
    
    # Delivery: 50% to hotel/commercial, 30% to a residential they don't live at, 
    # 20% to user's own address (sloppy fraud)
    address_type_dist = [('HOTEL', 0.30), ('COMMERCIAL', 0.20), 
                          ('RESIDENTIAL_NEW', 0.30), ('RESIDENTIAL_USER', 0.20)]
    
    # Device: new device 80%, user's existing device 20% (sloppy)
    # IP: 60% UK, 25% VPN/proxy, 15% direct foreign
    ip = pick_ip(uk_pct=0.60, vpn_pct=0.25, foreign_pct=0.15)
    
    # Time: 40% of stolen_card happens 2am-6am UK time (low monitoring)
    
    # Cart: 60% high-end items (steaks, premium combos); 40% normal
    
    return order, GroundTruth(is_fraud=True, fraud_category='stolen_card',
                              pattern_notes=f"variant={variant}, avs={avs}")
```

### Pattern 2: Account Takeover (ATO) (20%)

**Scenario:** Attacker has compromised an established trusted user's account (via credential stuffing or phishing). Trusted account = no model flag on the user side. But the order pattern shifts dramatically.

**Generation logic:**
```python
def generate_account_takeover_fraud(...):
    # Pick an established TRUSTED user
    user = pick_user(risk_tier='TRUSTED', min_orders=10)
    
    # New device — almost always
    device = create_brand_new_device(platform=different_from_user_history)
    
    # IP: 80% foreign (different country from user's history), 20% UK but different city
    ip_country = weighted_choice([('NG', 0.15), ('RU', 0.12), ('CN', 0.10), ('US', 0.10),
                                   ('VN', 0.08), ('PK', 0.08), ('UA', 0.07), 
                                   ('GB_different_city', 0.20), ('other', 0.10)])
    
    # Payment: 70% existing saved card (the real victim's card); 30% new card added during attack
    
    # Delivery address: new address (the attacker's drop point) — 90%
    
    # Order timing: 70% within 24h of an unusual login event (we don't simulate the login,
    # but the device/IP change at order time IS the unusual event)
    
    # Order value: bimodal — 50% normal value (testing the account), 50% high value (cashing out)
    
    # 30% of ATO orders happen within 48h of a "real" order by the legit user — meaning the
    # legit user might dispute it fast. The fraud_outcome reflects this.
    
    return order, GroundTruth(is_fraud=True, fraud_category='account_takeover',
                              pattern_notes=f"victim_user_id={user.user_id}, "
                                            f"new_device, ip_country={ip_country}")
```

### Pattern 3: Promo Abuse (25%)

**Scenario:** One person creates many fake accounts to repeatedly redeem new-user promotions. Usually same device fingerprint, same payment method (or rotation through prepaid cards), same delivery address (or addresses very close together).

**Generation logic:**
```python
class PromoAbuseRing:
    """Maintains state for a single promo abuse ring across many orders."""
    def __init__(self):
        self.ring_id = uuid4()
        self.device = create_shared_device()  # same device across all ring accounts
        self.base_address = generate_address(city='London')  # close-by deliveries
        self.payment_pool = generate_prepaid_cards(n=randint(3, 8))
        self.email_pattern = pick_disposable_or_aliased_pattern()  
            # e.g. "ringfraud{N}@mailinator.com" or "real.name+{N}@gmail.com" (gmail aliasing)
        self.created_users = []
    
    def generate_next_order(self):
        # Create or pick a brand new user in the ring
        if len(self.created_users) < 30 and random() < 0.7:
            user = create_brand_new_user_in_ring(self)
        else:
            user = random.choice(self.created_users)  # reuse, less common
        
        # ALWAYS use ring's device
        device = self.device
        
        # IP: usually same ISP/range as previous orders (but vary the exact IP)
        ip = same_subnet_as_ring(self)
        
        # Payment: rotate through ring's prepaid pool
        payment = random.choice(self.payment_pool)
        
        # Delivery: jitter around base_address (within 500m) — same building/street
        delivery = jitter(self.base_address, max_meters=500)
        
        # ORDER MUST USE THE NEW USER PROMO
        promo = WELCOME10  # always
        
        # Order value: just above min_order to maximise discount %
        order_total = WELCOME10.min_order_pence + uniform(0, 500)

# In simulator setup, create 50 active promo abuse rings; each generates ~5-30 orders over the simulation lifetime
PROMO_ABUSE_RINGS: list[PromoAbuseRing] = []
```

**Tell rate target: 80%+** — promo abuse is genuinely easy to detect with simple velocity rules and that's realistic; production systems catch most of it.

### Pattern 4: Refund / Friendly Fraud Abuse (10%)

**Scenario:** Established user who has learned that complaining post-delivery gets a refund or credit. Repeated pattern.

**Generation logic:**
```python
def generate_refund_abuse_fraud(...):
    # Pick a user from a maintained pool of ~500 "refund abusers"
    user = pick_refund_abuser()  # users marked with this trait at seed time
    
    # Order itself is COMPLETELY NORMAL — same device, same payment, same address
    # The fraud only manifests post-delivery as a refund complaint
    
    # The signal is in the user's history:
    # - user_refunds_lifetime is elevated (3-10)
    # - user_chargebacks_lifetime sometimes elevated
    # - order value is often above their average
    # - sometimes orders are placed at stores with weak refund-dispute defense (low ratings)
    
    return order, GroundTruth(is_fraud=True, fraud_category='refund_abuse', ...)
```

Critically, this pattern requires the **seed phase to mark certain users** with elevated `user_refunds_lifetime` (3-10) so they have a history when their first refund_abuse order is generated. Add to `seed.py`: select 500 random users post-seed and set `refunds_lifetime` to a higher value.

The chargeback / refund itself is generated in `chargebacks.py` (Phase 3).

**Tell rate target: ~50-60%** — this is hard to detect from a single order; the signal is mostly historical and behavioural.

### Pattern 5: Collusive Merchant (5%)

**Scenario:** A small set of merchants are in on a card-cycling scheme. Stolen cards are run through legitimate-looking orders at these merchants; the merchant gets paid; merchant kicks back to fraudster.

**Generation logic:**
```python
# At simulator startup, mark 10 specific stores as "collusive" in a Python set
COLLUSIVE_STORES: set[UUID] = mark_random_stores(n=10, after_seed=True)

def generate_collusive_merchant_fraud(...):
    store = random.choice(list(COLLUSIVE_STORES))
    
    # User: usually new account or low activity (mule accounts)
    user = create_or_pick_low_activity_user()
    
    # Card: stolen — same pattern as stolen_card (foreign, often prepaid)
    card = generate_stolen_pattern_card()
    
    # Order: very normal looking AT the store level (the merchant makes the order look real)
    # - Items: realistic mix
    # - Order value: middle of normal range for that store (not flagged-high)
    # - AVS/CVV: usually MATCH (merchant ignores mismatches)
    
    # Multiple cards used at the same store in a short window → key signal
    # We achieve this by always picking a collusive store, so over time the store has 
    # a much higher rate of new-card / new-user orders than legit stores
    
    return order, GroundTruth(is_fraud=True, fraud_category='collusive_merchant',
                              pattern_notes=f"store_id={store.store_id}")
```

### Pattern 6: Triangulation Fraud (5%)

**Scenario:** Fraudster sells food orders cheaply on a side channel (e.g. Telegram, social media). Customer pays fraudster directly, fraudster places real order to customer's address using a stolen card. Customer gets food (and feels they got a deal), fraudster gets cash, victim of card theft sees a charge for food they never ordered.

**Generation logic:**
```python
def generate_triangulation_fraud(...):
    # Card: stolen pattern
    card = generate_stolen_pattern_card()
    
    # User: usually a "fraudster account" — moderate age, multiple addresses used historically,
    # multiple cards used historically
    user = pick_or_create_triangulation_account()
    
    # Delivery: to a NEW residential address (the customer who paid the fraudster)
    delivery = generate_new_uk_residential_address()
    
    # Order: very normal looking — fraudster is delivering real food, knows the customer wants it
    # Value: realistic, not suspiciously high
    
    # Device: usually fraudster's own device (consistent across triangulation orders)
    # The signal is: account places orders to many different new addresses with many different cards
    
    return order, GroundTruth(is_fraud=True, fraud_category='triangulation', ...)
```

### Pattern 7: Reseller / Bulk Ordering (5%)

**Scenario:** Someone running an unauthorised secondary delivery service. Places large bulk orders, frequently, to the same address.

**Generation logic:**
```python
def generate_reseller_fraud(...):
    # User: established account, often slightly elevated risk
    user = pick_or_create_reseller_account()  # maintain a pool of ~50
    
    # Item count: VERY HIGH — 10-25 items
    cart = build_bulk_cart(min_items=10, max_items=25)
    
    # Delivery address: ALWAYS same as user's other reseller orders
    delivery = user.reseller_address
    
    # Order frequency: this user places 3-8 orders per day, often from the same store
    
    # Payment: legit user's own card usually — they're not committing payment fraud per se,
    # they're violating platform ToS. But this is still "fraud" from the platform's POV
    # because they're abusing pricing.
    
    return order, GroundTruth(is_fraud=True, fraud_category='reseller', ...)
```

**Note:** This is the "softest" fraud category — borderline ToS violation. The chargeback for these is rare (these users don't dispute their own charges). The label comes via a different path: platform fraud team investigation. In simulation, we mark these as fraud in ground truth but generate chargebacks for only 10% of them.

### Cross-cutting: "near misses" and "noisy positives"

To make the dataset realistic and the modelling problem non-trivial:

- **5% of legit orders should look slightly suspicious:** new device, new payment method, new address, foreign card. These are the false-positive risks the model must learn to navigate. The Phase 2 generator already produces some of these naturally; ensure rates roughly match real world.
- **15% of fraud orders should look entirely normal:** all signals match user history, no obvious tells. These represent the hard fraud the model will miss. This is realistic — real fraud teams catch ~80-90%, not 100%.

## simulator/ground_truth.py

```python
class GroundTruth(BaseModel):
    order_id: UUID
    is_fraud: bool
    fraud_category: str | None   # 'stolen_card', 'account_takeover', etc., or None for legit
    pattern_notes: str | None    # debugging info
    ring_id: UUID | None         # for promo_abuse, collusive_merchant — group identifier

async def record_ground_truth(pool, gt: GroundTruth):
    """Insert into simulator_ground_truth using simulator role."""
```

Every order (legit or fraud) creates a ground_truth row. Legit orders get `is_fraud=False, fraud_category=None`. This matters because Phase 5 training uses ground truth as labels (legitimately — training has access).

## Migration 004: Ground Truth Table

```sql
CREATE TABLE simulator_ground_truth (
    order_id        UUID PRIMARY KEY,
    is_fraud        BOOLEAN NOT NULL,
    fraud_category  VARCHAR(50),
    pattern_notes   TEXT,
    ring_id         UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_gt_fraud ON simulator_ground_truth(is_fraud);
CREATE INDEX idx_gt_category ON simulator_ground_truth(fraud_category);
CREATE INDEX idx_gt_ring ON simulator_ground_truth(ring_id);

-- CRITICAL: scoring_user must NOT have access
REVOKE ALL ON simulator_ground_truth FROM scoring_user;
REVOKE SELECT ON simulator_ground_truth FROM PUBLIC;

-- simulator_user (writes) and analyst_user (reads for dashboard) keep access
GRANT INSERT ON simulator_ground_truth TO simulator_user;
GRANT SELECT ON simulator_ground_truth TO analyst_user;
-- Phase 5 training uses a dedicated training_user; grant when that role is created
```

**Verify this:** the test suite must include a test that connects as `scoring_user` and confirms `SELECT * FROM simulator_ground_truth` raises permission denied. This is non-negotiable — it's the integrity guarantee of the whole system.

## simulator/chargebacks.py

Daemon. Runs every hour (configurable). For each newly-DELIVERED order in the last 90 days that doesn't already have a chargeback decision:

```python
async def maybe_generate_chargeback(order, ground_truth):
    delivered_age_hours = (now - order.delivered_at).total_seconds() / 3600
    
    if ground_truth.is_fraud:
        if ground_truth.fraud_category == 'reseller':
            chargeback_probability = 0.10  # most reseller fraud doesn't chargeback
        elif ground_truth.fraud_category == 'refund_abuse':
            chargeback_probability = 0.30  # user complains for refund, sometimes escalates
        elif ground_truth.fraud_category == 'promo_abuse':
            chargeback_probability = 0.05  # promo abuse rarely chargebacks (fraudster keeps it)
        else:
            chargeback_probability = 0.60  # stolen_card / ATO / triangulation / collusive
    else:
        chargeback_probability = 0.002  # legit baseline (genuine disputes)
    
    # Chargeback timing: lognormal distribution
    # If is_fraud: mean 14 days, sigma 0.7 (range mostly 5-45 days)
    # If legit: mean 30 days, sigma 0.8 (slower, often weeks later)
    
    # Use a deterministic seed based on order_id so re-runs are consistent
    ...
    
    if should_chargeback_now(delivered_age_hours, chargeback_probability, timing):
        INSERT INTO chargebacks (...)
        UPDATE orders SET chargeback_received_at=NOW(), 
                          chargeback_amount_pence=order.total_pence,
                          fraud_outcome='CHARGEBACK' if is_fraud else 'LEGIT'
        -- and also UPDATE orders_archive if order is in cold storage
```

**Important:** Chargebacks can arrive for orders that have already moved to `orders_archive`. The chargeback logic must check both tables.

For orders past 60 days post-delivery without a chargeback: set `fraud_outcome='LEGIT'` so the label is finalised. This is what training will use as the negative class.

Also: handle **refund_abuse** specially. These don't always result in a card chargeback; instead, a "refund event" is logged. Add to migration 004:

```sql
CREATE TABLE refunds (
    refund_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id         UUID NOT NULL,
    order_placed_at  TIMESTAMPTZ NOT NULL,
    amount_pence     BIGINT NOT NULL,
    reason           VARCHAR(100),
    initiated_by     VARCHAR(20),   -- USER, MERCHANT, SYSTEM
    issued_at        TIMESTAMPTZ NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

For refund_abuse fraud: generate a `refund` row at 0-5 days post delivery with `initiated_by=USER`.

## Modified simulator/generator.py

In `create_one_order`:

```python
async def create_one_order(...):
    if random() < config.FRAUD_RATE:
        order, ground_truth = await fraud_patterns.generate_fraud_order(...)
    else:
        order, ground_truth = await generate_legit_order(...)  # existing Phase 2 path
    
    async with pool.acquire() as conn:
        async with conn.transaction():
            await insert_order(conn, order)
            await insert_order_items(conn, order)
            await insert_order_events(conn, order)
            await record_ground_truth(conn, ground_truth)  # NEW
    
    if config.SCORING_ENABLED:
        await call_scoring_service(order)
```

The fraud pattern picker:
```python
async def generate_fraud_order(...) -> tuple[Order, GroundTruth]:
    pattern = weighted_choice([
        ('stolen_card', 0.30),
        ('account_takeover', 0.20),
        ('promo_abuse', 0.25),
        ('refund_abuse', 0.10),
        ('collusive_merchant', 0.05),
        ('triangulation', 0.05),
        ('reseller', 0.05),
    ])
    return await PATTERN_HANDLERS[pattern](...)
```

## State management for stateful fraud patterns

Several patterns require state between orders (promo abuse rings, collusive merchants, reseller accounts, refund abusers). Store this state in:

1. **Redis** (live state, accessed by every fraud-generating order):
   - `fraud:promo_rings` set of ring IDs
   - `fraud:promo_ring:{ring_id}` hash with device_id, address, payment_pool, user_ids
   - `fraud:collusive_stores` set of store IDs
   - `fraud:reseller_accounts` set of user IDs
   - `fraud:refund_abusers` set of user IDs
   - `fraud:triangulation_accounts` set of user IDs
2. **Postgres** (persistent across restarts): use a `simulator_state` table:
   ```sql
   CREATE TABLE simulator_state (
       key   VARCHAR(100) PRIMARY KEY,
       value JSONB NOT NULL,
       updated_at TIMESTAMPTZ DEFAULT NOW()
   );
   ```

On simulator startup, load state from Postgres, mirror to Redis. Snapshot back to Postgres every 5 minutes.

Initialise rings/accounts during seed if they don't exist. Defaults:
- 50 promo abuse rings
- 10 collusive stores
- 50 reseller accounts
- 500 refund abuser flagged users
- 30 triangulation accounts

## Updated .env.example

```
# Phase 3 additions
FRAUD_RATE=0.02
FRAUD_PROMO_ABUSE_RINGS=50
FRAUD_COLLUSIVE_STORES=10
FRAUD_RESELLER_ACCOUNTS=50
FRAUD_REFUND_ABUSERS=500
FRAUD_TRIANGULATION_ACCOUNTS=30
CHARGEBACK_DAEMON_INTERVAL_MIN=60
```

## Tests

**tests/test_fraud_patterns.py**

For each of 7 patterns:
- `test_<pattern>_signals_present`: generate 100 instances, assert expected signal distribution. E.g. for stolen_card: ≥60% have `avs_result=NO_MATCH`, ≥70% have `card_issuer_country != 'GB'`, mean order_total ≥ £50.
- `test_<pattern>_ground_truth_recorded`: every generated fraud order has matching `simulator_ground_truth` row.

Cross-pattern:
- `test_fraud_distribution`: generate 1000 fraud orders, assert distribution matches the 30/20/25/10/5/5/5 split (within ±3%).
- `test_promo_abuse_ring_consistency`: orders in the same ring share device, similar address, payment pool.
- `test_collusive_store_concentration`: over 1000 orders, the 10 collusive stores receive ≥10x the fraud rate of normal stores.

Critical access control:
- `test_scoring_user_cannot_read_ground_truth`: connect as `scoring_user`, attempt SELECT on `simulator_ground_truth`, assert `psycopg2.errors.InsufficientPrivilege` raised.
- `test_scoring_user_cannot_join_to_ground_truth`: attempt `SELECT o.* FROM orders o JOIN simulator_ground_truth gt USING (order_id)`, assert error.

Chargebacks:
- `test_chargeback_rates`: simulate 10000 delivered orders (mix of fraud and legit), run chargeback daemon, assert overall chargeback rate is ~1.5% (within ±0.5%), per-category rates match expected (60% for stolen_card etc.).
- `test_chargeback_timing`: chargebacks distributed across 1-45 days as expected.
- `test_chargeback_on_archived_order`: archive a fraudulent order, then trigger chargeback, assert it correctly updates `orders_archive`.

## Acceptance Criteria for Phase 3

- Run the simulator for 4 hours; observe `simulator_ground_truth` distribution: ~2% is_fraud=true, broken down approximately as specified
- Run the chargeback daemon for a simulated 30 days; ~1.5% of all orders have chargebacks; fraud orders dominate the chargeback set (>80% of chargebacks are on `is_fraud=true` orders)
- All 7 fraud patterns produce distinguishable but non-trivial signal — measure: a quick logistic regression on a few obvious features (`avs_result`, `card_issuer_country`, `account_age_days`, `device_user_count`) achieves at least AUPRC 0.4 (means fraud is detectable) but at most AUPRC 0.7 (means it's still a hard problem worth ML for)
- Scoring user CANNOT query ground truth (verified by test)
- All tests in `tests/test_fraud_patterns.py` pass
- After Phase 3, the database is producing the training data Phase 5 will use

## Out of Scope for Phase 3

- Feature store (Phase 4)
- ML training (Phase 5)
- Scoring service (Phase 6)
- Dashboard (Phase 7)
