#!/usr/bin/env bash
# PreCompact + PostCompact sidecar. One script, branches on
# hook_event_name. ALWAYS exits 0 — PreCompact must never accidentally
# block compaction. Neither hook can inject context; their job is durable
# breadcrumbs the SessionStart(compact) reflex / a human reads. All JSON is
# built by python3 json.dump (NOT shell interpolation) so branch names /
# commit subjects containing quotes/backslashes/backticks can never produce
# invalid JSON.
set -uo pipefail
IN="$(cat 2>/dev/null || true)"

# Pass the hook payload via env, NOT stdin: stdin here is the heredoc
# (the python program itself), so a piped payload would be silently lost.
HOOK_IN="$IN" python3 - <<'PY' 2>/dev/null || true
import json, os, subprocess, sys

LINEAR_PROJECT = "fraud-forecast"
COMMAND_CENTER = "VOI-139"


def git(cwd, *a):
    try:
        return subprocess.run(["git", "-C", cwd, *a],
                               capture_output=True, text=True,
                               timeout=5).stdout.strip() or "?"
    except Exception:
        return "?"


try:
    raw = os.environ.get("HOOK_IN", "")
    d = json.loads(raw) if raw.strip() else {}
except Exception:
    d = {}

event = d.get("hook_event_name", "")
cwd = d.get("cwd") or "."
trigger = d.get("trigger") or "?"
out_dir = os.path.join(cwd, ".claude", "hooks")
try:
    os.makedirs(out_dir, exist_ok=True)
except Exception:
    sys.exit(0)

ts = git(cwd, "log", "-1", "--format=%cI")
branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
commit = git(cwd, "log", "-1", "--oneline")

if event == "PreCompact":
    obj = {
        "ts": ts, "event": "PreCompact", "trigger": trigger,
        "branch": branch, "commit": commit,
        "linear": {"project": LINEAR_PROJECT, "commandCenter": COMMAND_CENTER},
        "note": ("On resume read Linear " + LINEAR_PROJECT + " + In-Progress "
                 "issues + " + COMMAND_CENTER + "; load linear-interface "
                 "skill; emit RESUME-ACK."),
    }
    path = os.path.join(out_dir, "resume-pointer.json")
elif event == "PostCompact":
    obj = {
        "ts": ts, "event": "PostCompact", "trigger": trigger,
        "branch": branch, "commit": commit,
    }
    path = os.path.join(out_dir, "last-compaction.json")
else:
    sys.exit(0)

try:
    with open(path, "w") as f:
        json.dump(obj, f)
        f.write("\n")
except Exception:
    pass
PY
exit 0
