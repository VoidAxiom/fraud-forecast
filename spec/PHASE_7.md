# PHASE 7: Monitoring, Retraining, Drift Detection, Polish

## Goal of This Phase

Close the loop. Build the operator-facing Streamlit dashboard that shows what the system is doing in real time. Add automated weekly retraining, drift detection, mock analyst review, and partition maintenance. Write the final README. Verify the full system meets all acceptance criteria from MASTER.md.

This is the phase where the platform becomes operable, not just runnable.

## Prerequisites

- Phases 1–6 complete.
- Scoring service has been running for at least 24 hours with healthy throughput.
- Sufficient data for drift comparisons (at least one promoted model with reference distribution stored).

## Deliverables

1. `monitoring/dashboard.py` — Streamlit dashboard on port 8080
2. `monitoring/analyst_mock.py` — auto-resolves REVIEW queue based on ground truth (delayed)
3. `monitoring/drift_detector.py` — KS-test on score distributions
4. `monitoring/retraining_scheduler.py` — APScheduler weekly retrain
5. `monitoring/partition_maintenance.py` — calls `ensure_future_partitions` weekly
6. Updated `docker-compose.yml` — adds `monitoring`, `scheduler` services
7. `README.md` — full setup, teardown, troubleshooting
8. `tests/test_dashboard.py`, `tests/test_drift.py`, `tests/test_retraining.py`
9. Final integration test: `tests/test_system_acceptance.py` — verifies all MASTER acceptance criteria

## monitoring/dashboard.py

Streamlit app. Connects to Postgres as `analyst_user` (CAN read ground truth). Auto-refresh every 30 seconds.

### Layout

Sidebar:
- Time range selector: Last 1h / 6h / 24h / 7d
- Refresh interval: 10s / 30s / 60s / off
- Filter: by store_city, by fraud_category, by model_version

Main page — panels in this order:

**1. System Health Strip (top, always visible)**
- Stack status: Postgres ✓ / Redis ✓ / Scoring ✓ / TF Serving ✓ / Simulator ✓
- Current order rate (ord/sec, rolling 1min)
- Current scoring p99 latency (ms, rolling 1min)
- Current model version
- Hot table size / Cold table size
- Pending REVIEW queue depth

**2. Order Volume Time Series**
- Plotly line chart: orders/minute over selected range
- Stacked by `order_status` (PLACED, ACCEPTED, ..., DELIVERED, CANCELLED, REJECTED)
- Reference line: expected rate (50 * 60 = 3000 ord/min)

**3. Fraud Score Distribution (live)**
- Plotly histogram of `fraud_score` for orders in the selected range
- Overlay: distribution from 7 days ago (reference)
- Mark decision thresholds (0.5, 0.85) as vertical lines

**4. Decision Breakdown Over Time**
- Stacked area chart: APPROVE / REVIEW / DECLINE counts per 5-minute bucket
- Tooltips with percentages

**5. Model Performance (vs Ground Truth)**
*This is the killer panel — uses analyst_user role to JOIN orders to simulator_ground_truth.*

- Confusion matrix at current thresholds (only for orders with finalised labels — chargeback window closed)
- Live precision @ DECLINE / @ REVIEW
- Live recall against fraud labels
- Per-fraud-category recall (one row per category, sparkline showing trend)

**6. Latency Panel**
- p50, p95, p99 of scoring latency over time (line chart with 3 series)
- Histogram of latency distribution for last 1h
- Breakdown: feature_fetch / xgboost / tf_serving / db_write (read from `/metrics` endpoint of scoring service)

**7. Rules Engine Activity**
- Table: each rule, fires/hour, % of fires that were actually fraud (precision per rule)
- Highlights rules with <50% precision (candidates for re-tuning)

**8. Top REVIEW Queue**
- Top 20 highest-scoring orders pending review
- Columns: order_id, score, total_pence, user_email (masked), rules_triggered, age
- "Resolve" button → marks as FRAUD or LEGIT (Phase 7 mock flow will auto-resolve based on ground truth, but the UI exists for completeness)

**9. Drift Detection Panel**
- Last drift run timestamp, KS statistic, p-value, status (OK / WARNING / ALERT)
- Top 5 features with highest drift since last training
- Trigger button: "Force retrain now"

### Implementation pattern

```python
import streamlit as st
import plotly.express as px
import pandas as pd
from sqlalchemy import create_engine

st.set_page_config(page_title='Fraud Platform Monitor', layout='wide', 
                   initial_sidebar_state='expanded')

engine = create_engine(os.getenv('DATABASE_URL_ANALYST'))

@st.cache_data(ttl=30)
def load_orders_window(hours: int) -> pd.DataFrame:
    query = """
    SELECT order_id, placed_at, total_pence, order_status, 
           fraud_score, fraud_decision, fraud_score_version, fraud_rules_triggered
    FROM orders 
    WHERE placed_at > NOW() - INTERVAL '%s hours'
    UNION ALL
    SELECT order_id, placed_at, total_pence, order_status,
           fraud_score, fraud_decision, fraud_score_version, fraud_rules_triggered
    FROM orders_archive
    WHERE placed_at > NOW() - INTERVAL '%s hours'
    """
    return pd.read_sql(query, engine, params=(hours, hours))

# Auto-refresh
if st.sidebar.button('Refresh now') or st_autorefresh(interval=30000, key='refresh'):
    pass

# Render panels...
```

Use `st_autorefresh` (from `streamlit-autorefresh`) for live updates.

Mask emails as `f"{name[:2]}***@{domain}"` for display. Never show full user emails in the dashboard.

### Performance

The dashboard reads from `orders` and `orders_archive`. For a 24h window at 4.3M orders/day, queries can be slow. Use:
- Materialized views refreshed every 5 minutes for the heavy aggregations
- Or write a small `monitoring/aggregations.py` module that runs every 60s and writes pre-computed aggregates to Redis (`monitor:orders_per_min_5d`, etc.) that the dashboard reads cheaply

Recommend the Redis approach — same pattern as feature store, conceptually familiar.

## monitoring/analyst_mock.py

Background job. Every 5 minutes:
- Find orders with `fraud_decision='REVIEW'` and age >= 24 hours
- Look up ground truth (`simulator_ground_truth`)
- Update `fraud_outcome` based on ground truth (FRAUD or LEGIT)
- Set `fraud_reviewed_at = NOW()`, `fraud_reviewed_by = 'auto_analyst_mock'`

This simulates the human review queue closing the loop. In production this would be real human analysts.

```python
# Runs in the scheduler container
async def resolve_review_queue():
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT o.order_id, o.placed_at, gt.is_fraud, gt.fraud_category
            FROM orders o
            JOIN simulator_ground_truth gt USING (order_id)
            WHERE o.fraud_decision = 'REVIEW'
              AND o.fraud_reviewed_at IS NULL
              AND o.placed_at < NOW() - INTERVAL '24 hours'
            LIMIT 1000
        """)
        for row in rows:
            outcome = 'FRAUD' if row['is_fraud'] else 'LEGIT'
            await conn.execute("""
                UPDATE orders SET fraud_outcome = $1, fraud_reviewed_at = NOW(),
                                  fraud_reviewed_by = 'auto_analyst_mock',
                                  fraud_outcome_set_at = NOW()
                WHERE order_id = $2 AND placed_at = $3
            """, outcome, row['order_id'], row['placed_at'])
            # Also update orders_archive if it's there
```

Run this from `monitoring/scheduler.py` container, which is the analyst_user role.

## monitoring/drift_detector.py

Daily job. Compares current score distribution to a reference distribution (saved at last model promotion).

```python
from scipy.stats import ks_2samp

def detect_drift(model_version: str) -> DriftReport:
    # Load reference: 7-day score distribution at time of promotion
    ref_dist = load_reference_distribution(model_version)
    
    # Load current: last 24h scores
    current_scores = load_current_scores(hours=24)
    
    ks_stat, p_value = ks_2samp(ref_dist, current_scores)
    
    # Threshold: p < 0.001 = ALERT, p < 0.01 = WARNING, else OK
    if p_value < 0.001: status = 'ALERT'
    elif p_value < 0.01: status = 'WARNING'
    else: status = 'OK'
    
    # Also check per-feature drift for top 20 model features
    feature_drifts = {}
    for feat in TOP_FEATURES:
        # ... per-feature KS test on raw feature values from fraud_decisions.features_snapshot
        pass
    
    report = DriftReport(
        timestamp=datetime.now(timezone.utc),
        model_version=model_version,
        score_ks_stat=ks_stat, score_p_value=p_value, status=status,
        feature_drifts=feature_drifts,
        sample_size_current=len(current_scores),
        sample_size_reference=len(ref_dist),
    )
    
    save_to_redis(f'monitor:drift_latest', report.json())
    
    if status == 'ALERT':
        logger.warning('drift_detected', ks_stat=ks_stat, p_value=p_value)
        # Phase 7 does not auto-trigger retraining on drift — just flags it.
        # Phase 8 (future) could auto-trigger.
    
    return report
```

Reference distribution saved at promote-time by Phase 5's `promote.py` — add to that script: when promoting, sample 100K orders' scores from the prior 7 days and save to `models/{type}/{version}/reference_scores.npy`.

## monitoring/retraining_scheduler.py

APScheduler-based. Two jobs:

1. **Weekly retrain** (every Sunday 04:00 Europe/London):
   - Run full Phase 5 training pipeline
   - Run evaluation
   - If passes promotion gate, auto-promote
   - Trigger reloads on `xgboost_server` (POST /reload), TF Serving auto-detects via filesystem polling

2. **Daily drift check** (every day 06:00 Europe/London):
   - Run `drift_detector.detect_drift()`
   - Log results

3. **Hourly partition maintenance** (every hour at :15):
   - Call Postgres function `ensure_future_partitions(weeks_ahead := 8)` defined in Phase 1
   - Ensures partitions exist for the next 8 weeks at all times

4. **5-minute analyst mock** (every 5 minutes):
   - Run `resolve_review_queue()`

```python
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

scheduler = BlockingScheduler(timezone='Europe/London')

scheduler.add_job(
    weekly_retrain, CronTrigger(day_of_week='sun', hour=4, minute=0),
    id='weekly_retrain', max_instances=1, coalesce=True,
)
scheduler.add_job(
    daily_drift_check, CronTrigger(hour=6, minute=0),
    id='daily_drift', max_instances=1,
)
scheduler.add_job(
    partition_maintenance, CronTrigger(minute=15),
    id='partition_maint', max_instances=1,
)
scheduler.add_job(
    analyst_mock_resolve, 'interval', minutes=5,
    id='analyst_mock', max_instances=1,
)

scheduler.start()
```

```python
def weekly_retrain():
    version = f"v{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    logger.info('weekly_retrain_start', version=version)
    
    # 1. Data load
    config = TrainingDataConfig(
        start_date=datetime.now() - timedelta(days=60),
        end_date=datetime.now() - timedelta(days=2),
        label_finalisation_buffer_days=45,
    )
    df = load_training_data(config)
    df.to_parquet(f'ml/data/raw_{version}.parquet')
    
    # 2. Transform
    subprocess.run(['python', '-m', 'ml.transform.run_transform',
                    '--input', f'ml/data/raw_{version}.parquet',
                    '--output', f'ml/data/transformed/{version}'], check=True)
    
    # 3. Train both
    subprocess.run(['python', '-m', 'ml.training.train_xgboost', '--version', version], check=True)
    subprocess.run(['python', '-m', 'ml.training.train_dnn', '--version', version], check=True)
    
    # 4. Evaluate
    subprocess.run(['python', '-m', 'ml.training.evaluate', '--version', version], check=True)
    
    # 5. Try to promote (gates on regression)
    try:
        subprocess.run(['python', '-m', 'ml.training.promote', 
                        '--version', version, '--model-type', 'dnn'], check=True)
        subprocess.run(['python', '-m', 'ml.training.promote',
                        '--version', version, '--model-type', 'xgboost'], check=True)
        
        # 6. Reload XGBoost server
        httpx.post(f'{XGBOOST_SERVER_URL}/reload', timeout=30)
        # TF Serving auto-reloads via filesystem polling
        
        logger.info('weekly_retrain_promoted', version=version)
    except subprocess.CalledProcessError:
        logger.warning('weekly_retrain_promotion_blocked', version=version,
                       reason='regression_check_failed')
```

## monitoring/partition_maintenance.py

Simple wrapper around the Postgres function:

```python
def run_partition_maintenance():
    with get_engine('app').begin() as conn:
        conn.execute(text("SELECT ensure_future_partitions(weeks_ahead := 8)"))
    logger.info('partition_maintenance_complete')
```

## Updated docker-compose.yml

```yaml
monitoring:
  build: .
  command: streamlit run monitoring/dashboard.py --server.port 8080 --server.address 0.0.0.0
  ports: ['8080:8080']
  environment:
    - DATABASE_URL_ANALYST=postgresql://analyst_user:analyst_dev_password@postgres:5432/fraud_platform
    - REDIS_URL=redis://redis:6379/0
    - SCORING_METRICS_URL=http://scoring_service:8000/metrics
  depends_on: [postgres, redis, scoring_service]

scheduler:
  build: .
  command: python -m monitoring.retraining_scheduler
  environment:
    - DATABASE_URL=postgresql://app:app_dev_password@postgres:5432/fraud_platform
    - REDIS_URL=redis://redis:6379/0
    - XGBOOST_SERVER_URL=http://xgboost_server:8001
  volumes:
    - ./ml:/app/ml
    - ./models:/var/lib/models
    - ./shared:/app/shared
  depends_on: [postgres, scoring_service]
  restart: unless-stopped
```

## README.md

Final user-facing documentation. Sections:

1. **Overview** — what the system does, in 3 paragraphs
2. **Architecture Diagram** — ASCII or PNG showing the 3 subsystems and their connections
3. **Prerequisites** — Docker, 16GB RAM minimum, 50GB free disk
4. **Quick Start** — `make reset && make seed && make start-simulation && make train && make promote && make start-scoring && make open-dashboard`
5. **Subsystem Detail** — short description of each phase's component
6. **Configuration** — all env vars from .env.example documented
7. **Operations** — how to monitor, common Makefile commands
8. **Troubleshooting** — top 10 likely issues:
   - "Postgres won't start" → check shm_size, check port collision
   - "Seeding takes >30 minutes" → increase workers, check disk I/O
   - "Scoring p99 >100ms" → check Redis CPU, check DB pool exhaustion
   - "TF Serving won't load model" → check that model dir contains numeric subdir, check models.config
   - "Dashboard empty" → check materialised views, check analyst_user perms
   - etc.
9. **Data Model** — link to PHASE_1.md for full schema
10. **ML Pipeline** — link to PHASE_5.md
11. **What's Simulated vs Real** — explicit list. "This is a learning/demonstration platform. The simulator is realistic but not a real food delivery service. Card BINs, postcodes, addresses are all synthetic but format-correct."
12. **Acceptance Criteria** — copied verbatim from MASTER.md, with a checkbox per item

## Tests

**tests/test_dashboard.py**
- Smoke test: dashboard module imports without error
- `test_load_orders_window_query_runs`: the SQL queries used by the dashboard execute successfully against a seeded DB
- `test_email_masking`: `mask_email('alice@example.com')` returns `'al***@example.com'`
- `test_analyst_role_can_read_ground_truth`: verify the analyst_user (used by dashboard) CAN read simulator_ground_truth
- `test_analyst_role_cannot_modify_orders`: analyst is read-only on orders

**tests/test_drift.py**
- `test_no_drift_returns_ok`: pass identical distributions, assert status=OK
- `test_drift_detected_returns_alert`: pass clearly different distributions, assert status=ALERT
- `test_drift_report_persists`: after run, the report exists in Redis

**tests/test_retraining.py**
- `test_weekly_retrain_runs_full_pipeline`: with mocked subprocess calls, assert all 5 steps invoked in order
- `test_promotion_failure_doesnt_break_scheduler`: if promote fails (regression), scheduler logs warning and continues
- `test_partition_maintenance_idempotent`: run twice in a row, no errors

**tests/test_system_acceptance.py**

This is the big one — verifies every MASTER.md acceptance criterion.

- Run `make reset && make seed-small` (use --scale 0.1 for CI speed)
- Start simulation, run for 5 minutes
- Assert order generation rate ≥ 4 ord/sec (10% of 50)
- Assert hot table contains only orders meeting hot criteria
- Train a model on accumulated data; assert it completes
- Promote; assert symlink updated
- Hit scoring service with 100 requests; assert p99 < 200ms (relaxed for CI)
- Assert dashboard renders without error
- Connect as scoring_user; assert `SELECT * FROM simulator_ground_truth` fails
- Tear down with `make down`; verify clean state

## Operational Polish

Several small things that make the system feel finished:

- **Graceful shutdown:** every long-running daemon traps SIGTERM, finishes current batch, exits cleanly
- **Structured logging everywhere:** JSON to stdout, consistent fields (`event`, `timestamp`, `service`, `level`, `order_id`, `duration_ms`)
- **Healthcheck endpoints:** every service exposes `/health` or equivalent
- **Resource limits in docker-compose:** memory/CPU caps to prevent runaway containers
- **`make logs SERVICE=scoring_service` target** — tails specific service logs
- **`make psql ROLE=scoring`** — opens psql as a specific role for debugging
- **`make redis-cli`** — for live feature store inspection
- **Backup script:** `scripts/backup.sh` does `pg_dump` of critical tables (ground truth, fraud_decisions) so a long-running simulation's labels can be saved off
- **Reset script:** `scripts/factory_reset.sh` does `make down && rm -rf models/* ml/data/*` for full clean state

## Acceptance Criteria for Phase 7

All of the system-wide acceptance criteria from MASTER.md must pass:

- [ ] `docker-compose up` brings the entire stack from cold to generating orders within 90 seconds (excluding seed)
- [ ] Simulator sustains 50 orders/second for 24 hours without errors
- [ ] Hot table contains only orders meeting the hot criteria; cold table grows as expected
- [ ] Scoring service maintains p99 latency <100ms under sustained 50 ord/sec load
- [ ] Trained ensemble model achieves AUPRC ≥ 0.75 on a held-out test set
- [ ] Per-fraud-category recall: stolen_card ≥0.70, account_takeover ≥0.60, promo_abuse ≥0.80
- [ ] Dashboard updates in real-time with all required panels
- [ ] Scoring service has no DB access to `simulator_ground_truth` (enforced at Postgres role level)
- [ ] pytest suite passes; ≥70% line coverage on non-ML code
- [ ] Full system can be torn down and rebuilt from scratch via `make reset`

Additionally:

- [ ] Weekly retraining job runs successfully end-to-end (verify on a manual trigger)
- [ ] Drift detection produces sensible reports
- [ ] Mock analyst flow resolves the REVIEW queue within 24-30 hours of orders being placed
- [ ] Partition maintenance keeps 8 weeks of future partitions available at all times
- [ ] README is complete, accurate, and someone unfamiliar with the project can `make reset && make seed-small && make start-simulation` and see the dashboard within 30 minutes

## What "Done" Looks Like

A new developer can clone the repo, run `make reset && make seed-small && make start-simulation`, open `localhost:8080`, and within 5 minutes see:
- Orders ticking through the simulation
- A live fraud score distribution
- The model making decisions
- A dashboard showing what's happening

They can then run `make train && make promote` to see the model lifecycle in action.

The system is a complete, working, end-to-end fraud detection platform. It's not a real food delivery service — it's a simulator + ML system designed to mirror the architecture of one, in the style of mid-2020 production ML practice, localised to the UK.

## Out of Scope

Anything not listed above. Specifically NOT in this build:
- Real-time stream processing (Kafka, Flink) — Postgres NOTIFY is sufficient at this scale
- Multi-region deployment / disaster recovery
- A/B testing framework for models (the promote gate is the substitute)
- Hyperparameter tuning automation (e.g. Optuna sweeps)
- Graph features (PYG, link analysis between users/devices/payments)
- Anything PyTorch — we're explicitly in the TF era
- Real PCI compliance, real authentication, real user-facing app
