#!/usr/bin/env bash
# =============================================================================
# Caption Animator - macOS .app / .dmg release builder
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
# A patched copy is built in .macos-build/payload.
#
# Useful overrides:
#   APP_NAME="Caption Animator"
#   BUNDLE_ID="com.example.captionanimator"
#   VERSION="0.1.0"
#   PYTHON_BIN="/opt/homebrew/bin/python3.12"
#   FFMPEG_SOURCE="$HOME/ffmpeg-full/bin/ffmpeg"
#   FFPROBE_SOURCE="$HOME/ffmpeg-full/bin/ffprobe"
#   BUNDLE_WHISPER_MODEL="small"    # use "none" to download models on first use
#   BUNDLE_DENO=1                   # set 0 to omit Deno
#   MACOS_SIGN_IDENTITY="Developer ID Application: Company (TEAMID)"
#   NOTARY_PROFILE="notarytool-keychain-profile"
#
# The resulting files are written to ./release/.
# =============================================================================
set -Eeuo pipefail
IFS=$'\n\t'

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

APP_NAME="${APP_NAME:-Caption Animator}"
APP_SAFE_NAME="${APP_SAFE_NAME:-CaptionAnimator}"
BUNDLE_ID="${BUNDLE_ID:-com.example.captionanimator}"
MIN_MACOS="${MIN_MACOS:-12.0}"
APP_PORT="${APP_PORT:-5151}"
BUNDLE_WHISPER_MODEL="${BUNDLE_WHISPER_MODEL:-small}"
BUNDLE_DENO="${BUNDLE_DENO:-1}"
MACOS_SIGN_IDENTITY="${MACOS_SIGN_IDENTITY:-}"
NOTARY_PROFILE="${NOTARY_PROFILE:-}"

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
BUILD_NUMBER="$((10#$PREVIOUS_BUILD + 1))"
printf '%s\n' "$BUILD_NUMBER" > "$BUILD_NUMBER_FILE"
log "Version: $APP_VERSION (build $BUILD_NUMBER)"

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
$PYTHON_BIN - "$PAYLOAD_DIR/templates/index.html" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = text.replace(
    "https://cdn.jsdelivr.net/npm/lil-gui@0.20/+esm",
    "/assets/vendor/lil-gui.esm.js",
)
path.write_text(text, encoding="utf-8")
PY

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

if [[ -d "$PAYLOAD_DIR/tools/realesrgan" ]]; then
  REALESRGAN_HELPER="$(find "$PAYLOAD_DIR/tools/realesrgan" -type f -name 'realesrgan-ncnn-vulkan' -print -quit)"
  if [[ -n "$REALESRGAN_HELPER" ]]; then
    chmod 755 "$REALESRGAN_HELPER"
    REALESRGAN_ARCHS="$(/usr/bin/lipo -archs "$REALESRGAN_HELPER" 2>/dev/null || true)"
    if [[ "$TARGET_ARCH" == "arm64" && -n "$REALESRGAN_ARCHS" && " $REALESRGAN_ARCHS " != *" arm64 "* ]]; then
      warn "Bundled Real-ESRGAN is $REALESRGAN_ARCHS; Apple-silicon users will need Rosetta 2 for AI upscaling."
    fi
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
"$VENV_PYTHON" - "$PAYLOAD_DIR/desktop_launcher.py" "$APP_NAME" "$APP_PORT" <<'PYLAUNCH'
from pathlib import Path
import sys

output = Path(sys.argv[1])
app_name = sys.argv[2]
app_port = int(sys.argv[3])
template = r'''from __future__ import annotations

import os
import shutil
import sys
import threading
import traceback
from pathlib import Path

APP_NAME = __APP_NAME__
APP_PORT = int(os.environ.get("CAPTION_ANIMATOR_PORT", __APP_PORT__))


def resource_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def data_root() -> Path:
    override = os.environ.get("CAPTION_ANIMATOR_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / APP_NAME


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
    if bundled_fonts.is_dir():
        for source in bundled_fonts.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(bundled_fonts)
            target = user_fonts / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)

    bundled_bin = resources / "bin"
    path_entries = [str(bundled_bin), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    existing = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys([p for p in path_entries + existing if p]))
    os.environ["CAPTION_ANIMATOR_DATA_DIR"] = str(data)
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
    try:
        from app import FFMPEG_BIN, FFPROBE_BIN, app, ensure_dirs

        ensure_dirs()
        if "--smoke-test" in sys.argv:
            assert FFMPEG_BIN and Path(FFMPEG_BIN).is_file(), "Bundled FFmpeg is missing"
            assert FFPROBE_BIN and Path(FFPROBE_BIN).is_file(), "Bundled ffprobe is missing"
            assert (resources / "templates" / "index.html").is_file(), "Template is missing"
            return 0

        from werkzeug.serving import make_server
        import webview

        server = make_server("127.0.0.1", APP_PORT, app, threaded=True)
        server_thread = threading.Thread(target=server.serve_forever, name="flask-server", daemon=True)
        server_thread.start()

        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.create_window(
            APP_NAME,
            f"http://127.0.0.1:{APP_PORT}/",
            width=1440,
            height=920,
            min_size=(980, 680),
            resizable=True,
            background_color="#0c0f14",
            text_select=True,
            zoomable=True,
        )
        webview.start(
            debug=os.environ.get("CAPTION_ANIMATOR_DEBUG") == "1",
            private_mode=False,
            storage_path=str(data / "webview"),
        )
        server.shutdown()
        return 0
    except Exception:
        message = traceback.format_exc()
        write_fatal(data, message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''
template = template.replace("__APP_NAME__", repr(app_name)).replace("__APP_PORT__", repr(app_port))
output.write_text(template, encoding="utf-8")
PYLAUNCH
log "Generating macOS icon"
"$VENV_PYTHON" - "$BUILD_ROOT" "$APP_SAFE_NAME" <<'PY'
from pathlib import Path
from PIL import Image, ImageDraw
import subprocess
import sys

root = Path(sys.argv[1])
name = sys.argv[2]
iconset = root / f"{name}.iconset"
iconset.mkdir(parents=True, exist_ok=True)
canvas = Image.new("RGBA", (1024, 1024), (12, 15, 20, 255))
d = ImageDraw.Draw(canvas)
d.rounded_rectangle((70, 70, 954, 954), radius=190, fill=(12, 15, 20, 255), outline=(125, 211, 252, 255), width=42)
d.rounded_rectangle((210, 245, 814, 779), radius=48, outline=(255, 255, 255, 245), width=34)
d.polygon([(430, 380), (430, 644), (655, 512)], fill=(125, 211, 252, 255))
d.rounded_rectangle((230, 820, 794, 862), radius=20, fill=(125, 211, 252, 220))
for size in (16, 32, 128, 256, 512):
    canvas.resize((size, size), Image.Resampling.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
    canvas.resize((size * 2, size * 2), Image.Resampling.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(root / f"{name}.icns")], check=True)
PY

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
  xcrun notarytool submit "$NOTARY_APP_ZIP" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DIST_APP"
  xcrun stapler validate "$DIST_APP"
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
  xcrun notarytool submit "$DMG_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
  log "Stapling notarization ticket to DMG"
  xcrun stapler staple "$DMG_PATH"
  xcrun stapler validate "$DMG_PATH"
fi

log "Writing SHA-256 checksums"
(
  cd "$RELEASE_DIR"
  shasum -a 256 "$(basename "$ZIP_PATH")" "$(basename "$DMG_PATH")" > "${APP_SAFE_NAME}-v${APP_VERSION}-b${BUILD_NUMBER}-SHA256.txt"
)

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
