# fraud_forecast — agent operating guide

**fraud_forecast** — TODO — fill in once the stack is decided

This file is the operating contract for Claude as **director / spec author /
scope gate / final integrator**. It does not describe the product; it describes
how the agent system behaves. Product intent lives in Linear (project
**fraud-forecast**) and in `docs/`.

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

10. **Teardown (Claude).** `git worktree remove <path>`; `git branch -D
    <branch>` if local lingers; `bash scripts/codex-runs-gc.sh --aggressive
    --days 3`. Conservative-mode GC is a no-op in this template (no
    autonomous loop writes `loop-status.txt`) — always run aggressive. The
    slug-protection check inside the GC script ensures live work is never
    collected.

**No commit, PR title, PR body, or Linear comment carries `Co-Authored-By`,
`🤖`, "Generated with Claude Code", or any Claude/Anthropic credit
footer.** Overrides any harness/tooling default.

## Evidence (no change merges without it)

Any number/claim a change asserts (computed outputs, metrics, behaviours)
**must be really produced by real logic** — never fabricated, hardcoded, or
mocked. The logic that produces it is unit-tested so "it is real" is
enforced, not asserted. The implementer subagent's Impl Contract enforces
this at the worker level; Claude verifies at pre-PR.

Per change:
- `TODO` clean (typecheck + tests, incl. any determinism tests).
- `TODO` clean.
- If a visual/runtime inspection harness exists, `TODO` — say
  what you observed and judged.

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

**GH connector hygiene (avoid phantom cloud tasks).** ANY `@codex` PR
comment that is not EXACTLY `@codex review` spawns a Codex cloud task that
narrates sandbox commits/PRs which do **NOT** land in this repo. So:
- Fix narration → comment with **NO** `@codex` mention; resolve the thread.
- Re-review → a bare standalone **`@codex review`** and nothing else.
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

## File-scope contract (Claude's own scope)

Claude itself is bounded by `hooks/write-scope-guard.mjs`. Allowed Claude
writes:
- `scripts/**`, `**/*.test.*` (orchestration tooling, tests)
- `.claude/**`, `.codex/**`, `hooks/**` (agent behaviour surface)
- `docs/**`, `**/*.md` (documentation; this CLAUDE.md, AGENTS.md, etc.)
- `.gitignore` (root only — anchored exact-file rule, not `*.gitignore`)
- `~/.claude/projects/<encoded-key>/memory/**` (Claude Code project memory
  — outside the project root by design)

Anything else (including all `src/**`) is denied. To change
production code, dispatch the implementer subagent. The hook explains in
its deny message which subagent_type to spawn.

The implementer subagent's tools list strips `Edit`/`Write`/`MultiEdit`
entirely (defense-in-depth — even if the hook were bypassed, the impl has
no tool to write a file directly). The impl writes via codex exec, period.

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

- Build/test/inspect commands: see "Evidence" above (configured at
  template instantiation; edit here as the project evolves).
- `.env` (gitignored) holds optional keys for a *possible* future feature.
  Never commit/log them; never spend them without an explicit go-ahead.
- See `~/.claude` project memory for goal/architecture/autonomy notes.
