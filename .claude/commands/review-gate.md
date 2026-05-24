---
description: Drive a PR through the Codex review gate to a clean squash-merge
argument-hint: <PR-NUMBER> (defaults to the PR for the current branch)
---

Follow the "Internal review loop" + "Build loop" review steps in
`CLAUDE.md`. Run autonomously; the only hard stops are metered API spend
and destructive/irreversible actions. The Linear issue auto-transitions
Done via the PR's `Closes VOI-N` link on merge — verify it
did.

PR: **$1** — if empty, resolve it from the current branch
(`gh pr view --json number -q .number`).

This command runs on the **director** side. The implementer subagent owns
the eye-emoji loop (it drives `@codex review` posts, polls
`scripts/review-gate.sh wait`, fixes findings via codex-run, resolves old
threads, posts new `@codex review`, notifies you on REVIEWED-CLEAN /
CLEAN-COMMENT-MANUAL). Your job here is the **merge gate**: final-head
scope check, audit-trail check, head-pinned verdict verification, then
squash-merge + teardown.

Loop until the merge gate is CLEAN:

1. **`bash scripts/review-gate.sh status $1`** — CI checks +
   `mergeStateStatus` + unresolved review-thread count + head-pinned Codex
   verdict check (the script's `GATE: CLEAN` line is the canonical signal).
2. If the impl is still iterating (FINDINGS / WAITING / CLEAN-COMMENT-MANUAL
   without manual confirmation): let the impl finish. It re-engages you on
   REVIEWED-CLEAN or CLEAN-COMMENT-MANUAL+confirmation. Don't reach into
   the eye-emoji loop unless the impl is stuck.
3. **Final-head re-gate** (when the impl notifies REVIEWED-CLEAN, or with
   your explicit confirmation on CLEAN-COMMENT-MANUAL):
   - `bash scripts/impl-precommit-scope.sh --base
     origin/main --worktree <impl worktree>
     --scope-file <packet allowlist>` against the FINAL PR head.
     Codex-response commits can drift; non-negotiable.
   - codex-exec audit-trail check on the FINAL head: every file in
     `git -C <worktree> diff --name-only origin/main...HEAD`
     appears in ≥1 `.codex-runs/<run-id>/git_diff.patch` on the branch.
   - Confirm `GATE: CLEAN` (head-pinned verdict + zero unresolved threads
     + `mergeStateStatus = CLEAN` + green CI).
4. **Merge.** `gh pr merge $1 --squash --delete-branch`. Confirm the Linear
   issue moved to Done.
5. **Teardown.** `git worktree remove <path>`; `git branch -D <branch>`
   if local lingers; `bash scripts/codex-runs-gc.sh --aggressive --days 3`.

**Never merge** with unresolved Codex conversations, failing checks, a
non-`CLEAN` merge state, no head-pinned Codex verdict on the current head,
or `GATE: CLEAN-COMMENT-MANUAL` without operator confirmation that the
clean comment answered a request issued after the current head was
pushed. "Didn't find any major issues" as a comment is advisory only.
Don't merge on a stale state — wait and re-poll.
