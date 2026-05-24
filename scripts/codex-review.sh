#!/usr/bin/env bash
# Internal stateless adversarial reviewer — a pre-PR backstop that emulates
# the GitHub Codex review bot but runs BEFORE the PR is opened, so issues are
# caught on our side first (the GH bot becomes a true backstop, not the
# primary net). Read-only; never edits; uses a stronger model at high
# reasoning, fresh context each run (stateless/adversarial by construction).
#
#   scripts/codex-review.sh <run-id> [base-ref]
#
# Reviews `git diff <base>...HEAD` (base default: origin/main).
# Writes the review to .codex-runs/<run-id>/review.md and prints it. Claude
# reads it, fixes P1/P2, then opens the PR.
#
# Env: CODEX_REVIEW_MODEL (default gpt-5.5),
#      CODEX_REVIEW_EFFORT (default xhigh).
set -uo pipefail

RUN_ID="${1:-}"
BASE_REF="${2:-origin/main}"
MODEL="${CODEX_REVIEW_MODEL:-gpt-5.5}"
EFFORT="${CODEX_REVIEW_EFFORT:-xhigh}"

[ -n "$RUN_ID" ] || { echo "usage: codex-review.sh <run-id> [base-ref]" >&2; exit 2; }
# Reject path traversal / separators: RUN_ID is interpolated into a path.
case "$RUN_ID" in
  *[!A-Za-z0-9._-]* | -* | *..* )
    echo "invalid run-id '$RUN_ID' — use only [A-Za-z0-9._-], no '..'" >&2
    exit 2 ;;
esac
command -v codex >/dev/null 2>&1 || { echo "codex CLI not found" >&2; exit 127; }

RUN=".codex-runs/$RUN_ID"
mkdir -p "$RUN"

MERGE_BASE="$(git merge-base "$BASE_REF" HEAD 2>/dev/null || echo "$BASE_REF")"
DIFF="$(git diff "$MERGE_BASE"...HEAD)"
STAT="$(git diff --stat "$MERGE_BASE"...HEAD)"
if [ -z "$DIFF" ]; then echo "no diff vs $BASE_REF — nothing to review" >&2; exit 3; fi

# The prompt is fed via stdin (no ARG_MAX), so send the COMPLETE diff — a
# silently truncated diff could miss defects after the cutoff while still
# reporting "NO BLOCKING ISSUES", which is unacceptable for a required gate.
# Only guard against diffs so large they'd blow the model context: fail
# LOUDLY (never silently partial-review) and tell the operator to split.
DIFF_BYTES=$(printf '%s' "$DIFF" | wc -c | tr -d ' ')
MAX_DIFF_BYTES="${CODEX_REVIEW_MAX_BYTES:-1200000}"
if [ "$DIFF_BYTES" -gt "$MAX_DIFF_BYTES" ]; then
  echo "diff is ${DIFF_BYTES}B > ${MAX_DIFF_BYTES}B — too large for one review." >&2
  echo "Split it: review subsets by path, e.g. limit the branch's scope or" >&2
  echo "run focused reviews per directory; do NOT trust a partial pass." >&2
  exit 4
fi

# Reviewer prompt — mirrors the open-source OpenAI Codex review prompt:
# flag-criteria, prefer zero findings over uncertain ones, state the
# conditions for the bug, match codebase rigor, [P0]-[P3], correctness
# verdict — plus this project's invariants.
PROMPT="You are a STATELESS, ADVERSARIAL senior reviewer for fraud-forecast
(TODO — fill in once the stack is decided). You have NO prior context and no stake. Review ONLY
the diff below (changes vs ${BASE_REF}); judge only defects INTRODUCED by
this change.

Flag an issue ONLY if ALL hold: it meaningfully impacts accuracy,
performance, security, or maintainability; it is discrete and actionable;
it matches the rigor level of this codebase; it was introduced in this
change; the author would very likely fix it if aware; it does not rely on
unstated assumptions; it has provable downstream effects; and it is a
genuine bug, not an intentional choice. Prefer reporting NOTHING over an
uncertain or speculative finding (false positives are worse than silence —
this is a required gate). Do not give style/preference nits.

Pay special attention to this project's invariants: every number a change
shows or asserts must be REALLY computed & unit-tested (never
faked/hardcoded/mocked); strict typing (no type lies, no unchecked index
access asserting a false type); domain/factual accuracy (math, numbers,
claims must be correct); determinism (no RNG/clock in render/layout — seeded);
responsiveness & no overflow (mobile+desktop); accessibility (real controls,
names, keyboard, reduced-motion); cross-layer contracts (single source of
truth, no self-referential config, layout-math == rendered size); test
adequacy; security (path/usage of untrusted input).

Each finding, most severe first, EXACTLY:
[P0|P1|P2|P3] <file:area> — <the concrete defect> — <the conditions under
which it manifests> — <why it matters> — <specific minimal fix>
Severity: P0 blocking/correctness/security/data-loss, P1 serious, P2
should-fix, P3 minor. Be brief (≤1 short paragraph each, ≤3-line snippets),
matter-of-fact, no overstatement, immediately comprehensible.

End with one line: VERDICT: correct  (no blocking issues; existing code &
tests still work) — or — VERDICT: incorrect  (>=1 P0/P1, or it breaks
existing code/tests).
If there are genuinely no qualifying findings, output exactly:
NO BLOCKING ISSUES
VERDICT: correct
Do NOT praise, summarize, or restate the diff.

=== git diff --stat ===
${STAT}

=== git diff ${MERGE_BASE}...HEAD (COMPLETE — review every hunk) ===
${DIFF}"

echo "internal review: run=$RUN_ID model=$MODEL effort=$EFFORT base=$BASE_REF" >&2
# Feed the prompt via stdin, NOT argv: a single argv string fails around
# ~131 KiB on Linux ("Argument list too long"), and substantive diffs blow
# past that. `codex exec` with no positional prompt reads it from stdin.
printf '%s' "$PROMPT" > "$RUN/review-prompt.txt"
codex exec --json --sandbox read-only \
  -c model="$MODEL" \
  -c model_reasoning_effort="$EFFORT" \
  < "$RUN/review-prompt.txt" \
  > "$RUN/review-events.jsonl" 2> "$RUN/review-stderr.log"
CODE=$?
echo "$CODE" > "$RUN/review-exit.txt"

# Extract the final assistant text from the JSON event stream (defensive:
# prefer an explicit agent/assistant message, else the last non-empty line).
python3 - "$RUN/review-events.jsonl" > "$RUN/review.md" 2>/dev/null <<'PY'
import json,sys
out=""
try:
    for line in open(sys.argv[1]):
        line=line.strip()
        if not line or line[0]!="{": continue
        try: ev=json.loads(line)
        except: continue
        for k in ("message","text","content","delta"):
            v=ev.get(k)
            if isinstance(v,str) and v.strip(): out=v
        item=ev.get("item") or {}
        if isinstance(item,dict):
            t=item.get("text") or item.get("message")
            if isinstance(t,str) and t.strip(): out=t
except Exception as e:
    out=f"(parse error: {e})"
print(out.strip() or "(no review text captured — inspect review-events.jsonl)")
PY

echo "=== internal adversarial review ($RUN_ID) ==="
cat "$RUN/review.md"
echo
echo "(exit=$CODE; raw: $RUN/review-events.jsonl)" >&2

# A required gate must NOT appear to pass when no real review was produced
# (codex exit is ~0 for a completed run even if the extractor caught nothing,
# or codex itself errored). Only succeed when review.md is a genuine result:
# the exact sentinel, or at least one [P0..P3] finding. Otherwise fail loudly.
if [ "$CODE" -ne 0 ]; then
  echo "ERROR: codex exec failed (exit $CODE) — no trustworthy review." >&2
  exit "$CODE"
fi
if grep -qx 'NO BLOCKING ISSUES' "$RUN/review.md" \
   || grep -Eq '\[P[0-3]\]' "$RUN/review.md"; then
  exit 0
fi
echo "ERROR: no valid review captured (empty / placeholder / unexpected" >&2
echo "format). Inspect $RUN/review-events.jsonl; do NOT treat as a pass." >&2
exit 5
