#!/usr/bin/env bash
# impl-precommit-scope.sh — validates that a set of paths is within the
# impl's allowed path scope. Two modes:
#
#   --cached (default) — validate the STAGED diff (`git diff --cached`).
#                        This is the pre-commit hook mode: the per-worktree
#                        hook (see hooks/worktree-impl-hooks/pre-commit)
#                        invokes the script with no args before every
#                        commit, blocking out-of-scope staged paths. The
#                        hook enforces the ROLE allowlist only (catch-early);
#                        per-packet scope is enforced by Claude's --base
#                        invocation below.
#
#   --base <ref> --worktree <path> --scope-file <path>
#                      — validate the COMMITTED diff between <ref> and
#                        HEAD (`git diff <ref>...HEAD`) for the worktree
#                        at <worktree-path>. This is the AUTHORITATIVE
#                        mechanical gate Claude runs during the pre-PR
#                        review loop AND against the final PR head before merge.
#                        It enforces BOTH layers:
#                          (a) the per-packet allowlist (--scope-file),
#                          (b) the role allowlist (hooks/write-scope-guard.mjs).
#                        Without --scope-file, the role allowlist alone is too
#                        broad. Without --worktree, the script's cwd-derived
#                        target could silently validate the main checkout
#                        instead of the impl's branch.
#
# The PreToolUse `write-scope-guard` only catches Edit/Write/MultiEdit. Git
# operations go through Bash and bypass that hook. This script closes the
# git-side gap by replaying the same scope-guard logic against every path
# in the chosen diff (including deletions and both sides of renames/copies),
# AND adds the per-packet allowlist that the role-only hook can't know.
#
# Scope file format (--scope-file):
#   - one entry per line; blank lines and lines starting with `#` ignored.
#   - each entry is either an EXACT file path (matches `path == entry`)
#     OR a directory prefix ending in `/` (matches `path == entry` OR
#     `path startsWith entry`).
#   - NO globs (`*`, `**`, brace expansion). Anchored matchers only.
#
# Fail-closed semantics:
# - Any path the hook does not EXPLICITLY allow is treated as deny.
# - Hook parse errors / unexpected exit / missing decision → deny.
# - Staged/committed deletions are validated equally (an impl deleting an
#   out-of-scope file is just as bad as creating one).
# - Both sides of renames + copies are validated.
# - Missing/empty --scope-file in --base mode → exit 1 (refuses to run;
#   the spec MUST define the packet allowlist).
#
# Exit codes:
#   0 — all paths in the chosen diff are within scope (also: empty diff)
#   2 — one or more paths out of scope (lists them; refuses to commit)
#   1 — bad args / preflight error

set -euo pipefail

# --- arg parsing -------------------------------------------------------
MODE="cached"
BASE_REF=""
SCOPE_FILE=""
WORKTREE_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cached)
      MODE="cached"
      shift 1
      ;;
    --base)
      MODE="base"
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "✗ impl-precommit-scope: --base requires a ref argument" >&2
        exit 1
      fi
      BASE_REF="$2"
      shift 2
      ;;
    --base=*)
      MODE="base"
      BASE_REF="${1#--base=}"
      if [[ -z "$BASE_REF" ]]; then
        echo "✗ impl-precommit-scope: --base= requires a non-empty ref" >&2
        exit 1
      fi
      shift 1
      ;;
    --worktree)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "✗ impl-precommit-scope: --worktree requires a path argument" >&2
        exit 1
      fi
      WORKTREE_ARG="$2"
      shift 2
      ;;
    --worktree=*)
      WORKTREE_ARG="${1#--worktree=}"
      if [[ -z "$WORKTREE_ARG" ]]; then
        echo "✗ impl-precommit-scope: --worktree= requires a non-empty path" >&2
        exit 1
      fi
      shift 1
      ;;
    --scope-file)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "✗ impl-precommit-scope: --scope-file requires a path argument" >&2
        exit 1
      fi
      SCOPE_FILE="$2"
      shift 2
      ;;
    --scope-file=*)
      SCOPE_FILE="${1#--scope-file=}"
      if [[ -z "$SCOPE_FILE" ]]; then
        echo "✗ impl-precommit-scope: --scope-file= requires a non-empty path" >&2
        exit 1
      fi
      shift 1
      ;;
    -h|--help)
      sed -n '2,55p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "✗ impl-precommit-scope: unknown arg '$1'" >&2
      echo "  usage: $0 [--cached]" >&2
      echo "         $0 --base <ref> --worktree <path> --scope-file <path>" >&2
      exit 1
      ;;
  esac
done

# --- mode-specific arg validation --------------------------------------
if [[ "$MODE" == "base" ]]; then
  if [[ -z "$WORKTREE_ARG" ]]; then
    echo "✗ impl-precommit-scope: --base mode requires --worktree <path>" >&2
    echo "  (refusing to use cwd-derived target — the per-impl worktree must be named explicitly)" >&2
    exit 1
  fi
  if [[ ! -d "$WORKTREE_ARG" ]]; then
    echo "✗ impl-precommit-scope: --worktree '$WORKTREE_ARG' does not exist or is not a directory" >&2
    exit 1
  fi
  if ! git -C "$WORKTREE_ARG" rev-parse --show-toplevel >/dev/null 2>&1; then
    echo "✗ impl-precommit-scope: --worktree '$WORKTREE_ARG' is not inside a git checkout" >&2
    exit 1
  fi
  if [[ -z "$SCOPE_FILE" ]]; then
    echo "✗ impl-precommit-scope: --base mode requires --scope-file <path>" >&2
    echo "  the role allowlist alone is too broad — the packet spec must define which" >&2
    echo "  files THIS packet is allowed to touch. Generate from the spec's allowed-paths" >&2
    echo "  section (one entry per line; trailing / for directory prefixes; no globs)." >&2
    exit 1
  fi
  if [[ ! -f "$SCOPE_FILE" ]]; then
    echo "✗ impl-precommit-scope: --scope-file '$SCOPE_FILE' not found" >&2
    exit 1
  fi
fi

# Resolve the worktree we'll validate. In --cached mode the per-worktree
# pre-commit hook invokes with no flags and cwd == worktree root, so the
# fallback to `git rev-parse --show-toplevel` is correct there.
if [[ -n "$WORKTREE_ARG" ]]; then
  WORKTREE_ROOT="$(cd "$WORKTREE_ARG" && git rev-parse --show-toplevel)"
else
  WORKTREE_ROOT="$(git rev-parse --show-toplevel)"
fi

if [[ "$MODE" == "base" ]]; then
  if ! git -C "$WORKTREE_ROOT" rev-parse --verify "$BASE_REF" >/dev/null 2>&1; then
    echo "✗ impl-precommit-scope: --base ref '$BASE_REF' not found in worktree '$WORKTREE_ROOT'" >&2
    exit 1
  fi
fi

# --- packet allowlist parsing (only used in --base mode) ---------------
declare -a packet_files=()
declare -a packet_prefixes=()
if [[ -n "$SCOPE_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" ]] && continue
    [[ "$line" == \#* ]] && continue
    if [[ "$line" == *'*'* || "$line" == *'?'* || "$line" == *'['* || "$line" == *']'* || "$line" == *'{'* || "$line" == *'}'* ]]; then
      echo "✗ impl-precommit-scope: scope file '$SCOPE_FILE' contains glob metacharacter in entry: $line" >&2
      echo "  use anchored entries only — exact paths or directory prefixes ending in /" >&2
      exit 1
    fi
    if [[ "$line" == /* || "$line" == *'..'* ]]; then
      echo "✗ impl-precommit-scope: scope file '$SCOPE_FILE' contains absolute or ../ entry: $line" >&2
      echo "  entries must be repo-relative and may not contain '..'" >&2
      exit 1
    fi
    if [[ "$line" == */ ]]; then
      packet_prefixes+=("$line")
    else
      packet_files+=("$line")
    fi
  done < "$SCOPE_FILE"
  if (( ${#packet_files[@]} + ${#packet_prefixes[@]} == 0 )); then
    echo "✗ impl-precommit-scope: scope file '$SCOPE_FILE' has no usable entries" >&2
    echo "  refusing to run with an empty packet allowlist (--base mode requires explicit scope)." >&2
    exit 1
  fi
fi

# in_packet_scope <path> → 0 if allowed by packet allowlist, 1 otherwise.
# Only meaningful in --base mode; --cached skips packet enforcement.
#
# NB 1: guard each `for x in "${arr[@]}"` with `(( ${#arr[@]} > 0 ))` —
# without the guard, an unset/empty array expanded under set -u with the
# default-empty sub would iterate once over a single empty string, and
# bash's pattern-match `[[ "$p" == ""* ]]` reduces to `[[ "$p" == * ]]`
# which matches every string (silently allowing all paths).
# NB 2: ALL loop variables MUST be `local` so the outer validation loop's
# $f isn't overwritten by this function's loop.
in_packet_scope() {
  local p="$1"
  local _psf_file
  local _psf_dir
  if (( ${#packet_files[@]} > 0 )); then
    for _psf_file in "${packet_files[@]}"; do
      [[ "$p" == "$_psf_file" ]] && return 0
    done
  fi
  if (( ${#packet_prefixes[@]} > 0 )); then
    for _psf_dir in "${packet_prefixes[@]}"; do
      [[ "$p" == "${_psf_dir%/}" ]] && return 0
      [[ "$p" == "$_psf_dir"* ]] && return 0
    done
  fi
  return 1
}

# AGENT_TYPE selects which scope allowlist the hook applies. DELIBERATELY
# hard-coded — using any worker-writable value (a file at the worktree root,
# an env var the worker controls) to select the authorization scope is the
# exact identity-forgery hole the rail exists to prevent.
AGENT_TYPE="implementer"

# Resolve main repo root from the worktree. Worktrees provisioned by
# worktree-new.sh sit at <repo-parent>/.fraud-forecast-worktrees/<name>/
# (sibling of the main repo). git's --git-common-dir gives the main repo's
# .git directory; its dirname is the main repo root.
GIT_COMMON_DIR="$(git -C "$WORKTREE_ROOT" rev-parse --git-common-dir 2>/dev/null || echo '')"
if [[ -n "$GIT_COMMON_DIR" ]]; then
  MAIN_REPO_ROOT="$(cd "$GIT_COMMON_DIR/.." && pwd)"
else
  MAIN_REPO_ROOT="$WORKTREE_ROOT"
fi
HOOK="${MAIN_REPO_ROOT}/hooks/write-scope-guard.mjs"

if [[ ! -f "$HOOK" ]]; then
  echo "✗ impl-precommit-scope: hook not found at $HOOK" >&2
  exit 1
fi

# Collect every path that needs scope validation. -z (NUL-delimited) for
# safe handling of spaces / quotes / newlines in filenames.
#
# --no-renames is essential: by default git emits only the destination path
# for detected renames. An impl could `git mv lib/secret.ts components/X.tsx`
# (out-of-scope source, in-scope destination), and the scope check would only
# see the destination. --no-renames decomposes every rename into delete +
# add, so BOTH paths get validated.
paths_to_check=()

STAGED_TMP="$(mktemp -t impl-precommit-staged.XXXXXX)"
cleanup_tmp() { rm -f "$STAGED_TMP"; }
trap cleanup_tmp EXIT

if [[ "$MODE" == "base" ]]; then
  # `<ref>...HEAD` (triple-dot) diffs against the merge-base.
  git -C "$WORKTREE_ROOT" diff --name-only -z --no-renames "$BASE_REF...HEAD" > "$STAGED_TMP"
  DIFF_DESC="committed-diff vs $BASE_REF"
  EMPTY_MSG="no committed changes vs $BASE_REF; nothing to validate"
else
  git -C "$WORKTREE_ROOT" diff --cached --name-only -z --no-renames > "$STAGED_TMP"
  DIFF_DESC="staged-diff"
  EMPTY_MSG="no staged changes; nothing to validate"
fi

if [[ ! -s "$STAGED_TMP" ]]; then
  echo "impl-precommit-scope: $EMPTY_MSG"
  exit 0
fi

while IFS= read -r -d '' p; do
  [[ -z "$p" ]] && continue
  if [[ "$p" == *$'\n'* ]]; then
    echo "✗ impl-precommit-scope: $DIFF_DESC path contains a newline byte; refusing to validate" >&2
    exit 2
  fi
  paths_to_check+=("$p")
done < "$STAGED_TMP"

if (( ${#paths_to_check[@]} == 0 )); then
  echo "✗ impl-precommit-scope: git emitted $DIFF_DESC bytes but the parser found no records" >&2
  echo "  raw bytes (hex):" >&2
  xxd "$STAGED_TMP" | head -10 | sed 's/^/    /' >&2 || true
  exit 1
fi

# Validate each path via the hook. Fail-closed.
deny_count=0
denied_paths=()

for f in "${paths_to_check[@]}"; do
  # Layer 1 — packet allowlist (only in --base mode; --cached has no spec context).
  if [[ -n "$SCOPE_FILE" ]]; then
    if ! in_packet_scope "$f"; then
      deny_count=$((deny_count + 1))
      denied_paths+=("$f  [outside packet allowlist — see $SCOPE_FILE]")
      continue
    fi
  fi

  # Layer 2 — role allowlist via the canonical write-scope-guard hook.
  payload="$(node -e '
    const fp = process.argv[1];
    const agent = process.argv[2];
    process.stdout.write(JSON.stringify({
      tool_name: "Write",
      tool_input: { file_path: fp },
      agent_id: "impl",
      agent_type: agent,
      cwd: process.argv[3],
    }));
  ' "$f" "$AGENT_TYPE" "$WORKTREE_ROOT")"

  # Run the hook from the trusted main repo, but name the impl worktree as
  # the active write root. Keeps hook code immutable from the impl side
  # while proving the path is inside the isolated checkout.
  set +e
  out="$(printf '%s' "$payload" | CLAUDE_PROJECT_DIR="$MAIN_REPO_ROOT" TRUSTED_WORKTREE_ROOT="$WORKTREE_ROOT" node "$HOOK" 2>/dev/null)"
  hook_exit=$?
  set -e

  if (( hook_exit != 0 )); then
    deny_count=$((deny_count + 1))
    denied_paths+=("$f  [hook errored — fail-closed]")
    continue
  fi

  if [[ "$out" == *'"permissionDecision":"deny"'* ]]; then
    deny_count=$((deny_count + 1))
    denied_paths+=("$f  [outside role allowlist for agent_type=$AGENT_TYPE]")
    continue
  fi

  if [[ -n "$out" ]]; then
    deny_count=$((deny_count + 1))
    denied_paths+=("$f  [unexpected hook output — fail-closed]")
    continue
  fi
done

if (( deny_count > 0 )); then
  echo "✗ impl-precommit-scope: $deny_count $DIFF_DESC path(s) out of scope for agent_type=$AGENT_TYPE" >&2
  for p in "${denied_paths[@]}"; do
    echo "  • $p" >&2
  done
  echo "" >&2
  if [[ "$MODE" == "base" ]]; then
    echo "Out-of-scope work already landed in the impl's branch — escalate to Claude." >&2
    echo "Do not push or open a PR until the offending commits are removed or rewritten." >&2
    if [[ -n "$SCOPE_FILE" ]]; then
      echo "  packet allowlist: $SCOPE_FILE" >&2
    fi
  else
    echo "Unstage them and re-run, or escalate to Claude if the spec requires touching these paths." >&2
  fi
  exit 2
fi

if [[ -n "$SCOPE_FILE" ]]; then
  echo "✓ impl-precommit-scope: all ${#paths_to_check[@]} $DIFF_DESC path(s) within packet+role scope (scope-file=$SCOPE_FILE, agent_type=$AGENT_TYPE)"
else
  echo "✓ impl-precommit-scope: all ${#paths_to_check[@]} $DIFF_DESC path(s) within role scope (agent_type=$AGENT_TYPE)"
fi
