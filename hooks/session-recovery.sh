#!/usr/bin/env bash
# SessionStart context-recovery hook. Bash wrapper around
# session-recovery.py whose ONLY job is to guarantee this reflex can never
# silently vanish: if python3 is missing or the helper errors, a hardcoded
# loud-degraded payload is still emitted. `clear` source is intentionally
# NOT recovered (the user deliberately wiped context).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IN="$(cat)"

SRC="$(printf '%s' "$IN" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("source",""))
except Exception: print("")' 2>/dev/null || echo "")"

# Respect a deliberate /clear: emit nothing, do not recover.
[ "$SRC" = "clear" ] && exit 0

OUT="$(printf '%s' "$IN" | python3 "$HERE/session-recovery.py" 2>/dev/null || true)"
if [ -n "$OUT" ]; then
  printf '%s\n' "$OUT"
  exit 0
fi

# Loud fallback — never silent-empty.
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"↻ CONTEXT-RECOVERY (fallback: recovery helper unavailable). Before planning or acting you MUST: read Linear project fraud-forecast description + In-Progress issues + VOI-139 and its latest comment; load a linear-interface skill if present, else MEMORY.md. FIRST output line must be exactly: ↻ RESUME-ACK source=? branch=? started=<N> (N = In-Progress issue count you actually found). Linear unreachable -> DEGRADED MODE on git+MEMORY.md, say so, started=UNAVAILABLE. Absent ack = reflex failed; investigate before continuing."}}'
exit 0
