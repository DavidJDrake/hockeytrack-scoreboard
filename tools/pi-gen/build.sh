#!/usr/bin/env bash
# Build the scoreboard image with pi-gen in Docker.
#
#   tools/pi-gen/build.sh <version> <workdir>
#
# Leaves one <workdir>/deploy/*-scoreboard.img.xz. Needs Docker with
# --privileged and about 25 GB free. CI runs this; see
# .github/workflows/image.yml.
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
git clone --quiet --branch arm64 https://github.com/RPi-Distro/pi-gen.git "$WORK"
git -C "$WORK" checkout --quiet --detach "$REF"
[ "$(git -C "$WORK" rev-parse HEAD)" = "$REF" ] || { echo "build.sh: pi-gen is not at $REF" >&2; exit 1; }

cp "$HERE/config" "$WORK/config"
cp -a "$HERE/stage-scoreboard" "$WORK/stage-scoreboard"
# Stage 2 is Raspberry Pi OS Lite; only the scoreboard stage exports an image.
touch "$WORK/stage2/SKIP_IMAGES"

# What the appliance needs, as an explicit list. The device's config directory
# holds a developer's own panel identity and must never reach an image.
files="$WORK/stage-scoreboard/01-install/files"
mkdir -p "$files/device"
cp -a "$REPO/device/scoreboard" "$REPO/device/requirements.txt" "$REPO/device/certs" \
  "$REPO/device/polkit" "$REPO/device/scoreboard-appliance.service" \
  "$REPO/device/scoreboard-netcfg.service" "$files/device/"
find "$files/device" -name '__pycache__' -type d -prune -exec rm -rf {} +
cp "$REPO/tools/pi-setup.sh" "$files/pi-setup.sh"
printf '%s · %s · %s\n' "$VERSION" "$(date -u +%Y-%m-%d)" "$(git -C "$REPO" rev-parse --short HEAD)" \
  >"$files/scoreboard-build"

(cd "$WORK" && ./build-docker.sh)

count="$(find "$WORK/deploy" -maxdepth 1 -name '*-scoreboard.img.xz' | wc -l)"
[ "$count" -eq 1 ] || { echo "build.sh: expected one image in $WORK/deploy, found $count" >&2; exit 1; }
