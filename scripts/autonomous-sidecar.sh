#!/usr/bin/env bash
# Autonomous-mode sidecar for fraud-forecast.
#
# Purpose: when Claude is running in autonomous mode (user is away,
# pre-authorized the full build loop), this script surveys the live state
# every 20 min and prints (a) the autonomous-mode MANTRA, (b) per in-flight
# item: STATUS (PROGRESSING / WAITING-ON-CODEX / STALLED), (c) explicit
# DECISION per item: NO-ACTION / VERIFY-VIA-TASKLIST / ACT-NOW.
#
# Usage:
#   bash scripts/autonomous-sidecar.sh
#
# Exit code:
#   0 — report printed.
#   non-zero — sidecar itself errored; investigate.
#
# Read-only: never mutates. Claude reads the output + acts.

set -uo pipefail

REPO="${FRAUD_FORECAST_REPO:-/Users/sureshkasipandy/Projects/fraud-forecast}"
WT_ROOT="${FRAUD_FORECAST_WORKTREES:-/Users/sureshkasipandy/Projects/.fraud-forecast-worktrees}"
STALL_THRESHOLD_MIN="${SIDECAR_STALL_MIN:-15}"

cd "$REPO" 2>/dev/null || { echo "✗ sidecar: cannot cd $REPO" >&2; exit 1; }

now_epoch=$(date +%s)

# ───── MANTRA ─────
cat <<'MANTRA'
================================================================================
                        AUTONOMOUS MODE — THE MANTRA
================================================================================

  ACT, DON'T NARRATE. Every stall is a failure to act.

  • Impl silent → check on it (TaskList). Alive → wait. Dead → re-dispatch.
  • Codex 👀'd → wait for verdict. Codex hasn't → re-trigger after 2 min.
  • PR clean → merge. Verdict's the user's confirmation now.
  • Queue has next → dispatch. Phase boundary → start next phase.
  • Genuinely external-blocked & nothing pending → end turn. Next tick rechecks.

  "What happened?" from the user is the failure metric.
  Idling instead of acting wastes the autonomy granted. The build loop is
  yours to drive until Phase 7 closes or you hit an honest blocker.
================================================================================

MANTRA

# ───── PRIMARY ─────
echo "=== PRIMARY ==="
echo "branch: $(git rev-parse --abbrev-ref HEAD)"
echo "HEAD:   $(git log --oneline -1)"
uncommitted_lines=$(git -c color.status=false status --short | wc -l | tr -d ' ')
if [ "$uncommitted_lines" -gt 0 ]; then
  echo "uncommitted in primary working tree ($uncommitted_lines file(s)):"
  git -c color.status=false status --short | sed 's/^/  /'
else
  echo "uncommitted in primary working tree: (none)"
fi
echo
echo "live containers (compose project 'fraud-forecast'):"
docker compose -p fraud-forecast ps --format 'table {{.Name}}\t{{.Status}}' 2>/dev/null \
  | sed 's/^/  /' || echo "  (docker compose ps failed)"
echo

# ───── IN-FLIGHT WORK (PER-PACKET STATUS + DECISION) ─────
echo "=== IN-FLIGHT WORK ==="

# Helper: mtime of newest file under a directory (epoch); empty if no files.
_newest_mtime_in() {
  find "$1" -type f -print0 2>/dev/null | xargs -0 stat -f '%m' 2>/dev/null | sort -nr | head -1
}

# Helper: age in minutes of an epoch timestamp.
_age_min() {
  local then="${1:-}"
  if [ -z "$then" ]; then echo "?"; return; fi
  echo $(( (now_epoch - then) / 60 ))
}

WTS=$(git worktree list --porcelain | awk '/^worktree / {print $2}' | grep "^$WT_ROOT" || true)
PRS_JSON=$(gh pr list --state open --json number,headRefName,headRefOid,title 2>/dev/null || echo '[]')
PR_COUNT=$(echo "$PRS_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')

if [ -z "$WTS" ] && [ "$PR_COUNT" = "0" ]; then
  echo "  (no per-packet worktrees, no open PRs — between packets)"
  echo
  echo "DECISION: ACT-NOW — read VOI-139 'Phase queue', spec + dispatch next packet."
else

  # Build a map: worktree → branch → PR (if any)
  for wt in $WTS; do
    wt_name=$(basename "$wt")
    branch=$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')
    head=$(git -C "$wt" rev-parse --short HEAD 2>/dev/null || echo '?')
    head_full=$(git -C "$wt" rev-parse HEAD 2>/dev/null || echo '?')
    ahead=$(git -C "$wt" rev-list --count origin/main..HEAD 2>/dev/null || echo '?')
    behind=$(git -C "$wt" rev-list --count HEAD..origin/main 2>/dev/null || echo '?')
    dirty=$(git -C "$wt" -c color.status=false status --short | wc -l | tr -d ' ')

    # Pushed?
    pushed_state="(unpushed)"
    if git -C "$wt" ls-remote --exit-code origin "$branch" >/dev/null 2>&1; then
      remote_head=$(git -C "$wt" ls-remote origin "$branch" 2>/dev/null | awk '{print $1}')
      if [ "$remote_head" = "$head_full" ]; then
        pushed_state="(pushed)"
      else
        pushed_state="(pushed @ ${remote_head:0:7} ≠ local $head — local commits unpushed)"
      fi
    fi

    # PR for this branch?
    pr_num=$(echo "$PRS_JSON" | python3 -c "
import json,sys
prs=json.load(sys.stdin)
for p in prs:
    if p['headRefName']=='$branch':
        print(p['number']); break
")

    # Activity timestamps:
    #   newest_local = newest commit OR newest codex-run artifact on the branch
    last_commit_epoch=$(git -C "$wt" log -1 --format=%ct 2>/dev/null || echo 0)
    runs_dir="$wt/.codex-runs"
    newest_run_epoch=0
    last_slice_name=""
    if [ -d "$runs_dir" ]; then
      newest_run_epoch=$(_newest_mtime_in "$runs_dir")
      newest_run_epoch=${newest_run_epoch:-0}
      last_slice_name=$(ls -1t "$runs_dir"/*/result.json 2>/dev/null | head -1 | xargs -I{} dirname {} 2>/dev/null | xargs -I{} basename {} 2>/dev/null || echo "")
    fi
    last_local_epoch=$last_commit_epoch
    [ "$newest_run_epoch" -gt "$last_local_epoch" ] && last_local_epoch=$newest_run_epoch
    local_age=$(_age_min "$last_local_epoch")

    # PR activity (last comment / last review thread message)
    pr_age="?"
    pr_state=""
    pr_threads_open=""
    pr_codex_acked="?"
    if [ -n "$pr_num" ]; then
      last_comment_iso=$(gh api "repos/VoidAxiom/fraud-forecast/issues/$pr_num/comments" \
        --jq '[.[].updated_at] | max // ""' 2>/dev/null)
      if [ -n "$last_comment_iso" ]; then
        last_comment_epoch=$(date -j -f '%Y-%m-%dT%H:%M:%SZ' "$last_comment_iso" '+%s' 2>/dev/null || echo 0)
        pr_age=$(_age_min "$last_comment_epoch")
      fi
      # Latest @codex review request + its 👀 reaction
      latest_rr_json=$(gh api "repos/VoidAxiom/fraud-forecast/issues/$pr_num/comments" \
        --jq '[.[] | select(.body | startswith("@codex review")) | select(.user.login != "chatgpt-codex-connector" and .user.login != "chatgpt-codex-connector[bot]")] | sort_by(.created_at) | last' 2>/dev/null)
      if [ -n "$latest_rr_json" ] && [ "$latest_rr_json" != "null" ]; then
        rr_id=$(echo "$latest_rr_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null)
        eyes_count=$(gh api "repos/VoidAxiom/fraud-forecast/issues/comments/$rr_id/reactions" \
          --jq '[.[] | select(.content=="eyes") | select(.user.login=="chatgpt-codex-connector[bot]" or .user.login=="chatgpt-codex-connector")] | length' 2>/dev/null || echo 0)
        if [ "${eyes_count:-0}" -gt 0 ]; then
          pr_codex_acked="yes"
        else
          pr_codex_acked="no"
        fi
      fi
      # Review-gate verdict
      status_out=$(bash "$REPO/scripts/review-gate.sh" status "$pr_num" 2>&1)
      pr_state=$(echo "$status_out" | grep -E '^GATE:' | head -1)
      pr_threads_open=$(echo "$status_out" | grep -E 'review threads:' | head -1)
    fi

    echo "  PACKET: $wt_name"
    echo "    branch:    $branch"
    echo "    HEAD:      $head ($ahead ahead, $behind behind) $pushed_state"
    echo "    dirty:     $dirty file(s) uncommitted; last local activity $local_age min ago"
    [ -n "$last_slice_name" ] && echo "    last codex run: $last_slice_name"
    if [ -n "$pr_num" ]; then
      echo "    PR:        #$pr_num (last PR comment $pr_age min ago)"
      [ -n "$pr_threads_open" ] && echo "    threads:   ${pr_threads_open#review threads: }"
      [ -n "$pr_state" ] && echo "    $pr_state"
      echo "    latest @codex review 👀-ack: $pr_codex_acked"
    else
      echo "    PR:        (none)"
    fi

    # ───── DECISION HEURISTIC ─────
    decision=""
    decision_reason=""

    # Compute the most-recent activity that matters for liveness
    recent_age_min=$local_age
    if [ -n "$pr_num" ] && [ "$pr_age" != "?" ] && [ "$pr_age" -lt "$recent_age_min" ]; then
      recent_age_min=$pr_age
    fi

    # Classifier:
    if [ -n "$pr_num" ]; then
      if echo "$status_out" | grep -q 'GATE: CLEAN ('; then
        # Head-pinned clean
        decision="ACT-NOW"
        decision_reason="PR has head-pinned CLEAN verdict — Claude does final-head re-gate + squash-merge"
      elif echo "$status_out" | grep -q 'GATE: CLEAN-COMMENT-MANUAL'; then
        decision="ACT-NOW"
        decision_reason="PR has CLEAN-COMMENT-MANUAL — Claude judges head-pin via timeline; merges if clean comment post-dates current head push"
      elif echo "$status_out" | grep -q 'GATE: BLOCKED.*unresolved'; then
        if [ "$pr_codex_acked" = "yes" ] && [ "$recent_age_min" -lt "$STALL_THRESHOLD_MIN" ]; then
          decision="NO-ACTION (in-flight)"
          decision_reason="codex 👀'd the latest request + recent activity (${recent_age_min} min old); impl is iterating — let it work"
        elif [ "$recent_age_min" -lt "$STALL_THRESHOLD_MIN" ]; then
          decision="VERIFY-VIA-TASKLIST"
          decision_reason="recent activity (${recent_age_min} min old) but codex hasn't 👀'd latest request; verify the impl agent is alive via TaskList — if alive, wait (it likely just posted); if dead, re-dispatch with state-aware resume"
        else
          decision="ACT-NOW"
          decision_reason="silent for $recent_age_min min (>= $STALL_THRESHOLD_MIN threshold) AND threads still unresolved — impl agent likely stalled; re-dispatch with state-aware resume reading the unresolved threads"
        fi
      else
        # GATE: BLOCKED with 0 unresolved (waiting for codex first review)
        if [ "$pr_codex_acked" = "yes" ]; then
          if [ "$recent_age_min" -lt "$STALL_THRESHOLD_MIN" ]; then
            decision="NO-ACTION (external-blocked on codex bot)"
            decision_reason="codex 👀'd the request; verdict in flight (typically 3-10 min after ack); recent activity ${recent_age_min} min ago"
          else
            decision="VERIFY-VIA-TASKLIST"
            decision_reason="codex 👀'd $recent_age_min+ min ago but no verdict yet; verify impl agent is alive (it should be in 'wait' phase); if dead, re-dispatch wait"
          fi
        else
          if [ "$recent_age_min" -lt "$STALL_THRESHOLD_MIN" ]; then
            decision="NO-ACTION (in-flight)"
            decision_reason="no 👀 yet but recent activity (${recent_age_min} min); impl agent is in wait helper's grace window"
          else
            decision="ACT-NOW"
            decision_reason="no 👀 + silent for $recent_age_min min — impl agent likely stalled before re-triggering; re-dispatch wait, which will re-trigger"
          fi
        fi
      fi
    else
      # Worktree exists, no PR
      if [ "$ahead" != "0" ] && [ "$ahead" != "?" ]; then
        if [ "$pushed_state" = "(unpushed)" ]; then
          if [ "$recent_age_min" -lt "$STALL_THRESHOLD_MIN" ]; then
            decision="VERIFY-VIA-TASKLIST"
            decision_reason="worktree has $ahead unpushed commit(s); recent activity — likely impl about to notify-done; verify alive; if dead, run pre-PR gate then re-dispatch push/PR"
          else
            decision="ACT-NOW"
            decision_reason="worktree has $ahead unpushed commit(s) but $recent_age_min min silent — impl agent likely stalled; run pre-PR gate directly + re-dispatch impl to push + open PR"
          fi
        else
          decision="ACT-NOW"
          decision_reason="worktree has $ahead commit(s) pushed but no PR opened yet — re-dispatch impl to open PR + trigger @codex review"
        fi
      else
        if [ "$recent_age_min" -lt "$STALL_THRESHOLD_MIN" ]; then
          decision="NO-ACTION (in-flight)"
          decision_reason="worktree has 0 commits + recent codex-run activity (${recent_age_min} min) — impl is in inner loop (codex-exec → /codex:review iterating); let it work"
        else
          decision="VERIFY-VIA-TASKLIST"
          decision_reason="worktree has 0 commits + $recent_age_min min silent — impl agent may be stalled in inner loop; verify alive; if dead, re-dispatch"
        fi
      fi
    fi

    echo "    DECISION:  $decision"
    echo "      └ $decision_reason"
    echo
  done

  # Bare-PRs without a worktree (rare — impl pushed then we tore down worktree but PR not merged)
  echo "$PRS_JSON" | python3 -c "
import json, sys, subprocess
prs = json.load(sys.stdin)
wt_branches = set()
import os
WT_ROOT = '$WT_ROOT'
if os.path.isdir(WT_ROOT):
    for d in os.listdir(WT_ROOT):
        wt_path = os.path.join(WT_ROOT, d)
        try:
            branch = subprocess.check_output(['git','-C',wt_path,'rev-parse','--abbrev-ref','HEAD'], stderr=subprocess.DEVNULL).decode().strip()
            wt_branches.add(branch)
        except Exception:
            pass
for p in prs:
    if p['headRefName'] not in wt_branches:
        print(f\"  ORPHAN-PR: #{p['number']} on {p['headRefName']} (no worktree)\")
        print(f\"    DECISION:  ACT-NOW\")
        print(f\"      └ PR open but no worktree; re-provision worktree if more iteration needed, OR check if it's mergeable + merge.\")
"
fi

echo
echo "=== LINEAR ==="
echo "  command center: https://linear.app/voidaxiom/issue/VOI-139"
echo "  (Claude reads VOI-139 'Live status' + 'Phase queue' for the canonical state)"
echo

echo "=== TICK COMPLETE @ $(date +%H:%M:%S) ==="
echo
echo "If every DECISION above is 'NO-ACTION (in-flight)' or 'NO-ACTION (external-blocked …)',"
echo "Claude ends the turn cleanly — next tick will recheck in ~20 min."
echo "Otherwise: ACT-NOW items are taken immediately; VERIFY-VIA-TASKLIST items are"
echo "checked via TaskList for agent liveness, then acted on per the doctrine."
