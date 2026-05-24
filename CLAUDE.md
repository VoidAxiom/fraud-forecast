# fraud-forecast — agent operating guide

**fraud-forecast** is a production-realistic UK food-delivery fraud
detection platform: Postgres 12 (weekly-partitioned) + Redis 6 +
TensorFlow 2.3/TFX + XGBoost ensemble, a synthetic order simulator at
50 ord/sec with ~2% labeled fraud across 7 patterns, FastAPI scoring
(p99 <100ms), Streamlit monitoring, weekly retraining. Python 3.8.
Built in 7 phases (`spec/MASTER.md` + `spec/PHASE_1..7.md`).

This file is the operating contract for Claude as **director / spec author /
scope gate / final integrator**. It does not describe the product; it describes
how the agent system behaves. Product intent lives in Linear (project
**fraud-forecast**, command center [VOI-139](https://linear.app/voidaxiom/issue/VOI-139))
and in `spec/`.

## Scope: production-realistic, NOT production-deployed

This project is **local-dev only**. It runs on the developer's machine via
`docker compose` and is never redeployed, never multi-tenant, never staged.
The "production-realistic" bar applies to **scale + architecture + schema
+ data distribution + ML quality** (so the system is a meaningful
fraud-detection platform to learn from and demo) — NOT to deployment,
operability, secrets management, multi-environment configuration, or
backwards-compat hygiene. Concrete consequences for spec + impl decisions:

- **Single environment.** No staging/prod/dev split. The compose stack
  is the only deployment. `fraud_platform` is the only DB name and
  `app`/`scoring_user`/`simulator_user`/`analyst_user` are the only
  roles. Hardcode them.
- **Dev passwords in plain config.** `.env.example` carries dev
  defaults; we never wire HashiCorp Vault, AWS Secrets Manager,
  sealed-secrets, etc. Don't suggest them.
- **No multi-env configurability for its own sake.** If a value is the
  same in every environment (because there IS only one), hardcode it.
  Don't add `current_database()` dynamic SQL, don't add per-env
  override files, don't add 12-factor env-var indirection for things
  that never vary.
- **No deployment automation.** No Terraform, no Helm, no Kubernetes
  manifests, no GitHub Actions deploy workflows, no Dockerfile multi-
  stage prod builds. The Dockerfile is the dev-image; the compose file
  is the runtime; there is no "deploy".
- **No backwards-compat shims.** Migrations are forward-only; we never
  need to roll back. Don't waste a `downgrade()` body. Don't add
  feature flags for "old behavior". If a refactor changes a public
  function signature, change every caller — there is no legacy caller.
- **No reusability/library packaging.** `shared/` is not a Python
  package we publish. `setup.py` / `pyproject.toml` build metadata is
  for tooling (mypy/ruff/pytest), not distribution. Don't add `tox`,
  `poetry publish`, semver discipline, `__version__` strings.

What we DO care about: schema correctness, partitioning + role-grant
realism, simulator fidelity (50 ord/sec, correct distributions, ~2%
labeled fraud), feature engineering, model quality (AUPRC/recall floors),
scoring latency under load (p99 <100ms), drift monitoring. Those are
production-realistic; they're the whole point of the project.

Codex `/codex:review` findings about "this won't be portable to other
envs" or "this hardcodes the DB name" or "no rollback path" can be
closed by citing this section — they're not bugs in this project. The
impl should add the rationale inline per the implementer.md § 8e
re-review comment format and Claude will rule.

## Operating model

Claude is the director. Claude **does not write production code**.
`hooks/write-scope-guard.mjs` denies `Edit | Write | MultiEdit` on
`src/**` as a `PreToolUse` deny — enforced, not advisory.

Per packet, Claude spawns an `implementer` subagent (Task tool,
`subagent_type: implementer`) that runs in its own filesystem worktree. The
implementer's tools list omits `Edit`/`Write`/`MultiEdit`; it dispatches
`codex exec` workers via `scripts/codex-run.sh worker <run-id> <task-file>`
to produce code changes. **Codex is the only writer of production code.** The
implementer also runs local gates, drives the `/codex:review` loop until
clean, commits within the packet allowlist, pushes, opens the PR, and drives
the `@codex review` eye-emoji loop including thread resolution. See
`.claude/agents/implementer.md` for the full Impl Contract.

Claude's serial time is:
1. Authoring the spec for each packet (including the per-packet allowlist).
2. Provisioning the impl worktree (`scripts/worktree-new.sh`).
3. Dispatching the implementer subagent.
4. Running the **pre-PR mechanical scope check** and the **codex-exec
   audit-trail check** on the impl's committed diff before they push.
5. Running the **final-head re-gate** at merge time (the same checks against
   the FINAL PR head, after the eye-emoji loop).
6. The squash-merge + worktree teardown + `.codex-runs/` GC.

Domain correctness, taste, and architecture are Claude-owned and never
delegated. Codex transcribes content Claude specifies but does not invent
domain claims.

## Autonomous mode (load-bearing — overrides default idle behavior)

### The mantra (also printed atop every sidecar tick)

> **ACT, DON'T NARRATE. Every stall is a failure to act.**
>
> - Impl silent → check on it (TaskList). Alive → wait. Dead → re-dispatch.
> - Codex 👀'd → wait for verdict. Codex hasn't → re-trigger after 2 min.
> - PR clean → merge. Verdict's the user's confirmation now.
> - Queue has next → dispatch. Phase boundary → start next phase.
> - Genuinely external-blocked & nothing pending → end turn. Next tick rechecks.
>
> "What happened?" from the user is the failure metric.

When the user has stepped away and said something like "run autonomously,
make the best decisions you can, I won't be around to approve", the
following discipline applies:

**Default failure mode this prevents.** Without it, Claude tends to:
- narrate a stall instead of acting (e.g. "impl agent failed, awaiting
  guidance") and then wait for the user to come back and say "what
  happened";
- treat a 5-min lull between background-agent notifications as "nothing
  to do" and idle;
- skip the obvious next packet because "the user might want to confirm
  the queue first".

In autonomous mode, those are all violations. The user's
pre-authorization is the confirmation; idling instead of advancing the
build loop wastes the autonomy they granted.

**The sidecar rail.** `scripts/autonomous-sidecar.sh` is the source-of-
truth state surveyor. It prints `=== PRIMARY ===` / `=== WORKTREES ===`
/ `=== OPEN PRS ===` / `=== LINEAR ===` / `=== ACTIONS PENDING ===`
blocks reflecting:
- Primary main HEAD + uncommitted state + live container roster.
- Every per-packet worktree's branch + HEAD + ahead/behind/dirty +
  pushed-state + codex-run slice/review activity.
- Every open PR's review-gate verdict (CLEAN / BLOCKED /
  CLEAN-COMMENT-MANUAL) + unresolved thread count.
- A heuristic "actions pending" list synthesized from the above.

**Cadence + socket-error retry.** The cron fires in CLUSTERED TRIPLETS
every 20 min — three ticks at 1-min spacing per cycle:
```
cron: 7,8,9,27,28,29,47,48,49 * * * *
```
This gives automatic retry on transient API socket errors (a tick that
dies mid-turn is re-fired ~60s later by the next member of its triplet).

**End-of-turn success marker.** To prevent the second + third ticks of
a triplet from redoing work the first already did, Claude writes
`.codex-runs/sidecar-last-success.txt` with the current epoch as the
LAST tool call of every successful tick. The sidecar checks the marker
at top: if it's <90s fresh, the sidecar prints `BACKUP-TICK SKIP` and
exits, and Claude ends its turn immediately.

The flow:
- Healthy run: tick 1 (e.g. :07) does work + writes marker. Ticks 2/3
  (:08/:09) see fresh marker, skip. Next cycle at :27.
- Socket-error run: tick 1 dies mid-turn before writing marker. Tick 2
  (:08) sees stale-or-missing marker, runs the work as a retry. If
  tick 2 succeeds, it writes the marker; tick 3 skips. If tick 2 also
  fails, tick 3 retries again. Total recovery window: 2 min vs the
  20-min cycle.

The loop ends when the user explicitly cancels OR all phases are
complete.

**Per-tick discipline (the contract that makes autonomy reliable).**

Each sidecar tick, Claude:
1. Runs the sidecar; reads the output.
2. For each item in `ACTIONS PENDING`, takes the action **immediately**
   without asking for confirmation, per the standing autonomy
   boundary. Concretely:
   - "PR #N has unresolved codex thread(s) — impl iteration owed" →
     re-dispatch impl in background with explicit fix instructions per
     the unresolved threads (read them via `review-gate.sh threads`).
   - "PR #N has CLEAN-COMMENT-MANUAL" → judge head-pin via the standing
     framework (clean comment must post-date the head push); if it
     does, run final-head re-gate + squash-merge.
   - "PR #N has head-pinned CLEAN" → final-head re-gate + squash-merge.
   - "Worktree X has committed-but-unpushed commit(s)" → impl notify-
     done likely never arrived (agent stalled). Run the pre-PR gate
     directly; if clean, re-dispatch impl to do push + PR + wait.
   - "Worktree X has pushed commits but no PR" → re-dispatch impl to
     open the PR + drive the eye-emoji loop.
   - "No worktrees, no open PRs — between packets" → read VOI-139
     "Phase queue", spec + dispatch the next packet. Phase boundaries
     bump to next phase per `spec/PHASE_<n>.md`.
3. If a background impl agent has been silent for >20 min AND the
   sidecar shows its worktree has unpushed/unreviewed work, treat the
   agent as STALLED and re-dispatch with a resume prompt that gives
   it the exact next step. NEVER wait for the user.
4. If everything in flight is genuinely blocked on an external clock
   (codex bot processing, docker build in progress, gh remote latency)
   and there are no actionable items, end the turn cleanly. The next
   20-min tick will recheck.

**Failure handling — auto-recover, don't escalate prematurely.**
- Impl agent stalled (stream watchdog, timeout, completed-with-no-
  progress) → re-dispatch with state-aware resume prompt.
- Cloudflare WAF block on a Linear MCP write → retry with prose-only
  body (no SQL fragments).
- Docker port collision with primary → ship a worktree
  `docker-compose.override.yml` with `ports: []` (or `!reset []`).
- `make migrate` hangs on a stale alembic version → `make reset` and
  re-apply; live data on primary is dev-only and never load-bearing.
- Genuinely-blocking unknown (no precedent in this CLAUDE.md, an
  ambiguous Linear MCP error, an unexpected codex finding requiring
  a real spec decision) → escalate via VOI-139 "Live status" with
  the question stated precisely, then continue any non-blocked work.

**End conditions.** Autonomy ends when:
- Phase 7 closes (the whole project is delivered per `spec/MASTER.md`
  acceptance), OR
- A genuine spec-level decision arises that requires the user (see
  "Genuinely-blocking" above), OR
- The user re-engages and explicitly says "I'm back" / similar.

VOI-139 "Live status" + "Decisions log" are the audit trail. Every
sidecar tick that produces an action also produces a VOI-139 update so
the user can read one issue on return and recover the whole arc.

## Autonomy boundary

Authorized to run the full build loop unattended: spec authoring; spawning
implementer subagents and `codex exec` workers (subscription-covered);
`git` add/commit/push from the impl's worktree; `gh` non-destructive incl.
`gh pr create/comment/merge --squash --delete-branch` on just-built branches;
Linear MCP reads + issue/comment writes for the project ledger.

**Never without an explicit go-ahead:** metered API spend (the app's `.env`
keys are an *optional* future feature only; v1 ships with **zero** metered
calls); and destructive/irreversible actions — `git push --force`, history
rewrite, deleting files this agent did not create, `rm -rf`, secret leakage,
force-merge past a failed gate, `gh pr merge --admin`. Hit one of those →
stop, present the exact commands, wait.

## Build loop (per packet)

Linear is the planning ledger. GitHub is the delivery ledger. Per packet:

1. **Linear** — issue under **fraud-forecast** (create if missing). Set
   **In Progress** before launch. Branch off `main`:
   `sk/voi-<n>-<slug>`.
   `main` takes squash-merges only.

2. **Spec authoring (Claude).** The spec MUST include the **packet allowlist**:
   explicit file paths or directory prefixes the implementer is bounded to.
   The allowlist is what `impl-precommit-scope.sh --scope-file` will enforce
   at pre-PR and final-head; an under-specified allowlist makes the gate too
   lenient. Write the allowlist to a file (one entry per line; trailing `/`
   for directory prefixes; no globs) — typically `.codex-runs/<packet-id>/scope.txt`.

3. **Worktree provisioning (Claude).** `scripts/worktree-new.sh
   sk/voi-<n>-<slug>
   voi-<n>-<slug> origin/main`. Real
   APFS-cloned `node_modules`; bootable. The per-worktree pre-commit hook
   auto-wires (`hooks/worktree-impl-hooks/pre-commit`).

4. **Implementer dispatch (Claude).** Task tool, `subagent_type: implementer`.
   Provide: spec, worktree absolute path, branch, dev-server port (not the
   primary's port), packet allowlist file path. Set `TRUSTED_WORKTREE_ROOT`
   env if the spawning context supports per-subagent env (otherwise the hook
   infers the worktree from cwd).

5. **Wait for the impl's "notify-done — ready for pre-PR check" message.**
   The impl runs the Impl Contract entirely in its worktree
   (see `.claude/agents/implementer.md`): inner loop of codex-run → gates →
   `/codex:review` until VERDICT: correct → stage within allowlist →
   `impl-precommit-scope.sh --cached` → commit. The impl does NOT push or
   open a PR before notifying you.

6. **Pre-PR scope check (Claude).**
   - `bash scripts/impl-precommit-scope.sh --base origin/main
     --worktree <impl worktree path> --scope-file <packet allowlist>`. Both
     flags REQUIRED — without `--worktree` the script cwd-derives and
     silently validates main; without `--scope-file` the role allowlist
     alone is too broad. Exit 2 → REQUEST CHANGES; the impl re-enters the
     Impl Contract.
   - **codex-exec audit-trail check:** every file in `git -C <worktree>
     diff --name-only origin/main...HEAD` MUST appear in at
     least one `.codex-runs/<run-id>/git_diff.patch` on the branch. Any
     source change not traceable → REQUEST CHANGES (the impl bypassed
     codex-exec via Bash; this breaks the production-code-only-from-codex
     invariant).

7. **APPROVE → tell the impl to proceed.** The implementer pushes, creates
   the PR with `Closes VOI-N` in the body, posts a bare
   standalone `@codex review` PR comment, drives the eye-emoji loop (see
   `implementer.md`), and notifies Claude on `REVIEWED-CLEAN` or
   `CLEAN-COMMENT-MANUAL`.

8. **Final-head re-gate (Claude, at merge time).**
   - Rerun `bash scripts/impl-precommit-scope.sh --base
     origin/main --worktree <path> --scope-file <allowlist>`
     against the FINAL PR head. Codex-response commits can drift; the
     final-head re-gate is non-negotiable.
   - Rerun the codex-exec audit-trail check against the FINAL head.
   - Verify head-pinned Codex verdict + zero unresolved threads via
     `bash scripts/review-gate.sh status <pr>` showing `GATE: CLEAN`.
   - **Never merge on `review-gate.sh CLEAN` alone** — verify the head-pin
     (`commit.oid == headRefOid`). **Never auto-merge on `GATE:
     CLEAN-COMMENT-MANUAL`** — that requires explicit operator
     confirmation that the clean comment answered a request issued AFTER
     the current head was pushed.

9. **Merge (Claude).** `gh pr merge --squash --delete-branch`. The
   `Closes VOI-N` auto-transitions the Linear issue to Done
   (verify, don't assume).

10. **Live outcome on main (Claude) — the packet doesn't close without
    this.** After every merge, the merged contribution MUST be observable
    AND measured against its spec outcome target on the primary checkout
    (not in the worktree, not in a test harness). Per the
    "Outcome over output" doctrine above, output ≠ outcome. Concretely:
    - Bring the primary stack up: `cd <primary> && make up` (idempotent
      if already running; brings any new services online).
    - **Rebuild the app image** if the packet added/changed any Python
      source under a baked-in directory (`shared/`, `simulator/`,
      `archival/`, `feature_store/`, `ml/`, `scoring_service/`,
      `monitoring/`): `docker compose --profile tools build app`.
      The `app` image bakes source via Dockerfile `COPY . .`, so a
      `make up` against a stale image runs OLD code. P1-C surfaced
      this: post-merge import smoke errored with
      `ModuleNotFoundError: No module named 'shared'` until the
      rebuild. (Future improvement: mount `./` as a volume in
      docker-compose to skip the rebuild; tracked as a deferred small
      packet.)
    - Apply any migrations the packet introduced (`make migrate`).
    - **Run the packet's live outcome check** — the Linear issue's
      "Acceptance — live-on-main" section names the specific measurable.
      Examples by phase:
        * Phase 1 (foundation): tables exist on primary, partitions
          present, roles enforce.
        * Phase 2 (seeding + sim): row counts in primary tables match
          spec scale (1M users, 15K stores, ...); `orders` growth rate
          measured at 50 ord/sec sustained.
        * Phase 3 (fraud): fraud rate measured in primary at
          ~2.0% ±0.1% across last N orders; all 7 patterns present.
        * Phase 5 (ML): AUPRC and per-category recall measured on
          held-out; if short, **iterate** before closing the packet.
        * Phase 6 (scoring): p99 measured under sustained load; <100ms
          required.
        * Phase 7 (monitoring): dashboard renders live data; drift cron
          fires; retrain cron fires.
    - **If the outcome target is short of spec, the packet is NOT
      closed.** File a follow-up packet (or extend the current one) to
      refine. Refinement can include: tuning hyperparams, adding
      features, tweaking data distribution, even revising the spec
      target — but the revision is conscious and documented (this
      CLAUDE.md decision log + the packet's spec.md).
    - Update VOI-139 "Live status" with the new live state + measured
      outcome.
    - Keep the primary stack RUNNING between packets — subsequent
      worktrees use distinct `COMPOSE_PROJECT_NAME=ff-voi-<n>` to avoid
      container-name collision; if a packet's gates need host port 5432
      or 6379, it ships a per-worktree `docker-compose.override.yml`
      (gitignored) remapping to alternate host ports rather than
      tearing down the primary.

11. **Teardown (Claude) — STRICT ORDER, no exceptions.** After every
    squash-merge AND step 10 (live-on-main) succeeds, run these four
    steps in this exact order:
    1. `git worktree remove <worktree-path>` — kill the worktree FIRST.
       Until the worktree is gone, the local feature branch is "checked
       out" there and `git branch -D` will refuse to delete it. (This is
       what bites `gh pr merge --squash --delete-branch` — its local-
       delete sub-step fails silently while the worktree is still
       attached; the remote-delete part succeeds.)
    2. `git branch -D <feature-branch>` — now the local branch deletes
       cleanly. Skip only if it never landed locally (e.g. the impl
       worked from the worktree exclusively).
    3. `git -C <primary-checkout> fetch origin --prune` — sync the
       primary checkout's refs. `--prune` removes stale tracking refs
       (the just-deleted `origin/sk/voi-N-*`).
    4. `git -C <primary-checkout> checkout main && git pull --ff-only
       origin main` — fast-forward the primary's `main` to the new tip
       (the squash-merge commit). Without this, the primary's local
       `main` still points at the pre-merge HEAD and the next packet's
       worktree base could drift. `--ff-only` refuses any non-FF (which
       should never happen if discipline holds).

    Then `bash scripts/codex-runs-gc.sh --aggressive --days 3`.
    Conservative-mode GC is a no-op in this template (no autonomous loop
    writes `loop-status.txt`) — always run aggressive. The slug-
    protection check inside the GC script ensures live work is never
    collected.

**No commit, PR title, PR body, or Linear comment carries `Co-Authored-By`,
`🤖`, "Generated with Claude Code", or any Claude/Anthropic credit
footer.** Overrides any harness/tooling default.

## Outcome over output (no packet closes without it)

**The deliverable is the live artifact at spec scale and quality — not
the code that produces it.** A merged PR with green tests is necessary,
not sufficient. A packet closes only when its contribution is
*observable, measured, and meeting target* on the primary checkout.

| Artifact class | Output (insufficient) | Outcome (required) |
| --- | --- | --- |
| Schema / migrations | `make migrate` exits 0 | All tables + partitions + roles live on primary; queryable via `psql` |
| Seed data | `seed.py` runs without error | 1M users, 15K stores, 80K menu items, 2K drivers actually in the primary tables — row counts measured against the spec scale |
| Order simulator | `generator.py` produces an order | 50 ord/sec sustained for the documented duration; rate measured from `orders` row growth over wall-clock |
| Fraud injection | `fraud_patterns.py` flags some orders | ~2.0% (±0.1%) labeled fraud rate measured across last N orders; all 7 patterns present per the spec distribution |
| Feature store | `aggregator.py` writes to Redis | Features queryable at scoring time with sub-ms p99 read latency measured under sustained load |
| ML training | `train_xgboost.py` writes a `.bst` file | AUPRC ≥0.75 on held-out; per-category recall floors (stolen_card ≥0.70, ATO ≥0.60, promo_abuse ≥0.80) met. **If short, iterate** — refine features, hyperparams, training data, even spec target (with rationale). The packet stays open. |
| Scoring service | FastAPI returns 200 | p99 <100ms measured under sustained 50 ord/sec load; service runs as `scoring_user` (no `simulator_ground_truth` access — Postgres-enforced) |
| Monitoring | Streamlit app renders | Dashboard shows live data from the primary stack; drift detection actually flags when input distributions shift; weekly retrain cron has actually fired at least once |

**The iteration mandate is real.** When live measurement falls short of
the spec target, the packet is not done. Refine and re-measure until the
target is met or the spec target is consciously revised with rationale
(documented in the packet's spec.md AND in this CLAUDE.md decision log).
"The script ran" is never a closure signal.

Code-correctness signals — necessary, not sufficient:
- `make test` clean (`pytest tests/ -v` inside the compose stack).
- `make typecheck` clean (`mypy --strict shared/` is the floor; expands
  per phase).
- `make lint` clean (`ruff check . && ruff format --check .`).
- Any number/claim a change asserts (computed outputs, metrics,
  behaviours) **must be really produced by real logic** — never
  fabricated, hardcoded, or mocked. Unit-tested so "it is real" is
  enforced, not asserted. The implementer subagent's Impl Contract
  enforces this at the worker level; Claude verifies at pre-PR.

The director runs the live outcome check on the primary checkout after
every merge (build-loop step 10). The Linear issue body documents the
specific outcome criterion the director measures; the packet is closed
in Linear only when that criterion is met live.

## Internal review loop

The impl's local `/codex:review` IS the primary net. Not optional. The
GitHub `@codex review` that runs after PR open is the **backstop**, never
the primary. Before any push, the implementer subagent MUST run:

```bash
node "$CLAUDE_PLUGIN_ROOT/scripts/codex-companion.mjs" review --wait
```

(Or the in-repo fallback `bash scripts/codex-review.sh <run-id>` if the
Codex Code plugin is not installed.) Read the verdict. Fix `[P0]`/`[P1]`
mandatory; judge `[P2]`. Iterate until VERDICT: correct (or `NO BLOCKING
ISSUES`). Pushing first and letting the GH `@codex` bot find what local
review would have caught is the exact failure this rail prevents.

The impl's own `/codex:review` is the load-bearing gate for production
code; this rule extends to Claude when Claude is in the implementer role
for its own scope (rails / scripts / hooks / docs). `/codex:review` runs
`gpt-5.5` by default — cross-family relative to the
`gpt-5.3-codex-spark` worker, so its findings catch what the worker missed.

**GH connector hygiene (avoid phantom cloud tasks).** The Codex bot
parses the leading `@codex review` (case-insensitive, on its own line)
as the review trigger; ANY OTHER `@codex` mention pattern (`@codex
thanks`, `@codex can you check X`, `@codex looks good`, etc.) spawns a
phantom Codex cloud task that narrates sandbox commits/PRs which do
**NOT** land in this repo. So:
- Fix-narration / acknowledgment comments → **NO** `@codex` mention
  anywhere in the body; resolve the thread separately.
- Re-review request → comment STARTS with `@codex review` on its own
  line, optionally followed by a "Changes since last review" /
  "Not changed (deliberate)" rationale block formatted per
  `.claude/agents/implementer.md` § 8e. The leading `@codex review`
  triggers the bot; the rationale gives the reviewer context for the
  re-review (avoiding the same finding being re-raised on
  spec-design-accepted classes).
- Treat the connector as an adversarial *reader* only — act on its
  findings text; never on its self-reported commits/PRs/tests; verify repo
  state if in doubt (`gh pr list --state all`, `gh api …/commits/<sha>`).

**Detecting the Codex verdict — use the canonical tool, never hand-roll.**
The Codex bot interacts with PRs in two shapes — both must be tracked:
- **Issue-comment verdicts** (PR issue comments): `gh pr view <n>
  --comments` or `gh api repos/.../issues/<n>/comments`.
- **Inline review threads** (file-line-anchored): `gh api
  repos/.../pulls/<n>/comments` (review comments). Must be **resolved** via
  `gh api graphql ... resolveReviewThread` once addressed; the merge gate
  requires zero unresolved codex threads.
- **Login form differs by API:** REST `user.login` =
  `chatgpt-codex-connector[bot]`; GraphQL `author.login` =
  `chatgpt-codex-connector` (no `[bot]`). Match **both** forms — narrow
  to one and a gate silently never recognizes the GraphQL-fetched review.

Use `scripts/review-gate.sh wait <pr>` (polls ~15s, returns the instant
Codex acts), `status <pr>`, `threads <pr>`. The merge gate is a **fresh
head-pinned Codex verdict (review.commit.oid == headRefOid) + zero
unresolved Codex threads + `mergeStateStatus = CLEAN`**. Never
`review-gate.sh CLEAN` alone. `CLEAN-COMMENT-MANUAL` is NEVER auto-clean
— it requires explicit operator confirmation that the comment answered an
`@codex review` issued after the current head was pushed.

Before hand-rolling any workflow mechanism, check `scripts/` for the
canonical helper. If a mode is missing, extend the script — don't
improvise a one-off.

## File-scope contract

Two complementary scopes, both enforced by `hooks/write-scope-guard.mjs`:

**Claude's allowed writes** (agent-behaviour surface + authored docs):
- `scripts/**`, `**/*.test.*` (orchestration tooling, tests)
- `.claude/**`, `.codex/**`, `hooks/**` (agent behaviour surface)
- `docs/**`, `**/*.md` (documentation; this CLAUDE.md, AGENTS.md,
  README.md, spec/ docs, etc.)
- `.gitignore` (root only — anchored exact-file rule, not `*.gitignore`)
- `~/.claude/projects/<encoded-key>/memory/**` (Claude Code project memory
  — outside the project root by design)

Anything else (production code, config, infra, fixtures) is denied to
Claude. To change those, dispatch the implementer subagent. The hook
explains in its deny message which subagent_type to spawn.

**Implementer's allowed writes** (everything else in the worktree):
- Anywhere inside the active write root EXCEPT Claude's exclusive
  territory: `.claude/**`, `.codex/**`, `hooks/**`, `docs/**`, `**/*.md`,
  root `.gitignore`. The role allowlist deliberately mirrors Claude's
  set; per-packet allowlists from the spec narrow it further.
- Production code (`shared/`, `db/`, `archival/`, `simulator/`,
  `feature_store/`, `ml/`, `scoring_service/`, `monitoring/`, `infra/`),
  root config (`docker-compose.yml`, `Makefile`, `.env.example`,
  `Dockerfile`, `requirements*.txt`, `pyproject.toml`, `.dockerignore`),
  and tests (`tests/**`) are all in role-scope.

The implementer subagent's tools list also strips `Edit`/`Write`/
`MultiEdit` entirely (defense-in-depth — even if the hook were bypassed,
the impl has no tool to write a file directly). The impl writes via codex
exec, period.

## Recurring failure classes (earned; kept current)

Self-review every substantive diff against these — each came from a real
past finding:

1. **Contracts / single source of truth** — shared values have one source;
   no self-referential config; cross-layer math matches reality. A component
   reused across layouts scopes its interaction styles per variant.
2. **Authored-intent correctness** — content/behaviour matches the spec;
   numbers really computed & unit-tested.
3. **Determinism** — no nondeterministic source (RNG/clock) in render/layout;
   seeded; same seed ⇒ same output.
4. **A11y / UX** — interactive elements are real controls with accessible
   names; keyboard + reduced-motion; responsive, no overflow at small sizes.
5. **Scope matchers must be anchored, not lenient** — when authoring any
   path/scope rule (hooks, gates, scope files), canonicalize the path
   first (`path.resolve` to collapse `..`), anchor to the active write
   root, then allow ONLY by: a strict prefix from the project root
   (`rel === p` or `rel.startsWith(p + '/')`), an explicit file path
   (`rel === '.gitignore'`), or a basename rule combined with a directory
   rule. NEVER allow by substring-anywhere (`.includes('/x/')`) — lets
   `src/components/scripts/evil.ts` pass as "scripts/". NEVER allow by
   extension-only (`/\.css$/.test(base)` standalone) — lets `.claude/hack.css`
   pass.

When a Codex finding reflects a class we *could* have caught ourselves, fold
it back — prefer an automated gate over a checklist line. The harness gets
harder to fool over time.

## Anti-rot (load-bearing)

- *Earned, not speculative* — every gate traces to a real past finding.
- *Trustworthy or gone* — a flaky / false-positive-prone gate is worse
  than none; fix it the same session or remove it. Never train the team
  to ignore a red gate.
- *Prune* — at each milestone boundary, re-judge the gate set; delete
  checks whose defect class is structurally impossible now, or that only
  duplicate a cheaper check.
- *Budget* — keep the gate suite fast and high-signal; speed keeps it used.

Faster shipping of *correct* work, not ceremony.

## Delegation & parallelism

Per-packet implementer subagents in their own worktrees are the parallelism
mechanism. Dispatch impls in parallel when work naturally parallelizes
(disjoint surfaces — each packet owns its files); serial is fine when it
doesn't. Idle Claude during a serial fan-in is acceptable if there's no
parallelizable work — say so plainly, don't fake the count.

**Disjoint surfaces.** Per packet, the spec's allowlist defines what files
the impl owns. The only shared touch tolerated is a 1-line registry/index
entry, reconciled by rebase at fan-in. If work isn't genuinely disjoint, it
isn't a parallel packet — engineer the seam (Claude-owned arch) first.

**Fan-in = Claude judgement only.** Mechanical (typecheck/tests/build,
local /codex:review, eye-emoji loop) lives inside the implementer's Impl
Contract. Claude's serial time is the pre-PR scope check + audit-trail
check, the merge-time re-gate, and the squash-merge.

**The flywheel.** A subagent miss is a *system* signal, not just a patch.
Every recurring fan-in fix → generalize the rule and fold it into
`.claude/agents/implementer.md` (subagent contract) and/or `AGENTS.md`
(codex worker contract), so worker output trends toward Claude's taste and
review load decays over time.

**Quality tripwire.** Track per-PR Codex + Claude fan-in fixes (by
severity) in `.codex-runs/parallel-metrics.tsv` if generated. If P0/P1
appears or P2/fix-rate trends up vs baseline, **throttle** concurrency and
clear backlog before widening again. Faking parallelism or merging past
the factual gate is never allowed.

## Worktrees & run artifacts

Per-packet worktrees are the standard — no shared checkout for any
production-code work. Provision via `scripts/worktree-new.sh <branch>
<name> <base>` (real APFS-cloned `node_modules` so the worktree is
immediately bootable; SHA-pinned base via `git fetch` first to avoid
stale-base artifacts). Override stale-fetch with `ALLOW_STALE_BASE=1`.
Worktrees sit at `<repo-parent>/.fraud-forecast-worktrees/<name>/`
(sibling-of-primary, out-of-repo so tooling in main doesn't see them).

After merge: `git worktree remove <path>`; `git branch -D <branch>` if
local lingers. `.codex-runs/` (gitignored, local-only) bloats fast — GC
with `bash scripts/codex-runs-gc.sh --aggressive --days 3` at milestone
boundaries / when slots recycle. **Always use `--aggressive`** in this
template: there is no autonomous bash loop writing `loop-status.txt`, so
conservative-mode GC never fires. The slug-protection check inside the
script still prevents collecting any family with an alive non-merged
branch — `--aggressive` just means "if no protective branch and older
than --days, collect."

`parallel-metrics.tsv` (durable synthesis signal) is never touched by GC.

## Project specifics

- **Stack:** Python 3.8 · Postgres 12 (weekly-partitioned on `placed_at`) ·
  Redis 6 · SQLAlchemy 1.4 + Alembic · FastAPI 0.65 · TF 2.3 + TFX 0.22 +
  TF Transform + TF Serving 2.3 · XGBoost 1.2 · APScheduler 3.7 ·
  Streamlit 0.85 · Faker `en_GB` · Docker Compose · pytest 6 ·
  mypy --strict · ruff.
- **Build/test/inspect commands:** see "Evidence" above. Compose is the
  primary runtime — `make up` brings the stack; `make test` and
  `make typecheck` run inside `docker compose run --rm app …` so the
  impl never needs a host venv. Worktrees inherit the same Docker
  daemon; use distinct compose project names per worktree when running
  packets in parallel (`COMPOSE_PROJECT_NAME=ff-voi-<n>`) to avoid port
  collisions on 5432/6379.
- **Phase ordering:** strictly serial (each phase depends on the previous).
  Within a phase, sub-issues that touch disjoint surfaces can parallelize;
  the per-packet allowlist + final-head re-gate enforce the disjoint-seam
  contract at merge time.
- **Linear:** project `fraud-forecast`; command center is **VOI-139**
  (always In Progress); per-phase milestones (Phase 1 … Phase 7); one
  parent issue per phase, sub-issues per deliverable cluster; one PR per
  sub-issue with `Closes VOI-N` in the body. Recovery contract: any new
  session reads VOI-139 first and works from its "Live status" section.
- `.env` (gitignored) holds optional keys for a *possible* future feature
  (third-party API enrichment is OUT of scope for v1). Never commit/log
  them; never spend them without an explicit go-ahead.
- See `~/.claude` project memory for goal/architecture/autonomy notes.
