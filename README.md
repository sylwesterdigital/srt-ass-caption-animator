<img width="1641" height="1356" alt="Screenshot 2026-07-14 at 13 11 24" src="https://github.com/user-attachments/assets/fbcd8371-c668-456c-aefc-50cb13aa2c38" />


# Caption Animator

Caption Animator is a local macOS video editor for captioning, trimming, reframing, transforming, and exporting social video.

The interface runs in a native macOS WebKit window. Video processing is performed locally through Flask, FFmpeg, Faster-Whisper, and optional Real-ESRGAN AI upscaling.

**Publisher:** WORKWORK.FUN  
**Author:** Sylwester Mielniczuk  
**Copyright:** © 2026

## Download

Signed and notarized macOS builds are published on the GitHub Releases page:

https://github.com/sylwesterdigital/srt-ass-caption-animator/releases

For most users, download the `.dmg`, open it, and drag **Caption Animator** into **Applications**.

## Current platform support

- macOS 12 or later
- Current public build: Apple Silicon (`arm64`)
- Supported Macs: M1, M2, M3, M4, and later Apple Silicon models
- Intel and universal builds are not currently published

The application bundle is Developer ID signed and notarized by Apple.

## Main features

### Projects and media

- Create and reopen local projects
- Import multiple project videos
- Keep project media and rendered versions between sessions
- Import local MP4, MOV, M4V, and WebM files
- Import supported online media through yt-dlp
- Download available caption tracks when provided by the source
- Convert incompatible WebM, VP9, AV1, or Opus downloads to WebKit-compatible MP4

### Captions

- Generate captions locally with Faster-Whisper
- Edit caption text and timing
- Display caption clips on the timeline
- Import or export SRT and VTT captions
- Render ASS-styled captions into the final video
- Control font, size, position, outline, shadow, background, spacing, and alignment
- Apply animated caption entrances and exits
- Use custom uploaded fonts
- Split captions by word count, duration, punctuation, pauses, characters, and line count

### Video editing and transformation

- Trim and split video using a visual timeline
- Generate previews before full rendering
- Change playback speed while preserving audio pitch
- Keep speech sections or silence sections
- Crop and reframe video
- Change output aspect ratio and canvas size
- Scale or downscale using FFmpeg
- Upscale using Real-ESRGAN
- Apply colour grading
- Add solid, linear-gradient, or radial-gradient colour overlays
- Add independent text overlays
- Mix additional audio tracks with volume and pan controls

### Export and diagnostics

- Export processed video as MP4
- View processing progress and job logs
- Cancel active processing jobs
- Open the persistent application log from the UI
- Reveal generated files in Finder

## Included in the macOS release

The packaged application includes the tools needed for normal operation:

- FFmpeg and ffprobe
- Faster-Whisper runtime
- The configured Faster-Whisper model, currently `small`
- yt-dlp
- Deno for current yt-dlp JavaScript support
- Real-ESRGAN assets when present in the source project
- Python runtime and required packages
- Local WebKit desktop shell

Users do not need to install Python, Homebrew, FFmpeg, or the bundled Whisper model separately.

## Local data

The installed app keeps writable data outside the signed application bundle.

Application data:

```text
~/Library/Application Support/Caption Animator
```

Application log:

```text
~/Library/Logs/Caption Animator/app.log
```

The data folder may contain:

- Projects and application state
- Imported media
- Rendered outputs
- Uploaded fonts
- Downloaded models and helper tools
- Temporary processing assets

Removing the app from `/Applications` does not automatically remove this data.

## Viewing logs

Use the **Logs** control inside the application, or open the log directly:

```bash
open "$HOME/Library/Logs/Caption Animator/app.log"
```

Follow the log live in Terminal:

```bash
tail -f "$HOME/Library/Logs/Caption Animator/app.log"
```

Run the packaged executable with more verbose logging:

```bash
CAPTION_ANIMATOR_LOG_LEVEL=DEBUG \
  "/Applications/Caption Animator.app/Contents/MacOS/Caption Animator"
```

## Development

### Requirements

- macOS
- Python 3.12 recommended
- FFmpeg and ffprobe
- Python packages used by the backend, including Flask, Werkzeug, pysubs2, fonttools, Pillow, NumPy, and Faster-Whisper

The development backend currently expects FFmpeg at:

```text
~/ffmpeg-full/bin/ffmpeg
~/ffmpeg-full/bin/ffprobe
```

These paths are replaced automatically in packaged releases.

### Run locally

Create and activate a virtual environment:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install \
  Flask Werkzeug pysubs2 fonttools Pillow numpy faster-whisper
```

Start the development server:

```bash
python app.py
```

Open:

```text
http://127.0.0.1:5151
```

The source development server uses Flask debug mode and automatic reload. The packaged application disables both.

## Project layout

```text
.
├── app.py
├── templates/
│   └── index.html
├── assets/
│   ├── fonts/
│   └── images/icons/
├── fonts/
├── tools/
│   └── realesrgan/
├── srt_to_animated_ass.py
├── build_macos_release_v2.sh
├── release_signed.sh
├── publish_github_release.sh
├── VERSION.txt
├── BUILD_NUMBER.txt
├── README.md
└── RELEASE.md
```

Some generated or machine-local items may intentionally remain untracked.

## Building a release

The full signed, notarized, and GitHub publishing workflow is documented in [RELEASE.md](RELEASE.md).

The normal signed build command is:

```bash
./release_signed.sh
```

Generated installers are placed in:

```text
release/
```

## Privacy and network use

Video editing, FFmpeg processing, and local transcription are performed on the Mac.

Network access is used when required for:

- Downloading media through yt-dlp
- Downloading optional models or helper assets
- Fetching dependencies during a release build
- Apple notarization
- Publishing GitHub Releases

Do not include private media in bug reports. Attach only the relevant log excerpt and reproduction steps.

## Known limitations

- The current distributed build is Apple Silicon only.
- Large files, transcription, and AI upscaling can require substantial disk space and processing time.
- Online media providers can change formats or block downloads without notice.
- Real-ESRGAN compatibility depends on the architecture of the bundled native helper.
- Browser/WebKit media support varies by codec; social downloads are remuxed or transcoded to MP4 when needed.
- This is an early release. Keep original source files and test important exports before deleting project media.

## License

See [LICENSE](LICENSE).

## Credits

**WORKWORK.FUN**  
Created by **Sylwester Mielniczuk**  
© 2026
