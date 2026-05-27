# REVIEW.md — Conventions for reviewing this project

## Scope

**fraud-forecast** is a production-realistic UK food-delivery fraud detection
platform, but it is **local-dev only**: docker compose, single environment,
never redeployed. "Production-realistic" applies to schema correctness, scale
targets (50 ord/sec, ~2% fraud, p99 <100ms), data distributions, partition
+ role realism, ML quality — NOT to deployment, secrets management,
multi-env config, or backwards-compat hygiene (see CLAUDE.md §Scope).

## Severity calibration for THIS project

- **P0** — drop everything. Secret leakage in commits; data-loss migration;
  broken `make up` on clean checkout; role-separation bypass (e.g. scoring
  service granted access to `simulator_ground_truth`).
- **P1** — must fix this cycle. Divergence from `spec/PHASE_*.md` verbatim
  contract; hardcoded/mocked number where real computation was required
  (AGENTS.md §Test-value contract); naive `datetime.now()` on a `TIMESTAMPTZ`
  path (AGENTS.md §Backend correctness); float for GBP money.
- **P2** — fix eventually. Substring scope matcher instead of anchored prefix
  (CLAUDE.md §Recurring failure classes #5); partition-routing change
  without a `pg_class` test (AGENTS.md §Backend correctness); pytest expected
  literal without derivation comment.
- **P3** — nice to have. Naming, doc polish, comment that would age out.

## Out of scope — DO NOT FLAG

Close these by citing CLAUDE.md §Scope:

- **Deployment portability.** No staging/prod split; no Terraform/Helm/K8s;
  compose file IS the runtime.
- **Secret management.** Dev passwords in `.env.example` are intentional;
  no Vault / sealed-secrets findings.
- **Multi-env configurability.** `current_database()` dynamic SQL, per-env
  override files, 12-factor indirection for values that never vary — out
  of scope. Hardcode `fraud_platform`, the four Postgres roles, etc.
- **Backwards-compat shims.** Migrations forward-only; no `downgrade()`
  bodies; no feature flags for "old behavior".
- **Reusability / packaging.** `shared/` is not a published package; no
  `tox`, semver, `__version__` strings.
- **Scaling beyond spec.** The spec targets (50 ord/sec, p99 <100ms) are
  the bar. "What if 10,000 ord/sec" is out of scope.
- **Frontend / a11y / UX.** No UI (Phase 7 Streamlit dashboard excepted —
  it's read-only).
- **Metered API spend.** v1 ships with zero metered calls; optional `.env`
  keys are future feature.

## Project-specific anti-patterns to flag

Format: **Pattern** → severity | rationale | suggested action.

- **GBP as `float` / `Decimal`; not BIGINT pence** → P1 | AGENTS.md §Backend
  correctness ("Money is integer pence, never float") | "Change to `int`
  (pence); route through `shared/money.py` if rounding needed."
- **`datetime.now()` / `datetime.utcnow()` without injected clock** → P1 |
  AGENTS.md §Backend correctness (Timezone discipline) | "Inject a clock;
  production passes `lambda: datetime.now(LONDON)`; tests pass a fixed clock."
- **Displayed/asserted number is hardcoded or mocked instead of computed
  by real logic** → P1 | AGENTS.md §Test-value contract; CLAUDE.md §Outcome
  over output | "Replace literal with real computation; add a pytest that
  asserts through the production path."
- **Pytest expected literal with no derivation comment** → P2 (P1 if the
  literal is wrong) | AGENTS.md §Backend correctness | "Add the arithmetic
  as a comment next to the literal."
- **Migration rewrites a merged migration, or autogenerates DDL for
  partitioning/roles/functions instead of `op.execute(raw_sql)`** → P1 |
  AGENTS.md §Backend correctness | "Revert; add a new forward migration."
- **Partitioned-table change without a `pg_class` routing test** → P2
  (P1 if partition key changed) | AGENTS.md §Backend correctness | "Add
  routing test that inserts across two weeks and queries `pg_class`."
- **Nondeterministic source on a test-asserted path** (`random.random()`
  unseeded, `datetime.now()` un-injected, `uuid.uuid4()` in a seeded
  context) → P1 | AGENTS.md §Backend correctness; CLAUDE.md §Recurring
  failure classes #3 | "Thread a seeded `Random()` / injected
  clock / injected uuid generator; assert deterministic output."
- **Paper over a Postgres permission error by escalating role** → P0 |
  AGENTS.md §Backend correctness (Role-separation) | "Revert the escalation;
  either grant the specific privilege in a migration with rationale, or
  fix the caller to use the right role."
- **Scope / path matcher uses substring or extension-only, not anchored
  prefix** → P2 (P1 if security-gating) | CLAUDE.md §Recurring failure
  classes #5 | "Canonicalize path first; allow only by strict project-root
  prefix, explicit file, or basename+dir combo."
- **Self-referential config; value defined in two places that must
  agree** → P2 | CLAUDE.md §Recurring failure classes #1 | "Pick one
  source; import the other side from it."
- **Source-tree file in PR diff is NOT traceable to any
  `.codex-runs/<run-id>/git_diff.patch` on the branch** → P1 |
  implementer.md §You do not write code | "Re-produce via a codex-run
  worker so the audit trail is complete; revert the bypass-written bytes
  first."

## When the spec is the contract

`spec/PHASE_*.md` is source-of-truth for every packet. Divergence from
the relevant section is **P1 minimum** (P0 if downstream code already
depends on a different contract). Quote the spec line in the finding. If
the spec is genuinely ambiguous, flag as **P2 "spec needs clarification"**
rather than assuming intent.

When divergence is deliberate, the packet `spec.md` in
`.codex-runs/<packet>/spec.md` MUST include a "Divergence from source"
block (CLAUDE.md §Spec authoring discipline). Absence is itself a P1.

## "Earned, not speculative" rule

Every finding must be grounded in a concrete behavior the code exhibits,
not speculation about what *might* happen at scale, in a future env, or
under inputs the project doesn't accept. "Trustworthy or gone" (CLAUDE.md
§Anti-rot) — a finding the author can dismiss with a one-line rebuttal
shouldn't have been raised. Prefer no finding over a weak finding, especially
at P0/P1.

## When `tested` tag matters

Tests are required when: (a) the change introduces or modifies a displayed
/ asserted number; (b) the change touches partition routing; (c) the change
introduces nondeterminism the simulator depends on; (d) the packet acceptance
criterion explicitly names a test (CLAUDE.md §Outcome over output table;
AGENTS.md §This repo). Tests are NOT required just because a file changed —
don't flag missing tests on refactors, comment edits, or pure-typing changes.

## Multi-reviewer behavior

- If your finding contradicts another reviewer's finding on the same PR,
  note this in your rationale.
- Convergence between reviewers is signal; divergence is hypothesis.
- Do not soften your finding to align with another reviewer. Surface
  disagreement explicitly.

## What lint/types/tests already catch

`ruff check . && ruff format --check .` enforce style; `mypy --strict
shared/` (expanding per phase) catches type lies; `pytest` runs inside
`docker compose run --rm app` against real Postgres/Redis. Pre-commit hook
in each worktree runs `scripts/impl-precommit-scope.sh` against staged
diff. Flag only where the diff escapes these (typing widened to `Any`,
`# type: ignore` without comment, test deleted/`xfail`'d to pass a gate,
scope check defeated by writing outside the worktree).

## What humans add that bots can't

- **Spec intent vs spec letter** — faithful to what the spec was *trying*
  to specify, or just to the letter?
- **Recurring-failure-class re-emergence** — has a class CLAUDE.md
  §Recurring failure classes already absorbed come back in new clothes?
- **Cross-packet drift** — does this PR make a future packet's documented
  contract harder to honor?
- **Genuinely-blocking unknowns** that need a director ruling, not another
  code-side iteration.
