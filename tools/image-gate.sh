#!/usr/bin/env bash
# Inspect a built scoreboard image before it is published.
#
#   tools/image-gate.sh <rootfs> <bootfs> [<repo>] [--image <flash image>]
#
# <rootfs> and <bootfs> are slot A's root and boot partitions, mounted
# read-only. With --image, the assembled six-partition image
# (tools/image-layout.sh) is inspected too: its partition table, that slot B
# is byte-identical to slot A, SETUP and STATE. Those are read with sfdisk,
# mtools and debugfs, never mounted.
#
# Anyone on the internet can flash this image, so anything baked into it is
# shared by every panel that runs it: a private key, a password, an SSH key or
# a Wi-Fi password in the image is a secret handed to strangers. This turns
# "the image contains no secrets" into a check that fails the build. It
# inspects the filesystem; it does not boot the image. Booting is proven on
# real hardware (docs/hardware-checks.md).
#
# Since 2026-09-18 it checks a second thing: that the image can do its one
# job. v0.1.1 passed every no-secrets rule here, shipped, booted unattended --
# and showed a black screen, because SDL's kmsdrm backend dlopens its EGL,
# GLES and DRI libraries at runtime and none of them was in the image (H5).
# A secret-free image that cannot light the panel is still a bad release, and
# a release costs thirty-five minutes of build plus a human with a card
# reader. Anything the display path needs at runtime but nothing in the image
# depends on belongs here, asserted by path.
#
# A gate that can be bypassed guards nothing, so every check here fails
# closed: an error probing the image (a find or grep that could not read
# something) is treated the same as finding a secret, never as "clean".
#
# Exits 0 when every assertion holds, 1 on the first that does not.
set -euo pipefail

usage="usage: image-gate.sh <rootfs> <bootfs> [<repo>] [--image <flash image>]"
ROOT="${1:?$usage}"
BOOT="${2:?$usage}"
shift 2
REPO=""
IMAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --image) IMAGE="${2:?$usage}"; shift 2 ;;
    --*) echo "$usage" >&2; exit 2 ;;
    *) [ -z "$REPO" ] || { echo "$usage" >&2; exit 2; }; REPO="$1"; shift ;;
  esac
done
[ -n "$REPO" ] || REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAYOUT="$REPO/tools/image-layout.sh"

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

# Does an image-absolute path resolve to something that really exists, inside
# the image? Returns 0/1; it never fails the gate itself, so the caller can
# say what was missing and why it matters.
#
# The symlink chain is walked by hand rather than left to `[ -e ]` or
# `readlink -e`, because several of the files this gate looks for are the head
# of a versioned symlink chain (libEGL.so.1 -> libEGL.so.1.1.0) while others,
# gbm/dri_gbm.so and 50_mesa.json among them, are plain files -- and the host
# must never be consulted about any of it: an
# ABSOLUTE link target inside a rootfs means "/usr/... in that rootfs", but
# the kernel resolving it here would read the build machine's /usr instead.
# That cuts both ways -- it can pass an image missing the file because the
# host happens to have one, and fail a good image because an x86 host has no
# aarch64 multiarch directory. A relative target is resolved beside the link,
# as it would be on the panel. The hop limit stops a symlink loop spinning.
image_resolves() {
  local cur="$ROOT/$1" target hops=0
  while [ -L "$cur" ]; do
    hops=$((hops + 1))
    [ "$hops" -le 16 ] || return 1
    target="$(readlink -- "$cur")" || return 1
    case "$target" in
      /*) cur="$ROOT$target" ;;
      *) cur="$(dirname -- "$cur")/$target" ;;
    esac
  done
  [ -e "$cur" ]
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
# Only an enablement symlink counts as "enabled". Every Raspberry Pi OS image
# carries the unit file itself (userconf-pi is a Recommends of
# raspberrypi-sys-mods), so its presence is not a fault. Both unit trees are
# scanned: a package can ship a .wants symlink under /usr/lib just as an
# enable writes one under /etc.
for units in "$ROOT/etc/systemd/system" "$ROOT/usr/lib/systemd/system"; do
  reject_symlink "$units" "${units#"$ROOT"}"
  [ -d "$units" ] || continue
  run_find "$units" \( -path '*.wants/*' -o -path '*.requires/*' -o -path '*.upholds/*' \) \
    -name 'userconfig.service' -print -quit
  [ -z "$FOUND" ] || fail "the first-boot user-creation wizard is enabled (${FOUND#"$ROOT"}); nobody can answer it on a panel with no keyboard"
done

# The mask itself is asserted, not merely assumed. "If the mask stopped working
# the enable would succeed, and the rule above would catch the symlink" covers
# unmask-then-enable, but not unmask-WITHOUT-enable: dh_installsystemd's
# postinst only re-enables a unit that was already enabled, so a path that
# leaves userconfig.service present, unmasked and unenabled passes that rule,
# boots perfectly, and is armed for the next thing that enables it on a panel
# already in the field. The mask is the control; the control has to be visible.
#
# A mask is any symlink that RESOLVES to /dev/null, so the target is
# canonicalized rather than string-compared: ../../../dev/null is as valid a
# mask as /dev/null and systemd treats them alike. A relative target is
# resolved inside the image and then made image-absolute again, so a link that
# climbs out of the rootfs cannot pass by landing on the host's /dev/null.
# (/dev/null is in fact the one target where the image/host distinction is
# moot -- it is the same path either way and nothing is read through it -- but
# resolving inside the root is what makes that a fact rather than an accident.)
# Used here and by the network-surface rules below, which mask several more
# units for the same reason: $1 is the unit name, $2 names the thing in the
# failure message. Every message says "not masked", which is the phrase the
# fixtures match on.
assert_masked() {
  local unit="$1" what="$2" mask mask_target mask_resolved root_resolved
  mask="$ROOT/etc/systemd/system/$unit"
  [ -L "$mask" ] \
    || fail "$what is not masked (/etc/systemd/system/$unit is not a symlink); only a mask stops it being enabled later"
  mask_target="$(readlink "$mask")"
  case "$mask_target" in
    /*) mask_resolved="$mask_target" ;;
    *)  mask_resolved="$(readlink -m -- "$ROOT/etc/systemd/system/$mask_target")"
        root_resolved="$(readlink -m -- "$ROOT")"
        mask_resolved="${mask_resolved#"$root_resolved"}" ;;
  esac
  [ "$mask_resolved" = "/dev/null" ] \
    || fail "$what is not masked (/etc/systemd/system/$unit resolves to $(sanitize_for_log "$mask_resolved"), not /dev/null)"
}
assert_masked userconfig.service "the first-boot wizard's unit"

# An autologin would hand a shell to whoever walks up to the panel, without
# the password that every account in the image deliberately lacks.
# raspi-config writes one whenever a boot behaviour of B2 or B4 is chosen, and
# rename-user's undo path goes through exactly that code. A getty drop-in is
# not suspicious by itself -- noclear.conf is a common and harmless one -- so
# only agetty's autologin spellings fail: "--autologin <user>", which
# raspi-config writes, and the short form agetty also accepts, detached
# ("-a pi") or attached ("-api"). The short form is matched only on a line
# that runs agetty, so a stray "-a" in some other directive cannot trip it.
#
# Four unit families can carry it -- getty@, serial-getty@, autovt@ and
# console-getty -- as a drop-in in either unit tree, or as a full unit file
# placed directly in /etc/systemd/system, which overrides the packaged one
# outright. Nothing stock lives in either of those places: pi-gen ships no
# getty drop-in at all, and no getty unit under /etc, so a false positive here
# is impossible on a stock image rather than merely unlikely. (The packaged
# templates under /usr/lib are deliberately NOT read: they are stock by
# definition, and their comments discuss agetty's options.)
autologin_re_long='--autologin'
autologin_re_short='agetty.*[[:space:]]-a'
for units in "$ROOT/etc/systemd/system" "$ROOT/usr/lib/systemd/system"; do
  [ -d "$units" ] || continue
  # find -P does not descend into a directory that is itself a symlink, so a
  # symlinked drop-in directory would hide every file in it from the scan.
  run_find "$units" -type l \
    \( -name 'getty@*.service.d' -o -name 'serial-getty@*.service.d' \
       -o -name 'autovt@*.service.d' -o -name 'console-getty.service.d' \) -print -quit
  [ -z "$FOUND" ] || fail "a getty drop-in directory is a symlink (${FOUND#"$ROOT"}); it must be a real directory so the gate can see inside it"
  # -xtype f, not -type f: a drop-in that is a symlink to a real file is read
  # by systemd and must be read here too.
  run_find "$units" -xtype f \
    \( -path '*/getty@*.service.d/*' -o -path '*/serial-getty@*.service.d/*' \
       -o -path '*/autovt@*.service.d/*' -o -path '*/console-getty.service.d/*' \) -print
  while IFS= read -r conf; do
    [ -n "$conf" ] || continue
    if grep_or_fail -qE -e "$autologin_re_long" -e "$autologin_re_short" "$conf"; then
      fail "a console autologin drop-in is present (${conf#"$ROOT"}); the panel's console must not log anyone in"
    fi
  done <<<"$FOUND"
done
# A replacement unit, as opposed to a drop-in, only overrides the packaged one
# when it sits directly in /etc/systemd/system -- hence -maxdepth 1, which also
# keeps the enablement symlinks in getty.target.wants/ out of this scan.
run_find "$ROOT/etc/systemd/system" -maxdepth 1 -xtype f \
  \( -name 'getty@*.service' -o -name 'serial-getty@*.service' \
     -o -name 'autovt@*.service' -o -name 'console-getty.service' \) -print
while IFS= read -r unit_override; do
  [ -n "$unit_override" ] || continue
  if grep_or_fail -qE -e "$autologin_re_long" -e "$autologin_re_short" "$unit_override"; then
    fail "a console autologin is configured (${unit_override#"$ROOT"}); the panel's console must not log anyone in"
  fi
done <<<"$FOUND"
ok "the first-boot user-creation wizard is masked and not armed, and no console autologin"

# With no getty on tty1 and the panel owning the console, a startup failure
# shows a black screen and nothing else: scoreboard-appliance.service sends
# stdout and stderr to the journal, a display failure exits 78 and
# RestartPreventExitStatus=78 then stops the service outright, and every other
# crash restarts every 3 s in silence. raspberrypi-sys-mods ships
# /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf with
# Storage=volatile, so unless the stage overrides it the journal is RAM-only
# and pulling the card yields nothing at all. The card is the last diagnosis
# surface an appliance has, so this asserts it works.
#
# It asserts the EFFECTIVE settings, not the presence of our file, and it
# models what journald actually does rather than a simplification of it:
#
#  - The main file is read first and drop-ins override it, so the base value is
#    read rather than assumed. Only the highest-priority copy of journald.conf
#    is read (/etc over /run over /usr/local/lib over /usr/lib).
#  - All four drop-in directories are read, not just /etc and /usr/lib.
#  - Of several drop-ins sharing a NAME, systemd reads only the one from the
#    highest-priority directory -- it does not read both -- which is how a
#    vendor drop-in gets shadowed. A drop-in symlinked to /dev/null is the
#    documented way to disable one: that name then contributes nothing, rather
#    than falling through to the vendor copy it is shadowing.
#  - The chosen files are then sorted by filename, and the last one to set an
#    option wins. So a later-sorting drop-in could put Storage=volatile back,
#    and a rule that only looked for our own file would wave that through.
#
# Storage=auto means "persistent if /var/log/journal exists", so it passes when
# the directory is there; volatile and none never do. The directory is asserted
# separately, which is what makes auto safe to accept.
journal_conf=""
journal_dirs=()
for base in etc run usr/local/lib usr/lib; do
  candidate="$ROOT/$base/systemd/journald.conf"
  if [ -z "$journal_conf" ] && [ -f "$candidate" ]; then
    journal_conf="$candidate"
  fi
  dir="$ROOT/$base/systemd/journald.conf.d"
  reject_symlink "$dir" "${dir#"$ROOT"}"
  if [ -d "$dir" ]; then
    journal_dirs+=("$dir")
  fi
done

# Reads one setting out of one journald config file into SETTING_VALUE, empty
# when the file does not set it. Called directly, never through a command
# substitution: grep_line_or_fail can fail() inside, and inside $(...) that
# would kill a subshell and let the gate carry on.
SETTING_VALUE=""
read_setting() {
  local file="$1" name="$2"
  SETTING_VALUE=""
  if grep_line_or_fail -iE "^[[:space:]]*${name}[[:space:]]*=" "$file"; then
    SETTING_VALUE="$(printf '%s\n' "$LINE" | tail -n 1 | sed -E "s/^[[:space:]]*[^=]*=[[:space:]]*//; s/[[:space:]]+\$//")"
  fi
}

journal_storage=""
journal_sync=""
if [ -n "$journal_conf" ]; then
  read_setting "$journal_conf" Storage
  journal_storage="$SETTING_VALUE"
  read_setting "$journal_conf" SyncIntervalSec
  journal_sync="$SETTING_VALUE"
fi
if [ "${#journal_dirs[@]}" -gt 0 ]; then
  # rank orders the directories by priority so that, for one filename, the
  # highest-priority copy is the one kept; then the kept files are ordered by
  # filename, which is the order systemd applies them in.
  run_find "${journal_dirs[@]}" -maxdepth 1 \( -type f -o -type l \) -name '*.conf' -print
  chosen="$(printf '%s\n' "$FOUND" | awk -v root="$ROOT" '
      $0 == "" { next }
      { rel = substr($0, length(root) + 1)
        rank = 4
        if (rel ~ /^\/etc\//) rank = 1
        else if (rel ~ /^\/run\//) rank = 2
        else if (rel ~ /^\/usr\/local\/lib\//) rank = 3
        name = $0; sub(/.*\//, "", name)
        if (!(name in best) || rank < bestrank[name]) { best[name] = $0; bestrank[name] = rank }
      }
      END { for (n in best) print n "\t" best[n] }' \
    | LC_ALL=C sort -t "$(printf '\t')" -k1,1 | cut -f2-)"
  while IFS= read -r conf; do
    [ -n "$conf" ] || continue
    # A drop-in symlinked to /dev/null disables that name outright: it
    # contributes nothing, and because it already shadowed any lower-priority
    # copy of the same name, nothing else contributes under that name either.
    if [ -L "$conf" ] && [ "$(readlink -m -- "$conf")" = "/dev/null" ]; then
      continue
    fi
    [ -f "$conf" ] || continue
    read_setting "$conf" Storage
    [ -z "$SETTING_VALUE" ] || journal_storage="$SETTING_VALUE"
    read_setting "$conf" SyncIntervalSec
    [ -z "$SETTING_VALUE" ] || journal_sync="$SETTING_VALUE"
  done <<<"$chosen"
fi
reject_symlink "$ROOT/var/log/journal" "/var/log/journal"
case "$journal_storage" in
  persistent) ;;
  auto | "") [ -d "$ROOT/var/log/journal" ] \
      || fail "the journal is not persistent (journald Storage resolves to '$(sanitize_for_log "${journal_storage:-auto, the default}")' and /var/log/journal does not exist, so it stays in RAM)" ;;
  *) fail "the journal is not persistent (journald Storage resolves to '$(sanitize_for_log "$journal_storage")'); a panel that fails to start would leave nothing on the card to read" ;;
esac
[ -d "$ROOT/var/log/journal" ] \
  || fail "/var/log/journal is missing, so the journal has nowhere on the card to persist to"
# journald's default SyncIntervalSec is 5 minutes for ERR and below, and the
# scoreboard's startup failures are logged at ERR. Someone watching a black
# screen pulls the power long before five minutes, which would lose exactly the
# line this whole feature exists to capture.
[ "$journal_sync" = "30s" ] \
  || fail "journald SyncIntervalSec resolves to '$(sanitize_for_log "${journal_sync:-unset, so 5min}")', not 30s; a failure logged at ERR could be lost when the power is pulled"
ok "the journal is persistent, capped and synced every 30s, and /var/log/journal exists"

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

# Raspberry Pi Connect is a remote-access agent, and pi-gen's stage2 installs
# rpi-connect-lite as a matter of course. `rpi-connect signin` links the device
# to a Raspberry Pi account that can then open a shell on it from a browser.
# An appliance whose posture is that nobody can reach a panel -- including the
# people who build it -- carries no such agent, so the scoreboard stage purges
# it and this is the check that it stayed purged. dpkg's status file is the
# package signal, the same one the cloud-init rule uses, and it catches a
# `remove` that should have been a `purge` too: that leaves a
# "Status: deinstall ok config-files" stanza behind, still with a Package: line.
# The two programs and three user units are a backstop for a copy installed
# outside dpkg.
#
# Those paths are named exactly rather than scanned for by name, because the
# package also ships thirteen rpi-connect-*.1.gz manual pages -- and "a scan
# that cannot tell a program from its documentation" is the bug that already
# failed one real build (GitHub Actions run 35298398347).
if grep_or_fail -qxE 'Package: (rpi-connect|rpi-connect-lite)' "$status"; then
  fail "Raspberry Pi Connect is installed (dpkg lists it); an appliance carries no remote-access agent"
fi
for path in usr/bin/rpi-connect usr/bin/rpi-connectd \
            usr/lib/systemd/user/rpi-connect.service \
            usr/lib/systemd/user/rpi-connect-signin.service \
            usr/lib/systemd/user/rpi-connect-signin.path; do
  [ ! -e "$ROOT/$path" ] && [ ! -L "$ROOT/$path" ] \
    || fail "Raspberry Pi Connect is installed (/$path exists); an appliance carries no remote-access agent"
done
ok "no remote-access agent"

# --- Network surface (added 2026-09-19) ----------------------------------
#
# The panel needs DHCP, DNS, NTP and outbound TLS. It accepts no inbound
# connection, has no login and no Bluetooth function. Everything below
# asserts that tools/pi-gen/stage-scoreboard/05-no-listeners did its job and
# that nothing afterwards -- a pi-gen bump, export-image's dist-upgrade, a
# Recommends -- put any of it back. Spec 9.13 carries the reasoning and the
# evidence for each decision; this file is the part that fails the build.
#
# The package rule comes first and is the same signal the cloud-init and
# remote-access rules use: dpkg's status file, which also catches a `remove`
# that should have been a `purge`, since that leaves a
# "Status: deinstall ok config-files" stanza with its Package: line intact.
# `ssh` is the metapackage; the -x anchor means it matches only that exact
# line and not ssh-import-id or openssh-server.
if grep_or_fail -qxE 'Package: (avahi-daemon|libnss-mdns|bluez|bluez-firmware|rpi-usb-gadget|ssh-import-id|rpi-update|openssh-server|openssh-sftp-server|openssh-client|ssh)' "$status"; then
  fail "a package the appliance purges for network surface is installed (dpkg lists one of avahi-daemon, libnss-mdns, bluez, bluez-firmware, rpi-usb-gadget, ssh-import-id, rpi-update, openssh-server, openssh-sftp-server, openssh-client, ssh)"
fi
# The programs and units themselves, named by exact path rather than scanned
# for by name -- a name scan is what once rejected OpenSSH's own manual page
# and cost a release build (GitHub Actions run 35298398347). Only paths whose
# owning package is unambiguous are listed: bluez-firmware's HCI blobs are
# deliberately absent from this list, because a future firmware package could
# legitimately ship a file of the same name, and the dpkg rule above already
# covers it.
for path in usr/sbin/avahi-daemon usr/lib/systemd/system/avahi-daemon.service \
            usr/lib/systemd/system/avahi-daemon.socket etc/avahi/avahi-daemon.conf \
            usr/libexec/bluetooth/bluetoothd usr/bin/bluetoothctl \
            usr/lib/systemd/system/bluetooth.service \
            usr/share/dbus-1/system-services/org.bluez.service \
            usr/bin/rpi-usb-gadget usr/lib/systemd/system/rpi-usb-gadget-ics.service \
            usr/bin/ssh-import-id usr/bin/ssh-import-id-gh usr/bin/ssh-import-id-lp \
            usr/bin/rpi-update \
            usr/sbin/sshd usr/lib/openssh/sshd-session \
            usr/lib/systemd/system/ssh.service usr/lib/systemd/system/ssh.socket \
            usr/bin/ssh usr/bin/ssh-keygen; do
  [ ! -e "$ROOT/$path" ] && [ ! -L "$ROOT/$path" ] \
    || fail "/$path is in the image; the appliance purges the package that ships it (network surface)"
done

# Nothing from the must-not-listen set may be wired into any target, through a
# .wants, .requires or .upholds directory, in either unit tree -- the same
# notion of "enabled" the SSH and wizard rules above use. Purging a package
# takes its own enablement symlinks with it (deb-systemd-helper purge removes
# the ones it created), so on a hardened image there is nothing here to find;
# one of these names appearing means the package came back, or something
# hand-wrote the link.
#
# Two names are deliberately NOT in this list. The four SSH unit names have
# their own rule above. And sshswitch.service is enabled on every Raspberry Pi
# OS image by raspberrypi-sys-mods, which is load-bearing and stays -- so its
# symlink is still there on a correctly hardened image and failing on it would
# reject a good build. The mask asserted below is what neutralizes it.
#
# hciuart.service is named although no installed package ships it today
# (pi-bluetooth is not in this image): it is the unit a pi-gen bump would use
# to attach the adapter, and naming it costs nothing.
for units in "$ROOT/etc/systemd/system" "$ROOT/usr/lib/systemd/system"; do
  [ -d "$units" ] || continue
  run_find "$units" \( -path '*.wants/*' -o -path '*.requires/*' -o -path '*.upholds/*' \) \
    \( -name 'avahi-daemon.service' -o -name 'avahi-daemon.socket' \
       -o -name 'bluetooth.service' -o -name 'hciuart.service' \
       -o -name 'rpi-usb-gadget-ics.service' \) -print -quit
  [ -z "$FOUND" ] || fail "a daemon the panel must not run is enabled (${FOUND#"$ROOT"})"
done

# And the masks are asserted rather than assumed, for the reason the wizard's
# mask is: a purge that is later undone leaves the unit unmasked and unenabled
# -- which passes the rule above, boots perfectly, and is armed for the next
# thing that enables it.
assert_masked avahi-daemon.service "the mDNS responder's unit"
assert_masked avahi-daemon.socket "the mDNS responder's socket"
assert_masked bluetooth.service "the Bluetooth daemon's unit"
assert_masked sshswitch.service "the boot-partition SSH switch"
for unit in ssh.service ssh.socket sshd.service sshd.socket; do
  assert_masked "$unit" "the SSH server's $unit"
done
# dtoverlay=disable-bt makes the PL011 the primary UART, which makes
# enable_uart default to 1, which turns the console=serial0,115200 already in
# cmdline.txt into a live kernel console on GPIO 14/15 -- and
# systemd-getty-generator then puts a login prompt on it, because it reads
# /sys/class/tty/console/active and instantiates serial-getty@<tty>.service
# for every active non-virtual console. The kernel console is deliberately
# KEPT: it is the diagnosis path a panel with no login, no getty on tty1 and a
# black screen has never had (spec 9.13). The prompt is not kept: every
# account in the image is locked, so it serves nobody.
#
# The TEMPLATE is what must be masked, not an instance. The instance name
# depends on what the firmware calls the port, so it cannot be asserted from
# here; systemd resolves serial-getty@ttyAMA0.service through the template
# when no unit of that exact name exists, and /etc/systemd/system/
# serial-getty@.service is found first. The mask does not touch kernel console
# output -- printk does not go through a getty -- and it does not trip the
# autologin scans above, which are -xtype f and so skip a symlink to a
# character device.
assert_masked 'serial-getty@.service' "the serial login prompt"

# Bluetooth is off in the device tree, not merely daemonless. Without this the
# kernel still attaches the on-board adapter over HCI UART and answers for it.
# The line must be live: commented out, it is a note about what someone meant
# to do. config.txt is read by the firmware, which accepts leading whitespace,
# so the match does too.
btcfg="$BOOT/config.txt"
[ -f "$btcfg" ] || fail "the boot partition has no config.txt, so Bluetooth cannot be shown to be off"
grep_or_fail -qE '^[[:space:]]*dtoverlay=disable-bt[[:space:]]*$' "$btcfg" \
  || fail "/boot/firmware/config.txt does not carry an uncommented dtoverlay=disable-bt; the kernel would attach the on-board Bluetooth adapter"

# pi-gen's stage2/02-net-tweaks writes 0 into a systemd-rfkill state file for
# each known on-board Bluetooth address, which means "come up UNBLOCKED"
# (systemd stores one_zero(soft) there). With the overlay above there is no
# such device, so these files are inert -- but an inert file that says
# "unblocked" is a trap for whoever removes the overlay later.
rfkill_state="$ROOT/var/lib/systemd/rfkill"
reject_symlink "$rfkill_state" "/var/lib/systemd/rfkill"
if [ -d "$rfkill_state" ]; then
  run_find "$rfkill_state" -maxdepth 1 -type f -name '*:bluetooth' -print
  while IFS= read -r state; do
    [ -n "$state" ] || continue
    if grep_or_fail -qxE '[[:space:]]*0[[:space:]]*' "$state"; then
      fail "${state#"$ROOT"} un-blocks the Bluetooth radio (it holds 0); the appliance's radio must come up soft-blocked"
    fi
  done <<<"$FOUND"
fi

# systemd-ssh-generator honours systemd.ssh_listen=<address> from the kernel
# command line and writes an sshd-extra.socket with that ListenStream, wired
# into sockets.target -- a listening sshd armed by editing one file on the
# boot partition, with no package and no enablement symlink for the rules
# above to see. Purging openssh-server is what actually defuses it (the
# generator gives up when find_executable("sshd") fails), and this rule keeps
# the boot partition itself honest. Every cmdline is read: the A/B layout
# has one per slot (cmdline-a.txt, cmdline-b.txt), and a leftover cmdline.txt
# is checked here too even though the layout rule below refuses it.
for cmdline in "$BOOT/cmdline.txt" "$BOOT/cmdline-a.txt" "$BOOT/cmdline-b.txt"; do
  if [ -f "$cmdline" ] && grep_or_fail -qE '(^|[[:space:]])systemd\.ssh_listen=' "$cmdline"; then
    fail "/boot/firmware/${cmdline##*/} carries systemd.ssh_listen=, which makes systemd generate a listening sshd socket"
  fi
done
ok "no mDNS responder, no Bluetooth stack, no SSH server, no USB-network gadget"

# A rule that does not name a daemon, so that the next one to arrive is caught
# without anybody remembering to add it here: no ENABLED socket unit may listen
# on anything but a local socket.
#
# WHAT THIS CAN AND CANNOT DO, said plainly because it would otherwise read as
# a general "nothing listens" proof, which it is not. It judges socket units.
# It would NOT have caught avahi-daemon, the very thing this section exists to
# remove: avahi-daemon.socket is ListenStream=/run/avahi-daemon/socket, a UNIX
# activation socket, and the UDP 5353 bind is done by the daemon itself after
# it starts. Nothing in a unit file describes that. The same is true of any
# daemon that opens its own sockets -- which is most of them. This rule covers
# exactly one shape: socket activation on a network address.
#
# It is safe to run against a real image because every enabled socket unit in
# stock trixie plus Raspberry Pi OS listens on a local address. Read from the
# debs on 2026-09-19: systemd's fourteen socket units are all AF_UNIX paths,
# a FIFO, netlink or /dev/rfkill; udev's are /run/udev/control and netlink;
# dbus.socket is /run/dbus/system_bus_socket. The one stock unit that would
# fail is openssh-server's ssh.socket (ListenStream=22) -- which this image
# purges, and which failing is the correct outcome.
#
# Enablement is the same notion used above: a .wants/.requires/.upholds
# symlink in either unit tree. A socket unit placed directly in
# /etc/systemd/system is judged too, since that overrides the packaged one.
# Drop-ins are read as well: a Listen= line in a .d/*.conf is as live as one
# in the unit.
# An allow-list rather than a deny-list, so an address shape nobody thought of
# fails closed. vsock: and vsock-stream: forms are deliberately NOT allowed:
# an AF_VSOCK socket only means anything inside a VM, a Raspberry Pi is not
# one, and systemd's own vsock socket (sshd-vsock.socket) is written by a
# generator at boot only when it detects virtualization -- so nothing on this
# image can legitimately ship one, and a unit that did would be worth failing
# the build over rather than waving through.
socket_listen_is_local() {
  case "$1" in
    /* | @* | %t/*) return 0 ;;                      # AF_UNIX path or abstract
    127.0.0.1:* | '[::1]:'* | localhost:*) return 0 ;; # loopback only
    *) return 1 ;;
  esac
}
check_socket_unit() {
  local unit_file="$1" name confs conf raw value units
  name="$(basename -- "$unit_file")"
  confs="$unit_file"
  for units in "$ROOT/etc/systemd/system" "$ROOT/usr/lib/systemd/system"; do
    [ -d "$units/$name.d" ] || continue
    reject_symlink "$units/$name.d" "${units#"$ROOT"}/$name.d"
    run_find "$units/$name.d" -maxdepth 1 -xtype f -name '*.conf' -print
    confs="$confs
$FOUND"
  done
  while IFS= read -r conf; do
    [ -n "$conf" ] || continue
    [ -f "$conf" ] || continue
    if grep_line_or_fail -iE '^[[:space:]]*Listen(Stream|Datagram|SequentialPacket)[[:space:]]*=' "$conf"; then
      while IFS= read -r raw; do
        [ -n "$raw" ] || continue
        value="$(printf '%s\n' "$raw" | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]+$//')"
        # An empty assignment resets the list; it listens on nothing.
        [ -n "$value" ] || continue
        socket_listen_is_local "$value" \
          || fail "socket unit $name listens on '$(sanitize_for_log "$value")' (${conf#"$ROOT"}), which is not a local address; the panel accepts no inbound connection"
      done <<<"$LINE"
    fi
  done <<<"$confs"
}
enabled_sockets=""
for units in "$ROOT/etc/systemd/system" "$ROOT/usr/lib/systemd/system"; do
  [ -d "$units" ] || continue
  run_find "$units" \( -path '*.wants/*' -o -path '*.requires/*' -o -path '*.upholds/*' \) \
    -name '*.socket' -print
  enabled_sockets="$enabled_sockets
$FOUND"
done
run_find "$ROOT/etc/systemd/system" -maxdepth 1 -xtype f -name '*.socket' -print
enabled_sockets="$enabled_sockets
$FOUND"
while IFS= read -r sock; do
  [ -n "$sock" ] || continue
  # An enablement symlink points at the unit file; a masked one points at
  # /dev/null and starts nothing, so it is not read as a listener.
  if [ -L "$sock" ]; then
    target="$(readlink "$sock")"
    case "$target" in
      /*) candidate="$ROOT$target" ;;
      *) candidate="$(dirname -- "$sock")/$target" ;;
    esac
  else
    candidate="$sock"
  fi
  [ "$(readlink -m -- "$candidate")" != "/dev/null" ] || continue
  [ -f "$candidate" ] || continue
  check_socket_unit "$candidate"
done <<<"$enabled_sockets"
ok "no enabled socket unit listens on a non-local address"

cfg="$ROOT/opt/scoreboard/.venv/pyvenv.cfg"
[ -f "$cfg" ] || fail "no virtualenv at /opt/scoreboard/.venv"
grep -qE '^include-system-site-packages[[:space:]]*=[[:space:]]*true' "$cfg" \
  || fail "the virtualenv does not include system site packages, so it cannot see the distribution's pygame"
run_find "$ROOT/opt/scoreboard/.venv" -type d -name pygame -print -quit
[ -z "$FOUND" ] || fail "the virtualenv carries its own pygame (${FOUND#"$ROOT"}), which has no kmsdrm driver"
[ -d "$ROOT/usr/lib/python3/dist-packages/pygame" ] || fail "the distribution's pygame is not installed"
ok "the virtualenv uses the distribution's pygame"

# The right pygame is not enough: it dlopens the rest of the display path at
# runtime. SDL_egl.c opens "libEGL.so.1" and "libGLESv2.so.2" by those exact
# sonames; the glvnd dispatcher reads a vendor JSON to find Mesa's
# libEGL_mesa.so.0; and libgbm dlopens its backend, gbm/dri_gbm.so. None of
# those is a dependency of anything else in the image, so apt never installs
# them unasked; the two package lists name them explicitly, and this is the
# check that they arrived.
#
# That is the WHOLE chain. An earlier version of this block also asserted
# dri/vc4_dri.so and dri/v3d_dri.so, on the belief that Mesa's GBM backend
# loads a per-driver DRI module. It does not, and inspecting the 26.2.2 arm64
# debs says so plainly: gbm/dri_gbm.so and libEGL_mesa.so.0 import no dlopen
# at all and both carry DT_NEEDED on libgallium-26.2.2-...so; no "%s_dri.so"
# template exists in libgallium, libEGL_mesa, dri_gbm.so or libgbm; and
# libgallium's strings carry VC4_DEBUG and V3D_DEBUG, because the vc4 and v3d
# gallium drivers are compiled into it. Every dri/*_dri.so is in fact a
# symlink to libdril_dri.so, a small shim that dlopens libEGL.so.1 itself --
# Mesa's legacy-DRI-over-EGL layer for the X server, which sits downstream of
# this path rather than under it. Those two rules were asserting files that do
# not carry the display, and "dril" is new enough upstream that a rename would
# have failed a perfectly good build, so they are gone.
#
# The paths are what the trixie arm64 debs actually ship, read with dpkg-deb -c
# on 2026-09-18 -- not recalled. Several are the head of a versioned symlink
# chain, which image_resolves() follows inside the image root.
ARCH_LIB="usr/lib/aarch64-linux-gnu"
need_display_file() {
  image_resolves "$1" || fail \
    "the display path is incomplete: /$1 is missing or unresolvable (shipped by $2). SDL loads it at runtime, so nothing in the image depends on it and apt will not install it on its own -- the panel would come up black"
}
need_display_file "$ARCH_LIB/libEGL.so.1" "libegl1"
need_display_file "$ARCH_LIB/libEGL_mesa.so.0" "libegl-mesa0"
need_display_file "usr/share/glvnd/egl_vendor.d/50_mesa.json" "libegl-mesa0"
need_display_file "$ARCH_LIB/libGLESv2.so.2" "libgles2"
need_display_file "$ARCH_LIB/libgbm.so.1" "libgbm1"
need_display_file "$ARCH_LIB/gbm/dri_gbm.so" "libgbm1"
ok "the display path is complete: EGL dispatcher, Mesa EGL vendor and its glvnd JSON, GLES2, and GBM with its backend"

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
# application code, the one certificate, and exactly the release-signing
# PUBLIC keys the repository names. A file counts as a certificate or key if
# its name has one of the usual extensions, matched case-insensitively and
# against symlinks too (! -type d, not -type f, so a symlink standing in for
# a certificate cannot dodge this), or if its content is plainly a
# certificate regardless of its name. A .pem under certs/release-signing/ is
# allowed only when the repository holds a byte-identical file of that name:
# these keys are the trust root every panel verifies a release against, so
# an extra one is a second signer nobody approved.
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
  if [ "$suspect" -eq 1 ]; then
    case "$rel" in
      certs/AmazonRootCA1.pem) ;;
      # The release public keys (design 6.3): the repository's copies, byte
      # for byte, here and again below against the whole directory.
      certs/release-signing/*.pem)
        [ -f "$REPO/device/$rel" ] || fail "release-signing key $rel is in the image but not in the repository"
        [ -f "$path" ] && [ ! -L "$path" ] && cmp -s "$path" "$REPO/device/$rel" \
          || fail "release-signing key $rel is not a regular file or differs from the repository's device/$rel" ;;
      *) fail "unexpected certificate or key file under /opt/scoreboard: $rel" ;;
    esac
  fi
done <<<"$(printf '%s\n' "$FOUND" | sort)"
# The one certificate this image may ship must be the repository's own file,
# and a regular file -- not a symlink standing in for something else that a
# name or content match alone would wave through.
ca="$ROOT/opt/scoreboard/certs/AmazonRootCA1.pem"
[ -f "$ca" ] && [ ! -L "$ca" ] || fail "/opt/scoreboard/certs/AmazonRootCA1.pem is missing or is not a regular file"
cmp -s "$ca" "$REPO/device/certs/AmazonRootCA1.pem" \
  || fail "/opt/scoreboard/certs/AmazonRootCA1.pem differs from the repository's copy"
ok "the only certificate shipped is Amazon's root CA"

# The release public keys are the trust root every update is verified
# against (design 6.3): the panel accepts a manifest that any key in
# /opt/scoreboard/certs/release-signing/ verifies. So the directory must
# hold exactly the files the repository names, each byte for byte the
# repository's: every .pem a public key (a key added to the image that is
# not in the repository would let whoever holds its private half sign a
# release for every panel), every repository key present (a panel with a
# missing key cannot verify the next release and is stranded at the next
# rotation), and the README beside the keys the one non-key file allowed.
# The private halves live in AWS KMS and never in either place; the
# "no private keys" scan above already covers the directory, and the
# repository's copies are checked for one too, because this is where a
# mistake would be worst.
keydir="$ROOT/opt/scoreboard/certs/release-signing"
repokeys="$REPO/device/certs/release-signing"
reject_symlink "$keydir" "/opt/scoreboard/certs/release-signing"
[ -d "$keydir" ] || fail "/opt/scoreboard/certs/release-signing is missing; the panel would have no key to verify a release with"
run_find "$keydir" -mindepth 1 -print
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  name="${entry#"$keydir/"}"
  [ -f "$entry" ] && [ ! -L "$entry" ] || fail "release-signing/$(sanitize_for_log "$name") is not a regular file"
  case "$name" in
    README.md) ;;
    *.pem)
      grep_or_fail -qa -- '-----BEGIN PUBLIC KEY-----' "$entry" \
        || fail "release-signing/$name is not a SubjectPublicKeyInfo public key" ;;
    *) fail "release-signing/$(sanitize_for_log "$name") is not a .pem public key or the README" ;;
  esac
  [ -f "$repokeys/$name" ] || fail "release-signing/$name is in the image but not in the repository"
  cmp -s "$entry" "$repokeys/$name" || fail "release-signing/$name differs from the repository's copy"
done <<<"$(printf '%s\n' "$FOUND" | sort)"
run_find "$repokeys" -mindepth 1 -name '*.pem' -print
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  name="${entry##*/}"
  [ -f "$keydir/$name" ] \
    || fail "release-signing/$name is in the repository but not in the image; the panel could not verify a release signed by it"
  ! grep_or_fail -qaE -- '-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----' "$entry" \
    || fail "device/certs/release-signing/$name is a private key; only the public half may ever be in the repository"
done <<<"$FOUND"
ok "the release-signing keys are exactly the repository's public keys"

# The updater's health unit is what turns a trial boot into a commit or a
# rollback (design 5.2). A root without it enabled would run a trial slot
# uncommitted with nothing to reboot it, and the planner would then refuse to
# plan forever; the gate refuses to build it instead (design 7.3 step 1).
# The timer is what makes the panel look at all.
for pair in "multi-user.target.wants/scoreboard-health.service" "timers.target.wants/scoreboard-update.timer"; do
  link="$ROOT/etc/systemd/system/$pair"
  [ -L "$link" ] || fail "${pair#*/} is not enabled (/etc/systemd/system/$pair is not a symlink)"
  [ -f "$ROOT/etc/systemd/system/${pair#*/}" ] || fail "${pair#*/} is enabled but the unit file is missing"
done
for unit in scoreboard-update.service 'scoreboard-update@.service' \
            'scoreboard-update@a.service.d/slot.conf' 'scoreboard-update@b.service.d/slot.conf'; do
  [ -f "$ROOT/etc/systemd/system/$unit" ] || fail "the updater's $unit is missing"
done
ok "the updater's health unit and timer are enabled"

# --- The A/B layout (added 2026-09-25) ---------------------------------------
#
# What follows asserts the decisions in
# docs/superpowers/specs/2026-09-25-ota-update-design.md, section 4, against
# slot A's root and boot partitions, and with --image against the assembled
# card. The expected texts come from tools/image-layout.sh --print, so the
# gate cannot drift from what the layout script writes.

# The fstab, byte for byte. It is what makes the root read-only (no root
# entry for systemd-remount-fs to act on), mounts STATE and SETUP nofail so a
# torn shared partition boots to the help screen rather than emergency mode,
# and names exactly the five things that persist. Any other line is a sixth
# thing persisting, or a root that is writable again.
[ -x "$LAYOUT" ] || fail "$LAYOUT is missing; the gate reads the expected layout from it"
[ -f "$ROOT/etc/fstab" ] && [ ! -L "$ROOT/etc/fstab" ] || fail "/etc/fstab is missing or is a symlink"
"$LAYOUT" --print fstab | cmp -s - "$ROOT/etc/fstab" \
  || fail "/etc/fstab differs from what tools/image-layout.sh writes; the root's read-only mount, the nofail options or the list of what persists has changed"
ok "the fstab is the layout's, byte for byte"

# The pinned uid and gid. The identity on STATE is owned by this number, and
# a root whose scoreboard user has any other number cannot read it: every
# trial of that root would roll back and the fleet would be stranded. The
# number is read from the layout script, which owns the STATE skeleton, so
# the two cannot disagree; tools/pi-setup.sh's copy is asserted by
# device/tests/test_pi_setup.py.
SCOREBOARD_ID="$("$LAYOUT" --print scoreboard-id)"
[ "$SCOREBOARD_ID" -gt 0 ] 2>/dev/null || fail "tools/image-layout.sh --print scoreboard-id did not print a number"
scoreboard_uid="$(awk -F: '$1 == "scoreboard" { print $3 }' "$ROOT/etc/passwd")"
scoreboard_gid="$(awk -F: '$1 == "scoreboard" { print $4 }' "$ROOT/etc/passwd")"
[ "$scoreboard_uid" = "$SCOREBOARD_ID" ] && [ "$scoreboard_gid" = "$SCOREBOARD_ID" ] \
  || fail "the scoreboard user is uid ${scoreboard_uid:-missing} gid ${scoreboard_gid:-missing} in /etc/passwd, not the pinned $SCOREBOARD_ID; the identity on STATE would be unreadable by this root"
[ -f "$ROOT/etc/group" ] || fail "/etc/group is missing"
[ "$(awk -F: '$1 == "scoreboard" { print $3 }' "$ROOT/etc/group")" = "$SCOREBOARD_ID" ] \
  || fail "the scoreboard group is not gid $SCOREBOARD_ID in /etc/group"
ok "the scoreboard uid and gid are pinned at $SCOREBOARD_ID"

# The mount points the bind mounts need. A missing one makes that bind fail
# (nofail: quietly), and the panel would run with its identity, or the
# updater's records, on the read-only root instead of STATE.
for dir in var/lib/scoreboard var/lib/scoreboard-update var/lib/NetworkManager etc/NetworkManager/system-connections var/log/journal; do
  reject_symlink "$ROOT/$dir" "/$dir"
  [ -d "$ROOT/$dir" ] || fail "/$dir is missing; it is a bind-mount target for STATE"
done
[ -f "$ROOT/etc/fake-hwclock.data" ] && [ ! -L "$ROOT/etc/fake-hwclock.data" ] \
  || fail "/etc/fake-hwclock.data is missing; a bind mount needs a target file"
run_find "$ROOT/var/lib/scoreboard-update" -mindepth 1 -print -quit
[ -z "$FOUND" ] || fail "/var/lib/scoreboard-update is not empty (${FOUND#"$ROOT"}); the updater's records live on STATE, and a channel file in the image would put every panel on that channel"
# NetworkManager keeps resolv.conf under /run only when /etc/resolv.conf
# points there; a regular file here would be a write to a read-only /etc.
[ -L "$ROOT/etc/resolv.conf" ] && [ "$(readlink "$ROOT/etc/resolv.conf")" = "/run/NetworkManager/resolv.conf" ] \
  || fail "/etc/resolv.conf is not a symlink to /run/NetworkManager/resolv.conf; NetworkManager would write to the read-only root"
ok "the bind-mount targets exist and resolv.conf lives under /run"

# The generator that mounts the running slot's boot partition, and the
# watchdog. Both are copied from the repository, so they are compared to it.
generator="$ROOT/usr/lib/systemd/system-generators/scoreboard-bootfs"
[ -f "$generator" ] && [ ! -L "$generator" ] && [ -x "$generator" ] \
  || fail "/usr/lib/systemd/system-generators/scoreboard-bootfs is missing or not executable; /boot/firmware would never be mounted"
cmp -s "$generator" "$REPO/device/generators/scoreboard-bootfs" \
  || fail "the scoreboard-bootfs generator differs from device/generators/scoreboard-bootfs"
watchdog="$ROOT/etc/systemd/system.conf.d/10-scoreboard-watchdog.conf"
[ -f "$watchdog" ] && [ ! -L "$watchdog" ] || fail "/etc/systemd/system.conf.d/10-scoreboard-watchdog.conf is missing; a hung trial boot would wait for the plug"
cmp -s "$watchdog" "$REPO/device/system.conf.d/10-scoreboard-watchdog.conf" \
  || fail "the watchdog drop-in differs from device/system.conf.d/10-scoreboard-watchdog.conf"
grep -qE '^RuntimeWatchdogSec=60$' "$watchdog" || fail "the watchdog drop-in does not set RuntimeWatchdogSec=60"
# Nothing of ours may write /boot/firmware (design 4.3): the running slot's
# boot partition is mounted read-write for rpi-eeprom-update alone, and a
# unit that could write it could make the running slot unbootable. Every
# unit and drop-in under /etc/systemd/system is checked, whatever its name.
# Two directives can open the path: ReadWritePaths= and BindPaths=, whose
# destination follows a colon. /boot itself (a parent that makes the same
# directory writable) counts as /boot/firmware; /boot/setup, the health
# unit's own write path on another partition, does not.
reject_symlink "$ROOT/etc/systemd/system" "/etc/systemd/system"
out="$(grep -rlaE -- '^[[:space:]]*(ReadWritePaths|BindPaths)=(.*[[:space:]=+:-])?/boot(/firmware([[:space:]/:]|$)|/?([[:space:]:]|$))' \
  "$ROOT/etc/systemd/system" 2>&1)" && status=0 || status=$?
[ "$status" -le 1 ] || fail "could not scan /etc/systemd/system for ReadWritePaths: $(sanitize_for_log "$out")"
[ -z "$out" ] || fail "a unit may write /boot/firmware (ReadWritePaths= or BindPaths= naming /boot or /boot/firmware): $(sanitize_for_log "${out#"$ROOT"}")"
ok "the bootfs generator and the 60 s watchdog are installed, and no unit writes /boot/firmware"

# The rule above reads a unit's ReadWritePaths= and nothing else, which says
# nothing about a root unit that has no ProtectSystem= at all: that process
# can write the running slot's FAT, the root's remounted paths and all of
# STATE whatever list it carries or omits. So a unit that declares itself a
# SETUP writer (names /boot/setup in ReadWritePaths=) must also carry
# ProtectSystem=strict, or the declaration is decoration; and
# each of the two SETUP writers design 4.3 allows, scoreboard-netcfg and
# the updater's health unit (which commits a trial by rewriting
# autoboot.txt), must declare exactly the places it writes.
run_find "$ROOT/etc/systemd/system" -type f \( -name '*.service' -o -name '*.conf' \) -print
while IFS= read -r unit_path; do
  [ -n "$unit_path" ] || continue
  grep_or_fail -qE '^[[:space:]]*ReadWritePaths=(.*[[:space:]])?-?/boot/setup([[:space:]/]|$)' "$unit_path" || continue
  grep_or_fail -qE '^[[:space:]]*ProtectSystem=strict[[:space:]]*$' "$unit_path" \
    || fail "${unit_path#"$ROOT"} names /boot/setup in ReadWritePaths= without ProtectSystem=strict; a root unit with no ProtectSystem= can write the whole card, so the list means nothing"
done <<<"$FOUND"
netcfg_unit="$ROOT/etc/systemd/system/scoreboard-netcfg.service"
grep_or_fail -qE '^[[:space:]]*ProtectSystem=strict[[:space:]]*$' "$netcfg_unit" \
  || fail "scoreboard-netcfg.service has no ProtectSystem=strict; it runs as root and would have the whole card writable"
# A path inside STATE has to carry the `-` prefix. STATE is nofail, and on
# the torn-STATE boot design 4.3 promises to survive, /state is an empty
# directory on the read-only root and nothing under it exists; systemd then
# cannot set up the unit's namespace (226/NAMESPACE) and the unit never
# runs at all, which for the SETUP writer kills the one repair path that
# boot has. The mount points themselves (/state, /boot/setup, the bind
# targets under /var and /etc) are directories on the root and are there
# on every boot, so only a path beneath /state/ is caught here.
run_find "$ROOT/etc/systemd/system" -type f \( -name '*.service' -o -name '*.conf' \) -print
while IFS= read -r unit_path; do
  [ -n "$unit_path" ] || continue
  ! grep_or_fail -qE '^[[:space:]]*ReadWritePaths=(.*[[:space:]])?/state/' "$unit_path" \
    || fail "${unit_path#"$ROOT"} names a path inside STATE in ReadWritePaths= without the - prefix; on a torn-STATE boot the path is absent, namespace setup fails and the unit never runs"
done <<<"$FOUND"
grep_or_fail -qE '^[[:space:]]*ReadWritePaths=/boot/setup -/state/network[[:space:]]*$' "$netcfg_unit" \
  || fail "scoreboard-netcfg.service does not open exactly /boot/setup and -/state/network in ReadWritePaths=; the setup file and the country are the only things it writes"
health_unit="$ROOT/etc/systemd/system/scoreboard-health.service"
grep_or_fail -qE '^[[:space:]]*ReadWritePaths=/boot/setup /var/lib/scoreboard-update[[:space:]]*$' "$health_unit" \
  || fail "scoreboard-health.service does not open exactly /boot/setup and /var/lib/scoreboard-update in ReadWritePaths=; autoboot.txt and its records are the only things it writes"
ok "every unit that writes SETUP is sandboxed to what it writes, and no unit requires a STATE path to exist"

# The journal prune. /etc/machine-id is empty on the read-only root, so
# every boot gets a new id and journald opens a new /var/log/journal/<id>/
# on STATE; SystemMaxUse bounds only the running id's directory, and the
# others would fill STATE in about a month of nightly power cycles. The
# script and its unit are copied from the repository, so they are compared
# to it, and the unit must be pulled in by sysinit.target: it has
# DefaultDependencies=no so it can run before systemd-journal-flush.
prune="$ROOT/usr/local/sbin/scoreboard-journal-prune"
[ -f "$prune" ] && [ ! -L "$prune" ] && [ -x "$prune" ] \
  || fail "/usr/local/sbin/scoreboard-journal-prune is missing or not executable; one journal directory per boot would fill STATE"
cmp -s "$prune" "$REPO/device/scoreboard-journal-prune" \
  || fail "the journal prune script differs from device/scoreboard-journal-prune"
prune_unit="$ROOT/etc/systemd/system/scoreboard-journal-prune.service"
[ -f "$prune_unit" ] && [ ! -L "$prune_unit" ] || fail "scoreboard-journal-prune.service is not installed"
cmp -s "$prune_unit" "$REPO/device/scoreboard-journal-prune.service" \
  || fail "scoreboard-journal-prune.service differs from device/scoreboard-journal-prune.service"
prune_link="$ROOT/etc/systemd/system/sysinit.target.wants/scoreboard-journal-prune.service"
[ -L "$prune_link" ] || fail "scoreboard-journal-prune.service is not enabled in sysinit.target.wants"
target="$(readlink "$prune_link")"
case "$target" in
  /*) candidate="$ROOT$target" ;;
  *) candidate="$(dirname "$prune_link")/$target" ;;
esac
resolved="$(readlink -f -- "$candidate" 2>/dev/null || true)"
[ -n "$resolved" ] && [ "$resolved" = "$(readlink -f -- "$prune_unit")" ] \
  || fail "scoreboard-journal-prune.service's enable symlink does not resolve to the installed unit (points to $target)"
ok "the journal prune is installed and runs before the journal is flushed"

# Ordering after the nofail mounts. STATE, SETUP and every bind are nofail
# so a torn partition boots to the help screen (design 4.3), but
# systemd.mount(5) says a nofail mount is not ordered before local-fs.target,
# so no default dependency makes anything wait for them. Each unit that
# reads one names it in After=, which waits for the mount job to end however
# it ends and nothing more; RequiresMountsFor= would defeat the help-screen
# intent, so it is not what is asserted. A unit that has to be read for this
# is a unit that must exist as a real file.
unit_after() {
  # $1 unit file (image path), $2 the unit's name for the message, then the
  # mount units it must be After=. Every After= line is read, because a
  # unit may carry several.
  local file="$1" name="$2" want after
  shift 2
  [ -f "$file" ] && [ ! -L "$file" ] || fail "$name is missing from /etc/systemd/system or is a symlink"
  after=" $(sed -nE 's/^[[:space:]]*After=[[:space:]]*(.*[^[:space:]])[[:space:]]*$/\1/p' "$file" | tr '\n' ' ') "
  for want in "$@"; do
    case "$after" in
      *" $want "*) ;;
      *) fail "$name is not After=$want; a slow fsck of that partition would start it against the empty mount point on the read-only root" ;;
    esac
  done
}
unit_after "$ROOT/etc/systemd/system/scoreboard.service" scoreboard.service \
  state.mount var-lib-scoreboard.mount 'var-lib-scoreboard\x2dupdate.mount'
unit_after "$ROOT/etc/systemd/system/scoreboard-netcfg.service" scoreboard-netcfg.service \
  boot-setup.mount state.mount
nm_dropin="$ROOT/etc/systemd/system/NetworkManager.service.d/10-scoreboard-state.conf"
reject_symlink "$ROOT/etc/systemd/system/NetworkManager.service.d" "/etc/systemd/system/NetworkManager.service.d"
cmp -s "$nm_dropin" "$REPO/device/NetworkManager.service.d/10-scoreboard-state.conf" 2>/dev/null \
  || fail "/etc/systemd/system/NetworkManager.service.d/10-scoreboard-state.conf is missing or differs from device/NetworkManager.service.d/; NetworkManager would read an empty system-connections on a slow STATE"
unit_after "$nm_dropin" "NetworkManager's drop-in" \
  'etc-NetworkManager-system\x2dconnections.mount' var-lib-NetworkManager.mount
ok "the units that read STATE and SETUP are ordered after their mounts"

# The first-boot resize is disarmed in both its halves. The `resize` cmdline
# token is checked with the boot files below; the unit stage2 enabled is
# masked by tools/pi-gen/stage-scoreboard/06-fixed-layout, which says why a
# mask and not a disable (ConditionFirstBoot=yes is true on every boot of a
# root whose machine id is empty). Its enablement symlink must be gone too:
# systemd ignores a wants link to a masked unit, but the link is what a
# pi-gen bump that renames the unit would leave behind unnoticed.
assert_masked rpi-resize.service "the first-boot root resize"
run_find "$ROOT/etc/systemd/system" \( -path '*.wants/*' -o -path '*.requires/*' -o -path '*.upholds/*' \) \
  -name 'rpi-resize.service' -print -quit
[ -z "$FOUND" ] || fail "rpi-resize.service is still enabled (${FOUND#"$ROOT"}); the root must never resize"
ok "the first-boot root resize is masked and not enabled"

# Slot A's boot partition: one set of files serves both slots, so config.txt
# must start with the block that picks a cmdline by the partition it was
# loaded from, each cmdline must name its own root, say ro and not rw, and
# carry neither a first-boot init= nor pi-gen's `resize` token (the root
# must never resize; tools/image-layout.sh says where each comes from), and
# the cmdline.txt that would name a root this card does not have must be
# gone.
DISK_ID="$("$LAYOUT" --print disk-id)"
[ -f "$BOOT/config.txt" ] || fail "config.txt is missing from the boot partition"
head_lines="$("$LAYOUT" --print config-head | awk 'END { print NR }')"
head -n "$head_lines" -- "$BOOT/config.txt" | cmp -s - <("$LAYOUT" --print config-head) \
  || fail "config.txt does not start with the [boot_partition=N] cmdline block; both slots would boot the same root"
for slot in a b; do
  cmd="$BOOT/cmdline-$slot.txt"
  [ -f "$cmd" ] || fail "cmdline-$slot.txt is missing from the boot partition"
  [ "$(awk 'END { print NR }' "$cmd")" -eq 1 ] || fail "cmdline-$slot.txt must be one line"
  tokens=" $(tr -d '\n' <"$cmd") "
  case "$slot" in a) want="root=PARTUUID=$DISK_ID-05" ;; b) want="root=PARTUUID=$DISK_ID-06" ;; esac
  case "$tokens" in *" $want "*) ;; *) fail "cmdline-$slot.txt does not carry $want" ;; esac
  case "$tokens" in *" ro "*) ;; *) fail "cmdline-$slot.txt lacks ro; the root would mount writable" ;; esac
  case "$tokens" in *" rw "*) fail "cmdline-$slot.txt carries rw; the root would mount writable" ;; esac
  case "$tokens" in *" init="*) fail "cmdline-$slot.txt carries an init=; the first-boot resize must never run" ;; esac
  case "$tokens" in *" resize "*) fail "cmdline-$slot.txt carries pi-gen's resize token; the initramfs would grow the root partition on the first boot" ;; esac
done
[ ! -e "$BOOT/cmdline.txt" ] || fail "cmdline.txt is still on the boot partition; it names a root this card does not have"
[ -f "$BOOT/README.txt" ] || fail "README.txt is missing from the boot partition; a person who finds this drive must be sent to SETUP"
ok "slot A's boot files select the slot's own read-only root"

# --- The assembled card, with --image -----------------------------------------
if [ -n "$IMAGE" ]; then
  [ -f "$IMAGE" ] || fail "$IMAGE does not exist"
  for tool in sfdisk mcopy mdir debugfs sha256sum; do
    command -v "$tool" >/dev/null || fail "$tool is not installed; the gate cannot read the assembled image without it"
  done
  export MTOOLS_SKIP_CHECK=1 MTOOLSRC=/dev/null
  # The table, normalized to the script's own form: sfdisk --dump prefixes
  # each entry with the device path, which is stripped, and pads the numbers.
  actual_table="$(sfdisk --dump "$IMAGE" 2>/dev/null | grep -vE '^device:' | sed -E 's/^.* : (start=)/\1/; s/ +/ /g; s/= /=/g' | sed -E '/^$/d')"
  expected_table="$("$LAYOUT" --print table | sed -E 's/ +/ /g; s/= /=/g' | sed -E '/^$/d')"
  [ "$actual_table" = "$expected_table" ] \
    || fail "the partition table differs from tools/image-layout.sh --print table: $(sanitize_for_log "$(printf '%s' "$actual_table" | tr '\n' ';')")"
  ok "the partition table and disk identifier are the layout's"

  # Offsets and sizes in bytes, from the table just verified: the Nth entry
  # of sfdisk --dump is partition N.
  part_field() {
    sfdisk --dump "$IMAGE" 2>/dev/null | awk -v n="$1" -v f="$2" '
      / : start=/ { i++; if (i == n) { match($0, f "= *[0-9]+"); v = substr($0, RSTART, RLENGTH); sub(/.*= */, "", v); print v * 512 } }'
  }
  part_start() { part_field "$1" start; }
  part_size() { part_field "$1" size; }
  part_sha() { dd if="$IMAGE" bs=1M iflag=skip_bytes,count_bytes skip="$(part_start "$1")" count="$(part_size "$1")" status=none | sha256sum | cut -d' ' -f1; }

  # Slot B is slot A. A freshly flashed card has two identical, bootable
  # slots, and the update payloads are these same bytes.
  [ "$(part_sha 2)" = "$(part_sha 3)" ] || fail "BOOT-B (partition 3) is not byte-identical to BOOT-A (partition 2)"
  [ "$(part_sha 5)" = "$(part_sha 6)" ] || fail "ROOT-B (partition 6) is not byte-identical to ROOT-A (partition 5)"
  ok "slot B is byte-identical to slot A"

  # SETUP: autoboot.txt as flashed, a README, and NOTHING that makes it
  # bootable. With firmware on it, a lost autoboot.txt would boot from SETUP
  # with no root to go with it; with none, the walk lands on a slot.
  setup_offset="$(part_start 1)"
  setup_files="$(mdir -i "$IMAGE@@$setup_offset" -b :: 2>/dev/null | sed -E 's|^::/||' | sort | tr '\n' ' ' | sed -E 's/ $//')"
  [ "$setup_files" = "README.txt autoboot.txt" ] \
    || fail "SETUP holds '$(sanitize_for_log "$setup_files")', not exactly README.txt and autoboot.txt; anything that makes it bootable is where a lost autoboot.txt would land"
  mcopy -i "$IMAGE@@$setup_offset" ::autoboot.txt - 2>/dev/null | cmp -s - <("$LAYOUT" --print autoboot) \
    || fail "SETUP's autoboot.txt is not the layout's (slot A default, slot B on tryboot)"
  ok "SETUP holds autoboot.txt for slot A and nothing bootable"

  # STATE: the skeleton and nothing else. An identity here would make every
  # panel the same panel; a connection profile would be a Wi-Fi password
  # handed to strangers; a channel file would put every panel on that
  # channel. debugfs reads the partition copied out of the image, sparsely.
  state_img="$(mktemp "${TMPDIR:-/tmp}/gate-state.XXXXXX")"
  trap 'rm -f "$state_img"' EXIT
  dd if="$IMAGE" of="$state_img" bs=1M iflag=skip_bytes,count_bytes skip="$(part_start 7)" count="$(part_size 7)" conv=sparse status=none
  state_ls() { debugfs -R "ls -l $1" "$state_img" 2>/dev/null | awk 'NF >= 8 && $1 ~ /^[0-9]+$/ && $NF != "." && $NF != ".." { print $NF, $2, $4, $5 }'; }
  state_entries="$(state_ls / | grep -v '^lost+found ' | sort)"
  expected_entries="$("$LAYOUT" --print state-skeleton | awk '$1 !~ /\// { mode = ($2 == "d") ? "4" $3 : "10" $3; print $1, mode, $4, $4 }' | sort)"
  [ "$state_entries" = "$expected_entries" ] \
    || fail "STATE's top level is not the skeleton (name mode uid gid):
$(sanitize_for_log "$state_entries")
expected:
$expected_entries"
  # Then the whole tree at any depth, as one listing against the skeleton's
  # paths, rather than a hand-kept loop over the directories that are known
  # to exist today (a directory added to the skeleton and forgotten in such
  # a loop would ship whatever was under it). rdump copies the tree out;
  # ownership is not applied when the gate is not root, which is why the
  # modes and owners were read with ls -l above. Every extra path is named,
  # so the reason is in the log and not in a second run.
  state_dump="$(mktemp -d "${TMPDIR:-/tmp}/gate-state-tree.XXXXXX")"
  trap 'rm -rf "$state_img" "$state_dump"' EXIT
  debugfs -R "rdump / $state_dump" "$state_img" >/dev/null 2>&1 || fail "debugfs could not dump STATE's tree"
  run_find "$state_dump" -mindepth 1 ! -path "$state_dump/lost+found" ! -path "$state_dump/lost+found/*" -printf '%P\n'
  state_tree="$(printf '%s\n' "$FOUND" | sed '/^$/d' | sort)"
  expected_tree="$("$LAYOUT" --print state-skeleton | awk '{ print $1 }' | sort)"
  extra="$(comm -23 <(printf '%s\n' "$state_tree") <(printf '%s\n' "$expected_tree") | tr '\n' ' ' | sed 's/ $//')"
  missing="$(comm -13 <(printf '%s\n' "$state_tree") <(printf '%s\n' "$expected_tree") | tr '\n' ' ' | sed 's/ $//')"
  [ -z "$extra" ] || fail "STATE holds paths beyond the skeleton, at any depth: $(sanitize_for_log "$extra"); no identity, record, profile or journal may ship"
  [ -z "$missing" ] || fail "STATE lacks skeleton paths: $missing; a bind mount with no source directory fails quietly (nofail)"
  ok "STATE holds the skeleton, owned as pinned, and nothing else at any depth"
fi

echo "image-gate: all checks passed"
