#!/usr/bin/env bash
set -Eeuo pipefail

# deploy_homepage.sh — build and deploy the Cut homepage.
# Source is never modified; all changes happen in an isolated build copy.

PROJECT_DIR="${PROJECT_DIR:-/Users/smielniczuk/Documents/works/srt-ass-caption-animator/homepage}"
SOURCE_HTML="${SOURCE_HTML:-index.html}"
BUILD_ROOT="${BUILD_ROOT:-$PROJECT_DIR/.deploy_build}"

REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_HOST="${REMOTE_HOST:-yolo.cx}"
REMOTE_PORT="${REMOTE_PORT:-18021}"
REMOTE_DIR="${REMOTE_DIR:-/var/www/mojoworks/labs/cut}"
REMOTE_URL="${REMOTE_URL:-https://mojoworks.xyz/labs/cut/}"
REMOTE_OWNER="${REMOTE_OWNER:-www-data:www-data}"
REMOTE_CHMOD="${REMOTE_CHMOD:-Du=rwx,Dgo=rx,Fu=rw,Fgo=r}"

GITHUB_REPO="${GITHUB_REPO:-sylwesterdigital/srt-ass-caption-animator}"
GITHUB_API="${GITHUB_API:-https://api.github.com}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
RELEASE_CHANNEL="${RELEASE_CHANNEL:-any}"   # any, stable, prerelease
RELEASE_ARCH="${RELEASE_ARCH:-arm64}"
EXPECTED_RELEASE_TAG="${EXPECTED_RELEASE_TAG:-}"

DO_MIN=1
DO_PRECOMPRESS=1
DO_BRAND_UPDATE=1
DO_CACHE_BUST=1
DO_DRY_RUN=0
DO_DELETE_REMOTE=0
DO_VERIFY_REMOTE=1
DROP_CONSOLE=0
KEEP_BUILD=0

RSYNC_BIN="${RSYNC_BIN:-/opt/homebrew/bin/rsync}"
command -v "$RSYNC_BIN" >/dev/null 2>&1 || RSYNC_BIN="$(command -v rsync || true)"

if [[ -t 1 ]]; then
  GREEN="$(printf '\033[32m')"; RED="$(printf '\033[31m')"
  YELLOW="$(printf '\033[33m')"; CYAN="$(printf '\033[36m')"
  BLUE="$(printf '\033[34m')"; BOLD="$(printf '\033[1m')"
  RESET="$(printf '\033[0m')"
else
  GREEN=""; RED=""; YELLOW=""; CYAN=""; BLUE=""; BOLD=""; RESET=""
fi

info(){ printf "%b==>%b %s\n" "$CYAN" "$RESET" "$*"; }
ok(){ printf "%bOK%b %s\n" "${GREEN}${BOLD}" "$RESET" "$*"; }
warn(){ printf "%bWARN:%b %s\n" "${YELLOW}${BOLD}" "$RESET" "$*" >&2; }
die(){ printf "%bERROR:%b %s\n" "${RED}${BOLD}" "$RESET" "$*" >&2; exit 1; }
step(){ printf "\n%b%s%b\n" "${BLUE}${BOLD}" "$*" "$RESET"; }
trap 'code=$?; printf "%bFAILED%b at line %s, exit %s.\n" "${RED}${BOLD}" "$RESET" "$LINENO" "$code" >&2; exit "$code"' ERR

need_bin(){ command -v "$1" >/dev/null 2>&1 || die "Required tool '$1' not found."; }
has_bin(){ command -v "$1" >/dev/null 2>&1; }
retry_cmd(){
  local attempts="$1" delay="$2"; shift 2
  local try=1
  until "$@"; do
    local code=$?
    if [[ "$try" -ge "$attempts" ]]; then return "$code"; fi
    warn "Command failed (attempt $try/$attempts). Retrying in ${delay}s: $*"
    sleep "$delay"
    try=$((try + 1))
  done
}

usage(){
  cat <<'USAGE'
Usage: ./deploy_homepage.sh [options]

Defaults:
  Source: /Users/smielniczuk/Documents/works/srt-ass-caption-animator/homepage
  Target: root@yolo.cx:18021:/var/www/mojoworks/labs/cut
  URL:    https://mojoworks.xyz/labs/cut/

Options:
  --project-dir DIR
  --source FILE
  --remote-dir DIR
  --remote-url URL
  --release-channel any|stable|prerelease
  --release-tag TAG
  --release-arch ARCH
  --no-min
  --drop-console
  --no-precompress
  --no-brand-update
  --no-cache-bust
  --dry-run
  --delete-remote
  --no-verify-remote
  --keep-build
  -h, --help

Environment:
  GITHUB_TOKEN may be set for a private repository or higher API rate limits.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-dir) [[ $# -ge 2 ]] || die "--project-dir requires a value"; PROJECT_DIR="$2"; BUILD_ROOT="$2/.deploy_build"; shift 2 ;;
    --source) [[ $# -ge 2 ]] || die "--source requires a value"; SOURCE_HTML="$2"; shift 2 ;;
    --remote-dir) [[ $# -ge 2 ]] || die "--remote-dir requires a value"; REMOTE_DIR="$2"; shift 2 ;;
    --remote-url) [[ $# -ge 2 ]] || die "--remote-url requires a value"; REMOTE_URL="$2"; shift 2 ;;
    --release-channel) [[ $# -ge 2 ]] || die "--release-channel requires a value"; RELEASE_CHANNEL="$2"; shift 2 ;;
    --release-tag) [[ $# -ge 2 ]] || die "--release-tag requires a value"; EXPECTED_RELEASE_TAG="$2"; shift 2 ;;
    --release-arch) [[ $# -ge 2 ]] || die "--release-arch requires a value"; RELEASE_ARCH="$2"; shift 2 ;;
    --no-min) DO_MIN=0; shift ;;
    --drop-console) DROP_CONSOLE=1; shift ;;
    --no-precompress) DO_PRECOMPRESS=0; shift ;;
    --no-brand-update) DO_BRAND_UPDATE=0; shift ;;
    --no-cache-bust) DO_CACHE_BUST=0; shift ;;
    --dry-run) DO_DRY_RUN=1; shift ;;
    --delete-remote) DO_DELETE_REMOTE=1; shift ;;
    --no-verify-remote) DO_VERIFY_REMOTE=0; shift ;;
    --keep-build) KEEP_BUILD=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
done

case "$RELEASE_CHANNEL" in any|stable|prerelease) ;; *) die "Invalid release channel: $RELEASE_CHANNEL" ;; esac
[[ -d "$PROJECT_DIR" ]] || die "Homepage directory not found: $PROJECT_DIR"
[[ -n "$RSYNC_BIN" ]] || die "rsync not found"
need_bin python3; need_bin curl; need_bin ssh; need_bin "$RSYNC_BIN"
[[ "$DO_PRECOMPRESS" -eq 1 ]] && need_bin gzip

export NO_UPDATE_NOTIFIER=1
export NPM_CONFIG_UPDATE_NOTIFIER=false
export npm_config_update_notifier=false

TERSER=()
if [[ "$DO_MIN" -eq 1 ]]; then
  if has_bin terser; then TERSER=(terser)
  elif [[ -x "$PROJECT_DIR/node_modules/.bin/terser" ]]; then TERSER=("$PROJECT_DIR/node_modules/.bin/terser")
  elif has_bin npx; then TERSER=(npx --yes terser)
  else die "Terser not found. Install Node.js/npm or run with --no-min."; fi
fi

[[ "$DO_PRECOMPRESS" -eq 0 ]] || has_bin brotli || warn "brotli missing; .br files will be skipped"

STAMP="$(date +%Y%m%d%H%M%S)"
DEPLOYED_AT="$(date +%Y-%m-%dT%H:%M:%S%z)"
BUILD_DIR="$BUILD_ROOT/cut-homepage-$STAMP"
API_JSON="$BUILD_DIR/.releases.json"
RELEASE_ENV="$BUILD_DIR/.release.env"
mkdir -p "$BUILD_DIR"

cleanup(){ [[ "$KEEP_BUILD" -eq 1 ]] || rm -rf "$BUILD_DIR"; }
trap cleanup EXIT

step "Copy homepage"
info "Source: $PROJECT_DIR"
info "Target: $REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR"
# Deploy only public website files. This prevents extracted ZIP folders,
# archives, README files, working assets, and old placeholder backups from
# becoming public accidentally.
"$RSYNC_BIN" -a --prune-empty-dirs "$PROJECT_DIR/" "$BUILD_DIR/" \
  --include '/*.html' \
  --include '/*.css' \
  --include '/*.js' \
  --include '/*.mjs' \
  --include '/*.json' \
  --include '/*.xml' \
  --include '/*.txt' \
  --include '/*.webmanifest' \
  --include '/*.ico' \
  --include '/assets/' \
  --exclude '/assets/placeholders/old/***' \
  --include '/assets/***' \
  --include '/fonts/' \
  --include '/fonts/***' \
  --include '/images/' \
  --include '/images/***' \
  --include '/media/' \
  --include '/media/***' \
  --include '/videos/' \
  --include '/videos/***' \
  --exclude '*'

find "$BUILD_DIR" \( -name '.DS_Store' -o -name '._*' \) -delete 2>/dev/null || true

# Defence in depth for the current homepage structure.
rm -rf \
  "$BUILD_DIR/CaptionAnimator-commercial-page-with-png-placeholders" \
  "$BUILD_DIR/assets/placeholders/old"

if [[ ! -f "$BUILD_DIR/$SOURCE_HTML" ]]; then
  fallback="$(find "$BUILD_DIR" -maxdepth 1 -type f -name 'index*.html' | sort | tail -n 1)"
  [[ -n "$fallback" ]] || die "No source HTML found"
  warn "$SOURCE_HTML not found; using $(basename "$fallback")"
  cp "$fallback" "$BUILD_DIR/index.html"
elif [[ "$SOURCE_HTML" != index.html ]]; then
  cp "$BUILD_DIR/$SOURCE_HTML" "$BUILD_DIR/index.html"
fi
[[ -f "$BUILD_DIR/index.html" ]] || die "index.html was not created"
ok "Homepage copied"

step "Resolve latest GitHub release"
CURL_ARGS=(--fail --silent --show-error --location --retry 3 \
  --header 'Accept: application/vnd.github+json' \
  --header 'X-GitHub-Api-Version: 2022-11-28')
[[ -z "$GITHUB_TOKEN" ]] || CURL_ARGS+=(--header "Authorization: Bearer $GITHUB_TOKEN")
if [[ -n "$EXPECTED_RELEASE_TAG" ]]; then
  info "Pinned release tag: $EXPECTED_RELEASE_TAG"
  curl "${CURL_ARGS[@]}"     "$GITHUB_API/repos/$GITHUB_REPO/releases/tags/$EXPECTED_RELEASE_TAG"     -o "$API_JSON"
else
  curl "${CURL_ARGS[@]}"     "$GITHUB_API/repos/$GITHUB_REPO/releases?per_page=100"     -o "$API_JSON"
fi

python3 - "$API_JSON" "$RELEASE_ENV" "$RELEASE_CHANNEL" "$RELEASE_ARCH" "$GITHUB_REPO" "$EXPECTED_RELEASE_TAG" <<'PY'
from pathlib import Path
import json, shlex, sys
src, dst, channel, arch, repo, expected_tag = sys.argv[1:]
data = json.loads(Path(src).read_text())

if expected_tag:
    if not isinstance(data, dict):
        raise SystemExit(f"Unexpected GitHub response for pinned release {expected_tag}")
    if data.get("draft"):
        raise SystemExit(f"Pinned release {expected_tag} is still a draft")
    if str(data.get("tag_name") or "") != expected_tag:
        raise SystemExit(
            f"GitHub returned tag {data.get('tag_name')!r}, expected {expected_tag!r}"
        )
    r = data
else:
    if not isinstance(data, list):
        raise SystemExit(
            data.get("message", "Unexpected GitHub response")
            if isinstance(data, dict)
            else "Unexpected GitHub response"
        )
    items = [release for release in data if not release.get("draft")]
    if channel == "stable":
        items = [release for release in items if not release.get("prerelease")]
    if channel == "prerelease":
        items = [release for release in items if release.get("prerelease")]
    if not items:
        raise SystemExit(f"No {channel} release found for {repo}")
    r = items[0]

if channel == "stable" and r.get("prerelease"):
    raise SystemExit(f"Release {r.get('tag_name')} is a prerelease, not stable")
if channel == "prerelease" and not r.get("prerelease"):
    raise SystemExit(f"Release {r.get('tag_name')} is stable, not a prerelease")

def score(a):
    n = str(a.get("name", "")).lower()
    s = 1000 if n.endswith(".dmg") else 700 if n.endswith(".zip") else -500
    s += 160 if "macos" in n or "mac-os" in n else 0
    s += 140 if arch.lower() in n else 0
    s += 80 if "cut" in n else 0
    s -= 1000 if any(x in n for x in ("sha256", "checksum", ".txt", ".json")) else 0
    return s
assets = r.get("assets") or []
a = max(assets, key=score) if assets else None
if a and score(a) < 0: a = None
release_url = str(r.get("html_url") or f"https://github.com/{repo}/releases")
values = {
    "RELEASE_TAG": str(r.get("tag_name") or ""),
    "RELEASE_NAME": str(r.get("name") or r.get("tag_name") or ""),
    "RELEASE_URL": release_url,
    "DOWNLOAD_URL": str((a or {}).get("browser_download_url") or release_url),
    "DOWNLOAD_ASSET_NAME": str((a or {}).get("name") or ""),
    "RELEASE_PUBLISHED_AT": str(r.get("published_at") or r.get("created_at") or ""),
    "RELEASE_IS_PRERELEASE": "1" if r.get("prerelease") else "0",
}
Path(dst).write_text("\n".join(f"{k}={shlex.quote(v)}" for k,v in values.items()) + "\n")
PY
# shellcheck disable=SC1090
source "$RELEASE_ENV"
if [[ -n "$EXPECTED_RELEASE_TAG" && "$RELEASE_TAG" != "$EXPECTED_RELEASE_TAG" ]]; then
  die "Resolved release $RELEASE_TAG, expected $EXPECTED_RELEASE_TAG."
fi
info "Release: $RELEASE_TAG — $RELEASE_NAME"
info "Release page: $RELEASE_URL"
info "Download: $DOWNLOAD_URL"
[[ -z "$DOWNLOAD_ASSET_NAME" ]] || info "Asset: $DOWNLOAD_ASSET_NAME"

if [[ -n "$DOWNLOAD_ASSET_NAME" && "$DOWNLOAD_ASSET_NAME" != Cut-* ]]; then
  warn "The newest GitHub release still uses the legacy asset name: $DOWNLOAD_ASSET_NAME"
  warn "The homepage link is correct for the newest published release. It will switch to a Cut-* asset after the next Cut release is published."
fi

curl --fail --silent --show-error --location --head --retry 3 "$RELEASE_URL" >/dev/null
ok "Release page verified"

step "Update release links"
BUILD_DIR="$BUILD_DIR" GITHUB_REPO="$GITHUB_REPO" RELEASE_TAG="$RELEASE_TAG" \
RELEASE_NAME="$RELEASE_NAME" RELEASE_URL="$RELEASE_URL" DOWNLOAD_URL="$DOWNLOAD_URL" \
DOWNLOAD_ASSET_NAME="$DOWNLOAD_ASSET_NAME" RELEASE_PUBLISHED_AT="$RELEASE_PUBLISHED_AT" \
RELEASE_IS_PRERELEASE="$RELEASE_IS_PRERELEASE" DEPLOYED_AT="$DEPLOYED_AT" \
REMOTE_URL="$REMOTE_URL" DO_BRAND_UPDATE="$DO_BRAND_UPDATE" DO_CACHE_BUST="$DO_CACHE_BUST" STAMP="$STAMP" \
python3 <<'PY'
from pathlib import Path
import html, json, os, re
from urllib.parse import quote
root = Path(os.environ["BUILD_DIR"])
repo = os.environ["GITHUB_REPO"]
repo_url = f"https://github.com/{repo}"
tag = os.environ["RELEASE_TAG"]
name = os.environ["RELEASE_NAME"]
release_url = os.environ["RELEASE_URL"]
download_url = os.environ["DOWNLOAD_URL"]
asset = os.environ["DOWNLOAD_ASSET_NAME"]
published = os.environ["RELEASE_PUBLISHED_AT"]
deployed = os.environ["DEPLOYED_AT"]
remote = os.environ["REMOTE_URL"]
brand = os.environ["DO_BRAND_UPDATE"] == "1"
cache = os.environ["DO_CACHE_BUST"] == "1"
token = quote(tag or os.environ["STAMP"], safe="-._")

anchor = re.compile(r'(?P<prefix><a\b[^>]*?\bhref\s*=\s*)(?P<q>["\'])(?P<href>.*?)(?P=q)(?P<suffix>[^>]*>)(?P<body>.*?)(?P<close></a>)', re.I|re.S)
asset_ref = re.compile(r'(?P<prefix>\b(?:src|href)\s*=\s*)(?P<q>["\'])(?P<path>(?!https?:|//|data:|mailto:|tel:|#)[^"\']+?\.(?:js|mjs|css))(?P<tail>[?#][^"\']*)?(?P=q)', re.I)

def visible(s): return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip().lower()
def patch(m):
    href = html.unescape(m.group("href")).strip()
    if not href.startswith(repo_url + "/releases"): return m.group(0)
    text = visible(m.group("body")); opening = (m.group("prefix") + m.group("suffix")).lower()
    target = release_url if ("view" in text and "release" in text) else download_url if ("download" in text or "nav-download" in opening) else release_url
    return m.group("prefix") + m.group("q") + html.escape(target, quote=True) + m.group("q") + m.group("suffix") + m.group("body") + m.group("close")

def meta(doc, key, value):
    tag_html = f'<meta name="{html.escape(key, quote=True)}" content="{html.escape(value, quote=True)}">'
    p = re.compile(rf'<meta\s+name=["\']{re.escape(key)}["\'][^>]*>', re.I)
    return p.sub(tag_html, doc, count=1) if p.search(doc) else re.sub(r'(<head\b[^>]*>)', r'\1\n  ' + tag_html, doc, count=1, flags=re.I)

count = 0
for path in sorted(root.rglob("*.html")):
    doc = path.read_text(encoding="utf-8")
    if brand:
        doc = doc.replace("Caption Animator", "Cut").replace("CAPTION ANIMATOR", "CUT").replace("CaptionAnimator", "Cut")
    for old,new in {
        "__LATEST_RELEASE_TAG__": tag,
        "__LATEST_RELEASE_NAME__": name,
        "__LATEST_RELEASE_URL__": release_url,
        "__LATEST_DOWNLOAD_URL__": download_url,
        "__LATEST_ASSET_NAME__": asset,
    }.items(): doc = doc.replace(old,new)
    count += sum(1 for m in anchor.finditer(doc) if html.unescape(m.group("href")).startswith(repo_url + "/releases"))
    doc = anchor.sub(patch, doc)
    for key,value in {
        "cut-release-tag": tag,
        "cut-release-name": name,
        "cut-release-url": release_url,
        "cut-download-url": download_url,
        "cut-download-asset": asset,
        "cut-release-published-at": published,
        "cut-deployed-at": deployed,
        "cut-deploy-url": remote,
    }.items(): doc = meta(doc,key,value)
    if cache:
        doc = asset_ref.sub(lambda m: m.group("prefix") + m.group("q") + m.group("path") + f"?v={token}" + m.group("q"), doc)
    path.write_text(doc, encoding="utf-8")

(root / "release.json").write_text(json.dumps({
    "product":"Cut", "repository":repo, "tag":tag, "name":name,
    "release_url":release_url, "download_url":download_url,
    "asset_name":asset, "published_at":published,
    "prerelease":os.environ["RELEASE_IS_PRERELEASE"] == "1",
    "deployed_at":deployed, "deployment_url":remote,
    "release_links_updated":count,
}, indent=2) + "\n")
print(f"Updated {count} release link(s).")
PY

if grep -RIn --include='*.html' "https://github.com/$GITHUB_REPO/releases/latest" "$BUILD_DIR" >/dev/null 2>&1; then
  die "A stale /releases/latest link remains"
fi
ok "Release links updated"

step "Validate public build contents"
if find "$BUILD_DIR" -mindepth 1 -maxdepth 1 -type d \
  ! -name assets \
  ! -name fonts \
  ! -name images \
  ! -name media \
  ! -name videos \
  -print -quit | grep -q .; then
  warn "Unexpected top-level directories were found:"
  find "$BUILD_DIR" -mindepth 1 -maxdepth 1 -type d \
    ! -name assets \
    ! -name fonts \
    ! -name images \
    ! -name media \
    ! -name videos \
    -print
  die "Refusing to deploy unexpected directories."
fi

if [[ -d "$BUILD_DIR/assets/placeholders/old" ]]; then
  die "Refusing to deploy assets/placeholders/old."
fi

ok "Public build contains only approved site directories"

step "Minify JavaScript"
if [[ "$DO_MIN" -eq 1 ]]; then
  count=0
  while IFS= read -r -d '' file; do
    case "$file" in *.min.js|*.min.mjs) continue ;; esac
    tmp="$file.deploy-min"
    compress="passes=2"; [[ "$DROP_CONSOLE" -eq 0 ]] || compress="$compress,drop_console=true"
    args=("$file" --ecma 2022 --compress "$compress" --mangle --comments false --output "$tmp")
    case "$file" in *.mjs) args+=(--module) ;; esac
    "${TERSER[@]}" "${args[@]}"
    [[ -s "$tmp" ]] || die "Empty minified file: $file"
    mv "$tmp" "$file"; count=$((count+1))
  done < <(find "$BUILD_DIR" -type f \( -name '*.js' -o -name '*.mjs' \) -print0)
  ok "Minified $count JavaScript file(s)"
else
  info "JavaScript minification skipped"
fi

step "Validate local page assets"
BUILD_DIR="$BUILD_DIR" python3 <<'PY'
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
import os, sys
root = Path(os.environ["BUILD_DIR"]).resolve(); missing=[]
class P(HTMLParser):
    def __init__(self): super().__init__(); self.refs=[]
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag in {"script","img","source","video","audio","iframe"} and a.get("src"): self.refs.append(a["src"])
        if tag == "link" and a.get("href"): self.refs.append(a["href"])
for page in root.rglob("*.html"):
    p=P(); p.feed(page.read_text(encoding="utf-8"))
    for ref in p.refs:
        if not ref or ref.startswith(("#","data:","mailto:","tel:","javascript:","//")) or "://" in ref: continue
        clean=unquote(urlsplit(ref).path)
        target=(root/clean.lstrip("/")) if clean.startswith("/") else (page.parent/clean)
        if not target.resolve().exists(): missing.append((page.relative_to(root),ref))
if missing:
    print("Missing local assets:", file=sys.stderr)
    for page,ref in missing[:50]: print(f"  {page} -> {ref}", file=sys.stderr)
    raise SystemExit(1)
print("All local assets exist.")
PY
ok "Page assets validated"
rm -f "$API_JSON" "$RELEASE_ENV"

if [[ "$DO_PRECOMPRESS" -eq 1 ]]; then
  step "Create gzip/Brotli files"
  gz=0; br=0
  while IFS= read -r -d '' file; do
    gzip -9 -kf "$file"; gz=$((gz+1))
    if has_bin brotli; then brotli -f -q 11 "$file"; br=$((br+1)); fi
  done < <(find "$BUILD_DIR" -type f \( -name '*.html' -o -name '*.css' -o -name '*.js' -o -name '*.mjs' -o -name '*.json' -o -name '*.svg' -o -name '*.xml' -o -name '*.txt' \) ! -name '*.gz' ! -name '*.br' -print0)
  ok "Created $gz gzip and $br Brotli file(s)"
fi

step "Deploy"
if [[ "$DO_DRY_RUN" -eq 0 ]]; then
  retry_cmd 4 8 ssh -o BatchMode=yes -o ConnectTimeout=15 \
    -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" "mkdir -p '$REMOTE_DIR'"
else
  info "Dry run: remote mkdir skipped"
fi

flags=(
  -avz
  --human-readable
  --itemize-changes
  --chmod="$REMOTE_CHMOD"
  --partial
  --partial-dir=.rsync-partial
  --delay-updates
)
[[ "$DO_DRY_RUN" -eq 0 ]] || flags+=(--dry-run)
[[ "$DO_DELETE_REMOTE" -eq 0 ]] || flags+=(--delete-delay)
use_chown=0
if "$RSYNC_BIN" --help 2>&1 | grep -q -- '--chown'; then flags+=(--chown="$REMOTE_OWNER"); use_chown=1; fi
retry_cmd 4 10 "$RSYNC_BIN" "${flags[@]}" \
  -e "ssh -o BatchMode=yes -o ConnectTimeout=15 -p $REMOTE_PORT" \
  "$BUILD_DIR/" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_DIR/"
ok "Rsync completed"

if [[ "$DO_DRY_RUN" -eq 0 && "$use_chown" -eq 0 ]]; then
  retry_cmd 4 8 ssh -o BatchMode=yes -o ConnectTimeout=15 \
    -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" "chown -R '$REMOTE_OWNER' '$REMOTE_DIR'"
fi

if [[ "$DO_DRY_RUN" -eq 0 && "$DO_VERIFY_REMOTE" -eq 1 ]]; then
  step "Verify public page"
  tmp="$(mktemp "${TMPDIR:-/tmp}/cut-homepage.XXXXXX")"
  if curl --fail --silent --show-error --location --retry 3 "${REMOTE_URL%/}/?deploy=$STAMP" -o "$tmp"; then
    grep -qi '<title[^>]*>.*Cut' "$tmp" && ok "Public Cut homepage verified" || warn "Page reachable, but title does not contain Cut"
  else
    warn "Public URL verification failed: $REMOTE_URL"
  fi
  rm -f "$tmp"
fi

[[ "$KEEP_BUILD" -eq 0 ]] || info "Build retained: $BUILD_DIR"
step "Finished"

if [[ "$DO_DRY_RUN" -eq 1 ]]; then
  ok "Dry run completed. No remote files were changed."
  info "Deployment target: $REMOTE_URL"
else
  ok "Deployed to $REMOTE_URL"
fi

ok "Download links target ${DOWNLOAD_ASSET_NAME:-$RELEASE_URL}"
