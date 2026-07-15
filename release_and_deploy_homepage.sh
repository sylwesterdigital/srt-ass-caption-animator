#!/usr/bin/env bash
# One command: build Cut, publish the GitHub release, then update the homepage.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

RELEASE_MODE="${RELEASE_MODE:-published}"
VERSION_OVERRIDE=""
SKIP_BUILD=0
SKIP_HOMEPAGE=0
HOMEPAGE_DRY_RUN=0
DELETE_REMOTE=1
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"
PUSH_CURRENT_BRANCH="${PUSH_CURRENT_BRANCH:-1}"
COMMIT_RELEASE_METADATA="${COMMIT_RELEASE_METADATA:-1}"

RELEASE_SCRIPT="${RELEASE_SCRIPT:-./release_signed.sh}"
PUBLISH_SCRIPT="${PUBLISH_SCRIPT:-./publish_github_release.sh}"
HOMEPAGE_SCRIPT="${HOMEPAGE_SCRIPT:-./deploy_homepage.sh}"
HOMEPAGE_URL="${HOMEPAGE_URL:-https://mojoworks.xyz/labs/cut/}"
LAST_RELEASE_ENV="${LAST_RELEASE_ENV:-release/.last-release.env}"

log() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32mOK\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage:
  ./release_and_deploy_homepage.sh
  ./release_and_deploy_homepage.sh --version 0.1.1
  ./release_and_deploy_homepage.sh --prerelease
  ./release_and_deploy_homepage.sh --skip-build
  ./release_and_deploy_homepage.sh --homepage-dry-run

Default workflow:
  1. Build, sign, notarize, and staple Cut.app/DMG.
  2. Publish the release as GitHub Latest.
  3. Wait until the new Cut asset is visible through GitHub.
  4. Deploy the homepage with links to that release.
  5. Delete obsolete remote homepage files.
  6. Verify release.json on the public website.

Options:
  --version VERSION       Set VERSION.txt before building
  --published             Publish as stable GitHub Latest (default)
  --prerelease            Publish as a prerelease
  --draft                 Create/update a draft; homepage deployment is skipped
  --skip-build            Publish the newest existing assets in ./release
  --skip-homepage         Do not deploy the homepage
  --homepage-dry-run      Build homepage but simulate rsync
  --keep-remote-files     Do not pass --delete-remote
  --allow-dirty           Permit uncommitted source changes
  --no-push               Do not push the current branch before tagging
  --no-metadata-commit    Do not commit VERSION.txt / BUILD_NUMBER.txt
  -h, --help              Show help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      [[ $# -ge 2 ]] || die "--version requires a value"
      VERSION_OVERRIDE="$2"
      shift 2
      ;;
    --published)
      RELEASE_MODE="published"
      shift
      ;;
    --prerelease)
      RELEASE_MODE="prerelease"
      shift
      ;;
    --draft)
      RELEASE_MODE="draft"
      shift
      ;;
    --skip-build)
      SKIP_BUILD=1
      shift
      ;;
    --skip-homepage)
      SKIP_HOMEPAGE=1
      shift
      ;;
    --homepage-dry-run)
      HOMEPAGE_DRY_RUN=1
      shift
      ;;
    --keep-remote-files)
      DELETE_REMOTE=0
      shift
      ;;
    --allow-dirty)
      ALLOW_DIRTY=1
      shift
      ;;
    --no-push)
      PUSH_CURRENT_BRANCH=0
      shift
      ;;
    --no-metadata-commit)
      COMMIT_RELEASE_METADATA=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

case "$RELEASE_MODE" in
  published|prerelease|draft) ;;
  *) die "RELEASE_MODE must be published, prerelease, or draft." ;;
esac

command -v git >/dev/null 2>&1 || die "git is required."
command -v gh >/dev/null 2>&1 || die "GitHub CLI is required."
command -v curl >/dev/null 2>&1 || die "curl is required."
command -v python3 >/dev/null 2>&1 || die "python3 is required."
gh auth status >/dev/null 2>&1 || die "Run gh auth login first."

[[ -x "$PUBLISH_SCRIPT" ]] || die "Missing executable: $PUBLISH_SCRIPT"

if [[ ! -x "$HOMEPAGE_SCRIPT" && -x "./homepage/deploy_homepage.sh" ]]; then
  HOMEPAGE_SCRIPT="./homepage/deploy_homepage.sh"
fi

if [[ "$SKIP_HOMEPAGE" == "0" && "$RELEASE_MODE" != "draft" ]]; then
  [[ -x "$HOMEPAGE_SCRIPT" ]] || die "Homepage deployment script not found: $HOMEPAGE_SCRIPT"
fi

if [[ -n "$VERSION_OVERRIDE" ]]; then
  [[ "$VERSION_OVERRIDE" =~ ^[0-9]+([.][0-9]+){1,3}([_-][0-9A-Za-z.-]+)?$ ]] \
    || die "Invalid version: $VERSION_OVERRIDE"
  printf '%s\n' "$VERSION_OVERRIDE" > VERSION.txt
  log "Version set to $VERSION_OVERRIDE"
fi

if [[ "$ALLOW_DIRTY" != "1" ]]; then
  DIRTY_SOURCE="$(
    git status --porcelain --untracked-files=all \
      | sed -E 's/^.. //' \
      | grep -Ev '^(\.macos-build/|release/|homepage/\.deploy_build/|VERSION\.txt$|BUILD_NUMBER\.txt$|DEV_BUILD_NUMBER\.txt$)' \
      || true
  )"
  if [[ -n "$DIRTY_SOURCE" ]]; then
    git status --short
    die "Commit the current Cut source before releasing, or use --allow-dirty deliberately."
  fi
fi

if [[ "$SKIP_BUILD" == "0" ]]; then
  [[ -x "$RELEASE_SCRIPT" ]] || die "Missing executable: $RELEASE_SCRIPT"
  log "Building, signing, notarizing, and stapling Cut"
  "$RELEASE_SCRIPT"
  ok "macOS release assets created"

  if [[ "$COMMIT_RELEASE_METADATA" == "1" ]]; then
    LATEST_DMG="$(
      find release -maxdepth 1 -type f         -name 'Cut-v*-b*-macOS-*.dmg' -print0       | xargs -0 ls -1t 2>/dev/null       | head -n 1 || true
    )"
    [[ -n "$LATEST_DMG" ]]       || die "The build completed but no Cut DMG was found."

    DMG_NAME="$(basename "$LATEST_DMG")"
    RELEASE_VERSION="${DMG_NAME#Cut-v}"
    RELEASE_VERSION="${RELEASE_VERSION%%-b*}"
    RELEASE_BUILD="${DMG_NAME#*-b}"
    RELEASE_BUILD="${RELEASE_BUILD%%-macOS-*}"

    git add VERSION.txt BUILD_NUMBER.txt
    if ! git diff --cached --quiet -- VERSION.txt BUILD_NUMBER.txt; then
      log "Committing release metadata"
      git commit -m "Release Cut ${RELEASE_VERSION} build ${RELEASE_BUILD}"
      ok "Release metadata committed"
    else
      log "Release metadata is already committed"
    fi
  fi
else
  warn "Skipping build; the newest existing assets in ./release will be published."
fi

log "Publishing GitHub release as $RELEASE_MODE"
ALLOW_DIRTY="$ALLOW_DIRTY" \
PUSH_CURRENT_BRANCH="$PUSH_CURRENT_BRANCH" \
GITHUB_RELEASE_MODE="$RELEASE_MODE" \
LAST_RELEASE_ENV="$LAST_RELEASE_ENV" \
"$PUBLISH_SCRIPT"

[[ -f "$LAST_RELEASE_ENV" ]] || die "Release metadata was not created: $LAST_RELEASE_ENV"
# shellcheck disable=SC1090
source "$LAST_RELEASE_ENV"

[[ -n "${RELEASE_TAG:-}" ]] || die "Release tag is missing."
[[ -n "${RELEASE_URL:-}" ]] || die "Release URL is missing."

if [[ "$RELEASE_MODE" == "draft" ]]; then
  warn "Draft release created. Homepage deployment was skipped because drafts are not public."
  printf '\nRelease:\n%s\n' "$RELEASE_URL"
  exit 0
fi

log "Waiting for GitHub to expose $RELEASE_TAG and its Cut assets"
RELEASE_VISIBLE=0
for attempt in $(seq 1 18); do
  RELEASE_JSON="$(
    gh release view "$RELEASE_TAG" \
      --json isDraft,isPrerelease,assets,url \
      2>/dev/null || true
  )"

  if RELEASE_JSON="$RELEASE_JSON" python3 - <<'PY'
import json
import os
import sys

try:
    data = json.loads(os.environ.get("RELEASE_JSON") or "{}")
except Exception:
    raise SystemExit(1)

assets = data.get("assets") or []
names = [str(asset.get("name") or "") for asset in assets]
valid = (
    not data.get("isDraft")
    and any(name.startswith("Cut-v") and name.endswith(".dmg") for name in names)
    and any(name.startswith("Cut-v") and name.endswith(".zip") for name in names)
    and any(name.startswith("Cut-v") and "SHA256" in name for name in names)
)
raise SystemExit(0 if valid else 1)
PY
  then
    RELEASE_VISIBLE=1
    break
  fi

  info="GitHub release propagation attempt ${attempt}/18"
  printf '==> %s\n' "$info"
  sleep 5
done

[[ "$RELEASE_VISIBLE" == "1" ]] \
  || die "The release exists, but its complete Cut assets were not visible after 90 seconds."

ok "GitHub release and assets are public"

if [[ "$SKIP_HOMEPAGE" == "1" ]]; then
  warn "Homepage deployment skipped."
  printf '\nRelease:\n%s\n' "$RELEASE_URL"
  exit 0
fi

HOMEPAGE_CHANNEL="stable"
[[ "$RELEASE_MODE" == "prerelease" ]] && HOMEPAGE_CHANNEL="prerelease"

HOMEPAGE_ARGS=(--release-channel "$HOMEPAGE_CHANNEL")
[[ "$DELETE_REMOTE" == "1" ]] && HOMEPAGE_ARGS+=(--delete-remote)
[[ "$HOMEPAGE_DRY_RUN" == "1" ]] && HOMEPAGE_ARGS+=(--dry-run --keep-build)

log "Updating the Cut homepage for $RELEASE_TAG"
"$HOMEPAGE_SCRIPT" "${HOMEPAGE_ARGS[@]}"

if [[ "$HOMEPAGE_DRY_RUN" == "1" ]]; then
  warn "Homepage deployment was a dry run. The GitHub release is public, but the live homepage was not changed."
  exit 0
fi

log "Verifying public homepage release metadata"
VERIFY_OK=0
for attempt in $(seq 1 12); do
  RELEASE_METADATA="$(
    curl --fail --silent --show-error --location \
      "${HOMEPAGE_URL%/}/release.json?tag=${RELEASE_TAG}&attempt=${attempt}" \
      2>/dev/null || true
  )"

  if RELEASE_METADATA="$RELEASE_METADATA" EXPECTED_TAG="$RELEASE_TAG" python3 - <<'PY'
import json
import os

try:
    data = json.loads(os.environ.get("RELEASE_METADATA") or "{}")
except Exception:
    raise SystemExit(1)

tag = str(data.get("tag") or "")
download = str(data.get("download_url") or "")
expected = os.environ["EXPECTED_TAG"]

valid = (
    tag == expected
    and "/releases/download/" in download
    and "/Cut-" in download
)
raise SystemExit(0 if valid else 1)
PY
  then
    VERIFY_OK=1
    break
  fi

  printf '==> Homepage propagation attempt %s/12\n' "$attempt"
  sleep 5
done

[[ "$VERIFY_OK" == "1" ]] \
  || die "The release was published, but the public homepage does not yet report $RELEASE_TAG."

printf '\n'
ok "Release and homepage deployment completed"
printf 'GitHub:   %s\n' "$RELEASE_URL"
printf 'Homepage: %s\n' "$HOMEPAGE_URL"
printf 'Tag:      %s\n' "$RELEASE_TAG"
