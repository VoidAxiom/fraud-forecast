#!/usr/bin/env bash
# Autonomous-mode sidecar for fraud-forecast.
#
# Prints a COMPACT (<30 line) tick: mantra → per-packet state + decision
# → trailer. Designed so each 20-min tick costs minimal context.
#
# Usage: bash scripts/autonomous-sidecar.sh
# Read-only. Exit 0 always (unless self-error).

set -uo pipefail

REPO="${FRAUD_FORECAST_REPO:-/Users/sureshkasipandy/Projects/fraud-forecast}"
WT_ROOT="${FRAUD_FORECAST_WORKTREES:-/Users/sureshkasipandy/Projects/.fraud-forecast-worktrees}"
STALL_MIN="${SIDECAR_STALL_MIN:-15}"

cd "$REPO" 2>/dev/null || { echo "✗ sidecar: cannot cd $REPO" >&2; exit 1; }
now=$(date +%s)

# ── backup-tick skip ──
# Cron fires 3 ticks at 1-min spacing per cycle so a socket-error-killed
# Claude turn gets retried within 60-120s. If the prior tick completed
# end-to-end (Claude wrote the marker at end-of-turn), this tick is a
# backup we don't need — exit fast with a SKIP signal so Claude's turn
# ends immediately.
MARKER="$REPO/.codex-runs/sidecar-last-success.txt"
if [ -f "$MARKER" ]; then
  marker_epoch=$(cat "$MARKER" 2>/dev/null || echo 0)
  age=$(( now - marker_epoch ))
  if [ "$age" -lt 90 ]; then
    echo "BACKUP-TICK SKIP — prior tick succeeded ${age}s ago (<90s); end turn, no work needed"
    exit 0
  fi
fi

# ── tick header + mantra (8 lines) ──
echo "=== AUTONOMOUS sidecar tick @ $(date +%H:%M:%S) ==="
echo "mantra: ACT, DON'T NARRATE. Idle = failure to act."
echo "  · Impl silent → TaskList check; alive=wait, dead=re-dispatch"
echo "  · Codex 👀'd → wait verdict; not 👀'd & >2min → re-trigger"
echo "  · PR clean → merge; queue has next → dispatch; nothing actionable → end turn"
echo

# ── primary (2 lines) ──
main_head=$(git log --oneline -1 main 2>/dev/null | cut -c1-72)
containers=$(docker compose -p fraud-forecast ps --format '{{.Name}}={{.State}}' 2>/dev/null | tr '\n' ' ')
echo "primary: $main_head"
echo "  live: ${containers:-none}"
echo

# ── packets (per-worktree + per-PR, condensed) ──
WTS=$(git worktree list --porcelain | awk '/^worktree / {print $2}' | grep "^$WT_ROOT" || true)
PRS_JSON=$(gh pr list --state open --json number,headRefName,headRefOid,title 2>/dev/null || echo '[]')
PR_COUNT=$(echo "$PRS_JSON" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')

actions_now=0
verify_owed=0
in_flight=0

if [ -z "$WTS" ] && [ "$PR_COUNT" = "0" ]; then
  echo "packets: (none in flight — between packets)"
  echo "  → ACT-NOW: read VOI-139 Phase queue, dispatch next packet"
  actions_now=1
else
  echo "packets:"
  for wt in $WTS; do
    wt_name=$(basename "$wt")
    branch=$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null)
    head_full=$(git -C "$wt" rev-parse HEAD 2>/dev/null)
    head_short=${head_full:0:7}
    ahead=$(git -C "$wt" rev-list --count origin/main..HEAD 2>/dev/null)

    # pushed?
    pushed="unpushed"
    if git -C "$wt" ls-remote --exit-code origin "$branch" >/dev/null 2>&1; then
      remote_head=$(git -C "$wt" ls-remote origin "$branch" 2>/dev/null | awk '{print $1}')
      [ "$remote_head" = "$head_full" ] && pushed="pushed" || pushed="local-ahead"
    fi

    # PR for branch?
    pr_num=$(echo "$PRS_JSON" | python3 -c "
import json,sys
for p in json.load(sys.stdin):
    if p['headRefName']=='$branch':
        print(p['number']); break
")

    # liveness: max(last commit, newest .codex-runs artifact)
    last_commit=$(git -C "$wt" log -1 --format=%ct 2>/dev/null || echo 0)
    newest_run=0
    if [ -d "$wt/.codex-runs" ]; then
      newest_run=$(find "$wt/.codex-runs" -type f -print0 2>/dev/null | xargs -0 stat -f '%m' 2>/dev/null | sort -nr | head -1)
      newest_run=${newest_run:-0}
    fi
    last_local=$last_commit
    [ "$newest_run" -gt "$last_local" ] && last_local=$newest_run
    local_age=$(( (now - last_local) / 60 ))

    # PR-side data
    pr_age="?"
    gate="?"
    threads_open=0
    acked="?"
    if [ -n "$pr_num" ]; then
      last_comment_iso=$(gh api "repos/VoidAxiom/fraud-forecast/issues/$pr_num/comments" \
        --jq '[.[].updated_at] | max // ""' 2>/dev/null)
      if [ -n "$last_comment_iso" ]; then
        # NB: -ju forces UTC; the bare -j -f flag chain parses as LOCAL on macOS,
        # which yielded bogus negative-idle ages once the local clock crossed a TZ boundary.
        last_comment_epoch=$(date -ju -f '%Y-%m-%dT%H:%M:%SZ' "$last_comment_iso" '+%s' 2>/dev/null || echo 0)
        pr_age=$(( (now - last_comment_epoch) / 60 ))
      fi
      status_out=$(bash "$REPO/scripts/review-gate.sh" status "$pr_num" 2>&1)
      gate=$(echo "$status_out" | grep -oE 'GATE: [A-Z-]+( \(.*\))?' | head -1 || echo "?")
      threads_open=$(echo "$status_out" | grep -oE '[0-9]+ UNRESOLVED' | grep -oE '^[0-9]+' || echo 0)
      # 👀-ack on latest non-codex @codex review request
      latest_rr=$(gh api "repos/VoidAxiom/fraud-forecast/issues/$pr_num/comments" \
        --jq '[.[] | select(.body | startswith("@codex review")) | select(.user.login != "chatgpt-codex-connector" and .user.login != "chatgpt-codex-connector[bot]")] | sort_by(.created_at) | last' 2>/dev/null)
      if [ -n "$latest_rr" ] && [ "$latest_rr" != "null" ]; then
        rr_id=$(echo "$latest_rr" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null)
        if [ -n "$rr_id" ]; then
          eyes=$(gh api "repos/VoidAxiom/fraud-forecast/issues/comments/$rr_id/reactions" \
            --jq '[.[] | select(.content=="eyes") | select(.user.login=="chatgpt-codex-connector[bot]" or .user.login=="chatgpt-codex-connector")] | length' 2>/dev/null)
          [ "${eyes:-0}" -gt 0 ] && acked="yes" || acked="no"
        fi
      fi
    fi

    # most recent activity (local or PR)
    recent=$local_age
    [ "$pr_age" != "?" ] && [ "$pr_age" -lt "$recent" ] && recent=$pr_age

    # decision
    if [ -n "$pr_num" ]; then
      if echo "$gate" | grep -q 'CLEAN ('; then
        decision="ACT-NOW: head-pinned CLEAN — merge"
        actions_now=$((actions_now+1))
      elif echo "$gate" | grep -q 'CLEAN-COMMENT-MANUAL'; then
        decision="ACT-NOW: CLEAN-COMMENT-MANUAL — judge timeline + merge"
        actions_now=$((actions_now+1))
      elif [ "$threads_open" -gt 0 ]; then
        if [ "$acked" = "yes" ] && [ "$recent" -lt "$STALL_MIN" ]; then
          decision="NO-ACTION: codex 👀'd, impl iterating (${recent}min idle)"
          in_flight=$((in_flight+1))
        elif [ "$recent" -lt "$STALL_MIN" ]; then
          decision="VERIFY: ${recent}min idle, no 👀 — TaskList alive? else re-dispatch"
          verify_owed=$((verify_owed+1))
        else
          decision="ACT-NOW: ${recent}min silent + $threads_open unresolved — re-dispatch impl"
          actions_now=$((actions_now+1))
        fi
      else
        # threads=0, waiting on first review
        if [ "$acked" = "yes" ] && [ "$recent" -lt "$STALL_MIN" ]; then
          decision="NO-ACTION: codex 👀'd, verdict in flight (${recent}min)"
          in_flight=$((in_flight+1))
        elif [ "$recent" -lt "$STALL_MIN" ]; then
          decision="NO-ACTION: ${recent}min idle, in wait helper grace window"
          in_flight=$((in_flight+1))
        else
          decision="ACT-NOW: ${recent}min silent + no 👀 — re-dispatch wait"
          actions_now=$((actions_now+1))
        fi
      fi
    else
      # no PR
      if [ "$ahead" -gt 0 ] && [ "$pushed" = "unpushed" ]; then
        if [ "$recent" -lt "$STALL_MIN" ]; then
          decision="VERIFY: $ahead unpushed commits, ${recent}min idle — impl about to notify? else pre-PR + re-dispatch"
          verify_owed=$((verify_owed+1))
        else
          decision="ACT-NOW: $ahead unpushed + ${recent}min silent — pre-PR gate + re-dispatch push"
          actions_now=$((actions_now+1))
        fi
      elif [ "$ahead" -gt 0 ]; then
        decision="ACT-NOW: $ahead commits pushed, no PR — re-dispatch impl to open PR"
        actions_now=$((actions_now+1))
      else
        if [ "$recent" -lt "$STALL_MIN" ]; then
          decision="NO-ACTION: 0 commits, ${recent}min activity — impl in inner loop"
          in_flight=$((in_flight+1))
        else
          decision="VERIFY: 0 commits + ${recent}min silent — impl stalled in inner loop?"
          verify_owed=$((verify_owed+1))
        fi
      fi
    fi

    # 1-line packet summary
    if [ -n "$pr_num" ]; then
      pr_tag="PR#$pr_num $gate threads:$threads_open 👀:$acked"
    else
      pr_tag="no PR"
    fi
    echo "  $wt_name [$head_short $ahead-ahead $pushed] $pr_tag"
    echo "    → $decision"
  done
fi

echo
echo "tick summary: ${actions_now} ACT-NOW, ${verify_owed} VERIFY, ${in_flight} NO-ACTION (in-flight)"
if [ "$actions_now" = "0" ] && [ "$verify_owed" = "0" ]; then
  echo "→ end turn cleanly; next tick in ~20min"
fi
echo
echo "→ Claude: write the success marker as your LAST action so backup ticks skip:"
echo "    echo $(date +%s) > $MARKER"
