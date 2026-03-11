from flask import Flask, request, send_from_directory, jsonify, render_template
from werkzeug.utils import secure_filename
import os
import re
import html
import uuid
import subprocess
import threading
import json
import pysubs2

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(APP_ROOT, "uploads")
OUTPUT_DIR = os.path.join(APP_ROOT, "outputs")
FFMPEG_BIN = os.path.expanduser("~/ffmpeg-full/bin/ffmpeg")
FFPROBE_BIN = os.path.expanduser("~/ffmpeg-full/bin/ffprobe")

app = Flask(__name__, template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

JOBS = {}


def ensure_dirs():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)


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
    pos_x, pos_y = _compute_position(video_info, settings)

    tags = [
        rf"\an{settings['alignment']}",
        rf"\pos({pos_x},{pos_y})",
        rf"\bord{settings['outline']:g}",
        rf"\shad{settings['shadow']:g}",
        rf"\blur{settings['blur']:g}",
        rf"\1c{settings['primary_colour']}",
        rf"\3c{settings['outline_colour']}",
        rf"\4c{settings['shadow_colour']}",
        rf"\fsp{settings['letter_spacing']:g}",
        rf"\frz{settings['rotation_z']:g}",
        rf"\fscx{settings['end_scale']}",
        rf"\fscy{settings['end_scale']}",
    ]

    prefix = "{" + "".join(tags) + "}"

    shown_past = len([word for word in past_words if word.strip()])
    revealed_count = 0
    active_done = False
    parts = []

    tokens = re.split(r"(\s+)", cue["text"])

    for token in tokens:
        if token == "":
            continue

        if token.isspace():
            parts.append(token.replace("\n", r"\N"))
            continue

        escaped_word = _escape_ass_text(token)

        if revealed_count < shown_past:
            parts.append(escaped_word)
        elif not active_done and active_word and token == active_word:
            parts.append(
                "{" + rf"\1c{settings['active_word_colour']}" + "}"
                + escaped_word +
                "{" + rf"\1c{settings['primary_colour']}" + "}"
            )
            active_done = True
        else:
            parts.append(
                "{" + r"\alpha&HFF&" + "}"
                + escaped_word +
                "{" + r"\alpha&H00&" + "}"
            )

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


def _build_line_override(text, duration, settings, video_info):
    pos_x, pos_y = _compute_position(video_info, settings)

    intro_ms = max(0, min(duration, settings["intro_ms"]))
    outro_ms = max(0, min(duration, settings["outro_ms"]))
    exit_start = max(0, duration - outro_ms)

    start_x = pos_x + settings["in_offset_x"]
    start_y = pos_y + settings["in_offset_y"]
    end_x = pos_x + settings["out_offset_x"]
    end_y = pos_y + settings["out_offset_y"]

    tags = [
        rf"\an{settings['alignment']}",
        rf"\bord{settings['outline']:g}",
        rf"\shad{settings['shadow']:g}",
        rf"\blur{settings['blur']:g}",
        rf"\1c{settings['primary_colour']}",
        rf"\3c{settings['outline_colour']}",
        rf"\4c{settings['shadow_colour']}",
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

    if outro_ms > 0:
        if settings["out_mode"] == "move":
            body += rf"\t({exit_start},{duration},\move({pos_x},{pos_y},{end_x},{end_y},{exit_start},{duration})\alpha{settings['end_alpha']})"
        else:
            body += rf"\t({exit_start},{duration},\alpha{settings['end_alpha']})"

    body += "}"
    return body + text




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
        style.backcolor = ass_bgr_to_color(settings["shadow_colour"], "&H00000000")
        style.bold = settings["bold"]
        style.italic = settings["italic"]
        style.borderstyle = 1
        style.outline = settings["outline"]
        style.shadow = settings["shadow"]
        style.alignment = settings["alignment"]
        style.marginv = settings["margin_v"]
        style.marginl = settings["margin_h"]
        style.marginr = settings["margin_h"]
        subs.styles["Default"] = style

        for cue in _parse_vtt_cues(src):
            timed_words = cue["timed_words"]

            if not timed_words:
                subs.events.append(
                    pysubs2.SSAEvent(
                        start=cue["start"],
                        end=cue["end"],
                        text=_build_vtt_word_reveal_text(
                            cue,
                            settings,
                            video_info,
                            [],
                            cue["text"],
                        ),
                        style="Default",
                    )
                )
                continue

            past_words = []

            for index, item in enumerate(timed_words):
                start_ms = max(cue["start"], item["start"])
                end_ms = cue["end"] if index == len(timed_words) - 1 else timed_words[index + 1]["start"]

                if end_ms <= start_ms:
                    past_words.append(item["word"])
                    continue

                subs.events.append(
                    pysubs2.SSAEvent(
                        start=start_ms,
                        end=end_ms,
                        text=_build_vtt_word_reveal_text(
                            cue,
                            settings,
                            video_info,
                            past_words,
                            item["word"],
                        ),
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
    style.backcolor = ass_bgr_to_color(settings["shadow_colour"], "&H00000000")
    style.bold = settings["bold"]
    style.italic = settings["italic"]
    style.borderstyle = 1
    style.outline = settings["outline"]
    style.shadow = settings["shadow"]
    style.alignment = settings["alignment"]
    style.marginv = settings["margin_v"]
    style.marginl = settings["margin_h"]
    style.marginr = settings["margin_h"]

    for line in subs:
        text = _escape_ass_text(line.text)
        duration = max(1, int(line.end) - int(line.start))
        line.text = _build_line_override(text, duration, settings, video_info)

    subs.save(dst)







def render_preview(video_path, ass_path, output_path, preview_seconds=8):
    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i", video_path,
    ]

    if preview_seconds is not None:
        cmd += ["-t", str(preview_seconds)]

    cmd += [
        "-vf", f"ass={ass_path}",
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "FFmpeg render failed")


def process_preview(job_id, video_path, srt_path, ass_path, preview_path, settings, preview_seconds=8):
    try:
        JOBS[job_id]["status"] = "preparing"
        JOBS[job_id]["message"] = "Reading video resolution..."
        video_info = get_video_info(video_path)

        JOBS[job_id]["status"] = "preparing"
        JOBS[job_id]["message"] = "Generating animated ASS..."
        srt_to_animated_ass(srt_path, ass_path, settings, video_info)

        JOBS[job_id]["status"] = "rendering"
        JOBS[job_id]["message"] = "Rendering video..."
        render_preview(video_path, ass_path, preview_path, preview_seconds=preview_seconds)

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






@app.route("/api/preview", methods=["POST"])
def api_preview():
    try:
        video = request.files.get("video")
        srt = request.files.get("srt")

        if not video or not srt:
            return jsonify({"ok": False, "error": "Upload both video and subtitle file."}), 400

        if not video.filename.lower().endswith((".mp4", ".mov", ".m4v")):
            return jsonify({"ok": False, "error": "Video must be mp4, mov, or m4v."}), 400

        if not srt.filename.lower().endswith((".srt", ".vtt")):
            return jsonify({"ok": False, "error": "Subtitle file must be .srt or .vtt"}), 400

        mode = request.form.get("mode", "preview")
        preview_seconds = 8 if mode == "preview" else None

        job_id = str(uuid.uuid4())[:8]

        video_name = f"{job_id}_{secure_filename(video.filename)}"
        srt_name = f"{job_id}_{secure_filename(srt.filename)}"
        ass_name = f"{job_id}.ass"
        output_name = f"{job_id}_{'preview' if mode == 'preview' else 'full'}.mp4"

        video_path = os.path.join(UPLOAD_DIR, video_name)
        srt_path = os.path.join(UPLOAD_DIR, srt_name)
        ass_path = os.path.join(OUTPUT_DIR, ass_name)
        output_path = os.path.join(OUTPUT_DIR, output_name)

        video.save(video_path)
        srt.save(srt_path)

        settings = {
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
            "primary_colour": _hex_to_ass_bgr(request.form.get("primary_colour", "#FFFFFF"), "00"),
            "outline_colour": _hex_to_ass_bgr(request.form.get("outline_colour", "#000000"), "00"),
            "shadow_colour": _hex_to_ass_bgr(request.form.get("shadow_colour", "#000000"), "00"),
            "active_word_colour": _hex_to_ass_bgr(request.form.get("active_word_colour", "#ff0000"), "00"),
            "active_word_lead_ms": _safe_int(request.form.get("active_word_lead_ms", 80), 80),
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
            "out_mode": request.form.get("out_mode", "fade"),
        }

        JOBS[job_id] = {
            "status": "queued",
            "message": "Queued...",
            "preview_file": None,
            "ass_file": None,
        }

        thread = threading.Thread(
            target=process_preview,
            args=(job_id, video_path, srt_path, ass_path, output_path, settings, preview_seconds),
            daemon=True,
        )
        thread.start()

        return jsonify({"ok": True, "job_id": job_id})

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500



@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found"}), 404

    return jsonify({
        "ok": True,
        "status": job["status"],
        "message": job["message"],
        "preview_url": f"/outputs/{job['preview_file']}" if job.get("preview_file") else None,
        "ass_url": f"/outputs/{job['ass_file']}" if job.get("ass_file") else None,
    })


@app.route("/outputs/<path:filename>")
def serve_output(filename):
    return send_from_directory(OUTPUT_DIR, filename)


if __name__ == "__main__":
    ensure_dirs()
    app.run(host="127.0.0.1", port=5151, debug=True, threaded=True)