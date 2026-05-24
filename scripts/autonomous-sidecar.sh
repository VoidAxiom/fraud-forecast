#!/usr/bin/env bash
# Autonomous-mode sidecar for fraud-forecast.
#
# Purpose: when Claude is running in autonomous mode (user is away,
# pre-authorized the full build loop), this script surveys the live state
# and prints a punch list of actionable items so Claude can't silently
# idle. It is the load-bearing rail behind the autonomous-mode protocol
# in CLAUDE.md "Autonomous mode" section.
#
# Usage (typical):
#   bash scripts/autonomous-sidecar.sh
#
# Fired:
#   - Every 20 minutes via /loop while in autonomous mode (Claude
#     self-pacing).
#   - Manually at any time to get a state snapshot.
#
# Exit code:
#   0 — report printed (regardless of whether actions are pending).
#   non-zero — sidecar itself errored; do NOT use absence of output as
#     "nothing to do" — investigate the script.
#
# Output shape:
#   === PRIMARY ===           # main branch HEAD + live stack containers
#   === WORKTREES ===         # per-packet worktree state (branch, HEAD, dirty?)
#   === OPEN PRS ===          # in-flight PR state (head, codex verdict, threads)
#   === LINEAR QUEUE ===      # VOI-139 active packet + Phase 1 queue
#   === ACTIONS PENDING ===   # numbered list of what Claude should do now
#
# Read-only: no writes, no mutations. Claude reads the output and decides
# actions. The Action block is heuristic — Claude is the source of truth
# for what to actually do next.

set -uo pipefail

REPO="${FRAUD_FORECAST_REPO:-/Users/sureshkasipandy/Projects/fraud-forecast}"
WT_ROOT="${FRAUD_FORECAST_WORKTREES:-/Users/sureshkasipandy/Projects/.fraud-forecast-worktrees}"

cd "$REPO" 2>/dev/null || { echo "✗ sidecar: cannot cd $REPO" >&2; exit 1; }

# ───── PRIMARY ─────
echo "=== PRIMARY ==="
echo "branch: $(git rev-parse --abbrev-ref HEAD)"
echo "HEAD:   $(git log --oneline -1)"
echo "uncommitted in primary working tree:"
git -c color.status=false status --short | sed 's/^/  /'
echo

# Live stack containers (postgres + redis + archiver expected by Phase 1)
echo "live containers (compose project 'fraud-forecast'):"
docker compose -p fraud-forecast ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null \
  | sed 's/^/  /' || echo "  (docker compose ps failed)"
echo

# ───── WORKTREES ─────
echo "=== WORKTREES ==="
WTS=$(git worktree list --porcelain | awk '/^worktree / {print $2}' | grep "^$WT_ROOT" || true)
if [ -z "$WTS" ]; then
  echo "  (no per-packet worktrees — between packets)"
else
  for wt in $WTS; do
    branch=$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')
    head=$(git -C "$wt" rev-parse --short HEAD 2>/dev/null || echo '?')
    ahead=$(git -C "$wt" rev-list --count origin/main..HEAD 2>/dev/null || echo '?')
    behind=$(git -C "$wt" rev-list --count HEAD..origin/main 2>/dev/null || echo '?')
    dirty=$(git -C "$wt" -c color.status=false status --short | wc -l | tr -d ' ')
    pushed="(unpushed)"
    if git -C "$wt" ls-remote --exit-code origin "$branch" >/dev/null 2>&1; then
      remote_head=$(git -C "$wt" ls-remote origin "$branch" 2>/dev/null | awk '{print substr($1,1,7)}')
      if [ "$remote_head" = "$head" ]; then
        pushed="(pushed @$remote_head)"
      else
        pushed="(pushed @$remote_head ≠ local $head)"
      fi
    fi
    echo "  $(basename "$wt")"
    echo "    branch:    $branch"
    echo "    HEAD:      $head ($ahead ahead, $behind behind origin/main) $pushed"
    echo "    dirty:     $dirty file(s) uncommitted"
    # Per-worktree codex-runs activity
    runs_dir="$wt/.codex-runs"
    if [ -d "$runs_dir" ]; then
      slice_count=$(ls -d "$runs_dir"/*-slice-*/ 2>/dev/null | wc -l | tr -d ' ')
      review_count=$(ls -d "$runs_dir"/*-review*/ 2>/dev/null | wc -l | tr -d ' ')
      last_run=$(ls -1t "$runs_dir"/*/result.json 2>/dev/null | head -1)
      last_run_age=""
      if [ -n "$last_run" ]; then
        last_run_age=" (last result.json: $(stat -f '%Sm' -t '%H:%M:%S' "$last_run") = $(basename $(dirname "$last_run")))"
      fi
      echo "    codex:     $slice_count slice(s), $review_count review(s)$last_run_age"
    fi
  done
fi
echo

# ───── OPEN PRS ─────
echo "=== OPEN PRS ==="
PRS=$(gh pr list --state open --json number,headRefName,headRefOid,title --jq '.[] | "\(.number)|\(.headRefName)|\(.headRefOid)|\(.title)"' 2>/dev/null)
if [ -z "$PRS" ]; then
  echo "  (no open PRs)"
else
  while IFS='|' read -r num head_branch head_oid title; do
    short_oid="${head_oid:0:7}"
    echo "  #$num  $head_branch @ $short_oid"
    echo "    title: $title"
    # Cheap status fetch — re-use review-gate.sh status but tail-only
    status_out=$(bash "$REPO/scripts/review-gate.sh" status "$num" 2>&1)
    # Pluck the GATE line + thread count
    gate=$(echo "$status_out" | grep -E '^GATE:' | head -1)
    threads=$(echo "$status_out" | grep -E 'review threads:' | head -1)
    mergestate=$(echo "$status_out" | grep -E '^mergeable=' | head -1)
    echo "    $mergestate"
    echo "    $threads"
    echo "    $gate"
  done <<< "$PRS"
fi
echo

# ───── LINEAR ─────
echo "=== LINEAR ==="
echo "(read VOI-139 \"Live status\" + Phase queue via Linear MCP for the canonical state)"
echo "  command center: https://linear.app/voidaxiom/issue/VOI-139"
echo

# ───── ACTIONS HEURISTIC ─────
echo "=== ACTIONS PENDING (heuristic — Claude judges) ==="
actions=0

# Per-PR: if GATE: BLOCKED with unresolved threads → impl iteration owed
echo "$PRS" | while IFS='|' read -r num head_branch head_oid title; do
  [ -z "$num" ] && continue
  status_out=$(bash "$REPO/scripts/review-gate.sh" status "$num" 2>&1 || true)
  if echo "$status_out" | grep -q 'GATE: BLOCKED'; then
    open_count=$(echo "$status_out" | grep -E 'review threads:' | grep -oE '[0-9]+ UNRESOLVED' | grep -oE '[0-9]+')
    if [ "${open_count:-0}" -gt 0 ]; then
      echo "  • PR #$num has $open_count unresolved codex thread(s) — impl iteration owed (fix → push → resolve → re-trigger)"
      actions=$((actions+1))
    fi
  fi
  if echo "$status_out" | grep -q 'GATE: CLEAN-COMMENT-MANUAL'; then
    echo "  • PR #$num has CLEAN-COMMENT-MANUAL — Claude judges head-pin and merges if timeline post-dates head push"
    actions=$((actions+1))
  fi
  if echo "$status_out" | grep -q 'GATE: CLEAN'; then
    echo "  • PR #$num has head-pinned CLEAN — Claude runs final-head re-gate + squash-merge"
    actions=$((actions+1))
  fi
done

# Worktrees with committed-and-pushed work + no PR → impl owes PR-open
for wt in $WTS; do
  branch=$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null)
  head=$(git -C "$wt" rev-parse --short HEAD 2>/dev/null)
  ahead=$(git -C "$wt" rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
  [ "$ahead" -gt 0 ] || continue
  pr_for_branch=$(gh pr list --head "$branch" --state all --json number --jq '.[0].number' 2>/dev/null)
  if [ -z "$pr_for_branch" ] && git -C "$wt" ls-remote --exit-code origin "$branch" >/dev/null 2>&1; then
    echo "  • Worktree $(basename "$wt") has $ahead commit(s) pushed but no PR — Claude pre-PR gates + dispatch impl to open PR"
    actions=$((actions+1))
  fi
done

# Worktrees with committed work + not pushed → Claude owes pre-PR gate or impl re-dispatch
for wt in $WTS; do
  branch=$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null)
  ahead=$(git -C "$wt" rev-list --count origin/main..HEAD 2>/dev/null || echo 0)
  [ "$ahead" -gt 0 ] || continue
  if ! git -C "$wt" ls-remote --exit-code origin "$branch" >/dev/null 2>&1; then
    echo "  • Worktree $(basename "$wt") has $ahead committed-but-unpushed commit(s) — Claude owes pre-PR gate (impl notify-done likely)"
    actions=$((actions+1))
  fi
done

# No worktrees + no open PRs → between packets; Claude reads VOI-139 queue + dispatches next
if [ -z "$WTS" ] && [ -z "$PRS" ]; then
  echo "  • No worktrees, no open PRs — between packets. Claude reads VOI-139 \"Phase queue\", dispatches next packet."
  actions=$((actions+1))
fi

if [ "$actions" = "0" ]; then
  echo "  (no actions surfaced from heuristic — all in-flight work is either codex-bot-waiting or running impl-subagent-waiting; Claude verifies via task notifications + may schedule next sidecar tick)"
fi

echo
echo "=== END sidecar @ $(date +%H:%M:%S) ==="
