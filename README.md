REPLACE: `README.md`

# srt-ass-caption-animator

Captions Animator — a browser UI plus Flask backend for creating **animated burned-in captions** on video.

The app lets you load a video and an `.srt` or `.vtt`, adjust caption styling and motion in a live helper overlay, then render either a short preview or the full exported video.

<img width="1611" height="1373" alt="Screenshot 2026-03-11 at 03 10 20" src="https://github.com/user-attachments/assets/cdb7d32e-6a12-4dce-ae18-ef184775a415" />

https://github.com/user-attachments/assets/995d45bb-cffb-4ddf-85dd-98b0f98e0722

---

## What it does

### Front end
- loads a source video and subtitle file
- shows a draggable / resizable helper caption on top of the displayed video
- exposes caption controls through `lil-gui`
- allows live adjustment of:
  - typography
  - placement
  - animation
  - colors
  - background
  - outline
  - shadow
  - spacing
  - rotation
  - presets
- saves / imports / exports presets
- sends current control values with files to the backend for rendering

### Backend
- accepts uploaded video and subtitle file
- reads video resolution
- converts SRT / VTT into animated ASS subtitles
- applies style and per-line / per-word animation settings
- uses FFmpeg to burn ASS subtitles into:
  - a preview render
  - a full final MP4 render
- exposes job polling and returns rendered files when ready

This is not just a generic subtitle editor. It is a **caption animation and render tool** for styling subtitles visually, previewing placement on the actual video frame, and exporting a final hard-subbed video.

---

## Main features

- SRT and VTT subtitle input
- Animated ASS subtitle generation
- Hard-subbed preview and full export
- Live caption positioning overlay in the browser
- Typography and motion controls via `lil-gui`
- Preset save / load / export / import
- Per-word highlight support
- Flask backend with render job polling
- FFmpeg-based final render pipeline

---

## Tech stack

- Python
- Flask
- FFmpeg
- HTML / CSS / JavaScript
- `lil-gui`
- ASS subtitle generation pipeline

---

## Project structure

A typical layout looks like this:

```text
.
├── app.py
├── requirements.txt
├── static/
├── templates/
├── storage/
├── README.md
└── ...
````

If your repository layout differs, keep `app.py` as the main Flask entry point and ensure `storage/` is writable.

---

## Requirements

Before running the app, install:

* Python 3.10+ recommended
* `ffmpeg` available in PATH
* `venv` support
* pip

Check FFmpeg:

```bash
ffmpeg -version
```

If that fails, install FFmpeg first.

### macOS

```bash
brew install ffmpeg
```

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install ffmpeg python3-venv
```

---

## Setup

### 1. Clone the repo

```bash
git clone <your-repo-url>
cd srt-ass-caption-animator
```

### 2. Create a virtual environment

```bash
python3 -m venv venv
```

### 3. Activate the virtual environment

#### macOS / Linux

```bash
source venv/bin/activate
```

#### Windows (PowerShell)

```powershell
venv\Scripts\Activate.ps1
```

### 4. Install Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

If you do not have a `requirements.txt` yet, generate one from your working environment or install the Flask / subtitle / rendering dependencies used by `app.py`.

---

## Running the app

With the virtual environment active:

```bash
python app.py
```

If the app uses Flask environment variables instead, you can also run it with:

```bash
export FLASK_APP=app.py
export FLASK_ENV=development
flask run
```

Typical local URL:

```text
http://127.0.0.1:5000
```

Open that in the browser.

---

## Typical workflow

1. Open the app in the browser
2. Load a source video
3. Load an `.srt` or `.vtt`
4. Drag and resize the helper caption overlay on the video
5. Adjust styles and animation in `lil-gui`
6. Save or export a preset if needed
7. Render a short preview
8. Render the full burned-in MP4 when satisfied

---

## Subtitle animation workflow

The render pipeline works like this:

1. subtitle file is uploaded
2. backend parses SRT / VTT
3. subtitle cues are converted into ASS
4. current style / animation controls are applied
5. FFmpeg burns ASS into the source video
6. app returns preview or final output

This gives you high-quality hard subtitles while keeping the browser UI fast and interactive.

---

## Presets

The UI supports presets for caption design and motion.

Typical preset actions:

* save current preset
* load a saved preset
* export preset to file
* import preset from file

This is useful when you want:

* repeatable branding
* reusable caption motion styles
* quick switching between visual treatments

---

## Active word highlight

The app supports an **active word highlight** mode through:

* `active_word_colour`
* `active_word_lead_ms`

These settings are part of the control model and are used to emphasize the current spoken word during subtitle playback / render.

A previous crash happened because:

* the values existed in `defaultControls`
* but were missing from `controls`
* `lil-gui` then crashed while trying to create the color picker for a missing property

That issue means both defaults and active UI control state must stay in sync whenever a new control is added.

---

## Output

The app produces:

* preview renders
* final rendered MP4 files with burned-in animated captions

Depending on your implementation, outputs may be stored under `storage/` and exposed through Flask routes for download or preview.

---

## Development notes

### Virtual environment

Always activate the repo-local virtual environment before running or updating dependencies.

### FFmpeg

FFmpeg must be installed system-wide and accessible from the shell used to start Flask.

### Storage

Make sure the app has permission to write to the `storage/` directory.

### Large files

Preview rendering is much faster than full rendering and should be used during styling iteration.

---

## Troubleshooting

### `ffmpeg: command not found`

Install FFmpeg and verify it is in PATH:

```bash
ffmpeg -version
```

### Flask app starts but render fails

Check:

* input file paths
* subtitle parsing
* FFmpeg availability
* write permissions for `storage/`

### Virtual environment command not found

Use:

```bash
python3 -m venv venv
```

and make sure Python has venv support installed.

### GUI crashes when opening a control

Check whether the property exists in both:

* `defaultControls`
* active `controls`

If a control exists only in defaults but not in the runtime control object, `lil-gui` can fail when binding it.

---

## Recommended development commands

Activate venv:

```bash
source venv/bin/activate
```

Install / refresh deps:

```bash
pip install -r requirements.txt
```

Run app:

```bash
python app.py
```

Deactivate venv:

```bash
deactivate
```

---

## Example use cases

* TikTok / Reels / Shorts caption styling
* hard-subbed explainer videos
* animated quote videos
* podcast subtitles
* social media exports with stronger typography and motion

---

## License

Add your license here, for example:

```text
MIT
```

or replace this section with your project’s actual license.

---

## Repository summary

`srt-ass-caption-animator` is a Flask-based caption animation tool that turns ordinary subtitle files into visually styled, animated, hard-burned captions using ASS + FFmpeg, with a browser-based live positioning and styling workflow.
