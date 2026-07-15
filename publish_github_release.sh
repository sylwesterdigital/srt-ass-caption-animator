#!/usr/bin/env bash
# Publish the newest signed and notarized Cut build from ./release.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

APP_SAFE_NAME="${APP_SAFE_NAME:-Cut}"
RELEASE_DIR="${RELEASE_DIR:-release}"
RELEASE_NOTES_FILE="${RELEASE_NOTES_FILE:-RELEASE_NOTES.md}"
GITHUB_RELEASE_MODE="${GITHUB_RELEASE_MODE:-draft}"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"
PUSH_CURRENT_BRANCH="${PUSH_CURRENT_BRANCH:-0}"
LAST_RELEASE_ENV="${LAST_RELEASE_ENV:-$RELEASE_DIR/.last-release.env}"

log() { printf '==> %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is not installed."
command -v gh >/dev/null 2>&1 || die "GitHub CLI is missing. Install it with: brew install gh"
command -v python3 >/dev/null 2>&1 || die "python3 is required."
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Run this inside the Git repository."
git remote get-url origin >/dev/null 2>&1 || die "No Git remote named origin is configured."
gh auth status >/dev/null 2>&1 || die "GitHub CLI is not authenticated. Run: gh auth login"

case "$GITHUB_RELEASE_MODE" in
  draft|prerelease|published) ;;
  *) die "GITHUB_RELEASE_MODE must be draft, prerelease, or published." ;;
esac

if [[ "$ALLOW_DIRTY" != "1" ]]; then
  DIRTY_SOURCE="$(
    git status --porcelain --untracked-files=all \
      | sed -E 's/^.. //' \
      | grep -Ev '^(\.macos-build/|release/|homepage/\.deploy_build/|VERSION\.txt$|BUILD_NUMBER\.txt$|DEV_BUILD_NUMBER\.txt$)' \
      || true
  )"
  if [[ -n "$DIRTY_SOURCE" ]]; then
    git status --short
    die "Commit the release source first, or deliberately use ALLOW_DIRTY=1."
  fi
fi

[[ -d "$RELEASE_DIR" ]] || die "Release directory not found: $RELEASE_DIR"
[[ -f "$RELEASE_NOTES_FILE" ]] || die "Release notes not found: $RELEASE_NOTES_FILE"

DMG_PATH="$(
  find "$RELEASE_DIR" -maxdepth 1 -type f \
    -name "${APP_SAFE_NAME}-v*-b*-macOS-*.dmg" -print0 \
  | xargs -0 ls -1t 2>/dev/null \
  | head -n 1 || true
)"
[[ -n "$DMG_PATH" && -f "$DMG_PATH" ]] || die "No Cut release DMG found in $RELEASE_DIR."

DMG_NAME="$(basename "$DMG_PATH")"
PREFIX="${APP_SAFE_NAME}-v"
REST="${DMG_NAME#${PREFIX}}"
[[ "$REST" != "$DMG_NAME" ]] || die "Unexpected DMG filename: $DMG_NAME"

VERSION="${REST%%-b*}"
AFTER_BUILD="${REST#*-b}"
BUILD_NUMBER="${AFTER_BUILD%%-macOS-*}"
ARCH="${AFTER_BUILD#*-macOS-}"
ARCH="${ARCH%.dmg}"

[[ -n "$VERSION" && -n "$BUILD_NUMBER" && -n "$ARCH" ]] \
  || die "Could not parse version, build, and architecture from $DMG_NAME."

TAG="${RELEASE_TAG:-v${VERSION}-b${BUILD_NUMBER}}"
ZIP_PATH="$RELEASE_DIR/${APP_SAFE_NAME}-v${VERSION}-b${BUILD_NUMBER}-macOS-${ARCH}.zip"
SHA_PATH="$RELEASE_DIR/${APP_SAFE_NAME}-v${VERSION}-b${BUILD_NUMBER}-SHA256.txt"

[[ -f "$ZIP_PATH" ]] || die "Missing ZIP: $ZIP_PATH"
[[ -f "$SHA_PATH" ]] || die "Missing checksum file: $SHA_PATH"

log "Verifying release checksums"
(
  cd "$RELEASE_DIR"
  shasum -a 256 -c "$(basename "$SHA_PATH")"
)

RENDERED_NOTES="$(mktemp "${TMPDIR:-/tmp}/cut-release-notes.XXXXXX.md")"
trap 'rm -f "$RENDERED_NOTES"' EXIT

python3 - "$RELEASE_NOTES_FILE" "$RENDERED_NOTES" "$VERSION" "$BUILD_NUMBER" "$ARCH" "$TAG" <<'PY'
from pathlib import Path
import sys

source, destination, version, build, arch, tag = sys.argv[1:]
text = Path(source).read_text(encoding="utf-8")
for token, value in {
    "{{VERSION}}": version,
    "{{BUILD_NUMBER}}": build,
    "{{ARCH}}": arch,
    "{{TAG}}": tag,
}.items():
    text = text.replace(token, value)
Path(destination).write_text(text, encoding="utf-8")
PY

if [[ "$PUSH_CURRENT_BRANCH" == "1" ]]; then
  BRANCH="$(git branch --show-current)"
  [[ -n "$BRANCH" ]] || die "Detached HEAD; cannot push the current branch."
  log "Pushing branch $BRANCH"
  git push origin "$BRANCH"
fi

HEAD_COMMIT="$(git rev-parse HEAD)"
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  TAG_COMMIT="$(git rev-list -n 1 "$TAG")"
  [[ "$TAG_COMMIT" == "$HEAD_COMMIT" ]] \
    || die "Tag $TAG points to $TAG_COMMIT, not current HEAD $HEAD_COMMIT."
  log "Using existing local tag: $TAG"
else
  log "Creating annotated tag: $TAG"
  git tag -a "$TAG" -m "Cut ${VERSION} build ${BUILD_NUMBER}"
fi

log "Pushing tag: $TAG"
git push origin "$TAG"

TITLE="Cut ${VERSION} (Build ${BUILD_NUMBER})"
ASSETS=("$DMG_PATH" "$ZIP_PATH" "$SHA_PATH")

case "$GITHUB_RELEASE_MODE" in
  draft)
    CREATE_FLAGS=(--draft --latest=false)
    EDIT_FLAGS=(--draft=true --prerelease=false --latest=false)
    ;;
  prerelease)
    CREATE_FLAGS=(--prerelease --latest=false)
    EDIT_FLAGS=(--draft=false --prerelease=true --latest=false)
    ;;
  published)
    CREATE_FLAGS=(--latest)
    EDIT_FLAGS=(--draft=false --prerelease=false --latest=true)
    ;;
esac

if gh release view "$TAG" >/dev/null 2>&1; then
  log "Updating existing GitHub Release: $TAG"
  gh release upload "$TAG" "${ASSETS[@]}" --clobber
  gh release edit "$TAG" \
    --title "$TITLE" \
    --notes-file "$RENDERED_NOTES" \
    "${EDIT_FLAGS[@]}"
else
  log "Creating GitHub Release: $TAG ($GITHUB_RELEASE_MODE)"
  gh release create "$TAG" "${ASSETS[@]}" \
    --verify-tag \
    --title "$TITLE" \
    --notes-file "$RENDERED_NOTES" \
    "${CREATE_FLAGS[@]}"
fi

RELEASE_URL="$(gh release view "$TAG" --json url --jq .url)"
mkdir -p "$(dirname "$LAST_RELEASE_ENV")"
{
  printf 'RELEASE_TAG=%q\n' "$TAG"
  printf 'RELEASE_VERSION=%q\n' "$VERSION"
  printf 'RELEASE_BUILD=%q\n' "$BUILD_NUMBER"
  printf 'RELEASE_ARCH=%q\n' "$ARCH"
  printf 'RELEASE_MODE=%q\n' "$GITHUB_RELEASE_MODE"
  printf 'RELEASE_URL=%q\n' "$RELEASE_URL"
  printf 'RELEASE_DMG=%q\n' "$DMG_PATH"
  printf 'RELEASE_ZIP=%q\n' "$ZIP_PATH"
  printf 'RELEASE_SHA=%q\n' "$SHA_PATH"
} > "$LAST_RELEASE_ENV"

printf '\nGitHub Release ready:\n%s\n' "$RELEASE_URL"
printf 'Release metadata: %s\n' "$LAST_RELEASE_ENV"

if [[ "$GITHUB_RELEASE_MODE" == "draft" ]]; then
  warn "The release remains a draft and is not visible to the homepage."
fi
