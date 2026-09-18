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
