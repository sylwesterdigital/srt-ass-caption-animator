#!/usr/bin/env bash
set -Eeuo pipefail

# Apply the current .gitignore to files that were already tracked.
# Files remain on disk; only Git's index is cleaned.

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "ERROR: Run this inside the Cut Git repository." >&2
  exit 1
}
cd "$ROOT"

[[ -f .gitignore ]] || {
  echo "ERROR: .gitignore is missing from $ROOT" >&2
  exit 1
}

echo "Tracked files now matched by .gitignore:"
COUNT=0
while IFS= read -r -d '' FILE; do
  printf '  %s\n' "$FILE"
  COUNT=$((COUNT + 1))
done < <(git ls-files -ci --exclude-standard -z)

if [[ "$COUNT" -eq 0 ]]; then
  echo "OK: No tracked ignored files need cleanup."
  exit 0
fi

printf '\nThis will remove %s ignored file(s) from Git tracking.\n' "$COUNT"
echo "The local files will NOT be deleted."
read -r -p "Continue? [y/N] " ANSWER

case "$ANSWER" in
  y|Y|yes|YES)
    ;;
  *)
    echo "Cancelled."
    exit 0
    ;;
esac

while IFS= read -r -d '' FILE; do
  git rm --cached --ignore-unmatch -- "$FILE"
done < <(git ls-files -ci --exclude-standard -z)

echo
echo "Repository index cleaned."
echo "Review the result:"
echo "  git status --short"
echo
echo "Then commit:"
echo '  git add .gitignore'
echo '  git commit -m "Clean generated files from repository"'
