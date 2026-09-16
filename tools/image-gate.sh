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
# A gate that can be bypassed guards nothing, so every check here fails
# closed: an error probing the image (a find or grep that could not read
# something) is treated the same as finding a secret, never as "clean".
#
# Exits 0 when every assertion holds, 1 on the first that does not.
set -euo pipefail

usage="usage: image-gate.sh <rootfs> <bootfs> [<repo>]"
ROOT="${1:?$usage}"
BOOT="${2:?$usage}"
REPO="${3:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

fail() { echo "image-gate: FAIL: $*" >&2; exit 1; }
ok() { echo "image-gate: ok: $*"; }

# Strip control bytes and break up "::", which GitHub Actions reads as the
# start of a workflow command, before anything derived from image content
# reaches the log.
sanitize_for_log() {
  printf '%s' "$1" | tr -d '\000-\037\177' | sed 's/::/: :/g'
}

[ -f "$ROOT/etc/passwd" ] && [ -f "$ROOT/etc/shadow" ] || fail "$ROOT is not a root filesystem (no /etc/passwd or /etc/shadow)"
[ -d "$BOOT" ] || fail "$BOOT does not exist"

# A panel's identity and its pending enrollment are generated after first
# boot. One baked in here would make every panel the same panel. A missing
# directory is not an error to swallow: there is simply nothing to find.
for dir in var/lib/scoreboard opt/scoreboard; do
  if [ -d "$ROOT/$dir" ]; then
    for name in device.json device.pem.crt private.pem.key enrollment.json; do
      hit="$(find "$ROOT/$dir" -name "$name" -print -quit)"
      [ -z "$hit" ] || fail "/$dir contains $name, which a panel must generate for itself"
    done
  fi
done
ok "no identity or enrollment files"

# authorized_keys2 is the same secret under a name ssh(1) also honors.
hit="$(find "$ROOT" -xdev -name 'authorized_keys*' -print -quit)"
[ -z "$hit" ] || fail "authorized_keys present at ${hit#"$ROOT"}"
ok "no authorized_keys"

# Every passwd entry must delegate its password to /etc/shadow (an 'x' in the
# password field); an empty field there is itself a passwordless login on
# many systems and would make the /etc/shadow check below moot. root and
# every human account -- selected by uid, not by name, so a second uid-0
# account cannot hide under a different name -- must then be locked in
# /etc/shadow: a '!' or '*' hash. Any other hash, including an empty one, is
# usable and fails.
while IFS=: read -r name pass uid _; do
  [ "$pass" = "x" ] || fail "account $name's /etc/passwd entry does not delegate to /etc/shadow (password field is '$pass')"
  if [ "$uid" -eq 0 ] || { [ "$uid" -ge 1000 ] && [ "$uid" -lt 65534 ]; }; then
    hash="$(awk -F: -v u="$name" '$1 == u { print $2 }' "$ROOT/etc/shadow")"
    case "$hash" in
      '!'* | '*'*) ;;
      *) fail "account $name has a usable or empty password" ;;
    esac
  fi
done <"$ROOT/etc/passwd"
ok "accounts are locked"

# Any ssh*/sshd* unit wired into ANY systemd target (not just the two
# conventional ones) starts sshd; a marker file on the boot partition enables
# it on the device's first boot, long after this check has run.
hit="$(find "$ROOT/etc/systemd/system" -path '*.wants/ssh*' -print -quit)"
[ -z "$hit" ] || fail "SSH is enabled (${hit#"$ROOT"})"
for name in ssh ssh.txt; do
  [ ! -e "$BOOT/$name" ] || fail "SSH is enabled by /boot/$name"
done
ok "SSH is not enabled"

# NetworkManager keeps saved connections in /etc and, for system-provided
# ones, under /usr/lib too; either carries a Wi-Fi psk in plain text.
for conns in "$ROOT/etc/NetworkManager/system-connections" "$ROOT/usr/lib/NetworkManager/system-connections"; do
  if [ -d "$conns" ] && [ -n "$(find "$conns" -type f -print -quit)" ]; then
    fail "a saved Wi-Fi connection is present in ${conns#"$ROOT"}"
  fi
done
if [ -d "$ROOT/etc/wpa_supplicant" ] \
  && grep -rqsE '^[[:space:]]*(psk|sae_password|password|wep_key[0-9]?)=' "$ROOT/etc/wpa_supplicant"; then
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
hit="$(find "$ROOT/opt/scoreboard/.venv" -type d -name pygame -print -quit)"
[ -z "$hit" ] || fail "the virtualenv carries its own pygame (${hit#"$ROOT"}), which has no kmsdrm driver"
[ -d "$ROOT/usr/lib/python3/dist-packages/pygame" ] || fail "the distribution's pygame is not installed"
ok "the virtualenv uses the distribution's pygame"

# A unit only counts as enabled if its .wants symlink resolves to the unit
# file this image installed. A symlink that merely exists but points at the
# wrong target, or at nothing, must not be read as "enabled".
for unit in scoreboard.service scoreboard-netcfg.service; do
  unit_file="$ROOT/etc/systemd/system/$unit"
  link="$ROOT/etc/systemd/system/multi-user.target.wants/$unit"
  [ -f "$unit_file" ] || fail "$unit is not installed"
  [ -L "$link" ] || fail "$unit is not enabled"
  target="$(readlink "$link")"
  case "$target" in
    /*) candidate="$ROOT$target" ;;
    *) candidate="$(dirname "$link")/$target" ;;
  esac
  resolved="$(readlink -f -- "$candidate" 2>/dev/null || true)"
  real_unit="$(readlink -f -- "$unit_file")"
  [ -n "$resolved" ] && [ "$resolved" = "$real_unit" ] \
    || fail "$unit's enable symlink does not resolve to the installed unit (points to $target)"
done
ok "both units are enabled"

cmp -s "$ROOT/etc/polkit-1/rules.d/10-scoreboard-network.rules" "$REPO/device/polkit/10-scoreboard-network.rules" \
  || fail "the polkit rule is missing or differs from device/polkit/10-scoreboard-network.rules"
ok "the polkit rule matches the repository"

# wc -l counts newlines, not lines, so a file missing its trailing newline is
# undercounted; awk's NR counts the final, unterminated line too.
build="$ROOT/etc/scoreboard-build"
[ -f "$build" ] || fail "/etc/scoreboard-build is missing"
lines="$(awk 'END { print NR }' "$build")"
first="$(head -n 1 -- "$build")"
[ "$lines" -eq 1 ] && [ -n "$(printf '%s' "$first" | tr -d '[:space:]')" ] \
  || fail "/etc/scoreboard-build must be a single non-empty line (found $lines line(s))"
ok "build identity: $(sanitize_for_log "$first")"

# A key found here is shared by every panel that flashes the image, whoever
# put it there -- a package's test fixture, a distribution snakeoil key, or a
# leftover build artifact -- and the boot partition ships in the same public
# image as the rootfs. The fix is to delete it in the stage, never to loosen
# this check. grep -a reads binary files as text instead of skipping them,
# and the exit status is read explicitly so a real read error (2) fails the
# gate instead of being mistaken for "no key found" (1).
scan_for_private_key() {
  local search="$1" where="$2" out rc hit
  [ -d "$search" ] || return 0
  set +e
  out="$(grep -rlaE -- '-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----' "$search" 2>&1)"
  rc=$?
  set -e
  case "$rc" in
    1) return 0 ;;
    0) hit="${out%%$'\n'*}"
       fail "a private key is in the image at $where${hit#"$search"}" ;;
    *) fail "scanning $where for private keys failed: ${out%%$'\n'*}" ;;
  esac
}
for dir in etc opt var home root; do
  scan_for_private_key "$ROOT/$dir" "/$dir"
done
scan_for_private_key "$BOOT" "the boot partition:"

# Legacy Debian named the RSA host key ssh_host_key with no algorithm suffix;
# match that and the current ssh_host_<algo>_key form.
if [ -d "$ROOT/etc/ssh" ]; then
  hit="$(find "$ROOT/etc/ssh" -name 'ssh_host_*key*' -print -quit)"
  [ -z "$hit" ] || fail "an SSH host key is in the image at ${hit#"$ROOT"}; each panel must generate its own"
fi
ok "no private keys"

# Everything under /opt/scoreboard other than the venv must be inert
# application code and the one certificate. A file counts as a certificate or
# key if its name has one of the usual extensions, matched case-insensitively
# and against symlinks too (! -type d, not -type f, so a symlink standing in
# for a certificate cannot dodge this), or if its content is plainly a
# certificate regardless of its name.
found="$(cd "$ROOT/opt/scoreboard" && find . -path ./.venv -prune -o ! -type d -print | sort)"
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  rel="${entry#./}"
  path="$ROOT/opt/scoreboard/$rel"
  suspect=0
  case "$(printf '%s' "$rel" | tr '[:upper:]' '[:lower:]')" in
    *.pem | *.crt | *.cer | *.der | *.p7b | *.p7c | *.p12 | *.pfx | *.key) suspect=1 ;;
  esac
  if [ "$suspect" -eq 0 ] && [ -f "$path" ] && grep -qas -- '-----BEGIN CERTIFICATE-----' "$path"; then
    suspect=1
  fi
  [ "$suspect" -eq 0 ] || [ "$rel" = "certs/AmazonRootCA1.pem" ] \
    || fail "unexpected certificate or key file under /opt/scoreboard: $rel"
done <<<"$found"
# The one certificate this image may ship must be the repository's own file,
# and a regular file -- not a symlink standing in for something else that a
# name or content match alone would wave through.
ca="$ROOT/opt/scoreboard/certs/AmazonRootCA1.pem"
[ -f "$ca" ] && [ ! -L "$ca" ] || fail "/opt/scoreboard/certs/AmazonRootCA1.pem is missing or is not a regular file"
cmp -s "$ca" "$REPO/device/certs/AmazonRootCA1.pem" \
  || fail "/opt/scoreboard/certs/AmazonRootCA1.pem differs from the repository's copy"
ok "the only certificate shipped is Amazon's root CA"

echo "image-gate: all checks passed"
