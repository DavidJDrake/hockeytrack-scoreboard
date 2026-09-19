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

APPLIANCE=0
SERVICE_USER=scoreboard
APP_DIR=/opt/scoreboard
STATE_DIR=/var/lib/scoreboard

die() { echo "pi-setup: $*" >&2; exit 1; }

render_unit() {
  if [ "$APPLIANCE" -eq 1 ]; then
    cat "$DEVICE/scoreboard-appliance.service"
    return
  fi
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
  # An image is built before any device has an identity, so there is nothing
  # to check for here; the identity arrives when the panel is registered.
  [ "$APPLIANCE" -eq 1 ] && return 0
  for f in device.json device.pem.crt private.pem.key AmazonRootCA1.pem; do
    [ -f "$CONFIG/$f" ] || die "missing $CONFIG/$f -- copy device/config/ over from the machine that ran make provision"
  done
  mode="$(stat -c %a "$CONFIG/private.pem.key")"
  case "$mode" in
    600 | 400) ;;
    *) die "$CONFIG/private.pem.key is mode $mode. It is this device's identity; make it owner-only: chmod 600 $CONFIG/private.pem.key" ;;
  esac
}

install_checkout() {
  [ "$(id -u)" -ne 0 ] || die "run this as the user the service should run as, not root; it uses sudo where it must"
  preflight

  echo "==> apt packages"
  sudo apt-get update
  # The distribution's pygame, because its SDL has kmsdrm; gpiozero for the
  # optional buttons (the code no-ops without it).
  sudo apt-get install -y python3-pygame python3-gpiozero python3-venv python3-cryptography ca-certificates

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
  local groups=video,render,input
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

install_appliance() {
  preflight
  echo "==> apt packages"
  apt-get update
  # Every Python dependency comes from the distribution's signed archive:
  # pygame for its kmsdrm driver, cryptography for enrollment, and paho-mqtt
  # because the service that imports it holds the panel's IoT private key.
  #
  # The four graphics packages at the end are the display path. They are here
  # because SDL's kmsdrm backend dlopens them at runtime rather than linking
  # them, so nothing in the image depends on libegl1 (libEGL.so.1),
  # libegl-mesa0 (libEGL_mesa.so.0 plus glvnd's 50_mesa.json), libgles2
  # (libGLESv2.so.2) or libgl1-mesa-dri (dri/vc4_dri.so and dri/v3d_dri.so,
  # which in Mesa 25/26 are NOT in mesa-libgallium) -- and apt installs none
  # of them on its own, since libsdl2-2.0-0 Recommends nothing at all. A
  # desktop image hides the gap; a Lite appliance image does not. v0.1.1
  # shipped without them and crash-looped on "EGL not initialized"
  # (docs/hardware-checks.md, H5). Keep this list identical to
  # tools/pi-gen/stage-scoreboard/00-packages/00-packages -- which
  # device/tests/test_pi_gen_recipe.py enforces.
  apt-get install -y python3-pygame python3-gpiozero python3-venv network-manager polkitd python3-cryptography python3-paho-mqtt ca-certificates libegl1 libegl-mesa0 libgles2 libgl1-mesa-dri

  echo "==> service account"
  getent passwd "$SERVICE_USER" >/dev/null || \
    useradd --system --home-dir "$STATE_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  # video, render and input must resolve, or opening /dev/dri and the input
  # devices fails outright; the unit no longer declares SupplementaryGroups=
  # itself, so this loop is the only place membership comes from. gpio stays
  # optional: it is created by raspberrypi-sys-mods rather than base Debian,
  # and the buttons it gates are already optional hardware (buttons.py
  # no-ops without gpiozero or without the wiring).
  for g in video render input; do
    getent group "$g" >/dev/null || die "required group '$g' does not exist on this image"
    usermod -aG "$g" "$SERVICE_USER"
  done
  getent group gpio >/dev/null && usermod -aG gpio "$SERVICE_USER"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$STATE_DIR"

  echo "==> application"
  mkdir -p "$APP_DIR"
  cp -a "$DEVICE/scoreboard" "$DEVICE/requirements.txt" "$DEVICE/certs" "$APP_DIR/"
  python3 -m venv --system-site-packages "$APP_DIR/.venv"
  # pip only confirms that what apt installed satisfies requirements.txt. With
  # --no-index it cannot reach PyPI (or the piwheels index Raspberry Pi OS
  # configures in /etc/pip.conf), so a missing or too-old package fails the
  # install instead of quietly downloading an unpinned, unhashed wheel; with
  # --no-cache-dir it leaves no pip cache in /root for an image to ship.
  "$APP_DIR/.venv/bin/pip" install --no-index --no-cache-dir --disable-pip-version-check -r "$APP_DIR/requirements.txt"
  local where
  where="$(PYGAME_HIDE_SUPPORT_PROMPT=1 "$APP_DIR/.venv/bin/python" -c 'import os, pygame; print(os.path.dirname(pygame.__file__))')"
  case "$where" in
    /usr/lib/python3/dist-packages/*) ;;
    *) die "the venv is using pygame from $where, not the system package, so it has no kmsdrm driver." ;;
  esac
  chown -R root:root "$APP_DIR"

  echo "==> units and polkit"
  # Rendered before tee opens the target, so a failure cannot leave an empty
  # unit behind -- see install_checkout() above for why this matters.
  local unit
  unit="$(render_unit)"
  printf '%s\n' "$unit" | tee /etc/systemd/system/scoreboard.service >/dev/null
  cp "$DEVICE/scoreboard-netcfg.service" /etc/systemd/system/scoreboard-netcfg.service
  install -D -m 644 "$DEVICE/polkit/10-scoreboard-network.rules" \
    /etc/polkit-1/rules.d/10-scoreboard-network.rules
  # Enabled by symlink rather than `systemctl enable`: this also runs inside a
  # pi-gen chroot, where there is no running systemd to talk to.
  mkdir -p /etc/systemd/system/multi-user.target.wants
  ln -sf /etc/systemd/system/scoreboard.service \
    /etc/systemd/system/multi-user.target.wants/scoreboard.service
  ln -sf /etc/systemd/system/scoreboard-netcfg.service \
    /etc/systemd/system/multi-user.target.wants/scoreboard-netcfg.service
  echo "Appliance installed. It starts on the next boot."
}

for arg in "$@"; do
  case "$arg" in
    --appliance) APPLIANCE=1 ;;
    --print-unit | --preflight) ACTION="$arg" ;;
    *) echo "usage: $0 [--appliance] [--preflight | --print-unit]" >&2; exit 2 ;;
  esac
done

case "${ACTION:-}" in
  --print-unit) render_unit ;;
  --preflight) preflight && echo "preflight ok" ;;
  "")
    if [ "$APPLIANCE" -eq 1 ]; then
      [ "$(id -u)" -eq 0 ] || die "--appliance installs system-wide; run it as root"
      install_appliance
    else
      install_checkout
    fi ;;
esac
