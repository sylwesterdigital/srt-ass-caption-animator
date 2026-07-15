#!/usr/bin/env bash
# =============================================================================
# Cut - macOS .app / .dmg release builder (source-owned UI)
#
# Run this script from the project root containing:
#   app.py
#   templates/index.html
#   assets/
#   fonts/                    (optional but recommended)
#   tools/realesrgan/         (optional; downloaded by the app if absent)
#   srt_to_animated_ass.py    (optional)
#
# The source tree is not modified, except VERSION.txt and BUILD_NUMBER.txt.
# A staged copy is built in .macos-build/payload. Visible UI is never injected during release.
#
# Useful overrides:
#   APP_NAME="Cut"
#   BUNDLE_ID="fun.workwork.cut"
#   VERSION="0.1.0"
#   BUILD_NUMBER_OVERRIDE="12"       # exact build number for resumable workflows
#   PERSIST_BUILD_NUMBER=1            # write BUILD_NUMBER.txt only after success
#   PYTHON_BIN="/opt/homebrew/bin/python3.12"
#   FFMPEG_SOURCE="$HOME/ffmpeg-full/bin/ffmpeg"
#   FFPROBE_SOURCE="$HOME/ffmpeg-full/bin/ffprobe"
#   BUNDLE_WHISPER_MODEL="small"    # use "none" to download models on first use
#   BUNDLE_DENO=1                   # set 0 to omit Deno
#   MACOS_SIGN_IDENTITY="Developer ID Application: Company (TEAMID)"
#   NOTARY_PROFILE="notarytool-keychain-profile"
#   APP_ICON_SOURCE="assets/images/icons/logo.svg"  # PNG, SVG, JPEG, or WebP
#   APP_ICON_BACKGROUND="#0c0f14"       # Finder icon background
#   APP_PUBLISHER="WORKWORK.FUN"
#   APP_AUTHOR="Sylwester Mielniczuk"
#   COPYRIGHT_YEAR="2026"
#   APP_LOG_LEVEL="INFO"
#
# The resulting files are written to ./release/.
# =============================================================================
set -Eeuo pipefail
IFS=$'\n\t'

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

APP_NAME="${APP_NAME:-Cut}"
APP_SAFE_NAME="${APP_SAFE_NAME:-Cut}"
BUNDLE_ID="${BUNDLE_ID:-fun.workwork.cut}"
MIN_MACOS="${MIN_MACOS:-12.0}"
APP_PORT="${APP_PORT:-5151}"
BUNDLE_WHISPER_MODEL="${BUNDLE_WHISPER_MODEL:-small}"
BUNDLE_DENO="${BUNDLE_DENO:-1}"
BUNDLE_REALESRGAN="${BUNDLE_REALESRGAN:-1}"
MACOS_SIGN_IDENTITY="${MACOS_SIGN_IDENTITY:-}"
NOTARY_PROFILE="${NOTARY_PROFILE:-}"
APP_PUBLISHER="${APP_PUBLISHER:-WORKWORK.FUN}"
APP_AUTHOR="${APP_AUTHOR:-Sylwester Mielniczuk}"
COPYRIGHT_YEAR="${COPYRIGHT_YEAR:-2026}"
APP_ICON_SOURCE="${APP_ICON_SOURCE:-}"
APP_ICON_BACKGROUND="${APP_ICON_BACKGROUND:-#0c0f14}"
APP_LOG_LEVEL="${APP_LOG_LEVEL:-INFO}"

BUILD_ROOT="$PROJECT_ROOT/.macos-build"
PAYLOAD_DIR="$BUILD_ROOT/payload"
DOWNLOAD_DIR="$BUILD_ROOT/downloads"
VENV_DIR="$BUILD_ROOT/venv"
SPEC_FILE="$BUILD_ROOT/${APP_SAFE_NAME}.spec"
ENTITLEMENTS_FILE="$BUILD_ROOT/entitlements.plist"
ICON_FILE="$BUILD_ROOT/${APP_SAFE_NAME}.icns"
DIST_DIR="$BUILD_ROOT/dist"
WORK_DIR="$BUILD_ROOT/work"
RELEASE_DIR="$PROJECT_ROOT/release"

VERSION_FILE="$PROJECT_ROOT/VERSION.txt"
BUILD_NUMBER_FILE="$PROJECT_ROOT/BUILD_NUMBER.txt"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

retry_cmd() {
  local attempts="$1" delay="$2"; shift 2
  local try=1
  until "$@"; do
    local code=$?
    if [[ "$try" -ge "$attempts" ]]; then
      return "$code"
    fi
    warn "Network command failed (attempt $try/$attempts). Retrying in ${delay}s: $*"
    sleep "$delay"
    try=$((try + 1))
  done
}

cleanup_on_error() {
  local status=$?
  if [[ $status -ne 0 ]]; then
    printf '\n\033[1;31mBuild failed (exit %s).\033[0m\n' "$status" >&2
    printf 'Inspect: %s\n' "$BUILD_ROOT" >&2
  fi
}
trap cleanup_on_error EXIT

[[ "$(uname -s)" == "Darwin" ]] || die "This release must be built on macOS."
command -v xcrun >/dev/null 2>&1 || die "Install Xcode Command Line Tools: xcode-select --install"
command -v hdiutil >/dev/null 2>&1 || die "hdiutil is required."
command -v ditto >/dev/null 2>&1 || die "ditto is required."
command -v curl >/dev/null 2>&1 || die "curl is required."
command -v unzip >/dev/null 2>&1 || die "unzip is required."

[[ -f "$PROJECT_ROOT/app.py" ]] || die "Missing app.py in $PROJECT_ROOT"
[[ -f "$PROJECT_ROOT/templates/index.html" ]] || die "Missing templates/index.html"
[[ -d "$PROJECT_ROOT/assets" ]] || die "Missing assets/"

# Prefer a Homebrew Python because macOS system Python causes pywebview focus issues.
if [[ -z "${PYTHON_BIN:-}" ]]; then
  for candidate in \
    /opt/homebrew/bin/python3.12 \
    /usr/local/bin/python3.12 \
    /opt/homebrew/bin/python3.13 \
    /usr/local/bin/python3.13 \
    "$(command -v python3.12 2>/dev/null || true)" \
    "$(command -v python3.13 2>/dev/null || true)" \
    "$(command -v python3 2>/dev/null || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
[[ -n "${PYTHON_BIN:-}" && -x "$PYTHON_BIN" ]] || die "Python 3 was not found. Install Homebrew Python 3.12 or set PYTHON_BIN."

PYTHON_VERSION="$($PYTHON_BIN -c 'import platform; print(platform.python_version())')"
PYTHON_ARCH="$($PYTHON_BIN -c 'import platform; print(platform.machine())')"
case "$PYTHON_ARCH" in
  arm64|aarch64) TARGET_ARCH="arm64"; DENO_ARCH="aarch64" ;;
  x86_64|amd64) TARGET_ARCH="x86_64"; DENO_ARCH="x86_64" ;;
  *) die "Unsupported Python architecture: $PYTHON_ARCH" ;;
esac

log "Builder Python: $PYTHON_BIN ($PYTHON_VERSION, $TARGET_ARCH)"
if [[ "$PYTHON_VERSION" == 3.14* ]]; then
  warn "Python 3.14 may have fewer prebuilt ML wheels. Python 3.12 is the safest build interpreter."
fi

# Version and monotonically increasing build number.
if [[ -n "${VERSION:-}" ]]; then
  APP_VERSION="$VERSION"
elif [[ -f "$VERSION_FILE" ]]; then
  APP_VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
else
  APP_VERSION="0.1.0"
  printf '%s\n' "$APP_VERSION" > "$VERSION_FILE"
fi
[[ -n "$APP_VERSION" ]] || die "VERSION.txt is empty."

PREVIOUS_BUILD="0"
if [[ -f "$BUILD_NUMBER_FILE" ]]; then
  PREVIOUS_BUILD="$(tr -cd '0-9' < "$BUILD_NUMBER_FILE")"
  PREVIOUS_BUILD="${PREVIOUS_BUILD:-0}"
fi

PERSIST_BUILD_NUMBER="${PERSIST_BUILD_NUMBER:-1}"
if [[ -n "${BUILD_NUMBER_OVERRIDE:-}" ]]; then
  [[ "$BUILD_NUMBER_OVERRIDE" =~ ^[0-9]+$ ]] \
    || die "BUILD_NUMBER_OVERRIDE must contain digits only."
  BUILD_NUMBER="$((10#$BUILD_NUMBER_OVERRIDE))"
else
  BUILD_NUMBER="$((10#$PREVIOUS_BUILD + 1))"
fi

[[ "$BUILD_NUMBER" -gt 0 ]] || die "Build number must be greater than zero."
case "$PERSIST_BUILD_NUMBER" in 0|1) ;; *) die "PERSIST_BUILD_NUMBER must be 0 or 1." ;; esac
log "Version: $APP_VERSION (build $BUILD_NUMBER; previous persisted build $PREVIOUS_BUILD)"

rm -rf "$PAYLOAD_DIR" "$DIST_DIR" "$WORK_DIR"
mkdir -p "$PAYLOAD_DIR" "$DOWNLOAD_DIR" "$DIST_DIR" "$WORK_DIR" "$RELEASE_DIR"

log "Collecting application files"
cp "$PROJECT_ROOT/app.py" "$PAYLOAD_DIR/app.py"
cp -R "$PROJECT_ROOT/templates" "$PAYLOAD_DIR/templates"
cp -R "$PROJECT_ROOT/assets" "$PAYLOAD_DIR/assets"
[[ -f "$PROJECT_ROOT/srt_to_animated_ass.py" ]] && cp "$PROJECT_ROOT/srt_to_animated_ass.py" "$PAYLOAD_DIR/"
[[ -f "$PROJECT_ROOT/LICENSE" ]] && cp "$PROJECT_ROOT/LICENSE" "$PAYLOAD_DIR/"
[[ -f "$PROJECT_ROOT/README.md" ]] && cp "$PROJECT_ROOT/README.md" "$PAYLOAD_DIR/"
[[ -d "$PROJECT_ROOT/fonts" ]] && cp -R "$PROJECT_ROOT/fonts" "$PAYLOAD_DIR/fonts"
[[ -d "$PROJECT_ROOT/tools" ]] && cp -R "$PROJECT_ROOT/tools" "$PAYLOAD_DIR/tools"
mkdir -p "$PAYLOAD_DIR/bin" "$PAYLOAD_DIR/models" "$PAYLOAD_DIR/assets/vendor"

# Runtime version metadata consumed by /api/app-version.
printf '%s\n' "$APP_VERSION" > "$PAYLOAD_DIR/VERSION.txt"
printf '%s\n' "$BUILD_NUMBER" > "$PAYLOAD_DIR/BUILD_NUMBER.txt"

# Remove development archives and vendor demo media that are not used at runtime.
if [[ -d "$PAYLOAD_DIR/fonts" ]]; then
  find "$PAYLOAD_DIR/fonts" -type f -name '*.zip' -delete
fi
if [[ -d "$PAYLOAD_DIR/tools/realesrgan" ]]; then
  find "$PAYLOAD_DIR/tools/realesrgan" -type f \
    \( -name '*.zip' -o -name 'input.jpg' -o -name 'input2.jpg' -o -name 'onepiece_demo.mp4' \) \
    -delete
fi

# Vendor the only remote JavaScript module used by the supplied index.html.
LIL_GUI_URL="https://cdn.jsdelivr.net/npm/lil-gui@0.20/+esm"
LIL_GUI_LOCAL="$PAYLOAD_DIR/assets/vendor/lil-gui.esm.js"
log "Vendoring lil-gui frontend module"
curl --fail --location --retry 3 --silent --show-error "$LIL_GUI_URL" -o "$LIL_GUI_LOCAL"
$PYTHON_BIN - "$PAYLOAD_DIR/templates/index.html" "$APP_VERSION" "$BUILD_NUMBER" <<'PYHTML'
from pathlib import Path
import html
import sys

path = Path(sys.argv[1])
app_version = sys.argv[2]
build_number = sys.argv[3]
text = path.read_text(encoding="utf-8")

text = text.replace(
    "https://cdn.jsdelivr.net/npm/lil-gui@0.20/+esm",
    "/assets/vendor/lil-gui.esm.js",
)

safe_version = html.escape(app_version, quote=True)
safe_build = html.escape(build_number, quote=True)
text = text.replace("__APP_VERSION__", safe_version)
text = text.replace("__BUILD_NUMBER__", safe_build)
text = text.replace(
    ">dev</span>",
    f">v{safe_version} · b{safe_build}</span>",
    1,
)

path.write_text(text, encoding="utf-8")
PYHTML

find_tool() {
  local env_value="$1"
  local legacy_value="$2"
  local command_name="$3"
  if [[ -n "$env_value" && -x "$env_value" ]]; then
    printf '%s\n' "$env_value"
    return 0
  fi
  if [[ -n "$legacy_value" && -x "$legacy_value" ]]; then
    printf '%s\n' "$legacy_value"
    return 0
  fi
  command -v "$command_name" 2>/dev/null || return 1
}

FFMPEG_SOURCE="$(find_tool "${FFMPEG_SOURCE:-}" "$HOME/ffmpeg-full/bin/ffmpeg" ffmpeg || true)"
FFPROBE_SOURCE="$(find_tool "${FFPROBE_SOURCE:-}" "$HOME/ffmpeg-full/bin/ffprobe" ffprobe || true)"
[[ -n "$FFMPEG_SOURCE" ]] || die "FFmpeg was not found. Set FFMPEG_SOURCE or install a full FFmpeg build."
[[ -n "$FFPROBE_SOURCE" ]] || die "ffprobe was not found. Set FFPROBE_SOURCE."

log "Bundling FFmpeg: $FFMPEG_SOURCE"
for native_tool in "$FFMPEG_SOURCE" "$FFPROBE_SOURCE"; do
  if /usr/bin/file "$native_tool" | grep 'Mach-O' >/dev/null; then
    TOOL_ARCHS="$(/usr/bin/lipo -archs "$native_tool" 2>/dev/null || true)"
    if [[ -n "$TOOL_ARCHS" && " $TOOL_ARCHS " != *" $TARGET_ARCH "* ]]; then
      die "$native_tool does not contain the required $TARGET_ARCH architecture (found: $TOOL_ARCHS)."
    fi
  fi
done
cp "$FFMPEG_SOURCE" "$PAYLOAD_DIR/bin/ffmpeg"
cp "$FFPROBE_SOURCE" "$PAYLOAD_DIR/bin/ffprobe"
chmod 755 "$PAYLOAD_DIR/bin/ffmpeg" "$PAYLOAD_DIR/bin/ffprobe"

if ! "$PAYLOAD_DIR/bin/ffmpeg" -hide_banner -filters 2>/dev/null | grep -E '(^|[[:space:]])ass([[:space:]]|$)' >/dev/null; then
  die "The selected FFmpeg does not include the libass 'ass' filter required for burned-in captions."
fi
if ! "$PAYLOAD_DIR/bin/ffmpeg" -hide_banner -encoders 2>/dev/null | grep 'libx264' >/dev/null; then
  die "The selected FFmpeg does not include libx264, which the app uses for video export."
fi

log "Bundling standalone yt-dlp"
YTDLP_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
curl --fail --location --retry 3 --silent --show-error "$YTDLP_URL" -o "$PAYLOAD_DIR/bin/yt-dlp"
chmod 755 "$PAYLOAD_DIR/bin/yt-dlp"
"$PAYLOAD_DIR/bin/yt-dlp" --version >/dev/null || die "Downloaded yt-dlp could not run on this Mac."

if [[ "$BUNDLE_DENO" == "1" ]]; then
  log "Bundling Deno JavaScript runtime for current yt-dlp YouTube support"
  DENO_ZIP="$DOWNLOAD_DIR/deno-${DENO_ARCH}-apple-darwin.zip"
  DENO_URL="https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}-apple-darwin.zip"
  curl --fail --location --retry 3 --silent --show-error "$DENO_URL" -o "$DENO_ZIP"
  rm -f "$PAYLOAD_DIR/bin/deno"
  unzip -jo "$DENO_ZIP" deno -d "$PAYLOAD_DIR/bin" >/dev/null
  chmod 755 "$PAYLOAD_DIR/bin/deno"
  "$PAYLOAD_DIR/bin/deno" --version >/dev/null || die "Downloaded Deno could not run."
fi

if [[ "$BUNDLE_REALESRGAN" == "1" ]]; then
  REALESRGAN_ROOT="$PAYLOAD_DIR/tools/realesrgan"
  REALESRGAN_HELPER="$(find "$REALESRGAN_ROOT" -type f -name 'realesrgan-ncnn-vulkan' -print -quit 2>/dev/null || true)"
  REALESRGAN_MODEL_PARAM="$(find "$REALESRGAN_ROOT" -type f -name 'realesrgan-x4plus.param' -print -quit 2>/dev/null || true)"
  REALESRGAN_MODEL_BIN="$(find "$REALESRGAN_ROOT" -type f -name 'realesrgan-x4plus.bin' -print -quit 2>/dev/null || true)"

  if [[ -z "$REALESRGAN_HELPER" || -z "$REALESRGAN_MODEL_PARAM" || -z "$REALESRGAN_MODEL_BIN" ]]; then
    log "Bundling complete Real-ESRGAN runtime"
    REALESRGAN_ZIP="$DOWNLOAD_DIR/realesrgan-ncnn-vulkan-20220424-macos.zip"
    REALESRGAN_URL="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip"
    curl --fail --location --retry 3 --silent --show-error "$REALESRGAN_URL" -o "$REALESRGAN_ZIP"
    rm -rf "$REALESRGAN_ROOT"
    mkdir -p "$REALESRGAN_ROOT"
    unzip -q "$REALESRGAN_ZIP" -d "$REALESRGAN_ROOT"
    REALESRGAN_HELPER="$(find "$REALESRGAN_ROOT" -type f -name 'realesrgan-ncnn-vulkan' -print -quit)"
    REALESRGAN_MODEL_PARAM="$(find "$REALESRGAN_ROOT" -type f -name 'realesrgan-x4plus.param' -print -quit)"
    REALESRGAN_MODEL_BIN="$(find "$REALESRGAN_ROOT" -type f -name 'realesrgan-x4plus.bin' -print -quit)"
  fi

  [[ -n "$REALESRGAN_HELPER" ]] || die "Real-ESRGAN executable is missing after bundling."
  [[ -n "$REALESRGAN_MODEL_PARAM" && -n "$REALESRGAN_MODEL_BIN" ]] || die "Real-ESRGAN model files are missing after bundling."
  chmod 755 "$REALESRGAN_HELPER"
  REALESRGAN_ARCHS="$(/usr/bin/lipo -archs "$REALESRGAN_HELPER" 2>/dev/null || true)"
  if [[ "$TARGET_ARCH" == "arm64" && -n "$REALESRGAN_ARCHS" && " $REALESRGAN_ARCHS " != *" arm64 "* ]]; then
    warn "Bundled Real-ESRGAN is $REALESRGAN_ARCHS; Apple-silicon users will need Rosetta 2 for AI upscaling."
  fi
fi

# Create/reuse an isolated build environment.
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  log "Creating build virtual environment"
  rm -rf "$VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

log "Installing packaging and runtime dependencies"
"$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel
"$VENV_PIP" install --upgrade \
  "pyinstaller>=6.21,<7" \
  "pyinstaller-hooks-contrib" \
  "Flask>=3.1,<4" \
  "Werkzeug>=3.1,<4" \
  "pysubs2>=1.8,<2" \
  "fonttools>=4.59" \
  "Pillow>=11" \
  "CairoSVG>=2.7" \
  "numpy>=1.26" \
  "faster-whisper>=1.2" \
  "pywebview>=6"

# Pre-download the default Faster-Whisper model. Other model choices still download
# on first use into the user's Application Support cache.
if [[ -n "$BUNDLE_WHISPER_MODEL" && "$BUNDLE_WHISPER_MODEL" != "none" ]]; then
  log "Embedding Faster-Whisper model: $BUNDLE_WHISPER_MODEL"
  MODEL_DIR="$PAYLOAD_DIR/models/faster-whisper-$BUNDLE_WHISPER_MODEL"
  rm -rf "$MODEL_DIR"
  "$VENV_PYTHON" - "$BUNDLE_WHISPER_MODEL" "$MODEL_DIR" <<'PY'
from huggingface_hub import snapshot_download
from pathlib import Path
import sys
model = sys.argv[1]
out = Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
snapshot_download(
    repo_id=f"Systran/faster-whisper-{model}",
    local_dir=str(out),
    local_dir_use_symlinks=False,
)
PY
fi

log "Patching staged Flask backend for an installed macOS app"
"$VENV_PYTHON" - "$PAYLOAD_DIR/app.py" "$APP_NAME" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
app_name = sys.argv[2]
text = path.read_text(encoding="utf-8")

if "import sys\n" not in text:
    text = text.replace("import os\n", "import os\nimport sys\n", 1)
if "import logging\n" not in text:
    text = text.replace("import time\n", "import time\nimport logging\n", 1)

old_root = 'APP_ROOT = os.path.dirname(os.path.abspath(__file__))'
new_root = f'''RESOURCE_ROOT = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_ROOT = RESOURCE_ROOT
DATA_ROOT = os.environ.get("CAPTION_ANIMATOR_DATA_DIR") or os.path.join(
    os.path.expanduser("~/Library/Application Support"),
    {app_name!r},
)'''
if old_root not in text:
    raise SystemExit("Could not patch APP_ROOT; app.py layout has changed.")
text = text.replace(old_root, new_root, 1)

replacements = {
    'UPLOAD_DIR = os.path.join(APP_ROOT, "uploads")': 'UPLOAD_DIR = os.path.join(DATA_ROOT, "uploads")',
    'OUTPUT_DIR = os.path.join(APP_ROOT, "outputs")': 'OUTPUT_DIR = os.path.join(DATA_ROOT, "outputs")',
    'TOOLS_DIR = os.path.join(APP_ROOT, "tools")': 'TOOLS_DIR = os.path.join(DATA_ROOT, "tools")',
    'APP_STATE_PATH = os.path.join(APP_ROOT, "app_state.json")': 'APP_STATE_PATH = os.path.join(DATA_ROOT, "app_state.json")',
    'FONTS_DIR = os.path.join(APP_ROOT, "fonts")': 'FONTS_DIR = os.path.join(DATA_ROOT, "fonts")',
    'app = Flask(__name__, template_folder="templates")': 'app = Flask(__name__, template_folder=os.path.join(RESOURCE_ROOT, "templates"))',
    'app.config["TEMPLATES_AUTO_RELOAD"] = True': 'app.config["TEMPLATES_AUTO_RELOAD"] = False',
    'tempfile.mkdtemp(prefix="libass_fonts_", dir=APP_ROOT)': 'tempfile.mkdtemp(prefix="libass_fonts_", dir=DATA_ROOT)',
    'ytdlp_bin = shutil.which("yt-dlp") or shutil.which("yt_dlp")': 'ytdlp_bin = _resolve_packaged_tool("yt-dlp") or shutil.which("yt_dlp")',
    'app.run(host="127.0.0.1", port=5151, debug=True, threaded=False, use_reloader=True)': 'app.run(host="127.0.0.1", port=5151, debug=False, threaded=True, use_reloader=False)',
}
for old, new in replacements.items():
    if old not in text:
        print(f"warning: patch target not found: {old}", file=sys.stderr)
    text = text.replace(old, new, 1)

old_ffmpeg = '''FFMPEG_BIN = os.path.expanduser("~/ffmpeg-full/bin/ffmpeg")
FFPROBE_BIN = os.path.expanduser("~/ffmpeg-full/bin/ffprobe")'''
new_ffmpeg = '''PACKAGED_BIN_DIR = os.path.join(RESOURCE_ROOT, "bin")


def _resolve_packaged_tool(name, legacy_path=None):
    candidates = [
        os.path.join(PACKAGED_BIN_DIR, name),
        legacy_path,
        shutil.which(name),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            try:
                current_mode = os.stat(candidate).st_mode
                os.chmod(candidate, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except Exception:
                pass
            if os.access(candidate, os.X_OK):
                return candidate
    return None


FFMPEG_BIN = _resolve_packaged_tool("ffmpeg", os.path.expanduser("~/ffmpeg-full/bin/ffmpeg"))
FFPROBE_BIN = _resolve_packaged_tool("ffprobe", os.path.expanduser("~/ffmpeg-full/bin/ffprobe"))'''
if old_ffmpeg not in text:
    raise SystemExit("Could not patch FFmpeg paths; app.py layout has changed.")
text = text.replace(old_ffmpeg, new_ffmpeg, 1)

# Keep compatibility with older source trees. Current app.py owns these routes.
if "def api_app_logs()" not in text:
    log_marker = 'app.config["TEMPLATES_AUTO_RELOAD"] = False\n'
    log_injection = r'''APP_LOG_PATH = os.environ.get(
    "CAPTION_ANIMATOR_LOG_FILE",
    os.path.join(os.path.expanduser("~/Library/Logs"), __APP_NAME__, "app.log"),
)
APP_LOGGER = logging.getLogger("caption_animator")


@app.before_request
def _caption_animator_log_request_start():
    request.environ["caption_animator.request_started"] = time.monotonic()


@app.after_request
def _caption_animator_log_request_end(response):
    started = request.environ.get("caption_animator.request_started")
    elapsed_ms = ((time.monotonic() - started) * 1000.0) if started else 0.0
    APP_LOGGER.info(
        "%s %s -> %s (%.1f ms)",
        request.method,
        request.full_path.rstrip("?"),
        response.status_code,
        elapsed_ms,
    )
    return response


@app.route("/api/app_logs")
def api_app_logs():
    try:
        log_path = os.path.abspath(APP_LOG_PATH)
        if not os.path.isfile(log_path):
            return jsonify({"ok": True, "path": log_path, "lines": []})
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - (512 * 1024)), os.SEEK_SET)
            text_value = handle.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "path": log_path,
            "lines": text_value.splitlines()[-500:],
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/reveal_app_logs", methods=["POST"])
def api_reveal_app_logs():
    try:
        log_path = os.path.abspath(APP_LOG_PATH)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        if not os.path.exists(log_path):
            open(log_path, "a", encoding="utf-8").close()
        if platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", log_path])
        return jsonify({"ok": True, "path": log_path})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


'''.replace("__APP_NAME__", repr(app_name))
    if log_marker not in text:
        raise SystemExit("Could not inject persistent logging routes.")
    text = text.replace(log_marker, log_marker + log_injection, 1)

# Retain more detailed per-job command output in the UI.
text = text.replace('"message": text[-600:]', '"message": text[-2000:]')
text = text.replace('del logs[:-80]', 'del logs[:-300]')
text = text.replace('"logs": job.get("logs", [])[-30:]', '"logs": job.get("logs", [])[-120:]')
text = text.replace(
    '"Started: " + " ".join(str(part) for part in cmd[:6]) + (" ..." if len(cmd) > 6 else "")',
    '"Started: " + " ".join(str(part) for part in cmd[:24]) + (" ..." if len(cmd) > 24 else "")',
)

# Prefer MP4 merging and normalize incompatible WebM/AV1/Opus downloads to H.264/AAC MP4.
text = text.replace(
    '        if "--ffmpeg-location" in help_text and FFMPEG_BIN and os.path.exists(FFMPEG_BIN):\n            ytdlp_shared_args += ["--ffmpeg-location", os.path.dirname(FFMPEG_BIN)]',
    '        if "--ffmpeg-location" in help_text and FFMPEG_BIN and os.path.exists(FFMPEG_BIN):\n            ytdlp_shared_args += ["--ffmpeg-location", os.path.dirname(FFMPEG_BIN)]\n\n        if "--merge-output-format" in help_text:\n            ytdlp_shared_args += ["--merge-output-format", "mp4"]',
)

social_marker = 'def download_social_video_job(job_id, url, settings):\n'
social_helper = r'''def _ensure_social_media_browser_compatible(media_path, job_id=None):
    # Return an H.264/AAC MP4 suitable for the macOS WebKit preview.
    probe_cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-show_entries", "stream=codec_type,codec_name",
        "-of", "json",
        media_path,
    ]
    video_codec = ""
    audio_codec = ""
    try:
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
        if probe_result.returncode == 0:
            payload = json.loads(probe_result.stdout or "{}")
            for stream in payload.get("streams") or []:
                if stream.get("codec_type") == "video" and not video_codec:
                    video_codec = str(stream.get("codec_name") or "").lower()
                elif stream.get("codec_type") == "audio" and not audio_codec:
                    audio_codec = str(stream.get("codec_name") or "").lower()
    except Exception as exc:
        _append_job_log(job_id, f"Compatibility probe warning: {exc}")

    extension = os.path.splitext(media_path)[1].lower()
    compatible_video = video_codec == "h264"
    compatible_audio = not audio_codec or audio_codec == "aac"
    if extension in (".mp4", ".m4v", ".mov") and compatible_video and compatible_audio:
        _append_job_log(job_id, f"Downloaded media is browser compatible: {video_codec}/{audio_codec or 'no-audio'}")
        return media_path

    output_path = os.path.splitext(media_path)[0] + "_browser.mp4"
    _set_job_progress(
        job_id,
        status="rendering",
        message="Converting downloaded video to browser-compatible MP4...",
        phase="social_import",
    )
    _append_job_log(
        job_id,
        f"Normalizing {extension or 'media'} ({video_codec or 'unknown'}/{audio_codec or 'no-audio'}) to H.264/AAC MP4.",
    )
    command = [
        FFMPEG_BIN, "-y",
        "-i", media_path,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_job_subprocess(
        command,
        job_id=job_id,
        failure_message="Downloaded video compatibility conversion failed",
    )
    if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
        raise RuntimeError("Browser-compatible MP4 conversion did not produce an output file.")
    try:
        if os.path.abspath(media_path) != os.path.abspath(output_path):
            os.remove(media_path)
    except OSError:
        pass
    return output_path


'''
if social_marker not in text:
    raise SystemExit("Could not locate social download job for compatibility patch.")
text = text.replace(social_marker, social_helper + social_marker, 1)
text = text.replace(
    '        media_path = _find_social_download_media_file(work_dir)\n        media_filename = os.path.basename(media_path)',
    '        media_path = _find_social_download_media_file(work_dir)\n        media_path = _ensure_social_media_browser_compatible(media_path, job_id=job_id)\n        media_filename = os.path.basename(media_path)',
    1,
)

# Prefer bundled Real-ESRGAN, while keeping the existing per-user downloader fallback.
needle = 'def _resolve_realesrgan_backend(job_id=None):\n'
injection = '''def _resolve_realesrgan_backend(job_id=None):
    bundled_root = os.path.join(RESOURCE_ROOT, "tools", "realesrgan")
    bundled_binary = _find_realesrgan_binary(bundled_root)
    bundled_models = _find_realesrgan_models(bundled_root)
    if bundled_binary and bundled_models:
        return {
            "binary": bundled_binary,
            "model_dir": bundled_models,
        }
'''
if needle not in text:
    raise SystemExit("Could not patch Real-ESRGAN resolver.")
text = text.replace(needle, injection, 1)

# Resolve any embedded Faster-Whisper model before allowing the library to fetch it.
needle = '        model = WhisperModel(model_name, compute_type="auto")'
injection = '''        bundled_model_path = os.path.join(
            RESOURCE_ROOT,
            "models",
            f"faster-whisper-{model_name}",
        )
        if os.path.isdir(bundled_model_path):
            model_name = bundled_model_path

        model = WhisperModel(model_name, compute_type="auto")'''
if needle not in text:
    raise SystemExit("Could not patch Faster-Whisper model lookup.")
text = text.replace(needle, injection, 1)

path.write_text(text, encoding="utf-8")
PY

log "Generating native desktop launcher"
"$VENV_PYTHON" - "$PAYLOAD_DIR/desktop_launcher.py" "$APP_NAME" "$APP_PORT" "$BUNDLE_ID" <<'PYLAUNCH'
from pathlib import Path
import sys

output = Path(sys.argv[1])
app_name = sys.argv[2]
app_port = int(sys.argv[3])
app_bundle_id = sys.argv[4]
template = r'''from __future__ import annotations

import fcntl
import logging
import multiprocessing
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

APP_NAME = __APP_NAME__
APP_BUNDLE_ID = __APP_BUNDLE_ID__
DEFAULT_APP_PORT = __APP_PORT__
_INSTANCE_LOCK_HANDLE = None


def configured_app_port() -> int:
    raw_value = (
        os.environ.get("CUT_PORT")
        or os.environ.get("CAPTION_ANIMATOR_PORT")
        or str(DEFAULT_APP_PORT)
    )
    try:
        port = int(raw_value)
    except (TypeError, ValueError):
        return int(DEFAULT_APP_PORT)

    if 0 <= port <= 65535:
        return port
    return int(DEFAULT_APP_PORT)


APP_PORT = configured_app_port()


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def data_root() -> Path:
    override = (
        os.environ.get("CUT_DATA_DIR")
        or os.environ.get("CAPTION_ANIMATOR_DATA_DIR")
    )
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / APP_NAME


def local_port_is_available(port: int) -> bool:
    if port == 0:
        return True

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.25)
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def create_local_server(make_server, flask_app, preferred_port: int, logger):
    selected_port = preferred_port

    if selected_port != 0 and not local_port_is_available(selected_port):
        logger.warning(
            "Requested port %s is already in use; selecting a free local port.",
            selected_port,
        )
        selected_port = 0

    try:
        server = make_server(
            "127.0.0.1",
            selected_port,
            flask_app,
            threaded=True,
        )
    except SystemExit as exc:
        # Werkzeug can raise SystemExit when a port becomes occupied between
        # the availability probe and server creation. Retry atomically with
        # port 0 so the operating system assigns an available loopback port.
        if selected_port == 0:
            raise RuntimeError(
                "Could not allocate a local application port."
            ) from exc

        logger.warning(
            "Requested port %s became unavailable; retrying with a free port.",
            selected_port,
        )
        server = make_server(
            "127.0.0.1",
            0,
            flask_app,
            threaded=True,
        )

    actual_port = int(
        getattr(server, "server_port", 0)
        or getattr(server, "port", 0)
        or selected_port
    )
    if actual_port <= 0:
        server.server_close()
        raise RuntimeError("Local server did not report its bound port.")

    return server, actual_port


def wait_for_local_server(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                ("127.0.0.1", port),
                timeout=0.25,
            ):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def activate_existing_instance(logger) -> None:
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'tell application id "{APP_BUNDLE_ID}" to activate',
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except Exception:
        logger.exception("Could not activate the existing Cut instance")


def acquire_single_instance(data: Path, logger) -> bool:
    global _INSTANCE_LOCK_HANDLE

    lock_path = data / ".desktop-instance.lock"
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning(
            "Another %s desktop instance is already running; activating it instead of opening a second window.",
            APP_NAME,
        )
        handle.close()
        activate_existing_instance(logger)
        return False

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _INSTANCE_LOCK_HANDLE = handle
    logger.info("Single-instance lock acquired: %s", lock_path)
    return True


class StreamToLogger:
    def __init__(self, logger: logging.Logger, level: int) -> None:
        self.logger = logger
        self.level = level
        self.buffer = ""

    def write(self, value: str) -> int:
        self.buffer += str(value or "")
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if line.strip():
                self.logger.log(self.level, line.rstrip())
        return len(value or "")

    def flush(self) -> None:
        if self.buffer.strip():
            self.logger.log(self.level, self.buffer.rstrip())
        self.buffer = ""


def configure_logging(data: Path) -> Path:
    log_dir = Path.home() / "Library" / "Logs" / APP_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"
    level_name = os.environ.get("CAPTION_ANIMATOR_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root_logger.addHandler(handler)
    sys.stdout = StreamToLogger(logging.getLogger("stdout"), logging.INFO)
    sys.stderr = StreamToLogger(logging.getLogger("stderr"), logging.ERROR)
    os.environ["CAPTION_ANIMATOR_LOG_FILE"] = str(log_path)
    os.environ["CAPTION_ANIMATOR_LOG_LEVEL"] = level_name
    logging.getLogger("caption_animator").info("Logging started: %s", log_path)
    logging.getLogger("caption_animator").info("Application data: %s", data)
    logging.getLogger("caption_animator").info(
        "Bundled fonts synchronized: copied=%s updated=%s",
        os.environ.get("CAPTION_ANIMATOR_BUNDLED_FONTS_COPIED", "0"),
        os.environ.get("CAPTION_ANIMATOR_BUNDLED_FONTS_UPDATED", "0"),
    )
    return log_path


def prepare_runtime() -> tuple[Path, Path]:
    resources = resource_root()
    data = data_root()
    for folder in (
        data,
        data / "uploads",
        data / "outputs",
        data / "fonts",
        data / "tools",
        data / "logs",
        data / "cache",
        data / "webview",
        data / "models",
    ):
        folder.mkdir(parents=True, exist_ok=True)

    bundled_fonts = resources / "fonts"
    user_fonts = data / "fonts"
    copied_font_count = 0
    updated_font_count = 0

    if bundled_fonts.is_dir():
        for source in bundled_fonts.rglob("*"):
            if not source.is_file():
                continue

            relative = source.relative_to(bundled_fonts)
            target = user_fonts / relative
            target.parent.mkdir(parents=True, exist_ok=True)

            managed_ui_font = (
                bool(relative.parts)
                and relative.parts[0].casefold() == "ui"
            )
            should_copy = not target.exists()

            if not should_copy and managed_ui_font:
                try:
                    should_copy = (
                        source.stat().st_size
                        != target.stat().st_size
                        or source.read_bytes()
                        != target.read_bytes()
                    )
                except OSError:
                    should_copy = True

            if should_copy:
                existed = target.exists()
                shutil.copy2(source, target)
                if existed:
                    updated_font_count += 1
                else:
                    copied_font_count += 1

    os.environ["CAPTION_ANIMATOR_BUNDLED_FONTS_COPIED"] = str(
        copied_font_count
    )
    os.environ["CAPTION_ANIMATOR_BUNDLED_FONTS_UPDATED"] = str(
        updated_font_count
    )

    bundled_bin = resources / "bin"
    path_entries = [str(bundled_bin), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    existing = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys([p for p in path_entries + existing if p]))
    os.environ["CUT_DATA_DIR"] = str(data)
    os.environ["CAPTION_ANIMATOR_DATA_DIR"] = str(data)
    os.environ.setdefault("CUT_APP_NAME", APP_NAME)
    os.environ.setdefault("CAPTION_ANIMATOR_APP_NAME", APP_NAME)
    os.environ.setdefault("HF_HOME", str(data / "models" / "huggingface"))
    os.environ.setdefault("XDG_CACHE_HOME", str(data / "cache"))
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.chdir(data)
    return resources, data


def write_fatal(data: Path, message: str) -> None:
    log_path = data / "logs" / "fatal.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(message, encoding="utf-8")
    try:
        import subprocess
        escaped = message.replace("\\", "\\\\").replace('"', '\\"')[:1600]
        subprocess.run(
            ["osascript", "-e", f'display alert "{APP_NAME}" message "{escaped}" as critical'],
            check=False,
        )
    except Exception:
        pass


def main() -> int:
    resources, data = prepare_runtime()
    log_path = configure_logging(data)
    logger = logging.getLogger("caption_animator")
    try:
        logger.info("Starting %s", APP_NAME)
        logger.info("Resources: %s", resources)
        logger.info("Python: %s", sys.version.replace("\n", " "))
        from app import FFMPEG_BIN, FFPROBE_BIN, app, ensure_dirs

        ensure_dirs()
        if "--smoke-test" in sys.argv:
            assert FFMPEG_BIN and Path(FFMPEG_BIN).is_file(), "Bundled FFmpeg is missing"
            assert FFPROBE_BIN and Path(FFPROBE_BIN).is_file(), "Bundled ffprobe is missing"
            assert (resources / "templates" / "index.html").is_file(), "Template is missing"
            bundled_ui_fonts = resources / "fonts" / "ui"
            if bundled_ui_fonts.is_dir():
                assert any(
                    path.is_file()
                    and path.suffix.lower()
                    in {".ttf", ".otf", ".woff", ".woff2"}
                    for path in bundled_ui_fonts.rglob("*")
                ), "fonts/ui exists but contains no supported font files"
            return 0

        if not acquire_single_instance(data, logger):
            return 0

        from werkzeug.serving import make_server
        import webview

        server, server_port = create_local_server(
            make_server,
            app,
            APP_PORT,
            logger,
        )
        server_url = f"http://127.0.0.1:{server_port}/"
        os.environ["CUT_PORT_ACTUAL"] = str(server_port)
        os.environ["CAPTION_ANIMATOR_PORT_ACTUAL"] = str(server_port)

        server_thread = threading.Thread(
            target=server.serve_forever,
            name="flask-server",
            daemon=True,
        )
        server_thread.start()

        if not wait_for_local_server(server_port):
            try:
                server.shutdown()
            finally:
                server.server_close()
            raise RuntimeError(
                f"Local application server did not start on port {server_port}."
            )

        if server_port == APP_PORT:
            logger.info("Local server listening at %s", server_url)
        else:
            logger.warning(
                "Port %s was unavailable; local server is using port %s.",
                APP_PORT,
                server_port,
            )

        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        window = webview.create_window(
            APP_NAME,
            server_url,
            width=1440,
            height=920,
            min_size=(980, 680),
            resizable=True,
            background_color="#0c0f14",
            text_select=True,
            zoomable=True,
        )

        shutdown_started = threading.Event()

        def stop_application(*_args) -> None:
            if shutdown_started.is_set():
                return
            shutdown_started.set()
            logger.info("Main window closed; shutting down local server and application process.")

            def stop_server() -> None:
                try:
                    server.shutdown()
                    server.server_close()
                except Exception:
                    logger.exception("Local server shutdown failed")

            threading.Thread(target=stop_server, name="flask-shutdown", daemon=True).start()

            # Cocoa applications normally remain alive after their final window closes.
            # End this single-window desktop app explicitly after WebKit has had time to flush storage.
            def force_exit() -> None:
                time.sleep(1.0)
                os._exit(0)

            threading.Thread(target=force_exit, name="desktop-exit", daemon=True).start()

        window.events.closed += stop_application
        webview.start(
            debug=os.environ.get("CAPTION_ANIMATOR_DEBUG") == "1",
            private_mode=False,
            storage_path=str(data / "webview"),
        )
        stop_application()
        return 0
    except Exception:
        message = traceback.format_exc()
        logging.getLogger("caption_animator").exception("Fatal application error")
        write_fatal(data, message + f"\n\nLog file: {log_path}")
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
'''
template = (
    template.replace("__APP_NAME__", repr(app_name))
    .replace("__APP_BUNDLE_ID__", repr(app_bundle_id))
    .replace("__APP_PORT__", repr(app_port))
)
output.write_text(template, encoding="utf-8")
PYLAUNCH
log "Generating macOS icon"
"$VENV_PYTHON" - "$BUILD_ROOT" "$APP_SAFE_NAME" "$APP_ICON_SOURCE" "$APP_ICON_BACKGROUND" <<'PYICON'
from pathlib import Path
from PIL import Image, ImageColor, ImageDraw, ImageOps
import subprocess
import sys

root = Path(sys.argv[1])
name = sys.argv[2]
source_value = sys.argv[3].strip()
background_value = sys.argv[4].strip() or "#0c0f14"
iconset = root / f"{name}.iconset"

try:
    parsed_background = ImageColor.getrgb(background_value)
except Exception:
    parsed_background = (12, 15, 20)

if len(parsed_background) == 3:
    background_rgba = (*parsed_background, 255)
else:
    background_rgba = parsed_background


def make_icon_background():
    result = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    draw = ImageDraw.Draw(result)
    draw.rounded_rectangle(
        (60, 60, 964, 964),
        radius=210,
        fill=background_rgba,
        outline=(255, 255, 255, 24),
        width=2,
    )
    return result
if iconset.exists():
    import shutil
    shutil.rmtree(iconset)
iconset.mkdir(parents=True, exist_ok=True)

canvas = None
if source_value:
    source = Path(source_value).expanduser()
    if not source.is_absolute():
        source = (Path.cwd() / source).resolve()
    if not source.is_file():
        raise SystemExit(f"APP_ICON_SOURCE does not exist: {source}")
    if source.suffix.lower() == ".svg":
        import cairosvg
        rendered = root / f"{name}-source.png"
        cairosvg.svg2png(url=str(source), write_to=str(rendered), output_width=1024, output_height=1024)
        image = Image.open(rendered).convert("RGBA")
    else:
        image = Image.open(source).convert("RGBA")
    image = ImageOps.contain(image, (760, 760), Image.Resampling.LANCZOS)
    canvas = make_icon_background()
    canvas.alpha_composite(
        image,
        (
            (1024 - image.width) // 2,
            (1024 - image.height) // 2,
        ),
    )

if canvas is None:
    canvas = Image.new("RGBA", (1024, 1024), background_rgba)
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((70, 70, 954, 954), radius=190, fill=(12, 15, 20, 255), outline=(125, 211, 252, 255), width=42)
    d.rounded_rectangle((210, 245, 814, 779), radius=48, outline=(255, 255, 255, 245), width=34)
    d.polygon([(430, 380), (430, 644), (655, 512)], fill=(125, 211, 252, 255))
    d.rounded_rectangle((230, 820, 794, 862), radius=20, fill=(125, 211, 252, 220))

for size in (16, 32, 128, 256, 512):
    canvas.resize((size, size), Image.Resampling.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
    canvas.resize((size * 2, size * 2), Image.Resampling.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(root / f"{name}.icns")], check=True)
PYICON

cat > "$ENTITLEMENTS_FILE" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
</dict>
</plist>
PLIST

log "Generating PyInstaller spec"
cat > "$SPEC_FILE" <<'PY'
from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

BUILD_ROOT = Path(SPECPATH)
PAYLOAD = BUILD_ROOT / "payload"
APP_NAME = os.environ["BUILD_APP_NAME"]
BUNDLE_ID = os.environ["BUILD_BUNDLE_ID"]
APP_VERSION = os.environ["BUILD_APP_VERSION"]
BUILD_NUMBER = os.environ["BUILD_NUMBER"]
TARGET_ARCH = os.environ["BUILD_TARGET_ARCH"]
MIN_MACOS = os.environ["BUILD_MIN_MACOS"]
APP_PUBLISHER = os.environ["BUILD_APP_PUBLISHER"]
APP_AUTHOR = os.environ["BUILD_APP_AUTHOR"]
COPYRIGHT_YEAR = os.environ["BUILD_COPYRIGHT_YEAR"]
SIGN_IDENTITY = os.environ.get("MACOS_SIGN_IDENTITY") or None
ENTITLEMENTS = str(BUILD_ROOT / "entitlements.plist")
ICON = str(BUILD_ROOT / f"{os.environ['BUILD_APP_SAFE_NAME']}.icns")


def add_tree(source: Path, destination: str, *, executable_names=()):
    data_entries = []
    binary_entries = []
    if not source.is_dir():
        return data_entries, binary_entries
    for item in source.rglob("*"):
        if not item.is_file():
            continue
        relative_parent = item.relative_to(source).parent
        dest = str(Path(destination) / relative_parent)
        if item.name in executable_names:
            binary_entries.append((str(item), dest))
        else:
            data_entries.append((str(item), dest))
    return data_entries, binary_entries


datas = [
    (str(PAYLOAD / "templates"), "templates"),
    (str(PAYLOAD / "assets"), "assets"),
]
binaries = [
    (str(PAYLOAD / "bin" / "ffmpeg"), "bin"),
    (str(PAYLOAD / "bin" / "ffprobe"), "bin"),
    (str(PAYLOAD / "bin" / "yt-dlp"), "bin"),
]
if (PAYLOAD / "bin" / "deno").exists():
    binaries.append((str(PAYLOAD / "bin" / "deno"), "bin"))
if (PAYLOAD / "fonts").is_dir():
    datas.append((str(PAYLOAD / "fonts"), "fonts"))
if (PAYLOAD / "models").is_dir():
    datas.append((str(PAYLOAD / "models"), "models"))
if (PAYLOAD / "srt_to_animated_ass.py").is_file():
    datas.append((str(PAYLOAD / "srt_to_animated_ass.py"), "."))

# Keep the third-party Real-ESRGAN package byte-for-byte. Its macOS release can
# be a different architecture from the main app and is launched as a subprocess.
extra_datas, _ = add_tree(PAYLOAD / "tools", "tools")
datas += extra_datas

hiddenimports = []
for package in (
    "webview",
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "huggingface_hub",
    "av",
    "PIL",
    "fontTools",
    "pysubs2",
):
    try:
        pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(package)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hiddenimports
    except Exception as exc:
        print(f"collect_all({package!r}) warning: {exc}")

# Cocoa imports are selected dynamically by pywebview on macOS.
hiddenimports += collect_submodules("webview.platforms")
hiddenimports = sorted(set(hiddenimports))

analysis = Analysis(
    [str(PAYLOAD / "desktop_launcher.py")],
    pathex=[str(PAYLOAD)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch",
        "tensorflow",
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "gi",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=TARGET_ARCH,
    codesign_identity=SIGN_IDENTITY,
    entitlements_file=ENTITLEMENTS,
)

collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

app = BUNDLE(
    collection,
    name=f"{APP_NAME}.app",
    icon=ICON,
    bundle_identifier=BUNDLE_ID,
    info_plist={
        "CFBundleDisplayName": APP_NAME,
        "CFBundleName": APP_NAME,
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": BUILD_NUMBER,
        "CFBundleGetInfoString": f"{APP_NAME} {APP_VERSION} — {APP_PUBLISHER}",
        "NSHumanReadableCopyright": f"© {COPYRIGHT_YEAR} {APP_PUBLISHER}. Created by {APP_AUTHOR}.",
        "LSMinimumSystemVersion": MIN_MACOS,
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {
            "NSAllowsLocalNetworking": True,
        },
        "LSApplicationCategoryType": "public.app-category.video",
    },
)
PY

export BUILD_APP_NAME="$APP_NAME"
export BUILD_APP_SAFE_NAME="$APP_SAFE_NAME"
export BUILD_BUNDLE_ID="$BUNDLE_ID"
export BUILD_APP_VERSION="$APP_VERSION"
export BUILD_NUMBER="$BUILD_NUMBER"
export BUILD_TARGET_ARCH="$TARGET_ARCH"
export BUILD_MIN_MACOS="$MIN_MACOS"
export BUILD_APP_PUBLISHER="$APP_PUBLISHER"
export BUILD_APP_AUTHOR="$APP_AUTHOR"
export BUILD_COPYRIGHT_YEAR="$COPYRIGHT_YEAR"
export CAPTION_ANIMATOR_LOG_LEVEL="$APP_LOG_LEVEL"
export MACOS_SIGN_IDENTITY

log "Building ${APP_NAME}.app with PyInstaller"
"$VENV_DIR/bin/pyinstaller" \
  --noconfirm \
  --clean \
  --distpath "$DIST_DIR" \
  --workpath "$WORK_DIR" \
  "$SPEC_FILE"

DIST_APP="$DIST_DIR/${APP_NAME}.app"
[[ -d "$DIST_APP" ]] || die "PyInstaller did not produce $DIST_APP"

# Real-ESRGAN is copied as a resource so PyInstaller does not reject an Intel
# helper in an Apple-silicon app. Restore its execute bit, sign nested Mach-O
# helpers, then re-sign the outer bundle.
log "Finalizing bundled native helper signatures"
while IFS= read -r helper; do
  case "$(basename "$helper")" in
    realesrgan-ncnn-vulkan|realesrgan-ncnn-vulkan.exe) chmod 755 "$helper" ;;
  esac
  if /usr/bin/file "$helper" | grep 'Mach-O' >/dev/null; then
    if [[ -n "$MACOS_SIGN_IDENTITY" ]]; then
      /usr/bin/codesign --force --options runtime --timestamp --sign "$MACOS_SIGN_IDENTITY" "$helper"
    else
      /usr/bin/codesign --force --sign - "$helper"
    fi
  fi
done < <(find "$DIST_APP" -type f -path '*/tools/*' -print)

if [[ -n "$MACOS_SIGN_IDENTITY" ]]; then
  /usr/bin/codesign --force --deep --options runtime --timestamp \
    --entitlements "$ENTITLEMENTS_FILE" \
    --sign "$MACOS_SIGN_IDENTITY" "$DIST_APP"
else
  /usr/bin/codesign --force --deep --sign - "$DIST_APP"
fi

log "Validating app bundle"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$DIST_APP"
/usr/sbin/spctl --assess --type execute --verbose=2 "$DIST_APP" || {
  if [[ -z "$MACOS_SIGN_IDENTITY" ]]; then
    warn "Gatekeeper assessment failed because this is only ad-hoc signed. That is expected for a tester build."
  elif [[ -z "$NOTARY_PROFILE" ]]; then
    warn "The app is Developer ID-signed but not notarized, so Gatekeeper may reject downloads."
  else
    warn "Gatekeeper assessment will be repeated after notarization."
  fi
}

log "Running packaged smoke test"
"$DIST_APP/Contents/MacOS/$APP_NAME" --smoke-test

# Notarize and staple the app itself first, so both the final ZIP and DMG contain
# an independently verifiable application bundle.
if [[ -n "$NOTARY_PROFILE" ]]; then
  [[ -n "$MACOS_SIGN_IDENTITY" ]] || die "NOTARY_PROFILE requires MACOS_SIGN_IDENTITY."
  NOTARY_APP_ZIP="$BUILD_ROOT/${APP_SAFE_NAME}-notary.zip"
  rm -f "$NOTARY_APP_ZIP"
  ditto -c -k --sequesterRsrc --keepParent "$DIST_APP" "$NOTARY_APP_ZIP"
  log "Submitting app bundle to Apple notarization service"
  retry_cmd 3 12 xcrun notarytool submit "$NOTARY_APP_ZIP" --keychain-profile "$NOTARY_PROFILE" --wait
  retry_cmd 3 8 xcrun stapler staple "$DIST_APP"
  retry_cmd 3 5 xcrun stapler validate "$DIST_APP"
  /usr/sbin/spctl --assess --type execute --verbose=2 "$DIST_APP"
fi

ARCH_LABEL="$TARGET_ARCH"
ZIP_PATH="$RELEASE_DIR/${APP_SAFE_NAME}-v${APP_VERSION}-b${BUILD_NUMBER}-macOS-${ARCH_LABEL}.zip"
DMG_PATH="$RELEASE_DIR/${APP_SAFE_NAME}-v${APP_VERSION}-b${BUILD_NUMBER}-macOS-${ARCH_LABEL}.dmg"
rm -f "$ZIP_PATH" "$DMG_PATH"

log "Creating release zip"
ditto -c -k --sequesterRsrc --keepParent "$DIST_APP" "$ZIP_PATH"

log "Creating drag-to-Applications DMG"
DMG_STAGE="$(mktemp -d)"
ditto "$DIST_APP" "$DMG_STAGE/${APP_NAME}.app"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$DMG_STAGE" \
  -ov \
  -format UDZO \
  "$DMG_PATH" >/dev/null
rm -rf "$DMG_STAGE"

if [[ -n "$MACOS_SIGN_IDENTITY" ]]; then
  /usr/bin/codesign --force --timestamp --sign "$MACOS_SIGN_IDENTITY" "$DMG_PATH"
fi

if [[ -n "$NOTARY_PROFILE" ]]; then
  log "Submitting DMG to Apple notarization service"
  retry_cmd 3 12 xcrun notarytool submit "$DMG_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
  log "Stapling notarization ticket to DMG"
  retry_cmd 3 8 xcrun stapler staple "$DMG_PATH"
  retry_cmd 3 5 xcrun stapler validate "$DMG_PATH"
fi

log "Writing SHA-256 checksums"
(
  cd "$RELEASE_DIR"
  shasum -a 256 "$(basename "$ZIP_PATH")" "$(basename "$DMG_PATH")" > "${APP_SAFE_NAME}-v${APP_VERSION}-b${BUILD_NUMBER}-SHA256.txt"
)

if [[ "$PERSIST_BUILD_NUMBER" == "1" ]]; then
  BUILD_NUMBER_TMP="${BUILD_NUMBER_FILE}.tmp.$$"
  printf '%s\n' "$BUILD_NUMBER" > "$BUILD_NUMBER_TMP"
  mv -f "$BUILD_NUMBER_TMP" "$BUILD_NUMBER_FILE"
  log "Persisted successful build number: $BUILD_NUMBER"
else
  log "Build number persistence disabled; BUILD_NUMBER.txt was not changed"
fi

printf '\n\033[1;32mRelease complete.\033[0m\n'
printf 'App:  %s\n' "$DIST_APP"
printf 'DMG:  %s\n' "$DMG_PATH"
printf 'ZIP:  %s\n' "$ZIP_PATH"
printf 'Data: ~/Library/Application Support/%s\n' "$APP_NAME"
if [[ -z "$MACOS_SIGN_IDENTITY" ]]; then
  printf '\nThis build is ad-hoc signed for testing. For public distribution, set:\n'
  printf '  MACOS_SIGN_IDENTITY="Developer ID Application: ..."\n'
  printf '  NOTARY_PROFILE="your-notarytool-profile"\n'
fi
