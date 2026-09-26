#!/usr/bin/env bash
# Build the six-partition A/B card image from pi-gen's two-partition output.
#
#   tools/image-layout.sh <rootfs> <bootfs> <version> <out dir>
#   tools/image-layout.sh --print <what>
#
# <rootfs> and <bootfs> are pi-gen's root and boot partitions, mounted
# read-only by the caller (.github/workflows/image.yml already mounts them for
# the gate). pi-gen stays pinned and unmodified: its export stage lays out one
# boot partition and one root sized to its contents and arms a first-boot
# resize, and a fixed A/B layout is incompatible with both, so the layout is
# built FROM pi-gen's image here rather than by patching pi-gen
# (docs/superpowers/specs/2026-09-25-ota-update-design.md, 4.4).
#
# MBR holds four primary partitions, so the three that are neither a boot
# slot nor SETUP are logical partitions inside an extended container that
# takes number 4: SETUP is 1, BOOT-A 2, BOOT-B 3, ROOT-A 5, ROOT-B 6 and STATE
# 7. The design numbered them 4, 5 and 6 (as the Raspberry Pi documentation's
# example does), which MBR cannot do; the boot slots keep the numbers
# autoboot.txt and config.txt's [boot_partition=N] filter depend on, and the
# fixed disk identifier keeps every PARTUUID stable, which is what the
# numbering was for. Each logical partition needs its EBR in the 4 MiB before
# it, which is where the three gaps in the table come from.
#
# Leaves in <out dir>:
#   scoreboard-<v>.boot.img      one slot's boot partition (FAT32, 256 MiB)
#   scoreboard-<v>.root.img      one slot's root partition (ext4, 3 GiB)
#   scoreboard-<v>.boot.img.xz   the same two, compressed: the update payloads
#   scoreboard-<v>.root.img.xz
#   scoreboard-<v>.img           the flash image: SETUP, BOOT-A, BOOT-B,
#                                ROOT-A, ROOT-B, STATE
#   scoreboard-<v>.payloads.json the payloads' sizes and sha256, as written
#                                and as compressed, for the release manifest
#
# Both slots are filled from the SAME two files, so a freshly flashed panel
# has two identical, bootable slots and its first update overwrites B; and the
# payloads are byte-identical to what a flashed panel's slot A holds, which the
# workflow asserts by extracting slot A from the assembled image and comparing
# hashes. Nothing here is ever mounted: the FAT filesystems are written with
# mtools and the ext4 ones with mkfs.ext4 -d and debugfs, so this runs without
# root wherever the inputs are readable, and the same is true of the tests.
#
# --print <what> prints one of the constants below or a file this script
# writes, so tools/image-gate.sh asserts the SAME fstab, autoboot.txt and
# partition table this script produced rather than a second copy of them:
#   disk-id, partitions, min-card-mib, scoreboard-id, table, fstab, autoboot,
#   config-head, readme, setup-readme, state-skeleton
set -euo pipefail

# --- The constants (design 4.1). Every size in MiB. -------------------------
#
# The MBR disk identifier is fixed so that the same boot files are valid on
# every card ever flashed and in every update payload: a PARTUUID is this
# identifier plus the partition number, so nothing depends on /dev/mmcblk0
# naming, and cmdline-a.txt's root= is the same bytes on every panel.
DISK_ID="5c0ab0ad"
# Every partition starts on a 4 MiB boundary. The sizes are fixed and the root
# never resizes, so the image is the same on every card and the unused rest of
# a larger card is unused on purpose.
SETUP_MIB=64
BOOT_MIB=256
ROOT_MIB=3072
STATE_MIB=256
FIRST_MIB=4
# 4 + 64 + 256 + 256 + (4 + 3072) + (4 + 3072) + (4 + 256): the partitions plus
# the 4 MiB before the first and the 4 MiB EBR gap before each logical one.
# The smallest cards sold as "8 GB" are about 7.4 GB, which is about 7,057
# MiB, so the margin is about 65 MiB. This is the one place that number
# lives; the download page quotes it.
MIN_CARD_MIB=6992
# The 4 MiB before each logical partition, holding its EBR and keeping the
# partition itself on a 4 MiB boundary.
EBR_MIB=4
# The build fails when a root would be fuller than this or a boot slot fuller
# than that, so growth is noticed at build time rather than as a failed update
# in the field. The scoreboard root uses about 2 GiB of its 3; a Lite boot
# partition holds about 75 MB of its 256 MiB.
ROOT_MAX_PERCENT=80
BOOT_MAX_PERCENT=60
# The scoreboard uid and gid, pinned in tools/pi-setup.sh: the identity on
# STATE is readable by a new root only if the number is the same in every
# root. The STATE skeleton's scoreboard/ is owned by this number, and the
# gate reads it from here (--print scoreboard-id) rather than keeping a copy.
SCOREBOARD_ID=900

# Partition starts, derived once so the table and the dd offsets cannot
# disagree.
SETUP_START=$FIRST_MIB
BOOT_A_START=$((SETUP_START + SETUP_MIB))
BOOT_B_START=$((BOOT_A_START + BOOT_MIB))
EXTENDED_START=$((BOOT_B_START + BOOT_MIB))
ROOT_A_START=$((EXTENDED_START + EBR_MIB))
ROOT_B_START=$((ROOT_A_START + ROOT_MIB + EBR_MIB))
STATE_START=$((ROOT_B_START + ROOT_MIB + EBR_MIB))
IMAGE_MIB=$((STATE_START + STATE_MIB))
EXTENDED_MIB=$((IMAGE_MIB - EXTENDED_START))
# The partition numbers the kernel and the bootloader use, in one place.
SETUP_PART=1
BOOT_A_PART=2
BOOT_B_PART=3
ROOT_A_PART=5
ROOT_B_PART=6
STATE_PART=7
[ "$IMAGE_MIB" -eq "$MIN_CARD_MIB" ] || { echo "image-layout: the partition sizes add up to $IMAGE_MIB MiB, not the stated minimum card size of $MIN_CARD_MIB" >&2; exit 1; }

die() { echo "image-layout: $*" >&2; exit 1; }

# --- What this script writes into the filesystems ---------------------------

# sfdisk's script form. Type c is FAT32 (LBA), 5 the extended container, 83
# Linux; the entries after the container are its logical partitions. MBR
# partitions have no names; SETUP, BOOT-A and the rest are the design's names
# for the partition NUMBERS, and the filesystem labels below are what a
# computer shows. The two boot slots and the two roots carry the same label
# because they are the same bytes (see the header).
print_table() {
  local mib_sectors=2048
  cat <<EOF
label: dos
label-id: 0x$DISK_ID
unit: sectors
sector-size: 512

start=$((SETUP_START * mib_sectors)), size=$((SETUP_MIB * mib_sectors)), type=c
start=$((BOOT_A_START * mib_sectors)), size=$((BOOT_MIB * mib_sectors)), type=c
start=$((BOOT_B_START * mib_sectors)), size=$((BOOT_MIB * mib_sectors)), type=c
start=$((EXTENDED_START * mib_sectors)), size=$((EXTENDED_MIB * mib_sectors)), type=5
start=$((ROOT_A_START * mib_sectors)), size=$((ROOT_MIB * mib_sectors)), type=83
start=$((ROOT_B_START * mib_sectors)), size=$((ROOT_MIB * mib_sectors)), type=83
start=$((STATE_START * mib_sectors)), size=$((STATE_MIB * mib_sectors)), type=83
EOF
}

# The root's fstab (design 4.3). Two mounts are deliberately absent: the root
# itself, which is whatever cmdline-a.txt or cmdline-b.txt named and stays
# read-only because no fstab entry exists for systemd-remount-fs to make it
# writable; and /boot/firmware, which differs by slot and is mounted by the
# scoreboard-bootfs generator instead. nofail on the two shared partitions
# and on every bind is deliberate: a torn SETUP or an unfixable STATE must
# give a panel that boots to its help screen and can be looked at, not
# emergency mode with no getty. Exactly five things persist, each by bind
# mount from STATE; everything else writable is tmpfs, and there is no
# overlay, because an overlay on /etc would let version N's stale file shadow
# version N+1's.
print_fstab() {
  cat <<EOF
PARTUUID=$DISK_ID-07  /state          ext4  rw,noatime,nodev,nosuid,noexec,nofail,x-systemd.device-timeout=10s   0 2
PARTUUID=$DISK_ID-01  /boot/setup     vfat  rw,nodev,nosuid,noexec,umask=022,nofail,x-systemd.device-timeout=10s 0 2
tmpfs             /tmp            tmpfs nodev,nosuid,size=64m           0 0
tmpfs             /var/tmp        tmpfs nodev,nosuid,size=16m           0 0
tmpfs             /var/log        tmpfs nodev,nosuid,noexec,size=16m    0 0
tmpfs             /var/lib/systemd tmpfs nodev,nosuid,noexec,size=8m    0 0
/state/scoreboard        /var/lib/scoreboard                    none bind,nofail,x-systemd.requires-mounts-for=/state 0 0
/state/update            /var/lib/scoreboard-update             none bind,nofail,x-systemd.requires-mounts-for=/state 0 0
/state/network/connections /etc/NetworkManager/system-connections none bind,nofail,x-systemd.requires-mounts-for=/state 0 0
/state/network/lib       /var/lib/NetworkManager                none bind,nofail,x-systemd.requires-mounts-for=/state 0 0
/state/journal           /var/log/journal                       none bind,nofail,x-systemd.requires-mounts-for=/state 0 0
/state/fake-hwclock.data /etc/fake-hwclock.data                 none bind,nofail,x-systemd.requires-mounts-for=/state 0 0
EOF
}

# autoboot.txt as flashed (design 4.2): slot A is the default, and a tryboot
# reboot tries slot B once. After a committed update the two numbers swap.
# This file is the only thing on the card that says which slot is current.
print_autoboot() {
  cat <<'EOF'
[all]
tryboot_a_b=1
boot_partition=2
[tryboot]
boot_partition=3
EOF
}

# The block prepended to config.txt, identical on both slots: the firmware
# sets boot_partition to the partition config.txt was loaded from, so one set
# of boot files serves both slots and each slot's cmdline names its own root.
print_config_head() {
  cat <<'EOF'
# A/B boot: the firmware sets boot_partition to the partition this file was
# loaded from (2 or 3); each slot's cmdline names its own root.
[boot_partition=2]
cmdline=cmdline-a.txt
[boot_partition=3]
cmdline=cmdline-b.txt
[all]
EOF
}

# Placed on every FAT partition. macOS and newer Windows may show all three;
# the setup file goes on the one whose label the instructions can name.
# Two READMEs, because a computer shows this card as three drives and two of
# them are the wrong one to edit. The boot slots send the person to SETUP;
# SETUP tells them they are in the right place, or it would be telling the
# person who found the right drive that it is the wrong one.
print_readme() {
  cat <<'EOF'
This card holds a HockeyTrack scoreboard panel.

To set up Wi-Fi, put scoreboard-setup.txt on the drive called SETUP, not on
this one. Nothing else on this card is meant to be edited.
EOF
}

print_setup_readme() {
  cat <<'EOF'
This card holds a HockeyTrack scoreboard panel, and this drive, SETUP, is
the one place on it meant to be edited.

To set up Wi-Fi, put scoreboard-setup.txt here, next to this file. The panel
reads it on its next start and removes the password from it. Leave
autoboot.txt alone: it is how the panel chooses which copy of its system to
start.
EOF
}

# The STATE skeleton (design 4.3): what survives boots and updates, and
# nothing else. One entry per line: path, mode, owner. journal/ is left
# root:root here because systemd-journal's gid is not pinned; systemd's own
# tmpfiles.d entry (z /var/log/journal 2755 root systemd-journal) corrects it
# on every boot. fake-hwclock.data is a file, so the bind mount has a target.
print_state_skeleton() {
  cat <<EOF
scoreboard d 0700 $SCOREBOARD_ID
update d 0755 0
network d 0755 0
network/connections d 0700 0
network/lib d 0755 0
journal d 2755 0
fake-hwclock.data f 0644 0
EOF
}

case "${1:-}" in
  --print)
    case "${2:-}" in
      disk-id) echo "$DISK_ID" ;;
      partitions) echo "setup=$SETUP_PART boot-a=$BOOT_A_PART boot-b=$BOOT_B_PART root-a=$ROOT_A_PART root-b=$ROOT_B_PART state=$STATE_PART" ;;
      min-card-mib) echo "$MIN_CARD_MIB" ;;
      scoreboard-id) echo "$SCOREBOARD_ID" ;;
      table) print_table ;;
      fstab) print_fstab ;;
      autoboot) print_autoboot ;;
      config-head) print_config_head ;;
      readme) print_readme ;;
      setup-readme) print_setup_readme ;;
      state-skeleton) print_state_skeleton ;;
      *) die "usage: image-layout.sh --print disk-id|partitions|min-card-mib|scoreboard-id|table|fstab|autoboot|config-head|readme|setup-readme|state-skeleton" ;;
    esac
    exit 0 ;;
esac

usage="usage: image-layout.sh <rootfs> <bootfs> <version> <out dir>"
ROOTFS="${1:?$usage}"
BOOTFS="${2:?$usage}"
VERSION="${3:?$usage}"
OUT="${4:?$usage}"
case "$VERSION" in
  *[!A-Za-z0-9.-]* | "") die "bad version: $VERSION" ;;
esac
[ -d "$ROOTFS" ] && [ -f "$ROOTFS/etc/passwd" ] || die "$ROOTFS is not a root filesystem"
[ -d "$BOOTFS" ] && [ -f "$BOOTFS/config.txt" ] && [ -f "$BOOTFS/cmdline.txt" ] || die "$BOOTFS is not a boot partition (no config.txt or cmdline.txt)"
for tool in sfdisk mkfs.vfat mcopy mmd mdir mkfs.ext4 debugfs dumpe2fs xz sha256sum; do
  command -v "$tool" >/dev/null || die "$tool is not installed (dosfstools, mtools, e2fsprogs, util-linux, xz-utils)"
done
mkdir -p "$OUT"
WORK="$(mktemp -d "$OUT/layout.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

BOOT_IMG="$OUT/scoreboard-$VERSION.boot.img"
ROOT_IMG="$OUT/scoreboard-$VERSION.root.img"
STATE_IMG="$WORK/state.img"
SETUP_IMG="$WORK/setup.img"
IMAGE="$OUT/scoreboard-$VERSION.img"

# mtools refuses a FAT whose geometry it dislikes unless told not to care,
# and would otherwise read ~/.mtoolsrc; neither belongs in a build.
export MTOOLS_SKIP_CHECK=1
export MTOOLSRC=/dev/null

# Percent of a FAT image in use, from mdir's "bytes free" line, so the
# headroom rule below is checked on the filesystem as written.
fat_used_percent() {
  local img="$1" free total
  # mdir groups the digits with spaces ("264 285 184 bytes free"), so the
  # whole prefix of the line is taken and every non-digit dropped.
  free="$(mdir -i "$img" :: | sed -nE 's/^(.*) bytes free.*$/\1/p' | tr -dc '0-9')"
  [ -n "$free" ] || die "could not read the free space of $img"
  total="$(stat -c %s "$img")"
  echo $(( (total - free) * 100 / total ))
}

# Percent of an ext4 image in use, from dumpe2fs, for the same rule.
ext4_used_percent() {
  local img="$1" blocks free
  blocks="$(dumpe2fs -h "$img" 2>/dev/null | awk -F: '/^Block count:/ { gsub(/ /, "", $2); print $2 }')"
  free="$(dumpe2fs -h "$img" 2>/dev/null | awk -F: '/^Free blocks:/ { gsub(/ /, "", $2); print $2 }')"
  [ -n "$blocks" ] && [ -n "$free" ] || die "could not read the free space of $img"
  echo $(( (blocks - free) * 100 / blocks ))
}

# --- 1. The boot slot -------------------------------------------------------
echo "==> boot slot"
grep -q 'root=' "$BOOTFS/cmdline.txt" || die "pi-gen's cmdline.txt has no root="
# Each slot's cmdline is pi-gen's with root= pointing at its own root
# partition, `ro` present, and everything that would grow or rewrite the root
# removed. At the pinned pi-gen (tools/pi-gen/PIGEN_REF) the line is
#
#   console=serial0,115200 console=tty1 root=ROOTDEV rootfstype=ext4
#   fsck.repair=yes rootwait resize
#
# with ROOTDEV substituted by export-image/04-set-partuuid. There is no `ro`
# and no `init=` on it: the design (4.2, fact 10) described an older pi-gen
# whose first boot ran `init=/usr/lib/raspberrypi-sys-mods/firstboot`; the
# pinned one arms the resize with the `resize` token instead, which the
# initramfs's resize_early script reads from /proc/cmdline to grow the root
# partition to the end of the card, and enables rpi-resize.service
# (stage2/01-sys-tweaks) to finish the job. The root must never resize
# (decision 1), so the token goes, and tools/pi-gen/stage-scoreboard/
# 06-fixed-layout masks the unit. `init=` is still dropped in case a pi-gen
# bump brings it back, and `rw` too: the kernel mounts the root read-only
# by default, and nothing in the fstab remounts it, but the gate wants the
# `ro` stated so the intent is on the card and not in a default.
make_cmdline() {
  local partition="$1"
  {
    tr -d '\n' <"$BOOTFS/cmdline.txt" | tr ' ' '\n' | grep -v '^$' \
      | grep -vE '^(init=|resize$|ro$|rw$)' \
      | sed -E "s|^root=.*|root=PARTUUID=$DISK_ID-$partition|"
    echo ro
  } | tr '\n' ' ' | sed -E 's/ $//'
  echo
}
make_cmdline "0$ROOT_A_PART" >"$WORK/cmdline-a.txt"
make_cmdline "0$ROOT_B_PART" >"$WORK/cmdline-b.txt"
# Read back rather than trusted, because the gate asserts the same four
# things and a build that fails here is cheaper than one that fails there.
for f in cmdline-a.txt cmdline-b.txt; do
  grep -qE '(^| )ro( |$)' "$WORK/$f" || die "$f lacks the ro flag"
  ! grep -qE '(^| )rw( |$)' "$WORK/$f" || die "$f still mounts the root writable"
  ! grep -qE '(^| )init=' "$WORK/$f" || die "$f still carries an init="
  ! grep -qE '(^| )resize( |$)' "$WORK/$f" || die "$f still carries pi-gen's resize token; the root must never resize"
done
{ print_config_head; cat "$BOOTFS/config.txt"; } >"$WORK/config.txt"
print_readme >"$WORK/README.txt"

rm -f "$BOOT_IMG"
truncate -s "${BOOT_MIB}M" "$BOOT_IMG"
mkfs.vfat -F 32 -n BOOT "$BOOT_IMG" >/dev/null
# Everything pi-gen put on the boot partition except the two files replaced
# above and cmdline.txt itself, which would otherwise sit there naming a
# root partition this card does not have.
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  rel="${entry#"$BOOTFS/"}"
  case "$rel" in config.txt | cmdline.txt) continue ;; esac
  if [ -d "$entry" ]; then
    mmd -i "$BOOT_IMG" "::$rel"
  else
    mcopy -i "$BOOT_IMG" "$entry" "::$rel"
  fi
done <<<"$(find "$BOOTFS" -mindepth 1 \( -type d -o -type f \) | sort)"
mcopy -i "$BOOT_IMG" "$WORK/config.txt" "$WORK/cmdline-a.txt" "$WORK/cmdline-b.txt" "$WORK/README.txt" ::
used="$(fat_used_percent "$BOOT_IMG")"
[ "$used" -le "$BOOT_MAX_PERCENT" ] || die "the boot slot is $used% full; the limit is $BOOT_MAX_PERCENT% so growth is caught here, not as a failed update"
echo "    $used% of $BOOT_MIB MiB used"

# --- 2. The root slot -------------------------------------------------------
echo "==> root slot"
rm -f "$ROOT_IMG"
truncate -s "${ROOT_MIB}M" "$ROOT_IMG"
# -d copies the tree with its ownership and modes without ever mounting the
# result. The rest of the changes to the root (the fstab, the mount points,
# an empty machine id) are made with debugfs for the same reason.
mkfs.ext4 -q -F -L ROOT -d "$ROOTFS" "$ROOT_IMG"
print_fstab >"$WORK/fstab"
: >"$WORK/machine-id"
# debugfs's scripted form has no conditionals, so what may or may not exist
# in the source root (/boot on pi-gen's root, /etc/machine-id) is probed
# first and the command list built to match.
exists_in_root() { debugfs -R "stat $1" "$ROOT_IMG" 2>/dev/null | grep -q '^Inode:'; }
{
  echo "rm /etc/fstab"
  echo "write $WORK/fstab /etc/fstab"
  echo "set_inode_field /etc/fstab uid 0"
  echo "set_inode_field /etc/fstab gid 0"
  echo "set_inode_field /etc/fstab mode 0100644"
  for dir in /state /boot /boot/setup; do
    exists_in_root "$dir" && continue
    echo "mkdir $dir"
    echo "set_inode_field $dir uid 0"
    echo "set_inode_field $dir gid 0"
    echo "set_inode_field $dir mode 040755"
  done
  ! exists_in_root /etc/machine-id || echo "rm /etc/machine-id"
  echo "write $WORK/machine-id /etc/machine-id"
  echo "set_inode_field /etc/machine-id uid 0"
  echo "set_inode_field /etc/machine-id gid 0"
  echo "set_inode_field /etc/machine-id mode 0100644"
} | debugfs -w "$ROOT_IMG" >"$WORK/debugfs.log" 2>&1
# debugfs reports most failures as text on stdout and still exits 0, so
# the log is read for them instead of trusting the exit status.
! grep -qiE 'error|not found|no such|failed' "$WORK/debugfs.log" || { cat "$WORK/debugfs.log" >&2; die "debugfs could not finish the root"; }
# Read back what was written, so a debugfs that silently did nothing fails
# here rather than on a panel.
debugfs -R "cat /etc/fstab" "$ROOT_IMG" 2>/dev/null | cmp -s - "$WORK/fstab" || die "the fstab in the root image is not the one this script wrote"
debugfs -R "stat /state" "$ROOT_IMG" 2>/dev/null | grep -q 'Type: directory' || die "/state was not created in the root image"
debugfs -R "stat /boot/setup" "$ROOT_IMG" 2>/dev/null | grep -q 'Type: directory' || die "/boot/setup was not created in the root image"
used="$(ext4_used_percent "$ROOT_IMG")"
[ "$used" -le "$ROOT_MAX_PERCENT" ] || die "the root slot is $used% full; the limit is $ROOT_MAX_PERCENT% so growth is caught here, not as a failed update"
echo "    $used% of $ROOT_MIB MiB used"

# --- 3. STATE ---------------------------------------------------------------
echo "==> state"
skeleton="$WORK/state"
mkdir -p "$skeleton"
while read -r path kind mode owner; do
  [ -n "$path" ] || continue
  case "$kind" in
    d) mkdir -p "$skeleton/$path" ;;
    f) : >"$skeleton/$path" ;;
  esac
done <<<"$(print_state_skeleton)"
truncate -s "${STATE_MIB}M" "$STATE_IMG"
mkfs.ext4 -q -F -L STATE -d "$skeleton" "$STATE_IMG"
# Ownership and mode are set on the image, not the staging directory: a
# build that does not run as root cannot chown to 900, and debugfs can.
{
  echo "set_inode_field / uid 0"
  echo "set_inode_field / gid 0"
  while read -r path kind mode owner; do
    [ -n "$path" ] || continue
    case "$kind" in d) full="04$mode" ;; f) full="010$mode" ;; esac
    echo "set_inode_field /$path uid $owner"
    echo "set_inode_field /$path gid $owner"
    echo "set_inode_field /$path mode $full"
  done <<<"$(print_state_skeleton)"
} | debugfs -w "$STATE_IMG" >"$WORK/debugfs-state.log" 2>&1
! grep -qiE 'error|not found|no such|failed' "$WORK/debugfs-state.log" || { cat "$WORK/debugfs-state.log" >&2; die "debugfs could not finish STATE"; }
debugfs -R "stat /scoreboard" "$STATE_IMG" 2>/dev/null | grep -qE "User: +$SCOREBOARD_ID +Group: +$SCOREBOARD_ID" \
  || die "STATE's scoreboard/ is not owned by $SCOREBOARD_ID:$SCOREBOARD_ID"

# --- 4. SETUP ---------------------------------------------------------------
echo "==> setup"
print_autoboot >"$WORK/autoboot.txt"
mkdir -p "$WORK/setup"
print_setup_readme >"$WORK/setup/README.txt"
truncate -s "${SETUP_MIB}M" "$SETUP_IMG"
mkfs.vfat -F 32 -n SETUP "$SETUP_IMG" >/dev/null
mcopy -i "$SETUP_IMG" "$WORK/autoboot.txt" "$WORK/setup/README.txt" ::

# --- 5. The flash image -----------------------------------------------------
echo "==> flash image"
rm -f "$IMAGE"
truncate -s "${IMAGE_MIB}M" "$IMAGE"
print_table | sfdisk --quiet "$IMAGE" >/dev/null
place() {
  # conv=sparse keeps the image sparse on disk; notrunc keeps the table.
  dd if="$1" of="$IMAGE" bs=1M seek="$2" conv=sparse,notrunc status=none
}
place "$SETUP_IMG" "$SETUP_START"
place "$BOOT_IMG" "$BOOT_A_START"
place "$BOOT_IMG" "$BOOT_B_START"
place "$ROOT_IMG" "$ROOT_A_START"
place "$ROOT_IMG" "$ROOT_B_START"
place "$STATE_IMG" "$STATE_START"
# The table is read back from the image rather than trusted: sfdisk --quiet
# hides warnings, and the gate will assert this same text.
sfdisk --dump "$IMAGE" | grep -q "label-id: 0x$DISK_ID" || die "the disk identifier did not land in the image"

# --- 6. The update payloads -------------------------------------------------
echo "==> payloads"
rm -f "$BOOT_IMG.xz" "$ROOT_IMG.xz"
xz -T0 -k "$BOOT_IMG" "$ROOT_IMG"
raw_boot_sha="$(sha256sum "$BOOT_IMG" | cut -d' ' -f1)"
raw_root_sha="$(sha256sum "$ROOT_IMG" | cut -d' ' -f1)"
boot_sha="$(sha256sum "$BOOT_IMG.xz" | cut -d' ' -f1)"
root_sha="$(sha256sum "$ROOT_IMG.xz" | cut -d' ' -f1)"
cat >"$OUT/scoreboard-$VERSION.payloads.json" <<EOF
{
  "layout": 1,
  "boot": {"file": "scoreboard-$VERSION.boot.img.xz", "size": $(stat -c %s "$BOOT_IMG.xz"), "sha256": "$boot_sha", "rawSize": $((BOOT_MIB * 1048576)), "rawSha256": "$raw_boot_sha"},
  "root": {"file": "scoreboard-$VERSION.root.img.xz", "size": $(stat -c %s "$ROOT_IMG.xz"), "sha256": "$root_sha", "rawSize": $((ROOT_MIB * 1048576)), "rawSha256": "$raw_root_sha"}
}
EOF
echo "image-layout: wrote $IMAGE ($IMAGE_MIB MiB; needs a card of at least $MIN_CARD_MIB MiB)"
