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
import hashlib


import platform
import pysubs2
from fontTools.ttLib import TTFont

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(APP_ROOT, "assets")
UPLOAD_DIR = os.path.join(APP_ROOT, "uploads")
OUTPUT_DIR = os.path.join(APP_ROOT, "outputs")
REVEAL_DIR = os.path.join(OUTPUT_DIR, "revealed_assets")
GLOBAL_OVERLAY_CACHE_DIR = os.path.join(OUTPUT_DIR, "global_overlay_cache")
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
ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}


# Ensure all runtime directories exist, including the custom font store.
def ensure_dirs():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    icons_dir = os.path.join(ASSETS_DIR, "images", "icons")
    os.makedirs(icons_dir, exist_ok=True)
    os.makedirs(os.path.join(ASSETS_DIR, "fonts"), exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(REVEAL_DIR, exist_ok=True)
    os.makedirs(GLOBAL_OVERLAY_CACHE_DIR, exist_ok=True)
    os.makedirs(FONTS_DIR, exist_ok=True)
    os.makedirs(TOOLS_DIR, exist_ok=True)
    os.makedirs(REALESRGAN_DIR, exist_ok=True)

    default_icons = {
        "volume.svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9v6h4l5 4V5L8 9H4Z"/><path d="M16 8.5a5 5 0 0 1 0 7"/><path d="M18.5 6a8.5 8.5 0 0 1 0 12"/></svg>',
        "headphone.svg": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14v-2a8 8 0 0 1 16 0v2"/><path d="M6 14h2a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2Z"/><path d="M16 14h2a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2h-2a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2Z"/></svg>',
    }
    for filename, svg in default_icons.items():
        icon_path = os.path.join(icons_dir, filename)
        if not os.path.exists(icon_path):
            with open(icon_path, "w", encoding="utf-8") as handle:
                handle.write(svg)


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
def render_preview(video_path, ass_path, output_path, preview_start=0, preview_seconds=8, aspect_settings=None, global_overlay_settings=None):
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

        ass_filter = None
        if ass_path:
            ass_filter = (
                f"ass='{_escape_ffmpeg_filter_path(ass_path)}'"
                f":fontsdir='{_escape_ffmpeg_filter_path(temp_fonts_dir)}'"
            )

        source_info = get_video_info(video_path)
        overlay_png_path = None
        extra_input_args = []
        use_global_overlay = _global_overlay_layer_enabled(global_overlay_settings)
        output_video_info = _video_info_after_aspect(source_info, aspect_settings) if _aspect_layer_enabled(aspect_settings) else source_info

        if use_global_overlay:
            overlay_duration = max(0.1, requested_end - requested_start) if preview_seconds is not None else source_duration
            if _global_overlay_uses_bitmap(global_overlay_settings):
                overlay_png_path = os.path.join(OUTPUT_DIR, f"global_overlay_{uuid.uuid4().hex[:12]}.png")
                _create_global_overlay_png(global_overlay_settings, output_video_info["width"], output_video_info["height"], overlay_png_path)
                extra_input_args = ["-loop", "1", "-i", overlay_png_path]
            else:
                extra_input_args = _build_solid_global_overlay_input_args(
                    global_overlay_settings,
                    output_video_info["width"],
                    output_video_info["height"],
                    overlay_duration,
                )

        video_filters = []
        if preview_seconds is not None:
            video_filters.append(f"trim=start={requested_start:.6f}:end={requested_end:.6f}")
            video_filters.append("setpts=PTS-STARTPTS")
        else:
            video_filters.append("setpts=PTS-STARTPTS")

        if _aspect_layer_enabled(aspect_settings):
            video_filters.append(_build_aspect_pad_filter(aspect_settings, source_info))

        filter_parts = [f"[0:v]{','.join(video_filters)}[basev]"]
        caption_input_label = "basev"

        if use_global_overlay:
            _append_global_overlay_filter(filter_parts, "basev", "gradedv", "1:v", global_overlay_settings)
            caption_input_label = "gradedv"

        if ass_filter:
            filter_parts.append(f"[{caption_input_label}]{ass_filter}[v]")
        else:
            filter_parts.append(f"[{caption_input_label}]null[v]")

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
            *extra_input_args,
            "-filter_complex", filter_complex,
            "-map", "[v]",
        ]

        if has_audio:
            cmd += ["-map", "[a]"]

        cmd += [
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "veryfast" if preview_seconds is not None else "medium",
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
            if 'overlay_png_path' in locals() and overlay_png_path:
                os.remove(overlay_png_path)
        except Exception:
            pass
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
    """Build one ASS event for the current caption cue.

    Browser preview shows the full phrase and changes only the active word colour.
    The burned-in render must match that behavior exactly. Older versions used this
    function as a word-reveal renderer, hiding future words or stopping at the
    active word. That made the export look different from the WYSIWYG overlay.
    """
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

    # Phrase mode: always show the entire phrase. Only the active word changes colour.
    # The active word is selected by timed-word index, not by string matching, so repeated
    # words and punctuation are rendered consistently with the browser overlay.
    tokens = re.split(r"(\s+)", transformed_text)
    parts = []
    word_index = 0
    has_active_word = bool(str(transformed_active_word or "").strip())

    for token in tokens:
        if token == "":
            continue

        if token.isspace():
            parts.append(token.replace("\n", r"\N"))
            continue

        escaped_word = _escape_ass_text(token)

        if has_active_word and word_index == current_index:
            colour = _pick_variant_colour(
                settings.get("active_word_colour_mode", "fixed"),
                settings["active_word_colour"],
                settings.get("active_palette", ""),
                cue["start"],
                word_index,
                settings.get("variation_seed", 0),
                31,
            )
        else:
            colour = _pick_variant_colour(
                settings.get("primary_colour_mode", "fixed"),
                settings["primary_colour"],
                settings.get("primary_palette", ""),
                cue["start"],
                word_index,
                settings.get("variation_seed", 0),
                17,
            )

        parts.append("{" + rf"\1c{colour}" + "}" + escaped_word)
        word_index += 1

    return prefix + hard_spaces + "".join(parts) + hard_spaces


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


def _parse_aspect_ratio(value, default="9:16"):
    raw_value = str(value or default).strip()
    if raw_value.lower() in ("original", "source", "none"):
        raw_value = default

    match = re.fullmatch(r"(\d{1,4})\s*:\s*(\d{1,4})", raw_value)
    if not match:
        raise RuntimeError("Aspect ratio must be like 1:1, 9:16, 4:5, or 16:9.")

    ratio_w = int(match.group(1))
    ratio_h = int(match.group(2))
    if ratio_w <= 0 or ratio_h <= 0:
        raise RuntimeError("Aspect ratio numbers must be greater than zero.")

    return ratio_w, ratio_h, f"{ratio_w}:{ratio_h}"


def _normalise_ffmpeg_colour(value, default="black"):
    raw_value = str(value or default).strip()
    if not raw_value:
        raw_value = default

    if re.fullmatch(r"#[0-9A-Fa-f]{6}", raw_value):
        return f"0x{raw_value[1:]}"

    if re.fullmatch(r"0x[0-9A-Fa-f]{6}", raw_value):
        return raw_value

    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", raw_value):
        return raw_value.lower()

    raise RuntimeError("Border colour must be a hex colour like #ff0000 or an FFmpeg colour name like black or white.")


def _normalise_aspect_size_mode(value):
    mode = str(value or "none").strip().lower()
    if mode in ("width", "height", "custom", "none"):
        return mode
    return "none"


def _safe_aspect_dimension(value, default_value):
    size = _safe_int(value, default_value)
    size = max(2, min(7680, size))
    return _even_size(size)


def _safe_aspect_nudge(value):
    nudge = _safe_int(value, 0)
    return max(-1000, min(1000, nudge))


def _check_aspect_canvas_limits(width, height):
    width = _even_size(width)
    height = _even_size(height)
    if width > 7680 or height > 7680:
        raise RuntimeError("Computed aspect canvas is larger than 7680px on one side. Use a smaller output width/height or a less extreme ratio.")
    return width, height


def _aspect_canvas_dimensions(source_info, aspect_settings):
    ratio_w, ratio_h, _ = _parse_aspect_ratio(aspect_settings.get("ratio", "9:16"))
    mode = _normalise_aspect_size_mode(aspect_settings.get("size_mode", "none"))

    if mode == "width":
        base_w = _safe_aspect_dimension(aspect_settings.get("target_width", 1080), 1080)
        base_h = _even_size(base_w * ratio_h / ratio_w)
        allow_scale = True
    elif mode == "height":
        base_h = _safe_aspect_dimension(aspect_settings.get("target_height", 1920), 1920)
        base_w = _even_size(base_h * ratio_w / ratio_h)
        allow_scale = True
    elif mode == "custom":
        base_w = _safe_aspect_dimension(aspect_settings.get("target_width", 1080), 1080)
        base_h = _safe_aspect_dimension(aspect_settings.get("target_height", 1920), 1920)
        allow_scale = True
    else:
        source_w = _even_size(source_info.get("width", 2))
        source_h = _even_size(source_info.get("height", 2))

        if source_w / source_h > ratio_w / ratio_h:
            base_w = source_w
            base_h = _even_size(source_w * ratio_h / ratio_w)
        else:
            base_w = _even_size(source_h * ratio_w / ratio_h)
            base_h = source_h
        allow_scale = False

    width_nudge = _safe_aspect_nudge(aspect_settings.get("width_nudge", 0))
    height_nudge = _safe_aspect_nudge(aspect_settings.get("height_nudge", 0))

    canvas_w = _even_size(base_w + width_nudge)
    canvas_h = _even_size(base_h + height_nudge)

    if not allow_scale:
        canvas_w = max(canvas_w, _even_size(source_info.get("width", 2)))
        canvas_h = max(canvas_h, _even_size(source_info.get("height", 2)))

    return _check_aspect_canvas_limits(canvas_w, canvas_h)


def _build_aspect_pad_filter(aspect_settings, source_info=None):
    ratio_w, ratio_h, _ = _parse_aspect_ratio(aspect_settings.get("ratio", "9:16"))
    pad_colour = _normalise_ffmpeg_colour(aspect_settings.get("border_colour", "black"), "black")
    mode = _normalise_aspect_size_mode(aspect_settings.get("size_mode", "none"))
    width_nudge = _safe_aspect_nudge(aspect_settings.get("width_nudge", 0))
    height_nudge = _safe_aspect_nudge(aspect_settings.get("height_nudge", 0))

    if mode in ("width", "height", "custom"):
        canvas_w, canvas_h = _aspect_canvas_dimensions(source_info or {"width": 2, "height": 2}, aspect_settings)
        return (
            f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2:flags=lanczos,"
            f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:{pad_colour},setsar=1"
        )

    if source_info and (width_nudge or height_nudge):
        canvas_w, canvas_h = _aspect_canvas_dimensions(source_info, aspect_settings)
        return f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:{pad_colour},setsar=1"

    return (
        f"pad='if(gt(iw/ih,{ratio_w}/{ratio_h}),iw,ceil(ih*{ratio_w}/{ratio_h}/2)*2)':"
        f"'if(gt(iw/ih,{ratio_w}/{ratio_h}),ceil(iw*{ratio_h}/{ratio_w}/2)*2,ih)':"
        f"(ow-iw)/2:(oh-ih)/2:{pad_colour},setsar=1"
    )


def _build_aspect_settings_from_form(form):
    # The browser UI keeps these as live canvas sliders. Older templates may still send aspect_target_* only.
    target_width = form.get("aspect_canvas_width", form.get("aspect_target_width", 1080))
    target_height = form.get("aspect_canvas_height", form.get("aspect_target_height", 1920))

    return {
        "enabled": _safe_bool(form.get("aspect_enabled", "0")),
        "ratio": form.get("aspect_ratio", "9:16"),
        "border_colour": form.get("aspect_border_colour", "#000000"),
        "size_mode": _normalise_aspect_size_mode(form.get("aspect_size_mode", "none")),
        "target_width": _safe_aspect_dimension(target_width, 1080),
        "target_height": _safe_aspect_dimension(target_height, 1920),
        "width_nudge": _safe_aspect_nudge(form.get("aspect_width_nudge", 0)),
        "height_nudge": _safe_aspect_nudge(form.get("aspect_height_nudge", 0)),
    }


def _aspect_layer_enabled(aspect_settings):
    return bool(aspect_settings and aspect_settings.get("enabled"))


_GLOBAL_OVERLAY_BLEND_MODES = {
    "normal",
    "multiply",
    "screen",
    "overlay",
    "darken",
    "lighten",
    "addition",
    "softlight",
    "hardlight",
    "difference",
    "exclusion",
}


def _safe_hex_colour(value, default="#000000"):
    raw_value = str(value or default).strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", raw_value):
        return raw_value.upper()
    if re.fullmatch(r"[0-9A-Fa-f]{6}", raw_value):
        return f"#{raw_value.upper()}"
    return default.upper()


def _normalise_global_overlay_kind(value):
    kind = str(value or "solid").strip().lower()
    return kind if kind in ("solid", "linear", "radial") else "solid"


def _normalise_global_overlay_blend(value):
    mode = str(value or "normal").strip().lower().replace(" ", "")
    return mode if mode in _GLOBAL_OVERLAY_BLEND_MODES else "normal"


def _parse_global_overlay_stops(stops_json, fallback_a="#000000", fallback_b="#FFFFFF"):
    raw_items = []
    try:
        parsed = json.loads(stops_json or "[]")
        if isinstance(parsed, list):
            raw_items = parsed
    except Exception:
        raw_items = []

    stops = []
    for item in raw_items[:16]:
        if not isinstance(item, dict):
            continue
        position = max(0.0, min(1.0, _safe_float(item.get("position", 0), 0)))
        colour = _safe_hex_colour(item.get("colour", item.get("color", fallback_a)), fallback_a)
        stops.append({"position": position, "colour": colour})

    if not stops:
        stops = [
            {"position": 0.0, "colour": _safe_hex_colour(fallback_a, "#000000")},
            {"position": 1.0, "colour": _safe_hex_colour(fallback_b, "#FFFFFF")},
        ]

    stops.sort(key=lambda item: item["position"])

    if stops[0]["position"] > 0:
        stops.insert(0, {"position": 0.0, "colour": stops[0]["colour"]})
    if stops[-1]["position"] < 1:
        stops.append({"position": 1.0, "colour": stops[-1]["colour"]})

    return stops


def _build_global_overlay_settings_from_form(form):
    return {
        "enabled": _safe_bool(form.get("global_overlay_enabled", "0")),
        "kind": _normalise_global_overlay_kind(form.get("global_overlay_kind", "solid")),
        "blend_mode": _normalise_global_overlay_blend(form.get("global_overlay_blend_mode", "normal")),
        "opacity": max(0.0, min(1.0, _safe_float(form.get("global_overlay_opacity", 0.25), 0.25))),
        "solid_colour": _safe_hex_colour(form.get("global_overlay_solid_colour", "#000000"), "#000000"),
        "angle": _safe_float(form.get("global_overlay_angle", 0), 0),
        "radial_x": max(0.0, min(1.0, _safe_float(form.get("global_overlay_radial_x", 0.5), 0.5))),
        "radial_y": max(0.0, min(1.0, _safe_float(form.get("global_overlay_radial_y", 0.5), 0.5))),
        "stops": _parse_global_overlay_stops(
            form.get("global_overlay_stops_json", ""),
            form.get("global_overlay_solid_colour", "#000000"),
            form.get("global_overlay_stop_colour", "#FFFFFF"),
        ),
    }


def _global_overlay_layer_enabled(settings):
    return bool(settings and settings.get("enabled") and float(settings.get("opacity", 0)) > 0)


# Return True only for overlay types that require a generated bitmap asset.
def _global_overlay_uses_bitmap(settings):
    return _normalise_global_overlay_kind((settings or {}).get("kind", "solid")) != "solid"


# Build a lavfi colour source for solid global overlays without generating a PNG file.
def _build_solid_global_overlay_input_args(settings, width, height, duration):
    blend_mode = _normalise_global_overlay_blend((settings or {}).get("blend_mode", "normal"))
    opacity = max(0.0, min(1.0, float((settings or {}).get("opacity", 0.25))))
    colour = _safe_hex_colour((settings or {}).get("solid_colour", "#000000"), "#000000").replace("#", "0x")
    alpha_suffix = f"@{opacity:.6f}" if blend_mode == "normal" else ""
    safe_width = _even_size(max(2, min(7680, int(width))))
    safe_height = _even_size(max(2, min(7680, int(height))))
    safe_duration = max(0.1, float(duration or 0.1))

    return [
        "-f", "lavfi",
        "-i", f"color=c={colour}{alpha_suffix}:s={safe_width}x{safe_height}:d={safe_duration:.6f},format=rgba",
    ]


def _hex_to_rgb_tuple(value):
    value = _safe_hex_colour(value, "#000000").lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _interpolate_rgb(a, b, t):
    t = max(0.0, min(1.0, float(t)))
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _colour_for_position(stops, position):
    position = max(0.0, min(1.0, float(position)))
    if position <= stops[0]["position"]:
        return _hex_to_rgb_tuple(stops[0]["colour"])
    for idx in range(1, len(stops)):
        left = stops[idx - 1]
        right = stops[idx]
        if position <= right["position"]:
            span = max(0.000001, right["position"] - left["position"])
            local_t = (position - left["position"]) / span
            return _interpolate_rgb(_hex_to_rgb_tuple(left["colour"]), _hex_to_rgb_tuple(right["colour"]), local_t)
    return _hex_to_rgb_tuple(stops[-1]["colour"])


def _global_overlay_cache_key(settings, width, height):
    safe_settings = {
        "kind": _normalise_global_overlay_kind(settings.get("kind", "solid")),
        "blend_mode": _normalise_global_overlay_blend(settings.get("blend_mode", "normal")),
        "opacity": round(max(0.0, min(1.0, float(settings.get("opacity", 0.25)))), 6),
        "solid_colour": _safe_hex_colour(settings.get("solid_colour", "#000000"), "#000000"),
        "angle": round(_safe_float(settings.get("angle", 0), 0), 6),
        "radial_x": round(max(0.0, min(1.0, _safe_float(settings.get("radial_x", 0.5), 0.5))), 6),
        "radial_y": round(max(0.0, min(1.0, _safe_float(settings.get("radial_y", 0.5), 0.5))), 6),
        "stops": settings.get("stops") or [],
        "width": int(width),
        "height": int(height),
    }
    payload = json.dumps(safe_settings, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _create_global_overlay_png(settings, width, height, output_path):
    """Create a cached RGBA bitmap for gradient global overlays.

    Solid overlays use an FFmpeg lavfi colour source. This function is reserved for
    linear and radial gradients that need an image-backed overlay input.
    """
    from PIL import Image
    import math

    width = _even_size(max(2, min(7680, int(width))))
    height = _even_size(max(2, min(7680, int(height))))
    kind = _normalise_global_overlay_kind(settings.get("kind", "solid"))
    opacity = max(0.0, min(1.0, float(settings.get("opacity", 0.25))))
    blend_mode = _normalise_global_overlay_blend(settings.get("blend_mode", "normal"))
    alpha = 255 if blend_mode != "normal" else int(round(opacity * 255))

    if kind == "solid":
        raise RuntimeError("Solid global overlays must use the FFmpeg colour source path.")

    ensure_dirs()
    cache_key = _global_overlay_cache_key(settings, width, height)
    cache_path = os.path.join(GLOBAL_OVERLAY_CACHE_DIR, f"{cache_key}.png")
    if os.path.exists(cache_path):
        shutil.copy2(cache_path, output_path)
        return output_path

    stops = settings.get("stops") or _parse_global_overlay_stops("", "#000000", "#FFFFFF")

    try:
        import numpy as np

        positions = np.array([max(0.0, min(1.0, float(stop["position"]))) for stop in stops], dtype=np.float32)
        colours = np.array([_hex_to_rgb_tuple(stop["colour"]) for stop in stops], dtype=np.float32)

        if kind == "radial":
            yy, xx = np.ogrid[0:height, 0:width]
            cx = max(0.0, min(1.0, float(settings.get("radial_x", 0.5)))) * max(1, width - 1)
            cy = max(0.0, min(1.0, float(settings.get("radial_y", 0.5)))) * max(1, height - 1)
            corners = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
            max_dist = max(math.hypot(x - cx, y - cy) for x, y in corners) or 1
            t = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / max_dist
        else:
            yy, xx = np.mgrid[0:height, 0:width]
            # Match CSS linear-gradient angle semantics: 0deg points upward, 90deg points right.
            angle = math.radians(float(settings.get("angle", 0)))
            dx = math.sin(angle)
            dy = -math.cos(angle)
            corners = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
            projections = [x * dx + y * dy for x, y in corners]
            min_p = min(projections)
            span = (max(projections) - min_p) or 1
            t = ((xx * dx + yy * dy) - min_p) / span

        t = np.clip(t, 0.0, 1.0)
        channels = [np.interp(t, positions, colours[:, channel]) for channel in range(3)]
        alpha_channel = np.full_like(t, alpha, dtype=np.float32)
        rgba = np.dstack([*channels, alpha_channel]).clip(0, 255).astype(np.uint8)
        image = Image.fromarray(rgba, "RGBA")
        image.save(cache_path)
        shutil.copy2(cache_path, output_path)
        return output_path

    except Exception:
        # Fallback for environments without NumPy. This is slower but keeps the feature working.
        image = Image.new("RGBA", (width, height))
        pixels = image.load()

        if kind == "radial":
            cx = max(0.0, min(1.0, float(settings.get("radial_x", 0.5)))) * max(1, width - 1)
            cy = max(0.0, min(1.0, float(settings.get("radial_y", 0.5)))) * max(1, height - 1)
            corners = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
            max_dist = max(math.hypot(x - cx, y - cy) for x, y in corners) or 1
            for y in range(height):
                for x in range(width):
                    t = math.hypot(x - cx, y - cy) / max_dist
                    pixels[x, y] = (*_colour_for_position(stops, t), alpha)
        else:
            # Match CSS linear-gradient angle semantics: 0deg points upward, 90deg points right.
            angle = math.radians(float(settings.get("angle", 0)))
            dx = math.sin(angle)
            dy = -math.cos(angle)
            corners = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
            projections = [x * dx + y * dy for x, y in corners]
            min_p = min(projections)
            span = (max(projections) - min_p) or 1
            for y in range(height):
                base = y * dy
                for x in range(width):
                    t = ((x * dx + base) - min_p) / span
                    pixels[x, y] = (*_colour_for_position(stops, t), alpha)

        image.save(cache_path)
        shutil.copy2(cache_path, output_path)
        return output_path

def _append_global_overlay_filter(filter_parts, input_label, output_label, image_label, settings):
    blend_mode = _normalise_global_overlay_blend(settings.get("blend_mode", "normal"))
    opacity = max(0.0, min(1.0, float(settings.get("opacity", 0.25))))

    base_rgba = f"{output_label}_base_rgba"
    overlay_rgba = f"{output_label}_overlay_rgba"
    filter_parts.append(f"[{input_label}]format=rgba[{base_rgba}]")
    filter_parts.append(f"[{image_label}]format=rgba[{overlay_rgba}]")

    if blend_mode == "normal":
        # Normal mode uses input alpha so CSS opacity and FFmpeg overlay match.
        filter_parts.append(f"[{base_rgba}][{overlay_rgba}]overlay=0:0:shortest=1,format=yuv420p[{output_label}]")
    else:
        # Non-normal modes use FFmpeg blend opacity in RGBA space.
        filter_parts.append(
            f"[{base_rgba}][{overlay_rgba}]blend=all_mode={blend_mode}:all_opacity={opacity:.6f}:shortest=1,format=yuv420p[{output_label}]"
        )

def apply_global_overlay_to_video(video_path, output_path, global_overlay_settings, preview_seconds=None, job_id=None):
    if not _global_overlay_layer_enabled(global_overlay_settings):
        shutil.copy2(video_path, output_path)
        return

    info = get_video_info(video_path)
    duration = get_video_duration(video_path)
    overlay_png_path = None
    extra_input_args = []

    try:
        if _global_overlay_uses_bitmap(global_overlay_settings):
            overlay_png_path = os.path.join(OUTPUT_DIR, f"global_overlay_{uuid.uuid4().hex[:12]}.png")
            _create_global_overlay_png(global_overlay_settings, info["width"], info["height"], overlay_png_path)
            extra_input_args = ["-loop", "1", "-i", overlay_png_path]
        else:
            extra_input_args = _build_solid_global_overlay_input_args(
                global_overlay_settings,
                info["width"],
                info["height"],
                duration,
            )

        filter_parts = []
        _append_global_overlay_filter(filter_parts, "0:v", "v", "1:v", global_overlay_settings)
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-nostdin",
            "-hwaccel", "none",
            "-i", video_path,
            *extra_input_args,
            "-filter_complex", ";".join(filter_parts),
            "-map", "[v]",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "veryfast" if preview_seconds is not None else "medium",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-shortest",
            "-movflags", "+faststart",
            output_path,
        ]
        _run_job_subprocess(cmd, job_id=job_id, failure_message="Global colour overlay failed")
    finally:
        try:
            if overlay_png_path:
                os.remove(overlay_png_path)
        except Exception:
            pass

def _video_info_after_aspect(source_info, aspect_settings):
    if not _aspect_layer_enabled(aspect_settings):
        return dict(source_info)

    width, height = _aspect_canvas_dimensions(source_info, aspect_settings)
    return {
        "width": int(width),
        "height": int(height),
    }


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




def _append_job_log(job_id, message):
    """Keep a compact server-side timeline for a running job."""
    if not job_id or job_id not in JOBS:
        return
    text = str(message or "").strip()
    if not text:
        return
    entry = {
        "time": time.strftime("%H:%M:%S"),
        "message": text[-600:],
    }
    logs = JOBS[job_id].setdefault("logs", [])
    logs.append(entry)
    del logs[:-80]


def _job_cancel_requested(job_id):
    return bool(job_id and JOBS.get(job_id, {}).get("cancel_requested"))


def _raise_if_job_cancelled(job_id):
    if _job_cancel_requested(job_id):
        raise RuntimeError("Job cancelled by user.")


def _run_job_subprocess(cmd, job_id=None, failure_message="FFmpeg command failed"):
    """Run a subprocess while streaming stderr into job logs and allowing cancellation."""
    started_at = time.monotonic()
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    if job_id and job_id in JOBS:
        JOBS[job_id]["process"] = process
        _append_job_log(job_id, "Started: " + " ".join(str(part) for part in cmd[:6]) + (" ..." if len(cmd) > 6 else ""))

    stderr_lines = []

    try:
        while True:
            _raise_if_job_cancelled(job_id)

            line = process.stderr.readline() if process.stderr else ""
            if line:
                clean_line = line.strip()
                if clean_line:
                    stderr_lines.append(clean_line)
                    if len(stderr_lines) > 30:
                        stderr_lines = stderr_lines[-30:]
                    if job_id and (
                        "frame=" in clean_line
                        or "time=" in clean_line
                        or "speed=" in clean_line
                        or "error" in clean_line.lower()
                        or "warning" in clean_line.lower()
                    ):
                        _append_job_log(job_id, clean_line)

            if process.poll() is not None:
                break

            if job_id and int(time.monotonic() - started_at) % 5 == 0:
                JOBS[job_id]["eta_seconds"] = None

            time.sleep(0.02)

        if process.stdout:
            process.stdout.read()

        if process.returncode != 0:
            raise RuntimeError("\n".join(stderr_lines[-12:]) or failure_message)

        if job_id:
            _append_job_log(job_id, "Finished subprocess successfully.")

    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
        raise
    finally:
        if job_id and job_id in JOBS and JOBS[job_id].get("process") is process:
            JOBS[job_id]["process"] = None


def _video_has_audio(video_path):
    """Return True when the source has at least one audio stream."""
    result = subprocess.run(
        [
            FFPROBE_BIN,
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=index",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool((result.stdout or "").strip())


def _safe_json_list(value):
    try:
        parsed = json.loads(value or "[]")
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _allowed_audio_file(filename):
    ext = os.path.splitext(filename or "")[1].lower()
    return ext in ALLOWED_AUDIO_EXTENSIONS


# Parse extra audio-track mixer settings from a request form.
def _build_audio_mix_settings_from_form(form):
    tracks = []
    for index, item in enumerate(_safe_json_list(form.get("audio_tracks_json", ""))):
        if not isinstance(item, dict):
            continue
        tracks.append({
            "index": index,
            "name": str(item.get("name", f"Audio {index + 1}") or f"Audio {index + 1}"),
            "volume": max(0.0, min(3.0, _safe_float(item.get("volume", 1.0), 1.0))),
            "pan": max(-1.0, min(1.0, _safe_float(item.get("pan", 0.0), 0.0))),
        })

    return {
        "enabled": _safe_bool(form.get("audio_mix_enabled", "0")),
        "source_volume": max(0.0, min(3.0, _safe_float(form.get("audio_source_volume", 1.0), 1.0))),
        "tracks": tracks,
    }


# Save uploaded extra audio tracks and return paths aligned with audio mixer track metadata.
def _save_audio_track_uploads(files, job_id):
    audio_paths = []
    for index, audio_file in enumerate(files or []):
        if not audio_file or not audio_file.filename:
            continue
        if not _allowed_audio_file(audio_file.filename):
            raise RuntimeError("Audio files must be mp3, wav, m4a, aac, flac, ogg, or opus.")

        audio_bytes = audio_file.read()
        audio_hash = hashlib.sha1(audio_bytes).hexdigest()[:16]
        audio_ext = os.path.splitext(secure_filename(audio_file.filename))[1].lower() or ".m4a"
        audio_name = f"audio_{job_id}_{index}_{audio_hash}{audio_ext}"
        audio_path = os.path.join(UPLOAD_DIR, audio_name)
        if not os.path.exists(audio_path):
            with open(audio_path, "wb") as handle:
                handle.write(audio_bytes)
        audio_paths.append(audio_path)
    return audio_paths


# Mix additional uploaded audio tracks with the rendered video audio while preserving video duration.
def mix_audio_into_video(video_path, audio_paths, output_path, audio_settings=None, job_id=None):
    settings = audio_settings or {}
    tracks = list(settings.get("tracks") or [])
    paths = list(audio_paths or [])

    if not settings.get("enabled") or not paths or not tracks:
        shutil.copy2(video_path, output_path)
        return False

    duration = max(0.1, get_video_duration(video_path))
    has_source_audio = _video_has_audio(video_path)
    source_volume = max(0.0, min(3.0, float(settings.get("source_volume", 1.0))))

    input_args = ["-i", video_path]
    for audio_path in paths:
        input_args += ["-stream_loop", "-1", "-i", audio_path]

    filter_parts = []
    mix_inputs = []

    if has_source_audio:
        filter_parts.append(f"[0:a]aformat=channel_layouts=stereo,volume={source_volume:.6f}[basea]")
    else:
        filter_parts.append(f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={duration:.6f}[basea]")
    mix_inputs.append("[basea]")

    for index, audio_path in enumerate(paths):
        track = tracks[index] if index < len(tracks) else {}
        volume = max(0.0, min(3.0, float(track.get("volume", 1.0))))
        pan = max(-1.0, min(1.0, float(track.get("pan", 0.0))))
        left_gain = 1.0 if pan <= 0 else max(0.0, 1.0 - pan)
        right_gain = 1.0 if pan >= 0 else max(0.0, 1.0 + pan)
        input_index = index + 1
        filter_parts.append(
            f"[{input_index}:a]atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,"
            f"aformat=channel_layouts=stereo,volume={volume:.6f},"
            f"pan=stereo|c0={left_gain:.6f}*c0|c1={right_gain:.6f}*c1[aextra{index}]"
        )
        mix_inputs.append(f"[aextra{index}]")

    filter_parts.append("".join(mix_inputs) + f"amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=0,alimiter=limit=0.98[aout]")

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-nostdin",
        *input_args,
        "-filter_complex", ";".join(filter_parts),
        "-map", "0:v:0",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-t", f"{duration:.6f}",
        "-movflags", "+faststart",
        output_path,
    ]

    _run_job_subprocess(cmd, job_id=job_id, failure_message="Audio mix failed")
    return True


def _build_trim_settings_from_form(form):
    segments = []
    for item in _safe_json_list(form.get("trim_segments_json", "")):
        if not isinstance(item, dict):
            continue
        start = max(0.0, _safe_float(item.get("start", 0), 0))
        end = max(start + 0.05, _safe_float(item.get("end", start + 0.05), start + 0.05))
        segments.append({"start": start, "end": end})

    if not segments:
        start = max(0.0, _safe_float(form.get("trim_start", 0), 0))
        end = max(start + 0.05, _safe_float(form.get("trim_end", 0), 0))
        if end > start + 0.05:
            segments.append({"start": start, "end": end})

    segments.sort(key=lambda item: item["start"])
    return {"segments": segments}


def _build_grading_settings_from_form(form):
    vignette_strength = form.get("grading_vignette_strength", form.get("grading_vignette", 0.0))
    return {
        "preset": str(form.get("grading_preset", "none") or "none"),
        "tint_colour": _safe_hex_colour(form.get("grading_tint_colour", "#f59e0b"), "#f59e0b"),
        "tint_strength": max(0.0, min(1.0, _safe_float(form.get("grading_tint_strength", 0.0), 0.0))),
        "contrast": max(-1.0, min(3.0, _safe_float(form.get("grading_contrast", 1.0), 1.0))),
        "saturation": max(0.0, min(3.0, _safe_float(form.get("grading_saturation", 1.0), 1.0))),
        "exposure": max(-2.0, min(2.0, _safe_float(form.get("grading_exposure", 0.0), 0.0))),
        "sharpness": max(0.0, min(3.0, _safe_float(form.get("grading_sharpness", 0.0), 0.0))),
        "blur": max(0.0, min(20.0, _safe_float(form.get("grading_blur", 0.0), 0.0))),
        "vignette_strength": max(0.0, min(1.0, _safe_float(vignette_strength, 0.0))),
        "vignette_colour": _safe_hex_colour(form.get("grading_vignette_colour", "#000000"), "#000000"),
        "vignette_radius": max(0.05, min(1.2, _safe_float(form.get("grading_vignette_radius", 0.56), 0.56))),
        "vignette_feather": max(0.02, min(1.2, _safe_float(form.get("grading_vignette_feather", 0.38), 0.38))),
        "vignette_center_x": max(-0.5, min(1.5, _safe_float(form.get("grading_vignette_center_x", 0.5), 0.5))),
        "vignette_center_y": max(-0.5, min(1.5, _safe_float(form.get("grading_vignette_center_y", 0.5), 0.5))),
    }


def process_trim_video(video_path, output_path, trim_settings, preview_start=0, preview_seconds=None, job_id=None):
    """Render one or more kept ranges from the selected source video into a new asset."""
    segments = list((trim_settings or {}).get("segments") or [])
    if not segments:
        raise RuntimeError("Add at least one trim range before rendering.")

    duration = get_video_duration(video_path)
    clean_segments = []
    for item in segments:
        start = max(0.0, min(duration, float(item.get("start", 0))))
        end = max(start + 0.05, min(duration, float(item.get("end", start + 0.05))))
        if end > start:
            clean_segments.append({"start": start, "end": end})

    if not clean_segments:
        raise RuntimeError("Trim ranges are outside the video duration.")

    has_audio = _video_has_audio(video_path)
    filter_parts = []
    concat_inputs = []

    for index, segment in enumerate(clean_segments):
        filter_parts.append(
            f"[0:v]trim=start={segment['start']:.6f}:end={segment['end']:.6f},setpts=PTS-STARTPTS[v{index}]"
        )
        concat_inputs.append(f"[v{index}]")
        if has_audio:
            filter_parts.append(
                f"[0:a]atrim=start={segment['start']:.6f}:end={segment['end']:.6f},asetpts=PTS-STARTPTS[a{index}]"
            )
            concat_inputs.append(f"[a{index}]")

    filter_parts.append(
        "".join(concat_inputs) + f"concat=n={len(clean_segments)}:v=1:a={1 if has_audio else 0}[vout]" + ("[aout]" if has_audio else "")
    )

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-nostdin",
        "-hwaccel", "none",
        "-i", video_path,
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]",
    ]

    if has_audio:
        cmd += ["-map", "[aout]"]

    cmd += [
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "veryfast" if preview_seconds is not None else "medium",
        "-pix_fmt", "yuv420p",
    ]

    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]

    if preview_seconds is not None:
        cmd += ["-t", f"{max(0.1, float(preview_seconds)):.6f}"]

    cmd += ["-movflags", "+faststart", output_path]
    _run_job_subprocess(cmd, job_id=job_id, failure_message="Trim render failed")


def _grading_filter(settings):
    preset = str((settings or {}).get("preset", "none") or "none")
    filters = []

    if preset == "black_white":
        filters.append("hue=s=0")
    elif preset == "invert":
        filters.append("negate")
    elif preset == "sepia":
        filters.append("colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131")
    elif preset == "tint":
        rr, gg, bb = _hex_to_rgb_tuple((settings or {}).get("tint_colour", "#f59e0b"))
        strength = max(0.0, min(1.0, float((settings or {}).get("tint_strength", 0.35))))
        rs = ((rr / 255.0) - 0.5) * strength
        gs = ((gg / 255.0) - 0.5) * strength
        bs = ((bb / 255.0) - 0.5) * strength
        filters.append(f"colorbalance=rs={rs:.6f}:gs={gs:.6f}:bs={bs:.6f}")

    contrast = float((settings or {}).get("contrast", 1.0))
    saturation = float((settings or {}).get("saturation", 1.0))
    exposure = float((settings or {}).get("exposure", 0.0))
    if abs(contrast - 1.0) > 0.001 or abs(saturation - 1.0) > 0.001 or abs(exposure) > 0.001:
        filters.append(f"eq=contrast={contrast:.6f}:saturation={saturation:.6f}:brightness={exposure / 4.0:.6f}")

    sharpness = float((settings or {}).get("sharpness", 0.0))
    if sharpness > 0.001:
        amount = min(5.0, sharpness * 0.9)
        filters.append(f"unsharp=5:5:{amount:.6f}:5:5:0.0")

    blur = float((settings or {}).get("blur", 0.0))
    if blur > 0.001:
        radius = max(1, min(20, int(round(blur))))
        filters.append(f"boxblur={radius}:1")

    return ",".join(filters) if filters else "null"


def process_grading_video(video_path, output_path, grading_settings, preview_start=0, preview_seconds=None, job_id=None):
    """Apply non-destructive colour, lens, sharpness, blur, and soft vignette grading into a new project video."""
    source_meta = get_video_stream_meta(video_path)
    source_duration = get_video_duration(video_path)
    render_start = max(0.0, float(preview_start or 0.0)) if preview_seconds is not None else 0.0
    render_duration = max(0.1, float(preview_seconds)) if preview_seconds is not None else source_duration
    vignette_strength = max(0.0, min(1.0, float((grading_settings or {}).get("vignette_strength", 0.0))))

    base_filters = []
    if preview_seconds is not None:
        base_filters.append(f"trim=start={render_start:.6f}:duration={render_duration:.6f}")
        base_filters.append("setpts=PTS-STARTPTS")

    grade_filter = _grading_filter(grading_settings)
    if grade_filter:
        base_filters.append(grade_filter)

    if vignette_strength > 0.001:
        width = max(2, int(source_meta.get("width") or 2))
        height = max(2, int(source_meta.get("height") or 2))
        fps = str(source_meta.get("fps") or "30")
        vignette_hex = _safe_hex_colour((grading_settings or {}).get("vignette_colour", "#000000"), "#000000").lstrip("#")
        vignette_colour = f"0x{vignette_hex}"
        radius = max(0.05, min(1.2, float((grading_settings or {}).get("vignette_radius", 0.56))))
        feather = max(0.02, min(1.2, float((grading_settings or {}).get("vignette_feather", 0.38))))
        center_x = max(-0.5, min(1.5, float((grading_settings or {}).get("vignette_center_x", 0.5))))
        center_y = max(-0.5, min(1.5, float((grading_settings or {}).get("vignette_center_y", 0.5))))
        distance_expression = f"sqrt(((X/W)-{center_x:.6f})*((X/W)-{center_x:.6f})+((Y/H)-{center_y:.6f})*((Y/H)-{center_y:.6f}))"
        alpha_expression = f"255*{vignette_strength:.6f}*min(max((({distance_expression})-{radius:.6f})/{feather:.6f}\\,0)\\,1)"
        base_chain = ",".join(base_filters) if base_filters else "null"
        filter_complex = (
            f"[0:v]{base_chain},format=rgba[basev];"
            f"[1:v]format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha_expression}'[vig];"
            f"[basev][vig]overlay=shortest=1:format=auto,format=yuv420p[vout]"
        )
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-nostdin",
            "-hwaccel", "none",
            "-i", video_path,
            "-f", "lavfi",
            "-i", f"color=c={vignette_colour}:s={width}x{height}:r={fps}:d={render_duration:.6f}",
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "0:a:0?",
        ]
    else:
        cmd = [
            FFMPEG_BIN,
            "-y",
            "-nostdin",
            "-hwaccel", "none",
            "-i", video_path,
            "-vf", ",".join(base_filters) if base_filters else grade_filter,
            "-map", "0:v:0",
            "-map", "0:a:0?",
        ]

    if preview_seconds is not None:
        cmd += ["-af", f"atrim=start={render_start:.6f}:duration={render_duration:.6f},asetpts=PTS-STARTPTS"]

    cmd += [
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "veryfast" if preview_seconds is not None else "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_job_subprocess(cmd, job_id=job_id, failure_message="Grading render failed")

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
    # Backwards-compatible wrapper. The UI now calls this "Video Scale" because
    # FFmpeg/Lanczos can downscale as well as upscale. AI mode remains limited to
    # Real-ESRGAN's supported integer upscales.
    source_meta = get_video_stream_meta(video_path)
    raw_factor = _safe_float(upscale_factor, 1.0)
    scale_factor = max(0.05, min(8.0, raw_factor))
    upscale_mode = "ai" if str(upscale_mode or "traditional").lower() == "ai" else "traditional"

    if upscale_mode == "ai":
        if scale_factor < 1.5:
            raise RuntimeError("AI scale only supports upscaling. Use FFmpeg Lanczos for 1x or downscaling.")
        upscale_factor = 4 if scale_factor >= 3 else 2
    else:
        upscale_factor = scale_factor

    target_width = _even_size(source_meta["width"] * upscale_factor)
    target_height = _even_size(source_meta["height"] * upscale_factor)

    if upscale_mode == "traditional":
        direction = "Downscaling" if upscale_factor < 1 else "Scaling"
        _set_job_progress(job_id, "rendering", f"{direction} video {upscale_factor:g}x...")
        vf_expr = f"scale={target_width}:{target_height}:flags=lanczos:param0=3,setsar=1"

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
            "-preset", "veryfast" if preview_seconds is not None else "medium",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            output_path,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Video scale failed")
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

        _set_job_progress(job_id, "rendering", "Encoding scaled video...", phase="encoding", current=None, total=None, eta_seconds=None)

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
            raise RuntimeError(encode_result.stderr.strip() or 'Could not encode scaled video')


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
        "-preset", "veryfast" if preview_seconds is not None else "medium",
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
        "-preset", "veryfast" if preview_seconds is not None else "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Speed processing failed")


# Render a padded canvas around the source video to fit a selected aspect ratio.
# This is the Flask equivalent of the standalone vid2.sh padding workflow, with safer server-side validation.
def process_aspect_ratio_video(video_path, output_path, aspect_settings, preview_start=0, preview_seconds=None):
    source_info = get_video_info(video_path)
    vf_expr = _build_aspect_pad_filter(aspect_settings, source_info)

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
        "-preset", "veryfast" if preview_seconds is not None else "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Aspect-ratio conversion failed")


def _safe_crop_dimension(value, fallback=0):
    size = _safe_int(value, fallback)
    if size <= 0:
        return 0
    return _even_size(max(2, min(7680, size)))


def _safe_crop_offset(value):
    offset = _safe_int(value, 0)
    if offset <= 0:
        return 0
    return _even_size(max(0, min(7680, offset)))


def _safe_crop_anchor(value):
    value = str(value or "center").strip().lower().replace("-", "_")
    allowed = {"center", "top_left", "top_right", "bottom_right", "bottom_left", "custom"}
    return value if value in allowed else "center"


def _build_crop_settings_from_form(form):
    return {
        "width": _safe_crop_dimension(form.get("crop_width", 0), 0),
        "height": _safe_crop_dimension(form.get("crop_height", 0), 0),
        "x": _safe_crop_offset(form.get("crop_x", 0)),
        "y": _safe_crop_offset(form.get("crop_y", 0)),
        "anchor": _safe_crop_anchor(form.get("crop_anchor", "center")),
    }


def _resolve_crop_box(source_info, crop_settings):
    source_w = _even_size(source_info.get("width", 2))
    source_h = _even_size(source_info.get("height", 2))

    crop_w = crop_settings.get("width") or source_w
    crop_h = crop_settings.get("height") or source_h
    crop_w = _even_size(max(2, min(source_w, crop_w)))
    crop_h = _even_size(max(2, min(source_h, crop_h)))

    anchor = _safe_crop_anchor(crop_settings.get("anchor", "center"))
    if anchor == "top_left":
        crop_x, crop_y = 0, 0
    elif anchor == "top_right":
        crop_x, crop_y = source_w - crop_w, 0
    elif anchor == "bottom_right":
        crop_x, crop_y = source_w - crop_w, source_h - crop_h
    elif anchor == "bottom_left":
        crop_x, crop_y = 0, source_h - crop_h
    elif anchor == "custom":
        crop_x = _even_size(crop_settings.get("x", 0))
        crop_y = _even_size(crop_settings.get("y", 0))
    else:
        crop_x = _even_size((source_w - crop_w) / 2)
        crop_y = _even_size((source_h - crop_h) / 2)

    crop_x = _even_size(max(0, min(source_w - crop_w, crop_x)))
    crop_y = _even_size(max(0, min(source_h - crop_h, crop_y)))
    return crop_x, crop_y, crop_w, crop_h


def process_crop_video(video_path, output_path, crop_settings, preview_start=0, preview_seconds=None):
    source_info = get_video_info(video_path)
    crop_x, crop_y, crop_w, crop_h = _resolve_crop_box(source_info, crop_settings)
    vf_expr = f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},setsar=1"

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
        "-preset", "veryfast" if preview_seconds is not None else "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Video crop failed")


# Run one post-processing job and publish the resulting video through the existing job polling flow.
# Reuses the app’s preview_url player loading instead of inventing a second status system.
def process_video_job(job_id, video_path, output_path, process_kind, settings, preview_start=0, preview_seconds=None):
    try:
        _append_job_log(job_id, f"Queued {process_kind} job.")
        _raise_if_job_cancelled(job_id)

        if process_kind == "silence":
            _set_job_progress(job_id, status="preparing", message="Analyzing silence...", phase="silence")
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
            _set_job_progress(job_id, status="rendering", message="Processing video speed...", phase="speed")
            process_speed_video(
                video_path,
                output_path,
                speed_factor=settings["speed_factor"],
                preview_start=preview_start,
                preview_seconds=preview_seconds,
            )
        elif process_kind == "aspect":
            _set_job_progress(job_id, status="rendering", message="Converting aspect ratio...", phase="aspect")
            process_aspect_ratio_video(
                video_path,
                output_path,
                aspect_settings=settings["aspect"],
                preview_start=preview_start,
                preview_seconds=preview_seconds,
            )
        elif process_kind in ("upscale", "scale"):
            _set_job_progress(job_id, status="preparing", message="Preparing video scale...", phase="scale")
            process_upscale_video(
                video_path,
                output_path,
                upscale_factor=settings["upscale_factor"],
                upscale_mode=settings["upscale_mode"],
                preview_start=preview_start,
                preview_seconds=preview_seconds,
                job_id=job_id,
            )
        elif process_kind == "crop":
            _set_job_progress(job_id, status="rendering", message="Cropping video...", phase="crop")
            process_crop_video(
                video_path,
                output_path,
                crop_settings=settings["crop"],
                preview_start=preview_start,
                preview_seconds=preview_seconds,
            )
        elif process_kind == "trim":
            _set_job_progress(job_id, status="rendering", message="Trimming selected ranges...", phase="trim")
            process_trim_video(
                video_path,
                output_path,
                trim_settings=settings["trim"],
                preview_start=preview_start,
                preview_seconds=preview_seconds,
                job_id=job_id,
            )
        elif process_kind == "grade":
            _set_job_progress(job_id, status="rendering", message="Applying grading...", phase="grading")
            process_grading_video(
                video_path,
                output_path,
                grading_settings=settings["grading"],
                preview_start=preview_start,
                preview_seconds=preview_seconds,
                job_id=job_id,
            )
        else:
            raise RuntimeError("Unsupported processing mode.")

        _raise_if_job_cancelled(job_id)

        aspect_settings = settings.get("aspect")
        if process_kind != "aspect" and _aspect_layer_enabled(aspect_settings):
            _set_job_progress(job_id, status="rendering", message="Applying Aspect Canvas...", phase="aspect")
            aspect_output_path = os.path.join(OUTPUT_DIR, f"{job_id}_{process_kind}_aspect.mp4")
            process_aspect_ratio_video(
                output_path,
                aspect_output_path,
                aspect_settings=aspect_settings,
                preview_start=0,
                preview_seconds=None,
            )
            _raise_if_job_cancelled(job_id)
            os.replace(aspect_output_path, output_path)

        global_overlay_settings = settings.get("global_overlay")
        if _global_overlay_layer_enabled(global_overlay_settings):
            _set_job_progress(job_id, status="rendering", message="Applying global colour overlay...", phase="global_overlay")
            graded_output_path = os.path.join(OUTPUT_DIR, f"{job_id}_{process_kind}_global_overlay.mp4")
            apply_global_overlay_to_video(output_path, graded_output_path, global_overlay_settings, preview_seconds=preview_seconds, job_id=job_id)
            _raise_if_job_cancelled(job_id)
            os.replace(graded_output_path, output_path)

        overlay_ass_name = None
        overlay_settings = settings.get("overlay")

        if _overlay_layer_enabled(overlay_settings):
            _set_job_progress(job_id, status="rendering", message="Burning text overlay...", phase="text_overlay")

            overlay_ass_path = os.path.join(OUTPUT_DIR, f"{job_id}_{process_kind}_overlay.ass")
            final_output_path = os.path.join(OUTPUT_DIR, f"{job_id}_{process_kind}_with_overlay.mp4")
            processed_video_info = get_video_info(output_path)
            processed_duration = get_video_duration(output_path)

            create_text_overlay_ass(overlay_ass_path, overlay_settings, processed_video_info, processed_duration)
            render_preview(output_path, overlay_ass_path, final_output_path, preview_start=0, preview_seconds=None)
            _raise_if_job_cancelled(job_id)
            os.replace(final_output_path, output_path)
            overlay_ass_name = os.path.basename(overlay_ass_path)

        audio_mix_settings = settings.get("audio_mix")
        audio_paths = list(settings.get("audio_paths") or [])
        if audio_mix_settings and audio_paths and audio_mix_settings.get("enabled"):
            _set_job_progress(job_id, status="rendering", message="Mixing additional audio...", phase="audio_mix")
            mixed_output_path = os.path.join(OUTPUT_DIR, f"{job_id}_{process_kind}_audio_mix.mp4")
            mix_audio_into_video(output_path, audio_paths, mixed_output_path, audio_mix_settings, job_id=job_id)
            _raise_if_job_cancelled(job_id)
            os.replace(mixed_output_path, output_path)

        _set_job_progress(job_id, status="done", message="Processed video ready.", phase="done", current=None, total=None, eta_seconds=None)
        JOBS[job_id]["preview_file"] = os.path.basename(output_path)
        JOBS[job_id]["ass_file"] = overlay_ass_name
        _append_job_log(job_id, "Output ready: " + os.path.basename(output_path))

    except Exception as exc:
        if str(exc) == "Job cancelled by user.":
            _set_job_progress(job_id, status="cancelled", message="Job cancelled.", phase="cancelled", current=None, total=None, eta_seconds=None)
            _append_job_log(job_id, "Cancelled by user.")
        else:
            _set_job_progress(job_id, status="error", message=str(exc), phase="error", current=None, total=None, eta_seconds=None)
            _append_job_log(job_id, "Error: " + str(exc))





# Transcribe one video file into WEBVTT using faster-whisper.
# Generates normal segment cues or word-timestamp cues for the existing VTT parser.
def transcribe_video_to_vtt(
    video_path,
    model_name="small",
    language=None,
    word_timestamps=True,
    vad_filter=True,
    chunk_max_words=4,
    chunk_min_words=1,
    chunk_max_seconds=2.2,
    chunk_min_seconds=0.25,
    chunk_max_chars=42,
    chunk_split_at_punctuation=True,
    chunk_punctuation=".?!…",
    chunk_split_on_gap=True,
    chunk_gap_seconds=0.55,
    chunk_words_per_line=0,
    chunk_max_lines=2,
):
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

    def _clamp_int(value, default_value, min_value, max_value):
        try:
            parsed = int(float(value))
        except Exception:
            parsed = default_value
        return max(min_value, min(max_value, parsed))

    def _clamp_float(value, default_value, min_value, max_value):
        try:
            parsed = float(value)
        except Exception:
            parsed = default_value
        return max(min_value, min(max_value, parsed))

    max_words = _clamp_int(chunk_max_words, 4, 1, 30)
    min_words = _clamp_int(chunk_min_words, 1, 1, max_words)
    max_seconds = _clamp_float(chunk_max_seconds, 2.2, 0.3, 12.0)
    min_seconds = _clamp_float(chunk_min_seconds, 0.25, 0.0, max_seconds)
    max_chars = _clamp_int(chunk_max_chars, 42, 8, 180)
    split_punctuation_enabled = bool(chunk_split_at_punctuation)
    punctuation_chars = str(chunk_punctuation or ".?!…")
    split_gap_enabled = bool(chunk_split_on_gap)
    gap_seconds = _clamp_float(chunk_gap_seconds, 0.55, 0.05, 3.0)
    words_per_line = _clamp_int(chunk_words_per_line, 0, 0, 12)
    max_lines = _clamp_int(chunk_max_lines, 2, 1, 4)

    def _word_dict(text_value, start_value, end_value):
        return {
            "text": _clean_text(text_value),
            "start": max(0.0, float(start_value or 0.0)),
            "end": max(float(start_value or 0.0) + 0.01, float(end_value or (float(start_value or 0.0) + 0.01))),
        }

    def _fallback_words(text_value, start_value, end_value):
        tokens = [_clean_text(item) for item in re.findall(r"\S+", str(text_value or ""))]
        tokens = [item for item in tokens if item]
        if not tokens:
            return []

        duration = max(0.05, float(end_value or start_value) - float(start_value or 0.0))
        step = duration / len(tokens)
        result = []
        for index, token in enumerate(tokens):
            word_start = float(start_value or 0.0) + index * step
            word_end = float(start_value or 0.0) + (index + 1) * step
            result.append(_word_dict(token, word_start, word_end))
        return result

    def _words_from_segment(segment, start_value, end_value, text_value):
        source_words = list(getattr(segment, "words", []) or [])
        words = []

        for index, word in enumerate(source_words):
            word_text = _clean_text(getattr(word, "word", ""))
            if not word_text:
                continue

            word_start = float(getattr(word, "start", start_value) or start_value)
            raw_end = getattr(word, "end", None)
            if raw_end is None and index + 1 < len(source_words):
                raw_end = getattr(source_words[index + 1], "start", None)
            word_end = float(raw_end or min(end_value, word_start + 0.25))
            words.append(_word_dict(word_text, word_start, min(end_value, max(word_start + 0.01, word_end))))

        return words or _fallback_words(text_value, start_value, end_value)

    def _should_end_chunk(chunk_words, next_word=None, force=False):
        if force:
            return True

        if not chunk_words:
            return False

        chunk_text = " ".join(item["text"] for item in chunk_words).strip()
        chunk_duration = max(0.0, chunk_words[-1]["end"] - chunk_words[0]["start"])
        has_min_words = len(chunk_words) >= min_words
        has_min_seconds = chunk_duration >= min_seconds

        hard_limit = (
            len(chunk_words) >= max_words
            or len(chunk_text) >= max_chars
            or chunk_duration >= max_seconds
        )
        if hard_limit and has_min_words:
            return True

        if not (has_min_words and has_min_seconds):
            return False

        if split_punctuation_enabled and chunk_words[-1]["text"].rstrip().endswith(tuple(punctuation_chars)):
            return True

        if split_gap_enabled and next_word:
            gap = max(0.0, float(next_word["start"]) - float(chunk_words[-1]["end"]))
            if gap >= gap_seconds:
                return True

        return False

    def _split_into_chunks(words):
        chunks = []
        current = []

        for index, word in enumerate(words):
            current.append(word)
            next_word = words[index + 1] if index + 1 < len(words) else None

            if _should_end_chunk(current, next_word=next_word, force=next_word is None):
                chunks.append(current)
                current = []

        if current:
            chunks.append(current)

        return chunks

    def _format_cue_text(chunk_words):
        if not word_timestamps:
            return _wrap_plain_cue_text(" ".join(item["text"] for item in chunk_words))

        parts = []
        for index, item in enumerate(chunk_words):
            if index == 0:
                prefix = ""
            elif words_per_line and (index % words_per_line == 0) and ((index // words_per_line) < max_lines):
                prefix = "\n"
            else:
                prefix = " "
            parts.append(f"<{_vtt_ts(item['start'])}><c>{prefix}{item['text']}</c>")
        return "".join(parts)

    def _wrap_plain_cue_text(text_value):
        words = re.findall(r"\S+", str(text_value or ""))
        if not words_per_line or not words:
            return " ".join(words)

        lines = []
        line = []
        for word in words:
            line.append(word)
            if len(line) >= words_per_line and len(lines) < max_lines - 1:
                lines.append(" ".join(line))
                line = []
        if line:
            lines.append(" ".join(line))
        return "\n".join(lines)

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

            segment_words = _words_from_segment(segment, start_value, end_value, text_value)
            if not segment_words:
                continue

            for chunk_words in _split_into_chunks(segment_words):
                cue_start = max(start_value, min(item["start"] for item in chunk_words))
                cue_end = min(end_value, max(item["end"] for item in chunk_words))
                cue_end = max(cue_start + 0.01, cue_end)
                cue_text = _format_cue_text(chunk_words)

                if not _clean_text(re.sub(r"<[^>]+>", "", cue_text)):
                    continue

                lines.append(f"{_vtt_ts(cue_start)} --> {_vtt_ts(cue_end)}")
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
            chunk_max_words=settings["caption_chunk_max_words"],
            chunk_min_words=settings["caption_chunk_min_words"],
            chunk_max_seconds=settings["caption_chunk_max_seconds"],
            chunk_min_seconds=settings["caption_chunk_min_seconds"],
            chunk_max_chars=settings["caption_chunk_max_chars"],
            chunk_split_at_punctuation=settings["caption_chunk_split_at_punctuation"],
            chunk_punctuation=settings["caption_chunk_punctuation"],
            chunk_split_on_gap=settings["caption_chunk_split_on_gap"],
            chunk_gap_seconds=settings["caption_chunk_gap_seconds"],
            chunk_words_per_line=settings["caption_chunk_words_per_line"],
            chunk_max_lines=settings["caption_chunk_max_lines"],
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





# Split caption text into display words for estimated word timing when exact word timestamps are not available.
def _caption_words_for_estimated_timing(text):
    return re.findall(r"\S+", str(text or "").replace("\r", " ").replace("\n", " "))


# Build a deterministic per-word timeline across a cue. This restores active-word colouring
# for edited/SRT captions where the browser editor no longer has exact Whisper/VTT word timestamps.
def _build_estimated_timed_words_for_cue(cue):
    words = _caption_words_for_estimated_timing(cue.get("text", ""))
    if not words:
        return []

    start = int(cue.get("start") or 0)
    end = max(start + 1, int(cue.get("end") or start + 1))
    duration = max(1, end - start)

    timed_words = []
    for index, word in enumerate(words):
        word_start = start + int(round((duration * index) / len(words)))
        timed_words.append({
            "start": max(start, min(end - 1, word_start)),
            "word": word,
        })

    return timed_words


# Normalize exact word timings before building active-word ASS events.
# Whisper/VTT cues that start at 00:00:00 can sometimes contain several words with the
# same timestamp (often 0 ms). If we use those duplicated timestamps directly, all early
# word windows collapse and the last word can stay highlighted for the whole first cue.
def _normalise_timed_words_for_render(cue, timed_words):
    cue_start = int(cue.get("start") or 0)
    cue_end = max(cue_start + 1, int(cue.get("end") or cue_start + 1))

    cleaned = []
    for item in list(timed_words or []):
        word = str(item.get("word", "") if isinstance(item, dict) else "").strip()
        if not word:
            continue

        try:
            # Do not use `or cue_start` here: an actual timestamp of 0 is valid.
            raw_start = int(float(item.get("start"))) if item.get("start") is not None else cue_start
        except Exception:
            raw_start = cue_start

        cleaned.append({
            "start": max(cue_start, min(cue_end - 1, raw_start)),
            "word": word,
        })

    if not cleaned:
        return _build_estimated_timed_words_for_cue(cue)

    if len(cleaned) == 1:
        return cleaned

    starts = [item["start"] for item in cleaned]
    unique_starts = len(set(starts))
    positive_steps = sum(1 for left, right in zip(starts, starts[1:]) if right > left)

    # If the timing data is mostly flat/non-increasing, the render would collapse into
    # one long event. Fall back to even timing across the cue so phrase-mode colouring
    # still moves from word to word and matches the expected preview behaviour.
    if unique_starts <= 1 or positive_steps < max(1, len(cleaned) // 3):
        return _build_estimated_timed_words_for_cue({
            **cue,
            "text": " ".join(item["word"] for item in cleaned),
        })

    return cleaned


# Build monotonic active-word windows for one caption cue.
# This keeps phrase mode as a full visible phrase while changing only the active word.
def _build_active_word_windows(cue, timed_words, settings):
    cue_start = int(cue.get("start") or 0)
    cue_end = max(cue_start + 1, int(cue.get("end") or cue_start + 1))
    timed_words = _normalise_timed_words_for_render(cue, timed_words)

    if not timed_words:
        return []

    if len(timed_words) == 1:
        return [(cue_start, cue_end, 0, timed_words[0])]

    reveal_offset_ms = int(settings.get("reveal_offset_ms", 0))
    active_lead_ms = int(settings.get("active_word_lead_ms", 0))

    def build_thresholds(use_active_lead):
        thresholds = []
        for index, item in enumerate(timed_words):
            timing_jitter = _pick_variant_offset(
                settings.get("random_timing_jitter"),
                settings.get("timing_jitter_ms", 0),
                cue_start,
                index,
                settings.get("variation_seed", 0),
                211,
            )
            raw_start = int(item.get("start") if item.get("start") is not None else cue_start)
            threshold = raw_start + reveal_offset_ms + timing_jitter

            # The first word should own the beginning of the cue. Applying active lead
            # before 00:00:00 is what caused first-cue windows to collapse.
            if index > 0 and use_active_lead:
                threshold -= active_lead_ms

            if index == 0:
                threshold = cue_start

            thresholds.append(max(cue_start, min(cue_end - 1, int(threshold))))
        return thresholds

    thresholds = build_thresholds(True)

    # If active lead/jitter makes the first cue or any dense cue non-monotonic, retry
    # without active lead before falling back to evenly distributed windows.
    if any(right <= left for left, right in zip(thresholds, thresholds[1:])):
        thresholds = build_thresholds(False)

    if any(right <= left for left, right in zip(thresholds, thresholds[1:])) or thresholds[-1] >= cue_end:
        duration = max(1, cue_end - cue_start)
        thresholds = [cue_start + int(round((duration * index) / len(timed_words))) for index in range(len(timed_words))]
        thresholds[0] = cue_start

    windows = []
    for index, item in enumerate(timed_words):
        start_ms = thresholds[index]
        end_ms = thresholds[index + 1] if index + 1 < len(thresholds) else cue_end
        start_ms = max(cue_start, min(cue_end - 1, start_ms))
        end_ms = max(start_ms + 1, min(cue_end, end_ms))
        windows.append((start_ms, end_ms, index, item))

    return windows


# Append one or more SSA events for a caption cue using exact word times when available,
# or estimated equal word timing when the cue came from SRT/editor text.
def _append_word_reveal_events(subs, cue, settings, video_info):
    timed_words = list(cue.get("timed_words") or [])
    windows = _build_active_word_windows(cue, timed_words, settings)

    if not windows:
        cue_start = int(cue.get("start") or 0)
        cue_end = max(cue_start + 1, int(cue.get("end") or cue_start + 1))
        subs.events.append(
            pysubs2.SSAEvent(
                start=cue_start,
                end=cue_end,
                text=_build_vtt_word_reveal_text(cue, settings, video_info, [], cue.get("text", "")),
                style="Default",
            )
        )
        return

    words = [item.get("word", "") for _, _, _, item in windows]

    for start_ms, end_ms, index, item in windows:
        subs.events.append(
            pysubs2.SSAEvent(
                start=start_ms,
                end=end_ms,
                text=_build_vtt_word_reveal_text(cue, settings, video_info, words[:index], item.get("word", "")),
                style="Default",
            )
        )






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

        for cue in _parse_vtt_cues(src):
            _append_word_reveal_events(subs, cue, settings, video_info)

        subs.save(dst)
        return

    source_subs = pysubs2.load(src, encoding="utf-8")

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

    for line_index, line in enumerate(source_subs):
        raw_text = str(line.text or "").replace(r"\N", "\n")
        # Remove any imported ASS override tags before rebuilding the final animated layer.
        clean_text = re.sub(r"\{[^}]*\}", "", raw_text).strip()
        cue = {
            "start": int(line.start),
            "end": int(line.end),
            "text": clean_text,
            "timed_words": [],
        }
        _append_word_reveal_events(subs, cue, settings, video_info)

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
        source_video_info = get_video_info(video_path)
        source_duration = get_video_duration(video_path)
        aspect_settings = settings.get("aspect")
        video_info = _video_info_after_aspect(source_video_info, aspect_settings)

        burn_captions = bool(settings.get("burn_captions", True))
        ass_render_path = None

        if burn_captions:
            if not srt_path:
                raise RuntimeError("Subtitle file is required when caption burn-in is enabled.")

            JOBS[job_id]["status"] = "preparing"
            JOBS[job_id]["message"] = "Generating animated ASS..."
            srt_to_animated_ass(srt_path, ass_path, settings, video_info)

            if append_text_overlay_to_ass(ass_path, settings.get("overlay"), video_info, source_duration):
                JOBS[job_id]["message"] = "Adding text overlay layer..."

            if preview_start or preview_seconds is not None:
                shift_ass_for_preview(ass_path, preview_start=preview_start, preview_seconds=preview_seconds)

            ass_render_path = ass_path
        elif _overlay_layer_enabled(settings.get("overlay")):
            JOBS[job_id]["status"] = "preparing"
            JOBS[job_id]["message"] = "Generating text overlay layer..."
            create_text_overlay_ass(ass_path, settings.get("overlay"), video_info, source_duration)

            if preview_start or preview_seconds is not None:
                shift_ass_for_preview(ass_path, preview_start=preview_start, preview_seconds=preview_seconds)

            ass_render_path = ass_path
        else:
            JOBS[job_id]["status"] = "preparing"
            JOBS[job_id]["message"] = "Rendering without burned captions..."

        JOBS[job_id]["status"] = "rendering"
        JOBS[job_id]["message"] = "Rendering video..."
        render_preview(
            video_path,
            ass_render_path,
            preview_path,
            preview_start=preview_start,
            preview_seconds=preview_seconds,
            aspect_settings=aspect_settings,
            global_overlay_settings=settings.get("global_overlay"),
        )

        audio_mix_settings = settings.get("audio_mix")
        audio_paths = list(settings.get("audio_paths") or [])
        if audio_mix_settings and audio_paths and audio_mix_settings.get("enabled"):
            JOBS[job_id]["message"] = "Mixing additional audio..."
            mixed_preview_path = os.path.join(OUTPUT_DIR, f"{job_id}_caption_audio_mix.mp4")
            mix_audio_into_video(preview_path, audio_paths, mixed_preview_path, audio_mix_settings, job_id=job_id)
            os.replace(mixed_preview_path, preview_path)

        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["message"] = "Render ready."
        JOBS[job_id]["preview_file"] = os.path.basename(preview_path)
        JOBS[job_id]["ass_file"] = os.path.basename(ass_render_path) if ass_render_path else None

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
        burn_captions = _safe_bool(request.form.get("render_burn_captions", "1"), True)

        if not video:
            return jsonify({"ok": False, "error": "Upload a video file."}), 400

        if burn_captions and not srt:
            return jsonify({"ok": False, "error": "Upload a subtitle file or disable caption burn-in."}), 400

        # Accept source videos including WebM for preview rendering.
        if not video.filename.lower().endswith((".mp4", ".mov", ".m4v", ".webm")):
            return jsonify({"ok": False, "error": "Video must be mp4, mov, m4v, or webm."}), 400

        if srt and not srt.filename.lower().endswith((".srt", ".vtt")):
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
        srt_bytes = srt.read() if srt else b""

        video_hash = hashlib.sha1(video_bytes).hexdigest()[:16]
        srt_hash = hashlib.sha1(srt_bytes).hexdigest()[:16] if srt_bytes else None

        video_ext = os.path.splitext(secure_filename(video.filename))[1].lower() or ".mp4"
        srt_ext = os.path.splitext(secure_filename(srt.filename))[1].lower() if srt else ".srt"

        video_name = f"src_{video_hash}{video_ext}"
        srt_name = f"src_{srt_hash}{srt_ext}" if srt_hash else None
        ass_name = f"{job_id}.ass"

        # Build output filename for the queued render job.
        output_name = f"{job_id}_{'preview' if mode == 'preview' else 'full'}.mp4"        

        video_path = os.path.join(UPLOAD_DIR, video_name)
        srt_path = os.path.join(UPLOAD_DIR, srt_name) if srt_name else None
        ass_path = os.path.join(OUTPUT_DIR, ass_name)
        output_path = os.path.join(OUTPUT_DIR, output_name)

        if not os.path.exists(video_path):
            with open(video_path, "wb") as f:
                f.write(video_bytes)

        if srt_path and not os.path.exists(srt_path):
            with open(srt_path, "wb") as f:
                f.write(srt_bytes)

        audio_paths = _save_audio_track_uploads(request.files.getlist("audio_tracks"), job_id)

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
            "aspect": _build_aspect_settings_from_form(request.form),
            "global_overlay": _build_global_overlay_settings_from_form(request.form),
            "audio_mix": _build_audio_mix_settings_from_form(request.form),
            "audio_paths": audio_paths,
            "burn_captions": burn_captions,
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
            "logs": [{"time": time.strftime("%H:%M:%S"), "message": "Job queued."}],
            "cancel_requested": False,
            "process": None,
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
        if process_kind not in ("silence", "speed", "upscale", "scale", "aspect", "crop", "trim", "grade"):
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

        audio_paths = _save_audio_track_uploads(request.files.getlist("audio_tracks"), job_id)

        settings = {
            "silence_threshold": _safe_float(request.form.get("silence_threshold", -40), -40),
            "min_silence_duration": _safe_float(request.form.get("min_silence_duration", 0.4), 0.4),
            "silence_mode": request.form.get("silence_mode", "talk"),
            "speed_factor": _safe_float(request.form.get("speed_factor", 1.25), 1.25),
            "upscale_factor": max(0.05, min(8.0, _safe_float(request.form.get("video_scale_factor", request.form.get("upscale_factor", 1)), 1))),
            "upscale_mode": "ai" if request.form.get("video_scale_mode", request.form.get("upscale_mode", "traditional")) == "ai" else "traditional",
            "aspect": _build_aspect_settings_from_form(request.form),
            "crop": _build_crop_settings_from_form(request.form),
            "trim": _build_trim_settings_from_form(request.form),
            "grading": _build_grading_settings_from_form(request.form),
            "overlay": _build_overlay_settings_from_form(request.form),
            "global_overlay": _build_global_overlay_settings_from_form(request.form),
            "audio_mix": _build_audio_mix_settings_from_form(request.form),
            "audio_paths": audio_paths,
        }

        if process_kind == "aspect":
            settings["aspect"]["enabled"] = True

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
            "caption_language": (request.form.get("caption_language", "en") or "en").strip(),
            "caption_word_timestamps": _safe_bool(request.form.get("caption_word_timestamps", "1"), True),
            "caption_vad_filter": _safe_bool(request.form.get("caption_vad_filter", "1"), True),
            "caption_chunk_max_words": _safe_int(request.form.get("caption_chunk_max_words", 4), 4),
            "caption_chunk_min_words": _safe_int(request.form.get("caption_chunk_min_words", 1), 1),
            "caption_chunk_max_seconds": _safe_float(request.form.get("caption_chunk_max_seconds", 2.2), 2.2),
            "caption_chunk_min_seconds": _safe_float(request.form.get("caption_chunk_min_seconds", 0.25), 0.25),
            "caption_chunk_max_chars": _safe_int(request.form.get("caption_chunk_max_chars", 42), 42),
            "caption_chunk_split_at_punctuation": _safe_bool(request.form.get("caption_chunk_split_at_punctuation", "1"), True),
            "caption_chunk_punctuation": (request.form.get("caption_chunk_punctuation", ".?!…") or ".?!…").strip(),
            "caption_chunk_split_on_gap": _safe_bool(request.form.get("caption_chunk_split_on_gap", "1"), True),
            "caption_chunk_gap_seconds": _safe_float(request.form.get("caption_chunk_gap_seconds", 0.55), 0.55),
            "caption_chunk_words_per_line": _safe_int(request.form.get("caption_chunk_words_per_line", 0), 0),
            "caption_chunk_max_lines": _safe_int(request.form.get("caption_chunk_max_lines", 2), 2),
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
        "logs": job.get("logs", [])[-30:],
        "cancel_requested": bool(job.get("cancel_requested")),
    })



@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404

    job["cancel_requested"] = True
    job["message"] = "Cancelling..."
    _append_job_log(job_id, "Cancel requested.")

    process = job.get("process")
    if process and getattr(process, "poll", lambda: None)() is None:
        try:
            process.terminate()
        except Exception:
            pass

    return jsonify({"ok": True})



def _safe_output_path_from_filename(filename_value):
    """Resolve an output filename or relative output path under OUTPUT_DIR only."""
    raw = str(filename_value or "").strip().replace("\\", "/")
    raw = raw.lstrip("/")
    if raw.startswith("outputs/"):
        raw = raw[len("outputs/"):]
    if not raw:
        raise ValueError("Missing output filename.")

    parts = [secure_filename(part) for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part in ("", "..") for part in parts):
        raise ValueError("Unsafe output filename.")

    output_path = os.path.abspath(os.path.join(OUTPUT_DIR, *parts))
    output_root = os.path.abspath(OUTPUT_DIR)
    if not output_path.startswith(output_root + os.sep):
        raise ValueError("Unsafe output path.")
    return output_path


def _reveal_file_in_system_manager(file_path):
    """Reveal an existing file in Finder / Explorer / system file manager."""
    file_path = os.path.abspath(file_path)
    system_name = platform.system()
    manager_name = "file manager"

    if system_name == "Darwin":
        manager_name = "Finder"
        subprocess.Popen(["open", "-R", file_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif system_name == "Windows":
        manager_name = "File Explorer"
        subprocess.Popen(["explorer", f"/select,{file_path}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        manager_name = "file manager"
        subprocess.Popen(["xdg-open", os.path.dirname(file_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return manager_name


@app.route("/api/reveal_asset_file", methods=["POST"])
def api_reveal_asset_file():
    """Save the selected browser-side project asset into the app output folder and reveal it.

    Browser projects keep videos as IndexedDB blobs, so there is no Finder path to reveal
    until the selected asset is written back to the local Flask app's filesystem.
    """
    try:
        ensure_dirs()

        video = request.files.get("video")
        if not video:
            return jsonify({"ok": False, "error": "No video file was provided."}), 400

        original_name = secure_filename(video.filename or "project-video.mp4")
        base, ext = os.path.splitext(original_name)
        ext = ext.lower() or ".mp4"

        if ext not in (".mp4", ".mov", ".m4v", ".webm"):
            return jsonify({"ok": False, "error": "Only mp4, mov, m4v, or webm assets can be revealed."}), 400

        project_id = secure_filename(request.form.get("project_id", "project"))[:48] or "project"
        asset_id = secure_filename(request.form.get("asset_id", "asset"))[:48] or "asset"
        safe_base = (base or "project-video")[:120]
        stamp = time.strftime("%Y%m%d_%H%M%S")
        output_name = f"{stamp}_{project_id}_{asset_id}_{safe_base}{ext}"
        output_path = os.path.abspath(os.path.join(REVEAL_DIR, output_name))

        reveal_root = os.path.abspath(REVEAL_DIR)
        if not output_path.startswith(reveal_root + os.sep):
            return jsonify({"ok": False, "error": "Unsafe output path."}), 400

        video.save(output_path)

        try:
            manager_name = _reveal_file_in_system_manager(output_path)
        except FileNotFoundError:
            return jsonify({
                "ok": False,
                "error": "Saved the asset copy, but could not open the system file manager.",
                "path": output_path,
            }), 500

        rel_name = os.path.relpath(output_path, OUTPUT_DIR).replace(os.sep, "/")
        return jsonify({
            "ok": True,
            "message": f"Saved a project copy and revealed it in {manager_name}.",
            "manager": manager_name,
            "filename": rel_name,
            "path": output_path,
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500



@app.route("/api/reveal_existing_output", methods=["POST"])
def api_reveal_existing_output():
    """Reveal an existing Flask output file without making another copy."""
    try:
        ensure_dirs()
        payload = request.get_json(silent=True) or {}
        filename = request.form.get("filename") or payload.get("filename")
        output_path = _safe_output_path_from_filename(filename)

        if not os.path.isfile(output_path):
            return jsonify({"ok": False, "error": "The original output file is no longer on disk."}), 404

        manager_name = _reveal_file_in_system_manager(output_path)
        rel_name = os.path.relpath(output_path, OUTPUT_DIR).replace(os.sep, "/")
        return jsonify({
            "ok": True,
            "message": f"Revealed original output in {manager_name}.",
            "manager": manager_name,
            "filename": rel_name,
            "path": output_path,
        })

    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/delete_server_asset_file", methods=["POST"])
def api_delete_server_asset_file():
    """Delete a generated/revealed file that lives under the Flask outputs directory."""
    try:
        ensure_dirs()
        payload = request.get_json(silent=True) or {}
        filename = request.form.get("filename") or payload.get("filename")
        output_path = _safe_output_path_from_filename(filename)

        if not os.path.exists(output_path):
            return jsonify({
                "ok": True,
                "deleted": False,
                "message": "The disk file was already missing.",
            })

        if not os.path.isfile(output_path):
            return jsonify({"ok": False, "error": "Refusing to delete a non-file path."}), 400

        os.remove(output_path)
        rel_name = os.path.relpath(output_path, OUTPUT_DIR).replace(os.sep, "/")
        return jsonify({
            "ok": True,
            "deleted": True,
            "message": "Deleted the disk file.",
            "filename": rel_name,
            "path": output_path,
        })

    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/dev_flush", methods=["POST"])
def api_dev_flush():
    """Clear local development render trash, uploads, revealed assets, overlay cache, and live job state."""
    try:
        ensure_dirs()

        for job in list(JOBS.values()):
            job["cancel_requested"] = True
            process = job.get("process")
            if process and process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass

        time.sleep(0.1)

        deleted_files = 0
        deleted_dirs = 0

        def clear_directory_contents(directory):
            nonlocal deleted_files, deleted_dirs
            if not os.path.isdir(directory):
                return

            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                try:
                    if os.path.isdir(path) and not os.path.islink(path):
                        shutil.rmtree(path, ignore_errors=True)
                        deleted_dirs += 1
                    else:
                        os.remove(path)
                        deleted_files += 1
                except FileNotFoundError:
                    pass

        clear_directory_contents(UPLOAD_DIR)
        clear_directory_contents(OUTPUT_DIR)
        os.makedirs(REVEAL_DIR, exist_ok=True)
        os.makedirs(GLOBAL_OVERLAY_CACHE_DIR, exist_ok=True)

        if os.path.isfile(APP_STATE_PATH):
            try:
                os.remove(APP_STATE_PATH)
                deleted_files += 1
            except FileNotFoundError:
                pass

        JOBS.clear()

        return jsonify({
            "ok": True,
            "message": "Development cache and processing trash cleared.",
            "deleted_files": deleted_files,
            "deleted_dirs": deleted_dirs,
        })

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(ASSETS_DIR, filename)


@app.route("/favicon.svg")
def favicon():
    return send_from_directory(os.path.join(ASSETS_DIR, "images", "icons"), "favicon.svg")


@app.route("/outputs/<path:filename>")
def serve_output(filename):
    return send_from_directory(OUTPUT_DIR, filename)


if __name__ == "__main__":
    ensure_dirs()
    app.run(host="127.0.0.1", port=5151, debug=True, threaded=False, use_reloader=True)