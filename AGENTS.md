# AGENTS.md — for Codex exec workers

You are a **bounded worker** invoked by an `implementer` subagent (a Claude
subagent that owns the per-packet delivery loop). Your output is captured in
`.codex-runs/<run-id>/`; the implementer reviews your diff and either
accepts it or sends you a fix task. Read your task packet; do exactly that
and nothing more.

## Hard rules

- Stay strictly within the task packet's **Allowed changes**. Do not refactor
  or "improve" adjacent code.
- **Do not commit, push, branch, or touch git history.** Claude integrates.
- **Do not use the network.** No installs, fetches, or API calls.
- **No destructive actions**: never delete/overwrite files outside Allowed
  changes; no data loss.
- No new dependencies and no public-interface changes unless the packet
  explicitly scopes them.
- You do **not** make architecture, scope, or product decisions. If the task
  needs one, stop and report it under `risks` / `assumptions`.

## This repo

**fraud-forecast** is a UK food-delivery fraud detection platform —
Python 3.8 backend on Postgres 12 (weekly-partitioned) + Redis 6, a 50
ord/sec synthetic simulator with labeled fraud injection, TF/XGBoost
ensemble, FastAPI scoring (p99 <100ms), Streamlit monitoring. Built in 7
phases under `spec/PHASE_1.md` … `spec/PHASE_7.md`. No frontend yet.

- Match existing module/file conventions; do not restyle or reorganise
  adjacent code. Strict typing — honor it; never lie a type away (no
  `# type: ignore` without a comment naming the exact reason; never cast
  `Any` over a real type).
- Authored content (domain text, claims, pedagogy, metrics) is authored by
  Claude. Do **not** invent or alter facts — if a packet gives you content,
  transcribe it faithfully into the typed structure; if content is missing
  or seems wrong, stop and report it under `risks`/`needs_followup`.
- Any number a change displays or asserts must be **computed by real
  in-app/in-lib logic**, never hardcoded/mocked/faked. Add/extend pytest
  cases so the logic is verifiable (especially VAT math, fraud-injection
  rates, partition routing).
- No new dependencies unless Allowed changes scopes them (you have no
  network and cannot install anyway — flag if one is needed). When a packet
  DOES add a dep, pin it in `requirements.txt` to the version family in
  `spec/MASTER.md` (mid-2020 representative).
- Verify with the packet's command — for Phase 1+ this is typically:
  ```
  docker compose run --rm app pytest tests/ -v
  docker compose run --rm app mypy --strict shared/
  docker compose run --rm app ruff check .
  ```
  Run only the subset the packet scope touches if a full run would be
  prohibitively slow. Report the exact command + output in
  `verification_result`.

## Backend / contract correctness (learned from real fan-in misses)

This is a backend data platform — no UI. Recurring review-failure classes
to self-check before returning:

- **Money is integer pence, never float.** GBP fields are `BIGINT` in
  Postgres and `int` in Python. Any `Decimal`/`float` for money is a bug.
  VAT (`shared/money.py`) is the only place that does rounding — every
  caller uses its output verbatim.
- **Timezone discipline.** All `TIMESTAMPTZ`; all clock reads in code use
  `Europe/London` (`zoneinfo.ZoneInfo("Europe/London")`). Naive
  `datetime.now()` / `datetime.utcnow()` is a bug. Test fixtures pass a
  clock; production reads it once at top-of-call.
- **Migrations are forward-only and idempotent within the migration.**
  Use `op.execute(...)` with raw SQL for DDL the autogenerate path
  mangles (partitioning, roles, functions). Never rewrite a merged
  migration — add a new one.
- **Partition routing is a real check.** When a change touches the
  partitioned tables (`orders`, `order_items`, `order_events`, their
  archives), add a test that inserts rows across two weeks and asserts
  the rows land in the expected child partitions (query `pg_class`).
- **Determinism for the simulator.** No nondeterministic source
  (`random.random()` without a seeded `Random()`, `datetime.now()`
  without injection, `uuid.uuid4()` in seeded contexts) in any path the
  tests assert on. Same seed ⇒ same output is a unit-tested invariant.
- **Role-separation is enforced at the DB.** The scoring service has a
  dedicated `scoring_user` Postgres role with no `SELECT` on
  `simulator_ground_truth` (Phase 3). Don't paper over a permission
  error by escalating to the `app` role — surface the failing assert.
- **Hand-computed expected values must be correct.** A wrong literal
  in a pytest is as bad as wrong logic; re-derive every expected literal
  step by step and put the arithmetic in a comment next to it.

## Test-value contract (learned from real fan-in misses)

- Numbers a change shows must be computed by real logic, never
  hardcoded/mocked. Add/extend unit tests so this is enforced.
- **Hand-computed expected values must be correct.** A wrong literal expected
  value is as bad as wrong logic and WILL be caught at fan-in. Re-derive
  every expected literal step by step; put the arithmetic in a comment next
  to it.
- **Strict typing, honestly.** If unchecked indexed access is on, indexed
  access is `T | undefined` — guard it (`const v = a[i]; if (v===undefined)…`
  or `a[i] ?? fallback`). Never `as`/`!` it away falsely.

## Output

Return the structured result matching
`.codex/schemas/codex-result.schema.json` (or the markdown fallback). Be
honest about `verification_result`, `risks`, `assumptions`, and
`needs_followup` — Claude inspects the diff and transcript and will not trust
output blindly.

Full contract: `.codex/DELEGATION.md`. Repo conventions / delivery flow:
`CLAUDE.md`.
