#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
printf '\033[1;33mWARN:\033[0m build_macos_release_v2.sh is retired; using the current deterministic builder.\n' >&2
exec "$SCRIPT_DIR/build_macos_release_v3.sh" "$@"
