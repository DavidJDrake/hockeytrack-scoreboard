#!/bin/bash -e
# Disarm the first-boot root resize. The A/B card's partitions are fixed
# (OTA design, decision 1): ROOT-A and ROOT-B are the same 3 GiB in every
# image and every update payload, and a root that grew to the card would
# make slot A differ from the payload the update path writes over slot B.
#
# pi-gen arms the resize in two places at the pinned commit. stage1's
# cmdline.txt ends in `resize`, which the initramfs's resize_early script
# (raspberrypi-sys-mods, scripts/local-premount) reads from /proc/cmdline
# to move the root partition's end to the end of the card; and
# stage2/01-sys-tweaks runs `systemctl enable rpi-resize`, whose unit grows
# the filesystem into the partition on the first boot. The token is dropped
# by tools/image-layout.sh when it writes cmdline-a.txt and cmdline-b.txt;
# the unit is masked here.
#
# Masked, not only disabled, because on this card the unit would otherwise
# run on EVERY boot: it carries ConditionFirstBoot=yes, and systemd judges a
# boot to be the first when /etc/machine-id is empty, which is what the
# read-only root ships (image-layout.sh empties it so no two panels share
# an id). Its ExecStartPost, `systemctl disable`, would then fail on the
# read-only root each time and leave a failed unit in every journal. The
# enablement symlink stage2 wrote is removed as well: a mask alone leaves a
# dangling link that `systemctl status` reports and the gate would have to
# explain.
#
# Written as a plain symlink rather than `systemctl mask`, as the other
# stages do, because there is no running systemd in the chroot; the gate
# asserts this exact shape (assert_masked rpi-resize.service) and that no
# .wants directory names the unit.
ln -sfn /dev/null "${ROOTFS_DIR}/etc/systemd/system/rpi-resize.service"
find "${ROOTFS_DIR}/etc/systemd/system" \
	\( -path '*.wants/*' -o -path '*.requires/*' -o -path '*.upholds/*' \) \
	-name 'rpi-resize.service' -delete
