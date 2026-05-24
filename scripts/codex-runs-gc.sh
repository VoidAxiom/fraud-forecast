#!/usr/bin/env bash
# Garbage-collect .codex-runs/ (gitignored, local-only) so it doesn't bloat.
#
# Protocol (don't delete too early): a run dir is removed ONLY if it is
# clearly finished — its loop-status is terminal AND no git branch/worktree
# still references its work. parallel-metrics.tsv (the durable synthesis
# signal) is NEVER touched. Run dirs with no loop-status (in-flight, or a
# bare worker packet still being fanned in) are kept unless --aggressive and
# older than DAYS.
#
#   scripts/codex-runs-gc.sh [--dry-run] [--aggressive] [--days N]
set -uo pipefail

# --- Isolated self-test ----------------------------------------------------
# `codex-runs-gc.sh --self-test` builds a throwaway git repo with synthetic
# .codex-runs families + refs and re-invokes THIS script inside it, asserting
# the merged/gone policy. Hermetic: no network, no touch of the real repo.
if [ "${1:-}" = "--self-test" ]; then
  set -e
  SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  T="$(mktemp -d)"
  trap 'rm -rf "$T"' EXIT
  cd "$T"
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t \
         GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
  git init -q -b main .
  git commit -q --allow-empty -m base
  git update-ref refs/remotes/origin/main HEAD
  # (i) finished family whose ONLY ref is merged into origin/main -> GC'd.
  git branch sk/voi-77-merged HEAD
  mkdir -p .codex-runs/voi77-merged
  echo APPROVED > .codex-runs/voi77-merged/loop-status.txt
  mkdir -p .codex-runs/voi77-merged-w0
  # (ii) finished family whose ref exists and is NOT merged -> kept.
  git commit -q --allow-empty -m unmerged
  git branch sk/voi-88-open HEAD
  git update-ref -d refs/heads/main 2>/dev/null || true
  mkdir -p .codex-runs/voi88-open
  echo APPROVED > .codex-runs/voi88-open/loop-status.txt
  echo keep > .codex-runs/parallel-metrics.tsv
  bash "$SELF" >/dev/null
  fail=0
  [ ! -d .codex-runs/voi77-merged ]    || { echo "FAIL (i): merged family not GC'd" >&2; fail=1; }
  [ ! -d .codex-runs/voi77-merged-w0 ] || { echo "FAIL (i): merged child not GC'd" >&2; fail=1; }
  [ -d .codex-runs/voi88-open ]        || { echo "FAIL (ii): unmerged family wrongly GC'd" >&2; fail=1; }
  [ -f .codex-runs/parallel-metrics.tsv ] || { echo "FAIL: parallel-metrics.tsv deleted" >&2; fail=1; }
  if [ "$fail" = 0 ]; then echo "codex-runs-gc self-test: PASS"; else exit 1; fi
  exit 0
fi
# ---------------------------------------------------------------------------

cd "$(git rev-parse --show-toplevel)" || exit 1
RUNS=".codex-runs"
[ -d "$RUNS" ] || { echo "no $RUNS"; exit 0; }

DRY=0; AGGR=0; DAYS=3
while [ $# -gt 0 ]; do case "$1" in
  --dry-run) DRY=1;; --aggressive) AGGR=1;;
  # --days must take a NUMERIC value. Only consume the next token as the
  # value when it actually is a nonnegative integer — otherwise a flag like
  # `--dry-run` would be swallowed as the day count.
  --days)
    case "${2:-}" in
      ''|*[!0-9]*) echo "codex-runs-gc: --days requires a nonnegative integer value" >&2; exit 2;;
      *) DAYS="$2"; shift;;
    esac;;
  *) echo "unknown arg $1" >&2; exit 2;; esac; shift; done

# Defense in depth: DAYS must be a nonnegative integer before any deletion.
case "$DAYS" in
  ''|*[!0-9]*) echo "codex-runs-gc: --days must be a nonnegative integer (got '$DAYS')" >&2; exit 2;;
esac

# Branch slugs still alive locally or on origin — never GC a run whose work
# could still be open.
ALIVE="$(git for-each-ref --format='%(refname:short)' refs/heads refs/remotes/origin 2>/dev/null | tr '\n' ' ')"

# Refs MERGED into origin/main do NOT protect their run family:
# the work is landed, so even though the branch may not be deleted yet, its
# terminal .codex-runs artifacts are collectible (merged/gone policy). A
# family is protected ONLY by an ALIVE ref that is NOT in this set.
MERGED="$(git for-each-ref --format='%(refname:short)' --merged origin/main refs/heads refs/remotes/origin 2>/dev/null | tr '\n' ' ')"

# True iff some ref name matching the run slug exists in ALIVE *and* that
# same matching ref is NOT merged into origin/main (so work may
# still be open). Iterates real ref names so the merged check is per-ref.
slug_protected(){ # $1 = slug fragment (base id minus the issue prefix)
  local frag r rl
  frag="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  for r in $ALIVE; do
    rl="$(printf '%s' "$r" | tr '[:upper:]' '[:lower:]')"
    case "$rl" in
      *"$frag"*)
        case " $MERGED " in *" $r "*) ;; *) return 0;; esac;;
    esac
  done
  return 1
}

# GC by FAMILY, not per dir. Run families form when a parent codex-run is
# followed by child runs (e.g. <id>/, <id>-w0/, <id>-rev1/, <id>-fix1/)
# during local /codex:review iteration. In this template (no autonomous
# bash loop), the terminal-status branch ('loop-status.txt') is never
# written, so families are collected only when --aggressive is set AND no
# alive non-merged branch references the slug. The family grouping is
# still load-bearing because per-dir decisions would keep child dirs
# forever. derive each run's base id, decide ONCE per base, remove the
# WHOLE family together. ('loop-status.txt' remains forward-compatible:
# projects that re-introduce a bash loop later get the conservative-mode
# GC path back automatically.)
base_of(){ # strip child suffixes to the run base
  local n="$1"
  n="${n%-w[0-9]}"; n="${n%-w[0-9][0-9]}"
  n="${n%-rev[0-9]}"; n="${n%-rev[0-9][0-9]}"
  n="${n%-fix[0-9]}"; n="${n%-fix[0-9][0-9]}"
  n="${n%-prepr}"; n="${n%-selfreview}"
  printf '%s' "$n"
}
# base_of() prints WITHOUT a trailing newline (so it composes in ${}), so
# newline-terminate each here or `sort -u` would collapse every base into
# one mashed token.
bases="$(for d in "$RUNS"/*/; do base_of "$(basename "$d")"; echo; done \
         | sort -u)"

kept=0; gone=0
for base in $bases; do
  [ -n "$base" ] || continue
  # Never GC while an alive, NOT-yet-merged branch could still reference
  # this work.
  if slug_protected "${base#voi*-}"; then
    kept=$((kept+1)); continue
  fi
  # Exact family by base_of() equality — NOT a "$base"-* glob, which would
  # sweep a separate run whose id merely has this base as a prefix.
  fam_dirs=()
  for d in "$RUNS"/*/; do
    [ "$(base_of "$(basename "$d")")" = "$base" ] && fam_dirs+=("$d")
  done
  # bash 3.2 (macOS) errors on "${arr[@]}" when arr is empty under set -u.
  [ "${#fam_dirs[@]}" -gt 0 ] || continue
  status="$(cat "$RUNS/$base/loop-status.txt" 2>/dev/null || echo "")"
  finished=0
  case "$status" in APPROVED|ITER_CAP|ESCALATE|WORKER_FAILED) finished=1;; esac
  newest=""
  for d in "${fam_dirs[@]}"; do
    [ -n "$(find "$d" -maxdepth 0 -mtime -"$DAYS" 2>/dev/null)" ] && newest=1
  done
  if [ "$finished" = 1 ] || { [ "$AGGR" = 1 ] && [ -z "$newest" ]; }; then
    for fam in "${fam_dirs[@]}"; do
      [ -d "$fam" ] || continue
      if [ "$DRY" = 1 ]; then echo "would rm $fam (base='$base' status='${status:-none}')"
      else rm -rf "$fam"; fi
      gone=$((gone+1))
    done
  else
    kept=$((kept+1))
  fi
done
echo "codex-runs-gc: removed=$gone family-dirs, kept=$kept families (parallel-metrics.tsv preserved)"
