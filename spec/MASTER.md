# Fraud Detection Platform for UK Food Delivery — Master Overview

This document gives the full project context. Each of the 7 phase documents (`PHASE_1.md` through `PHASE_7.md`) is self-contained and can be fed to Claude Code independently to build that phase. Read this file first to understand the whole, then execute phases in order.

---

## Project Summary

Build a production-realistic fraud detection platform for a UK online food ordering company at the scale of Just Eat / Deliveroo / Uber Eats UK. The platform consists of three subsystems:

1. **Environment** — PostgreSQL + Redis infrastructure with hot/cold order partitioning
2. **Simulator** — Synthetic order generator producing ~50 orders/second with labeled fraud injection
3. **Fraud Detection** — TensorFlow-based ML pipeline (training, real-time scoring, monitoring) representative of mid-2020 production ML capabilities

## Scale Targets

- **1,000,000 users** across 10 major UK cities
- **15,000 stores** across **5,000 merchants**
- **80,000 menu items**
- **2,000 drivers**
- **300,000 devices**, **200,000 payment methods**, **150,000 user addresses**
- **Order generation rate:** 50 orders/second sustained (~4.3M orders/day)
- **Fraud injection rate:** ~2% of orders
- **Database:** hot table maintains <48h post-terminal orders + all active orders; cold table is the long-term store
- **Scoring latency target:** p99 < 100ms

## Tech Stack (chosen to represent mid-2020 production capabilities)

- **Language:** Python 3.8
- **Database:** PostgreSQL 12, partitioned by week on `placed_at`
- **Cache / Feature Store:** Redis 6
- **ML Framework:** TensorFlow 2.3, TFX 0.22 components, TF Transform, TF Serving 2.3
- **Tabular Model:** XGBoost 1.2
- **ORM / Migrations:** SQLAlchemy 1.4, Alembic
- **Services:** FastAPI 0.65, Uvicorn
- **Orchestration:** Docker Compose
- **Scheduling:** APScheduler 3.7
- **Dashboard:** Streamlit 0.85
- **Data generation:** Faker 8.x with `en_GB` locale
- **Testing:** pytest 6.x, pytest-asyncio

## UK Localisation Rules (apply throughout)

- **Country code:** `GB` (ISO 3166-1)
- **Currency:** GBP, stored as integer pence (never floats)
- **VAT:** 20% standard rate. Hot takeaway food = 20% VAT; cold takeaway food = 0% VAT (zero-rated). Delivery fee = 20% VAT. Service fee = 20% VAT. The simulator must compute these correctly per item.
- **Phone format:** `+44` prefix, e.g. `+447700900123`
- **Postcode format:** UK pattern (e.g. `SW1A 1AA`, `M1 1AE`, `EH1 1YZ`). Validation regex: `^[A-Z]{1,2}[0-9][A-Z0-9]? ?[0-9][A-Z]{2}$`
- **Cities (weighted by population for user/store distribution):** London (35%), Birmingham (8%), Manchester (8%), Glasgow (6%), Leeds (6%), Liverpool (5%), Bristol (5%), Edinburgh (5%), Sheffield (4%), Newcastle (4%), other (14% — distribute across smaller UK cities: Cardiff, Belfast, Nottingham, Southampton, Brighton, Cambridge, Oxford, Reading, etc.)
- **Cuisines (weighted by UK market reality):** Indian (15%), Chinese (12%), Italian/Pizza (15%), Kebab/Turkish (10%), Fish & Chips (8%), Burger/American (10%), Thai (5%), Japanese/Sushi (5%), Caribbean (3%), Lebanese (3%), Polish (2%), British/Pub (5%), Vietnamese (2%), Other (5%)
- **POS systems (UK distribution):** Lightspeed (15%), Square (15%), Epos Now (12%), Toast (8%), Clover (8%), Vita Mojo (5%), Deliveroo Tablet (10%), Uber Eats Tablet (10%), in-house (17%)
- **Card schemes:** Visa (45%), Mastercard (35%), Amex (8%), Maestro (5%), Other (7%)
- **Card issuer mix (UK-realistic, used in seeding):**
  - Traditional banks (60%): Barclays, HSBC, Lloyds, NatWest, Santander UK, Halifax, Nationwide, RBS, TSB
  - Digital banks (25%): Monzo, Revolut, Starling, Chase UK — flag these as `is_digital_native_bank=true` since real fraud systems weight them slightly higher risk for new accounts
  - Foreign-issued (10%): random EU / US BINs
  - Prepaid (5%): higher fraud weight
- **Timezone:** `Europe/London` everywhere (handles BST/GMT). All TIMESTAMPTZ in DB.
- **Email domains:** UK skew — gmail.com (35%), outlook.com (12%), hotmail.com (15%), yahoo.co.uk (8%), btinternet.com (5%), icloud.com (10%), googlemail.com (3%), live.co.uk (2%), other (5%), disposable (5% — mailinator.com, guerrillamail.com, tempmail.io, throwawaymail.com, 10minutemail.com)
- **Names:** UK distribution via Faker `en_GB` locale
- **Bank holidays (affect order patterns):** New Year's Day, Good Friday, Easter Monday, Early May bank holiday, Spring bank holiday, Summer bank holiday, Christmas Day, Boxing Day. Simulator's "load" pattern should show holiday spikes (Boxing Day +40%, NYE +30%) and normal weekly pattern (Fri/Sat dinner peak).

## Why TensorFlow Over PyTorch (Mid-2020 Context)

This decision is documented for context — Claude Code does not need to revisit it. TensorFlow Extended (TFX) was the only mature end-to-end ML pipeline framework in mid-2020. TF Transform solved the training/serving skew problem by compiling preprocessing into the SavedModel graph. TF Serving had 4 years of production hardening. PyTorch's TorchServe v0.1 had just launched (April 2020) and was not yet production-ready. For a system needing weekly retraining, real-time scoring at <100ms p99, and tight ops coupling, TensorFlow + TFX won decisively.

## Project Directory Layout

```
uk-food-fraud/
├── docker-compose.yml
├── .env.example
├── README.md
├── Makefile
├── infra/
│   ├── postgres/init.sql
│   └── redis/redis.conf
├── db/
│   ├── alembic.ini
│   └── migrations/
├── shared/
│   ├── __init__.py
│   ├── models.py              # SQLAlchemy ORM
│   ├── db.py                  # session factory
│   ├── enums.py               # status enums
│   ├── uk_data.py             # UK constants (cities, postcodes, BINs, etc.)
│   └── money.py               # GBP/pence helpers
├── simulator/
│   ├── seed.py
│   ├── generator.py
│   ├── fraud_patterns.py
│   ├── lifecycle.py
│   ├── chargebacks.py
│   └── ground_truth.py
├── archival/
│   └── archiver.py
├── feature_store/
│   ├── aggregator.py
│   ├── client.py
│   └── batch_compute.py
├── ml/
│   ├── transform/preprocessing.py
│   ├── training/
│   │   ├── data_loader.py
│   │   ├── train_xgboost.py
│   │   ├── train_dnn.py
│   │   ├── evaluate.py
│   │   └── promote.py
│   ├── registry/model_registry.py
│   └── serving/tf_serving.Dockerfile
├── scoring_service/
│   ├── main.py
│   ├── rules_engine.py
│   ├── ensemble.py
│   ├── xgboost_server.py
│   └── feature_builder.py
├── monitoring/
│   └── dashboard.py
└── tests/
    ├── conftest.py
    ├── test_schema.py
    ├── test_archiver.py
    ├── test_simulator.py
    ├── test_fraud_patterns.py
    ├── test_feature_store.py
    ├── test_rules_engine.py
    └── test_scoring_e2e.py
```

## Phase Execution Order

Each phase has its own document. Build phases in order — each depends on previous phases being complete.

1. **PHASE_1.md — Foundation:** Docker Compose, PostgreSQL schema, SQLAlchemy models, Alembic migrations, archival job
2. **PHASE_2.md — Seeding & Legit Order Simulation:** UK-localised seed data, order generator (no fraud yet), lifecycle advancer
3. **PHASE_3.md — Fraud Injection & Chargebacks:** 7 fraud patterns, ground truth table with revoked permissions, delayed chargeback generator
4. **PHASE_4.md — Feature Store:** Redis-backed streaming + batch features for ML
5. **PHASE_5.md — ML Training Pipeline:** TF Transform preprocessing, XGBoost + Keras DNN training, evaluation
6. **PHASE_6.md — Scoring Service:** FastAPI scoring endpoint, rules engine, ensemble logic, TF Serving integration
7. **PHASE_7.md — Monitoring, Retraining, Polish:** Streamlit dashboard, weekly retraining cron, drift detection, README

## Acceptance Criteria (System-Wide)

The system is "done" when all of the following are true:

- `docker-compose up` brings the entire stack from cold start to generating orders within 90 seconds (seeding takes longer; the order generator must start within 90s of seeding completion)
- The simulator sustains 50 orders/second for 24 hours without errors
- Hot table contains only orders meeting the hot criteria; cold table grows as expected
- Scoring service maintains p99 latency <100ms under sustained 50 ord/sec load
- Trained ensemble model achieves AUPRC ≥ 0.75 on a held-out test set
- Per-fraud-category recall: stolen_card ≥0.70, account_takeover ≥0.60, promo_abuse ≥0.80
- Dashboard updates in real-time with all required panels
- Scoring service has no DB access to `simulator_ground_truth` (enforced at Postgres role level)
- pytest suite passes; ≥70% line coverage on non-ML code
- Full system can be torn down and rebuilt from scratch via `make reset`

## Schema Appendix

The full database DDL is included in PHASE_1.md and is the source of truth. Subsequent phases reference table and column names defined there.
