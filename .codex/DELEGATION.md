# Claude → Codex Exec Delegation Contract

Canonical in-repo copy of the delegation contract. Claude is director,
architect, taste owner, orchestrator, reviewer, integrator, final judge.
`codex exec` is the implementation substrate. Codex is the flood; Claude
guides the flow.

## Ownership

**Claude owns:** product direction, architecture, decomposition, taste,
feedback loops, integration, final acceptance.

**Codex exec owns bounded work:** implementation slices, tests/fixtures/
scripts, narrow bug fixes, codebase exploration, runtime inspection, PR
feedback fixes, alternate implementation attempts.

Codex does **not** own direction, architecture, scope, or final judgment.

## Core rule

Delegate when work is **bounded, separable, describable, verifiable**. Never
delegate "build the whole feature" — only scoped packets.

## Command modes

Driven by `scripts/codex-run.sh <role> <run-id> [task-file]`. Canonical forms:

**Explorer (read-only):**

```bash
codex exec --json --sandbox read-only \
  -c model="$CODEX_MODEL" \
  --output-schema .codex/schemas/codex-result.schema.json \
  -o .codex-runs/<run-id>/result.json \
  "$(cat .codex-runs/<run-id>/task.md)" \
  > .codex-runs/<run-id>/events.jsonl \
  2> .codex-runs/<run-id>/stderr.log
```

**Worker (bounded edits):**

```bash
codex exec --json --sandbox workspace-write \
  -c approval_policy="on-request" \
  -c approvals_reviewer=auto_review \
  -c sandbox_workspace_write.network_access=false \
  -c model="$CODEX_MODEL" \
  --output-schema .codex/schemas/codex-result.schema.json \
  -o .codex-runs/<run-id>/result.json \
  "$(cat .codex-runs/<run-id>/task.md)" \
  > .codex-runs/<run-id>/events.jsonl \
  2> .codex-runs/<run-id>/stderr.log
```

`approvals_reviewer=auto_review` handles runtime approval prompts only — it is
**not** PR review and does **not** expand the sandbox. Network is off by
default; do not enable network or API spend unless explicitly authorized.

## Run packet

`scripts/codex-run.sh` creates `.codex-runs/<run-id>/` with: `task.md`,
`command.sh`, `events.jsonl`, `stdout.md`, `stderr.log`, `exit_code.txt`,
`result.json`, `git_diff.patch`, `git_diff_stat.txt`, `files_changed.txt`,
`artifacts/`, `metadata.json`. This is the external-subagent transcript.
`.codex-runs/` is git-ignored.

## Task packet

Every task.md follows `.codex/task-template.md`: role, single bounded
objective, minimal context, scope, allowed/forbidden changes, constraints
(follow AGENTS.md, no scope expansion, no architecture decisions, no deps
unless allowed, no public-interface changes unless scoped, **do not commit**,
no network unless authorized, no destructive actions), verification command,
"return structured output matching the schema."

## After every run — do not trust Codex blindly

Inspect: exit code, result.json/stdout, stderr.log, events.jsonl, git diff
stat, changed files, the actual diff, verification output, artifacts. Then
decide: accept / revise / discard / run another Codex / fix directly /
escalate to CI+PR review. Claude remains final integrator.

## Feedback loop

Implementation is not success; **measured improvement** is. Per slice:
define "good" → delegate bounded work → inspect result/diff/artifacts →
verify (tests/build/runtime) → judge with Claude taste → iterate until good
enough. Mediocre is not accepted: re-prompt, run alternates, fix directly,
or use CI/review feedback.

## Parallel strategy

Multiple execs when scopes don't clash: Read-parallel Explorers; Sidecar
Workers (Codex does tests/scripts while Claude owns core); Competing Workers
(separate branches/worktrees → alternate patches); Review swarm (Codex
reviews one diff for correctness/tests/perf/security/UI). Parallel Workers
use separate branches/worktrees; never two Workers on the same files.

## Resume

`codex exec resume --last "<follow-up>"` or `codex exec resume <SESSION_ID>
"<follow-up>"`. Not native continuity — re-provide critical constraints,
branch, scope, objective.

## Mid-turn steering

Raw `codex exec` is non-steerable mid-turn. On drift: kill process → collect
run packet → inspect/revert diff → restart or resume with corrected prompt.

## Runtime/UI loops — Claude-owned (override)

Code review alone is insufficient for UI/runtime bugs, but **Claude does this
inspection directly — it is not delegated to Codex.** Claude owns the
runtime/visual feedback loop using its own tools: any headless inspection
harness the project has, plus the **chrome-devtools / playwright MCP** (live
DOM, screenshots, console/network errors). Codex may still be asked to
*write* regression tests/fixtures/seeded scenarios as bounded packets, but
running and judging runtime behaviour stays with Claude. Human-reported
runtime bugs become regression checks where practical.

## GitHub / Linear / CI loop

Linear issue = intent → Claude decomposes → Codex implements bounded slice →
Claude verifies locally → PR opened (`Closes <ISSUE-ID>`) → CI + GitHub Codex
review → Claude polls feedback → Codex fixes bounded feedback → Claude
verifies/integrates → Linear auto-updated on merge. Failed CI/review is
feedback, not interruption. Acknowledge each finding with a **top-level
`@codex` PR comment highlighting the change**, then **resolve the old
thread** and wait for re-review (which arrives as **new threads**). The gate
is "zero unresolved Codex threads + CI green + mergeStateStatus CLEAN + a
fresh Codex verdict on the current head". See `CLAUDE.md` for mechanics.

## Cost / network policy

Default to ChatGPT/Codex **subscription** usage, not API-key automation. No
OpenAI/Anthropic API, network, paid cloud, or external services unless
explicitly authorized. `.env` keys (if any) are **not** authorized for
routine spend.

## Claude responsibility

Not a passive dispatcher. Decompose intelligently, route, inspect, reject bad
work, build feedback loops, preserve architecture/taste, integrate carefully,
keep improving until excellent.

---

## Operational addendum (verified facts / open items)

- `codex exec` has **no network by default**; on **macOS** the seatbelt
  sandbox ignores `network_access=true` (would need `danger-full-access` /
  unsandboxed). Default stays no-network. Never use
  `--dangerously-bypass-approvals-and-sandbox` implicitly.
- Codex is **subscription-covered, not metered** — not a money gate.
- **Verify before trusting:** the `model` id and the exact `-o result.json`
  ⊕ `--json`→stdout interaction can change across codex-cli versions.
  `scripts/codex-run.sh` is defensive (always keeps `events.jsonl`/
  `stdout.md`; derives the result from `result.json` if present, else the
  last JSON event) and `CODEX_MODEL` is a single env var. Run one smoke task
  and inspect the packet before trusting these.
- `auto_review` (mid-run self-unblock) ≠ the PR merge gate (`@codex review`
  + conversation resolution + a fresh verdict on head). Both coexist.
