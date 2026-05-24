# PHASE 4: Feature Store (Streaming + Batch)

## Goal of This Phase

Build the Redis-backed feature store that the scoring service (Phase 6) reads from at request time. Two layers:

1. **Streaming features:** updated on every new order (velocity, recency). Updated within seconds of an order being placed.
2. **Batch features:** computed daily from `orders_archive` (lifetime aggregates, historical rates). Updated overnight.

Real-time lookups must return all features for a given order context in **<10ms p99**.

## Prerequisites

- Phases 1-3 complete.
- Simulator generating orders (legit + fraud).
- Redis available.

## Why a Feature Store?

The scoring service needs features like "user's order count in the last 1 hour" and "device's lifetime fraud rate" at request time, with <100ms total budget. Computing these from Postgres on every request is too slow. The feature store pre-computes them and serves from Redis.

In real production: you'd use Feast, Tecton, or Vertex AI Feature Store. For this build, we implement the same patterns directly on Redis — gives a transparent understanding of the mechanism.

## Deliverables

1. `feature_store/aggregator.py` — long-running daemon, updates streaming features
2. `feature_store/batch_compute.py` — daily job, computes batch features
3. `feature_store/client.py` — synchronous + async API used by scoring service
4. `feature_store/schema.py` — Pydantic models defining the FeatureSet contract
5. Modified `simulator/generator.py` — emits `pg_notify('order_placed', order_id::text)` after insert (if not already from Phase 2)
6. Updated `docker-compose.yml` — adds `feature_aggregator` and `feature_batch` services
7. `tests/test_feature_store.py`

## Feature Catalog

### Streaming features (updated on every new order)

**User velocity:**
- `user_orders_1h` — count in last 60 min
- `user_orders_24h` — count in last 24 hours
- `user_spend_1h_pence` — sum of total in last 60 min
- `user_spend_24h_pence` — sum of total in last 24 hours
- `user_unique_stores_24h` — distinct store_id count
- `user_unique_payment_methods_24h`
- `user_last_order_age_minutes` — minutes since previous order

**Device velocity:**
- `device_orders_1h`
- `device_orders_24h`
- `device_unique_users_24h`
- `device_unique_payment_methods_24h`

**Payment method velocity:**
- `payment_orders_1h`
- `payment_orders_24h`
- `payment_unique_users_24h`
- `payment_decline_count_24h` (when Phase 6 records declines)

**IP velocity:**
- `ip_orders_1h`
- `ip_orders_24h`
- `ip_unique_users_24h`
- `ip_unique_devices_24h`

**Store velocity:**
- `store_orders_1h`
- `store_orders_24h`
- `store_unique_users_24h`
- `store_unique_cards_1h` — early signal of carding attack on a store

**Delivery address velocity:**
- `address_orders_24h`
- `address_unique_users_24h`

### Batch features (updated daily)

**User historical:**
- `user_lifetime_order_count`
- `user_lifetime_spend_pence`
- `user_avg_order_value_pence`
- `user_lifetime_chargeback_count`
- `user_lifetime_refund_count`
- `user_lifetime_chargeback_rate` (chargebacks / orders)
- `user_unique_devices_used`
- `user_unique_payment_methods_used`
- `user_unique_delivery_addresses`
- `user_account_age_days`
- `user_days_since_last_order`
- `user_distinct_cities_ordered_from`

**Device historical:**
- `device_lifetime_order_count`
- `device_lifetime_chargeback_rate`
- `device_unique_users_lifetime`
- `device_first_seen_days_ago`
- `device_distinct_payment_methods_lifetime`

**Payment method historical:**
- `payment_lifetime_order_count`
- `payment_lifetime_chargeback_count`
- `payment_lifetime_chargeback_rate`
- `payment_unique_users_lifetime`
- `payment_distinct_delivery_addresses_lifetime`

**IP historical:**
- `ip_lifetime_order_count`
- `ip_unique_users_lifetime`
- `ip_chargeback_rate`
- `ip_first_seen_days_ago`

**Store historical:**
- `store_avg_order_value_pence`
- `store_chargeback_rate`
- `store_unique_cards_30d`
- `store_total_orders_30d`

**Merchant historical:**
- `merchant_chargeback_rate`
- `merchant_total_stores`

**Email domain risk:**
- `email_domain_chargeback_rate` (rolling 90d, computed per domain)
- `email_domain_total_orders` (rolling 90d)

## Redis Key Schema

Use a consistent prefix scheme. All keys namespaced under `fs:` (feature store).

| Key pattern | Type | TTL | Description |
|---|---|---|---|
| `fs:user:{user_id}:stream` | hash | 86400s | All user streaming features |
| `fs:user:{user_id}:orders_zset` | sorted set | 86400s | `score=unix_ts, member=order_id` — for sliding window counts |
| `fs:user:{user_id}:stores_zset` | sorted set | 86400s | for unique store counts |
| `fs:user:{user_id}:payments_zset` | sorted set | 86400s | |
| `fs:user:{user_id}:batch` | hash | 172800s (refreshed daily) | All user batch features |
| `fs:device:{device_id}:stream` | hash | 86400s | |
| `fs:device:{device_id}:orders_zset` | sorted set | 86400s | |
| `fs:device:{device_id}:users_zset` | sorted set | 86400s | |
| `fs:device:{device_id}:batch` | hash | 172800s | |
| `fs:payment:{pm_id}:stream` | hash | 86400s | |
| `fs:payment:{pm_id}:orders_zset` | sorted set | 86400s | |
| `fs:payment:{pm_id}:batch` | hash | 172800s | |
| `fs:ip:{ip}:stream` | hash | 86400s | |
| `fs:ip:{ip}:orders_zset` | sorted set | 86400s | |
| `fs:ip:{ip}:batch` | hash | 172800s | |
| `fs:store:{store_id}:stream` | hash | 86400s | |
| `fs:store:{store_id}:orders_zset` | sorted set | 86400s | |
| `fs:store:{store_id}:batch` | hash | 172800s | |
| `fs:merchant:{merchant_id}:batch` | hash | 172800s | |
| `fs:email_domain:{domain}:batch` | hash | 172800s | |
| `fs:address:{addr_id}:stream` | hash | 86400s | |

## feature_store/aggregator.py

Async daemon. Listens on Postgres NOTIFY channel `order_placed`. For each notification, updates all relevant streaming features.

```python
async def main():
    pool = await asyncpg.create_pool(DATABASE_URL_APP, min_size=5, max_size=20)
    redis = await aioredis.from_url(REDIS_URL)
    
    listen_conn = await asyncpg.connect(DATABASE_URL_APP)
    await listen_conn.add_listener('order_placed', on_order_placed)
    
    async def on_order_placed(connection, pid, channel, payload):
        order_id = UUID(payload)
        try:
            await update_features_for_order(pool, redis, order_id)
        except Exception:
            logger.exception('feature_update_failed', order_id=str(order_id))
            METRICS.errors.inc()
    
    # Heartbeat + window cleanup task (every 60s)
    asyncio.create_task(cleanup_loop(redis))
    
    while True:
        await asyncio.sleep(3600)  # listener is event-driven
```

Backup polling mode in case NOTIFY is missed (Postgres can drop notifications under heavy load): every 30 seconds, query for orders placed in the last 60 seconds that aren't yet in `fs:processed_orders` (a Redis set with 600s TTL), and process them.

#### update_features_for_order

```python
async def update_features_for_order(pool, redis, order_id):
    # Fetch order with one query (already in hot table since it was just inserted)
    order = await pool.fetchrow("""
        SELECT order_id, user_id, store_id, merchant_id, device_id, ip_address, 
               payment_method_id, delivery_address_id, total_pence, placed_at,
               user_email_domain
        FROM orders WHERE order_id = $1
    """, order_id)
    
    now_ts = int(time.time())
    cutoff_1h = now_ts - 3600
    cutoff_24h = now_ts - 86400
    
    # Use a single pipeline for atomicity and speed
    async with redis.pipeline() as pipe:
        # === USER ===
        u_key = f"fs:user:{order['user_id']}"
        pipe.zadd(f"{u_key}:orders_zset", {str(order_id): now_ts})
        pipe.zremrangebyscore(f"{u_key}:orders_zset", 0, cutoff_24h)
        pipe.zadd(f"{u_key}:stores_zset", {str(order['store_id']): now_ts})
        pipe.zremrangebyscore(f"{u_key}:stores_zset", 0, cutoff_24h)
        if order['payment_method_id']:
            pipe.zadd(f"{u_key}:payments_zset", 
                      {str(order['payment_method_id']): now_ts})
        # ... etc
        
        # === DEVICE === (similar)
        # === PAYMENT === (similar)
        # === IP === (similar)
        # === STORE === (similar)
        # === ADDRESS === (similar)
        
        await pipe.execute()
    
    # Compute aggregates and write to :stream hashes (could be lazy via get-time computation,
    # but we eagerly write so reads are O(1) hash lookup)
    await write_user_stream_aggregates(redis, order['user_id'], now_ts)
    await write_device_stream_aggregates(redis, order['device_id'], now_ts)
    # ... etc
```

#### Stream aggregate computation

```python
async def write_user_stream_aggregates(redis, user_id, now_ts):
    u = f"fs:user:{user_id}"
    cutoff_1h = now_ts - 3600
    cutoff_24h = now_ts - 86400
    
    async with redis.pipeline() as pipe:
        pipe.zcount(f"{u}:orders_zset", cutoff_1h, '+inf')
        pipe.zcount(f"{u}:orders_zset", cutoff_24h, '+inf')
        pipe.zcount(f"{u}:stores_zset", cutoff_24h, '+inf')
        pipe.zcount(f"{u}:payments_zset", cutoff_24h, '+inf')
        results = await pipe.execute()
    
    orders_1h, orders_24h, unique_stores_24h, unique_payments_24h = results
    
    # Spend aggregates need a separate zset where score=timestamp and member encodes spend
    # OR a separate scheme. Simpler: maintain a parallel zset where member='{order_id}:{pence}'.
    # Easiest: store spend_1h directly via INCRBY on a hash, decrement via a TTL-based scheme,
    # OR query Postgres for spend (slower but simpler).
    # 
    # Recommended: maintain rolling-window sums via a second sorted set keyed by spend:
    # zadd fs:user:{uid}:spend_zset score=ts member='{order_id}:{pence}'
    # Then on read: ZRANGEBYSCORE → parse members → sum the pence.
    # Cap members at e.g. last 1000 orders per key to bound memory.
    
    await redis.hset(f"{u}:stream", mapping={
        'orders_1h': orders_1h,
        'orders_24h': orders_24h,
        'unique_stores_24h': unique_stores_24h,
        'unique_payments_24h': unique_payments_24h,
        # spend, last_order_age, etc.
        'updated_at': now_ts,
    })
    await redis.expire(f"{u}:stream", 86400)
```

#### Cleanup loop

Every 60 seconds, scan a sample of stream keys and trim the underlying zsets to last 24h. This keeps Redis memory bounded at scale (at 50 ord/sec × 86400 = 4.3M orders/day, the device/IP/payment zsets can grow rapidly for high-traffic entities).

Bound: `LIMIT 5000 zsets per cleanup pass`. Use `SCAN` with `MATCH 'fs:*:orders_zset'`.

## feature_store/batch_compute.py

Daily job. Schedule: 02:00 Europe/London via APScheduler in its own container. Reads from `orders_archive` (and recent `orders` for the most recent 48h) using SQLAlchemy + Postgres.

```python
def run_batch():
    logger.info('batch_compute_start')
    t0 = time.time()
    
    compute_user_batch_features()       # ~30M reads from archive
    compute_device_batch_features()
    compute_payment_batch_features()
    compute_ip_batch_features()
    compute_store_batch_features()
    compute_merchant_batch_features()
    compute_email_domain_batch_features()
    
    logger.info('batch_compute_done', duration_s=time.time() - t0)
```

#### Example: user batch features

```sql
WITH user_stats AS (
    SELECT 
        user_id,
        COUNT(*) as lifetime_order_count,
        SUM(total_pence) as lifetime_spend_pence,
        AVG(total_pence)::BIGINT as avg_order_value_pence,
        COUNT(DISTINCT device_id) as unique_devices_used,
        COUNT(DISTINCT payment_method_id) as unique_payment_methods_used,
        COUNT(DISTINCT delivery_address_id) as unique_delivery_addresses,
        COUNT(DISTINCT store_city) as distinct_cities_ordered_from,
        MAX(placed_at) as last_order_at
    FROM (
        SELECT * FROM orders WHERE placed_at > NOW() - INTERVAL '2 days'
        UNION ALL
        SELECT * FROM orders_archive
    ) all_orders
    GROUP BY user_id
),
user_chargebacks AS (
    SELECT user_id, COUNT(*) as cb_count
    FROM chargebacks cb JOIN ... 
    GROUP BY user_id
)
-- Write to Redis in batches via Python pipeline
```

Execute in chunks of 10K users at a time to avoid massive single transactions. Use server-side cursors (`itersize=10000`).

For each user, write to Redis:
```python
await redis.hset(f"fs:user:{uid}:batch", mapping={
    'lifetime_order_count': ...,
    'lifetime_spend_pence': ...,
    ...
})
await redis.expire(f"fs:user:{uid}:batch", 172800)  # 2 days, refreshed daily
```

Expected runtime at 1M users: 30-60 minutes. Parallelise across multiple workers if needed.

## feature_store/client.py

Synchronous and async APIs.

```python
@dataclass
class FeatureSet:
    """Complete feature set for one scoring request."""
    # User
    user_account_age_days: int | None
    user_lifetime_order_count: int | None
    user_lifetime_chargeback_rate: float | None
    user_orders_1h: int
    user_orders_24h: int
    user_spend_1h_pence: int
    user_spend_24h_pence: int
    user_unique_stores_24h: int
    user_last_order_age_minutes: int | None
    # ... all features
    # Device
    device_lifetime_order_count: int | None
    device_unique_users_lifetime: int | None
    device_orders_1h: int
    # ... etc
    # ... payment, ip, store, merchant, address, email_domain
    
    # Meta
    feature_fetch_latency_ms: float
    missing_features: list[str]   # which features fell back to defaults

class FeatureStoreClient:
    def __init__(self, redis_url: str):
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True)
    
    def get_features(
        self,
        user_id: UUID,
        device_id: UUID | None,
        payment_method_id: UUID | None,
        ip_address: str,
        store_id: UUID,
        merchant_id: UUID,
        delivery_address_id: UUID | None,
        email_domain: str,
    ) -> FeatureSet:
        """Fetches all features in one pipelined call. Target: <10ms."""
        t0 = time.perf_counter()
        
        pipe = self.redis.pipeline(transaction=False)
        pipe.hgetall(f"fs:user:{user_id}:stream")
        pipe.hgetall(f"fs:user:{user_id}:batch")
        if device_id:
            pipe.hgetall(f"fs:device:{device_id}:stream")
            pipe.hgetall(f"fs:device:{device_id}:batch")
        if payment_method_id:
            pipe.hgetall(f"fs:payment:{payment_method_id}:stream")
            pipe.hgetall(f"fs:payment:{payment_method_id}:batch")
        pipe.hgetall(f"fs:ip:{ip_address}:stream")
        pipe.hgetall(f"fs:ip:{ip_address}:batch")
        pipe.hgetall(f"fs:store:{store_id}:stream")
        pipe.hgetall(f"fs:store:{store_id}:batch")
        pipe.hgetall(f"fs:merchant:{merchant_id}:batch")
        pipe.hgetall(f"fs:email_domain:{email_domain}:batch")
        if delivery_address_id:
            pipe.hgetall(f"fs:address:{delivery_address_id}:stream")
        
        results = pipe.execute()
        
        # Stitch results into FeatureSet, applying defaults for missing features
        fs = build_feature_set(results)
        fs.feature_fetch_latency_ms = (time.perf_counter() - t0) * 1000
        return fs
```

**Default values for missing features:** This matters a lot. If a brand-new user has no batch features yet, the client should NOT raise — it should return reasonable defaults that the model can handle:
- counts → 0
- rates → 0.0 (NOT NaN — Keras hates NaN)
- ages → 0 days
- spend → 0 pence
- Track which features were missing in `missing_features` for monitoring

Async version `AsyncFeatureStoreClient` using `aioredis`. Same interface, async methods.

## Modified simulator/generator.py (small change)

After successful order insert, emit the notification (Phase 4 wires this up if Phase 2 didn't already):

```sql
NOTIFY order_placed, '<order_id_str>';
```

Or in Python:
```python
await conn.execute("SELECT pg_notify('order_placed', $1)", str(order_id))
```

## Updated docker-compose.yml

**feature_aggregator**
- Same image
- Command: `python -m feature_store.aggregator`
- Depends on: postgres, redis (healthy)
- Restart: unless-stopped

**feature_batch**
- Same image
- Command: `python -m feature_store.batch_compute --daemon`
- (`--daemon` makes it schedule itself via APScheduler; alternative is to run via cron)

## Tests

**tests/test_feature_store.py**

- `test_streaming_features_update_on_order`: place an order via SQL+NOTIFY, wait 500ms, assert `fs:user:{uid}:stream` `orders_1h` incremented
- `test_sliding_window_decay`: place an order, advance fake time by 65 minutes (test uses ZADD with manual timestamps), assert `orders_1h` returns to 0 but `orders_24h` still shows it
- `test_batch_features_populated`: run `batch_compute.run_batch()` on test data, assert all expected fields present in Redis
- `test_client_get_features_under_10ms`: with 1000 users primed in Redis, call `get_features()` 100 times, assert p99 < 10ms (this is real on a local Redis; in CI may need to relax to 25ms)
- `test_missing_features_have_defaults`: brand new user with no Redis data → `get_features` returns FeatureSet with 0s and `missing_features` populated
- `test_aggregator_recovers_from_dropped_notify`: pause aggregator, insert orders, restart aggregator, assert backup polling picks up the orders within 60s
- `test_no_unbounded_redis_growth`: insert 50K orders to one user's zset (simulating attack), wait for cleanup loop, assert zset trimmed to last 24h window only

Stress test:
- `test_stream_throughput`: insert orders at 100/sec for 60 seconds while aggregator runs; assert no errors, p99 update latency <1 sec (time from INSERT to feature visible in Redis)

## Acceptance Criteria for Phase 4

- Streaming features update within 2 seconds of an order being placed (measured via timestamp comparison)
- `get_features()` returns full FeatureSet in <10ms p99 under load (verify with benchmark script)
- Daily batch job completes in <60 minutes on full 1M-user dataset
- After 24h of running, Redis memory usage stays <2GB (verify with `INFO memory`)
- Aggregator survives Postgres connection blips (auto-reconnects)
- No feature is ever `null`/`None` returned from client — defaults are applied
- All tests pass

## Out of Scope for Phase 4

- ML training (Phase 5) — but Phase 5 will use the client to load features
- Scoring service (Phase 6) — but Phase 6's scoring is the primary consumer
- Dashboard (Phase 7)
