#!/usr/bin/env bash
# Create a git worktree for the per-packet implementer subagent with REAL,
# in-tree node_modules so the worktree is immediately bootable.
#
# Why a real copy, not a symlink: codex's workspace-write sandbox only
# permits writes inside the worktree dir. A symlinked node_modules resolves
# for reads but its write target is OUTSIDE the worktree, so tools that write
# caches under node_modules (tsbuildinfo, vite temp, etc.) hit EPERM → the
# worker's self-verification false-fails. APFS clonefile makes a real
# independent copy ~free.
#
#   scripts/worktree-new.sh <branch> <dir-or-name> [base-ref]
#
# Arg 2 may be a bare NAME (no "/"): worktrees are then collected under a
# single sibling parent `<parent-of-primary>/.fraud-forecast-worktrees/<name>`
# so the parent dir stays a single project entry, all ephemeral worktrees
# live in one bulk-cleanable place, and an eventual primary-folder rename
# doesn't churn them. An explicit path (containing "/") is honoured as-is.
set -uo pipefail

BRANCH="${1:-}"; DIR="${2:-}"; BASE="${3:-main}"
[ -n "$BRANCH" ] && [ -n "$DIR" ] || {
  echo "usage: worktree-new.sh <branch> <dir-or-name> [base-ref]" >&2; exit 2; }

# Freshness + base pinning (stale-base class): `gh pr merge` does NOT advance
# local refs, so a worktree cut from local `main`/`origin/*`
# can be born stale (missing an already-merged dependency → false build
# fails) and `origin/main` can move mid-run (poisoning review
# diffs). Fetch first, then resolve BASE to a CONCRETE SHA off the freshest
# ref so the worktree is stable regardless of concurrent merges.
# Fail LOUDLY if the fetch fails — silently pinning a stale origin/* recreates
# the exact stale-base worktree this guard prevents. Override: ALLOW_STALE_BASE=1.
if ! git fetch origin --quiet 2>/dev/null; then
  if [ "${ALLOW_STALE_BASE:-}" = 1 ]; then
    echo "worktree-new: git fetch failed — ALLOW_STALE_BASE=1 set, proceeding off LOCAL refs (may be stale)" >&2
  else
    echo "worktree-new: git fetch origin failed — refusing to provision off a possibly-stale base." >&2
    echo "Fix the remote/creds, or set ALLOW_STALE_BASE=1 to override deliberately." >&2
    exit 1
  fi
fi
# Prefer origin/<BASE> ONLY for simple branch names (freshness intent). For
# an explicit commit-ish (HEAD, HEAD~1, a^, a SHA) resolve it EXACTLY first —
# else `origin/HEAD` etc. silently mis-bases off the remote default and
# drops local prerequisite commits.
case "$BASE" in
  HEAD|*[~^:]*|*[!A-Za-z0-9._/-]*) _commitish=1 ;;
  *) printf '%s' "$BASE" | grep -Eq '^[0-9a-f]{7,40}$' && _commitish=1 || _commitish=0 ;;
esac
if [ "$_commitish" = 1 ]; then
  BASE="$(git rev-parse --verify "${BASE}^{commit}" 2>/dev/null \
          || git rev-parse --verify "origin/${BASE}^{commit}" 2>/dev/null \
          || echo "$BASE")"
else
  BASE="$(git rev-parse --verify "origin/${BASE}^{commit}" 2>/dev/null \
          || git rev-parse --verify "${BASE}^{commit}" 2>/dev/null \
          || echo "$BASE")"
fi

PRIMARY="$(git rev-parse --show-toplevel)"
case "$DIR" in
  */*) : ;;  # explicit path — honour as given
  *)
    WT_ROOT="$(dirname "$PRIMARY")/.fraud-forecast-worktrees"
    mkdir -p "$WT_ROOT"
    DIR="$WT_ROOT/$DIR" ;;
esac
[ -d "$PRIMARY/node_modules" ] || {
  echo "primary checkout has no node_modules ($PRIMARY) — run npm ci there first" >&2
  exit 1; }

# Attach an existing branch (re-provisioning a feature branch's worktree)
# or create a fresh one off BASE.
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  git worktree add -q "$DIR" "$BRANCH" || exit 1
else
  git worktree add -q "$DIR" -b "$BRANCH" "$BASE" || exit 1
fi

# Prefer APFS copy-on-write clone (instant, ~zero extra space until a file
# is modified, a fully independent real directory). Fall back ONLY to a
# plain recursive copy. NO hardlink fallback: a hardlink tree shares inodes
# with the primary node_modules, so any in-place rewrite under a worker's
# node_modules would corrupt the primary checkout and break isolation.
SRC="$PRIMARY/node_modules"; DST="$DIR/node_modules"
if cp -cR "$SRC" "$DST" 2>/dev/null; then
  MODE="APFS clone"
else
  cp -R "$SRC" "$DST" || { echo "failed to provision node_modules in $DIR" >&2; exit 1; }
  MODE="copy"
fi

# Auto-wire the per-worktree pre-commit hook (impl-precommit-scope + shell-syntax).
# Requires `git config extensions.worktreeConfig true` on the main repo, which
# init.sh sets at template install time. If the per-worktree hook dir is
# absent (older template install / project removed it), skip silently — the
# explicit step-5 invocation of impl-precommit-scope.sh in the Impl Contract
# is the load-bearing check; this hook is defense-in-depth.
HOOKS_DIR="$PRIMARY/hooks/worktree-impl-hooks"
if [ -d "$HOOKS_DIR" ]; then
  if git -C "$DIR" config --worktree core.hooksPath "$HOOKS_DIR" 2>/dev/null; then
    WT_HOOK_STATUS="wired"
  else
    WT_HOOK_STATUS="(extensions.worktreeConfig not enabled on main repo — run 'git config extensions.worktreeConfig true' there to enable)"
  fi
else
  WT_HOOK_STATUS="(hooks/worktree-impl-hooks/ not present in main repo — skipped)"
fi
echo "worktree $DIR on $BRANCH (base $BASE); node_modules: $MODE (real, in-tree); worktree pre-commit: $WT_HOOK_STATUS"
