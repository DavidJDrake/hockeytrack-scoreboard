#!/usr/bin/env bash
# Build the scoreboard image with pi-gen in Docker.
#
#   tools/pi-gen/build.sh <version> <workdir>
#
# Leaves one <workdir>/deploy/*-scoreboard.img.xz: pi-gen's two-partition
# image, which tools/image-layout.sh then turns into the six-partition A/B
# card image and the update payloads. Needs Docker with --privileged and
# about 25 GB free. CI runs this; see .github/workflows/image.yml.
set -euo pipefail

VERSION="${1:?usage: build.sh <version> <workdir>}"
WORK="${2:?usage: build.sh <version> <workdir>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
REF="$(tr -d '[:space:]' <"$HERE/PIGEN_REF")"

case "$VERSION" in
  *[!A-Za-z0-9.-]* | "") echo "build.sh: bad version: $VERSION" >&2; exit 2 ;;
esac

rm -rf "$WORK"
# Fetch the pinned commit itself rather than cloning the arm64 branch and
# checking the commit out: a force-push upstream can drop the commit from the
# branch, but GitHub still serves it by SHA until it is garbage-collected.
git init --quiet "$WORK"
git -C "$WORK" remote add origin https://github.com/RPi-Distro/pi-gen.git
git -C "$WORK" fetch --quiet --depth 1 origin "$REF"
git -C "$WORK" checkout --quiet --detach FETCH_HEAD
[ "$(git -C "$WORK" rev-parse HEAD)" = "$REF" ] || { echo "build.sh: pi-gen is not at $REF" >&2; exit 1; }

cp "$HERE/config" "$WORK/config"
cp -a "$HERE/stage-scoreboard" "$WORK/stage-scoreboard"
# Stage 2 is Raspberry Pi OS Lite; only the scoreboard stage exports an image.
touch "$WORK/stage2/SKIP_IMAGES"
# No cloud-init (see config). ENABLE_CLOUD_INIT=0 only skips the sub-stage's
# 01-run.sh; its 00-packages would still install cloud-init and
# rpi-cloud-init-mods, so the whole sub-stage is skipped.
touch "$WORK/stage2/04-cloud-init/SKIP"

# What the appliance needs, as an explicit list. The device's config directory
# holds a developer's own panel identity and must never reach an image.
files="$WORK/stage-scoreboard/01-install/files"
mkdir -p "$files/device"
cp -a "$REPO/device/scoreboard" "$REPO/device/requirements.txt" "$REPO/device/certs" \
  "$REPO/device/polkit" "$REPO/device/generators" "$REPO/device/system.conf.d" \
  "$REPO/device/NetworkManager.service.d" \
  "$REPO/device/scoreboard-appliance.service" \
  "$REPO/device/scoreboard-netcfg.service" \
  "$REPO/device/scoreboard-journal-prune" \
  "$REPO/device/scoreboard-journal-prune.service" "$files/device/"
find "$files/device" -name '__pycache__' -type d -prune -exec rm -rf {} +
cp "$REPO/tools/pi-setup.sh" "$files/pi-setup.sh"
printf '%s · %s · %s\n' "$VERSION" "$(date -u +%Y-%m-%d)" "$(git -C "$REPO" rev-parse --short HEAD)" \
  >"$files/scoreboard-build"

(cd "$WORK" && ./build-docker.sh)

count="$(find "$WORK/deploy" -maxdepth 1 -name '*-scoreboard.img.xz' | wc -l)"
[ "$count" -eq 1 ] || { echo "build.sh: expected one image in $WORK/deploy, found $count" >&2; exit 1; }
