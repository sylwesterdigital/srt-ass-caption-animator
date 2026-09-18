#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

ARCHIVE_DIR="${CUT_UPDATE_ARCHIVE_DIR:-$ROOT/archive}"
PROCESSED_DIR="$ARCHIVE_DIR/processed"
FAILED_DIR="$ARCHIVE_DIR/failed"
STATE_DIR="${CUT_UPDATE_STATE_DIR:-$HOME/Library/Application Support/Cut/watch-update}"
STATE_FILE="$STATE_DIR/pending.env"
PROCESSED_FILE="$STATE_DIR/processed.sha256"
LOCK_DIR="${TMPDIR:-/tmp}/cut-watch-update.lock"
POLL_SECONDS="${CUT_UPDATE_POLL_SECONDS:-5}"
STABLE_SECONDS="${CUT_UPDATE_STABLE_SECONDS:-3}"
RETRY_SECONDS="${CUT_UPDATE_RETRY_SECONDS:-60}"
AUTO_INSTALL_DEPS="${CUT_UPDATE_AUTO_INSTALL_DEPS:-1}"
AUTO_RELEASE="${CUT_UPDATE_AUTO_RELEASE:-1}"

MODE="watch"
PROCESS_FILE=""
case "${1:-}" in
  --once) MODE="once"; shift ;;
  --status) MODE="status"; shift ;;
  --release-current) MODE="release-current"; shift ;;
  --process) MODE="process"; PROCESS_FILE="${2:-}"; shift 2 ;;
  --help|-h)
    cat <<'USAGE'
Usage:
  ./scripts/watch-update.sh
  ./scripts/watch-update.sh --once
  ./scripts/watch-update.sh --process archive/Cut-update-v0.1.1.zip
  ./scripts/watch-update.sh --release-current
  ./scripts/watch-update.sh --status

The watcher consumes versioned Cut update archives from ./archive, verifies and
applies them, commits source changes, then resumes the signed macOS release,
GitHub publication and homepage deployment pipeline.
USAGE
    exit 0
    ;;
esac
[[ $# -eq 0 ]] || { echo "Unknown argument: $*" >&2; exit 2; }

mkdir -p "$ARCHIVE_DIR" "$PROCESSED_DIR" "$FAILED_DIR" "$STATE_DIR"
touch "$PROCESSED_FILE"

if [[ -t 1 ]]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; BLUE=$'\033[34m'; MAGENTA=$'\033[35m'; DIM=$'\033[2m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; YELLOW=""; CYAN=""; BLUE=""; MAGENTA=""; DIM=""; BOLD=""; RESET=""
fi

line(){ printf '%b%s%b\n' "${DIM}" "────────────────────────────────────────────────────────────────────────" "$RESET"; }
step(){ printf '\n%b%s%b\n' "${BLUE}${BOLD}" "$*" "$RESET"; }
info(){ printf '%b==>%b %s\n' "$CYAN" "$RESET" "$*"; }
ok(){ printf '%bOK%b %s\n' "${GREEN}${BOLD}" "$RESET" "$*"; }
warn(){ printf '%bWARN:%b %s\n' "${YELLOW}${BOLD}" "$RESET" "$*" >&2; }
fail(){ printf '%bERROR:%b %s\n' "${RED}${BOLD}" "$RESET" "$*" >&2; }
event(){ printf '%bUPDATE%b %s\n' "${MAGENTA}${BOLD}" "$RESET" "$*"; }
die(){ fail "$*"; exit 1; }

cleanup_lock(){ rm -rf "$LOCK_DIR"; }
if [[ "$MODE" != "status" ]]; then
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    die "Cut update watcher is already running: $LOCK_DIR"
  fi
  trap cleanup_lock EXIT INT TERM
fi

current_version(){
  if [[ -f VERSION.txt ]]; then tr -d '[:space:]' < VERSION.txt; else printf '0.0.0'; fi
}
current_build(){
  if [[ -f BUILD_NUMBER.txt ]]; then tr -cd '0-9' < BUILD_NUMBER.txt; else printf '0'; fi
}

save_pending(){
  local archive="$1" signature="$2" version="$3" release_mode="$4" summary="$5" commit="$6"
  local tmp="${STATE_FILE}.tmp.$$"
  {
    printf 'PENDING_ARCHIVE=%q\n' "$archive"
    printf 'PENDING_SIGNATURE=%q\n' "$signature"
    printf 'PENDING_VERSION=%q\n' "$version"
    printf 'PENDING_RELEASE_MODE=%q\n' "$release_mode"
    printf 'PENDING_SUMMARY=%q\n' "$summary"
    printf 'PENDING_COMMIT=%q\n' "$commit"
    printf 'PENDING_UPDATED_AT=%q\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$tmp"
  mv -f "$tmp" "$STATE_FILE"
}

clear_pending(){ rm -f "$STATE_FILE"; }

show_status(){
  printf '%bCut update watcher%b\n' "${BOLD}" "$RESET"
  printf 'Repository: %s\n' "$ROOT"
  printf 'Version:    %s\n' "$(current_version)"
  printf 'Build:      %s\n' "$(current_build)"
  printf 'Archive:    %s\n' "$ARCHIVE_DIR"
  if [[ -f "$STATE_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$STATE_FILE"
    printf 'Pending:    %s\n' "${PENDING_VERSION:-unknown}"
    printf 'Package:    %s\n' "${PENDING_ARCHIVE:-unknown}"
    printf 'Commit:     %s\n' "${PENDING_COMMIT:-unknown}"
  else
    printf 'Pending:    none\n'
  fi
}

if [[ "$MODE" == "status" ]]; then show_status; exit 0; fi

have(){ command -v "$1" >/dev/null 2>&1; }

brew_bin(){
  if have brew; then command -v brew; return 0; fi
  if [[ -x /opt/homebrew/bin/brew ]]; then printf '/opt/homebrew/bin/brew\n'; return 0; fi
  if [[ -x /usr/local/bin/brew ]]; then printf '/usr/local/bin/brew\n'; return 0; fi
  return 1
}

ensure_formula(){
  local formula="$1" command_name="$2" brew
  have "$command_name" && return 0
  brew="$(brew_bin || true)"
  [[ -n "$brew" ]] || die "Missing $command_name and Homebrew is not installed. Install Homebrew once, then rerun the watcher."
  [[ "$AUTO_INSTALL_DEPS" == "1" ]] || die "Missing $command_name. Install with: brew install $formula"
  info "Installing missing dependency: $formula"
  "$brew" install "$formula"
}

ffmpeg_has_required_features(){
  local ffmpeg="$1"
  [[ -n "$ffmpeg" && -x "$ffmpeg" ]] || return 1
  "$ffmpeg" -hide_banner -encoders 2>/dev/null | grep -F libx264 >/dev/null || return 1
  "$ffmpeg" -hide_banner -filters 2>/dev/null | grep -E '(^|[[:space:]])(ass|subtitles)([[:space:]]|$)' >/dev/null || return 1
  return 0
}

ffprobe_next_to(){
  local ffmpeg="$1" candidate
  candidate="$(dirname "$ffmpeg")/ffprobe"
  [[ -x "$candidate" ]] && { printf '%s\n' "$candidate"; return 0; }
  candidate="$(dirname "$ffmpeg")/ffprobe-alt"
  [[ -x "$candidate" ]] && { printf '%s\n' "$candidate"; return 0; }
  return 1
}

select_full_ffmpeg(){
  local brew prefix ffmpeg ffprobe candidate
  local -a candidates=()

  if [[ -n "${FFMPEG_SOURCE:-}" ]]; then
    candidates+=("$FFMPEG_SOURCE")
  fi

  brew="$(brew_bin || true)"
  if [[ -n "$brew" ]]; then
    prefix="$($brew --prefix homebrew-ffmpeg/ffmpeg/ffmpeg 2>/dev/null || true)"
    if [[ -n "$prefix" ]]; then
      [[ -x "$prefix/bin/ffmpeg-alt" ]] && candidates+=("$prefix/bin/ffmpeg-alt")
      [[ -x "$prefix/bin/ffmpeg" ]] && candidates+=("$prefix/bin/ffmpeg")
    fi
  fi

  # Developer-machine compatibility only. The resulting application embeds the
  # selected binary and never searches these paths on an end-user Mac.
  [[ -x "$HOME/ffmpeg-full/bin/ffmpeg" ]] && candidates+=("$HOME/ffmpeg-full/bin/ffmpeg")
  candidate="$(command -v ffmpeg 2>/dev/null || true)"
  [[ -n "$candidate" ]] && candidates+=("$candidate")

  local seen='|'
  for ffmpeg in "${candidates[@]}"; do
    [[ "$seen" == *"|$ffmpeg|"* ]] && continue
    seen+="$ffmpeg|"
    ffmpeg_has_required_features "$ffmpeg" || continue

    if [[ -n "${FFPROBE_SOURCE:-}" && -x "$FFPROBE_SOURCE" ]]; then
      ffprobe="$FFPROBE_SOURCE"
    else
      ffprobe="$(ffprobe_next_to "$ffmpeg" || true)"
    fi
    [[ -n "$ffprobe" && -x "$ffprobe" ]] || continue

    export FFMPEG_SOURCE="$ffmpeg"
    export FFPROBE_SOURCE="$ffprobe"
    return 0
  done
  return 1
}

install_full_ffmpeg(){
  local brew prefix
  brew="$(brew_bin || true)"
  [[ -n "$brew" ]] || die "A full FFmpeg build is required and Homebrew is not installed."
  [[ "$AUTO_INSTALL_DEPS" == "1" ]] || die "No FFmpeg with libx264 + libass was found. Enable automatic dependency installation or provide FFMPEG_SOURCE/FFPROBE_SOURCE."

  step "Full FFmpeg runtime"
  info "The FFmpeg currently available is not suitable for Cut."
  info "Installing an isolated full FFmpeg build with libass and x264 support."

  "$brew" tap homebrew-ffmpeg/ffmpeg
  if ! "$brew" list --versions homebrew-ffmpeg/ffmpeg/ffmpeg >/dev/null 2>&1; then
    "$brew" install homebrew-ffmpeg/ffmpeg/ffmpeg --with-alt-name
  else
    prefix="$($brew --prefix homebrew-ffmpeg/ffmpeg/ffmpeg 2>/dev/null || true)"
    if [[ -z "$prefix" || ! -x "$prefix/bin/ffmpeg-alt" || ! -x "$prefix/bin/ffprobe-alt" ]]; then
      "$brew" reinstall homebrew-ffmpeg/ffmpeg/ffmpeg --with-alt-name
    fi
  fi

  select_full_ffmpeg || die "Full FFmpeg installation completed, but Cut still cannot find a build containing libx264 and ASS/subtitle filters."
}

ensure_dependencies(){
  step "Local build prerequisites"
  [[ "$(uname -s)" == "Darwin" ]] || die "The macOS release watcher must run on macOS."
  have git || die "git is required."
  have curl || die "curl is required."
  have unzip || die "unzip is required."
  have zip || die "zip is required."
  have shasum || die "shasum is required."

  if ! have xcrun; then
    warn "Xcode Command Line Tools are missing. Opening Apple's installer."
    xcode-select --install >/dev/null 2>&1 || true
    die "Finish the Xcode Command Line Tools installation, then rerun the watcher."
  fi

  ensure_formula python@3.12 python3.12
  ensure_formula gh gh
  ensure_formula rsync rsync
  ensure_formula brotli brotli
  ensure_formula node node

  BREW_FOR_PY="$(brew_bin || true)"
  BREW_PY_PREFIX=""
  if [[ -n "$BREW_FOR_PY" ]]; then
    BREW_PY_PREFIX="$($BREW_FOR_PY --prefix python@3.12 2>/dev/null || true)"
  fi
  if [[ -n "$BREW_PY_PREFIX" && -x "$BREW_PY_PREFIX/bin/python3.12" ]]; then
    export PYTHON_BIN="$BREW_PY_PREFIX/bin/python3.12"
  elif [[ -x /opt/homebrew/bin/python3.12 ]]; then
    export PYTHON_BIN=/opt/homebrew/bin/python3.12
  elif [[ -x /usr/local/bin/python3.12 ]]; then
    export PYTHON_BIN=/usr/local/bin/python3.12
  else
    export PYTHON_BIN="$(command -v python3.12)"
  fi

  PYVER="$($PYTHON_BIN -c 'import platform; print(platform.python_version())')"
  [[ "$PYVER" == 3.12.* ]] || die "Release Python must be 3.12; found $PYVER at $PYTHON_BIN"

  if ! select_full_ffmpeg; then
    install_full_ffmpeg
  fi

  FFMPEG_VERSION="$($FFMPEG_SOURCE -hide_banner -version 2>/dev/null | head -n 1 || true)"
  ok "Python $PYVER is ready"
  ok "Full FFmpeg: $FFMPEG_SOURCE"
  [[ -z "$FFMPEG_VERSION" ]] || printf '  %b%s%b\n' "$DIM" "$FFMPEG_VERSION" "$RESET"
  ok "FFprobe: $FFPROBE_SOURCE"
  ok "GitHub CLI, rsync, Brotli and Node are ready"
}

archive_signature(){ shasum -a 256 "$1" | awk '{print $1}'; }

wait_until_stable(){
  local file="$1" size1 size2 hash1 hash2
  while true; do
    size1="$(stat -f '%z' "$file" 2>/dev/null || true)"
    hash1="$(archive_signature "$file" 2>/dev/null || true)"
    sleep "$STABLE_SECONDS"
    size2="$(stat -f '%z' "$file" 2>/dev/null || true)"
    hash2="$(archive_signature "$file" 2>/dev/null || true)"
    if [[ -n "$size1" && "$size1" == "$size2" && -n "$hash1" && "$hash1" == "$hash2" ]]; then
      return 0
    fi
    info "Update archive is still being written; waiting..."
  done
}

is_processed(){ grep -Fxq "$1" "$PROCESSED_FILE" 2>/dev/null; }
mark_processed(){ printf '%s\n' "$1" >> "$PROCESSED_FILE"; }

find_eligible_archive(){
  local cv
  cv="$(current_version)"
  "$PYTHON_BIN" - "$ARCHIVE_DIR" "$cv" "$PROCESSED_FILE" <<'PY'
from pathlib import Path
import hashlib, json, re, sys, zipfile
archive_dir=Path(sys.argv[1]); current=sys.argv[2]; processed_path=Path(sys.argv[3])
processed=set(processed_path.read_text().splitlines()) if processed_path.exists() else set()

def key(v):
    nums=[int(x) for x in re.findall(r'^\d+(?:\.\d+)*', v)[0].split('.')] if re.match(r'^\d+(?:\.\d+)*', v) else [0]
    return tuple(nums+[0]*(4-len(nums)))
items=[]
for path in archive_dir.glob('Cut-update-v*.zip'):
    try:
        sig=hashlib.sha256(path.read_bytes()).hexdigest()
        if sig in processed: continue
        with zipfile.ZipFile(path) as z:
            data=json.loads(z.read('manifest.json'))
        if data.get('schema') != 1 or data.get('product') != 'Cut': continue
        base=str(data.get('base_version') or '').strip()
        version=str(data.get('version') or '').strip()
        if base and base != current: continue
        if not version: continue
        items.append((key(version), path.stat().st_mtime, str(path)))
    except Exception:
        continue
if items:
    items.sort(key=lambda x:(x[0],x[1],x[2]))
    print(items[0][2])
PY
}

manifest_fields(){
  "$PYTHON_BIN" - "$1" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1])
d=json.loads(p.read_text())
vals=[
 str(d.get('version') or '').strip(),
 str(d.get('base_version') or '').strip(),
 str(d.get('release_mode') or 'published').strip(),
 str(d.get('summary') or 'Cut update').replace('\t',' ').replace('\n',' ').strip(),
]
print('\t'.join(vals))
PY
}

validate_extracted_update(){
  local dir="$1"
  "$PYTHON_BIN" - "$dir" <<'PY'
from pathlib import Path
import hashlib, json, os, sys
root=Path(sys.argv[1]).resolve()
manifest=root/'manifest.json'; payload=root/'payload'
if not manifest.is_file() or not payload.is_dir():
    raise SystemExit('Update must contain manifest.json and payload/.')
data=json.loads(manifest.read_text())
if data.get('schema') != 1 or data.get('product') != 'Cut':
    raise SystemExit('Unsupported update manifest.')
version=str(data.get('version') or '').strip()
if not version:
    raise SystemExit('Manifest version is missing.')
vp=payload/'VERSION.txt'
if not vp.is_file() or vp.read_text().strip()!=version:
    raise SystemExit('payload/VERSION.txt must match manifest version.')
files=data.get('files') or {}
for rel, expected in files.items():
    relp=Path(rel)
    if relp.is_absolute() or '..' in relp.parts:
        raise SystemExit(f'Unsafe payload path: {rel}')
    p=payload/relp
    if not p.is_file():
        raise SystemExit(f'Manifest file missing: {rel}')
    actual=hashlib.sha256(p.read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit(f'Checksum mismatch: {rel}')
for rel in data.get('delete') or []:
    relp=Path(str(rel))
    if relp.is_absolute() or '..' in relp.parts or str(relp) in ('','.','..'):
        raise SystemExit(f'Unsafe delete path: {rel}')
print('ok')
PY
}

validate_zip_paths(){
  "$PYTHON_BIN" - "$1" <<'PY'
import pathlib, sys, zipfile
p=sys.argv[1]
with zipfile.ZipFile(p) as z:
    for info in z.infolist():
        name=info.filename
        path=pathlib.PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or name.startswith('/'):
            raise SystemExit(f'Unsafe ZIP member: {name}')
        mode=(info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise SystemExit(f'Symlinks are not allowed in update packages: {name}')
print('ok')
PY
}

update_release_notes(){
  local manifest="$1"
  "$PYTHON_BIN" - "$manifest" "$ROOT/RELEASE_NOTES.md" <<'PY'
from pathlib import Path
import json, sys
manifest=Path(sys.argv[1]); target=Path(sys.argv[2])
d=json.loads(manifest.read_text())
version=str(d.get('version') or '').strip(); notes=d.get('notes') or []
if not notes: raise SystemExit(0)
marker=f'## Update {version}'
text=target.read_text() if target.exists() else '# Cut {{VERSION}} — Build {{BUILD_NUMBER}}\n\n'
if marker in text: raise SystemExit(0)
block=marker+'\n\n'+'\n'.join(f'- {str(n).strip()}' for n in notes if str(n).strip())+'\n\n'
lines=text.splitlines(True)
insert=0
for i,line in enumerate(lines):
    if i==0 and line.startswith('# '):
        insert=1
        if len(lines)>1 and lines[1].strip()=='': insert=2
        break
new=''.join(lines[:insert])+block+''.join(lines[insert:])
target.write_text(new)
PY
}

apply_deletes(){
  local manifest="$1"
  "$PYTHON_BIN" - "$manifest" "$ROOT" <<'PY'
from pathlib import Path
import json, shutil, sys
manifest=Path(sys.argv[1]); root=Path(sys.argv[2]).resolve(); d=json.loads(manifest.read_text())
for rel in d.get('delete') or []:
    p=(root/rel).resolve()
    if root not in p.parents and p != root:
        raise SystemExit(f'Unsafe delete outside repo: {rel}')
    if p.is_dir(): shutil.rmtree(p)
    elif p.exists() or p.is_symlink(): p.unlink()
PY
}

rollback_update(){
  local before="$1" manifest="$2"
  warn "Verification failed; rolling source tree back to $before"
  git reset --hard "$before" >/dev/null
  "$PYTHON_BIN" - "$manifest" "$ROOT" "$before" <<'PY'
from pathlib import Path
import json, subprocess, sys
manifest=Path(sys.argv[1]); root=Path(sys.argv[2]); before=sys.argv[3]
d=json.loads(manifest.read_text())
for rel in (d.get('files') or {}).keys():
    tracked=subprocess.run(['git','cat-file','-e',f'{before}:{rel}'],cwd=root,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    if not tracked:
        p=root/rel
        if p.is_file() or p.is_symlink():
            try: p.unlink()
            except OSError: pass
for rel in sorted({str(Path(x).parent) for x in (d.get('files') or {})}, reverse=True):
    p=root/rel
    try: p.rmdir()
    except OSError: pass
PY
}

process_archive(){
  local archive="$1" signature tmp fields version base release_mode summary before self_hash_before self_hash_after commit
  [[ -f "$archive" ]] || { warn "Update archive disappeared: $archive"; return 1; }

  wait_until_stable "$archive"
  signature="$(archive_signature "$archive")"
  is_processed "$signature" && return 0

  event "$(basename "$archive")"
  line
  info "SHA-256: $signature"
  unzip -tqq "$archive" >/dev/null || { fail "ZIP integrity check failed"; return 1; }
  validate_zip_paths "$archive" >/dev/null || return 1

  tmp="$(mktemp -d "${TMPDIR:-/tmp}/cut-update.XXXXXX")"
  if ! unzip -q "$archive" -d "$tmp"; then rm -rf "$tmp"; return 1; fi
  rm -rf "$tmp/__MACOSX"

  validate_extracted_update "$tmp" >/dev/null || { rm -rf "$tmp"; return 1; }
  fields="$(manifest_fields "$tmp/manifest.json")"
  IFS=$'\t' read -r version base release_mode summary <<EOF_FIELDS
$fields
EOF_FIELDS

  [[ "$release_mode" == published || "$release_mode" == prerelease || "$release_mode" == draft || "$release_mode" == none ]] \
    || { rm -rf "$tmp"; die "Unsupported release_mode in manifest: $release_mode"; }

  [[ -z "$base" || "$base" == "$(current_version)" ]] || {
    warn "Update $version expects Cut $base, current version is $(current_version)."
    rm -rf "$tmp"
    return 1
  }

  step "Applying Cut $version"
  printf '  %bFrom:%b %s\n' "$DIM" "$RESET" "$archive"
  printf '  %bSummary:%b %s\n' "$DIM" "$RESET" "$summary"

  DIRTY="$(git status --porcelain --untracked-files=all || true)"
  [[ -z "$DIRTY" ]] || {
    git status --short
    rm -rf "$tmp"
    die "Repository has uncommitted source changes. Commit/stash them before the watcher applies an update."
  }

  before="$(git rev-parse HEAD)"
  self_hash_before="$(shasum -a 256 "$SCRIPT_DIR/watch-update.sh" | awk '{print $1}')"

  apply_deletes "$tmp/manifest.json"
  rsync --archive --checksum "$tmp/payload/" "$ROOT/"
  chmod +x "$ROOT"/scripts/*.sh 2>/dev/null || true
  update_release_notes "$tmp/manifest.json"

  if ! "$ROOT/scripts/verify_repo.sh"; then
    rollback_update "$before" "$tmp/manifest.json"
    mkdir -p "$FAILED_DIR"
    cp -f "$archive" "$FAILED_DIR/$(basename "$archive")"
    rm -rf "$tmp"
    return 1
  fi

  git add -A
  if git diff --cached --quiet; then
    warn "Update contains no source changes; continuing with the existing commit."
  else
    git commit -m "Update Cut to ${version}: ${summary}" >/dev/null
  fi
  commit="$(git rev-parse HEAD)"
  save_pending "$archive" "$signature" "$version" "$release_mode" "$summary" "$commit"
  ok "Source update committed: ${commit:0:12}"

  self_hash_after="$(shasum -a 256 "$SCRIPT_DIR/watch-update.sh" | awk '{print $1}')"
  rm -rf "$tmp"

  if [[ "$self_hash_before" != "$self_hash_after" ]]; then
    info "Watcher updated itself; restarting into the new script before release."
    exec "$SCRIPT_DIR/watch-update.sh"
  fi

  resume_release
}

release_args_for_mode(){
  case "$1" in
    published) printf '%s\n' '--published' ;;
    prerelease) printf '%s\n' '--prerelease' ;;
    draft) printf '%s\n' '--draft' ;;
    none) printf '%s\n' '--skip-release' ;;
  esac
}

resume_release(){
  [[ -f "$STATE_FILE" ]] || return 0
  # shellcheck disable=SC1090
  source "$STATE_FILE"

  [[ -n "${PENDING_VERSION:-}" ]] || { clear_pending; return 0; }
  if [[ "${PENDING_RELEASE_MODE:-published}" == "none" || "$AUTO_RELEASE" != "1" ]]; then
    ok "Update ${PENDING_VERSION} applied; automatic release is disabled."
    mark_processed "$PENDING_SIGNATURE"
    if [[ -f "$PENDING_ARCHIVE" ]]; then mv -f "$PENDING_ARCHIVE" "$PROCESSED_DIR/"; fi
    clear_pending
    return 0
  fi

  ensure_dependencies
  step "Signed release + GitHub + homepage"
  info "Version: $PENDING_VERSION"
  info "Source commit: $PENDING_COMMIT"

  local mode_flag state_version state_stage args=()
  case "$PENDING_RELEASE_MODE" in
    published) mode_flag="--published" ;;
    prerelease) mode_flag="--prerelease" ;;
    draft) mode_flag="--draft" ;;
    *) die "Invalid pending release mode: $PENDING_RELEASE_MODE" ;;
  esac
  args=(--version "$PENDING_VERSION" "$mode_flag")

  if [[ -f "$ROOT/release/.release-workflow-state.env" ]]; then
    state_version=""; state_stage=""
    # shellcheck disable=SC1091
    source "$ROOT/release/.release-workflow-state.env"
    state_version="${RELEASE_VERSION:-}"
    state_stage="${WORKFLOW_STAGE:-}"
    if [[ -n "$state_version" && "$state_version" != "$PENDING_VERSION" && "$state_stage" != complete ]]; then
      warn "Archiving incomplete release state for $state_version before starting $PENDING_VERSION."
      args+=(--restart)
    fi
  fi

  if PYTHON_BIN="$PYTHON_BIN" FFMPEG_SOURCE="$FFMPEG_SOURCE" FFPROBE_SOURCE="$FFPROBE_SOURCE" \
       "$SCRIPT_DIR/release_and_deploy_homepage.sh" "${args[@]}"; then
    mark_processed "$PENDING_SIGNATURE"
    if [[ -f "$PENDING_ARCHIVE" ]]; then
      mv -f "$PENDING_ARCHIVE" "$PROCESSED_DIR/$(basename "$PENDING_ARCHIVE")"
    fi
    clear_pending
    line
    ok "Cut $PENDING_VERSION update, signed release, GitHub publication and homepage deployment completed"
    line
    return 0
  fi

  warn "Release pipeline did not finish. The resumable release state is preserved."
  warn "The watcher will retry in ${RETRY_SECONDS}s without reapplying the update."
  return 1
}

banner(){
  printf '\n%bCUT UPDATE / RELEASE WATCHER%b\n' "${MAGENTA}${BOLD}" "$RESET"
  line
  printf '  Repo       %s\n' "$ROOT"
  printf '  Version    %s\n' "$(current_version)"
  printf '  Build      %s\n' "$(current_build)"
  printf '  Watching   %s/Cut-update-v*.zip\n' "$ARCHIVE_DIR"
  printf '  Poll       %ss\n' "$POLL_SECONDS"
  printf '  Retry      %ss\n' "$RETRY_SECONDS"
  line
}

banner
# Update ingestion must stay independent from heavyweight release dependencies.
# Otherwise a future update cannot repair a broken compiler/runtime prerequisite.
have git || die "git is required to apply updates."
have unzip || die "unzip is required to apply updates."
have shasum || die "shasum is required to verify updates."
have python3 || die "python3 is required to verify update manifests."
"$SCRIPT_DIR/verify_repo.sh" || die "Repository verification failed before watcher startup."

if [[ -f "$STATE_FILE" ]]; then
  warn "A previous update has a pending release; resuming it first."
  if ! resume_release; then
    [[ "$MODE" == once || "$MODE" == process || "$MODE" == release-current ]] && exit 1
  fi
fi

if [[ "$MODE" == release-current ]]; then
  version="$(current_version)"
  commit="$(git rev-parse HEAD)"
  signature="manual-release-${version}-${commit}"
  save_pending "" "$signature" "$version" "published" "Release current Cut source" "$commit"
  resume_release
  exit $?
fi

if [[ "$MODE" == process ]]; then
  [[ -n "$PROCESS_FILE" ]] || die "--process requires a ZIP path"
  [[ "$PROCESS_FILE" == /* ]] || PROCESS_FILE="$ROOT/$PROCESS_FILE"
  process_archive "$PROCESS_FILE"
  exit $?
fi

process_one(){
  local archive
  archive="$(find_eligible_archive)"
  if [[ -n "$archive" && -f "$archive" ]]; then
    process_archive "$archive"
    return $?
  fi
  return 0
}

if [[ "$MODE" == once ]]; then
  process_one
  exit $?
fi

ok "Watcher is running. Drop the next Cut-update-v*.zip into archive/."
LAST_RETRY_AT=0
while true; do
  if [[ -f "$STATE_FILE" ]]; then
    now="$(date +%s)"
    if (( now - LAST_RETRY_AT >= RETRY_SECONDS )); then
      LAST_RETRY_AT="$now"
      resume_release || true
    fi
  else
    process_one || true
  fi
  sleep "$POLL_SECONDS"
done
