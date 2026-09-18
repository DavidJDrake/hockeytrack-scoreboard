#!/bin/bash -e
# Keep the journal on the card, so a panel that fails to start can be diagnosed.
#
# There is no other diagnosis surface left. scoreboard-appliance.service takes
# tty1 (TTYPath=/dev/tty1) and sends stdout and stderr to the journal; a
# display failure exits 78, which RestartPreventExitStatus=78 turns into a
# stopped service and a black screen; any other crash restarts every 3 s in
# silence. And raspberrypi-sys-mods ships
# /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf with
# Storage=volatile, so the journal lives in RAM: pulling the card and reading
# it on another machine yields nothing at all.
#
# PRECEDENCE. journald reads journald.conf, then every *.conf drop-in from
# /etc, /run, /usr/local/lib and /usr/lib. The drop-ins are sorted by filename
# across all those directories at once, and the lexicographically last file to
# set an option wins -- so beating 40-rpi-volatile-storage.conf takes a name
# that sorts after it, not merely a file under /etc. Hence the 95- prefix.
# (/etc only beats /usr/lib outright for a drop-in of the *same* name, which
# is the masking case, not this one.)
install -d -m 755 "${ROOTFS_DIR}/etc/systemd/journald.conf.d"
cat >"${ROOTFS_DIR}/etc/systemd/journald.conf.d/95-scoreboard-persistent-journal.conf" <<-'EOF'
	# Set by tools/pi-gen/stage-scoreboard/04-persistent-journal.
	# Overrides /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf,
	# which this file must sort after to win.
	[Journal]
	Storage=persistent
	SystemMaxUse=50M
	# journald's default SyncIntervalSec is 5 minutes for anything at ERR or
	# below, and the lines this whole feature exists to capture are ERR --
	# "cannot open the display: ..." among them. Someone watching a black
	# screen pulls the power long before five minutes are up, and would lose
	# exactly that line. 30s is the window instead. It costs extra fsyncs,
	# which an appliance that logs almost nothing when healthy barely
	# notices; CRIT and above are synced immediately either way.
	SyncIntervalSec=30s
EOF
chmod 644 "${ROOTFS_DIR}/etc/systemd/journald.conf.d/95-scoreboard-persistent-journal.conf"

# Storage=persistent creates /var/log/journal itself if it is missing, but
# creating it here means the image ships in the state it will run in, the gate
# can assert it, and nothing depends on journald's first-boot behaviour. 2755
# root:systemd-journal is what systemd's own tmpfiles.d entry sets it to.
install -d -m 2755 "${ROOTFS_DIR}/var/log/journal"
on_chroot <<-EOF
	chown root:systemd-journal /var/log/journal
EOF

# What this puts on the card: the scoreboard's own log lines. Audited on
# 2026-09-18 against device/scoreboard/ -- no secret reaches a log call. The
# Wi-Fi password is never interpolated into any message (netcfg's parse errors
# carry a length, never the value) and _run_nmcli converts a
# subprocess.TimeoutExpired, whose str() embeds the argv the password is in,
# into an argv-free NetworkError raised outside the handler so nothing is left
# on __context__ for log.exception to walk. The owner's email address is
# logged only as whether one exists. The collection token and the private key
# are never logged at all. What does land here is a panel's IoT thing name
# ("claimed as %s"), the game id it is following, nmcli's own error text, and
# the path of the boot-partition file it read. Not that file's contents, with
# one exact exception: a malformed country line is echoed back as
# `country must be a two-letter code such as US, got 'xyz'`, which is the
# user's typo and not a secret. Never the password, and never the SSID.
