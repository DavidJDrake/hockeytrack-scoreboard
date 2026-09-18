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
# and does exactly three things: writes /etc/ssh/sshd_config.d/rename_user.conf,
# runs `systemctl disable getty@tty1`, and runs `systemctl enable userconfig`.
# Anything this stage deleted would simply be written again afterwards, and
# upstream's own undo, `cancel-rename`, cannot run any earlier than the thing
# it undoes.
#
# WHAT SURVIVES. Masking the unit. systemctl refuses to enable a masked unit
# and creates no symlink ("Failed to enable unit ... is masked", exit 1), and
# rename-user does not run under `set -e`: its last command is an echo, so the
# export sub-stage still exits 0 and the build still succeeds. The mask is
# also what stops the unit being started by anything that enables it later.
#
# WHY NOT THE CONFIG SWITCH. DISABLE_FIRST_BOOT_USER_RENAME=1 would skip
# rename-user entirely, but pi-gen's build.sh refuses to build without
# FIRST_USER_PASS alongside it ("To disable user rename on first boot,
# FIRST_USER_PASS needs to be set"), and a password baked into an image
# strangers download is exactly what this image must not carry. Spec 6.1 and
# 9.2 keep both settings out of tools/pi-gen/config, and
# device/tests/test_pi_gen_recipe.py fails the build if either appears.
#
# TTY1. rename-user disables getty@tty1 after this stage and nothing here can
# put it back, so the image boots with no login prompt on tty1 and the console
# is left to the panel, which draws through kmsdrm. Of the two acceptable
# outcomes -- a plain login prompt, or none -- this is the one pi-gen leaves,
# and it costs nothing: every account in the image is locked, so a prompt
# would be decorative. What is not acceptable is an autologin shell on a panel
# anyone can walk up to, and nothing here creates one. tools/image-gate.sh
# fails the build if either the wizard or an autologin drop-in is present.
ln -sfn /dev/null "${ROOTFS_DIR}/etc/systemd/system/userconfig.service"

# Nothing arms the wizard this early today, so these two find nothing. They
# are here so that a pi-gen bump which moves the arming into a stage is undone
# outright rather than only masked, and so the state this stage guarantees is
# written down where it is enforced.
find "${ROOTFS_DIR}/etc/systemd/system" \
	\( -path '*.wants/*' -o -path '*.requires/*' -o -path '*.upholds/*' \) \
	-name 'userconfig.service' -delete
while IFS= read -r dropin; do
	if grep -qE -e '--autologin' -e 'agetty.*[[:space:]]-a[[:space:]]' "$dropin"; then
		rm -f "$dropin"
	fi
done < <(find "${ROOTFS_DIR}/etc/systemd/system" -type f \
	\( -path '*/getty@*.service.d/*' -o -path '*/serial-getty@*.service.d/*' \) -print)
