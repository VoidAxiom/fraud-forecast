# PHASE 6: Real-Time Scoring Service

## Goal of This Phase

Build the online fraud scoring service: a FastAPI HTTP endpoint that the simulator calls for every new order, returns a decision (APPROVE / REVIEW / DECLINE) in under 100ms p99, and logs an audit trail of every scored order.

This is the live production path. It combines: rules engine → feature fetch → XGBoost score → DNN score (via TF Serving) → ensemble → decision → audit log.

## Prerequisites

- Phases 1–5 complete.
- A trained, promoted model in the registry (`models/dnn/production` and `models/xgboost/production` symlinks exist).
- Feature store populated and updating.

## Stack

- FastAPI 0.65, Uvicorn (workers=4)
- TensorFlow Serving 2.3 (Docker image: `tensorflow/serving:2.3.0`)
- A separate `xgboost_server` FastAPI process (XGBoost doesn't natively integrate with TF Serving)
- `httpx` 0.18 for internal HTTP calls
- Connection to Postgres as `scoring_user` only

## Deliverables

1. `scoring_service/main.py` — FastAPI app, `/score`, `/health`, `/metrics` endpoints
2. `scoring_service/rules_engine.py` — hard rules evaluated before model
3. `scoring_service/feature_builder.py` — assembles raw feature dict from order + feature store
4. `scoring_service/ensemble.py` — combines XGB + DNN scores into final score and decision
5. `scoring_service/xgboost_server.py` — small FastAPI wrapping XGBoost model
6. `ml/serving/tf_serving.Dockerfile` — TF Serving image with models mounted
7. `ml/serving/models.config` — TF Serving model config
8. Updated `docker-compose.yml` — adds `scoring_service`, `xgboost_server`, `tf_serving`
9. Modified `simulator/generator.py` — calls `/score` when `SCORING_ENABLED=true`, applies decision
10. `tests/test_rules_engine.py`, `tests/test_scoring_e2e.py`

## API Contract

### POST /score

**Request body** — the full order context, exactly as the simulator builds it pre-insert:

```json
{
  "order_id": "uuid",
  "order_context": {
    "user": {
      "user_id": "uuid",
      "email": "user@example.com",
      "email_domain": "example.com",
      "account_age_days": 142,
      "total_orders_lifetime": 17,
      "is_guest": false,
      "risk_tier": "STANDARD"
    },
    "order": {
      "order_type": "DELIVERY",
      "channel": "IOS_APP",
      "subtotal_pence": 2450,
      "total_pence": 3120,
      "item_count": 3,
      "is_first_order_for_user": false,
      "is_new_payment_method": false,
      "is_new_delivery_address": false,
      "time_to_checkout_seconds": 87,
      "promo_code": null
    },
    "store": {
      "store_id": "uuid",
      "merchant_id": "uuid",
      "city": "London",
      "latitude": 51.5074,
      "longitude": -0.1278
    },
    "payment": {
      "payment_method_id": "uuid",
      "payment_type": "CREDIT_CARD",
      "card_bin": "535522",
      "card_brand": "MASTERCARD",
      "card_funding_type": "DEBIT",
      "card_issuer_country": "GB",
      "is_digital_native_bank": true,
      "avs_result": "MATCH",
      "cvv_result": "MATCH"
    },
    "device": {
      "device_id": "uuid",
      "device_type": "MOBILE_APP",
      "platform": "iOS",
      "os_version": "16.4",
      "app_version": "4.32.1"
    },
    "session": {
      "session_id": "uuid",
      "ip_address": "82.10.45.12",
      "ip_country": "GB",
      "ip_is_proxy": false,
      "ip_is_vpn": false,
      "ip_is_tor": false
    },
    "delivery": {
      "delivery_address_id": "uuid",
      "delivery_latitude": 51.50,
      "delivery_longitude": -0.13,
      "delivery_distance_km": 2.4,
      "address_type": "RESIDENTIAL",
      "ip_to_delivery_distance_km": 0.8,
      "billing_to_delivery_distance_km": 0.5
    }
  }
}
```

**Response body:**

```json
{
  "order_id": "uuid",
  "decision": "APPROVE",
  "score": 0.0421,
  "model_version": "v20260524_153000",
  "rules_triggered": [],
  "latency_ms": 38,
  "scored_at": "2026-05-24T15:32:17.421Z"
}
```

`decision`: one of `APPROVE`, `REVIEW`, `DECLINE`.

**Error response** (timeout, model unavailable, etc.): HTTP 503 with `{"error": "...", "fallback_decision": "REVIEW"}`. The simulator treats unscored orders as APPROVED but flags them in logs — this matches real-world failure modes where scoring being down should not block orders.

### GET /health

```json
{
  "status": "ok",
  "feature_store_reachable": true,
  "xgboost_reachable": true,
  "tf_serving_reachable": true,
  "model_version": "v20260524_153000",
  "uptime_seconds": 12345
}
```

Used by Docker healthcheck and the dashboard.

### GET /metrics

Plain text in Prometheus format. Counters: `scoring_requests_total`, `scoring_errors_total{reason}`, `scoring_decisions_total{decision}`. Histograms: `scoring_latency_ms`, `feature_fetch_latency_ms`, `xgboost_latency_ms`, `tf_serving_latency_ms`. No external Prometheus required for Phase 6 — the dashboard in Phase 7 will scrape these.

## scoring_service/main.py

```python
from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    app.state.feature_store = AsyncFeatureStoreClient(REDIS_URL)
    app.state.xgb_client = httpx.AsyncClient(
        base_url=XGBOOST_SERVER_URL, timeout=httpx.Timeout(0.05)
    )
    app.state.tfs_client = httpx.AsyncClient(
        base_url=TF_SERVING_URL, timeout=httpx.Timeout(0.08)
    )
    app.state.db_pool = await asyncpg.create_pool(
        DATABASE_URL_SCORING, min_size=5, max_size=20
    )
    app.state.rules_engine = RulesEngine()
    app.state.ensemble = Ensemble(weights=(0.6, 0.4))
    app.state.model_version = read_production_model_version()
    yield
    # Shutdown
    await app.state.feature_store.close()
    await app.state.xgb_client.aclose()
    await app.state.tfs_client.aclose()
    await app.state.db_pool.close()

app = FastAPI(lifespan=lifespan)

@app.post('/score', response_model=ScoreResponse)
async def score(req: ScoreRequest):
    t0 = time.perf_counter()
    METRICS.requests.inc()
    
    try:
        # 1. Fetch features
        t_feat = time.perf_counter()
        features = await app.state.feature_store.get_features(
            user_id=req.order_context.user.user_id,
            device_id=req.order_context.device.device_id,
            payment_method_id=req.order_context.payment.payment_method_id,
            ip_address=req.order_context.session.ip_address,
            store_id=req.order_context.store.store_id,
            merchant_id=req.order_context.store.merchant_id,
            delivery_address_id=req.order_context.delivery.delivery_address_id,
            email_domain=req.order_context.user.email_domain,
        )
        METRICS.feature_latency.observe((time.perf_counter() - t_feat) * 1000)
        
        # 2. Build raw feature dict (order context + feature store values, merged)
        raw_features = feature_builder.build(req.order_context, features)
        
        # 3. Rules engine — hard rules can short-circuit
        rules_result = app.state.rules_engine.evaluate(raw_features)
        # rules_result: {triggered: list[str], action: 'PASS'|'REVIEW'|'DECLINE'}
        
        # If a rule says DECLINE, skip the model call — save latency.
        # Still log a fraud_decision row with score=1.0, model_version='rules_only'.
        if rules_result.action == 'DECLINE':
            decision = 'DECLINE'
            score_value = 1.0
            await persist_decision(...)
            return build_response(...)
        
        # 4. Model inference (parallel: XGB + DNN)
        xgb_task = asyncio.create_task(call_xgboost(app.state.xgb_client, raw_features))
        dnn_task = asyncio.create_task(call_tf_serving(app.state.tfs_client, raw_features))
        try:
            xgb_score, dnn_score = await asyncio.gather(xgb_task, dnn_task)
        except asyncio.TimeoutError:
            METRICS.errors.labels(reason='model_timeout').inc()
            return fallback_response(req.order_id, 'REVIEW', 'model_timeout')
        
        # 5. Ensemble + decision
        final_score = app.state.ensemble.combine(xgb_score, dnn_score)
        
        # Rules can override "would have been APPROVE" up to REVIEW
        if rules_result.action == 'REVIEW' and final_score < 0.5:
            decision = 'REVIEW'
        else:
            decision = decide_from_score(final_score)
        
        # 6. Persist audit row
        await persist_decision(
            order_id=req.order_id, order_placed_at=req.order_context.order.placed_at,
            score=final_score, decision=decision,
            features_snapshot=raw_features,
            rules_triggered=rules_result.triggered,
            model_version=app.state.model_version,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
        
        # 7. Update order row (scoring_user can update only fraud_* columns)
        await update_order_fraud_columns(
            order_id=req.order_id, score=final_score,
            decision=decision, version=app.state.model_version,
            rules_triggered=rules_result.triggered,
        )
        
        METRICS.decisions.labels(decision=decision).inc()
        return ScoreResponse(
            order_id=req.order_id, decision=decision, score=final_score,
            model_version=app.state.model_version,
            rules_triggered=rules_result.triggered,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
    except Exception as e:
        logger.exception('scoring_failed', order_id=str(req.order_id))
        METRICS.errors.labels(reason='exception').inc()
        # Fail open with REVIEW
        return fallback_response(req.order_id, 'REVIEW', str(e))
```

### Decision thresholds

```python
def decide_from_score(score: float) -> str:
    if score >= 0.85: return 'DECLINE'
    if score >= 0.50: return 'REVIEW'
    return 'APPROVE'
```

These are starting thresholds. Real thresholds come from a precision-recall tradeoff against business cost: each DECLINE on a legit user costs the platform a real customer (LTV ~£200); each missed fraud costs the chargeback amount + fee (~£40). Reasonable: tune so DECLINE-rate is ~0.5% of all orders (the rate at which fraudster behaviour is unambiguous). Document this in code comments.

## scoring_service/rules_engine.py

Hard rules that fire before / alongside the model. These exist because:
1. Some patterns are easier to express as rules than to learn
2. Rules are interpretable (regulators, customer service)
3. Rules guarantee floor protection against catastrophic gaps in the model

```python
@dataclass
class RuleResult:
    rule_name: str
    triggered: bool
    action: str | None      # 'REVIEW' or 'DECLINE' or None
    note: str = ''

class RulesEngine:
    def evaluate(self, features: dict) -> RulesEvaluation:
        results = []
        for rule_fn in self.rules:
            r = rule_fn(features)
            if r.triggered:
                results.append(r)
        
        triggered_names = [r.rule_name for r in results]
        # Strongest action wins: DECLINE > REVIEW > PASS
        actions = {r.action for r in results}
        if 'DECLINE' in actions: action = 'DECLINE'
        elif 'REVIEW' in actions: action = 'REVIEW'
        else: action = 'PASS'
        
        return RulesEvaluation(triggered=triggered_names, action=action)

# === Specific rules ===

def rule_velocity_user_1h(f):
    if f['user_orders_1h'] > 10:
        return RuleResult('velocity_user_1h', True, 'REVIEW', 
                          f"user placed {f['user_orders_1h']} orders in last hour")
    return RuleResult('velocity_user_1h', False, None)

def rule_velocity_device_1h(f):
    if f['device_orders_1h'] > 5:
        return RuleResult('velocity_device_1h', True, 'REVIEW', ...)
    return RuleResult('velocity_device_1h', False, None)

def rule_card_bin_blocklist(f):
    # Maintained list of known compromised BINs / breach lists
    if f['card_bin'] in BIN_BLOCKLIST:
        return RuleResult('card_bin_blocklist', True, 'DECLINE', ...)
    return RuleResult('card_bin_blocklist', False, None)

def rule_tor_exit_node(f):
    if f['ip_is_tor']:
        return RuleResult('tor_exit_node', True, 'REVIEW', ...)
    return RuleResult('tor_exit_node', False, None)

def rule_high_value_new_user(f):
    if f['user_account_age_days'] <= 1 and f['total_pence'] > 7500:  # £75
        return RuleResult('high_value_new_user', True, 'REVIEW', ...)
    return RuleResult('high_value_new_user', False, None)

def rule_disposable_email_plus_new_card(f):
    if (f['user_email_domain'] in DISPOSABLE_EMAIL_DOMAINS 
        and f['is_new_payment_method']
        and f['is_new_delivery_address']):
        return RuleResult('disposable_email_new_card_new_address', True, 'REVIEW', ...)
    return RuleResult('disposable_email_new_card_new_address', False, None)

def rule_excessive_chargebacks(f):
    if f.get('user_lifetime_chargeback_count', 0) >= 3:
        return RuleResult('user_excessive_chargebacks', True, 'DECLINE', ...)
    return RuleResult('user_excessive_chargebacks', False, None)

def rule_geo_mismatch_extreme(f):
    if (f['ip_to_delivery_distance_km'] or 0) > 500 and f['ip_country'] != 'GB':
        return RuleResult('geo_mismatch_extreme', True, 'REVIEW', ...)
    return RuleResult('geo_mismatch_extreme', False, None)

# Register all rules
RULES = [
    rule_velocity_user_1h, rule_velocity_device_1h, rule_card_bin_blocklist,
    rule_tor_exit_node, rule_high_value_new_user, 
    rule_disposable_email_plus_new_card, rule_excessive_chargebacks,
    rule_geo_mismatch_extreme,
]
```

`BIN_BLOCKLIST`: a small static set for the simulation (e.g. 50 fake "compromised" BINs). In real life this comes from threat intel feeds. Store as a Python set; load at startup.

Document each rule's expected hit rate. At 50 ord/sec, the velocity_user rule should fire on ~0.1% of legit orders and ~30% of promo abuse fraud orders — visible in the dashboard.

## scoring_service/feature_builder.py

Merges the request's `order_context` (fields known at order placement) with `FeatureSet` (values from the feature store) into one flat dict matching the feature names the model expects.

```python
def build(order_context: OrderContext, fs: FeatureSet) -> dict:
    return {
        # From request (point-in-time correct by definition)
        'user_account_age_days': order_context.user.account_age_days,
        'user_email_domain': order_context.user.email_domain,
        'order_channel': order_context.order.channel,
        'subtotal_pence': order_context.order.subtotal_pence,
        'total_pence': order_context.order.total_pence,
        'item_count': order_context.order.item_count,
        'is_first_order_for_user': order_context.order.is_first_order_for_user,
        'is_new_payment_method': order_context.order.is_new_payment_method,
        'is_new_delivery_address': order_context.order.is_new_delivery_address,
        'time_to_checkout_seconds': order_context.order.time_to_checkout_seconds,
        'card_bin': order_context.payment.card_bin,
        'card_brand': order_context.payment.card_brand,
        'card_funding_type': order_context.payment.card_funding_type,
        'card_issuer_country': order_context.payment.card_issuer_country,
        'is_digital_native_bank': order_context.payment.is_digital_native_bank,
        'avs_result': order_context.payment.avs_result,
        'cvv_result': order_context.payment.cvv_result,
        'device_type': order_context.device.device_type,
        'platform': order_context.device.platform,
        'ip_country': order_context.session.ip_country,
        'ip_is_proxy': order_context.session.ip_is_proxy,
        'ip_is_vpn': order_context.session.ip_is_vpn,
        'ip_is_tor': order_context.session.ip_is_tor,
        'delivery_distance_km': order_context.delivery.delivery_distance_km,
        'ip_to_delivery_distance_km': order_context.delivery.ip_to_delivery_distance_km,
        'billing_to_delivery_distance_km': order_context.delivery.billing_to_delivery_distance_km,
        'delivery_address_type': order_context.delivery.address_type,
        # From feature store
        'user_lifetime_order_count': fs.user_lifetime_order_count,
        'user_lifetime_chargeback_rate': fs.user_lifetime_chargeback_rate,
        'user_orders_1h': fs.user_orders_1h,
        'user_orders_24h': fs.user_orders_24h,
        'user_spend_24h_pence': fs.user_spend_24h_pence,
        'user_unique_stores_24h': fs.user_unique_stores_24h,
        'device_lifetime_order_count': fs.device_lifetime_order_count,
        'device_unique_users_lifetime': fs.device_unique_users_lifetime,
        'device_orders_1h': fs.device_orders_1h,
        'payment_lifetime_chargeback_rate': fs.payment_lifetime_chargeback_rate,
        'ip_unique_users_24h': fs.ip_unique_users_24h,
        'store_chargeback_rate': fs.store_chargeback_rate,
        'merchant_chargeback_rate': fs.merchant_chargeback_rate,
        'email_domain_chargeback_rate': fs.email_domain_chargeback_rate,
    }
```

**Critical:** field name and order must match exactly what Phase 5's `preprocessing_fn` expects. Maintain a `FEATURE_SCHEMA` constant in `shared/feature_schema.py` shared between Phases 5 and 6.

## scoring_service/xgboost_server.py

Small FastAPI process that holds the XGBoost model in memory and serves predictions.

```python
app = FastAPI()
model: xgb.Booster = None
feature_names: list[str] = None

@app.on_event('startup')
async def load_model():
    global model, feature_names
    prod_dir = Path('/var/lib/models/xgboost/production')
    model = xgb.Booster()
    model.load_model(str(prod_dir / 'model.bin'))
    feature_names = json.loads((prod_dir / 'feature_names.json').read_text())

@app.post('/predict')
async def predict(req: dict):
    # req: {'features': {feature_name: value, ...}}
    # Apply the same numerical transforms as preprocessing_fn did at training time
    # (For XGBoost, we typically don't z-score, but DO handle missing values and OOV)
    
    # Build feature vector in canonical order
    x = np.array([[req['features'].get(f, 0) for f in feature_names]])
    dmatrix = xgb.DMatrix(x, feature_names=feature_names)
    pred = float(model.predict(dmatrix)[0])
    return {'score': pred}

@app.post('/reload')
async def reload_model():
    """Called after a new model is promoted; reloads from the production symlink."""
    await load_model()
    return {'status': 'reloaded', 'version': read_version()}
```

Run with uvicorn workers=2 (XGBoost prediction is CPU-bound, single-thread per worker).

**Note on XGBoost preprocessing:** XGBoost handles missing values and raw numerical well, so it doesn't need the same z-score preprocessing as the DNN. However, categorical features still need encoding. Use one of:
- (Simpler) Pre-encode categoricals at training time (label encoding stored in `feature_names.json` extras), apply same encoding here
- (More correct) Save a separate preprocessor for XGBoost and apply both here and during evaluation

For Phase 6, go with the simpler approach. Document the encoding scheme.

## TF Serving setup

### ml/serving/models.config

```
model_config_list: {
  config: {
    name: 'fraud_dnn',
    base_path: '/models/fraud_dnn',
    model_platform: 'tensorflow',
    model_version_policy: { latest: { num_versions: 1 } }
  }
}
```

### ml/serving/tf_serving.Dockerfile

```dockerfile
FROM tensorflow/serving:2.3.0
COPY models.config /models/models.config
# Models are mounted as a volume at runtime, not baked in
CMD ["tensorflow_model_server", \
     "--port=8500", "--rest_api_port=8501", \
     "--model_config_file=/models/models.config", \
     "--model_config_file_poll_wait_seconds=30"]
```

In `docker-compose.yml`:

```yaml
tf_serving:
  build:
    context: ./ml/serving
    dockerfile: tf_serving.Dockerfile
  ports:
    - '8501:8501'
  volumes:
    - ./models/dnn:/models/fraud_dnn:ro
  healthcheck:
    test: ['CMD-SHELL', 'curl -f http://localhost:8501/v1/models/fraud_dnn || exit 1']
    interval: 10s
    timeout: 3s
    retries: 5
```

The `models/dnn/{version}/saved_model/` directory must be at `models/dnn/{version}/` numeric subdirectory for TF Serving's auto-discovery. Promotion symlink trick: rename `v20260524_153000` to `1748100600` (unix timestamp) so TF Serving's "latest" picks it up. Document this convention in `ml/registry/model_registry.py`.

## scoring_service/ensemble.py

```python
class Ensemble:
    def __init__(self, weights=(0.6, 0.4)):
        self.w_xgb, self.w_dnn = weights
        assert abs(self.w_xgb + self.w_dnn - 1.0) < 1e-6
    
    def combine(self, xgb_score: float, dnn_score: float) -> float:
        return self.w_xgb * xgb_score + self.w_dnn * dnn_score
```

Weights come from the best ensemble weights found during Phase 5 evaluation. Loaded from `models/ensemble_config/production/config.json`.

## Modified simulator/generator.py

Wire scoring into the order creation flow:

```python
async def create_one_order(...):
    # ... build order ...
    
    if config.SCORING_ENABLED:
        try:
            score_response = await scoring_client.score(
                order_id=order.order_id,
                order_context=build_score_request_context(order, user, store, 
                                                          payment, device, session, delivery)
            )
            order.fraud_score = score_response.score
            order.fraud_decision = score_response.decision
            order.fraud_score_version = score_response.model_version
            order.fraud_rules_triggered = score_response.rules_triggered
            
            # APPROVE: proceed normally
            # REVIEW: insert order but flag it; mock analyst flow in Phase 7 resolves these
            # DECLINE: still insert the order (we need ground truth comparison), 
            #          but set order_status='REJECTED' so lifecycle doesn't progress it
            if score_response.decision == 'DECLINE':
                order.order_status = 'REJECTED'
                order.cancellation_reason = 'fraud_decline'
                order.cancelled_by = 'FRAUD'
                order.terminal_state_reached_at = now()
        except httpx.RequestError:
            # Scoring unavailable — fail open
            logger.warning('scoring_unavailable_fail_open')
            order.fraud_decision = 'APPROVE'
            order.fraud_score_version = 'fallback'
    
    async with pool.acquire() as conn:
        async with conn.transaction():
            await insert_order(conn, order)
            # ... etc
```

**Important — orders are inserted regardless of decision.** This is what enables training: we need to see what happened to "borderline" orders to evaluate the model. DECLINED orders go in with `order_status='REJECTED'`. REVIEW orders proceed normally but the dashboard will surface them. This is realistic — production fraud systems typically run in "shadow mode" first, capturing what they WOULD have done, before being given decline authority.

For the simulation, we run in "active mode" where DECLINE actually rejects (so we can measure precision of decline decisions against ground truth).

## Updated docker-compose.yml

```yaml
xgboost_server:
  build: .
  command: uvicorn scoring_service.xgboost_server:app --host 0.0.0.0 --port 8001 --workers 2
  volumes:
    - ./models:/var/lib/models:ro
  depends_on: [postgres]
  
tf_serving:
  build: { context: ./ml/serving, dockerfile: tf_serving.Dockerfile }
  ports: ['8501:8501']
  volumes:
    - ./models/dnn:/models/fraud_dnn:ro
  
scoring_service:
  build: .
  command: uvicorn scoring_service.main:app --host 0.0.0.0 --port 8000 --workers 4
  ports: ['8000:8000']
  environment:
    - DATABASE_URL_SCORING=postgresql://scoring_user:scoring_dev_password@postgres:5432/fraud_platform
    - REDIS_URL=redis://redis:6379/0
    - XGBOOST_SERVER_URL=http://xgboost_server:8001
    - TF_SERVING_URL=http://tf_serving:8501/v1/models/fraud_dnn:predict
    - MODELS_DIR=/var/lib/models
  volumes:
    - ./models:/var/lib/models:ro
  depends_on:
    - postgres
    - redis
    - xgboost_server
    - tf_serving
  healthcheck:
    test: ['CMD', 'curl', '-f', 'http://localhost:8000/health']
    interval: 10s
    retries: 5
```

## Latency budget (targets)

| Step | Budget | Notes |
|---|---|---|
| Feature fetch | 10ms | Redis pipelined |
| Rules engine | 1ms | pure Python |
| XGBoost predict | 15ms | over HTTP |
| TF Serving predict | 25ms | over HTTP |
| (XGB + TFS run in parallel) | max(15, 25) = 25ms | |
| Ensemble + decision | 1ms | |
| DB writes (1 INSERT + 1 UPDATE) | 15ms | with index |
| Misc / network | 8ms | |
| **Total p99 target** | **<100ms** | |

If the budget blows out under load, the most likely culprit is the DB write (idx update on a partitioned table). Consider: make the DB write async (fire-and-forget via a queue) so the response returns immediately and the audit row is written async. Document this tradeoff in code comments — it's a deliberate choice.

## Tests

**tests/test_rules_engine.py**

- For each rule: a case that triggers it, a case that doesn't
- `test_rule_priority`: DECLINE wins over REVIEW wins over PASS
- `test_no_rule_triggered_returns_pass`
- `test_rules_engine_is_pure`: same input twice → same output, no state

**tests/test_scoring_e2e.py**

Requires the full stack up.

- `test_score_returns_200_with_valid_input`: send a known-legit request, get a response with score < 0.5
- `test_score_returns_high_for_fraud_pattern`: send a synthetic "stolen card" payload, get score > 0.5
- `test_decision_thresholds`: send scores known to fall around boundaries, verify decisions
- `test_audit_row_inserted`: after a /score call, query `fraud_decisions` table, find the row
- `test_order_updated_with_decision`: after /score, the orders row has `fraud_score`, `fraud_decision` set
- `test_scoring_user_cannot_read_total_pence`: not actually — scoring user CAN read everything except ground truth. But test that it cannot UPDATE non-fraud columns.
- `test_feature_store_down_returns_fallback`: pause Redis, send /score, assert HTTP 200 with REVIEW + reasoning
- `test_xgboost_down_returns_fallback`: stop xgboost_server, send /score, assert fallback
- `test_load_50_orders_per_second_p99`: blast 3000 requests over 60 seconds via `httpx.AsyncClient`; assert p99 latency <100ms, error rate <1%
- `test_rule_short_circuits_model_call`: send a payload that triggers `card_bin_blocklist`, monitor `xgboost_server` logs/metrics to confirm it was NOT called

## Acceptance Criteria for Phase 6

- `make up` brings up the full stack including scoring service in <90 seconds from cold
- `/health` returns ok with all subcomponents healthy
- A POST to `/score` with valid input returns a response in <50ms p50 / <100ms p99 (measure via load test)
- Under sustained 50 orders/second, scoring service maintains p99 <100ms for 30+ minutes
- Decline rate over a 1-hour run is 0.5%–2% of orders (reasonable threshold tuning)
- Decline precision (declines that were actually fraud, per ground truth) is ≥70% — verified by querying `simulator_ground_truth` JOIN `orders` (run this query from `analyst_user`, NOT scoring_user)
- Audit table `fraud_decisions` has one row per scored order
- Scoring service cannot read `simulator_ground_truth` (still enforced; verify by running a test from inside the running scoring container)
- Rules engine has at least 8 distinct rules registered
- All tests pass

## Out of Scope for Phase 6

- Dashboard (Phase 7)
- Automated retraining (Phase 7)
- Drift detection (Phase 7)
- Mock analyst flow that resolves REVIEW queue (Phase 7)
