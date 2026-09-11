#!/usr/bin/env bash
# Set up a Raspberry Pi to run the scoreboard. Run it on the Pi, from this
# checkout, as the user the service should run as -- it uses sudo itself:
#
#   tools/pi-setup.sh               check, install, enable and start
#   tools/pi-setup.sh --preflight   only the read-only checks
#   tools/pi-setup.sh --print-unit  print the systemd unit it would install
#
# Every manual step this replaces can be half-done in a way that ends in the
# same symptom, a black panel with nothing useful in the log.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVICE="$REPO/device"
CONFIG="$DEVICE/config"
OS_RELEASE="${OS_RELEASE:-/etc/os-release}"
USER_NAME="$(id -un)"

die() { echo "pi-setup: $*" >&2; exit 1; }

render_unit() {
  case "$USER_NAME$DEVICE" in
    *'|'* | *'&'* | *\\*) die "cannot template a user or path containing | & or \\: $USER_NAME $DEVICE" ;;
  esac
  sed -e "s|@USER@|$USER_NAME|g" -e "s|@DEVICE_DIR@|$DEVICE|g" "$DEVICE/scoreboard.service"
}

preflight() {
  local codename mode f
  codename="$(. "$OS_RELEASE" && echo "${VERSION_CODENAME:-}")"
  case "$codename" in
    buster | bullseye | bookworm)
      die "Raspberry Pi OS $codename is too old. Its python3-pygame fails device/requirements.txt, so pip would install a PyPI wheel instead, and those are built without the kmsdrm driver the panel needs. Flash Raspberry Pi OS Lite (64-bit), Trixie or later." ;;
  esac
  for f in device.json device.pem.crt private.pem.key AmazonRootCA1.pem; do
    [ -f "$CONFIG/$f" ] || die "missing $CONFIG/$f -- copy device/config/ over from the machine that ran make provision"
  done
  mode="$(stat -c %a "$CONFIG/private.pem.key")"
  case "$mode" in
    600 | 400) ;;
    *) die "$CONFIG/private.pem.key is mode $mode. It is this device's identity; make it owner-only: chmod 600 $CONFIG/private.pem.key" ;;
  esac
}

install() {
  [ "$(id -u)" -ne 0 ] || die "run this as the user the service should run as, not root; it uses sudo where it must"
  preflight

  echo "==> apt packages"
  sudo apt-get update
  # The distribution's pygame, because its SDL has kmsdrm; gpiozero for the
  # optional buttons (the code no-ops without it).
  sudo apt-get install -y python3-pygame python3-gpiozero python3-venv

  echo "==> virtualenv"
  python3 -m venv --system-site-packages "$DEVICE/.venv"
  "$DEVICE/.venv/bin/pip" install -r "$DEVICE/requirements.txt"
  local where
  where="$(PYGAME_HIDE_SUPPORT_PROMPT=1 "$DEVICE/.venv/bin/python" -c 'import os, pygame; print(os.path.dirname(pygame.__file__))')"
  case "$where" in
    /usr/lib/python3/dist-packages/*) ;;
    *) die "the venv is using pygame from $where, not the system package, so it has no kmsdrm driver. Delete $DEVICE/.venv, check that apt's python3-pygame satisfies device/requirements.txt, and run this again." ;;
  esac

  echo "==> groups"
  local groups=video,render
  if getent group gpio >/dev/null; then groups="$groups,gpio"; fi
  # systemd resolves the user's groups each time it starts the unit, so the
  # service gets these without a re-login; interactive shells need one.
  sudo usermod -aG "$groups" "$USER_NAME"

  echo "==> systemd unit"
  # Rendered before tee opens the target, so a failure cannot leave an empty unit behind.
  local unit
  unit="$(render_unit)"
  printf '%s\n' "$unit" | sudo tee /etc/systemd/system/scoreboard.service >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable scoreboard
  sudo systemctl restart scoreboard

  echo
  echo "Installed. Watch it with: journalctl -u scoreboard -f"
  echo "Its first line names the video driver, the display size and the rotation chosen."
}

case "${1:-}" in
  --print-unit) render_unit ;;
  --preflight) preflight && echo "preflight ok" ;;
  "") install ;;
  *) echo "usage: $0 [--preflight | --print-unit]" >&2; exit 2 ;;
esac
