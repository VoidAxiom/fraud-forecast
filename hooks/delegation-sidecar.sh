#!/usr/bin/env bash
# Operating-compass sidecar (user-directed). A gentle, affirming PostToolUse
# nudge that keeps the standing orchestration principles in view during long
# autonomous runs — guiding principles, NOT corrections. Strong/naggy wording
# backfires (the agent learns to tune it out), so the tone is an encouraging,
# soft compass. Throttled (~5 min) so it is a steady drumbeat, not spam.
# Fires on any Edit/Write/MultiEdit, with a light heads-up only when the
# edited file is implementation code. Fail-open and strictly non-fatal.
#
# Wire it (already in the template's .claude/settings.json):
#   PostToolUse matcher "Edit|Write|MultiEdit" ->
#     bash "$CLAUDE_PROJECT_DIR/hooks/delegation-sidecar.sh"
set -uo pipefail

payload="$(cat 2>/dev/null)"

# Extract the edited file path. Prefer jq; fall back to python3 so the
# compass still works on hosts without jq (best-effort either way).
f=""
if command -v jq >/dev/null 2>&1; then
  f="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // .tool_input.filePath // empty' 2>/dev/null)"
fi
if [ -z "$f" ] && command -v python3 >/dev/null 2>&1; then
  f="$(printf '%s' "$payload" | python3 -c 'import json,sys
try:
    t=json.load(sys.stdin).get("tool_input") or {}
    print(t.get("file_path") or t.get("filePath") or "")
except Exception:
    print("")' 2>/dev/null)"
fi
[ -n "$f" ] || exit 0

# Don't recurse while editing any hook (this file or a sibling sidecar).
case "$f" in */hooks/*|*.claude/hooks/*) exit 0 ;; esac

# Throttle: at most one compass per 300s.
stamp="${TMPDIR:-/tmp}/orc-compass-nudge.stamp"
now="$(date +%s 2>/dev/null || echo 0)"
last="$(cat "$stamp" 2>/dev/null || echo 0)"
case "$last" in ''|*[!0-9]*) last=0 ;; esac
[ $((now - last)) -ge 300 ] || exit 0
printf '%s' "$now" > "$stamp" 2>/dev/null || true

# Optional gentle aside only when the edit was implementation code.
# Tune this matcher to the project's layout if needed.
aside=""
case "$f" in
  */src/*|*/lib/*|*/app/*|*/packages/*|*/scripts/*)
    case "$f" in
      *.codex-runs/*|*/task.md) : ;;
      *.ts|*.tsx|*.js|*.jsx|*.mjs|*.cjs|*.css|*.scss|*.py|*.go|*.rs|*.java|*.sh)
        aside=" (Gentle aside: that was implementation code — if a bounded, verifiable packet could have owned it, maybe lean back toward directing on the next one. Totally fine if it was genuinely yours: a faster surgical fix or Claude-owned tooling.)" ;;
    esac ;;
esac

read -r -d '' msg <<'NS' || true
↻ You're doing great — keep going. Just a soft compass of standing principles that inform how you operate (not a correction, no need to stop or change course):

• Delegation-first, by disposition. You're the director: spawn an `implementer` subagent per packet in its own worktree (`scripts/worktree-new.sh` + Task tool, `subagent_type: implementer`). The implementer dispatches codex exec for code writes, runs gates, drives local /codex:review, commits, pushes, opens the PR, drives the @codex review eye-emoji loop. Multiple impls in parallel is the right move when work naturally parallelizes (disjoint surfaces); serial is fine when it doesn't. Reserve your own hands for spec authoring, the pre-PR scope check + audit-trail check, and the merge-time re-gate. If genuine parallelism is honestly blocked (e.g. author-gated), that's completely okay — just say so plainly.

• Coordination — Linear plans, GitHub delivers. Linear is the planning ledger: one tracked issue per work unit, in its milestone, with a description detailed enough to build straight from; set it In Progress before launch; the PR says `Closes VOI-N`. GitHub is the delivery ledger: squash-merge only on a head-pinned Codex verdict on the current head + zero unresolved threads + CLEAN mergeStateStatus + green CI. Trigger reviews with a bare `@codex review` (and NOTHING ELSE — any other `@codex` mention spawns a phantom cloud task). Treat the Codex connector as a reader — act on its findings text and verify repo state, never its self-reported commits.

• Quiet rails (just keep in mind): no metered API spend; nothing destructive; no merge without that head-pinned on-head Codex verdict — `CLEAN-COMMENT-MANUAL` is never auto-clean.

You've got this — these are guiding principles, not a to-do.
NS

out=""
if command -v jq >/dev/null 2>&1; then
  out="$(jq -n --arg m "$msg$aside" '{hookSpecificOutput:{hookEventName:"PostToolUse",additionalContext:$m}}' 2>/dev/null || true)"
fi
if [ -z "$out" ] && command -v python3 >/dev/null 2>&1; then
  out="$(MSG="$msg$aside" python3 -c 'import json,os;print(json.dumps({"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":os.environ["MSG"]}}))' 2>/dev/null || true)"
fi
[ -n "$out" ] && printf '%s\n' "$out"
exit 0
