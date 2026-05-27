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

# ── ALWAYS fetch latest main from origin (every tick, no exception) ──
# Without this, the sidecar reads a stale local origin/main and misses
# merges Claude made in another worktree or that landed via squash-
# merge from a PR. That makes "newly merged" detection below useless.
git fetch origin main --quiet 2>/dev/null || true

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
main_head=$(git log --oneline -1 origin/main 2>/dev/null | cut -c1-72)
containers=$(docker compose -p fraud-forecast ps --format '{{.Name}}={{.State}}' 2>/dev/null | tr '\n' ' ')
echo "primary: $main_head"
echo "  live: ${containers:-none}"

# ── newly-merged PRs since last tick (cross-tick state) ──
# Compares current origin/main HEAD to the SHA we saw last tick. Any new
# commits on main are squash-merges from PRs (per repo policy). For each,
# pull the PR number from the conventional "(#N)" subject suffix and
# emit an ACT-NOW so Claude dispatches impls on any newly-unblocked work.
LAST_MAIN_MARKER="$REPO/.codex-runs/sidecar-last-known-main.txt"
cur_main_sha=$(git rev-parse origin/main 2>/dev/null || echo "")
prev_main_sha=""
[ -f "$LAST_MAIN_MARKER" ] && prev_main_sha=$(cat "$LAST_MAIN_MARKER" 2>/dev/null | head -c 40)
merged_voi_list=""
if [ -n "$prev_main_sha" ] && [ "$prev_main_sha" != "$cur_main_sha" ]; then
  # Walk new commits oldest→newest; extract VOI-N from "Closes VOI-N" or
  # the conventional "(#PR)" squash-merge suffix.
  new_merges=$(git log --pretty='%H %s' "$prev_main_sha..origin/main" 2>/dev/null | head -50)
  if [ -n "$new_merges" ]; then
    echo
    echo "merged since last tick:"
    while IFS= read -r line; do
      sha=${line:0:7}
      subject=${line:41}
      pr_num=$(echo "$subject" | grep -oE '\(#[0-9]+\)' | tr -d '(#)' | head -1)
      voi_num=$(echo "$subject" | grep -oE 'VOI-[0-9]+' | head -1)
      echo "  + $sha PR#${pr_num:-?} ${voi_num:-?}: $(echo "$subject" | cut -c1-60)"
      [ -n "$voi_num" ] && merged_voi_list="$merged_voi_list $voi_num"
    done <<< "$new_merges"
  fi
fi
# NOTE: the sidecar does NOT write $LAST_MAIN_MARKER itself. Claude
# advances it at end-of-turn (alongside the success marker) ONLY AFTER
# the dispatched "newly merged" work has actually been kicked off. If the
# sidecar wrote it here, a Claude turn that died between this survey and
# the dispatch would suppress the alert on the next tick (next prev_main
# == cur_main → no print). Tying the marker advance to Claude's success
# means the next tick re-announces the merge if Claude died mid-turn.
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
  # Track branches covered by worktree iteration so the second pass can
  # process open PRs whose branch has no worktree (director-owned doc/CI
  # PRs from primary, cherry-pick branches, etc).
  covered_branches=""
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
    covered_branches="$covered_branches|$branch|"
  done

  # ── second pass: open PRs not backed by a worktree (director-owned
  # branches from primary checkout, cherry-pick branches, etc).
  # Without this, PRs that don't map to a worktree are invisible to the
  # sidecar and rot unattended — exact failure mode that left PR #40's
  # codex P2s sitting for 30+ min on 2026-05-27.
  while IFS=$'\t' read -r pr_num branch head_sha; do
    case "$covered_branches" in *"|$branch|"*) continue ;; esac
    head_short=${head_sha:0:7}

    # Ownership: who's supposed to be driving this PR?
    # Claude (director) owns PRs whose ENTIRE changed-file set is in
    # Claude's exclusive territory: .claude/**, .codex/**, hooks/**,
    # docs/**, scripts/**, **/*.md, root .gitignore. Otherwise the PR
    # touches production code → impl-owned.
    owner="impl"
    changed_files=$(gh pr view "$pr_num" --json files --jq '.files[].path' 2>/dev/null)
    if [ -n "$changed_files" ]; then
      owner="claude"
      while IFS= read -r f; do
        case "$f" in
          .claude/*|.codex/*|hooks/*|docs/*|scripts/*|*.md|.gitignore) ;;
          *) owner="impl"; break ;;
        esac
      done <<< "$changed_files"
    fi

    # Impl-presence: any IMPL worktree (under $WT_ROOT) for this branch?
    # Excludes the primary checkout — git worktree list reports primary
    # too, but primary on a branch ≠ impl dispatched on that branch. The
    # convention is impl worktrees live at $WT_ROOT/<name>/. If owner=impl
    # AND impl_present=no, impl was never dispatched OR died and the
    # worktree was torn down — director must spawn a fresh impl.
    impl_present="no"
    if [ -d "$WT_ROOT" ]; then
      while read -r wt_path; do
        case "$wt_path" in "$WT_ROOT"/*)
          wt_branch=$(git -C "$wt_path" rev-parse --abbrev-ref HEAD 2>/dev/null)
          [ "$wt_branch" = "$branch" ] && impl_present="yes" && break
          ;;
        esac
      done < <(git worktree list --porcelain | awk '/^worktree / {print $2}')
    fi

    # PR-side state (head SHA, gate, threads, 👀, codex-clean-on-head)
    status_out=$(bash "$REPO/scripts/review-gate.sh" status "$pr_num" 2>&1)
    gate=$(echo "$status_out" | grep -oE 'GATE: [A-Z-]+( \(.*\))?' | head -1 || echo "?")
    threads_open=$(echo "$status_out" | grep -oE '[0-9]+ UNRESOLVED' | grep -oE '^[0-9]+' || echo 0)

    # Latest @codex review request from a non-bot (timestamp + 👀)
    latest_rr=$(gh api "repos/VoidAxiom/fraud-forecast/issues/$pr_num/comments" \
      --jq '[.[] | select(.body | startswith("@codex review")) | select(.user.login != "chatgpt-codex-connector" and .user.login != "chatgpt-codex-connector[bot]")] | sort_by(.created_at) | last' 2>/dev/null)
    acked="?"; rr_age="-"
    if [ -n "$latest_rr" ] && [ "$latest_rr" != "null" ]; then
      rr_id=$(echo "$latest_rr" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null)
      rr_at=$(echo "$latest_rr" | python3 -c 'import json,sys; print(json.load(sys.stdin)["created_at"])' 2>/dev/null)
      if [ -n "$rr_id" ]; then
        eyes=$(gh api "repos/VoidAxiom/fraud-forecast/issues/comments/$rr_id/reactions" \
          --jq '[.[] | select(.content=="eyes") | select(.user.login=="chatgpt-codex-connector[bot]" or .user.login=="chatgpt-codex-connector")] | length' 2>/dev/null)
        [ "${eyes:-0}" -gt 0 ] && acked="yes" || acked="no"
      fi
      if [ -n "$rr_at" ]; then
        rr_epoch=$(date -ju -f '%Y-%m-%dT%H:%M:%SZ' "$rr_at" '+%s' 2>/dev/null || echo 0)
        rr_age=$(( (now - rr_epoch) / 60 ))
      fi
    fi

    # Decision: owner directs who does the work.
    # - Claude-owned (doc/CI): Claude drives everything — fix findings via
    #   Edit/Bash, post @codex review, merge.
    # - Impl-owned: if impl_present=yes, impl is alive (or was) → check
    #   in via TaskList, re-dispatch if dead. If impl_present=no, the PR
    #   was opened without a worktree (e.g. you took over an impl-scope
    #   change directly, which violates the cardinal rule) — spawn a
    #   fresh impl for the fix and stop hand-editing impl-scope files.
    if echo "$gate" | grep -q 'CLEAN ('; then
      decision="ACT-NOW [$owner]: head-pinned CLEAN — merge (final-head re-gate first)"
      actions_now=$((actions_now+1))
    elif echo "$gate" | grep -q 'CLEAN-COMMENT-MANUAL'; then
      decision="ACT-NOW [$owner]: CLEAN-COMMENT-MANUAL — judge timeline (push < @codex review < clean comment) + merge"
      actions_now=$((actions_now+1))
    elif [ "$threads_open" -gt 0 ]; then
      if [ "$owner" = "claude" ]; then
        decision="ACT-NOW [claude]: $threads_open unresolved threads on doc/CI PR — read findings, fix via Edit/Bash, commit/push, resolve, re-trigger"
      elif [ "$impl_present" = "yes" ]; then
        decision="ACT-NOW [impl]: $threads_open unresolved threads — check impl via TaskList; alive=let them iterate, dead=re-dispatch with state-aware resume"
      else
        decision="ACT-NOW [impl ORPHANED]: $threads_open unresolved + impl-scope files but NO worktree — spawn a fresh impl for the fix; do NOT hand-edit impl-scope yourself"
      fi
      actions_now=$((actions_now+1))
    elif [ "$acked" = "yes" ]; then
      decision="NO-ACTION [$owner]: codex 👀'd ${rr_age}min ago — verdict in flight"
      in_flight=$((in_flight+1))
    elif [ "$rr_age" = "-" ]; then
      decision="ACT-NOW [$owner]: PR open + no @codex review yet — post bare @codex review"
      actions_now=$((actions_now+1))
    elif [ "$rr_age" -gt 5 ] 2>/dev/null; then
      decision="ACT-NOW [$owner]: @codex review posted ${rr_age}min ago + no 👀 — re-trigger (codex may have missed)"
      actions_now=$((actions_now+1))
    else
      decision="NO-ACTION [$owner]: @codex review posted ${rr_age}min ago, grace window for 👀 (≤5min)"
      in_flight=$((in_flight+1))
    fi

    echo "  orphan-PR $branch [$head_short] PR#$pr_num $gate threads:$threads_open 👀:$acked rr:${rr_age}min owner=$owner impl_present=$impl_present"
    echo "    → $decision"
  done < <(echo "$PRS_JSON" | python3 -c '
import json, sys
for p in json.load(sys.stdin):
    n, b, o = p["number"], p["headRefName"], p["headRefOid"]
    print("\t".join([str(n), b, o]))
' 2>/dev/null)
fi

echo
# Bump ACT-NOW if anything merged this tick — director should enumerate
# downstream-unblocked issues and spin up impls.
if [ -n "$merged_voi_list" ]; then
  actions_now=$((actions_now+1))
  echo "→ ACT-NOW (newly merged):${merged_voi_list}"
  echo "  - Live-verify each on primary per packet acceptance (Outcome over output)."
  echo "  - Read VOI-139 Phase queue + spec/PHASE_*.md DAG; any issue whose deps just"
  echo "    closed is now unblocked — dispatch impl(s) immediately, in parallel where"
  echo "    file surfaces are disjoint."
  echo
fi
echo "tick summary: ${actions_now} ACT-NOW, ${verify_owed} VERIFY, ${in_flight} NO-ACTION (in-flight)"
if [ "$actions_now" = "0" ] && [ "$verify_owed" = "0" ]; then
  echo "→ end turn cleanly; next tick in ~20min"
fi
echo
echo "→ Claude: write BOTH markers as your LAST actions so backup ticks skip"
echo "  AND the merged-PR alert doesn't get falsely suppressed if you died mid-turn."
echo "  Prefix with mkdir -p in case .codex-runs/ doesn't exist yet (fresh checkout):"
echo "    mkdir -p $(dirname "$MARKER") && echo $(date +%s) > $MARKER"
if [ -n "$merged_voi_list" ] || [ -n "${cur_main_sha:-}" ]; then
  echo "    mkdir -p $(dirname "$LAST_MAIN_MARKER") && echo $cur_main_sha > $LAST_MAIN_MARKER  # advance ONLY after dispatching the merged-PR work above"
fi
