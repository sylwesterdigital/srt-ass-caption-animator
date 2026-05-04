# Flask imports used by the app, API routes, and font file serving.
from flask import Flask, request, send_from_directory, jsonify, render_template, url_for

from werkzeug.utils import secure_filename
import os
import re
import html
import uuid
import subprocess
import threading
import json
import tempfile
import shutil
import stat
import urllib.error
import urllib.request
import zipfile
import time


import platform
import pysubs2
from fontTools.ttLib import TTFont

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(APP_ROOT, "uploads")
OUTPUT_DIR = os.path.join(APP_ROOT, "outputs")
TOOLS_DIR = os.path.join(APP_ROOT, "tools")
REALESRGAN_DIR = os.path.join(TOOLS_DIR, "realesrgan")
REALESRGAN_RELEASE = "v0.2.5.0"
REALESRGAN_BOOTSTRAP_LOCK = threading.Lock()
REALESRGAN_RELEASE_ASSETS = {
    "Darwin": {
        "filename": "realesrgan-ncnn-vulkan-20220424-macos.zip",
        "url": f"https://github.com/xinntao/Real-ESRGAN/releases/download/{REALESRGAN_RELEASE}/realesrgan-ncnn-vulkan-20220424-macos.zip",
    },
    "Linux": {
        "filename": "realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
        "url": f"https://github.com/xinntao/Real-ESRGAN/releases/download/{REALESRGAN_RELEASE}/realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
    },
    "Windows": {
        "filename": "realesrgan-ncnn-vulkan-20220424-windows.zip",
        "url": f"https://github.com/xinntao/Real-ESRGAN/releases/download/{REALESRGAN_RELEASE}/realesrgan-ncnn-vulkan-20220424-windows.zip",
    },
}


# Store reusable application state outside transient request objects.
APP_STATE_PATH = os.path.join(APP_ROOT, "app_state.json")

FFMPEG_BIN = os.path.expanduser("~/ffmpeg-full/bin/ffmpeg")
FFPROBE_BIN = os.path.expanduser("~/ffmpeg-full/bin/ffprobe")

app = Flask(__name__, template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024
# Enable Flask template auto-reload during development so HTML changes are picked up without restarting.
app.config["TEMPLATES_AUTO_RELOAD"] = True

JOBS = {}
# Manage uploaded custom fonts for browser preview and FFmpeg/libass rendering.
FONTS_DIR = os.path.join(APP_ROOT, "fonts")
ALLOWED_FONT_EXTENSIONS = {".ttf", ".otf", ".woff", ".woff2"}


# Ensure all runtime directories exist, including the custom font store.
def ensure_dirs():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(FONTS_DIR, exist_ok=True)
    os.makedirs(TOOLS_DIR, exist_ok=True)
    os.makedirs(REALESRGAN_DIR, exist_ok=True)


# Validate uploaded font file extensions before saving or serving them.
def _allowed_font_file(filename):
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in ALLOWED_FONT_EXTENSIONS


# Derive the font family label from the uploaded file name.
def _font_family_from_filename(filename):
    font_path = os.path.join(FONTS_DIR, secure_filename(filename or ""))

    def _read_name(record):
        try:
            value = record.toUnicode().strip()
        except Exception:
            return None
        return value or None

    try:
        font = TTFont(font_path)
        name_table = font["name"]

        names_by_id = {}
        for record in name_table.names:
            value = _read_name(record)
            if not value:
                continue
            names_by_id.setdefault(record.nameID, [])
            if value not in names_by_id[record.nameID]:
                names_by_id[record.nameID].append(value)

        # Best match for libass / real face selection:
        # 4 = Full font name
        if names_by_id.get(4):
            return names_by_id[4][0]

        # 16 + 17 = Typographic family + subfamily
        if names_by_id.get(16) and names_by_id.get(17):
            return f"{names_by_id[16][0]} {names_by_id[17][0]}".strip()

        # 1 + 2 = Legacy family + subfamily
        if names_by_id.get(1) and names_by_id.get(2):
            subfamily = names_by_id[2][0]
            if subfamily.lower() == "regular":
                return names_by_id[1][0]
            return f"{names_by_id[1][0]} {subfamily}".strip()

        # 6 = PostScript name
        if names_by_id.get(6):
            return names_by_id[6][0]

        if names_by_id.get(16):
            return names_by_id[16][0]

        if names_by_id.get(1):
            return names_by_id[1][0]

    except Exception:
        pass

    base = os.path.splitext(os.path.basename(filename or ""))[0]
    family = re.sub(r"[_\-]+", " ", base)
    family = re.sub(r"\s+", " ", family).strip()
    return family or "Custom Font"

# Escape filesystem paths for safe use inside ffmpeg filter strings.
def _escape_ffmpeg_filter_path(path_value):
    value = os.path.abspath(path_value or "")
    value = value.replace("\\", "/")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    value = value.replace(",", r"\,")
    value = value.replace("[", r"\[")
    value = value.replace("]", r"\]")
    return value



def _list_system_fonts():
    candidates = []

    if platform.system() == "Darwin":
        candidates = [
            "/System/Library/Fonts",
            "/Library/Fonts",
            os.path.expanduser("~/Library/Fonts"),
        ]
    elif platform.system() == "Windows":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        candidates = [
            os.path.join(windir, "Fonts"),
        ]
    else:
        candidates = [
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts"),
        ]

    seen = set()
    fonts = []

    for root_dir in candidates:
        if not os.path.isdir(root_dir):
            continue

        for root, _, files in os.walk(root_dir):
            for filename in files:
                if not _allowed_font_file(filename):
                    continue

                full_path = os.path.join(root, filename)

                try:
                    family = None
                    font = TTFont(full_path)
                    name_table = font["name"]

                    for preferred_name_id in (4, 16, 1, 6):
                        for record in name_table.names:
                            if record.nameID != preferred_name_id:
                                continue
                            try:
                                value = record.toUnicode().strip()
                            except Exception:
                                continue
                            if value:
                                family = value
                                break
                        if family:
                            break

                    if not family:
                        family = _font_family_from_filename(filename)

                    key = family.lower()
                    if key in seen:
                        continue

                    seen.add(key)
                    fonts.append({
                        "family": family,
                        "source": "system",
                    })
                except Exception:
                    continue

    fonts.sort(key=lambda item: item["family"].lower())
    return fonts

# List uploaded custom fonts for the frontend Typography controls.
def _list_custom_fonts():
    ensure_dirs()
    fonts = []

    for filename in sorted(os.listdir(FONTS_DIR), key=lambda value: value.lower()):
        safe_name = secure_filename(filename)
        if not safe_name or not _allowed_font_file(safe_name):
            continue

        fonts.append({
            "file": safe_name,
            "family": _font_family_from_filename(safe_name),
            "url": f"/fonts/{safe_name}",
        })

    return fonts


# Render a preview or full output while exposing uploaded fonts to libass.
def render_preview(video_path, ass_path, output_path, preview_start=0, preview_seconds=8):
    # Render a preview or full output while exposing uploaded fonts to libass.
    # Force AV1 inputs through an explicit software decoder and render with a clean libass font directory.
    import shutil

    source_duration = get_video_duration(video_path)

    requested_start = max(0.0, float(preview_start or 0.0))
    if requested_start >= source_duration:
        requested_start = 0.0

    requested_end = source_duration
    if preview_seconds is not None:
        requested_length = max(0.1, float(preview_seconds))
        requested_end = min(source_duration, requested_start + requested_length)

    has_audio = False
    audio_probe_cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=index",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    audio_probe_result = subprocess.run(audio_probe_cmd, capture_output=True, text=True)
    if audio_probe_result.returncode == 0 and (audio_probe_result.stdout or "").strip():
        has_audio = True

    video_codec_name = ""
    codec_probe_cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    codec_probe_result = subprocess.run(codec_probe_cmd, capture_output=True, text=True)
    if codec_probe_result.returncode == 0:
        video_codec_name = (codec_probe_result.stdout or "").strip().lower()

    input_decoder_args = []
    if video_codec_name == "av1":
        decoder_list_cmd = [FFMPEG_BIN, "-hide_banner", "-decoders"]
        decoder_list_result = subprocess.run(decoder_list_cmd, capture_output=True, text=True)
        decoder_listing = "\n".join([
            decoder_list_result.stdout or "",
            decoder_list_result.stderr or "",
        ])

        if re.search(r"^\s*V\S*\s+libdav1d\b", decoder_listing, flags=re.MULTILINE):
            input_decoder_args = [
                "-c:v", "libdav1d",
                "-threads:v", "1",
            ]
        else:
            input_decoder_args = [
                "-c:v", "av1",
                "-threads:v", "1",
            ]

    temp_fonts_dir = tempfile.mkdtemp(prefix="libass_fonts_", dir=APP_ROOT)

    try:
        # Vulnerable block: copy only validated font files into the temporary libass font directory.
        for filename in os.listdir(FONTS_DIR):
            safe_name = secure_filename(filename)
            if not safe_name or not _allowed_font_file(safe_name):
                continue

            source_font_path = os.path.join(FONTS_DIR, safe_name)
            if not os.path.isfile(source_font_path):
                continue

            shutil.copy2(source_font_path, os.path.join(temp_fonts_dir, safe_name))

        ass_filter = (
            f"ass='{_escape_ffmpeg_filter_path(ass_path)}'"
            f":fontsdir='{_escape_ffmpeg_filter_path(temp_fonts_dir)}'"
        )

        if preview_seconds is not None:
            video_chain = (
                f"[0:v]trim=start={requested_start:.6f}:end={requested_end:.6f},"
                f"setpts=PTS-STARTPTS,{ass_filter}[v]"
            )
        else:
            video_chain = f"[0:v]setpts=PTS-STARTPTS,{ass_filter}[v]"

        filter_parts = [video_chain]

        if has_audio:
            if preview_seconds is not None:
                filter_parts.append(
                    f"[0:a]atrim=start={requested_start:.6f}:end={requested_end:.6f},asetpts=PTS-STARTPTS[a]"
                )
            else:
                filter_parts.append("[0:a]asetpts=PTS-STARTPTS[a]")

        filter_complex = ";".join(filter_parts)

        # Vulnerable block: media paths and generated filter graphs are passed to ffmpeg as tokenized arguments.
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-nostdin",
            *input_decoder_args,
            "-i", video_path,
            "-filter_complex", filter_complex,
            "-map", "[v]",
        ]

        if has_audio:
            cmd += ["-map", "[a]"]

        cmd += [
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "medium",
            "-pix_fmt", "yuv420p",
        ]

        if has_audio:
            cmd += [
                "-c:a", "aac",
                "-b:a", "192k",
            ]

        cmd += [
            "-movflags", "+faststart",
            output_path,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "FFmpeg render failed")

    finally:
        try:
            shutil.rmtree(temp_fonts_dir, ignore_errors=True)
        except Exception:
            pass




def _hex_to_ass_bgr(value, alpha_hex="00"):
    value = (value or "#FFFFFF").strip()
    if value.startswith("#"):
        value = value[1:]
    if len(value) != 6:
        value = "FFFFFF"
    rr = value[0:2]
    gg = value[2:4]
    bb = value[4:6]
    return f"&H{alpha_hex}{bb}{gg}{rr}"

# Parse a comma-separated palette into normalized ASS colors.
# Falls back to the fixed color when the palette field is empty.
def _parse_palette_colours(palette_text, fallback_colour):
    colours = []

    for item in re.split(r"[,;\n]+", str(palette_text or "")):
        token = item.strip()
        if not token:
            continue

        if token.startswith("#"):
            token = token[1:]
            if re.fullmatch(r"[0-9A-Fa-f]{6}", token):
                rr = token[0:2]
                gg = token[2:4]
                bb = token[4:6]
                colours.append(f"&H00{bb}{gg}{rr}".upper())
            elif re.fullmatch(r"[0-9A-Fa-f]{8}", token):
                colours.append(f"&H{token}".upper())
            continue

        if token.upper().startswith("&H"):
            token = token[2:]
            if re.fullmatch(r"[0-9A-Fa-f]{8}", token):
                colours.append(f"&H{token}".upper())

    return colours or [str(fallback_colour or "&H00FFFFFF").upper()]    


# Pick one deterministic color from fixed, palette-step, or random-palette mode.
# The same cue, index, and seed always return the same color.
def _pick_variant_colour(mode, fixed_colour, palette_text, cue_start_ms, item_index, seed, salt=0):
    palette = _parse_palette_colours(palette_text, fixed_colour)
    base = abs(int(cue_start_ms or 0)) + abs(int(item_index or 0)) * 131 + abs(int(seed or 0)) * 17 + abs(int(salt or 0)) * 53

    if mode == "palette":
        return palette[base % len(palette)]

    if mode == "random":
        return palette[(base * 37 + 11) % len(palette)]

    return str(fixed_colour or palette[0]).upper()


# Pick one deterministic signed jitter offset for a cue/word.
# Preview and final render stay aligned because the value is seed-based.
def _pick_variant_offset(enabled, max_amount, cue_start_ms, item_index, seed, salt):
    limit = max(0, int(max_amount or 0))
    if not enabled or limit == 0:
        return 0

    base = abs(int(cue_start_ms or 0)) + abs(int(item_index or 0)) * 131 + abs(int(seed or 0)) * 17 + abs(int(salt or 0)) * 53
    return ((base * 73 + 19) % (limit * 2 + 1)) - limit



def _safe_int(value, default_value):
    try:
        return int(float(value))
    except Exception:
        return default_value


def _safe_float(value, default_value):
    try:
        return float(value)
    except Exception:
        return default_value


def _safe_bool(value, default_value=False):
    if value is None:
        return default_value
    return str(value).lower() in ("1", "true", "yes", "on")


def _escape_ass_text(text):
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N").strip()


# Apply optional all-caps transform before ASS text is emitted.
# Keeps one backend path for phrase and word-only rendering.
def _apply_text_case(text, settings):
    value = str(text or "")
    return value.upper() if settings.get("all_caps") else value



# Convert ASS BGR strings into pysubs2 color objects for reusable overlay styling.
def _ass_bgr_to_pysubs2_color(value, default="&H00FFFFFF"):
    value = (value or default).strip().upper()
    if value.startswith("&H"):
        value = value[2:]
    value = value.rjust(8, "0")[-8:]

    aa = int(value[0:2], 16)
    bb = int(value[2:4], 16)
    gg = int(value[4:6], 16)
    rr = int(value[6:8], 16)

    return pysubs2.Color(rr, gg, bb, aa)


# Parse the standalone text-overlay layer settings shared by caption renders and video-processing jobs.
def _build_overlay_settings_from_form(form):
    return {
        "enabled": _safe_bool(form.get("overlay_enabled", "0")),
        "text": str(form.get("overlay_text", "") or ""),
        "font_name": form.get("overlay_font_name", form.get("font_name", "Arial")),
        "font_size": _safe_int(form.get("overlay_font_size", 36), 36),
        "bold": _safe_bool(form.get("overlay_bold", "0")),
        "italic": _safe_bool(form.get("overlay_italic", "0")),
        "all_caps": _safe_bool(form.get("overlay_all_caps", "0")),
        "primary_colour": _hex_to_ass_bgr(form.get("overlay_colour", "#FFFFFF"), "00"),
        "outline_colour": _hex_to_ass_bgr(form.get("overlay_outline_colour", "#000000"), "00"),
        "shadow_colour": _hex_to_ass_bgr(form.get("overlay_shadow_colour", "#000000"), "00"),
        "background_colour": _hex_to_ass_bgr(form.get("overlay_background_colour", "#000000"), "00"),
        "use_background": _safe_bool(form.get("overlay_use_background", "0")),
        "background_alpha": _safe_float(form.get("overlay_background_alpha", 0.35), 0.35),
        "background_pad_x": _safe_int(form.get("overlay_background_pad_x", 8), 8),
        "outline": _safe_float(form.get("overlay_outline", 2), 2),
        "shadow": _safe_float(form.get("overlay_shadow", 0), 0),
        "blur": _safe_float(form.get("overlay_blur", 0.8), 0.8),
        "alignment": _safe_int(form.get("overlay_alignment", 5), 5),
        "margin_v": _safe_int(form.get("overlay_margin_v", 40), 40),
        "margin_h": _safe_int(form.get("overlay_margin_h", 40), 40),
        "anchor_x": _safe_float(form.get("overlay_anchor_x", 0.5), 0.5),
        "anchor_y": _safe_float(form.get("overlay_anchor_y", 0.18), 0.18),
        "offset_x": _safe_int(form.get("overlay_offset_x", 0), 0),
        "offset_y": _safe_int(form.get("overlay_offset_y", 0), 0),
        "rotation_z": _safe_float(form.get("overlay_rotation_z", 0), 0),
        "letter_spacing": _safe_float(form.get("overlay_letter_spacing", 0), 0),
    }


# Return True only when the extra burnt-in text layer should actually be emitted.
def _overlay_layer_enabled(overlay_settings):
    if not overlay_settings:
        return False

    if not overlay_settings.get("enabled"):
        return False

    return bool(str(overlay_settings.get("text", "") or "").strip())


# Apply the text-overlay layer style into an SSA file so it can be burnt in by libass.
def _ensure_overlay_style(subs, overlay_settings, style_name="OverlayLayer"):
    bg_alpha_hex = f"{max(0, min(255, round((1 - overlay_settings['background_alpha']) * 255))):02X}"
    bg_value = str(overlay_settings.get("background_colour", "&H00000000")).strip().upper()
    if bg_value.startswith("&H"):
        bg_value = bg_value[2:]
    bg_value = bg_value.rjust(8, "0")[-8:]

    box_color = _ass_bgr_to_pysubs2_color(f"&H{bg_alpha_hex}{bg_value[2:]}")
    borderstyle = 4 if overlay_settings.get("use_background") else 1

    style = subs.styles.get(style_name, pysubs2.SSAStyle())
    style.fontname = overlay_settings["font_name"]
    style.fontsize = overlay_settings["font_size"]
    style.primarycolor = _ass_bgr_to_pysubs2_color(overlay_settings["primary_colour"], "&H00FFFFFF")
    style.outlinecolor = _ass_bgr_to_pysubs2_color(overlay_settings["outline_colour"], "&H00000000")
    style.backcolor = box_color
    style.bold = overlay_settings["bold"]
    style.italic = overlay_settings["italic"]
    style.borderstyle = borderstyle
    style.outline = overlay_settings["outline"]
    style.shadow = overlay_settings["shadow"]
    style.alignment = overlay_settings["alignment"]
    style.marginv = overlay_settings["margin_v"]
    style.marginl = overlay_settings["margin_h"]
    style.marginr = overlay_settings["margin_h"]
    subs.styles[style_name] = style
    return style_name


# Build the override text used by the extra static overlay layer.
def _build_overlay_text_override(overlay_settings, video_info):
    pos_x, pos_y = _compute_position(video_info, overlay_settings)
    box_or_shadow_colour = overlay_settings["background_colour"] if overlay_settings.get("use_background") else overlay_settings["shadow_colour"]

    tags = [
        rf"\an{overlay_settings['alignment']}",
        rf"\pos({pos_x},{pos_y})",
        rf"\bord{overlay_settings['outline']:g}",
        rf"\shad{overlay_settings['shadow']:g}",
        rf"\blur{overlay_settings['blur']:g}",
        rf"\1c{overlay_settings['primary_colour']}",
        rf"\3c{overlay_settings['outline_colour']}",
        rf"\4c{box_or_shadow_colour}",
        rf"\fsp{overlay_settings['letter_spacing']:g}",
        rf"\frz{overlay_settings['rotation_z']:g}",
    ]

    overlay_text = _apply_text_case(str(overlay_settings.get("text", "") or ""), overlay_settings)
    overlay_text = _escape_ass_text(overlay_text)

    if overlay_settings.get("use_background") and int(overlay_settings.get("background_pad_x", 0)) > 0:
        hard_spaces = r"\h" * int(overlay_settings["background_pad_x"])
        overlay_text = f"{hard_spaces}{overlay_text}{hard_spaces}"

    return "{" + "".join(tags) + "}" + overlay_text


# Append the extra text-overlay layer to an existing ASS subtitle file so both burn in together.
def append_text_overlay_to_ass(ass_path, overlay_settings, video_info, duration_seconds):
    if not _overlay_layer_enabled(overlay_settings):
        return False

    subs = pysubs2.load(ass_path, encoding="utf-8")
    subs.info["PlayResX"] = str(video_info["width"])
    subs.info["PlayResY"] = str(video_info["height"])
    subs.info["ScaledBorderAndShadow"] = "yes"
    subs.info["WrapStyle"] = "0"

    style_name = _ensure_overlay_style(subs, overlay_settings)
    duration_ms = max(1, int(round(float(duration_seconds or 0) * 1000)))

    event = pysubs2.SSAEvent(
        start=0,
        end=duration_ms,
        text=_build_overlay_text_override(overlay_settings, video_info),
        style=style_name,
    )
    event.layer = 50
    subs.events.append(event)
    subs.save(ass_path)
    return True


# Generate a standalone ASS file for the extra text-overlay layer.
def create_text_overlay_ass(dst, overlay_settings, video_info, duration_seconds):
    if not _overlay_layer_enabled(overlay_settings):
        return False

    subs = pysubs2.SSAFile()
    subs.info["PlayResX"] = str(video_info["width"])
    subs.info["PlayResY"] = str(video_info["height"])
    subs.info["ScaledBorderAndShadow"] = "yes"
    subs.info["WrapStyle"] = "0"

    style_name = _ensure_overlay_style(subs, overlay_settings)
    duration_ms = max(1, int(round(float(duration_seconds or 0) * 1000)))

    event = pysubs2.SSAEvent(
        start=0,
        end=duration_ms,
        text=_build_overlay_text_override(overlay_settings, video_info),
        style=style_name,
    )
    event.layer = 50
    subs.events.append(event)
    subs.save(dst)
    return True

def _ts_to_ms(ts):
    hh, mm, rest = ts.split(":")
    ss, ms = rest.split(".")
    return (int(hh) * 3600 + int(mm) * 60 + int(ss)) * 1000 + int(ms)


def _strip_vtt_markup(text):
    text = re.sub(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", "", text)
    text = re.sub(r"</?c[^>]*>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)

    lines = []
    for line in text.splitlines():
        if line.strip():
            lines.append(" ".join(line.split()))
        else:
            lines.append("")

    return "\n".join(lines)


def _parse_vtt_cues(vtt_path):
    with open(vtt_path, "r", encoding="utf-8-sig") as f:
        raw = f.read().replace("\r\n", "\n").replace("\r", "\n")

    blocks = re.split(r"\n{2,}", raw)
    cues = []

    for block in blocks:
        if not block.strip():
            continue

        raw_lines = block.split("\n")
        nonempty_lines = [line for line in raw_lines if line.strip()]

        if not nonempty_lines:
            continue

        if nonempty_lines[0].startswith("WEBVTT"):
            continue
        if nonempty_lines[0].startswith("Kind:"):
            continue
        if nonempty_lines[0].startswith("Language:"):
            continue

        timing_idx = None
        for i, line in enumerate(raw_lines):
            if "-->" in line:
                timing_idx = i
                break

        if timing_idx is None:
            continue

        timing_line = raw_lines[timing_idx]

        m = re.search(
            r"(\d{2}:\d{2}:\d{2}\.\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}\.\d{3})",
            timing_line,
        )
        if not m:
            continue

        start_ms = _ts_to_ms(m.group(1))
        end_ms = _ts_to_ms(m.group(2))

        payload_lines = raw_lines[timing_idx + 1:]
        payload = "\n".join(payload_lines)

        full_text = _strip_vtt_markup(payload)
        if not full_text.strip():
            continue

        timed_words = []
        word_matches = list(
            re.finditer(
                r"<(\d{2}:\d{2}:\d{2}\.\d{3})><c>(.*?)</c>",
                payload,
                flags=re.DOTALL,
            )
        )

        first_word_pos = word_matches[0].start() if word_matches else len(payload)
        prefix_payload = payload[:first_word_pos]

        prefix_plain = re.sub(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", "", prefix_payload)
        prefix_plain = re.sub(r"</?c[^>]*>", "", prefix_plain)
        prefix_plain = re.sub(r"<[^>]+>", "", prefix_plain)
        prefix_plain = html.unescape(prefix_plain)

        for word in re.findall(r"\S+", prefix_plain):
            timed_words.append({
                "start": start_ms,
                "word": word,
            })

        for match in word_matches:
            word_start = _ts_to_ms(match.group(1))
            word_text = _strip_vtt_markup(match.group(2))

            for word in re.findall(r"\S+", word_text):
                timed_words.append({
                    "start": word_start,
                    "word": word,
                })

        cues.append({
            "start": start_ms,
            "end": end_ms,
            "text": full_text,
            "timed_words": timed_words,
        })

    return cues






def _build_vtt_word_reveal_text(cue, settings, video_info, past_words, active_word):
    current_index = len([word for word in past_words if str(word).strip()])

    pos_x, pos_y = _compute_position(video_info, settings)
    pos_x += _pick_variant_offset(
        settings.get("random_position_jitter"),
        settings.get("position_jitter_x", 0),
        cue["start"],
        current_index,
        settings.get("variation_seed", 0),
        101,
    )
    pos_y += _pick_variant_offset(
        settings.get("random_position_jitter"),
        settings.get("position_jitter_y", 0),
        cue["start"],
        current_index,
        settings.get("variation_seed", 0),
        151,
    )

    box_or_shadow_colour = _pick_variant_colour(
        settings.get("background_colour_mode", "fixed"),
        settings["background_colour"],
        settings.get("background_palette", ""),
        cue["start"],
        current_index,
        settings.get("variation_seed", 0),
        79,
    ) if settings.get("use_background") else settings["shadow_colour"]

    tags = [
        rf"\an{settings['alignment']}",
        rf"\pos({pos_x},{pos_y})",
        rf"\bord{settings['outline']:g}",
        rf"\shad{settings['shadow']:g}",
        rf"\blur{settings['blur']:g}",
        rf"\1c{settings['primary_colour']}",
        rf"\3c{settings['outline_colour']}",
        rf"\4c{box_or_shadow_colour}",
        rf"\fsp{settings['letter_spacing']:g}",
        rf"\frz{settings['rotation_z']:g}",
        rf"\fscx{settings['end_scale']}",
        rf"\fscy{settings['end_scale']}",
    ]

    prefix = "{" + "".join(tags) + "}"
    transformed_text = _apply_text_case(cue["text"], settings)
    transformed_active_word = _apply_text_case(active_word or "", settings)
    hard_spaces = r"\h" * max(0, int(settings.get("background_pad_x", 0))) if settings.get("use_background") else ""

    if settings.get("word_display_mode", "phrase") == "current_word":
        current_word = _escape_ass_text(transformed_active_word.strip())

        if not current_word:
            fallback_words = re.findall(r"\S+", transformed_text)
            current_word = _escape_ass_text(fallback_words[0]) if fallback_words else ""

        if not current_word:
            return prefix

        if hard_spaces:
            current_word = f"{hard_spaces}{current_word}{hard_spaces}"

        active_colour = _pick_variant_colour(
            settings.get("active_word_colour_mode", "fixed"),
            settings["active_word_colour"],
            settings.get("active_palette", ""),
            cue["start"],
            current_index,
            settings.get("variation_seed", 0),
            31,
        )

        return prefix + "{" + rf"\1c{active_colour}" + "}" + current_word

    shown_past = current_index
    revealed_count = 0
    active_done = False
    tokens = re.split(r"(\s+)", transformed_text)

    if settings.get("use_background"):
        visible_parts = []
        pending_space = ""

        for token in tokens:
            if token == "":
                continue

            if token.isspace():
                if visible_parts:
                    pending_space = token.replace("\n", r"\N")
                continue

            escaped_word = _escape_ass_text(token)

            if revealed_count < shown_past:
                if pending_space:
                    visible_parts.append(pending_space)
                    pending_space = ""

                word_colour = _pick_variant_colour(
                    settings.get("primary_colour_mode", "fixed"),
                    settings["primary_colour"],
                    settings.get("primary_palette", ""),
                    cue["start"],
                    revealed_count,
                    settings.get("variation_seed", 0),
                    17,
                )

                visible_parts.append("{" + rf"\1c{word_colour}" + "}" + escaped_word)
                revealed_count += 1
                continue

            if not active_done and transformed_active_word and token == transformed_active_word:
                if pending_space:
                    visible_parts.append(pending_space)
                    pending_space = ""

                active_colour = _pick_variant_colour(
                    settings.get("active_word_colour_mode", "fixed"),
                    settings["active_word_colour"],
                    settings.get("active_palette", ""),
                    cue["start"],
                    revealed_count,
                    settings.get("variation_seed", 0),
                    31,
                )

                visible_parts.append("{" + rf"\1c{active_colour}" + "}" + escaped_word)
                active_done = True
                break

            break

        if not visible_parts:
            return prefix

        return prefix + hard_spaces + "".join(visible_parts) + hard_spaces

    parts = []

    for token in tokens:
        if token == "":
            continue

        if token.isspace():
            parts.append(token.replace("\n", r"\N"))
            continue

        escaped_word = _escape_ass_text(token)

        if revealed_count < shown_past:
            word_colour = _pick_variant_colour(
                settings.get("primary_colour_mode", "fixed"),
                settings["primary_colour"],
                settings.get("primary_palette", ""),
                cue["start"],
                revealed_count,
                settings.get("variation_seed", 0),
                17,
            )

            parts.append("{" + rf"\1c{word_colour}" + "}" + escaped_word)

        elif not active_done and transformed_active_word and token == transformed_active_word:
            active_colour = _pick_variant_colour(
                settings.get("active_word_colour_mode", "fixed"),
                settings["active_word_colour"],
                settings.get("active_palette", ""),
                cue["start"],
                revealed_count,
                settings.get("variation_seed", 0),
                31,
            )

            parts.append("{" + rf"\1c{active_colour}" + "}" + escaped_word)
            active_done = True

        else:
            parts.append("{" + r"\alpha&HFF&" + "}" + escaped_word + "{" + r"\alpha&H00&" + "}")

        revealed_count += 1

    return prefix + "".join(parts)





def get_video_info(video_path):
    cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")

    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("No video stream found")

    return {
        "width": int(streams[0]["width"]),
        "height": int(streams[0]["height"]),
    }


# Probe total media duration in seconds for preview clipping and segment building.
# Keeps post-processing preview aligned with the existing preview window controls.
def get_video_duration(video_path):
    cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe duration failed")

    try:
        duration = float((result.stdout or "").strip())
    except Exception as exc:
        raise RuntimeError("Could not read video duration") from exc

    if duration <= 0:
        raise RuntimeError("Video duration is invalid")

    return duration


# Probe source video dimensions, frame rate, and audio presence for processing jobs.
def get_video_stream_meta(video_path):
    cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-show_entries", "stream=index,codec_type,width,height,avg_frame_rate,r_frame_rate",
        "-of", "json",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe stream info failed")

    try:
        data = json.loads(result.stdout or "{}")
    except Exception as exc:
        raise RuntimeError("Could not parse ffprobe stream info") from exc

    streams = data.get("streams") or []
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video_stream:
        raise RuntimeError("No video stream found")

    fps_value = (video_stream.get("avg_frame_rate") or "").strip()
    if not fps_value or fps_value == "0/0":
        fps_value = (video_stream.get("r_frame_rate") or "").strip()
    if not fps_value or fps_value == "0/0":
        fps_value = "30"

    return {
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "fps": fps_value,
        "has_audio": any(item.get("codec_type") == "audio" for item in streams),
    }


def _even_size(value):
    value = max(2, int(round(float(value))))
    if value % 2:
        value += 1
    return value


_JOB_PROGRESS_UNSET = object()


def _set_job_progress(
    job_id,
    status=None,
    message=None,
    phase=_JOB_PROGRESS_UNSET,
    current=_JOB_PROGRESS_UNSET,
    total=_JOB_PROGRESS_UNSET,
    eta_seconds=_JOB_PROGRESS_UNSET,
):
    job = JOBS.get(job_id)
    if not job:
        return
    if status is not None:
        job["status"] = status
    if message is not None:
        job["message"] = message
    if phase is not _JOB_PROGRESS_UNSET:
        job["phase"] = phase
    if current is not _JOB_PROGRESS_UNSET:
        job["progress_current"] = None if current is None else max(0, int(current))
    if total is not _JOB_PROGRESS_UNSET:
        job["progress_total"] = None if total is None else max(0, int(total))
    if eta_seconds is not _JOB_PROGRESS_UNSET:
        job["eta_seconds"] = None if eta_seconds is None else max(0, int(eta_seconds))


def _count_png_frames(directory_path):
    if not directory_path or not os.path.isdir(directory_path):
        return 0
    count = 0
    with os.scandir(directory_path) as entries:
        for entry in entries:
            if entry.is_file() and entry.name.lower().endswith('.png'):
                count += 1
    return count


def _estimate_eta_seconds(started_at, completed, total):
    try:
        completed = int(completed)
        total = int(total)
    except Exception:
        return None

    if not started_at or completed <= 0 or total <= 0 or completed >= total:
        return None

    elapsed = max(0.001, float(time.monotonic()) - float(started_at))
    rate = completed / elapsed
    if rate <= 0:
        return None

    remaining = max(0, total - completed)
    return int(round(remaining / rate))


def _find_first_executable(candidates):
    seen = set()
    for value in candidates:
        if not value:
            continue
        expanded = os.path.abspath(os.path.expanduser(str(value)))
        if expanded in seen:
            continue
        seen.add(expanded)

        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded

        resolved = shutil.which(str(value))
        if resolved and os.access(resolved, os.X_OK):
            return resolved

    return None


def _download_file(url, destination_path):
    temp_path = f"{destination_path}.part"
    request_obj = urllib.request.Request(
        url,
        headers={"User-Agent": "CaptionAnimator/1.0"},
    )

    try:
        with urllib.request.urlopen(request_obj, timeout=180) as response, open(temp_path, "wb") as target:
            shutil.copyfileobj(response, target)
        os.replace(temp_path, destination_path)
    except Exception:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        raise


def _mark_executable(path_value):
    if not path_value or not os.path.isfile(path_value):
        return

    try:
        current_mode = os.stat(path_value).st_mode
        os.chmod(path_value, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass


def _find_realesrgan_binary(search_root):
    if not search_root or not os.path.isdir(search_root):
        return None

    preferred_names = ["realesrgan-ncnn-vulkan.exe", "realesrgan-ncnn-vulkan"]
    for root, _, files in os.walk(search_root):
        lower_map = {filename.lower(): filename for filename in files}
        for preferred in preferred_names:
            actual_name = lower_map.get(preferred.lower())
            if not actual_name:
                continue

            candidate = os.path.join(root, actual_name)
            _mark_executable(candidate)
            if os.access(candidate, os.X_OK):
                return candidate

    return None


def _find_realesrgan_models(search_root):
    if not search_root or not os.path.isdir(search_root):
        return None

    for root, _, files in os.walk(search_root):
        lower_files = {filename.lower() for filename in files}
        if 'realesrgan-x4plus.param' in lower_files and 'realesrgan-x4plus.bin' in lower_files:
            return root

    return None


def _get_realesrgan_asset():
    asset = REALESRGAN_RELEASE_ASSETS.get(platform.system())
    if asset:
        return asset

    raise RuntimeError(
        f"Real-ESRGAN auto-install is not supported on {platform.system() or 'this platform'}."
    )


def _bootstrap_realesrgan_backend(job_id=None):
    ensure_dirs()
    asset = _get_realesrgan_asset()
    install_name = os.path.splitext(asset['filename'])[0]
    install_dir = os.path.join(REALESRGAN_DIR, install_name)
    archive_path = os.path.join(REALESRGAN_DIR, asset['filename'])

    with REALESRGAN_BOOTSTRAP_LOCK:
        existing_binary = _find_realesrgan_binary(install_dir)
        existing_models = _find_realesrgan_models(install_dir)
        if existing_binary and existing_models:
            return {
                'binary': existing_binary,
                'model_dir': existing_models,
            }

        if job_id:
            _set_job_progress(job_id, 'preparing', 'Downloading Real-ESRGAN runtime...')

        if not os.path.exists(archive_path):
            try:
                _download_file(asset['url'], archive_path)
            except urllib.error.URLError as exc:
                reason = getattr(exc, 'reason', None) or str(exc)
                raise RuntimeError(f"Could not download Real-ESRGAN runtime: {reason}") from exc
            except Exception as exc:
                raise RuntimeError(f"Could not download Real-ESRGAN runtime: {exc}") from exc

        if job_id:
            _set_job_progress(job_id, 'preparing', 'Installing Real-ESRGAN runtime...')

        extract_dir = f"{install_dir}__extracting"
        shutil.rmtree(extract_dir, ignore_errors=True)
        os.makedirs(extract_dir, exist_ok=True)

        try:
            with zipfile.ZipFile(archive_path, 'r') as archive:
                archive.extractall(extract_dir)
        except Exception as exc:
            shutil.rmtree(extract_dir, ignore_errors=True)
            try:
                os.remove(archive_path)
            except Exception:
                pass
            raise RuntimeError(f"Could not unpack Real-ESRGAN runtime: {exc}") from exc

        shutil.rmtree(install_dir, ignore_errors=True)
        os.replace(extract_dir, install_dir)

        binary = _find_realesrgan_binary(install_dir)
        model_dir = _find_realesrgan_models(install_dir)

        if not binary:
            raise RuntimeError('Real-ESRGAN runtime unpacked, but the executable was not found.')
        if not model_dir:
            raise RuntimeError('Real-ESRGAN runtime unpacked, but the model files were not found.')

        return {
            'binary': binary,
            'model_dir': model_dir,
        }


def _resolve_realesrgan_backend(job_id=None):
    binary = _find_first_executable([
        os.environ.get('REALESRGAN_BIN'),
        os.environ.get('AI_UPSCALER_BIN'),
        os.path.join(APP_ROOT, 'tools', 'realesrgan-ncnn-vulkan'),
        os.path.join(APP_ROOT, 'tools', 'realesrgan-ncnn-vulkan.exe'),
        os.path.join(REALESRGAN_DIR, 'realesrgan-ncnn-vulkan'),
        os.path.join(REALESRGAN_DIR, 'realesrgan-ncnn-vulkan.exe'),
        'realesrgan-ncnn-vulkan',
        'realesrgan-ncnn-vulkan.exe',
    ])

    model_dir = None
    candidate_model_dirs = [os.environ.get('REALESRGAN_MODEL_DIR')]

    if binary:
        candidate_model_dirs.extend([
            os.path.join(os.path.dirname(binary), 'models'),
            os.path.join(APP_ROOT, 'models'),
            os.path.join(APP_ROOT, 'models', 'realesrgan'),
            REALESRGAN_DIR,
        ])

    for candidate in candidate_model_dirs:
        if candidate and os.path.isdir(candidate):
            resolved_models = _find_realesrgan_models(candidate) or candidate
            if resolved_models and _find_realesrgan_models(resolved_models):
                model_dir = resolved_models
                break

    if binary and model_dir:
        return {
            'binary': binary,
            'model_dir': model_dir,
        }

    return _bootstrap_realesrgan_backend(job_id=job_id)


def process_upscale_video(video_path, output_path, upscale_factor=2, upscale_mode="traditional", preview_start=0, preview_seconds=None, job_id=None):
    upscale_factor = 4 if int(upscale_factor or 2) >= 4 else 2
    upscale_mode = "ai" if str(upscale_mode or "traditional").lower() == "ai" else "traditional"

    source_meta = get_video_stream_meta(video_path)
    target_width = _even_size(source_meta["width"] * upscale_factor)
    target_height = _even_size(source_meta["height"] * upscale_factor)

    if upscale_mode == "traditional":
        _set_job_progress(job_id, "rendering", f"Upscaling video {upscale_factor}x...")
        vf_expr = f"scale={target_width}:{target_height}:flags=lanczos:param0=3"

        cmd = [
            FFMPEG_BIN,
            "-y",
            "-nostdin",
            "-hwaccel", "none",
        ]

        if preview_start is not None and float(preview_start) > 0:
            cmd += ["-ss", f"{float(preview_start):.6f}"]

        cmd += ["-i", video_path]

        if preview_seconds is not None:
            cmd += ["-t", f"{max(0.1, float(preview_seconds)):.6f}"]

        cmd += [
            "-vf", vf_expr,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "medium",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            output_path,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Traditional upscale failed")
        return

    backend = _resolve_realesrgan_backend(job_id=job_id)

    with tempfile.TemporaryDirectory(prefix="video_upscale_") as work_dir:
        frames_in_dir = os.path.join(work_dir, "frames_in")
        frames_out_dir = os.path.join(work_dir, "frames_out")
        audio_path = os.path.join(work_dir, "audio.m4a")
        os.makedirs(frames_in_dir, exist_ok=True)
        os.makedirs(frames_out_dir, exist_ok=True)

        _set_job_progress(job_id, "preparing", "Extracting frames...")

        extract_cmd = [
            FFMPEG_BIN,
            "-y",
            "-nostdin",
            "-hwaccel", "none",
        ]

        if preview_start is not None and float(preview_start) > 0:
            extract_cmd += ["-ss", f"{float(preview_start):.6f}"]

        extract_cmd += ["-i", video_path]

        if preview_seconds is not None:
            extract_cmd += ["-t", f"{max(0.1, float(preview_seconds)):.6f}"]

        extract_cmd += [
            "-map", "0:v:0",
            "-vsync", "0",
            os.path.join(frames_in_dir, "frame_%08d.png"),
        ]

        extract_result = subprocess.run(extract_cmd, capture_output=True, text=True)
        if extract_result.returncode != 0:
            raise RuntimeError(extract_result.stderr.strip() or "Could not extract source frames")

        input_frames = sorted(
            name for name in os.listdir(frames_in_dir)
            if name.lower().endswith('.png')
        )
        if not input_frames:
            raise RuntimeError('No source frames were extracted for AI upscaling.')

        if source_meta["has_audio"]:
            audio_cmd = [
                FFMPEG_BIN,
                "-y",
                "-nostdin",
                "-hwaccel", "none",
            ]

            if preview_start is not None and float(preview_start) > 0:
                audio_cmd += ["-ss", f"{float(preview_start):.6f}"]

            audio_cmd += ["-i", video_path]

            if preview_seconds is not None:
                audio_cmd += ["-t", f"{max(0.1, float(preview_seconds)):.6f}"]

            audio_cmd += [
                "-map", "0:a:0",
                "-vn",
                "-c:a", "aac",
                "-b:a", "192k",
                audio_path,
            ]

            audio_result = subprocess.run(audio_cmd, capture_output=True, text=True)
            if audio_result.returncode != 0:
                audio_path = None

        total_frames = len(input_frames)
        _set_job_progress(
            job_id,
            "rendering",
            f"Upscaling frames with Real-ESRGAN... 0/{total_frames}",
            phase="ai_upscale",
            current=0,
            total=total_frames,
            eta_seconds=None,
        )

        ai_cmd = [
            backend['binary'],
            '-i', frames_in_dir,
            '-o', frames_out_dir,
            '-s', str(upscale_factor),
            '-n', 'realesrgan-x4plus',
            '-m', backend['model_dir'],
            '-f', 'png',
            '-t', '0',
        ]

        ai_log_path = os.path.join(work_dir, 'realesrgan.log')
        ai_started_at = time.monotonic()

        with open(ai_log_path, 'w+', encoding='utf-8', errors='replace') as ai_log_handle:
            ai_process = subprocess.Popen(
                ai_cmd,
                stdout=ai_log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=os.path.dirname(backend['binary']),
            )

            last_reported = -1
            while True:
                completed_frames = _count_png_frames(frames_out_dir)
                if completed_frames != last_reported:
                    eta_seconds = _estimate_eta_seconds(ai_started_at, completed_frames, total_frames)
                    _set_job_progress(
                        job_id,
                        "rendering",
                        f"Upscaling frames with Real-ESRGAN... {completed_frames}/{total_frames}",
                        phase="ai_upscale",
                        current=completed_frames,
                        total=total_frames,
                        eta_seconds=eta_seconds,
                    )
                    last_reported = completed_frames

                if ai_process.poll() is not None:
                    break

                time.sleep(0.35)

            ai_return_code = ai_process.wait()
            ai_log_handle.flush()
            ai_log_handle.seek(0)
            ai_details = ai_log_handle.read().strip()

        completed_frames = _count_png_frames(frames_out_dir)
        _set_job_progress(
            job_id,
            "rendering",
            f"Upscaling frames with Real-ESRGAN... {completed_frames}/{total_frames}",
            phase="ai_upscale",
            current=completed_frames,
            total=total_frames,
            eta_seconds=0 if completed_frames >= total_frames else None,
        )

        if ai_return_code != 0:
            raise RuntimeError(ai_details or 'AI frame upscale failed')

        encoded_frames = sorted(
            name for name in os.listdir(frames_out_dir)
            if name.lower().endswith('.png')
        )
        if not encoded_frames:
            raise RuntimeError('AI upscale produced no output frames')

        _set_job_progress(job_id, "rendering", "Encoding upscaled video...", phase="encoding", current=None, total=None, eta_seconds=None)

        encode_cmd = [
            FFMPEG_BIN,
            "-y",
            "-nostdin",
            '-framerate', source_meta['fps'],
            '-i', os.path.join(frames_out_dir, 'frame_%08d.png'),
        ]

        if audio_path and os.path.exists(audio_path):
            encode_cmd += ['-i', audio_path]

        encode_cmd += ['-map', '0:v:0']

        if audio_path and os.path.exists(audio_path):
            encode_cmd += ['-map', '1:a:0']

        encode_cmd += [
            '-c:v', 'libx264',
            '-crf', '18',
            '-preset', 'medium',
            '-pix_fmt', 'yuv420p',
        ]

        if audio_path and os.path.exists(audio_path):
            encode_cmd += ['-c:a', 'aac', '-b:a', '192k', '-shortest']
        else:
            encode_cmd += ['-an']

        encode_cmd += ['-movflags', '+faststart', output_path]

        encode_result = subprocess.run(encode_cmd, capture_output=True, text=True)
        if encode_result.returncode != 0:
            raise RuntimeError(encode_result.stderr.strip() or 'Could not encode upscaled video')


# Analyze silence and render either talk-only or silence-only segments into one output video.
# Uses ffmpeg silencedetect so no extra Python audio pipeline is needed inside the app.
def process_silence_video(video_path, output_path, silence_threshold=-40.0, min_silence_duration=0.4, keep_mode="talk", preview_start=0, preview_seconds=None):
    # Analyze silence and render the kept segments while forcing software decode for source-video compatibility.
    total_duration = get_video_duration(video_path)
    clip_start = max(0.0, float(preview_start or 0.0))
    clip_end = total_duration if preview_seconds is None else min(total_duration, clip_start + max(0.1, float(preview_seconds)))
    clip_duration = max(0.0, clip_end - clip_start)

    if clip_duration <= 0.05:
        raise RuntimeError("Preview window is empty.")

    # Vulnerable block: caller-provided timing and threshold values are passed as tokenized ffmpeg arguments.
    detect_cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "info",
        "-nostdin",
        "-hwaccel", "none",
    ]

    if clip_start > 0:
        detect_cmd += ["-ss", f"{clip_start:.6f}"]

    detect_cmd += ["-i", video_path]

    if preview_seconds is not None:
        detect_cmd += ["-t", f"{clip_duration:.6f}"]

    detect_cmd += [
        "-af", f"silencedetect=noise={float(silence_threshold):.2f}dB:d={float(min_silence_duration):.3f}",
        "-f", "null",
        "-",
    ]

    detect_result = subprocess.run(detect_cmd, capture_output=True, text=True)
    if detect_result.returncode != 0:
        raise RuntimeError(detect_result.stderr.strip() or "Silence analysis failed")

    silence_segments = []
    silence_start = None

    for line in (detect_result.stderr or "").splitlines():
        start_match = re.search(r"silence_start:\s*([0-9.]+)", line)
        if start_match:
            silence_start = float(start_match.group(1))
            continue

        end_match = re.search(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)", line)
        if end_match and silence_start is not None:
            start_value = max(0.0, silence_start)
            end_value = min(clip_duration, float(end_match.group(1)))
            if end_value - start_value >= float(min_silence_duration):
                silence_segments.append((start_value, end_value))
            silence_start = None

    if silence_start is not None and clip_duration - silence_start >= float(min_silence_duration):
        silence_segments.append((max(0.0, silence_start), clip_duration))

    keep_segments = []

    if keep_mode == "silence":
        keep_segments = silence_segments[:]
    else:
        if not silence_segments:
            keep_segments = [(0.0, clip_duration)]
        else:
            cursor = 0.0
            for start_value, end_value in silence_segments:
                if start_value > cursor:
                    keep_segments.append((cursor, start_value))
                cursor = max(cursor, end_value)

            if cursor < clip_duration:
                keep_segments.append((cursor, clip_duration))

    keep_segments = [
        (max(0.0, start_value), min(clip_duration, end_value))
        for start_value, end_value in keep_segments
        if end_value - start_value >= 0.03
    ]

    if not keep_segments:
        raise RuntimeError("No segments matched the selected silence settings.")

    filter_parts = []
    concat_inputs = []

    for index, (start_value, end_value) in enumerate(keep_segments):
        abs_start = clip_start + start_value
        abs_end = clip_start + end_value

        filter_parts.append(
            f"[0:v]trim=start={abs_start:.6f}:end={abs_end:.6f},setpts=PTS-STARTPTS[v{index}]"
        )
        filter_parts.append(
            f"[0:a]atrim=start={abs_start:.6f}:end={abs_end:.6f},asetpts=PTS-STARTPTS[a{index}]"
        )
        concat_inputs.append(f"[v{index}][a{index}]")

    filter_complex = ";".join(filter_parts) + ";" + "".join(concat_inputs) + f"concat=n={len(keep_segments)}:v=1:a=1[v][a]"

    # Vulnerable block: generated filter graph uses computed segment boundaries only and is passed as one ffmpeg argument.
    render_cmd = [
        FFMPEG_BIN,
        "-y",
        "-nostdin",
        "-hwaccel", "none",
        "-i", video_path,
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]

    render_result = subprocess.run(render_cmd, capture_output=True, text=True)
    if render_result.returncode != 0:
        raise RuntimeError(render_result.stderr.strip() or "Silence processing failed")


# Render a speed-adjusted video while keeping pitch stable with chained atempo filters.
# Supports both preview-window processing and full-length processing.
def process_speed_video(video_path, output_path, speed_factor=1.25, preview_start=0, preview_seconds=None):
    # Render the speed-adjusted output while forcing software decode for source-video compatibility.
    speed_factor = float(speed_factor)
    if speed_factor <= 0:
        raise RuntimeError("Speed factor must be greater than 0.")

    atempo_filters = []
    remaining = speed_factor

    while remaining > 2.0:
        atempo_filters.append("atempo=2.0")
        remaining /= 2.0

    while remaining < 0.5:
        atempo_filters.append("atempo=0.5")
        remaining /= 0.5

    atempo_filters.append(f"atempo={remaining:.8f}")
    filter_complex = f"[0:v]setpts={1.0 / speed_factor:.12f}*PTS[v];[0:a]{','.join(atempo_filters)}[a]"

    # Vulnerable block: user-controlled preview timing is passed as tokenized ffmpeg arguments and the filter graph is generated server-side.
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-nostdin",
        "-hwaccel", "none",
    ]

    if preview_start is not None and float(preview_start) > 0:
        cmd += ["-ss", f"{float(preview_start):.6f}"]

    cmd += ["-i", video_path]

    if preview_seconds is not None:
        cmd += ["-t", f"{max(0.1, float(preview_seconds)):.6f}"]

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Speed processing failed")


# Run one post-processing job and publish the resulting video through the existing job polling flow.
# Reuses the app’s preview_url player loading instead of inventing a second status system.
def process_video_job(job_id, video_path, output_path, process_kind, settings, preview_start=0, preview_seconds=None):
    try:
        if process_kind == "silence":
            JOBS[job_id]["status"] = "preparing"
            JOBS[job_id]["message"] = "Analyzing silence..."
            process_silence_video(
                video_path,
                output_path,
                silence_threshold=settings["silence_threshold"],
                min_silence_duration=settings["min_silence_duration"],
                keep_mode=settings["silence_mode"],
                preview_start=preview_start,
                preview_seconds=preview_seconds,
            )
        elif process_kind == "speed":
            JOBS[job_id]["status"] = "rendering"
            JOBS[job_id]["message"] = "Processing video speed..."
            process_speed_video(
                video_path,
                output_path,
                speed_factor=settings["speed_factor"],
                preview_start=preview_start,
                preview_seconds=preview_seconds,
            )
        elif process_kind == "upscale":
            JOBS[job_id]["status"] = "preparing"
            JOBS[job_id]["message"] = "Preparing video upscale..."
            process_upscale_video(
                video_path,
                output_path,
                upscale_factor=settings["upscale_factor"],
                upscale_mode=settings["upscale_mode"],
                preview_start=preview_start,
                preview_seconds=preview_seconds,
                job_id=job_id,
            )
        else:
            raise RuntimeError("Unsupported processing mode.")

        overlay_ass_name = None
        overlay_settings = settings.get("overlay")

        if _overlay_layer_enabled(overlay_settings):
            JOBS[job_id]["status"] = "rendering"
            JOBS[job_id]["message"] = "Burning text overlay..."

            overlay_ass_path = os.path.join(OUTPUT_DIR, f"{job_id}_{process_kind}_overlay.ass")
            final_output_path = os.path.join(OUTPUT_DIR, f"{job_id}_{process_kind}_with_overlay.mp4")
            processed_video_info = get_video_info(output_path)
            processed_duration = get_video_duration(output_path)

            create_text_overlay_ass(overlay_ass_path, overlay_settings, processed_video_info, processed_duration)
            render_preview(output_path, overlay_ass_path, final_output_path, preview_start=0, preview_seconds=None)
            os.replace(final_output_path, output_path)
            overlay_ass_name = os.path.basename(overlay_ass_path)

        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["message"] = "Processed video ready."
        JOBS[job_id]["preview_file"] = os.path.basename(output_path)
        JOBS[job_id]["ass_file"] = overlay_ass_name

    except Exception as exc:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["message"] = str(exc)




# Transcribe one video file into WEBVTT using faster-whisper.
# Generates normal segment cues or word-timestamp cues for the existing VTT parser.
def transcribe_video_to_vtt(video_path, model_name="small", language=None, word_timestamps=True, vad_filter=True):
    # Extract mono WAV audio from the source video and transcribe it into WEBVTT with optional word timestamps.
    try:
        from faster_whisper import WhisperModel
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: faster-whisper. Install it in your venv.") from exc

    def _vtt_ts(seconds):
        total_ms = max(0, int(round(float(seconds) * 1000)))
        hh = total_ms // 3600000
        mm = (total_ms % 3600000) // 60000
        ss = (total_ms % 60000) // 1000
        ms = total_ms % 1000
        return f"{hh:02d}:{mm:02d}:{ss:02d}.{ms:03d}"

    def _clean_text(value):
        return " ".join(str(value or "").split()).strip()

    tmp_wav_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            tmp_wav_path = tmp_wav.name

        # Vulnerable block: source path is passed as tokenized ffmpeg arguments and audio extraction is forced to software decode mode.
        extract_cmd = [
            FFMPEG_BIN,
            "-y",
            "-nostdin",
            "-hwaccel", "none",
            "-i", video_path,
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            tmp_wav_path,
        ]
        extract_result = subprocess.run(extract_cmd, capture_output=True, text=True)
        if extract_result.returncode != 0:
            raise RuntimeError(extract_result.stderr.strip() or "Audio extraction failed")

        model = WhisperModel(model_name, compute_type="auto")
        segments, _ = model.transcribe(
            tmp_wav_path,
            language=(language or None),
            vad_filter=bool(vad_filter),
            beam_size=5,
            word_timestamps=bool(word_timestamps),
            condition_on_previous_text=False,
        )

        lines = ["WEBVTT", ""]
        cue_count = 0

        for segment in segments:
            start_value = max(0.0, float(getattr(segment, "start", 0.0) or 0.0))
            end_value = max(start_value + 0.01, float(getattr(segment, "end", start_value + 0.01) or (start_value + 0.01)))
            text_value = _clean_text(getattr(segment, "text", ""))

            if not text_value:
                continue

            cue_text = text_value
            words = list(getattr(segment, "words", []) or [])

            if word_timestamps and words:
                timed_parts = []

                for index, word in enumerate(words):
                    word_text = _clean_text(getattr(word, "word", ""))
                    word_start = float(getattr(word, "start", start_value) or start_value)

                    if not word_text:
                        continue

                    prefix = "" if index == 0 else " "
                    timed_parts.append(f"<{_vtt_ts(word_start)}><c>{prefix}{word_text}</c>")

                if timed_parts:
                    cue_text = "".join(timed_parts)

            lines.append(f"{_vtt_ts(start_value)} --> {_vtt_ts(end_value)}")
            lines.append(cue_text)
            lines.append("")
            cue_count += 1

        if cue_count == 0:
            raise RuntimeError("No captions were produced from this video.")

        return "\n".join(lines).strip() + "\n"

    finally:
        if tmp_wav_path and os.path.exists(tmp_wav_path):
            try:
                os.remove(tmp_wav_path)
            except Exception:
                pass    


# Run one caption-transcription job and publish the generated VTT through the existing job poller.
# The resulting VTT is saved on disk and also returned inline so the UI can load it immediately.
def transcribe_captions_job(job_id, video_path, output_path, settings):
    try:
        JOBS[job_id]["status"] = "preparing"
        JOBS[job_id]["message"] = "Extracting audio..."

        vtt_text = transcribe_video_to_vtt(
            video_path,
            model_name=settings["caption_model"],
            language=settings["caption_language"],
            word_timestamps=settings["caption_word_timestamps"],
            vad_filter=settings["caption_vad_filter"],
        )

        JOBS[job_id]["status"] = "rendering"
        JOBS[job_id]["message"] = "Saving captions..."

        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(vtt_text)

        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["message"] = "Captions ready."
        JOBS[job_id]["preview_file"] = None
        JOBS[job_id]["ass_file"] = None
        JOBS[job_id]["captions_file"] = os.path.basename(output_path)
        JOBS[job_id]["captions_text"] = vtt_text

    except Exception as exc:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["message"] = str(exc)



def _alignment_to_anchor(alignment):
    alignment = int(alignment)

    if alignment in (1, 4, 7):
        anchor_x = 0.0
    elif alignment in (2, 5, 8):
        anchor_x = 0.5
    else:
        anchor_x = 1.0

    if alignment in (7, 8, 9):
        anchor_y = 0.0
    elif alignment in (4, 5, 6):
        anchor_y = 0.5
    else:
        anchor_y = 1.0

    return anchor_x, anchor_y


def _compute_position(video_info, settings):
    width = video_info["width"]
    height = video_info["height"]
    anchor_x, anchor_y = _alignment_to_anchor(settings["alignment"])

    pos_x = int(round(width * settings["anchor_x"]))
    pos_y = int(round(height * settings["anchor_y"]))

    if anchor_x == 0.0:
        pos_x += settings["margin_h"]
    elif anchor_x == 1.0:
        pos_x -= settings["margin_h"]

    if anchor_y == 0.0:
        pos_y += settings["margin_v"]
    elif anchor_y == 1.0:
        pos_y -= settings["margin_v"]

    pos_x += settings["offset_x"]
    pos_y += settings["offset_y"]

    return pos_x, pos_y


def _build_karaoke_text(text, mode, intro_ms):
    plain_text = text.replace(r"\N", r" \N ").split()
    if not plain_text:
        return text

    if mode == "words":
        tokens = plain_text
    elif mode == "letters":
        joined = text.replace(r"\N", "\n")
        tokens = []
        for ch in joined:
            if ch == "\n":
                tokens.append(r"\N")
            elif ch == " ":
                tokens.append(" ")
            else:
                tokens.append(ch)
    else:
        return text

    active_tokens = [t for t in tokens if t not in (" ", r"\N")]
    if not active_tokens:
        return text

    chunk = max(1, intro_ms // len(active_tokens))
    out = []
    for token in tokens:
        if token == " ":
            out.append(" ")
        elif token == r"\N":
            out.append(r"\N")
        else:
            out.append(r"{\k" + str(chunk // 10 if chunk >= 10 else 1) + r"}" + token)
    return "".join(out)











def _build_line_override(text, duration, settings, video_info, cue_start_ms=0, item_index=0):
    pos_x, pos_y = _compute_position(video_info, settings)

    jitter_x = _pick_variant_offset(
        settings.get("random_position_jitter"),
        settings.get("position_jitter_x", 0),
        cue_start_ms,
        item_index,
        settings.get("variation_seed", 0),
        101,
    )
    jitter_y = _pick_variant_offset(
        settings.get("random_position_jitter"),
        settings.get("position_jitter_y", 0),
        cue_start_ms,
        item_index,
        settings.get("variation_seed", 0),
        151,
    )

    pos_x += jitter_x
    pos_y += jitter_y

    intro_ms = max(0, min(duration, settings["intro_ms"]))
    outro_ms = max(0, min(duration, settings["outro_ms"]))
    exit_start = max(0, duration - outro_ms)

    start_x = pos_x + settings["in_offset_x"]
    start_y = pos_y + settings["in_offset_y"]
    end_x = pos_x + settings["out_offset_x"]
    end_y = pos_y + settings["out_offset_y"]

    primary_colour = _pick_variant_colour(
        settings.get("primary_colour_mode", "fixed"),
        settings["primary_colour"],
        settings.get("primary_palette", ""),
        cue_start_ms,
        item_index,
        settings.get("variation_seed", 0),
        17,
    )

    box_or_shadow_colour = _pick_variant_colour(
        settings.get("background_colour_mode", "fixed"),
        settings["background_colour"],
        settings.get("background_palette", ""),
        cue_start_ms,
        item_index,
        settings.get("variation_seed", 0),
        79,
    ) if settings.get("use_background") else settings["shadow_colour"]

    tags = [
        rf"\an{settings['alignment']}",
        rf"\bord{settings['outline']:g}",
        rf"\shad{settings['shadow']:g}",
        rf"\blur{settings['blur']:g}",
        rf"\1c{primary_colour}",
        rf"\3c{settings['outline_colour']}",
        rf"\4c{box_or_shadow_colour}",
        rf"\fsp{settings['letter_spacing']:g}",
        rf"\frz{settings['rotation_z']:g}",
        rf"\fscx{settings['start_scale']}",
        rf"\fscy{settings['start_scale']}",
        rf"\alpha{settings['start_alpha']}",
    ]

    if settings["animation_type"] in ("slide", "fade", "pop", "words", "letters"):
        if settings["animation_type"] == "slide":
            tags.append(rf"\move({start_x},{start_y},{pos_x},{pos_y},0,{intro_ms})")
        else:
            tags.append(rf"\pos({pos_x},{pos_y})")
    else:
        tags.append(rf"\pos({pos_x},{pos_y})")

    body = "{" + "".join(tags)

    if settings["animation_type"] == "pop":
        overshoot_scale = settings["end_scale"] + settings["overshoot_amount"]
        overshoot_end = min(duration, intro_ms + settings["overshoot_ms"])
        settle_end = min(duration, overshoot_end + settings["settle_ms"])
        body += rf"\t(0,{overshoot_end},\alpha&H00&\fscx{overshoot_scale}\fscy{overshoot_scale})"
        body += rf"\t({overshoot_end},{settle_end},\fscx{settings['end_scale']}\fscy{settings['end_scale']})"
    elif settings["animation_type"] == "slide":
        body += rf"\t(0,{intro_ms},\alpha&H00&\fscx{settings['end_scale']}\fscy{settings['end_scale']})"
    elif settings["animation_type"] == "fade":
        body += rf"\t(0,{intro_ms},\alpha&H00&\fscx{settings['end_scale']}\fscy{settings['end_scale']})"
    elif settings["animation_type"] in ("words", "letters"):
        body += rf"\alpha&H00&\fscx{settings['end_scale']}\fscy{settings['end_scale']}"
        text = _build_karaoke_text(text, settings["animation_type"], intro_ms)
    else:
        body += rf"\alpha&H00&\fscx{settings['end_scale']}\fscy{settings['end_scale']}"

    text = _apply_text_case(text, settings)

    if settings.get("use_background") and int(settings.get("background_pad_x", 0)) > 0:
        hard_spaces = r"\h" * int(settings["background_pad_x"])
        text = f"{hard_spaces}{text}{hard_spaces}"

    if outro_ms > 0:
        if settings["out_mode"] == "move":
            body += rf"\t({exit_start},{duration},\move({pos_x},{pos_y},{end_x},{end_y},{exit_start},{duration})\alpha{settings['end_alpha']})"
        else:
            body += rf"\t({exit_start},{duration},\alpha{settings['end_alpha']})"

    body += "}"
    return body + text






# Build the final ASS file for SRT or VTT using one consistent variation path.
# Color modes and deterministic jitter are applied in the per-line and per-word overrides.
def srt_to_animated_ass(src, dst, settings, video_info):
    def ass_bgr_to_color(value, default="&H00FFFFFF"):
        value = (value or default).strip().upper()
        if value.startswith("&H"):
            value = value[2:]
        value = value.rjust(8, "0")[-8:]

        aa = int(value[0:2], 16)
        bb = int(value[2:4], 16)
        gg = int(value[4:6], 16)
        rr = int(value[6:8], 16)

        return pysubs2.Color(rr, gg, bb, aa)

    ext = os.path.splitext(src)[1].lower()

    bg_alpha_hex = f"{max(0, min(255, round((1 - settings['background_alpha']) * 255))):02X}"
    bg_value = str(settings.get("background_colour", "&H00000000")).strip().upper()
    if bg_value.startswith("&H"):
        bg_value = bg_value[2:]
    bg_value = bg_value.rjust(8, "0")[-8:]

    box_color = ass_bgr_to_color(f"&H{bg_alpha_hex}{bg_value[2:]}")
    borderstyle = 4 if settings.get("use_background") else 1

    if ext == ".vtt":
        subs = pysubs2.SSAFile()
        subs.info["PlayResX"] = str(video_info["width"])
        subs.info["PlayResY"] = str(video_info["height"])
        subs.info["ScaledBorderAndShadow"] = "yes"
        subs.info["WrapStyle"] = "0"

        style = pysubs2.SSAStyle()
        style.fontname = settings["font_name"]
        style.fontsize = settings["font_size"]
        style.primarycolor = ass_bgr_to_color(settings["primary_colour"], "&H00FFFFFF")
        style.outlinecolor = ass_bgr_to_color(settings["outline_colour"], "&H00000000")
        style.backcolor = box_color
        style.bold = settings["bold"]
        style.italic = settings["italic"]
        style.borderstyle = borderstyle
        style.outline = settings["outline"]
        style.shadow = settings["shadow"]
        style.alignment = settings["alignment"]
        style.marginv = settings["margin_v"]
        style.marginl = settings["margin_h"]
        style.marginr = settings["margin_h"]
        subs.styles["Default"] = style

        reveal_offset_ms = int(settings.get("reveal_offset_ms", 0))

        for cue in _parse_vtt_cues(src):
            timed_words = cue["timed_words"]

            if not timed_words:
                subs.events.append(
                    pysubs2.SSAEvent(
                        start=cue["start"],
                        end=cue["end"],
                        text=_build_vtt_word_reveal_text(cue, settings, video_info, [], cue["text"]),
                        style="Default",
                    )
                )
                continue

            past_words = []

            for index, item in enumerate(timed_words):
                timing_jitter = _pick_variant_offset(
                    settings.get("random_timing_jitter"),
                    settings.get("timing_jitter_ms", 0),
                    cue["start"],
                    index,
                    settings.get("variation_seed", 0),
                    211,
                )

                start_ms = max(cue["start"], item["start"] + reveal_offset_ms + timing_jitter)

                if index == len(timed_words) - 1:
                    end_ms = cue["end"]
                else:
                    next_jitter = _pick_variant_offset(
                        settings.get("random_timing_jitter"),
                        settings.get("timing_jitter_ms", 0),
                        cue["start"],
                        index + 1,
                        settings.get("variation_seed", 0),
                        211,
                    )
                    end_ms = min(cue["end"], timed_words[index + 1]["start"] + reveal_offset_ms + next_jitter)

                if end_ms <= start_ms:
                    past_words.append(item["word"])
                    continue

                subs.events.append(
                    pysubs2.SSAEvent(
                        start=start_ms,
                        end=end_ms,
                        text=_build_vtt_word_reveal_text(cue, settings, video_info, past_words, item["word"]),
                        style="Default",
                    )
                )

                past_words.append(item["word"])

        subs.save(dst)
        return

    subs = pysubs2.load(src, encoding="utf-8")

    if "Default" not in subs.styles:
        subs.styles["Default"] = pysubs2.SSAStyle()

    subs.info["PlayResX"] = str(video_info["width"])
    subs.info["PlayResY"] = str(video_info["height"])
    subs.info["ScaledBorderAndShadow"] = "yes"
    subs.info["WrapStyle"] = "0"

    style = subs.styles["Default"]
    style.fontname = settings["font_name"]
    style.fontsize = settings["font_size"]
    style.primarycolor = ass_bgr_to_color(settings["primary_colour"], "&H00FFFFFF")
    style.outlinecolor = ass_bgr_to_color(settings["outline_colour"], "&H00000000")
    style.backcolor = box_color
    style.bold = settings["bold"]
    style.italic = settings["italic"]
    style.borderstyle = borderstyle
    style.outline = settings["outline"]
    style.shadow = settings["shadow"]
    style.alignment = settings["alignment"]
    style.marginv = settings["margin_v"]
    style.marginl = settings["margin_h"]
    style.marginr = settings["margin_h"]
    subs.styles["Default"] = style

    for line_index, line in enumerate(subs):
        text = _escape_ass_text(line.text)
        duration = max(1, int(line.end) - int(line.start))
        line.text = _build_line_override(
            text,
            duration,
            settings,
            video_info,
            cue_start_ms=int(line.start),
            item_index=line_index,
        )

    subs.save(dst)


# Trim and shift ASS events so preview renders start at local time zero while honoring preview_start.
def shift_ass_for_preview(ass_path, preview_start=0, preview_seconds=None):
    subs = pysubs2.load(ass_path, encoding="utf-8")
    start_ms = max(0, int(float(preview_start or 0) * 1000))
    end_ms = None if preview_seconds is None else start_ms + max(1, int(float(preview_seconds) * 1000))

    shifted_events = []

    for event in subs.events:
        if event.end <= start_ms:
          continue

        if end_ms is not None and event.start >= end_ms:
          continue

        new_event = event.copy()
        new_event.start = max(0, event.start - start_ms)
        new_event.end = max(1, event.end - start_ms)

        if end_ms is not None:
            new_event.end = min(new_event.end, max(1, end_ms - start_ms))

        if new_event.end <= new_event.start:
            continue

        shifted_events.append(new_event)

    subs.events = shifted_events
    subs.save(ass_path)








# Build ASS subtitles and render either a bounded preview or a full-length output segment.
def process_preview(job_id, video_path, srt_path, ass_path, preview_path, settings, preview_start=0, preview_seconds=8):
    try:
        JOBS[job_id]["status"] = "preparing"
        JOBS[job_id]["message"] = "Reading video resolution..."
        video_info = get_video_info(video_path)
        source_duration = get_video_duration(video_path)

        JOBS[job_id]["status"] = "preparing"
        JOBS[job_id]["message"] = "Generating animated ASS..."
        srt_to_animated_ass(srt_path, ass_path, settings, video_info)

        if append_text_overlay_to_ass(ass_path, settings.get("overlay"), video_info, source_duration):
            JOBS[job_id]["message"] = "Adding text overlay layer..."

        if preview_start or preview_seconds is not None:
            shift_ass_for_preview(ass_path, preview_start=preview_start, preview_seconds=preview_seconds)

        JOBS[job_id]["status"] = "rendering"
        JOBS[job_id]["message"] = "Rendering video..."
        render_preview(
            video_path,
            ass_path,
            preview_path,
            preview_start=preview_start,
            preview_seconds=preview_seconds,
        )

        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["message"] = "Render ready."
        JOBS[job_id]["preview_file"] = os.path.basename(preview_path)
        JOBS[job_id]["ass_file"] = os.path.basename(ass_path)

    except Exception as exc:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["message"] = str(exc)


@app.route("/")
def index():
    return render_template("index.html")


# Return uploaded custom fonts for the Typography UI.
@app.route("/api/fonts", methods=["GET"])
def api_fonts():
    try:
        uploaded_fonts = _list_custom_fonts()
        system_fonts = _list_system_fonts()

        merged = []
        seen = set()

        for item in uploaded_fonts + system_fonts:
            family = str(item.get("family", "")).strip()
            if not family:
                continue

            key = family.lower()
            if key in seen:
                continue

            seen.add(key)
            merged.append(item)

        merged.sort(key=lambda item: item["family"].lower())

        return jsonify({
            "ok": True,
            "fonts": merged,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# Accept one uploaded custom font file and return the refreshed font registry.
@app.route("/api/fonts/upload", methods=["POST"])
def api_fonts_upload():
    try:
        ensure_dirs()

        font = request.files.get("font")
        if not font or not font.filename:
            return jsonify({"ok": False, "error": "Upload a font file."}), 400

        filename = secure_filename(font.filename)
        if not filename:
            return jsonify({"ok": False, "error": "Invalid font filename."}), 400

        if not _allowed_font_file(filename):
            return jsonify({"ok": False, "error": "Allowed: .ttf, .otf, .woff, .woff2"}), 400

        target_path = os.path.join(FONTS_DIR, filename)

        # Vulnerable block: save only validated font files into the dedicated font directory.
        font.save(target_path)

        uploaded_font = {
            "file": filename,
            "family": _font_family_from_filename(filename),
            "url": f"/fonts/{filename}",
        }

        return jsonify({
            "ok": True,
            "font": uploaded_font,
            "fonts": _list_custom_fonts(),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# Serve uploaded custom font files for browser preview loading.
@app.route("/fonts/<path:filename>")
def serve_font(filename):
    safe_name = secure_filename(filename)
    if not safe_name or not _allowed_font_file(safe_name):
        return jsonify({"ok": False, "error": "Font not found."}), 404

    return send_from_directory(FONTS_DIR, safe_name)






@app.route("/api/preview", methods=["POST"])
def api_preview():
    try:
        import hashlib

        video = request.files.get("video")
        srt = request.files.get("srt")

        if not video or not srt:
            return jsonify({"ok": False, "error": "Upload both video and subtitle file."}), 400

        # Accept source videos including WebM for preview rendering.
        if not video.filename.lower().endswith((".mp4", ".mov", ".m4v", ".webm")):
            return jsonify({"ok": False, "error": "Video must be mp4, mov, m4v, or webm."}), 400

        if not srt.filename.lower().endswith((".srt", ".vtt")):
            return jsonify({"ok": False, "error": "Subtitle file must be .srt or .vtt"}), 400

        mode = request.form.get("mode", "preview")

        # Use configurable preview start and duration from the UI for preview jobs and keep full renders unrestricted.
        preview_start = 0
        preview_seconds = None

        if mode == "preview":
            # Vulnerable block: clamp user-provided preview window before passing it to ffmpeg.
            preview_start = max(0, min(24 * 60 * 60, _safe_float(request.form.get("preview_start", 0), 0)))
            preview_seconds = max(1, min(60, _safe_float(request.form.get("preview_duration", 8), 8)))

        job_id = str(uuid.uuid4())[:8]

        video_bytes = video.read()
        srt_bytes = srt.read()

        video_hash = hashlib.sha1(video_bytes).hexdigest()[:16]
        srt_hash = hashlib.sha1(srt_bytes).hexdigest()[:16]

        video_ext = os.path.splitext(secure_filename(video.filename))[1].lower() or ".mp4"
        srt_ext = os.path.splitext(secure_filename(srt.filename))[1].lower() or ".srt"

        video_name = f"src_{video_hash}{video_ext}"
        srt_name = f"src_{srt_hash}{srt_ext}"
        ass_name = f"{job_id}.ass"

        # Build output filename for the queued render job.
        output_name = f"{job_id}_{'preview' if mode == 'preview' else 'full'}.mp4"        

        video_path = os.path.join(UPLOAD_DIR, video_name)
        srt_path = os.path.join(UPLOAD_DIR, srt_name)
        ass_path = os.path.join(OUTPUT_DIR, ass_name)
        output_path = os.path.join(OUTPUT_DIR, output_name)

        if not os.path.exists(video_path):
            with open(video_path, "wb") as f:
                f.write(video_bytes)

        if not os.path.exists(srt_path):
            with open(srt_path, "wb") as f:
                f.write(srt_bytes)





        settings = {
            "word_display_mode": request.form.get("word_display_mode", "phrase"),
            "font_name": request.form.get("font_name", "Arial"),
            "font_size": _safe_int(request.form.get("font_size", 24), 24),
            "outline": _safe_float(request.form.get("outline", 2), 2),
            "shadow": _safe_float(request.form.get("shadow", 0), 0),
            "blur": _safe_float(request.form.get("blur", 0.8), 0.8),
            "margin_v": _safe_int(request.form.get("margin_v", 60), 60),
            "margin_h": _safe_int(request.form.get("margin_h", 40), 40),
            "alignment": _safe_int(request.form.get("alignment", 2), 2),
            "bold": _safe_bool(request.form.get("bold", "0")),
            "italic": _safe_bool(request.form.get("italic", "0")),
            "all_caps": _safe_bool(request.form.get("all_caps", "0")),

            "primary_colour": _hex_to_ass_bgr(request.form.get("primary_colour", "#FFFFFF"), "00"),
            "primary_colour_mode": request.form.get("primary_colour_mode", "fixed"),
            "primary_palette": request.form.get("primary_palette", ""),

            "active_word_colour": _hex_to_ass_bgr(request.form.get("active_word_colour", "#ff0000"), "00"),
            "active_word_colour_mode": request.form.get("active_word_colour_mode", "fixed"),
            "active_palette": request.form.get("active_palette", ""),
            "active_word_lead_ms": _safe_int(request.form.get("active_word_lead_ms", 80), 80),

            "outline_colour": _hex_to_ass_bgr(request.form.get("outline_colour", "#000000"), "00"),
            "shadow_colour": _hex_to_ass_bgr(request.form.get("shadow_colour", "#000000"), "00"),

            "background_colour": _hex_to_ass_bgr(request.form.get("background_colour", "#000000"), "00"),
            "background_colour_mode": request.form.get("background_colour_mode", "fixed"),
            "background_palette": request.form.get("background_palette", ""),
            "use_background": _safe_bool(request.form.get("use_background", "0")),
            "background_alpha": _safe_float(request.form.get("background_alpha", 0.45), 0.45),
            "background_pad_x": _safe_int(request.form.get("background_pad_x", 10), 10),

            "random_position_jitter": _safe_bool(request.form.get("random_position_jitter", "0")),
            "position_jitter_x": _safe_int(request.form.get("position_jitter_x", 0), 0),
            "position_jitter_y": _safe_int(request.form.get("position_jitter_y", 0), 0),

            "random_timing_jitter": _safe_bool(request.form.get("random_timing_jitter", "0")),
            "timing_jitter_ms": _safe_int(request.form.get("timing_jitter_ms", 0), 0),
            "variation_seed": _safe_int(request.form.get("variation_seed", 7), 7),

            "animation_type": request.form.get("animation_type", "fade"),
            "intro_ms": _safe_int(request.form.get("intro_ms", 180), 180),
            "outro_ms": _safe_int(request.form.get("outro_ms", 120), 120),
            "start_scale": _safe_int(request.form.get("start_scale", 100), 100),
            "end_scale": _safe_int(request.form.get("end_scale", 100), 100),
            "start_alpha": f"&H{request.form.get('start_alpha', '99')}&",
            "mid_alpha": f"&H{request.form.get('mid_alpha', '44')}&",
            "end_alpha": f"&H{request.form.get('end_alpha', '66')}&",
            "anchor_x": _safe_float(request.form.get("anchor_x", 0.5), 0.5),
            "anchor_y": _safe_float(request.form.get("anchor_y", 0.9), 0.9),
            "offset_x": _safe_int(request.form.get("offset_x", 0), 0),
            "offset_y": _safe_int(request.form.get("offset_y", 0), 0),
            "in_offset_x": _safe_int(request.form.get("in_offset_x", 0), 0),
            "in_offset_y": _safe_int(request.form.get("in_offset_y", 18), 18),
            "out_offset_x": _safe_int(request.form.get("out_offset_x", 0), 0),
            "out_offset_y": _safe_int(request.form.get("out_offset_y", 0), 0),
            "overshoot_amount": _safe_int(request.form.get("overshoot_amount", 8), 8),
            "overshoot_ms": _safe_int(request.form.get("overshoot_ms", 90), 90),
            "settle_ms": _safe_int(request.form.get("settle_ms", 90), 90),
            "rotation_z": _safe_float(request.form.get("rotation_z", 0), 0),
            "letter_spacing": _safe_float(request.form.get("letter_spacing", 0), 0),
            "reveal_offset_ms": _safe_int(request.form.get("reveal_offset_ms", 0), 0),
            "out_mode": request.form.get("out_mode", "fade"),
            "overlay": _build_overlay_settings_from_form(request.form),
        }




        JOBS[job_id] = {
            "status": "queued",
            "message": "Queued...",
            "preview_file": None,
            "ass_file": None,
            "phase": None,
            "progress_current": None,
            "progress_total": None,
            "eta_seconds": None,
        }

        threading.Thread(
                    target=process_preview,
                    args=(job_id, video_path, srt_path, ass_path, output_path, settings),
                    kwargs={
                        "preview_start": preview_start,
                        "preview_seconds": preview_seconds,
                    },
                    daemon=True,
                ).start()

        return jsonify({"ok": True, "job_id": job_id})

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# Start a silence-chop or speed-processing job for the current source video.
# Preview mode reuses the same preview_start and preview_duration values as caption preview.
@app.route("/api/process_video", methods=["POST"])
def api_process_video():
    try:
        import hashlib

        video = request.files.get("video")
        if not video:
            return jsonify({"ok": False, "error": "Upload a video first."}), 400

        # Accept source videos including WebM for silence-chop and speed-processing jobs.
        if not video.filename.lower().endswith((".mp4", ".mov", ".m4v", ".webm")):
            return jsonify({"ok": False, "error": "Video must be mp4, mov, m4v, or webm."}), 400

        process_kind = request.form.get("process_kind", "silence")
        if process_kind not in ("silence", "speed", "upscale"):
            return jsonify({"ok": False, "error": "Invalid processing mode."}), 400

        mode = request.form.get("mode", "preview")

        preview_start = 0
        preview_seconds = None
        if mode == "preview":
            preview_start = max(0, min(24 * 60 * 60, _safe_float(request.form.get("preview_start", 0), 0)))
            preview_seconds = max(1, min(60, _safe_float(request.form.get("preview_duration", 8), 8)))

        job_id = str(uuid.uuid4())[:8]

        video_bytes = video.read()
        video_hash = hashlib.sha1(video_bytes).hexdigest()[:16]
        video_ext = os.path.splitext(secure_filename(video.filename))[1].lower() or ".mp4"
        video_name = f"src_{video_hash}{video_ext}"

        output_name = f"{job_id}_{process_kind}_{'preview' if mode == 'preview' else 'full'}.mp4"
        video_path = os.path.join(UPLOAD_DIR, video_name)
        output_path = os.path.join(OUTPUT_DIR, output_name)

        if not os.path.exists(video_path):
            with open(video_path, "wb") as f:
                f.write(video_bytes)

        settings = {
            "silence_threshold": _safe_float(request.form.get("silence_threshold", -40), -40),
            "min_silence_duration": _safe_float(request.form.get("min_silence_duration", 0.4), 0.4),
            "silence_mode": request.form.get("silence_mode", "talk"),
            "speed_factor": _safe_float(request.form.get("speed_factor", 1.25), 1.25),
            "upscale_factor": 4 if int(_safe_float(request.form.get("upscale_factor", 2), 2)) >= 4 else 2,
            "upscale_mode": "ai" if request.form.get("upscale_mode", "traditional") == "ai" else "traditional",
            "overlay": _build_overlay_settings_from_form(request.form),
        }

        JOBS[job_id] = {
            "status": "queued",
            "message": "Queued...",
            "preview_file": None,
            "ass_file": None,
            "phase": None,
            "progress_current": None,
            "progress_total": None,
            "eta_seconds": None,
        }

        threading.Thread(
            target=process_video_job,
            args=(job_id, video_path, output_path, process_kind, settings),
            kwargs={
                "preview_start": preview_start,
                "preview_seconds": preview_seconds,
            },
            daemon=True,
        ).start()

        return jsonify({"ok": True, "job_id": job_id})

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500



@app.route("/api/get_captions", methods=["POST"])
def api_get_captions():
    try:
        import hashlib

        video = request.files.get("video")
        if not video:
            return jsonify({"ok": False, "error": "Upload a video first."}), 400

        # Accept source videos including WebM for caption extraction jobs.
        if not video.filename.lower().endswith((".mp4", ".mov", ".m4v", ".webm")):
            return jsonify({"ok": False, "error": "Video must be mp4, mov, m4v, or webm."}), 400

        job_id = str(uuid.uuid4())[:8]

        video_bytes = video.read()
        video_hash = hashlib.sha1(video_bytes).hexdigest()[:16]
        video_ext = os.path.splitext(secure_filename(video.filename))[1].lower() or ".mp4"

        video_name = f"src_{video_hash}{video_ext}"
        captions_name = f"{job_id}_captions.vtt"

        video_path = os.path.join(UPLOAD_DIR, video_name)
        captions_path = os.path.join(OUTPUT_DIR, captions_name)

        if not os.path.exists(video_path):
            with open(video_path, "wb") as handle:
                handle.write(video_bytes)

        settings = {
            "caption_model": request.form.get("caption_model", "small"),
            "caption_language": (request.form.get("caption_language", "") or "").strip(),
            "caption_word_timestamps": _safe_bool(request.form.get("caption_word_timestamps", "1"), True),
            "caption_vad_filter": _safe_bool(request.form.get("caption_vad_filter", "1"), True),
        }

        JOBS[job_id] = {
            "status": "queued",
            "message": "Queued...",
            "preview_file": None,
            "ass_file": None,
            "captions_file": None,
            "captions_text": None,
            "phase": None,
            "progress_current": None,
            "progress_total": None,
            "eta_seconds": None,
        }

        threading.Thread(
            target=transcribe_captions_job,
            args=(job_id, video_path, captions_path, settings),
            daemon=True,
        ).start()

        return jsonify({"ok": True, "job_id": job_id})

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500




@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404

    progress_current = job.get("progress_current")
    progress_total = job.get("progress_total")
    progress_percent = None

    if isinstance(progress_current, int) and isinstance(progress_total, int) and progress_total > 0:
        progress_percent = max(0.0, min(100.0, (progress_current / progress_total) * 100.0))

    return jsonify({
        "ok": True,
        "status": job["status"],
        "message": job["message"],
        "phase": job.get("phase"),
        "progress_current": progress_current,
        "progress_total": progress_total,
        "progress_percent": progress_percent,
        "eta_seconds": job.get("eta_seconds"),
        "preview_url": f"/outputs/{job['preview_file']}" if job.get("preview_file") else None,
        "ass_url": f"/outputs/{job['ass_file']}" if job.get("ass_file") else None,
        "captions_url": f"/outputs/{job['captions_file']}" if job.get("captions_file") else None,
        "captions_filename": job.get("captions_file"),
        "captions_text": job.get("captions_text"),
    })


@app.route("/outputs/<path:filename>")
def serve_output(filename):
    return send_from_directory(OUTPUT_DIR, filename)


if __name__ == "__main__":
    ensure_dirs()
    app.run(host="127.0.0.1", port=5151, debug=True, threaded=False, use_reloader=True)