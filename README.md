# fraud-forecast

Production-realistic UK food-delivery fraud detection platform.

- **Postgres 12** (weekly-partitioned by `placed_at`) + **Redis 6** for
  hot/cold orders and feature serving.
- **Synthetic simulator**: 50 orders/sec sustained, 1M users / 15K stores /
  80K menu items / 2K drivers across 10 weighted UK cities, ~2% labeled
  fraud across 7 patterns + delayed chargebacks.
- **Fraud detection**: TF 2.3 + TFX 0.22 + XGBoost 1.2 ensemble; FastAPI
  scoring (p99 <100ms); Streamlit monitoring; weekly retraining.

Full spec: [`spec/MASTER.md`](spec/MASTER.md) and `spec/PHASE_1..7.md`.
Live status tracking: Linear project `fraud-forecast`, command center
[VOI-139](https://linear.app/voidaxiom/issue/VOI-139).

---

## Prerequisites

- **Docker Desktop** (or Docker Engine ≥ 20.10) with `docker compose`
  v2 — the platform runs entirely in containers; you do not need a
  Python or Postgres installation on the host.
- **macOS / Linux** (Windows untested). Apple Silicon and Intel both
  supported.
- **Disk:** allow ~4 GB for images + ~2 GB for the seeded database. A
  full simulator run grows the volume by ~1 GB/day at 50 ord/sec.
- **Ports:** 5432 (Postgres), 6379 (Redis), 8000 (scoring service —
  Phase 6), 8501 (Streamlit dashboard — Phase 7). Override any in
  `.env` (see `.env.example`).

## First run

```bash
git clone https://github.com/VoidAxiom/fraud-forecast.git
cd fraud-forecast
cp .env.example .env       # fill in any local overrides
make up                     # brings Postgres + Redis, runs migrations
make test                   # runs the pytest suite inside the compose stack
```

`make up` waits for the Postgres healthcheck, then runs `alembic upgrade
head` to create all tables and partitions. `make down` stops the stack;
`make reset` tears down volumes and re-creates the world from scratch.

## Make targets

The Makefile is the single entry point for the local loop. Per phase:

| Target                  | What it does                                            | Available from |
|-------------------------|---------------------------------------------------------|----------------|
| `make up`               | Start postgres + redis, run migrations                  | Phase 1        |
| `make down`             | Stop containers (preserves volumes)                     | Phase 1        |
| `make reset`            | `down -v` + `up` (destroys data)                        | Phase 1        |
| `make migrate`          | `alembic upgrade head`                                  | Phase 1        |
| `make psql`             | `psql` shell as the `app` role                          | Phase 1        |
| `make redis-cli`        | `redis-cli` shell                                       | Phase 1        |
| `make test`             | `pytest tests/ -v` inside the compose stack             | Phase 1        |
| `make typecheck`        | `mypy --strict shared/` (scope grows per phase)         | Phase 1        |
| `make lint`             | `ruff check . && ruff format --check .`                 | Phase 1        |
| `make logs`             | `docker compose logs -f --tail=100`                     | Phase 1        |
| `make ps`               | `docker compose ps`                                     | Phase 1        |
| `make seed`             | Run UK seed data generator                              | Phase 2        |
| `make simulate`         | Start the 50 ord/sec order generator                    | Phase 2        |
| `make score`            | Start the FastAPI scoring service                       | Phase 6        |
| `make dashboard`        | Start the Streamlit monitoring dashboard                | Phase 7        |
| `make train`            | Run the weekly retraining pipeline                      | Phase 5        |

Per-target detail is in the spec for that phase.

## Repository layout

```
fraud-forecast/
├── docker-compose.yml           # primary runtime
├── Makefile                      # local loop entry point
├── .env.example                  # all environment knobs
├── infra/                        # postgres init.sql, redis.conf
├── db/                           # alembic.ini + migrations
├── shared/                       # ORM models, UK data, money helpers
├── simulator/                    # order generator, fraud patterns      (Phase 2-3)
├── archival/                     # nightly hot→cold mover                (Phase 1)
├── feature_store/                # Redis-backed features                 (Phase 4)
├── ml/                           # TF Transform + XGBoost + Keras DNN    (Phase 5)
├── scoring_service/              # FastAPI scoring + rules + ensemble    (Phase 6)
├── monitoring/                   # Streamlit dashboard                   (Phase 7)
├── tests/                        # pytest suite (mirrors module tree)
├── spec/                         # MASTER.md + PHASE_1..7.md (source of truth)
├── docs/                         # design notes
├── scripts/                      # orchestration tooling
└── .claude/, .codex/, hooks/    # agent operating contracts
```

## Agent operating model

This repo is built by an agent system, not by hand. The build loop is
defined in [`CLAUDE.md`](CLAUDE.md) (director) +
[`.claude/agents/implementer.md`](.claude/agents/implementer.md) (per-packet
implementer subagent) + [`AGENTS.md`](AGENTS.md) (Codex worker contract).

- **Linear** is the planning ledger: project `fraud-forecast`, command
  center VOI-139, per-phase milestones, one parent issue per phase with
  sub-issues per deliverable cluster.
- **GitHub** is the delivery ledger: one PR per sub-issue with
  `Closes VOI-N`, squash-merged on a head-pinned Codex review + zero
  unresolved threads + green CI.
- **Codex** is the only writer of production code. Claude (the director)
  authors specs, runs scope gates, and does final integration.

Want to run a packet yourself? See `CLAUDE.md` for the full build loop.
The short version: read the spec in `spec/PHASE_<n>.md`, draft a packet
allowlist at `.codex-runs/<packet-id>/scope.txt`, provision a worktree
via `scripts/worktree-new.sh`, and dispatch the implementer subagent.

## Status

Phase status lives in the Linear command center
[VOI-139](https://linear.app/voidaxiom/issue/VOI-139) — its "Live status"
section is updated after every packet merge.
