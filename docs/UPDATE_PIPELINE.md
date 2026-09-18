# Cut update / release pipeline

All release and maintenance shell tooling lives under `scripts/`.

## Normal operation

Run once and leave it open:

```bash
cd /Users/smielniczuk/Documents/works/srt-ass-caption-animator/scripts
./watch-update.sh
```

The watcher monitors `<repo>/archive/Cut-update-v*.zip`.

A valid update archive contains:

```text
manifest.json
payload/
  VERSION.txt
  ...repository-relative changed files...
```

`manifest.json` records the base version, target version, release mode, changed-file SHA-256 checksums, release notes and optional deleted paths.

When an eligible archive appears, the watcher:

1. waits until the ZIP is stable;
2. checks ZIP paths and checksums;
3. requires a clean Git source tree;
4. applies only the incremental payload and explicit deletions;
5. updates `RELEASE_NOTES.md` from the manifest;
6. runs Python and shell syntax/integrity checks;
7. commits the source update;
8. ensures local build dependencies are present;
9. runs the resumable signed/notarized macOS release pipeline;
10. publishes the GitHub release;
11. deploys and verifies the homepage;
12. moves the processed package to `archive/processed/`.

If signing, notarization, GitHub or homepage deployment fails after the source update, the pending state is preserved under `~/Library/Application Support/Cut/watch-update/`. The watcher retries the existing resumable release instead of reapplying the update or incrementing the build repeatedly.

## Runtime media-tool policy

The packaged application does not discover or execute a random user-installed `yt-dlp`.

- `yt-dlp[default]` is installed into the build environment and bundled by PyInstaller.
- `yt-dlp-ejs` is bundled with the matching yt-dlp version.
- Cut launches its own signed executable with `--yt-dlp-cli` to invoke the embedded Python package.
- Deno is bundled and passed to yt-dlp by its absolute in-app path.
- FFmpeg/FFprobe are bundled and passed by their in-app path.
- User yt-dlp configuration and plugin directories are ignored for application imports.
- Production PATH is restricted to the application's bundled bin directory and macOS system directories.

Development mode may use developer-installed native tools where explicitly allowed, but frozen production builds do not depend on Homebrew, pipx or user-local yt-dlp installations.

## Creating a compatible update package

```bash
./scripts/make-update.sh 0.1.2 /path/to/incremental-payload "Short update summary"
```

Optional environment variables:

```bash
BASE_VERSION=0.1.1
RELEASE_MODE=published
NOTES_FILE=/path/to/notes.txt
DELETE_PATHS='obsolete/file:obsolete/directory'
```

The resulting ZIP is created under `archive/` and can be consumed by the watcher.


## Build-runtime dependency repair (0.1.2)

The watcher can ingest and apply update archives before checking heavyweight release dependencies. This is intentional: an update must be able to repair a broken build prerequisite. For macOS release builds, the watcher validates FFmpeg by capabilities (libx264 plus ASS/subtitle filters), not merely by the presence of an `ffmpeg` command. It prefers an explicitly configured full build, then the isolated `homebrew-ffmpeg` alt-name formula, then a known developer full build, and finally PATH only if the candidate passes the capability checks. The selected FFmpeg/FFprobe binaries are copied into the signed Cut application; end-user Macs are never searched for FFmpeg, yt-dlp, Deno, or other release-time tools.
## 0.1.3 release-toolchain repair

The FFmpeg capability check must never stream FFmpeg directly into `grep -q` while Bash `pipefail` is enabled. A successful early grep match can close the pipe, FFmpeg receives SIGPIPE, and the pipeline is reported as failed. The watcher and macOS builder now capture the complete encoder/filter output first and then inspect that buffer.

The macOS builder also audits the final bundled FFmpeg and FFprobe from inside `Cut.app` with a minimal environment. The release fails if either executable cannot start, if ASS/subtitles or libx264 disappeared during collection, or if the packaged binaries still retain absolute Homebrew Cellar/opt references. This keeps build-machine discovery separate from end-user runtime behavior.


## 0.1.4 FFmpeg provisioning repair

The release watcher now uses Homebrew core `ffmpeg@6` as the deterministic build-machine source when it has to provision FFmpeg automatically. The versioned formula is keg-only, has bottled arm64 Sequoia builds, and enables both `libx264` and `libass`; it can therefore coexist with whatever `ffmpeg` command the developer already has on PATH. Existing explicit/full FFmpeg installs remain valid only when they pass the same capability checks.

FFmpeg discovery no longer relies on one ambiguous Homebrew prefix. The watcher checks the versioned formula first, then valid existing formulae, then scans installed Cellar kegs and finally developer fallbacks. Capability validation checks FFmpeg's own `-buildconf` output plus encoder/filter enumeration, and failed candidates are printed verbosely instead of collapsing into a generic error.

A fresh watcher process now initializes a lightweight Python 3 interpreter immediately so update ingestion works before release Python 3.12 is provisioned. Release preflight still pins Homebrew Python 3.12 for the macOS build.

The macOS builder independently prefers `ffmpeg@6` when it is invoked outside the watcher. FFmpeg and FFprobe are supplied to PyInstaller as native binaries, allowing PyInstaller to recursively collect non-system dylibs, rewrite macOS load paths and re-sign collected Mach-O files. The final app audit runs the packaged tools with a minimal environment and rejects any Mach-O dependency in `Contents/MacOS` or `Contents/Frameworks` that still points to Homebrew outside `Cut.app`.

## 0.1.5 — Batched native-runtime audit

The packaged-runtime portability audit now scans only native/loadable candidates
(`*.dylib`, `*.so`, and executable files) and invokes `otool` in batches instead
of launching `file` and `otool` once for every file in the PyInstaller bundle.
This preserves the external-Homebrew dependency check while avoiding the long
silent phase that could look like a hung release build.

## 0.1.6 — SDL3 runtime packaging repair

Homebrew's current SDL2 package is `sdl2-compat`, which presents an SDL2 ABI but loads
SDL3 dynamically at runtime. Because SDL3 is intentionally not a direct Mach-O linkage,
PyInstaller cannot discover it from `otool -L` alone. Cut now detects this arrangement,
embeds `libSDL3.0.dylib` explicitly, rewrites the packaged SDL2 compatibility layer to
search `@loader_path`, and verifies the packaged FFmpeg/FFprobe executables under a hard
timeout before notarization. The watcher also records its PID and automatically removes
stale lock directories left by interrupted releases.
