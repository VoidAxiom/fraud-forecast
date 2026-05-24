#!/usr/bin/env python3
"""SessionStart context-recovery payload builder.

Reads the SessionStart hook stdin JSON, emits the hookSpecificOutput JSON
that injects a forced retrieval reflex + un-fakeable visible ACK. Pure;
never raises to the caller (errors -> a still-valid loud degraded payload),
so the recovery reflex can never silently vanish.
"""
import json
import subprocess
import sys

LINEAR_PROJECT = "fraud-forecast"
COMMAND_CENTER = "VOI-139"


def _git(cwd: str, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "?"
    except Exception:
        return "?"


def build(payload: dict) -> str:
    src = str(payload.get("source") or "?")
    cwd = str(payload.get("cwd") or ".")
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    commit = _git(cwd, "log", "-1", "--oneline")
    ts = _git(cwd, "log", "-1", "--format=%cI") if branch != "?" else "?"

    msg = (
        f"↻ CONTEXT-RECOVERY (source={src}). The context window was just "
        f"reset; prior conversation is summarized or lost. Before planning or "
        f"taking ANY action you MUST recover state:\n"
        f"1) Linear: read project '{LINEAR_PROJECT}' description; list issues "
        f"with state=In Progress; open {COMMAND_CENTER} (command center) and "
        f"its latest comment; open the highest-priority In-Progress issue + "
        f"its latest comment.\n"
        f"2) git: branch {branch} @ {commit} (last commit {ts}).\n"
        f"3) Load a 'linear-interface' skill if it exists; otherwise read "
        f"the local memory MEMORY.md index.\n"
        f"THEN your VERY FIRST output line must be EXACTLY:\n"
        f"   ↻ RESUME-ACK source={src} branch={branch} started=<N>\n"
        f"where <N> is the number of In-Progress Linear issues you actually "
        f"found. You cannot know N without doing step 1 — that is "
        f"intentional; the ACK is un-fakeable proof the reflex fired.\n"
        f"If Linear is unreachable: enter DEGRADED MODE — say so explicitly, "
        f"rely on git + MEMORY.md, do NOT fabricate state, and still emit the "
        f"ACK line with started=UNAVAILABLE.\n"
        f"Absence of the RESUME-ACK line means this recovery reflex FAILED "
        f"and must be investigated before continuing."
    )
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": msg,
        }
    })


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    sys.stdout.write(build(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
