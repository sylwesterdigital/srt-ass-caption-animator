# Cut {{VERSION}} — Build {{BUILD_NUMBER}}

## Update 0.1.2

- Fixed the watcher so update ingestion no longer depends on release-time FFmpeg or other heavyweight build prerequisites.
- FFmpeg is now selected by capabilities, requiring both libx264 and ASS/subtitle filters instead of trusting whichever ffmpeg happens to be first on PATH.
- If no suitable FFmpeg is available, the watcher can install the isolated homebrew-ffmpeg build with alt command names, avoiding conflicts with Homebrew core ffmpeg.
- The selected FFmpeg and FFprobe are still copied into the signed Cut application; end-user Macs are never searched for FFmpeg, yt-dlp or Deno.
- The watcher can now receive a future repair update even when the current release toolchain is broken.

## Update 0.1.1

- Reworked online media import so packaged Cut never searches for or executes a random user-installed yt-dlp.
- Bundles yt-dlp[default], the matching yt-dlp-ejs package, Deno, FFmpeg and FFprobe as application-owned runtime components.
- Invokes yt-dlp through the signed Cut executable itself and pins the bundled Deno/FFmpeg paths while ignoring user yt-dlp config and plugin directories.
- Moved release and maintenance shell tooling under scripts/ with one current macOS builder; legacy builder names now forward to it.
- Added scripts/watch-update.sh to monitor archive/Cut-update-v*.zip, verify incremental updates, commit them, build/sign/notarize, publish GitHub releases and redeploy the homepage.
- Added automatic dependency checks/install for Homebrew-managed build prerequisites and stale Python build-environment detection.
- Added scripts/verify_repo.sh and scripts/make-update.sh for deterministic verification and future incremental update packages.

This release introduces the **Cut** product identity and consolidates the latest editing, caption, installation, and release-workflow improvements.

## Highlights

## Startup reliability

- Cut now tries the configured local port first.
- When that port is occupied, macOS assigns a free loopback port automatically.
- The desktop window waits for the local Flask server before opening.
- The selected port is recorded in the application log.
- Closing the window shuts down and closes the local server socket.


### New Cut identity

- Renamed the application and macOS bundle to **Cut**
- New release assets use the `Cut-v…` filename prefix
- Existing Caption Animator application data is preserved during migration
- Updated application badge, integrity report, release title, and homepage branding

### Faster Easy mode

- The Easy toolbar now has separate colours for normal text and the current spoken word
- Font size now uses a responsive range slider with a live numeric value
- Transport labels work correctly in compact Easy mode
- Text, icon-and-text, and icon-only transport modes are supported
- Compact video controls now size themselves to their visible content

### Project management

- Added project deletion to **Edit project**
- Deletion requires two separate OK / Cancel confirmations
- Removes project records, stored media, thumbnails, captions, generated outputs, and project-related temporary files
- Original source files selected from Finder are never deleted

### Captions, fonts, and diagnostics

- Improved active-caption following without rebuilding or jumping the caption list
- Added a compact theme-matched caption-editor scrollbar
- Recursively discovers nested TTF, OTF, WOFF, and WOFF2 UI font packages
- Synchronizes bundled UI fonts into Application Support
- Added component integrity checks and copyable reports
- Added Real-ESRGAN installation and repair guidance
- Improved release badge version/build reporting

### macOS release polish

- Signed and notarized Apple-silicon application
- Dark Finder icon background for the white transparent logo
- FFmpeg, FFprobe, Faster-Whisper, CTranslate2, local model data, and Deno included
- Complete DMG, application ZIP, and SHA-256 verification assets

## Installation

1. Download the `Cut` DMG for `{{ARCH}}`.
2. Open the DMG.
3. Drag **Cut** into **Applications**.
4. Choose **Replace** when upgrading an existing Cut installation.
5. Launch the installed copy from `/Applications`.
6. Open **Application Logs** and run **Check all**.

The application is signed and notarized. macOS may request confirmation on first launch.

## Included assets

- `Cut-v{{VERSION}}-b{{BUILD_NUMBER}}-macOS-{{ARCH}}.dmg`
- `Cut-v{{VERSION}}-b{{BUILD_NUMBER}}-macOS-{{ARCH}}.zip`
- `Cut-v{{VERSION}}-b{{BUILD_NUMBER}}-SHA256.txt`

## Known notes

- The current release targets Apple-silicon Macs.
- The first transcription may take longer if additional model data is required.
- Real-ESRGAN is optional; standard FFmpeg scaling remains available.
- Online media imports depend on third-party services and yt-dlp compatibility.

## Verification

```bash
shasum -a 256 -c Cut-v{{VERSION}}-b{{BUILD_NUMBER}}-SHA256.txt
```

Copyright © 2026 WORKWORK.FUN. Created by Sylwester Mielniczuk.
