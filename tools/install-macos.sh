#!/bin/bash
# Commission Dashboard — one-command installer for macOS.
#
# Goes in the repository at tools/install-macos.sh.
#
# WHY THIS EXISTS. The app is signed ad-hoc: it has no Apple Developer ID and
# no notarisation ticket, because notarisation needs a paid Apple account this
# project does not have. An ad-hoc signature is enough for the kernel to exec
# the binary on Apple Silicon, but not enough for Gatekeeper -- so a Mac that
# downloaded the disk image IN A BROWSER refuses to open the app and says it
# "cannot be verified".
#
# That refusal is not triggered by the signature. It is triggered by the
# com.apple.quarantine extended attribute, and ONLY a browser sets it. curl
# does not. A disk image fetched by this script arrives with no quarantine
# attribute at all, the app copied out of it inherits none, and Gatekeeper
# never has anything to object to. Verified on macOS 15.6 / Apple M4 against
# the v1.2.33 arm64 image: `xattr -l` on the .app printed nothing.
#
# So the browser route costs a failed launch, a trip to System Settings, an
# "Open Anyway" button that does not exist until a launch has been blocked and
# expires about an hour later, and an admin password. This route costs one
# paste. Same app, same signature -- the difference is only how it arrived.
#
# Run it with:
#   curl -fsSL https://raw.githubusercontent.com/NurulAqilahSaifulBahril/Commission/main/tools/install-macos.sh | bash
#
# Or, to read it before running it (recommended, and the same two commands IT
# would use):
#   curl -fsSL https://raw.githubusercontent.com/NurulAqilahSaifulBahril/Commission/main/tools/install-macos.sh -o install.sh
#   less install.sh && bash install.sh

set -euo pipefail

REPO="NurulAqilahSaifulBahril/Commission"
APP_NAME="CommissionDashboard.app"
DEST="/Applications/$APP_NAME"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
die()  { printf '\n\033[31mFailed: %s\033[0m\n\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "this installer is for macOS only."

# Intel Macs and Apple Silicon get different images; installing the wrong one
# either will not launch or runs under translation. uname -m is the arch of
# this shell, which is what we want -- not the arch of the hardware, since a
# Terminal running under Rosetta should still get the image it can execute.
case "$(uname -m)" in
  arm64)  ARCH_TAG="macos-arm64"; ARCH_HUMAN="Apple Silicon" ;;
  x86_64) ARCH_TAG="macos-intel"; ARCH_HUMAN="Intel" ;;
  *)      die "unrecognised architecture '$(uname -m)'." ;;
esac

say "Commission Dashboard installer"
info "This Mac: $ARCH_HUMAN ($(uname -m)), macOS $(sw_vers -productVersion)"

# Resolve the newest published release rather than pinning a version, so the
# command in the release notes does not go stale the moment the next tag lands.
say "Finding the latest release..."
TAG=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
      | awk -F'"' '/"tag_name"/ {print $4; exit}')
[ -n "${TAG:-}" ] || die "could not reach GitHub to find the latest release. Check your internet connection."
VERSION="${TAG#v}"
info "Latest is $TAG"

DMG_NAME="CommissionDashboard-Setup-$VERSION-$ARCH_TAG.dmg"
BASE="https://github.com/$REPO/releases/download/$TAG"

# Everything happens in a scratch directory that is removed on any exit path,
# success or failure, so a cancelled install leaves a 170 MB file nowhere.
WORK=$(mktemp -d)
MNT="$WORK/mnt"
cleanup() {
  [ -d "$MNT" ] && hdiutil detach "$MNT" -quiet 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

say "Downloading $DMG_NAME (about 170 MB)..."
curl -fL --progress-bar -o "$WORK/app.dmg" "$BASE/$DMG_NAME" \
  || die "download failed. Check your internet connection and try again."

# The release ships SHA256SUMS.txt covering every asset. Checking it here turns
# a truncated or corrupted download into a clear message now, rather than a
# "damaged" dialog after the app is already in /Applications.
say "Verifying the download..."
if curl -fsSL -o "$WORK/SHA256SUMS.txt" "$BASE/SHA256SUMS.txt" 2>/dev/null; then
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

# A running copy cannot be replaced safely: the files change underneath a live
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
# Framework.framework and the extended attributes on the signed bundle. cp -R
# can flatten Versions/Current, and a flattened framework invalidates the code
# signature -- which macOS reports as "damaged", the exact failure this whole
# script exists to avoid.
rm -rf "$DEST" 2>/dev/null || sudo rm -rf "$DEST"
if ! ditto "$MNT/$APP_NAME" "$DEST" 2>/dev/null; then
  info "Need administrator permission to write to /Applications."
  sudo ditto "$MNT/$APP_NAME" "$DEST" || die "could not copy the app into /Applications."
  sudo chown -R "$(id -u):$(id -g)" "$DEST" 2>/dev/null || true
fi

# Belt and braces. A curl download carries no quarantine, so there should be
# nothing here to remove -- but if someone ever pipes a browser-downloaded
# image through this script, or GitHub changes what it sets, this keeps the
# result identical either way. It is a no-op in the normal case.
xattr -d -r com.apple.quarantine "$DEST" 2>/dev/null || true

# If this fails the bundle was damaged in transit and the app would show the
# "damaged" dialog on launch. Better to say so here, naming the real cause.
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
