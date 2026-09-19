#!/bin/bash -e
# Take the panel's network surface down to what it actually uses.
#
# What the panel needs from a network is a short list: DHCP, DNS, NTP, and
# outbound TLS to AWS IoT (8883) and HTTPS. It accepts no inbound connection,
# it has no login, and every account in it is locked. Anything that listens or
# radiates beyond that list is surface a stranger's living room or a rink's
# public Wi-Fi can reach, for no benefit to the panel.
#
# The surface was read from two real boots of v0.1.2 on a Pi 4 (the journals in
# docs/hardware-checks.md's "Reading a failed panel" procedure) and from the
# v0.1.3 release build log, not from memory. What was actually running:
#
#   avahi-daemon   started, "Server startup complete. Host name is
#                  scoreboard.local", and pi-gen's stage2/01-sys-tweaks sets
#                  publish-workstation=yes -- so every panel announces itself
#                  and a _workstation._tcp record on any network it joins. It
#                  only reached lo in those boots because Wi-Fi never came up;
#                  avahi's shipped config sets no allow-interfaces and both
#                  use-ipv4 and use-ipv6, so an associated wlan0 gets it too.
#   bluetoothd     started, "Starting SDP server", on an adapter the kernel
#                  attached from the device tree (Bluetooth: hci0: BCM4345C0).
#   sshd           not running, but /usr/sbin/sshd present -- see below.
#   wpa_supplicant, NetworkManager, systemd-timesyncd, systemd-journald, dbus,
#                  polkit, udisks2, cron, logind: no non-loopback listener
#                  between them. systemd-resolved is not installed at all, so
#                  nothing answers LLMNR or provides a stub resolver, and
#                  NetworkManager ships no connectivity-check URI, so it makes
#                  no unsolicited outbound request either.
#
# Not installed, and so not dealt with here: systemd-resolved, dhcpcd,
# triggerhappy, ModemManager, cups, rpcbind, nfs-common, samba, pi-bluetooth.
#
# Spec section 9.13 carries the full table with the evidence line for each row.

# --- PURGE ---------------------------------------------------------------
#
# Purge, not remove, for the reason 03-no-remote-access gives: `remove` leaves
# a "Status: deinstall ok config-files" stanza in /var/lib/dpkg/status, and
# that file's "Package:" lines are the signal tools/image-gate.sh reads.
#
# Each of these was checked against both trixie arm64 Packages indexes for
# reverse dependencies. NOTHING installed in this image Depends on any of
# them, so the purge drags no other package out except the `ssh` metapackage,
# which is named here so the command says what it does:
#
#   avahi-daemon    The mDNS/DNS-SD responder above. Installed by pi-gen's
#                   stage2/01-sys-tweaks/00-packages -- the same list that
#                   brings ssh, sudo and console-setup -- so the SKIP-the-
#                   sub-stage trick used for cloud-init is not available here
#                   either. Hard reverse dependencies in the image: none.
#   libnss-mdns     The NSS half of the same feature, useless once the daemon
#                   is gone. It is purged for a second reason: it Recommends
#                   avahi-daemon, and it is one of only two installed packages
#                   that did. The other is rpi-usb-gadget, also purged below,
#                   so after this stage NOTHING in the image recommends avahi
#                   and no later apt run has a reason to fetch it back.
#   bluez           bluetoothd and its SDP server, plus the org.bluez D-Bus
#                   activation file that could start it without a unit. The
#                   panel has no Bluetooth function. libbluetooth3 is NOT
#                   purged and must not be: network-manager Depends on it for
#                   libnm-device-plugin-bluetooth.so.
#   bluez-firmware  Bluetooth-only HCI patch files (BCM4345C0.hcd and friends).
#                   Read with dpkg-deb -c on 2026-09-19: it ships no brcmfmac
#                   file at all. The Wi-Fi firmware this panel cannot boot
#                   without -- brcmfmac43455-sdio for the Pi 4,
#                   brcmfmac43436-sdio for the Zero 2 W -- is in
#                   firmware-brcm80211, which stays.
#   rpi-usb-gadget  A package whose entire job is exposing a network interface
#                   over USB. Its unit ships disabled today; a package that
#                   only has to be enabled to become a network interface is
#                   not something this image needs to carry.
#   ssh-import-id   Fetches public keys from Launchpad or GitHub straight into
#                   authorized_keys. It runs no daemon, which is exactly why
#                   it is easy to miss: it is the thing the gate's
#                   authorized_keys rules exist to prevent, packaged as a
#                   convenience.
#   rpi-update      Downloads unreleased firmware and kernels from GitHub,
#                   outside apt's signatures, straight over the running ones.
#   openssh-server, openssh-sftp-server, openssh-client, ssh
#                   See the next block; this is the one decision here that is
#                   not obvious.
#
# WHY OPENSSH GOES, not just ssh.service staying disabled. An sshd that is
# installed but disabled is one `systemctl enable` from listening -- but that
# is the weaker half of the argument, because the gate already fails an image
# with that symlink. The stronger half is that the binary's mere presence arms
# two paths the gate's unit rules cannot see:
#
#   1. systemd-ssh-generator, which is part of systemd and always runs. It
#      calls find_executable("sshd") and returns immediately if that fails
#      ("Disabling SSH generator logic, since sshd is not installed").  If it
#      does NOT fail, it honours systemd.ssh_listen=<address> from the KERNEL
#      COMMAND LINE and writes sshd-extra.socket with that ListenStream, wired
#      into sockets.target -- a listening sshd armed by editing cmdline.txt on
#      the boot partition, with no package change and no enablement symlink
#      for anything to notice. Both real boots already show its AF_UNIX
#      cousin: "Listening on sshd-unix-local.socket - OpenSSH Server Socket
#      (systemd-ssh-generator, AF_UNIX Local)".
#   2. raspberrypi-sys-mods' sshswitch.service, which runs
#      `systemctl enable --now ssh` when a file named ssh is on the boot
#      partition. It is masked below as well, but with no sshd there is
#      nothing for it to turn on.
#
# openssh-client goes with it because nothing needs it: the only installed
# package that Depends on it is ssh-import-id, purged here too. Losing
# /usr/bin/ssh-keygen also makes raspberrypi-sys-mods'
# regenerate_ssh_host_keys.service a no-op rather than a unit that generates
# host keys every first boot for a daemon that no longer exists -- its
# ConditionFileIsExecutable=/usr/bin/ssh-keygen simply stops matching. It
# ships no system unit of its own, only a user ssh-agent socket no session
# here ever starts.
#
# WHAT AUTOREMOVE WOULD TAKE, and why none is run here. Eighteen packages, not
# the five libraries an earlier draft of this comment named. The list is a
# COMPUTED CLOSURE, not a guess: take the packages apt would treat as manually
# installed (every name pi-gen lists in an NN-packages file, plus this
# repository's own 00-packages, plus everything of Priority required or
# important, which debootstrap installs with dpkg directly), walk
# Depends/Pre-Depends/Recommends over the image's 836 packages -- Recommends
# included, because APT::AutoRemove::RecommendsImportant defaults to true --
# and subtract what is still reachable once the purge set is gone:
#
#   libavahi-core7 libdaemon0                       avahi's own
#   libfido2-1 libcbor0.10 libwrap0 libwtmpdb0      OpenSSH's own
#   xauth libxmuu1 ncurses-term runit-helper        OpenSSH's Recommends
#   wget python3-requests python3-urllib3           ssh-import-id's Depends
#     python3-certifi python3-chardet
#     python3-charset-normalizer python3-idna
#   iputils-arping                                  rpi-usb-gadget's Depends
#
# None is load-bearing, checked rather than assumed: the appliance's only HTTP
# client is stdlib urllib.request (device/scoreboard/enroll.py), requirements
# .txt names paho-mqtt, pygame and cryptography and nothing else, neither
# python3-cryptography nor python3-paho-mqtt touches anything on that list,
# and raspi-config, raspberrypi-sys-mods and userconf-pi call none of wget,
# xauth or arping anywhere in their scripts. ca-certificates is NOT on the
# list and cannot be: it is named in two NN-packages files, so apt has it
# marked manual. Nor is libavahi-common3 (libcups2t64 Depends on it) or
# libbluetooth3 (network-manager does). As in 03-no-remote-access, this stage
# runs no autoremove of its own, because pi-gen's export-image/02-set-sources runs
# `apt-get -y dist-upgrade --auto-remove --purge` against the mounted image
# afterwards regardless. Adding one here would change nothing.
#
# COULD THAT LATER RUN PUT ANY OF THIS BACK? Only through a Recommends, and
# after this stage exactly one such edge is left: raspberrypi-sys-mods (which
# is load-bearing and stays) Recommends ssh-import-id. apt does not follow it.
# apt-pkg/depcache.cc's MarkInstall, for a package that is already installed,
# follows a non-critical dependency only when the dependency is NEW in the
# candidate version or was satisfied before; otherwise it logs "ignore old
# unsatisfied important dependency" and skips it. A Recommends we have just
# purged is neither. The remaining case -- upstream adding a Recommends in a
# version published between this build's apt-get update and export-image's --
# is not defended against by a pin or an apt-mark hold, deliberately: those
# would hide the change. tools/image-gate.sh fails the build instead, which is
# the outcome we want when the recipe and the archive disagree.
on_chroot <<EOF
DEBIAN_FRONTEND=noninteractive apt-get purge -y \
	avahi-daemon libnss-mdns \
	bluez bluez-firmware \
	rpi-usb-gadget ssh-import-id rpi-update \
	openssh-server openssh-sftp-server openssh-client ssh
EOF

# openssh-server owned /etc/ssh/sshd_config.d, so purging it takes the
# directory with it -- and pi-gen's export-image/01-user-rename runs
# `rename-user -f -s` AFTER this stage, which unconditionally does
# `cat > /etc/ssh/sshd_config.d/rename_user.conf`. rename-user has no `set -e`
# and its last statement is an echo, so a failed redirect there would not fail
# the build; but "would not fail the build" is a worse thing to rely on than
# a directory that costs nothing. That is the whole reason. (It does not make
# the gate's AuthorizedKeysFile scan over sshd_config.d any more meaningful:
# with no sshd in the image, that scan has nothing to protect either way.)
install -d -m 755 "${ROOTFS_DIR}/etc/ssh/sshd_config.d"

# --- MASKS ---------------------------------------------------------------
#
# Written as plain symlinks rather than `systemctl mask`, as
# 02-no-first-boot-wizard does, because there is no running systemd in the
# chroot to talk to -- and because this is the exact shape tools/image-gate.sh
# asserts.
#
# A mask on a package that has just been purged is not redundant. It is what
# stops the unit being enabled if the package ever comes back: systemctl
# refuses to enable a masked unit and creates no symlink, and deb-systemd-
# helper's unmask only removes a mask it created itself (init-system-helpers
# 1.69: "We cannot unconditionally unmask because that would interfere with
# the user's decision to mask a service"), never an administrator's own.
#
# sshswitch.service is the one mask here whose package stays. It comes from
# raspberrypi-sys-mods, which is load-bearing (99-com.rules, the journald
# drop-in, get_fw_loc), is enabled on every Raspberry Pi OS image, and reads
# the boot partition: put a file named `ssh` there and it runs
# `systemctl enable --now ssh`. With openssh-server purged that call can only
# fail, so masking it costs nothing and removes the path outright rather than
# leaving a failed unit in the journal of any panel whose card someone poked.
#
# serial-getty@.service is masked for a reason the Bluetooth block below
# creates rather than inherits, and it is the TEMPLATE that is masked, not an
# instance: systemd resolves serial-getty@ttyAMA0.service by looking for a
# unit of that exact name and then falling back to the template, and
# /etc/systemd/system/serial-getty@.service is found first, so every instance
# is masked -- including one systemd-getty-generator writes at boot, which is
# a name we cannot predict from here. See THE SERIAL CONSOLE below.
for unit in \
	avahi-daemon.service avahi-daemon.socket \
	bluetooth.service \
	sshswitch.service \
	ssh.service ssh.socket sshd.service sshd.socket \
	serial-getty@.service; do
	ln -sfn /dev/null "${ROOTFS_DIR}/etc/systemd/system/${unit}"
done

# --- THE BLUETOOTH RADIO -------------------------------------------------
#
# Purging bluez stops the daemon. It does not stop the radio: the adapter is
# attached by the kernel from the device tree (both boots show "Bluetooth:
# HCI UART driver ver 2.3" and "Bluetooth: hci0: BCM4345C0"), and pi-bluetooth
# -- which is where hciuart.service would come from -- is not installed in
# this image at all, so there is no attach unit to mask. The device tree is
# therefore the only place the radio can actually be switched off, and that is
# what disable-bt does.
#
# WHAT THE OVERLAY DOES, read from its source
# (arch/arm/boot/dts/overlays/disable-bt-overlay.dts): it sets the &bt node to
# status = "disabled", disables &uart1 (the mini UART), enables &uart0 (the
# PL011) on GPIO 14/15, and repoints /aliases serial0 at /soc/serial@7e201000.
# Five consequences, each checked rather than assumed:
#
#  - Wi-Fi is untouched. The overlay names only uart0, uart1, bt, bt_pins,
#    uart0_pins and /aliases. On the shared CYW43455 (Pi 4) and CYW43436
#    (Zero 2 W) the Wi-Fi side is on SDIO, not the UART: the journal has
#    brcmfmac on .../mmc_host/mmc1/mmc1:0001 while Bluetooth arrives over
#    HCI UART. Different bus, different node.
#  - The Pi 4B is covered, which is what this image now targets. The overlay
#    README says "On Pis prior to Pi 5 this restores UART0/ttyAMA0 over GPIOs
#    14 & 15", so it also covers the Zero 2 W if that board is ever unshelved
#    (docs/hardware-checks.md, H6); disable-bt-pi5 exists for the other case
#    and this image does not target it.
#  - The GPIO buttons are unaffected. They are BCM 5 and 6
#    (device/scoreboard/buttons.py); the overlay claims 14 and 15, which the
#    mini UART already had.
#  - It CREATES A SERIAL CONSOLE. This is the one consequence that is not a
#    removal, so it has its own block below.
#  - Nothing later undoes it. config.txt is written once, by pi-gen's
#    stage1/00-boot-files, and no stage or export-image step touches it again
#    (export-image/04-set-partuuid seds fstab and cmdline.txt, not config.txt).
#
# The block is appended under an explicit [all] rather than relying on the
# file happening to end in one. pi-gen's file does end in [all] today, after
# [cm4], [cm5] and [pi5] -- but a bump that adds another board-specific
# section at the end would otherwise scope this to that board alone.
config="${ROOTFS_DIR}/boot/firmware/config.txt"
if ! grep -qE '^[[:space:]]*dtoverlay=disable-bt[[:space:]]*$' "$config"; then
	cat >>"$config" <<-'EOF'

		[all]
		# Set by tools/pi-gen/stage-scoreboard/05-no-listeners.
		# The panel has no Bluetooth function. This disables the device
		# tree's &bt node, so no adapter is ever attached: no hci0, no
		# SDP server, no rfkill device to unblock. Wi-Fi is on SDIO and
		# is not affected. It also turns GPIO 14/15 into a real serial
		# console -- see 05-no-listeners' "THE SERIAL CONSOLE" block,
		# and spec 9.13.
		dtoverlay=disable-bt
	EOF
fi

# --- THE SERIAL CONSOLE THE OVERLAY CREATES ------------------------------
#
# This is a surface the hardening pass ADDS, so it is argued rather than
# assumed. What changes, read from Raspberry Pi's own UART documentation and
# from the two real boots:
#
#   Today the Pi 4's primary UART is the mini UART and the PL011 is the
#   secondary, carrying Bluetooth (documentation/asciidoc/computers/
#   configuration/interfaces.adoc, "Primary and secondary UARTs"). The default
#   for enable_uart follows the primary: "If the primary interface is PL011,
#   the system defaults to 'on'. If the primary interface is the more
#   sensitive mini UART, the system defaults to 'off'". So today the firmware
#   passes 8250.nr_uarts=0, the ttyS0 that console=serial0 resolves to never
#   registers, and there is NO serial console at all -- the journal shows only
#   "printk: legacy console [tty1] enabled" and no serial getty under
#   getty.target.
#
#   disable-bt makes the PL011 primary (fragment@5 points /aliases serial0 at
#   /soc/serial@7e201000). enable_uart therefore defaults to 1, the PL011
#   registers -- as ttyAMA0, since it now takes alias index 0; the journal
#   shows it today as ttyAMA1, which is what the secondary UART gets -- and
#   console=serial0,115200 becomes a live kernel console on GPIO 14/15.
#
# KEPT, because it is the diagnosis path this project has needed on every
# failed boot. v0.1.0, v0.1.1 and v0.1.2 all had to be diagnosed by powering
# the panel down and reading the card; spec 9.12 has been asking for an
# on-panel failure painter ever since. A 3.3 V USB-serial adapter on pins 8
# and 10 now reads the boot log live, and with
# systemd.journald.forward_to_console=1 added to cmdline.txt by hand (the
# procedure docs/hardware-checks.md already documents) it reads the journal
# live too -- with no card removal and no login. It costs nothing to anyone
# who does not attach a cable, and it cannot be reached over a network.
#
# THE LOGIN PROMPT IS NOT KEPT. systemd-getty-generator reads
# /sys/class/tty/console/active and instantiates serial-getty@<tty>.service
# for every active non-virtual console (src/getty-generator/getty-generator.c,
# add_serial_getty). That prompt serves nobody here: every account in the
# image is locked, so nothing can get past it. It is masked above, by the
# template rather than the instance, because the instance name depends on what
# the firmware calls the port. Kernel console output is unaffected by the
# mask: printk does not go through a getty.
#
# WHAT IT COSTS, and the escape hatch. printk to a 115200 UART is synchronous,
# so a boot that previously wrote to tty1 alone now also serializes every
# kernel line out the UART, attached or not. docs/hardware-checks.md H9 part 1
# measures first paint against v0.1.3 for exactly this reason. If it turns out
# to cost more than a second or two on a panel that is already dark for up to
# 85 s (spec 9.12), the fix is one line: drop console=serial0,115200 from
# /boot/firmware/cmdline.txt here. That file is written by pi-gen's
# stage1/00-boot-files and only sed-edited afterwards (export-image/
# 04-set-partuuid substitutes ROOTDEV), so this stage can edit it safely.

# pi-gen's stage2/02-net-tweaks/01-run.sh deliberately un-blocks the on-board
# Bluetooth adapter, by writing 0 into a state file for each known on-board
# address, because raspberrypi-sys-mods boots with rfkill.default_state=0 and
# that would otherwise block Bluetooth along with Wi-Fi. Undo it: systemd's
# src/rfkill/rfkill.c stores one_zero(event->soft) in these files and restores
# that soft-block state at boot, so 1 means "come up blocked". With the
# overlay above there is no such device to restore, and the files are inert --
# they are rewritten anyway so that removing the overlay later does not
# silently bring the radio back up unblocked.
#
# This cannot collide with the Wi-Fi country step, which is what makes the
# panel work at all: raspi-config's do_wifi_country writes 0 only to
# /var/lib/systemd/rfkill/*:wlan, and its other two levers are
# `nmcli radio wifi on` and `rfkill unblock wifi`. All three are Wi-Fi-typed.
# Nothing on the panel runs `rfkill unblock all`.
#
# find's output is captured into a variable first rather than piped in through
# a process substitution: `bash -e` does not see the exit status of a process
# substitution, so a find that failed part-way -- an unreadable directory, a
# broken mount -- would read here as "no files to rewrite" and the stage would
# succeed having changed nothing. The gate would then be the only thing
# between that and a published image. This way the stage fails instead.
if [ -d "${ROOTFS_DIR}/var/lib/systemd/rfkill" ]; then
	bt_state_files="$(find "${ROOTFS_DIR}/var/lib/systemd/rfkill" -maxdepth 1 -type f -name '*:bluetooth' -print)"
	while IFS= read -r state; do
		[ -n "$state" ] || continue
		echo 1 >"$state"
	done <<-EOF
		${bt_state_files}
	EOF
fi
