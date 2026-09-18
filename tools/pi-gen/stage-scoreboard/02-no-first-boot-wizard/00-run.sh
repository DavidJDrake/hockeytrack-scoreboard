#!/bin/bash -e
# Disarm Raspberry Pi OS's first-boot user-creation wizard.
#
# v0.1.0 booted to it on real hardware: userconf-pi's userconfig.service opens
# a whiptail dialog on tty8 asking for a new username and a password. This
# appliance has no keyboard, so nobody can answer it and the panel never
# reaches the "Not registered" screen (docs/hardware-checks.md, H5).
#
# WHERE IT IS ARMED. Not in stage2, and not before this stage: pi-gen runs
# export-image/01-user-rename against the *mounted image*, after every stage
# in STAGE_LIST has finished (build.sh's `for EXPORT_DIR in ${EXPORT_DIRS}`
# loop). With DISABLE_FIRST_BOOT_USER_RENAME at its default of 0 that
# sub-stage runs `rename-user -f -s` in the image's chroot. On a Lite image
# raspi-config's get_boot_cli reports 0 (default.target is multi-user and
# lightdm is not installed), so rename-user skips its whole desktop branch --
# no rpi-first-boot-wizard account, no piwiz.desktop, no sudoers drop-in --
# and does only this: consults raspi-config's get_autologin (whose other branch
# would clear /var/lib/userconf-pi/autologin, a marker this image does not
# have), writes /etc/ssh/sshd_config.d/rename_user.conf, runs `systemctl
# disable getty@tty1`, and runs `systemctl enable userconfig`. Anything this
# stage deleted would simply be written again afterwards, and upstream's own
# undo, `cancel-rename`, cannot run any earlier than the thing it undoes.
#
# The export stage keeps going afterwards: export-image/02-set-sources runs
# `apt-get update` and `apt-get -y dist-upgrade --auto-remove --purge` inside
# the image, so a userconf-pi upgrade can land after the arming. The mask
# survives that too -- deb-systemd-helper's unmask removes only a mask it
# created itself (init-system-helpers 1.69: "We cannot unconditionally unmask
# because that would interfere with the user's decision to mask a service"),
# and this one is an administrator's own symlink with no state file behind it.
#
# WHAT SURVIVES. Masking the unit. systemctl refuses to enable a masked unit
# and creates no symlink ("Failed to enable unit ... is masked", exit 1). The
# export sub-stage still succeeds, and the precise reason matters: on_chroot
# feeds its heredoc to `bash -e`, so errexit IS in effect there. It does not
# matter, because the only command in that heredoc is `rename-user`, which is
# a separate script with no `set -e` of its own (SHELLOPTS is not exported
# into it) and whose last command is an echo -- so it exits 0 whatever the
# masked enable did. The mask is also what stops the unit being started by
# anything that enables it later.
#
# WHY NOT THE CONFIG SWITCH. DISABLE_FIRST_BOOT_USER_RENAME=1 would skip
# rename-user entirely, but pi-gen's build.sh refuses to build without
# FIRST_USER_PASS alongside it ("To disable user rename on first boot,
# FIRST_USER_PASS needs to be set"), and a password baked into an image
# strangers download is exactly what this image must not carry. Spec 6.1 and
# 9.2 keep both settings out of tools/pi-gen/config, and
# device/tests/test_pi_gen_recipe.py fails the build if either appears.
#
# TTY1, and what it costs. rename-user disables getty@tty1 after this stage
# and nothing here can put it back, so the image boots with no login prompt on
# tty1 and the console is left to the panel, which draws through kmsdrm. Of the
# two acceptable outcomes -- a plain login prompt, or none -- this is the one
# pi-gen leaves. A prompt would be decorative *for access*, since every account
# in the image is locked; but the console was also the only place a startup
# failure could be read, and losing it is a real cost. What replaces it is the
# journal, which 04-persistent-journal keeps on the card for exactly this
# reason: a panel that fails to start now shows a black screen, and the card is
# where the reason is. What is not acceptable is an autologin shell on a panel
# anyone can walk up to, and nothing here creates one. tools/image-gate.sh
# fails the build if either the wizard or an autologin is present.
ln -sfn /dev/null "${ROOTFS_DIR}/etc/systemd/system/userconfig.service"

# Nothing arms the wizard this early today, so none of the sweeps below find
# anything. They are here so that a pi-gen bump which moves the arming into a
# stage is undone outright rather than only masked, and so that a shape
# tools/image-gate.sh refuses is cleaned here rather than failing a release
# build thirty-five minutes in. The gate and this file must therefore refuse
# the same shapes: device/tests/test_pi_gen_recipe.py asserts that the two
# agree on the unit-name set and on the autologin patterns, so they cannot
# drift apart again.
find "${ROOTFS_DIR}/etc/systemd/system" \
	\( -path '*.wants/*' -o -path '*.requires/*' -o -path '*.upholds/*' \) \
	-name 'userconfig.service' -delete

# Both unit trees, because the gate reads both. Removing a drop-in that came
# from a package would leave dpkg believing the file is still there; nothing
# ships one today, and an image that fails the gate is worse.
for units in "${ROOTFS_DIR}/etc/systemd/system" "${ROOTFS_DIR}/usr/lib/systemd/system"; do
	[ -d "$units" ] || continue
	# A drop-in directory that is itself a symlink hides its contents from
	# find -P, so neither this sweep nor the gate can see into one; the gate
	# fails the build on it, so it goes.
	while IFS= read -r linkdir; do
		rm -f "$linkdir"
	done < <(find "$units" -type l \
		\( -name 'getty@*.service.d' -o -name 'serial-getty@*.service.d' \
		   -o -name 'autovt@*.service.d' -o -name 'console-getty.service.d' \) -print)
	# -xtype f: a drop-in symlinked to a real file is read by systemd, so it
	# is read here. The short form is matched attached (-api) as well as
	# detached (-a pi).
	while IFS= read -r dropin; do
		if grep -qE -e '--autologin' -e 'agetty.*[[:space:]]-a' "$dropin"; then
			rm -f "$dropin"
		fi
	done < <(find "$units" -xtype f \
		\( -path '*/getty@*.service.d/*' -o -path '*/serial-getty@*.service.d/*' \
		   -o -path '*/autovt@*.service.d/*' -o -path '*/console-getty.service.d/*' \) -print)
done
# A replacement unit overrides the packaged one only when it sits directly in
# /etc/systemd/system -- hence -maxdepth 1, which also leaves the enablement
# symlinks under getty.target.wants/ alone.
while IFS= read -r override; do
	if grep -qE -e '--autologin' -e 'agetty.*[[:space:]]-a' "$override"; then
		rm -f "$override"
	fi
done < <(find "${ROOTFS_DIR}/etc/systemd/system" -maxdepth 1 -xtype f \
	\( -name 'getty@*.service' -o -name 'serial-getty@*.service' \
	   -o -name 'autovt@*.service' -o -name 'console-getty.service' \) -print)
