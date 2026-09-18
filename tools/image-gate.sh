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

# find (with the default -P) does not descend into a starting point that is
# itself a symlink -- it only inspects the link, never what it points to --
# so a symlink standing in for one of the directories this gate scans would
# hide everything inside it. Every scan below checks its own starting point
# with this first.
reject_symlink() {
  [ ! -L "$1" ] || fail "$2 is a symlink; it must be a real directory so the gate can see inside it"
}

# Run find, capturing its output in $FOUND. A nonzero exit -- a permission or
# read error partway through the scan -- fails the gate outright instead of
# being read as "nothing found". Call this directly (never as `x=$(run_find
# ...)`), so a fail() inside it exits the gate itself and not a subshell.
FOUND=""
run_find() {
  local rc
  set +e
  FOUND="$(find "$@" 2>&1)"
  rc=$?
  set -e
  [ "$rc" -eq 0 ] || fail "scanning failed (find $*): ${FOUND%%$'\n'*}"
}

# grep -q, but distinguishing "no match" (1) from a real read error (>1), so
# an unreadable file cannot be misread as clean. Returns 0/1 like grep -q for
# a normal match/no-match; a real error fails the gate outright. Same calling
# rule as run_find: call it directly, never through command substitution.
grep_or_fail() {
  local err rc
  set +e
  err="$(grep "$@" 2>&1 1>/dev/null)"
  rc=$?
  set -e
  case "$rc" in
    0) return 0 ;;
    1) return 1 ;;
    *) fail "scanning failed (grep $*): ${err%%$'\n'*}" ;;
  esac
}

# Same fail-closed contract as grep_or_fail, but the match itself is kept (in
# $LINE) instead of just its presence -- used below to read what an
# AuthorizedKeysFile directive was actually set to. Same calling rule: call
# it directly, never through command substitution.
LINE=""
grep_line_or_fail() {
  local rc
  set +e
  LINE="$(grep "$@" 2>&1)"
  rc=$?
  set -e
  case "$rc" in
    0) return 0 ;;
    1) LINE=""; return 1 ;;
    *) fail "scanning failed (grep $*): ${LINE%%$'\n'*}" ;;
  esac
}

[ -f "$ROOT/etc/passwd" ] && [ -f "$ROOT/etc/shadow" ] || fail "$ROOT is not a root filesystem (no /etc/passwd or /etc/shadow)"
[ -d "$BOOT" ] || fail "$BOOT does not exist"

# Several checks below read find's output one line at a time. A name holding a
# newline splits into two lines there, and the second can impersonate an
# allowed path: a directory named "x<newline>certs" holding its own
# AmazonRootCA1.pem reads as the one certificate this image may ship. No file
# pi-gen or this project writes has a newline in its name, so any such path
# fails the gate before a line-oriented check can be fooled by it.
for tree in "$ROOT" "$BOOT"; do
  run_find "$tree" -name "*"$'\n'"*" -print -quit
  [ -z "$FOUND" ] || fail "a path contains a newline: $(sanitize_for_log "${FOUND#"$tree"}")"
done
ok "no path contains a newline"

# A panel's identity and its pending enrollment are generated after first
# boot. One baked in here would make every panel the same panel. A missing
# directory is not an error to swallow: there is simply nothing to find.
for dir in var/lib/scoreboard opt/scoreboard; do
  reject_symlink "$ROOT/$dir" "/$dir"
  if [ -d "$ROOT/$dir" ]; then
    for name in device.json device.pem.crt private.pem.key enrollment.json; do
      run_find "$ROOT/$dir" -name "$name" -print -quit
      [ -z "$FOUND" ] || fail "/$dir contains $name, which a panel must generate for itself"
    done
  fi
done
ok "no identity or enrollment files"

# sshd(1) reads authorized_keys only from a user's own .ssh directory (root's,
# a home directory's, or one living anywhere else on the filesystem -- ssh
# does not care who owns the parent) or from a path under /etc/ssh when
# AuthorizedKeysFile points there (Debian's own docs suggest a per-user file
# such as /etc/ssh/authorized_keys/%u for centralized administration).
# authorized_keys2 is the same secret under a name ssh(1) also honors.
# Anywhere else, the same filename is just data: this is what let
# /usr/share/man/man5/authorized_keys.5.gz -- OpenSSH's own man page -- fail
# a real build (GitHub Actions run 35298398347) even though it holds no key.
run_find "$ROOT" -xdev \( -path '*/.ssh/authorized_keys*' -o -path "$ROOT/etc/ssh/authorized_keys*" \) -print -quit
[ -z "$FOUND" ] || fail "authorized_keys present at ${FOUND#"$ROOT"}"
ok "no authorized_keys outside a .ssh directory or /etc/ssh"

# The scan just above only looks where sshd reads keys from by default. Both
# AuthorizedKeysFile and AuthorizedKeysCommand can move that: AuthorizedKeysFile
# can name a path the scan above does not cover, and AuthorizedKeysCommand can
# run an arbitrary program that fetches keys from anywhere at all. Either one
# would quietly reopen the hole the narrower scan above just closed, so this
# check exists to keep that scan honest -- it is not itself a secret scan.
#
# AuthorizedKeysFile is judged token by token rather than against one exact
# string: a real sshd_config can write the stock default with tabs, extra
# spaces, or %h/ in front of it, and none of that changes what sshd actually
# reads. A token still passes after stripping a leading %h/ or ~/ (both mean
# the user's own home directory) when what remains starts with .ssh/
# (covered by the find above, for every user, not just the one the token
# happens to name) or is itself under /etc/ssh/ (also covered above).
# Anything else -- an absolute path elsewhere, a bare filename, a
# %u-expanded path outside /etc/ssh -- names somewhere this gate cannot see
# into, so it fails, naming the token. sshd itself only honors the first
# AuthorizedKeysFile it reads, but every occurrence here is judged, and a
# second one fails on its own even when every token in it is fine: sshd
# would never reach it, so it can only be leftover configuration nobody
# meant to leave live, not a working override this gate might miss.
check_authorized_keys_directive() {
  local file="$1" raw value token stripped occurrences
  [ -f "$file" ] || return 0
  if grep_line_or_fail -iE '^[[:space:]]*AuthorizedKeysCommand[[:space:]]+' "$file"; then
    while IFS= read -r raw; do
      [ -n "$raw" ] || continue
      value="$(printf '%s\n' "$raw" | sed -E 's/^[[:space:]]*[Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Ee][Dd][Kk][Ee][Yy][Ss][Cc][Oo][Mm][Mm][Aa][Nn][Dd][[:space:]]+//; s/[[:space:]]+$//')"
      [ "$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')" = "none" ] \
        || fail "AuthorizedKeysCommand is set to '$(sanitize_for_log "$value")' in ${file#"$ROOT"}; it can fetch keys from anywhere, which this gate cannot scan"
    done <<<"$LINE"
  fi
  if grep_line_or_fail -iE '^[[:space:]]*AuthorizedKeysFile[[:space:]]+' "$file"; then
    occurrences="$(awk 'END { print NR }' <<<"$LINE")"
    [ "$occurrences" -le 1 ] \
      || fail "AuthorizedKeysFile appears $occurrences times in ${file#"$ROOT"}; sshd only honors the first, so the rest is dead configuration this gate treats as a mistake"
    while IFS= read -r raw; do
      [ -n "$raw" ] || continue
      value="$(printf '%s\n' "$raw" | sed -E 's/^[[:space:]]*[Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Ee][Dd][Kk][Ee][Yy][Ss][Ff][Ii][Ll][Ee][[:space:]]+//; s/[[:space:]]+$//')"
      for token in $value; do
        stripped="${token#%h/}"
        stripped="${stripped#\~/}"
        case "$stripped" in
          .ssh/* | /etc/ssh/*) ;;
          *) fail "AuthorizedKeysFile in ${file#"$ROOT"} names '$(sanitize_for_log "$token")', which this gate does not scan" ;;
        esac
      done
    done <<<"$LINE"
  fi
}
check_authorized_keys_directive "$ROOT/etc/ssh/sshd_config"
reject_symlink "$ROOT/etc/ssh/sshd_config.d" "/etc/ssh/sshd_config.d"
if [ -d "$ROOT/etc/ssh/sshd_config.d" ]; then
  run_find "$ROOT/etc/ssh/sshd_config.d" -maxdepth 1 -type f -name '*.conf' -print
  while IFS= read -r conf; do
    [ -n "$conf" ] || continue
    check_authorized_keys_directive "$conf"
  done <<<"$FOUND"
fi
ok "AuthorizedKeysFile and AuthorizedKeysCommand are not overridden"

# Every passwd entry must delegate its password to /etc/shadow (an 'x' in the
# password field); an empty field there is itself a passwordless login on
# many systems and would make the /etc/shadow check below moot. Every
# account's shadow hash must then be locked: a '!' or '*' hash. Any other
# hash, including an empty one, is usable and fails -- not just root's or the
# first user's, since a system account is not automatically harmless.
while IFS=: read -r name pass _; do
  [ -n "$name" ] || continue # a blank line in /etc/passwd is not an account
  [ "$pass" = "x" ] || fail "account $name's /etc/passwd entry does not delegate to /etc/shadow (password field is '$pass')"
  hash="$(awk -F: -v u="$name" '$1 == u { print $2 }' "$ROOT/etc/shadow")"
  case "$hash" in
    '!'* | '*'*) ;;
    *) fail "account $name has a usable or empty password" ;;
  esac
done <"$ROOT/etc/passwd"
ok "accounts are locked"

# Locked accounts are only half of it: Raspberry Pi OS creates the first
# account at first boot instead, and an account created there would be neither
# locked nor in the image this gate just read. pi-gen's
# export-image/01-user-rename arms that with `rename-user -f -s`, which
# enables userconf-pi's userconfig.service; the unit then opens a whiptail
# dialog on tty8 asking for a new username and password, and will not let the
# boot finish until someone answers. A panel has no keyboard, so an image that
# ships it armed is a brick -- v0.1.0 was exactly that
# (docs/hardware-checks.md, H5). The scoreboard stage masks the unit, which
# makes that `systemctl enable` fail and create nothing, and this is the check
# that proves the mask still holds.
#
# Only an enablement symlink counts. Every Raspberry Pi OS image carries the
# unit file itself (userconf-pi is a Recommends of raspberrypi-sys-mods), and
# the mask -- /etc/systemd/system/userconfig.service pointing at /dev/null --
# is the fix rather than the fault, so neither of those fails the gate. Both
# unit trees are scanned: a package can ship a .wants symlink under /usr/lib
# just as an enable writes one under /etc.
for units in "$ROOT/etc/systemd/system" "$ROOT/usr/lib/systemd/system"; do
  reject_symlink "$units" "${units#"$ROOT"}"
  [ -d "$units" ] || continue
  run_find "$units" \( -path '*.wants/*' -o -path '*.requires/*' -o -path '*.upholds/*' \) \
    -name 'userconfig.service' -print -quit
  [ -z "$FOUND" ] || fail "the first-boot user-creation wizard is enabled (${FOUND#"$ROOT"}); nobody can answer it on a panel with no keyboard"
done

# An autologin drop-in would hand a shell to whoever walks up to the panel,
# without the password that every account in the image deliberately lacks.
# raspi-config writes one whenever a boot behaviour of B2 or B4 is chosen, and
# rename-user's undo path goes through exactly that code. A getty drop-in is
# not suspicious by itself -- noclear.conf is a common and harmless one -- so
# only agetty's autologin spellings fail: "--autologin <user>", which
# raspi-config writes, and the "-a <user>" short form agetty also accepts,
# which is only matched on a line that runs agetty so that a stray "-a" in
# some other directive cannot trip it. A plain login prompt on tty1 is fine;
# a session nobody had to log into is not.
for units in "$ROOT/etc/systemd/system" "$ROOT/usr/lib/systemd/system"; do
  [ -d "$units" ] || continue
  run_find "$units" -type f \
    \( -path '*/getty@*.service.d/*' -o -path '*/serial-getty@*.service.d/*' \) -print
  while IFS= read -r dropin; do
    [ -n "$dropin" ] || continue
    if grep_or_fail -qE -e '--autologin' -e 'agetty.*[[:space:]]-a[[:space:]]' "$dropin"; then
      fail "a console autologin drop-in is present (${dropin#"$ROOT"}); the panel's console must not log anyone in"
    fi
  done <<<"$FOUND"
done
ok "the first-boot user-creation wizard is not armed, and no console autologin"

# Only the real SSH unit names count -- Raspberry Pi OS ships sshswitch.service
# enabled in multi-user.target.wants on every image; it only starts sshd when
# a marker file is on the boot partition, which is checked separately below.
# A unit wired into ANY systemd target (not just the two conventional ones),
# through a .wants, .requires or .upholds directory, still starts sshd if it
# is one of these.
run_find "$ROOT/etc/systemd/system" \( -path '*.wants/*' -o -path '*.requires/*' -o -path '*.upholds/*' \) \
  \( -name 'ssh.service' -o -name 'sshd.service' -o -name 'ssh.socket' -o -name 'sshd.socket' \
     -o -name 'ssh@*.service' -o -name 'sshd@*.service' \) -print -quit
[ -z "$FOUND" ] || fail "SSH is enabled (${FOUND#"$ROOT"})"
for name in ssh ssh.txt; do
  [ ! -e "$BOOT/$name" ] || fail "SSH is enabled by /boot/$name"
done
ok "SSH is not enabled"

# NetworkManager keeps saved connections in /etc and, for system-provided
# ones, under /usr/lib too; either carries a Wi-Fi psk in plain text.
for conns in "$ROOT/etc/NetworkManager/system-connections" "$ROOT/usr/lib/NetworkManager/system-connections"; do
  reject_symlink "$conns" "${conns#"$ROOT"}"
  if [ -d "$conns" ]; then
    run_find "$conns" -type f -print -quit
    [ -z "$FOUND" ] || fail "a saved Wi-Fi connection is present in ${conns#"$ROOT"}"
  fi
done
reject_symlink "$ROOT/etc/wpa_supplicant" "/etc/wpa_supplicant"
if [ -d "$ROOT/etc/wpa_supplicant" ] \
  && grep_or_fail -rqE '^[[:space:]]*(psk|sae_password|password|wep_key[0-9]?)=' "$ROOT/etc/wpa_supplicant"; then
  fail "a Wi-Fi password is present in /etc/wpa_supplicant"
fi
for name in custom.toml firstrun.sh wpa_supplicant.conf scoreboard-setup.txt; do
  [ ! -e "$BOOT/$name" ] || fail "the boot partition carries $name, which can hold Wi-Fi credentials"
done
ok "no Wi-Fi credentials"

# cloud-init reads user-data, network-config and meta-data from the boot
# partition on first boot (pi-gen's NoCloud seed), and user-data can create
# accounts, set passwords and install SSH keys. The appliance owns its network
# through scoreboard-netcfg, so the image ships no cloud-init at all: no seed
# files, and no cloud-init package to read one that Raspberry Pi Imager or a
# stranger's SD card reader adds later. dpkg's status file is the package
# signal; the two files are a backstop for a copy installed outside dpkg.
for name in user-data network-config meta-data; do
  [ ! -e "$BOOT/$name" ] && [ ! -L "$BOOT/$name" ] \
    || fail "the boot partition carries $name, a cloud-init seed that can create accounts or set credentials"
done
status="$ROOT/var/lib/dpkg/status"
[ -f "$status" ] || fail "no /var/lib/dpkg/status, so the installed packages cannot be checked"
if grep_or_fail -qxE 'Package: (cloud-init|rpi-cloud-init-mods)' "$status"; then
  fail "cloud-init is installed (dpkg lists it)"
fi
for path in etc/cloud/cloud.cfg usr/bin/cloud-init; do
  [ ! -e "$ROOT/$path" ] && [ ! -L "$ROOT/$path" ] || fail "cloud-init is installed (/$path exists)"
done
ok "no cloud-init"

cfg="$ROOT/opt/scoreboard/.venv/pyvenv.cfg"
[ -f "$cfg" ] || fail "no virtualenv at /opt/scoreboard/.venv"
grep -qE '^include-system-site-packages[[:space:]]*=[[:space:]]*true' "$cfg" \
  || fail "the virtualenv does not include system site packages, so it cannot see the distribution's pygame"
run_find "$ROOT/opt/scoreboard/.venv" -type d -name pygame -print -quit
[ -z "$FOUND" ] || fail "the virtualenv carries its own pygame (${FOUND#"$ROOT"}), which has no kmsdrm driver"
[ -d "$ROOT/usr/lib/python3/dist-packages/pygame" ] || fail "the distribution's pygame is not installed"
ok "the virtualenv uses the distribution's pygame"

# The service that holds each panel's IoT private key imports paho-mqtt, so it
# comes from Debian's signed archive (python3-paho-mqtt), not an unpinned PyPI
# download. Appliance mode's pip only confirms requirements.txt is satisfied
# by what apt installed (--no-index), so the venv must hold nothing but the pip
# that python3 -m venv bundles, and no pip cache may ship.
[ -d "$ROOT/usr/lib/python3/dist-packages/paho" ] || fail "the distribution's paho-mqtt is not installed"
run_find "$ROOT/opt/scoreboard/.venv" -name '*.dist-info' ! -name 'pip-*.dist-info' -print -quit
[ -z "$FOUND" ] || fail "the virtualenv holds a package installed from PyPI ($(sanitize_for_log "${FOUND##*/}")); appliance packages come from apt"
[ ! -e "$ROOT/root/.cache/pip" ] && [ ! -L "$ROOT/root/.cache/pip" ] || fail "a pip cache ships in the image at /root/.cache/pip"
ok "no Python package from PyPI, and no pip cache"

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
  reject_symlink "$search" "$where"
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
reject_symlink "$ROOT/etc/ssh" "/etc/ssh"
if [ -d "$ROOT/etc/ssh" ]; then
  run_find "$ROOT/etc/ssh" -name 'ssh_host_*key*' -print -quit
  [ -z "$FOUND" ] || fail "an SSH host key is in the image at ${FOUND#"$ROOT"}; each panel must generate its own"
fi
ok "no private keys"

# Everything under /opt/scoreboard other than the venv must be inert
# application code and the one certificate. A file counts as a certificate or
# key if its name has one of the usual extensions, matched case-insensitively
# and against symlinks too (! -type d, not -type f, so a symlink standing in
# for a certificate cannot dodge this), or if its content is plainly a
# certificate regardless of its name.
run_find "$ROOT/opt/scoreboard" -path "$ROOT/opt/scoreboard/.venv" -prune -o ! -type d -print
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  rel="${entry#"$ROOT/opt/scoreboard/"}"
  path="$entry"
  suspect=0
  case "$(printf '%s' "$rel" | tr '[:upper:]' '[:lower:]')" in
    *.pem | *.crt | *.cer | *.der | *.p7b | *.p7c | *.p12 | *.pfx | *.key) suspect=1 ;;
  esac
  if [ "$suspect" -eq 0 ] && [ -f "$path" ] \
    && grep_or_fail -qaE -- '-----BEGIN (TRUSTED )?CERTIFICATE-----|-----BEGIN PKCS7-----' "$path"; then
    suspect=1
  fi
  [ "$suspect" -eq 0 ] || [ "$rel" = "certs/AmazonRootCA1.pem" ] \
    || fail "unexpected certificate or key file under /opt/scoreboard: $rel"
done <<<"$(printf '%s\n' "$FOUND" | sort)"
# The one certificate this image may ship must be the repository's own file,
# and a regular file -- not a symlink standing in for something else that a
# name or content match alone would wave through.
ca="$ROOT/opt/scoreboard/certs/AmazonRootCA1.pem"
[ -f "$ca" ] && [ ! -L "$ca" ] || fail "/opt/scoreboard/certs/AmazonRootCA1.pem is missing or is not a regular file"
cmp -s "$ca" "$REPO/device/certs/AmazonRootCA1.pem" \
  || fail "/opt/scoreboard/certs/AmazonRootCA1.pem differs from the repository's copy"
ok "the only certificate shipped is Amazon's root CA"

echo "image-gate: all checks passed"
