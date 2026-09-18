#!/bin/bash -e
# Remove Raspberry Pi Connect, the remote-access agent stage2 installs.
#
# `rpi-connect signin` links a device to a Raspberry Pi account, which can then
# open a shell on it from a browser. This image's whole posture is that nobody
# can reach a panel, including the people who build it, so it carries no such
# agent. That it is inert as shipped -- rpi-connect-lite installs only *user*
# units (/usr/lib/systemd/user/rpi-connect{,-signin}.service and one .path),
# and signing in takes a logged-in user, which no locked account can be -- is
# why it is harmless today, not a reason to ship 21 MB of remote-access code
# that a future account, a changed default or a package update could wake up.
#
# WHY IT IS REMOVED HERE rather than skipped. pi-gen installs it from
# stage2/01-sys-tweaks/00-packages, which is the same list that brings ssh,
# sudo, console-setup, raspberrypi-sys-mods, python3-venv and the rest of the
# image. So the mechanism build.sh uses for stage2/04-cloud-init -- a SKIP file
# on the whole sub-stage -- is not available: that sub-stage *is* the image.
# Nothing else needs it either: across the Debian and Raspberry Pi trixie
# indexes the only package that mentions it is rpd-utilities, which merely
# Recommends the full rpi-connect and belongs to the desktop stages this build
# does not run.
#
# PURGE, NOT REMOVE. A removed-but-not-purged package keeps a
# "Status: deinstall ok config-files" stanza in /var/lib/dpkg/status, and
# tools/image-gate.sh reads that file's "Package:" lines as its installed-set
# signal, exactly as it does for cloud-init. The full rpi-connect is named too,
# so a pi-gen bump that switches packages is covered; apt exits 0 for a package
# it knows but has not installed.
#
# NO AUTOREMOVE. The only package this purge can orphan is dbus-user-session:
# rpi-connect-lite's other dependency, init-system-helpers, is Priority
# required and is never autoremoved. dbus-user-session is also a Recommends of
# libpam-systemd, which this image installs, so apt's default
# (APT::AutoRemove::RecommendsImportant) would keep it regardless. Running
# autoremove blind inside a chroot to gain nothing is the wrong trade.
on_chroot <<EOF
DEBIAN_FRONTEND=noninteractive apt-get purge -y rpi-connect rpi-connect-lite
EOF
