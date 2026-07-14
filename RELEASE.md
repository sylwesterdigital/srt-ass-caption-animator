# macOS Release Guide

This document describes how to build, sign, notarize, test, customize, and publish another Caption Animator macOS release.

The release scripts package the Flask application as a native WebKit `.app`, then create a drag-to-Applications `.dmg`, a `.zip`, and SHA-256 checksums.

## Release scripts

```text
build_macos_release_v2.sh   Main builder
release_signed.sh           WORKWORK.FUN signed-release wrapper
publish_github_release.sh   GitHub Release publisher
```

The normal workflow is:

```bash
./release_signed.sh
ALLOW_DIRTY=1 ./publish_github_release.sh
```

The GitHub publisher creates a draft by default.

---

## Current release identity

The signed wrapper is currently configured with:

```text
Application name:     Caption Animator
Safe filename:        CaptionAnimator
Bundle identifier:    fun.workwork.captionanimator
Publisher:            WORKWORK.FUN
Author:               Sylwester Mielniczuk
Copyright year:       2026
Developer Team ID:    5P9V78UZAC
Notary profile:       workwork-caption-notary
```

The signing identity is selected by certificate SHA-1 fingerprint to avoid ambiguity between duplicate certificate display names:

```text
B97863CA4E17170FCD5FBFA4C76A8DF3D91D5F6B
```

Never store an Apple Account password or app-specific password in the repository.

---

## One-time Mac setup

### 1. Xcode command-line tools

```bash
xcode-select --install
```

Confirm the required tools exist:

```bash
xcrun --version
codesign --version
hdiutil help >/dev/null
```

### 2. Python

Python 3.12 is recommended:

```bash
brew install python@3.12
/opt/homebrew/bin/python3.12 --version
```

The build architecture follows the selected Python interpreter:

```bash
/opt/homebrew/bin/python3.12 -c \
  'import platform; print(platform.machine())'
```

On an Apple Silicon Mac, this should normally print:

```text
arm64
```

### 3. FFmpeg and ffprobe

The default local source is:

```text
~/ffmpeg-full/bin/ffmpeg
~/ffmpeg-full/bin/ffprobe
```

Confirm both work:

```bash
"$HOME/ffmpeg-full/bin/ffmpeg" -version
"$HOME/ffmpeg-full/bin/ffprobe" -version
```

A different build can be supplied with `FFMPEG_SOURCE` and `FFPROBE_SOURCE`.

### 4. Developer ID certificate

List signing identities:

```bash
security find-identity -v -p codesigning
```

The configured certificate should be present and valid. The wrapper uses its fingerprint rather than its repeated display name.

### 5. Notarization profile

The profile has already been stored in Keychain. Verify it:

```bash
xcrun notarytool history \
  --keychain-profile "workwork-caption-notary"
```

A machine without this profile must create it once:

```bash
xcrun notarytool store-credentials "workwork-caption-notary" \
  --apple-id "APPLE_ACCOUNT_EMAIL" \
  --team-id "5P9V78UZAC" \
  --password "APPLE_APP_SPECIFIC_PASSWORD"
```

The password in that command is an Apple-generated app-specific password, not the normal Apple Account password.

### 6. GitHub CLI

```bash
brew install gh
gh auth status
gh repo view
```

---

## Before each release

### 1. Review source changes

Test the development application:

```bash
python app.py
```

Check at least:

- Local video import
- Social URL import
- WebM or VP9/AV1 conversion to playable MP4
- Video playback
- Caption generation
- Caption editing and timing
- Caption editor layout while timelines are open
- Trim and split operations
- Preview render
- Full render
- Speed processing
- Silence processing
- Crop and aspect-ratio processing
- Colour and text overlays
- Audio mixing
- Traditional scaling
- Real-ESRGAN scaling
- Application shutdown
- Reopening projects
- Logs button and persistent log file

### 2. Set the marketing version

Edit `VERSION.txt`:

```text
0.1.1
```

Or override it for one build:

```bash
VERSION="0.1.1" ./release_signed.sh
```

`VERSION.txt` becomes `CFBundleShortVersionString`.

### 3. Understand build numbers

`BUILD_NUMBER.txt` is incremented automatically at the beginning of every build.

For example:

```text
3
```

becomes:

```text
4
```

The build number becomes `CFBundleVersion` and is included in asset names and Git tags.

A failed build may still consume a build number. This is acceptable and should not be manually reused after a distributed or submitted build.

### 4. Check available disk space

The current release can exceed 700 MB because it bundles Python, FFmpeg, ML libraries, and a Whisper model. The temporary build directory can require several gigabytes.

```bash
df -h .
```

---

## Build a signed and notarized release

Make scripts executable once:

```bash
chmod +x \
  build_macos_release_v2.sh \
  release_signed.sh \
  publish_github_release.sh
```

Build:

```bash
./release_signed.sh
```

The wrapper supplies the signing fingerprint, bundle identity, publisher, author, icon, and notarization profile to the main builder.

The builder performs these steps:

1. Creates `.macos-build/`.
2. Copies source files into a staged payload.
3. Vendors the frontend `lil-gui` module.
4. Bundles FFmpeg and ffprobe.
5. Downloads the current standalone yt-dlp.
6. Optionally bundles Deno.
7. Creates an isolated Python virtual environment.
8. Installs runtime and PyInstaller dependencies.
9. Optionally embeds a Faster-Whisper model.
10. Patches the staged backend and frontend for installed-app paths and desktop behavior.
11. Generates the desktop launcher.
12. Generates the `.icns`.
13. Generates the PyInstaller specification.
14. Builds the `.app`.
15. Signs nested native helpers.
16. Signs and validates the outer app.
17. Runs the packaged smoke test.
18. Notarizes and staples the `.app`.
19. Creates the `.zip`.
20. Creates and signs the `.dmg`.
21. Notarizes and staples the `.dmg`.
22. Writes SHA-256 checksums.

A successful output ends with:

```text
Release complete.
```

and reports:

```text
source=Notarized Developer ID
```

---

## Release output

Files are written to:

```text
release/
```

Example:

```text
CaptionAnimator-v0.1.0-b4-macOS-arm64.dmg
CaptionAnimator-v0.1.0-b4-macOS-arm64.zip
CaptionAnimator-v0.1.0-b4-SHA256.txt
```

The unpackaged app is located at:

```text
.macos-build/dist/Caption Animator.app
```

---

## Verify the finished release

Set the artifact name:

```bash
DMG="release/CaptionAnimator-v0.1.0-b4-macOS-arm64.dmg"
```

Validate the stapled ticket:

```bash
xcrun stapler validate "$DMG"
```

Check Gatekeeper:

```bash
spctl --assess \
  --type open \
  --context context:primary-signature \
  --verbose=4 \
  "$DMG"
```

Expected result:

```text
accepted
source=Notarized Developer ID
```

Mount and verify the app:

```bash
hdiutil attach "$DMG"

codesign --verify --deep --strict --verbose=4 \
  "/Volumes/Caption Animator/Caption Animator.app"

spctl --assess --type execute --verbose=4 \
  "/Volumes/Caption Animator/Caption Animator.app"

xcrun stapler validate \
  "/Volumes/Caption Animator/Caption Animator.app"
```

Detach when finished:

```bash
hdiutil detach "/Volumes/Caption Animator"
```

Verify checksums:

```bash
(
  cd release
  shasum -a 256 -c CaptionAnimator-v0.1.0-b4-SHA256.txt
)
```

---

## Install and smoke-test the DMG

Do not test only the app inside `.macos-build`. Test the exact DMG that users will download.

1. Remove or rename any existing `/Applications/Caption Animator.app`.
2. Open the new DMG.
3. Drag the app to Applications.
4. Launch it normally from Finder.
5. Run the full release checklist.
6. Close the final window and confirm the process exits.
7. Relaunch and confirm project persistence.
8. Test on another Apple Silicon Mac when possible.

Open the runtime log during testing:

```bash
tail -f "$HOME/Library/Logs/Caption Animator/app.log"
```

---

## Customize the application

All options can be passed as environment variables.

### Change the icon

The signed wrapper uses this default when it exists:

```text
assets/images/icons/logo.svg
```

Use a custom icon for one build:

```bash
APP_ICON_SOURCE="$HOME/Desktop/caption-animator-icon.png" \
  ./release_signed.sh
```

Supported source formats:

- PNG
- SVG
- JPEG
- WebP

Recommended source:

- Square
- 1024×1024 pixels
- Transparent or fully designed background
- Important artwork kept away from the outer edges

The builder scales the artwork into a 1024×1024 canvas and generates all required `.iconset` sizes before converting them to `.icns`.

For a permanent default, change the `APP_ICON_SOURCE` logic in `release_signed.sh`.

#### macOS icon cache

After changing an icon, remove the old installed app and reinstall the new build. If Finder or the Dock still shows the previous icon:

```bash
killall Finder
killall Dock
```

Do not change files inside a signed `.app` after signing. Any modification invalidates the signature.

### Change app name

For one build:

```bash
APP_NAME="New App Name" \
APP_SAFE_NAME="NewAppName" \
  ./release_signed.sh
```

`APP_NAME` is the visible application and volume name.

`APP_SAFE_NAME` is used in artifact filenames and internal build paths.

Changing the name also changes the default data and log directories unless explicitly overridden.

### Change bundle identifier

```bash
BUNDLE_ID="fun.workwork.newproduct" \
  ./release_signed.sh
```

Use a stable reverse-domain identifier. Changing it creates a distinct macOS application identity.

### Change publisher and author

```bash
APP_PUBLISHER="WORKWORK.FUN" \
APP_AUTHOR="Sylwester Mielniczuk" \
COPYRIGHT_YEAR="2026" \
  ./release_signed.sh
```

These values are inserted into the app metadata and staged UI branding.

### Change the embedded Whisper model

Default:

```bash
BUNDLE_WHISPER_MODEL="small" ./release_signed.sh
```

Smaller installer, model obtained later when required:

```bash
BUNDLE_WHISPER_MODEL="none" ./release_signed.sh
```

Other model example:

```bash
BUNDLE_WHISPER_MODEL="base" ./release_signed.sh
```

Larger models substantially increase release size and build time.

### Omit Deno

```bash
BUNDLE_DENO=0 ./release_signed.sh
```

This reduces the bundle slightly but can reduce compatibility with current yt-dlp JavaScript-dependent extractors.

### Select a different Python

```bash
PYTHON_BIN="/opt/homebrew/bin/python3.12" \
  ./release_signed.sh
```

The target architecture follows this Python executable.

### Select a different FFmpeg build

```bash
FFMPEG_SOURCE="/path/to/ffmpeg" \
FFPROBE_SOURCE="/path/to/ffprobe" \
  ./release_signed.sh
```

Both binaries must contain the target architecture.

Check:

```bash
lipo -archs /path/to/ffmpeg
lipo -archs /path/to/ffprobe
```

### Change minimum macOS version

```bash
MIN_MACOS="12.0" ./release_signed.sh
```

Do not lower this without testing all bundled native libraries on the older macOS release.

### Change local server port

```bash
APP_PORT="5152" ./release_signed.sh
```

The application binds only to `127.0.0.1`.

### Change log level

```bash
APP_LOG_LEVEL="DEBUG" ./release_signed.sh
```

Available Python logging levels include `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`.

---

## Build without signing

For local tester builds only:

```bash
./build_macos_release_v2.sh
```

Without `MACOS_SIGN_IDENTITY`, the builder uses ad-hoc signing. Gatekeeper rejection is expected after download.

A Developer ID-signed but non-notarized build can be created with:

```bash
MACOS_SIGN_IDENTITY="B97863CA4E17170FCD5FBFA4C76A8DF3D91D5F6B" \
  ./build_macos_release_v2.sh
```

Public distribution should use both signing and notarization.

---

## Publish to GitHub Releases

### Confirm authentication and repository

```bash
gh auth status
gh repo view
```

### Generated files do not need to be committed

The publisher normally requires a clean working tree because the tag points to the current committed `HEAD`.

To deliberately publish while local build files remain untracked:

```bash
ALLOW_DIRTY=1 ./publish_github_release.sh
```

Untracked files are not included in GitHub’s source archive. Only the selected DMG, ZIP, and checksum are uploaded as Release assets.

### Optional local-only Git excludes

To hide generated items from `git status` without modifying the repository `.gitignore`:

```bash
cat >> .git/info/exclude <<'EOF'
.DS_Store
.macos-build/
release/
BUILD_NUMBER.txt
VERSION.txt
build_macos_release.sh
build_macos_release_v2.sh
release_signed.sh
publish_github_release.sh
templates/.DS_Store
EOF
```

Do not exclude actual application source folders such as `assets/`, `templates/`, `fonts/`, or `tools/` unless that is a deliberate repository policy.

### Create a draft release

```bash
ALLOW_DIRTY=1 ./publish_github_release.sh
```

The publisher:

1. Finds the newest matching DMG in `release/`.
2. Parses version, build number, and architecture from its filename.
3. Verifies checksums.
4. Creates an annotated Git tag.
5. Pushes the tag.
6. Creates a draft GitHub Release.
7. Uploads the DMG, ZIP, and checksum.

The draft URL may contain `untagged-...`. That URL is temporary.

### Use custom release notes

Prepare a Markdown file, then update the draft:

```bash
gh release edit v0.1.0-b4 \
  --notes-file CaptionAnimator-v0.1.0-b4-RELEASE_NOTES.md
```

Open it:

```bash
gh release view v0.1.0-b4 --web
```

Publish after review:

```bash
gh release edit v0.1.0-b4 --draft=false
```

### Publish immediately

```bash
GITHUB_RELEASE_MODE=published \
ALLOW_DIRTY=1 \
  ./publish_github_release.sh
```

### Publish as prerelease

```bash
GITHUB_RELEASE_MODE=prerelease \
ALLOW_DIRTY=1 \
  ./publish_github_release.sh
```

### Override the tag

```bash
RELEASE_TAG="v0.1.0-b4" \
ALLOW_DIRTY=1 \
  ./publish_github_release.sh
```

### Replace assets on an existing release

Running the publisher for an existing tag uploads the local artifacts with `--clobber`.

It does not automatically replace custom release notes. Use `gh release edit --notes-file` separately.

---

## Architecture builds

### Apple Silicon

Use an arm64 Python and arm64 FFmpeg:

```bash
PYTHON_BIN="/opt/homebrew/bin/python3.12" \
FFMPEG_SOURCE="$HOME/ffmpeg-full/bin/ffmpeg" \
FFPROBE_SOURCE="$HOME/ffmpeg-full/bin/ffprobe" \
  ./release_signed.sh
```

The artifact suffix will be:

```text
macOS-arm64
```

### Intel

An Intel release must be built with:

- x86_64 Python
- x86_64 FFmpeg and ffprobe
- x86_64-compatible Python wheels
- compatible native helper tools

The artifact suffix will be:

```text
macOS-x86_64
```

This should be performed and tested on an Intel Mac or a controlled x86_64 build environment.

### Universal

The current builder does not create a universal2 application automatically. A universal release would require universal Python/native packages or merging separate architecture builds and re-signing every nested executable.

---

## Build and runtime logs

### Runtime log

```text
~/Library/Logs/Caption Animator/app.log
```

Open:

```bash
open "$HOME/Library/Logs/Caption Animator/app.log"
```

Follow:

```bash
tail -f "$HOME/Library/Logs/Caption Animator/app.log"
```

### PyInstaller warnings

```text
.macos-build/work/CaptionAnimator/warn-CaptionAnimator.txt
```

### PyInstaller dependency graph

```text
.macos-build/work/CaptionAnimator/xref-CaptionAnimator.html
```

### Build workspace

```text
.macos-build/
```

When a build fails, inspect the terminal output and files in that directory before deleting it.

---

## Clean rebuild

The builder automatically removes staged payload, distribution, and work folders while retaining cached downloads and the virtual environment.

For a completely fresh build:

```bash
rm -rf .macos-build
./release_signed.sh
```

To remove generated release artifacts:

```bash
rm -rf release
```

Be careful not to remove:

```text
~/Library/Application Support/Caption Animator
```

unless intentionally resetting installed-app data.

---

## Troubleshooting

### “No valid signing identity”

Check:

```bash
security find-identity -v -p codesigning
```

Confirm the certificate fingerprint in `release_signed.sh` still exists.

### Duplicate Developer ID names

Keep using the SHA-1 fingerprint rather than the certificate display name.

### Notarization authentication failure

Verify:

```bash
xcrun notarytool history \
  --keychain-profile "workwork-caption-notary"
```

Recreate the Keychain profile if the app-specific password was revoked.

### Notarization rejected

Get the submission ID from the failed build, then request the log:

```bash
xcrun notarytool log SUBMISSION_ID \
  --keychain-profile "workwork-caption-notary"
```

Review unsigned nested binaries, invalid entitlements, or modified files.

### DMG is accepted but app does not launch

Open the runtime log:

```bash
tail -n 300 \
  "$HOME/Library/Logs/Caption Animator/app.log"
```

Also launch the packaged executable from Terminal:

```bash
".macos-build/dist/Caption Animator.app/Contents/MacOS/Caption Animator"
```

### App appears to reopen after closing

Confirm the latest builder is being used:

```bash
BUILDER="./build_macos_release_v2.sh" ./release_signed.sh
```

Test the exact installed DMG build and inspect the runtime log for a second launcher process.

### Downloaded video does not play

Inspect the job logs and runtime log. The staged release patch should convert incompatible social downloads to H.264/AAC MP4.

Confirm packaged FFmpeg:

```bash
".macos-build/dist/Caption Animator.app/Contents/Frameworks/bin/ffmpeg" \
  -version
```

### Real-ESRGAN requires Rosetta

If the bundled helper is Intel-only, the builder warns that Apple Silicon users need Rosetta 2.

Install Rosetta:

```bash
softwareupdate --install-rosetta --agree-to-license
```

A native arm64 Real-ESRGAN helper is preferable when available.

### Old icon remains visible

Remove the previous installed app, install the new one, then run:

```bash
killall Finder
killall Dock
```

### GitHub publisher selects the wrong DMG

It chooses the most recently modified matching DMG. Remove stale artifacts or explicitly inspect `release/` before publishing:

```bash
ls -lt release/
```

### Working tree is not clean

Use this only when intentional:

```bash
ALLOW_DIRTY=1 ./publish_github_release.sh
```

Remember that the release tag still points to the current committed `HEAD`.

---

## Recommended release checklist

```text
[ ] Source changes tested locally
[ ] VERSION.txt updated
[ ] Icon reviewed
[ ] Copyright metadata reviewed
[ ] Developer ID identity present
[ ] Notary profile validates
[ ] Enough disk space available
[ ] Signed build completed
[ ] App notarization accepted
[ ] DMG notarization accepted
[ ] Stapler validation succeeded
[ ] Gatekeeper reports Notarized Developer ID
[ ] DMG installation tested
[ ] Core editing features tested
[ ] Application closes cleanly
[ ] Runtime logs checked
[ ] SHA-256 checksums pass
[ ] GitHub draft created
[ ] Release notes reviewed
[ ] Draft published
```

---

## Typical next release

Example for version `0.1.1`:

```bash
printf '0.1.1\n' > VERSION.txt

APP_ICON_SOURCE="assets/images/icons/logo.svg" \
  ./release_signed.sh

xcrun stapler validate \
  release/CaptionAnimator-v0.1.1-b*-macOS-arm64.dmg

ALLOW_DIRTY=1 ./publish_github_release.sh

gh release list --limit 10
gh release view v0.1.1-b4 --web
```

Use the actual build number printed by the build rather than assuming `b4`.
