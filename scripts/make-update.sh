#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

if [[ $# -lt 2 ]]; then
  cat <<'USAGE'
Usage:
  ./scripts/make-update.sh TARGET_VERSION PAYLOAD_DIR [SUMMARY]

Environment:
  BASE_VERSION=0.1.1
  RELEASE_MODE=published|prerelease|draft|none
  NOTES_FILE=/path/to/notes.txt   # one release-note bullet per non-empty line
  DELETE_PATHS='old/file.sh:old/dir'

The payload directory contains repository-relative files to overlay. VERSION.txt
is created/overwritten in the package so it always matches TARGET_VERSION.
USAGE
  exit 2
fi

TARGET_VERSION="$1"
SOURCE_DIR="$2"
SUMMARY="${3:-Cut update $TARGET_VERSION}"
BASE_VERSION="${BASE_VERSION:-$(tr -d '[:space:]' < VERSION.txt 2>/dev/null || true)}"
RELEASE_MODE="${RELEASE_MODE:-published}"
ARCHIVE_DIR="${CUT_UPDATE_ARCHIVE_DIR:-$ROOT/archive}"
NOTES_FILE="${NOTES_FILE:-}"
DELETE_PATHS="${DELETE_PATHS:-}"

[[ "$TARGET_VERSION" =~ ^[0-9]+([.][0-9]+){1,3}([_-][0-9A-Za-z.-]+)?$ ]] || { echo "Invalid target version: $TARGET_VERSION" >&2; exit 1; }
[[ -d "$SOURCE_DIR" ]] || { echo "Payload directory not found: $SOURCE_DIR" >&2; exit 1; }
case "$RELEASE_MODE" in published|prerelease|draft|none) ;; *) echo "Invalid RELEASE_MODE: $RELEASE_MODE" >&2; exit 1 ;; esac

mkdir -p "$ARCHIVE_DIR"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/cut-make-update.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT INT TERM
mkdir -p "$TMP/payload"
rsync --archive --checksum "$SOURCE_DIR/" "$TMP/payload/"
printf '%s\n' "$TARGET_VERSION" > "$TMP/payload/VERSION.txt"

python3 - "$TMP" "$TARGET_VERSION" "$BASE_VERSION" "$RELEASE_MODE" "$SUMMARY" "$NOTES_FILE" "$DELETE_PATHS" <<'PY'
from pathlib import Path
import hashlib, json, os, sys
root=Path(sys.argv[1]); version,base,mode,summary,notes_file,delete_paths=sys.argv[2:]
payload=root/'payload'
files={}
for p in sorted(payload.rglob('*')):
    if p.is_file():
        rel=p.relative_to(payload).as_posix()
        files[rel]=hashlib.sha256(p.read_bytes()).hexdigest()
notes=[]
if notes_file:
    for line in Path(notes_file).read_text().splitlines():
        line=line.strip().lstrip('-').strip()
        if line: notes.append(line)
deletes=[x for x in delete_paths.split(':') if x]
manifest={
    'schema':1,
    'product':'Cut',
    'version':version,
    'base_version':base,
    'release_mode':mode,
    'summary':summary,
    'notes':notes,
    'delete':deletes,
    'files':files,
}
(root/'manifest.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
PY

OUT="$ARCHIVE_DIR/Cut-update-v${TARGET_VERSION}.zip"
rm -f "$OUT"
(
  cd "$TMP"
  zip -qry "$OUT" manifest.json payload
)
shasum -a 256 "$OUT"
printf 'Created: %s\n' "$OUT"
