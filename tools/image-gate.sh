#!/usr/bin/env bash
# Inspect a built scoreboard image before it is published.
#
#   tools/image-gate.sh <rootfs> <bootfs> [<repo>]
#
# Anyone on the internet can flash this image, so anything baked into it is
# shared by every panel that runs it: a private key, a password, an SSH key or
# a Wi-Fi password in the image is a secret handed to strangers. This turns
# "the image contains no secrets" into a check that fails the build. It
# inspects the filesystem; it does not boot the image. Booting is proven on
# real hardware (docs/hardware-checks.md).
#
# Exits 0 when every assertion holds, 1 on the first that does not.
set -euo pipefail

usage="usage: image-gate.sh <rootfs> <bootfs> [<repo>]"
ROOT="${1:?$usage}"
BOOT="${2:?$usage}"
REPO="${3:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

fail() { echo "image-gate: FAIL: $*" >&2; exit 1; }
ok() { echo "image-gate: ok: $*"; }

[ -f "$ROOT/etc/passwd" ] && [ -f "$ROOT/etc/shadow" ] || fail "$ROOT is not a root filesystem (no /etc/passwd or /etc/shadow)"
[ -d "$BOOT" ] || fail "$BOOT does not exist"

# A panel's identity and its pending enrollment are generated after first
# boot. One baked in here would make every panel the same panel.
for dir in var/lib/scoreboard opt/scoreboard; do
  for name in device.json device.pem.crt private.pem.key enrollment.json; do
    hit="$(find "$ROOT/$dir" -name "$name" -print -quit 2>/dev/null || true)"
    [ -z "$hit" ] || fail "/$dir contains $name, which a panel must generate for itself"
  done
done
ok "no identity or enrollment files"

hit="$(find "$ROOT" -xdev -name authorized_keys -print -quit 2>/dev/null || true)"
[ -z "$hit" ] || fail "authorized_keys present at ${hit#"$ROOT"}"
ok "no authorized_keys"

# root and every human account (uid 1000-65533) must be locked: a '!' or '*'
# hash. An empty hash is a passwordless login and fails too.
while IFS=: read -r name _ uid _; do
  if [ "$name" = root ] || { [ "$uid" -ge 1000 ] && [ "$uid" -lt 65534 ]; }; then
    hash="$(awk -F: -v u="$name" '$1 == u { print $2 }' "$ROOT/etc/shadow")"
    case "$hash" in
      '!'* | '*'*) ;;
      *) fail "account $name has a usable or empty password" ;;
    esac
  fi
done <"$ROOT/etc/passwd"
ok "accounts are locked"

for unit in ssh.service sshd.service ssh.socket; do
  for target in multi-user.target.wants sockets.target.wants; do
    [ ! -e "$ROOT/etc/systemd/system/$target/$unit" ] && [ ! -L "$ROOT/etc/systemd/system/$target/$unit" ] \
      || fail "SSH is enabled ($target/$unit)"
  done
done
for name in ssh ssh.txt; do
  [ ! -e "$BOOT/$name" ] || fail "SSH is enabled by /boot/$name"
done
ok "SSH is not enabled"

conns="$ROOT/etc/NetworkManager/system-connections"
if [ -d "$conns" ] && [ -n "$(find "$conns" -type f -print -quit)" ]; then
  fail "a saved Wi-Fi connection is present in /etc/NetworkManager/system-connections"
fi
if [ -d "$ROOT/etc/wpa_supplicant" ] && grep -rqsE '^[[:space:]]*psk=' "$ROOT/etc/wpa_supplicant"; then
  fail "a Wi-Fi password is present in /etc/wpa_supplicant"
fi
for name in custom.toml firstrun.sh wpa_supplicant.conf scoreboard-setup.txt; do
  [ ! -e "$BOOT/$name" ] || fail "the boot partition carries $name, which can hold Wi-Fi credentials"
done
ok "no Wi-Fi credentials"

cfg="$ROOT/opt/scoreboard/.venv/pyvenv.cfg"
[ -f "$cfg" ] || fail "no virtualenv at /opt/scoreboard/.venv"
grep -qE '^include-system-site-packages[[:space:]]*=[[:space:]]*true' "$cfg" \
  || fail "the virtualenv does not include system site packages, so it cannot see the distribution's pygame"
hit="$(find "$ROOT/opt/scoreboard/.venv" -type d -name pygame -print -quit 2>/dev/null || true)"
[ -z "$hit" ] || fail "the virtualenv carries its own pygame (${hit#"$ROOT"}), which has no kmsdrm driver"
[ -d "$ROOT/usr/lib/python3/dist-packages/pygame" ] || fail "the distribution's pygame is not installed"
ok "the virtualenv uses the distribution's pygame"

for unit in scoreboard.service scoreboard-netcfg.service; do
  [ -f "$ROOT/etc/systemd/system/$unit" ] || fail "$unit is not installed"
  [ -L "$ROOT/etc/systemd/system/multi-user.target.wants/$unit" ] || fail "$unit is not enabled"
done
ok "both units are enabled"

cmp -s "$ROOT/etc/polkit-1/rules.d/10-scoreboard-network.rules" "$REPO/device/polkit/10-scoreboard-network.rules" \
  || fail "the polkit rule is missing or differs from device/polkit/10-scoreboard-network.rules"
ok "the polkit rule matches the repository"

build="$ROOT/etc/scoreboard-build"
[ -f "$build" ] || fail "/etc/scoreboard-build is missing"
[ "$(wc -l <"$build")" -eq 1 ] && [ -n "$(tr -d '[:space:]' <"$build")" ] \
  || fail "/etc/scoreboard-build must be a single non-empty line"
ok "build identity: $(cat "$build")"

# A key found here is shared by every panel that flashes the image, whoever
# put it there -- a package's test fixture or a distribution snakeoil key
# included. The fix is to delete it in the stage, never to loosen this.
for dir in etc opt var home root; do
  [ -d "$ROOT/$dir" ] || continue
  hit="$(grep -rlIs -- '-----BEGIN [A-Z ]*PRIVATE KEY-----' "$ROOT/$dir" | head -n 1 || true)"
  [ -z "$hit" ] || fail "a private key is in the image at ${hit#"$ROOT"}"
done
hit="$(find "$ROOT/etc/ssh" -name 'ssh_host_*_key' -print -quit 2>/dev/null || true)"
[ -z "$hit" ] || fail "an SSH host key is in the image at ${hit#"$ROOT"}; each panel must generate its own"
ok "no private keys"

found="$(cd "$ROOT/opt/scoreboard" && find . -path ./.venv -prune -o -type f \
  \( -name '*.pem' -o -name '*.crt' -o -name '*.key' -o -name '*.p12' -o -name '*.pfx' \) -print | sort)"
[ "$found" = "./certs/AmazonRootCA1.pem" ] \
  || fail "unexpected certificate or key files under /opt/scoreboard: $(echo "$found" | tr '\n' ' ')"
cmp -s "$ROOT/opt/scoreboard/certs/AmazonRootCA1.pem" "$REPO/device/certs/AmazonRootCA1.pem" \
  || fail "/opt/scoreboard/certs/AmazonRootCA1.pem differs from the repository's copy"
ok "the only certificate shipped is Amazon's root CA"

echo "image-gate: all checks passed"
