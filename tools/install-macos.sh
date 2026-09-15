#!/bin/bash
# Commission Dashboard — one-command installer for macOS.
#
# WHY THIS EXISTS. The app is signed ad-hoc: no Apple Developer ID and no
# notarisation ticket, because notarisation needs a paid Apple account this
# project does not have. An ad-hoc signature is enough for the kernel to exec
# the binary on Apple Silicon, but not enough for Gatekeeper -- so a Mac that
# downloaded the disk image IN A BROWSER refuses to open the app and says it
# "cannot be verified".
#
# That refusal is not triggered by the signature. It is triggered by the
# com.apple.quarantine extended attribute, and ONLY a browser sets it. curl
# does not. A disk image fetched by this script arrives with no quarantine
# attribute, the app copied out of it inherits none, and Gatekeeper never has
# anything to object to. Verified on macOS 15.6 / Apple M4 against the v1.2.34
# arm64 image: `xattr -l` on the .app printed nothing, and the app launched
# with no prompt of any kind.
#
# THE REPOSITORY IS PRIVATE, so every URL below needs a token -- release assets
# inherit repository visibility and return 404 to anonymous callers. That is
# deliberate: the repo went private because a SQLite database carrying password
# hashes had been committed to it, and because a seeded installer on a public
# release would hand live database credentials to anyone who found the URL.
#
# Supply the token by any of:
#   GITHUB_TOKEN=ghp_... bash install-macos.sh
#   gh auth login                     (the script picks the token up)
#   run it with no token and paste one when prompted
#
# The token needs only Contents:read on this repository. It is never written to
# disk by this script and never appears in the output.

set -euo pipefail

REPO="NurulAqilahSaifulBahril/Commission"
API="https://api.github.com/repos/$REPO"
APP_NAME="CommissionDashboard.app"
DEST="/Applications/$APP_NAME"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31mFailed: %s\033[0m\n\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "this installer is for macOS only."

# Intel Macs and Apple Silicon get different images. uname -m is the arch of
# this shell, which is what we want: a Terminal running under Rosetta should
# still get the image it can actually execute.
case "$(uname -m)" in
  arm64)  ARCH_TAG="macos-arm64"; ARCH_HUMAN="Apple Silicon" ;;
  x86_64) ARCH_TAG="macos-intel"; ARCH_HUMAN="Intel" ;;
  *)      die "unrecognised architecture '$(uname -m)'." ;;
esac

say "Commission Dashboard installer"
info "This Mac: $ARCH_HUMAN ($(uname -m)), macOS $(sw_vers -productVersion)"

# ── Token ────────────────────────────────────────────────────────────────────
TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
if [ -z "$TOKEN" ] && command -v gh >/dev/null 2>&1; then
  TOKEN=$(gh auth token 2>/dev/null || true)
  [ -n "$TOKEN" ] && info "Using the token from the GitHub CLI."
fi
if [ -z "$TOKEN" ]; then
  # -s so it is not echoed to the screen or captured by a scrollback buffer.
  printf '\n  This repository is private, so a GitHub token is needed.\n'
  printf '  Create one at https://github.com/settings/tokens with Contents:read.\n'
  printf '  Token (input hidden): '
  read -rs TOKEN < /dev/tty || true
  printf '\n'
fi
[ -n "$TOKEN" ] || die "no token supplied, so the private release cannot be read."

auth=(-H "Authorization: Bearer $TOKEN" -H "X-GitHub-Api-Version: 2022-11-28")

# Parsing GitHub's JSON needs more than awk can honestly manage: an asset entry
# carries several "id" fields (its own, and the uploader's). Use a real parser.
command -v python3 >/dev/null 2>&1 \
  || die "python3 is required to read the release listing.
    Install Apple's command line tools with:  xcode-select --install"

say "Finding the latest release..."
REL=$(curl -fsSL "${auth[@]}" -H "Accept: application/vnd.github+json" \
        "$API/releases/latest" 2>/dev/null) \
  || die "could not read the release listing.
    The token may be wrong, expired, or lack access to $REPO."

TAG=$(printf '%s' "$REL" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tag_name",""))')
[ -n "$TAG" ] || die "the release listing carried no tag name."
VERSION="${TAG#v}"
info "Latest is $TAG"

DMG_NAME="CommissionDashboard-Setup-$VERSION-$ARCH_TAG.dmg"

# A private asset cannot be fetched from its browser_download_url. It has to be
# requested from the API by asset id, with Accept: application/octet-stream.
ASSET_ID=$(printf '%s' "$REL" | python3 -c "
import json,sys
want = '$DMG_NAME'
for a in json.load(sys.stdin).get('assets', []):
    if a.get('name') == want:
        print(a.get('id')); break
")
[ -n "$ASSET_ID" ] || die "$DMG_NAME is not attached to $TAG.
    The release may still be building, or this Mac's architecture was not published."

WORK=$(mktemp -d)
MNT="$WORK/mnt"
cleanup() {
  [ -d "$MNT" ] && hdiutil detach "$MNT" -quiet 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

say "Downloading $DMG_NAME (about 170 MB)..."
curl -fL --progress-bar "${auth[@]}" -H "Accept: application/octet-stream" \
     -o "$WORK/app.dmg" "$API/releases/assets/$ASSET_ID" \
  || die "download failed. Check your internet connection and try again."

# SHA256SUMS.txt covers every asset. Checking it turns a truncated download into
# a clear message now, rather than a "damaged" dialog after the app is already
# in /Applications.
say "Verifying the download..."
SUMS_ID=$(printf '%s' "$REL" | python3 -c "
import json,sys
for a in json.load(sys.stdin).get('assets', []):
    if a.get('name') == 'SHA256SUMS.txt':
        print(a.get('id')); break
")
if [ -n "$SUMS_ID" ] && curl -fsSL "${auth[@]}" -H "Accept: application/octet-stream" \
     -o "$WORK/SHA256SUMS.txt" "$API/releases/assets/$SUMS_ID" 2>/dev/null; then
  EXPECTED=$(awk -v f="$DMG_NAME" '$2 == f || $2 == "*"f {print $1; exit}' "$WORK/SHA256SUMS.txt")
  ACTUAL=$(shasum -a 256 "$WORK/app.dmg" | awk '{print $1}')
  if [ -n "$EXPECTED" ] && [ "$EXPECTED" != "$ACTUAL" ]; then
    die "checksum mismatch - the download is corrupted. Run the command again.
    expected $EXPECTED
    got      $ACTUAL"
  fi
  info "Checksum OK"
else
  info "Checksums unavailable; skipping (not fatal)."
fi

# A running copy cannot be replaced safely: files change underneath a live
# process and the next launch reads a half-updated bundle.
if pgrep -qx "CommissionDashboard" 2>/dev/null; then
  say "Closing the running copy..."
  osascript -e 'quit app "CommissionDashboard"' 2>/dev/null || true
  for _ in $(seq 1 10); do
    pgrep -qx "CommissionDashboard" 2>/dev/null || break
    sleep 1
  done
fi

say "Installing to /Applications..."
mkdir -p "$MNT"
hdiutil attach "$WORK/app.dmg" -mountpoint "$MNT" -nobrowse -readonly -quiet \
  || die "could not open the disk image."
[ -d "$MNT/$APP_NAME" ] || die "the disk image does not contain $APP_NAME."

# ditto rather than cp -R: it preserves the symlinks inside Electron
# Framework.framework and the xattrs on the signed bundle. cp -R can flatten
# Versions/Current, and a flattened framework invalidates the code signature --
# which macOS reports as "damaged", the exact failure this script avoids.
rm -rf "$DEST" 2>/dev/null || sudo rm -rf "$DEST"
if ! ditto "$MNT/$APP_NAME" "$DEST" 2>/dev/null; then
  info "Need administrator permission to write to /Applications."
  sudo ditto "$MNT/$APP_NAME" "$DEST" || die "could not copy the app into /Applications."
  sudo chown -R "$(id -u):$(id -g)" "$DEST" 2>/dev/null || true
fi

# Belt and braces. A curl download carries no quarantine, so there should be
# nothing to remove -- but this keeps the result identical if someone ever
# routes a browser-downloaded image through here.
xattr -d -r com.apple.quarantine "$DEST" 2>/dev/null || true

codesign --verify --deep --strict "$DEST" 2>/dev/null \
  || die "the installed app fails signature verification. Run the command again."

say "Installed: Commission Dashboard $VERSION"
info "No Gatekeeper prompt: the image was fetched by curl, which does not set"
info "the quarantine flag that triggers it."
echo
info "First launch unpacks about 700 MB and takes a minute or two. The window"
info "says what it is doing. Later launches start straight away."
info "Then sign in with the username and password IT gave you."
echo

open -a "$DEST" 2>/dev/null || info "Open it from your Applications folder."
