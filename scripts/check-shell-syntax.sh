#!/usr/bin/env bash
# check-shell-syntax.sh — bash -n syntax-check shell scripts. Operates on
# the STAGED BLOB (via `git show :$FILE`), not the working-tree content, so
# the check sees exactly what would be committed. Catches the case where
# you `git add`'d a broken script, then fixed the working tree, but the
# broken version is what's staged.
#
# When called with no args (the pre-commit hook path), defaults to:
#   git ls-files '*.sh' 'scripts/hooks/*' 'hooks/worktree-impl-hooks/*'
#
# Includes a python3 -c apostrophe hint: a common pitfall when embedding
# Python in shell single-quoted strings.

set -euo pipefail

if ! command -v bash >/dev/null 2>&1; then
  echo "check-shell-syntax: bash is required for syntax checks" >&2
  exit 2
fi

if [ "$#" -eq 0 ]; then
  FILES=()
  while IFS= read -r FILE; do
    FILES+=("$FILE")
  done < <(git ls-files '*.sh' 'scripts/hooks/*' 'hooks/worktree-impl-hooks/*')
else
  FILES=("$@")
fi

if [ "${#FILES[@]}" -eq 0 ]; then
  echo "check-shell-syntax: no shell scripts matched"
  exit 0
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

HAS_MISSING_FILE=0
HAS_SYNTAX_ERROR=0

echo "check-shell-syntax: checking ${#FILES[@]} shell script(s)..."

for INDEX in "${!FILES[@]}"; do
  FILE="${FILES[$INDEX]}"
  if [ -z "$FILE" ]; then
    continue
  fi

  BLOB_FILE="$TMP_DIR/$INDEX.sh"
  if ! git show ":$FILE" >"$BLOB_FILE" 2>/dev/null; then
    echo "check-shell-syntax: not an index blob or path is not tracked: $FILE" >&2
    HAS_MISSING_FILE=1
    continue
  fi

  if ! bash -n "$BLOB_FILE"; then
    HAS_SYNTAX_ERROR=1
    echo "check-shell-syntax: bash syntax error in $FILE" >&2
    if grep -Fq "python3 -c '" "$BLOB_FILE"; then
      echo "  Hint: this often indicates an unescaped ASCII apostrophe inside a single-quoted \`python3 -c\` string." >&2
    fi
  fi
done

if [ "$HAS_MISSING_FILE" -ne 0 ]; then
  exit 2
fi
if [ "$HAS_SYNTAX_ERROR" -ne 0 ]; then
  exit 1
fi

echo "check-shell-syntax: all checked scripts parse cleanly."
exit 0
