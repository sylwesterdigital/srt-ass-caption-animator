#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

if [[ -t 1 ]]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; YELLOW=""; CYAN=""; BOLD=""; RESET=""
fi
info(){ printf '%b==>%b %s\n' "$CYAN" "$RESET" "$*"; }
ok(){ printf '%bOK%b %s\n' "${GREEN}${BOLD}" "$RESET" "$*"; }
die(){ printf '%bERROR:%b %s\n' "${RED}${BOLD}" "$RESET" "$*" >&2; exit 1; }

[[ -f app.py ]] || die "app.py is missing"
[[ -f VERSION.txt ]] || die "VERSION.txt is missing"
[[ -d templates ]] || die "templates/ is missing"
[[ -d assets ]] || die "assets/ is missing"
[[ -x scripts/build_macos_release_v3.sh ]] || die "scripts/build_macos_release_v3.sh is missing or not executable"
[[ -x scripts/release_and_deploy_homepage.sh ]] || die "scripts/release_and_deploy_homepage.sh is missing or not executable"

VERSION_VALUE="$(tr -d '[:space:]' < VERSION.txt)"
[[ "$VERSION_VALUE" =~ ^[0-9]+([.][0-9]+){1,3}([_-][0-9A-Za-z.-]+)?$ ]] || die "Invalid VERSION.txt: $VERSION_VALUE"

info "Python syntax"
PYTHON_CHECK="${PYTHON_CHECK:-$(command -v python3 || true)}"
[[ -n "$PYTHON_CHECK" ]] || die "python3 is required for verification"
"$PYTHON_CHECK" -m py_compile app.py
[[ ! -f srt_to_animated_ass.py ]] || "$PYTHON_CHECK" -m py_compile srt_to_animated_ass.py
ok "Python source compiles"

info "Shell syntax"
while IFS= read -r -d '' script; do
  bash -n "$script"
done < <(find scripts -maxdepth 1 -type f -name '*.sh' -print0 | sort -z)
ok "Shell scripts parse"

info "Repository layout"
ROOT_SHELLS="$(find . -maxdepth 1 -type f -name '*.sh' -print | sed 's#^./##' || true)"
[[ -z "$ROOT_SHELLS" ]] || die "Shell scripts must live under scripts/: $ROOT_SHELLS"
ok "Shell tooling is isolated under scripts/"

info "Deterministic media runtime"
grep -F 'def _yt_dlp_command()' app.py >/dev/null || die "app.py does not own the yt-dlp command"
if grep -F 'shutil.which("yt-dlp")' app.py >/dev/null || grep -F 'shutil.which("yt_dlp")' app.py >/dev/null; then
  die "app.py still searches the user PATH for yt-dlp"
fi
if grep -F 'yt-dlp_macos' scripts/build_macos_release_v3.sh >/dev/null; then
  die "builder still downloads the upstream yt-dlp_macos executable"
fi
grep -F 'yt-dlp[default]' scripts/requirements-macos.txt >/dev/null || die "yt-dlp[default] is not bundled"
grep -F '"yt_dlp_ejs"' scripts/build_macos_release_v3.sh >/dev/null || die "yt_dlp_ejs is not collected by PyInstaller"
grep -F 'f"deno:{deno_bin}"' app.py >/dev/null || die "app.py does not pin yt-dlp to the bundled Deno runtime"
if grep -F 'ensure_formula ffmpeg ffmpeg' scripts/watch-update.sh >/dev/null; then
  die "watcher still assumes the Homebrew core FFmpeg is suitable"
fi
grep -F 'homebrew-ffmpeg/ffmpeg/ffmpeg' scripts/watch-update.sh >/dev/null || die "watcher cannot provision a full FFmpeg build"
grep -F 'if ! select_full_ffmpeg; then' scripts/watch-update.sh >/dev/null || die "watcher does not validate FFmpeg capabilities"
ok "yt-dlp, EJS, Deno and FFmpeg are app-owned in packaged builds"

info "Release entry points"
for script in \
  scripts/watch-update.sh \
  scripts/release_signed.sh \
  scripts/release_and_deploy_homepage.sh \
  scripts/publish_github_release.sh \
  scripts/deploy_homepage.sh; do
  [[ -x "$script" ]] || die "$script is missing or not executable"
done
ok "Release/update entry points are ready"

printf '\n%bRepository verification passed%b — Cut %s\n' "${GREEN}${BOLD}" "$RESET" "$VERSION_VALUE"
