#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

BUILDER="${BUILDER:-./build_macos_release_v3.sh}"
[[ -x "$BUILDER" ]] || {
  echo "ERROR: $BUILDER is missing or not executable." >&2
  echo "Place release_signed.sh and build_macos_release_v3.sh in the project root." >&2
  exit 1
}

# Exact Developer ID certificate selected by SHA-1 fingerprint, avoiding the
# two duplicate display names in the Keychain.
export MACOS_SIGN_IDENTITY="${MACOS_SIGN_IDENTITY:-B97863CA4E17170FCD5FBFA4C76A8DF3D91D5F6B}"
export NOTARY_PROFILE="${NOTARY_PROFILE:-workwork-caption-notary}"
export BUNDLE_ID="${BUNDLE_ID:-fun.workwork.cut}"
export APP_NAME="${APP_NAME:-Cut}"
export APP_SAFE_NAME="${APP_SAFE_NAME:-Cut}"
export APP_PUBLISHER="${APP_PUBLISHER:-WORKWORK.FUN}"
export APP_AUTHOR="${APP_AUTHOR:-Sylwester Mielniczuk}"
export COPYRIGHT_YEAR="${COPYRIGHT_YEAR:-2026}"
export APP_LOG_LEVEL="${APP_LOG_LEVEL:-INFO}"
export APP_ICON_BACKGROUND="${APP_ICON_BACKGROUND:-#000000}"

# Use the existing project logo by default. Override with a 1024px PNG/SVG:
#   APP_ICON_SOURCE=/path/to/icon.png ./release_signed.sh
if [[ -z "${APP_ICON_SOURCE:-}" && -f "assets/images/icons/logo.svg" ]]; then
  export APP_ICON_SOURCE="assets/images/icons/logo.svg"
fi

exec "$BUILDER"
