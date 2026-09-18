#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
printf '\033[1;33mWARN:\033[0m build_macos_release.sh is a compatibility entry point; using build_macos_release_v3.sh.\n' >&2
exec "$SCRIPT_DIR/build_macos_release_v3.sh" "$@"
