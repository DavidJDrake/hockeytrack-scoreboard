"""tools/image-gate.sh against fixture root filesystems.

A gate that cannot fail guards nothing, so every assertion gets a fixture that
breaks exactly it, and the clean fixture must pass. The fixtures are plain
directories; nothing is mounted and nothing needs root.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "tools" / "image-gate.sh"
# Built at runtime so no PEM private-key header sits in the repository.
FAKE_KEY = "-----BEGIN " + "PRIVATE KEY-----\nnot a key\n-----END " + "PRIVATE KEY-----\n"


def clean_image(tmp_path: Path) -> tuple[Path, Path]:
    root, boot = tmp_path / "root", tmp_path / "boot"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "scoreboard:x:996:996::/var/lib/scoreboard:/usr/sbin/nologin\n"
        "pi:x:1000:1000:,,,:/home/pi:/bin/bash\n"
        "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
    )
    (root / "etc" / "shadow").write_text("root:*:20000:0:99999:7:::\nscoreboard:!:20000::::::\npi:!:20000:0:99999:7:::\nnobody:*:20000:0:99999:7:::\n")
    units = root / "etc" / "systemd" / "system"
    wants = units / "multi-user.target.wants"
    wants.mkdir(parents=True)
    for unit in ("scoreboard.service", "scoreboard-netcfg.service"):
        (units / unit).write_text("[Unit]\n")
        (wants / unit).symlink_to(f"/etc/systemd/system/{unit}")
    # Raspberry Pi OS (raspberrypi-sys-mods) ships this enabled on every
    # image, and raspberrypi-sys-mods stays -- so the enablement symlink is
    # still there on the hardened image. The scoreboard stage masks the unit
    # instead, because it reads the boot partition and would run
    # `systemctl enable --now ssh` for anyone who puts a file named ssh on the
    # card. Both the symlink and the mask belong in the clean fixture.
    (wants / "sshswitch.service").symlink_to("/usr/lib/systemd/system/sshswitch.service")
    # userconf-pi is a Recommends of raspberrypi-sys-mods, so every Raspberry
    # Pi OS image carries its unit file whether the wizard is armed or not,
    # and the scoreboard stage's fix is the mask symlink beside it. Neither is
    # an armed wizard -- only an enablement symlink is -- so both belong in
    # the clean fixture.
    lib_units = root / "usr" / "lib" / "systemd" / "system"
    lib_units.mkdir(parents=True)
    (lib_units / "userconfig.service").write_text(
        "[Unit]\nDescription=User configuration dialog\n[Install]\nWantedBy=multi-user.target\n")
    (units / "userconfig.service").symlink_to("/dev/null")
    (lib_units / "sshswitch.service").write_text(
        "[Unit]\nDescription=Turn on SSH if /boot/ssh is present\n[Install]\nWantedBy=multi-user.target\n")
    # A getty drop-in is not itself a finding: noclear.conf is the common one
    # and it logs nobody in. Only an autologin drop-in may fail the gate.
    (units / "getty@tty1.service.d").mkdir()
    (units / "getty@tty1.service.d" / "noclear.conf").write_text(
        "[Service]\nExecStart=\nExecStart=-/sbin/agetty --noclear %I $TERM\nTTYVTDisallocate=no\n")
    # The journal is the only diagnosis surface left once the panel owns tty1.
    # raspberrypi-sys-mods ships the volatile drop-in on every image, so the
    # stage's own file has to sort after it to win.
    lib_journal = root / "usr" / "lib" / "systemd" / "journald.conf.d"
    lib_journal.mkdir(parents=True)
    (lib_journal / "40-rpi-volatile-storage.conf").write_text("[Journal]\nStorage=volatile\n")
    etc_journal = root / "etc" / "systemd" / "journald.conf.d"
    etc_journal.mkdir(parents=True)
    (etc_journal / "95-scoreboard-persistent-journal.conf").write_text(
        "[Journal]\nStorage=persistent\nSystemMaxUse=50M\nSyncIntervalSec=30s\n")
    (root / "etc" / "systemd" / "journald.conf").write_text("[Journal]\n#Storage=auto\n#Compress=yes\n")
    (root / "var" / "log" / "journal").mkdir(parents=True)
    rules = root / "etc" / "polkit-1" / "rules.d"
    rules.mkdir(parents=True)
    shutil.copy(REPO / "device" / "polkit" / "10-scoreboard-network.rules", rules)
    (root / "etc" / "scoreboard-build").write_text("v0.1.0 · 2026-09-16 · abc1234\n")
    (root / "etc" / "NetworkManager" / "system-connections").mkdir(parents=True)
    # openssh-server is purged, so it no longer owns /etc/ssh/sshd_config.d --
    # the scoreboard stage recreates the directory because pi-gen's
    # export-image runs rename-user afterwards, which writes into it
    # unconditionally. Its one file is a Banner line no sshd will ever read.
    (root / "etc" / "ssh" / "sshd_config.d").mkdir(parents=True)
    (root / "etc" / "ssh" / "sshd_config.d" / "rename_user.conf").write_text(
        "Banner /usr/share/userconf-pi/sshd_banner\n")
    venv = root / "opt" / "scoreboard" / ".venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\ninclude-system-site-packages = true\nversion = 3.13.5\n")
    (root / "opt" / "scoreboard" / "certs").mkdir()
    shutil.copy(REPO / "device" / "certs" / "AmazonRootCA1.pem", root / "opt" / "scoreboard" / "certs")
    (root / "usr" / "lib" / "python3" / "dist-packages" / "pygame").mkdir(parents=True)
    # paho-mqtt comes from Debian's signed archive, like pygame, and the venv
    # holds nothing but the pip that python3 -m venv bundles.
    (root / "usr" / "lib" / "python3" / "dist-packages" / "paho").mkdir(parents=True)
    (venv / "lib" / "python3.13" / "site-packages" / "pip-25.1.1.dist-info").mkdir(parents=True)
    (venv / "lib" / "python3.13" / "site-packages" / "pip").mkdir()
    dpkg = root / "var" / "lib" / "dpkg"
    dpkg.mkdir(parents=True)
    (dpkg / "status").write_text(
        "Package: python3-paho-mqtt\nStatus: install ok installed\nVersion: 2.1.0-1\n\n"
        "Package: network-manager\nStatus: install ok installed\nVersion: 1.52.0-1\n"
    )
    (root / "var" / "lib" / "scoreboard").mkdir(parents=True)
    (root / "home" / "pi").mkdir(parents=True)
    (root / "root").mkdir()
    boot.mkdir()
    (boot / "config.txt").write_text(
        "dtparam=audio=on\n\n[all]\n# Set by 05-no-listeners.\ndtoverlay=disable-bt\n")
    (boot / "cmdline.txt").write_text(
        "console=serial0,115200 console=tty1 root=PARTUUID=abc-02 rootfstype=ext4 rootwait\n")
    _display_path(root)
    _network_surface(root)
    return root, boot


ARCH_LIB = "usr/lib/aarch64-linux-gnu"


def _display_path(root: Path) -> None:
    """The graphics files the panel needs to open its display at all.

    SDL's kmsdrm backend dlopens libEGL.so.1 and libGLESv2.so.2 by soname;
    the glvnd dispatcher reads 50_mesa.json to find libEGL_mesa.so.0; and
    libgbm dlopens its backend, gbm/dri_gbm.so. That is the whole chain --
    libEGL_mesa.so.0 and dri_gbm.so both hard-link (DT_NEEDED) against
    libgallium, which is where the vc4 and v3d drivers are compiled in, so
    nothing here loads a dri/*_dri.so at all (see tools/image-gate.sh).

    Several of these are the head of a versioned symlink chain in the real
    debs, so the fixture is built the same way: a gate that only tested the
    link itself would pass an image whose target was never unpacked.
    """
    arch = root / ARCH_LIB
    (arch / "gbm").mkdir(parents=True)
    for real, link in (("libEGL.so.1.1.0", "libEGL.so.1"),
                       ("libEGL_mesa.so.0.0.0", "libEGL_mesa.so.0"),
                       ("libGLESv2.so.2.1.0", "libGLESv2.so.2"),
                       ("libgbm.so.1.0.0", "libgbm.so.1")):
        (arch / real).write_bytes(b"\x7fELF not really")
        (arch / link).symlink_to(real)
    (arch / "gbm" / "dri_gbm.so").write_bytes(b"\x7fELF not really")
    vendor = root / "usr" / "share" / "glvnd" / "egl_vendor.d"
    vendor.mkdir(parents=True)
    (vendor / "50_mesa.json").write_text(
        '{"file_format_version":"1.0.0","ICD":{"library_path":"libEGL_mesa.so.0"}}\n')


# The units the scoreboard stage masks, and the human words the gate uses for
# each. Kept here so a break fixture can remove any one of them by name.
MASKED_UNITS = (
    "avahi-daemon.service", "avahi-daemon.socket", "bluetooth.service",
    "sshswitch.service", "ssh.service", "ssh.socket", "sshd.service", "sshd.socket",
    # The TEMPLATE, not an instance: disable-bt makes GPIO 14/15 a live kernel
    # console, systemd-getty-generator puts a serial-getty on it, and the
    # instance name depends on what the firmware calls the port.
    "serial-getty@.service",
)

# The socket units a real trixie + Raspberry Pi OS image actually has enabled,
# with the Listen= lines the debs ship, read with dpkg-deb on 2026-09-19. Every
# one is a local address, which is what makes the general socket rule safe to
# run against a real image. The clean fixture carries them so that a rule which
# rejected any of them would fail here rather than thirty-five minutes into a
# release build.
STOCK_SOCKETS = {
    "dbus.socket": "[Socket]\nListenStream=/run/dbus/system_bus_socket\n",
    "systemd-journald.socket":
        "[Socket]\nListenDatagram=/run/systemd/journal/socket\n"
        "ListenStream=/run/systemd/journal/stdout\n",
    "systemd-journald-dev-log.socket": "[Socket]\nListenDatagram=/run/systemd/journal/dev-log\n",
    "systemd-journald-audit.socket": "[Socket]\nListenNetlink=audit 1\n",
    "systemd-udevd-control.socket": "[Socket]\nListenSequentialPacket=/run/udev/control\n",
    "systemd-udevd-kernel.socket": "[Socket]\nListenNetlink=kobject-uevent 1\n",
    "systemd-rfkill.socket": "[Socket]\nListenSpecial=/dev/rfkill\n",
    "systemd-initctl.socket": "[Socket]\nListenFIFO=/run/initctl\n",
    "systemd-creds.socket": "[Socket]\nListenStream=/run/systemd/io.systemd.Credentials\n",
    "systemd-hostnamed.socket": "[Socket]\nListenStream=/run/systemd/io.systemd.Hostname\n",
}


def _network_surface(root: Path) -> None:
    """What the hardened image looks like once 05-no-listeners has run.

    No avahi, no bluez, no OpenSSH and no USB-network gadget in dpkg's status
    or on the filesystem; the units they would have brought masked; the
    Bluetooth rfkill state files pi-gen un-blocked put back to blocked; and
    the stock socket units, all of which listen on local addresses only.
    """
    units = root / "etc" / "systemd" / "system"
    for unit in MASKED_UNITS:
        (units / unit).symlink_to("/dev/null")
    # pi-gen's stage2/02-net-tweaks writes one of these per known on-board
    # Bluetooth address, containing 0 (unblocked). The stage rewrites them.
    rfkill = root / "var" / "lib" / "systemd" / "rfkill"
    rfkill.mkdir(parents=True)
    for addr in ("107d50c000.serial", "3f215040.serial", "20215040.serial",
                 "fe215040.serial", "soc"):
        (rfkill / f"platform-{addr}:bluetooth").write_text("1\n")
    # A Wi-Fi state file, which do_wifi_country sets to 0 and which must not be
    # confused with a Bluetooth one.
    (rfkill / "platform-soc:wlan").write_text("0\n")
    lib_units = root / "usr" / "lib" / "systemd" / "system"
    wants = lib_units / "sockets.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    for name, body in STOCK_SOCKETS.items():
        (lib_units / name).write_text(f"[Unit]\nDescription={name}\n{body}")
        (wants / name).symlink_to(f"/usr/lib/systemd/system/{name}")


def gate(root: Path, boot: Path):
    return subprocess.run(["bash", str(GATE), str(root), str(boot), str(REPO)],
                          capture_output=True, text=True, timeout=60)


def test_a_clean_image_passes(tmp_path):
    root, boot = clean_image(tmp_path)
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr
    assert "image-gate: all checks passed" in result.stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _add_package(r: Path, name: str, status: str) -> None:
    """Record a package in dpkg's status file.

    `deinstall ok config-files` is what a `remove` that should have been a
    `purge` leaves behind, stanza and Package: line intact.
    """
    status_file = r / "var/lib/dpkg/status"
    status_file.write_text(
        status_file.read_text() + f"\nPackage: {name}\nStatus: {status}\nVersion: 1.0\n")


def _enable_socket(r: Path, name: str, body: str) -> None:
    """Ship a socket unit under /usr/lib and enable it, as a package would."""
    lib_units = r / "usr/lib/systemd/system"
    (lib_units / name).write_text(f"[Unit]\nDescription={name}\n{body}")
    (lib_units / "sockets.target.wants" / name).symlink_to(f"/usr/lib/systemd/system/{name}")


def _symlink_scoreboard_state_dir(r: Path, b: Path) -> None:
    # find (default -P) does not descend into a starting point that is
    # itself a symlink, so replacing /var/lib/scoreboard with a symlink to a
    # real directory would hide an identity file inside it from the scan
    # that looks for exactly that, unless the gate refuses the symlink
    # outright.
    real = r / "var/lib/scoreboard-real"
    real.mkdir(parents=True)
    (real / "device.json").write_text("{}")
    shutil.rmtree(r / "var/lib/scoreboard")
    (r / "var/lib/scoreboard").symlink_to("scoreboard-real")


BREAKS = {
    "an identity file under /var/lib/scoreboard": (
        lambda r, b: _write(r / "var/lib/scoreboard/device.json", "{}"), "device.json"),
    "a leftover enrollment": (
        lambda r, b: _write(r / "var/lib/scoreboard/enrollment.json", "{}"), "enrollment.json"),
    "an authorized_keys file": (
        lambda r, b: _write(r / "home/pi/.ssh/authorized_keys", "ssh-ed25519 AAAA test\n"), "authorized_keys"),
    "a first user with a password": (
        lambda r, b: (r / "etc/shadow").write_text((r / "etc/shadow").read_text().replace("pi:!:", "pi:$6$salt$hash:")), "pi"),
    "a first user with an empty password": (
        lambda r, b: (r / "etc/shadow").write_text((r / "etc/shadow").read_text().replace("pi:!:", "pi::")), "pi"),
    "SSH enabled by unit": (
        lambda r, b: (r / "etc/systemd/system/multi-user.target.wants/ssh.service").symlink_to("/lib/systemd/system/ssh.service"), "SSH"),
    "SSH enabled from the boot partition": (
        lambda r, b: _write(b / "ssh", ""), "SSH"),
    "a saved Wi-Fi connection": (
        lambda r, b: _write(r / "etc/NetworkManager/system-connections/home.nmconnection", "[wifi-security]\npsk=hunter2\n"), "Wi-Fi"),
    "a Wi-Fi password in wpa_supplicant": (
        lambda r, b: _write(r / "etc/wpa_supplicant/wpa_supplicant.conf", "network={\n psk=hunter2\n}\n"), "Wi-Fi"),
    "a setup file on the boot partition": (
        lambda r, b: _write(b / "scoreboard-setup.txt", "ssid=x\npsk=y\n"), "Wi-Fi"),
    "a venv without system site packages": (
        lambda r, b: (r / "opt/scoreboard/.venv/pyvenv.cfg").write_text("include-system-site-packages = false\n"), "system site packages"),
    "a venv carrying its own pygame": (
        lambda r, b: (r / "opt/scoreboard/.venv/lib/python3.13/site-packages/pygame").mkdir(parents=True), "pygame"),
    "the scoreboard unit not enabled": (
        lambda r, b: (r / "etc/systemd/system/multi-user.target.wants/scoreboard.service").unlink(), "scoreboard.service"),
    "the network unit not enabled": (
        lambda r, b: (r / "etc/systemd/system/multi-user.target.wants/scoreboard-netcfg.service").unlink(), "scoreboard-netcfg.service"),
    "a polkit rule that differs from the repository's": (
        lambda r, b: _write(r / "etc/polkit-1/rules.d/10-scoreboard-network.rules", "polkit.addRule(function(){return polkit.Result.YES;});\n"), "polkit"),
    "no build identity": (
        lambda r, b: (r / "etc/scoreboard-build").unlink(), "scoreboard-build"),
    "a multi-line build identity": (
        lambda r, b: (r / "etc/scoreboard-build").write_text("v0.1.0\nextra\n"), "scoreboard-build"),
    "a private key under /etc": (
        lambda r, b: _write(r / "etc/ssl/private/ssl-cert-snakeoil.key", FAKE_KEY), "private key"),
    "a private key inside the venv": (
        lambda r, b: _write(r / "opt/scoreboard/.venv/lib/python3.13/site-packages/pkg/tests/key.pem", FAKE_KEY), "private key"),
    "an SSH host key": (
        lambda r, b: _write(r / "etc/ssh/ssh_host_ed25519_key", "binary-ish"), "host key"),
    "an extra certificate beside the CA": (
        lambda r, b: _write(r / "opt/scoreboard/certs/extra.crt", "cert"), "certificate"),
    "a CA that is not Amazon's": (
        lambda r, b: _write(r / "opt/scoreboard/certs/AmazonRootCA1.pem", "not the CA\n"), "AmazonRootCA1"),
    # Round 1 fixes: each of these bypassed tools/image-gate.sh before the fix.
    "a certificate with a .cer extension": (
        lambda r, b: _write(r / "opt/scoreboard/certs/evil.cer", "not a real cert\n"), "certificate"),
    "a certificate with an upper-case extension": (
        lambda r, b: _write(r / "opt/scoreboard/certs/EVIL.PEM", "not a real cert\n"), "certificate"),
    "a certificate symlink beside the CA": (
        lambda r, b: (r / "opt/scoreboard/certs/extra.pem").symlink_to("../data/x.txt"), "certificate"),
    "a certificate hiding behind an innocuous filename": (
        lambda r, b: _write(r / "opt/scoreboard/notes.txt",
                             "-----BEGIN CERTIFICATE-----\nnot a key\n-----END CERTIFICATE-----\n"), "certificate"),
    "the Amazon root CA shipped as a symlink": (
        lambda r, b: ((r / "opt/scoreboard/certs/AmazonRootCA1.pem").unlink(),
                       (r / "opt/scoreboard/certs/AmazonRootCA1.pem").symlink_to("/nonexistent/elsewhere.pem")),
        "AmazonRootCA1"),
    "a private key hidden by a NUL byte": (
        lambda r, b: _write_bytes(r / "etc/ssl/private/binary.key", b"\x00" + FAKE_KEY.encode()), "private key"),
    "a private key on the boot partition": (
        lambda r, b: _write(b / "backup.pem", FAKE_KEY), "private key"),
    "a two-line build identity without a trailing newline": (
        lambda r, b: (r / "etc/scoreboard-build").write_text("v0.1.0\nextra"), "scoreboard-build"),
    "an empty password field in /etc/passwd": (
        lambda r, b: (r / "etc/passwd").write_text((r / "etc/passwd").read_text().replace("pi:x:1000", "pi::1000")), "pi"),
    "a second account with uid 0": (
        lambda r, b: ((r / "etc/passwd").write_text((r / "etc/passwd").read_text() + "toor:x:0:0:toor:/root:/bin/bash\n"),
                       (r / "etc/shadow").write_text((r / "etc/shadow").read_text() + "toor:$6$salt$hash:20000:0:99999:7:::\n")),
        "toor"),
    "an authorized_keys2 file": (
        lambda r, b: _write(r / "home/pi/.ssh/authorized_keys2", "ssh-ed25519 AAAA test\n"), "authorized_keys"),
    "SSH enabled via an uncommon target": (
        lambda r, b: _write(r / "etc/systemd/system/graphical.target.wants/sshd.socket", ""), "SSH"),
    "the scoreboard unit enabled by a symlink to the wrong target": (
        lambda r, b: ((r / "etc/systemd/system/multi-user.target.wants/scoreboard.service").unlink(),
                       (r / "etc/systemd/system/multi-user.target.wants/scoreboard.service").symlink_to(
                           "/etc/systemd/system/other-service.service")),
        "scoreboard.service"),
    "a legacy ssh_host_key": (
        lambda r, b: _write(r / "etc/ssh/ssh_host_key", "binary-ish"), "host key"),
    "a saved Wi-Fi connection under /usr/lib/NetworkManager": (
        lambda r, b: _write(r / "usr/lib/NetworkManager/system-connections/home.nmconnection",
                             "[wifi-security]\npsk=hunter2\n"), "Wi-Fi"),
    "a Wi-Fi password via sae_password": (
        lambda r, b: _write(r / "etc/wpa_supplicant/wpa_supplicant.conf", "network={\n sae_password=hunter2\n}\n"), "Wi-Fi"),
    "a Wi-Fi password via a bare password key": (
        lambda r, b: _write(r / "etc/wpa_supplicant/wpa_supplicant.conf", "network={\n password=hunter2\n}\n"), "Wi-Fi"),
    "a Wi-Fi password via a WEP key": (
        lambda r, b: _write(r / "etc/wpa_supplicant/wpa_supplicant.conf", "network={\n wep_key0=hunter2\n}\n"), "Wi-Fi"),
    "a device certificate under /var/lib/scoreboard": (
        lambda r, b: _write(r / "var/lib/scoreboard/device.pem.crt", "cert\n"), "device.pem.crt"),
    "a device private key under /var/lib/scoreboard": (
        lambda r, b: _write(r / "var/lib/scoreboard/private.pem.key", "key\n"), "private.pem.key"),
    "a device certificate under /opt/scoreboard": (
        lambda r, b: _write(r / "opt/scoreboard/device.pem.crt", "cert\n"), "device.pem.crt"),
    "a device private key under /opt/scoreboard": (
        lambda r, b: _write(r / "opt/scoreboard/private.pem.key", "key\n"), "private.pem.key"),
    "an identity file under /opt/scoreboard": (
        lambda r, b: _write(r / "opt/scoreboard/device.json", "{}"), "device.json"),
    "the scoreboard unit file missing entirely": (
        lambda r, b: (r / "etc/systemd/system/scoreboard.service").unlink(), "scoreboard.service"),
    "the polkit rule missing entirely": (
        lambda r, b: (r / "etc/polkit-1/rules.d/10-scoreboard-network.rules").unlink(), "polkit"),
    "the distribution's pygame not installed": (
        lambda r, b: shutil.rmtree(r / "usr/lib/python3/dist-packages/pygame"), "pygame"),
    # Round 2 fixes: each of these bypassed tools/image-gate.sh before the fix.
    "SSH enabled by an uncommon target": (
        lambda r, b: _write(r / "etc/systemd/system/graphical.target.wants/ssh.service", ""), "SSH"),
    "var/lib/scoreboard replaced by a symlink hiding an identity file": (
        _symlink_scoreboard_state_dir, "symlink"),
    "a certificate hiding behind a TRUSTED CERTIFICATE header": (
        lambda r, b: _write(r / "opt/scoreboard/trusted-notes.txt",
                             "-----BEGIN TRUSTED CERTIFICATE-----\nnot a key\n-----END TRUSTED CERTIFICATE-----\n"),
        "certificate"),
    "a system account with a usable password": (
        lambda r, b: (r / "etc/shadow").write_text(
            (r / "etc/shadow").read_text().replace("scoreboard:!:", "scoreboard:$6$salt$hash:")),
        "scoreboard"),
    # Final fix wave: each of these bypassed tools/image-gate.sh before the fix.
    "cloud-init user-data on the boot partition": (
        lambda r, b: _write(b / "user-data", "#cloud-config\nssh_pwauth: true\n"), "cloud-init"),
    "cloud-init network-config on the boot partition": (
        lambda r, b: _write(b / "network-config", "network:\n  version: 2\n"), "cloud-init"),
    "cloud-init meta-data on the boot partition": (
        lambda r, b: _write(b / "meta-data", "instance_id: rpios-image\n"), "cloud-init"),
    "cloud-init's configuration in the rootfs": (
        lambda r, b: _write(r / "etc/cloud/cloud.cfg", "users: [default]\n"), "cloud-init"),
    "the cloud-init program in the rootfs": (
        lambda r, b: _write(r / "usr/bin/cloud-init", "#!/usr/bin/python3\n"), "cloud-init"),
    "cloud-init recorded as a package by dpkg": (
        lambda r, b: (r / "var/lib/dpkg/status").write_text(
            (r / "var/lib/dpkg/status").read_text() + "\nPackage: cloud-init\nStatus: install ok installed\n"),
        "cloud-init"),
    "rpi-cloud-init-mods recorded as a package by dpkg": (
        lambda r, b: (r / "var/lib/dpkg/status").write_text(
            (r / "var/lib/dpkg/status").read_text() + "\nPackage: rpi-cloud-init-mods\nStatus: install ok installed\n"),
        "cloud-init"),
    "no dpkg status to check packages against": (
        lambda r, b: (r / "var/lib/dpkg/status").unlink(), "dpkg"),
    "a pip cache left in root's home": (
        lambda r, b: _write(r / "root/.cache/pip/http-v2/0/entry", "cached wheel"), "pip cache"),
    "a PyPI package installed into the venv": (
        lambda r, b: (r / "opt/scoreboard/.venv/lib/python3.13/site-packages/paho_mqtt-2.1.0.dist-info").mkdir(), "PyPI"),
    "the distribution's paho-mqtt not installed": (
        lambda r, b: shutil.rmtree(r / "usr/lib/python3/dist-packages/paho"), "paho"),
    # A directory named "x<newline>certs" splits find's output into two
    # lines, the second reading as the one allowed certificate path.
    "a foreign CA smuggled behind a newline in a directory name": (
        lambda r, b: _write(r / "opt/scoreboard/x\ncerts/AmazonRootCA1.pem", "not the CA\n"), "newline"),
    "a newline in a path under /etc": (
        lambda r, b: _write(r / "etc/odd\nname", ""), "newline"),
    "a newline in a path on the boot partition": (
        lambda r, b: _write(b / "odd\nname", ""), "newline"),
    "SSH enabled through a requires directory": (
        lambda r, b: _write(r / "etc/systemd/system/multi-user.target.requires/ssh.service", ""), "SSH"),
    "SSH enabled through an upholds directory": (
        lambda r, b: _write(r / "etc/systemd/system/multi-user.target.upholds/sshd.socket", ""), "SSH"),
    # Round 3 fix: the rule above was narrowed to stop matching documentation
    # (the man page and doc file are now in the clean fixture); these prove
    # the narrowing did not also stop matching real key locations.
    "an authorized_keys file under root's .ssh": (
        lambda r, b: _write(r / "root/.ssh/authorized_keys", "ssh-ed25519 AAAA test\n"), "authorized_keys"),
    "an authorized_keys2 file under root's .ssh": (
        lambda r, b: _write(r / "root/.ssh/authorized_keys2", "ssh-ed25519 AAAA test\n"), "authorized_keys"),
    "an authorized_keys file under a .ssh directory that is neither home nor root": (
        lambda r, b: _write(r / "srv/app/.ssh/authorized_keys", "ssh-ed25519 AAAA test\n"), "authorized_keys"),
    "an authorized_keys2 file under a .ssh directory that is neither home nor root": (
        lambda r, b: _write(r / "srv/app/.ssh/authorized_keys2", "ssh-ed25519 AAAA test\n"), "authorized_keys"),
    "authorized_keys directly under /etc/ssh": (
        lambda r, b: _write(r / "etc/ssh/authorized_keys", "ssh-ed25519 AAAA test\n"), "authorized_keys"),
    "authorized_keys2 directly under /etc/ssh": (
        lambda r, b: _write(r / "etc/ssh/authorized_keys2", "ssh-ed25519 AAAA test\n"), "authorized_keys"),
    "a per-user authorized_keys file under /etc/ssh": (
        lambda r, b: _write(r / "etc/ssh/authorized_keys/pi", "ssh-ed25519 AAAA test\n"), "authorized_keys"),
    # A per-user path under /etc/ssh (like the fixture above) is a token the
    # narrowed scan already covers, so it must pass, not fail -- these are
    # tokens the scan does NOT cover: an absolute path elsewhere entirely.
    "an AuthorizedKeysFile token naming a path outside /etc/ssh": (
        lambda r, b: _write(r / "etc/ssh/sshd_config",
                             "AuthorizedKeysFile /var/lib/ssh-keys/%u\n"), "/var/lib/ssh-keys/%u"),
    "an AuthorizedKeysFile token naming a path outside /etc/ssh, in sshd_config.d": (
        lambda r, b: _write(r / "etc/ssh/sshd_config.d/50-custom.conf",
                             "AuthorizedKeysFile .ssh/authorized_keys /etc/foo/%u\n"), "/etc/foo/%u"),
    "AuthorizedKeysFile repeated in sshd_config": (
        lambda r, b: _write(r / "etc/ssh/sshd_config",
                             "AuthorizedKeysFile .ssh/authorized_keys\n"
                             "AuthorizedKeysFile .ssh/authorized_keys2\n"), "AuthorizedKeysFile appears"),
    "an AuthorizedKeysCommand in sshd_config": (
        lambda r, b: _write(r / "etc/ssh/sshd_config",
                             "AuthorizedKeysCommand /usr/local/bin/fetch-keys %u\n"
                             "AuthorizedKeysCommandUser nobody\n"), "AuthorizedKeysCommand"),
    "AuthorizedKeysCommand set to a plain command": (
        lambda r, b: _write(r / "etc/ssh/sshd_config", "AuthorizedKeysCommand /usr/bin/whatever\n"),
        "AuthorizedKeysCommand"),
    # Round 4: v0.1.0 booted to the first-boot user-creation wizard on real
    # hardware (docs/hardware-checks.md, H5) and the gate saw nothing wrong
    # with it.
    "the first-boot user-creation wizard enabled": (
        lambda r, b: (r / "etc/systemd/system/multi-user.target.wants/userconfig.service").symlink_to(
            "/usr/lib/systemd/system/userconfig.service"), "wizard"),
    "the first-boot user-creation wizard enabled through a requires directory": (
        lambda r, b: _write(r / "etc/systemd/system/multi-user.target.requires/userconfig.service", ""), "wizard"),
    "the first-boot user-creation wizard enabled through an upholds directory": (
        lambda r, b: _write(r / "etc/systemd/system/graphical.target.upholds/userconfig.service", ""), "wizard"),
    "the first-boot user-creation wizard enabled from /usr/lib": (
        lambda r, b: _write(r / "usr/lib/systemd/system/multi-user.target.wants/userconfig.service", ""), "wizard"),
    "a console autologin drop-in": (
        lambda r, b: _write(r / "etc/systemd/system/getty@tty1.service.d/autologin.conf",
                            "[Service]\nExecStart=\n"
                            "ExecStart=-/sbin/agetty --autologin pi --noclear %I $TERM\n"), "autologin"),
    "a serial console autologin drop-in": (
        lambda r, b: _write(r / "etc/systemd/system/serial-getty@ttyAMA0.service.d/autologin.conf",
                            "[Service]\nExecStart=\n"
                            "ExecStart=-/sbin/agetty --autologin root %I $TERM\n"), "autologin"),
    "a console autologin drop-in using agetty's short flag": (
        lambda r, b: _write(r / "etc/systemd/system/getty@tty1.service.d/50-auto.conf",
                            "[Service]\nExecStart=\n"
                            "ExecStart=-/sbin/agetty -a pi --noclear %I $TERM\n"), "autologin"),
    "a console autologin drop-in shipped under /usr/lib": (
        lambda r, b: _write(r / "usr/lib/systemd/system/getty@tty1.service.d/autologin.conf",
                            "[Service]\nExecStart=\n"
                            "ExecStart=-/sbin/agetty --autologin pi %I $TERM\n"), "autologin"),
    # Raspberry Pi Connect: pi-gen's stage2 installs rpi-connect-lite, and the
    # scoreboard stage purges it. The dpkg rule comes first because a `remove`
    # that should have been a `purge` leaves the stanza behind.
    "Raspberry Pi Connect recorded as a package by dpkg": (
        lambda r, b: (r / "var/lib/dpkg/status").write_text(
            (r / "var/lib/dpkg/status").read_text()
            + "\nPackage: rpi-connect-lite\nStatus: install ok installed\nVersion: 2.12.2\n"),
        "Raspberry Pi Connect"),
    "the full Raspberry Pi Connect recorded as a package by dpkg": (
        lambda r, b: (r / "var/lib/dpkg/status").write_text(
            (r / "var/lib/dpkg/status").read_text()
            + "\nPackage: rpi-connect\nStatus: install ok installed\nVersion: 2.12.2\n"),
        "Raspberry Pi Connect"),
    "Raspberry Pi Connect removed but not purged": (
        lambda r, b: (r / "var/lib/dpkg/status").write_text(
            (r / "var/lib/dpkg/status").read_text()
            + "\nPackage: rpi-connect-lite\nStatus: deinstall ok config-files\nVersion: 2.12.2\n"),
        "Raspberry Pi Connect"),
    "the Raspberry Pi Connect agent binary in the rootfs": (
        lambda r, b: _write(r / "usr/bin/rpi-connectd", "#!/bin/sh\n"), "Raspberry Pi Connect"),
    "the Raspberry Pi Connect command in the rootfs": (
        lambda r, b: _write(r / "usr/bin/rpi-connect", "#!/bin/sh\n"), "Raspberry Pi Connect"),
    "a Raspberry Pi Connect user unit in the rootfs": (
        lambda r, b: _write(r / "usr/lib/systemd/user/rpi-connect.service", "[Unit]\n"),
        "Raspberry Pi Connect"),
    "a Raspberry Pi Connect sign-in path unit in the rootfs": (
        lambda r, b: _write(r / "usr/lib/systemd/user/rpi-connect-signin.path", "[Path]\n"),
        "Raspberry Pi Connect"),
    # Fix round 1. The mask is the control, so its absence is a finding on its
    # own: an unmask WITHOUT a re-enable leaves the unit live for the next
    # thing that enables it, and every other wizard rule still passes.
    "the wizard's mask missing entirely": (
        lambda r, b: (r / "etc/systemd/system/userconfig.service").unlink(), "not masked"),
    "the wizard's mask replaced by a regular file": (
        lambda r, b: ((r / "etc/systemd/system/userconfig.service").unlink(),
                      _write(r / "etc/systemd/system/userconfig.service", "[Unit]\n")), "not masked"),
    "the wizard's mask pointing somewhere other than /dev/null": (
        lambda r, b: ((r / "etc/systemd/system/userconfig.service").unlink(),
                      (r / "etc/systemd/system/userconfig.service").symlink_to(
                          "/usr/lib/systemd/system/userconfig.service")), "not masked"),
    # Autologin scan gaps found in review.
    "an autologin drop-in that is a symlink to a file": (
        lambda r, b: (_write(r / "etc/elsewhere.conf",
                             "[Service]\nExecStart=-/sbin/agetty --autologin pi %I $TERM\n"),
                      (r / "etc/systemd/system/getty@tty1.service.d/50-link.conf").symlink_to(
                          "../../../elsewhere.conf")), "autologin"),
    "a getty drop-in directory that is itself a symlink": (
        lambda r, b: ((r / "etc/real-dropins").mkdir(),
                      _write(r / "etc/real-dropins/autologin.conf",
                             "[Service]\nExecStart=-/sbin/agetty --autologin pi %I $TERM\n"),
                      (r / "etc/systemd/system/getty@tty2.service.d").symlink_to("../../real-dropins")),
        "symlink"),
    "an autologin drop-in using agetty's attached short flag": (
        lambda r, b: _write(r / "etc/systemd/system/getty@tty1.service.d/60-attached.conf",
                            "[Service]\nExecStart=\nExecStart=-/sbin/agetty -api --noclear %I $TERM\n"),
        "autologin"),
    "an autologin drop-in on autovt@": (
        lambda r, b: _write(r / "etc/systemd/system/autovt@tty3.service.d/autologin.conf",
                            "[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin pi %I $TERM\n"),
        "autologin"),
    "an autologin drop-in on console-getty": (
        lambda r, b: _write(r / "etc/systemd/system/console-getty.service.d/autologin.conf",
                            "[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin root - $TERM\n"),
        "autologin"),
    "a full getty unit override configuring autologin": (
        lambda r, b: _write(r / "etc/systemd/system/getty@tty1.service",
                            "[Service]\nExecStart=-/sbin/agetty --autologin pi --noclear %I $TERM\n"),
        "autologin"),
    "a full console-getty unit override configuring autologin": (
        lambda r, b: _write(r / "etc/systemd/system/console-getty.service",
                            "[Service]\nExecStart=-/sbin/agetty -aroot - $TERM\n"),
        "autologin"),
    # The journal is the last diagnosis surface; a volatile one leaves nothing
    # on the card when a panel fails to start.
    "the persistent-journal drop-in missing": (
        lambda r, b: (r / "etc/systemd/journald.conf.d/95-scoreboard-persistent-journal.conf").unlink(),
        "journal is not persistent"),
    "a later-sorting drop-in putting the journal back in RAM": (
        lambda r, b: _write(r / "etc/systemd/journald.conf.d/99-volatile-again.conf",
                            "[Journal]\nStorage=volatile\n"), "journal is not persistent"),
    "the journal storage set to none": (
        lambda r, b: (r / "etc/systemd/journald.conf.d/95-scoreboard-persistent-journal.conf").write_text(
            "[Journal]\nStorage=none\n"), "journal is not persistent"),
    "/var/log/journal missing": (
        lambda r, b: (r / "var/log/journal").rmdir(), "nowhere on the card"),
    # Fix round 2: the replay must match journald, not a simplification of it.
    "a volatile drop-in in /usr/local/lib that sorts last": (
        lambda r, b: _write(r / "usr/local/lib/systemd/journald.conf.d/99-local.conf",
                            "[Journal]\nStorage=volatile\n"), "journal is not persistent"),
    "Storage=auto with no /var/log/journal": (
        lambda r, b: ((r / "etc/systemd/journald.conf.d/95-scoreboard-persistent-journal.conf").write_text(
                          "[Journal]\nStorage=auto\nSyncIntervalSec=30s\n"),
                      (r / "var/log/journal").rmdir()), "stays in RAM"),
    "the main journald.conf turning storage off with no drop-in to fix it": (
        lambda r, b: ((r / "etc/systemd/journald.conf").write_text("[Journal]\nStorage=none\n"),
                      (r / "etc/systemd/journald.conf.d/95-scoreboard-persistent-journal.conf").write_text(
                          "[Journal]\nSyncIntervalSec=30s\n")), "journal is not persistent"),
    "the sync interval left at journald's five-minute default": (
        lambda r, b: (r / "etc/systemd/journald.conf.d/95-scoreboard-persistent-journal.conf").write_text(
            "[Journal]\nStorage=persistent\nSystemMaxUse=50M\n"), "SyncIntervalSec"),
    "a later drop-in relaxing the sync interval": (
        lambda r, b: _write(r / "etc/systemd/journald.conf.d/99-slow-sync.conf",
                            "[Journal]\nSyncIntervalSec=5min\n"), "SyncIntervalSec"),
    # The display path. v0.1.1 passed every rule above and still showed a
    # black screen, because none of these files was in the image
    # (docs/hardware-checks.md, H5). One break per file, because each is a
    # separate link in one chain: SDL cannot reach the vendor library without
    # the dispatcher, the dispatcher cannot find the vendor library without
    # the JSON, and libgbm cannot reach libgallium without its backend.
    "the EGL dispatcher missing": (
        lambda r, b: ((r / ARCH_LIB / "libEGL.so.1").unlink(),
                      (r / ARCH_LIB / "libEGL.so.1.1.0").unlink()), "libEGL.so.1"),
    "the EGL dispatcher left as a symlink to nothing": (
        lambda r, b: (r / ARCH_LIB / "libEGL.so.1.1.0").unlink(), "libEGL.so.1"),
    "a display library symlinked to a host path that is not in the image": (
        # /bin/sh exists on the build machine but not in this rootfs. Only a
        # check that resolves the link INSIDE the image root catches it; one
        # that lets the host resolve it reads the image as fine.
        lambda r, b: ((r / ARCH_LIB / "libEGL.so.1").unlink(),
                      (r / ARCH_LIB / "libEGL.so.1").symlink_to("/bin/sh")), "libEGL.so.1"),
    "Mesa's EGL vendor library missing": (
        lambda r, b: ((r / ARCH_LIB / "libEGL_mesa.so.0").unlink(),
                      (r / ARCH_LIB / "libEGL_mesa.so.0.0.0").unlink()), "libEGL_mesa.so.0"),
    "the glvnd EGL vendor file missing": (
        lambda r, b: (r / "usr/share/glvnd/egl_vendor.d/50_mesa.json").unlink(), "50_mesa.json"),
    "the GLES2 library missing": (
        lambda r, b: ((r / ARCH_LIB / "libGLESv2.so.2").unlink(),
                      (r / ARCH_LIB / "libGLESv2.so.2.1.0").unlink()), "libGLESv2.so.2"),
    "libgbm missing": (
        lambda r, b: ((r / ARCH_LIB / "libgbm.so.1").unlink(),
                      (r / ARCH_LIB / "libgbm.so.1.0.0").unlink()), "libgbm.so.1"),
    "the GBM backend missing": (
        lambda r, b: (r / ARCH_LIB / "gbm/dri_gbm.so").unlink(), "dri_gbm.so"),
    # --- Network surface, 2026-09-19 -------------------------------------
    # The survey that led to 05-no-listeners found avahi answering mDNS on
    # every interface and the Bluetooth radio deliberately un-blocked, on an
    # image that passed every rule above.
    "avahi recorded as a package by dpkg": (
        lambda r, b: _add_package(r, "avahi-daemon", "install ok installed"), "network surface"),
    "avahi removed but not purged": (
        lambda r, b: _add_package(r, "avahi-daemon", "deinstall ok config-files"), "network surface"),
    "the mDNS NSS module recorded as a package by dpkg": (
        lambda r, b: _add_package(r, "libnss-mdns", "install ok installed"), "network surface"),
    "bluez recorded as a package by dpkg": (
        lambda r, b: _add_package(r, "bluez", "install ok installed"), "network surface"),
    "the Bluetooth firmware recorded as a package by dpkg": (
        lambda r, b: _add_package(r, "bluez-firmware", "install ok installed"), "network surface"),
    "the USB network gadget recorded as a package by dpkg": (
        lambda r, b: _add_package(r, "rpi-usb-gadget", "install ok installed"), "network surface"),
    "ssh-import-id recorded as a package by dpkg": (
        lambda r, b: _add_package(r, "ssh-import-id", "install ok installed"), "network surface"),
    "rpi-update recorded as a package by dpkg": (
        lambda r, b: _add_package(r, "rpi-update", "install ok installed"), "network surface"),
    "openssh-server recorded as a package by dpkg": (
        lambda r, b: _add_package(r, "openssh-server", "install ok installed"), "network surface"),
    "openssh-client recorded as a package by dpkg": (
        lambda r, b: _add_package(r, "openssh-client", "install ok installed"), "network surface"),
    "the ssh metapackage recorded as a package by dpkg": (
        lambda r, b: _add_package(r, "ssh", "install ok installed"), "network surface"),
    "the avahi daemon binary in the rootfs": (
        lambda r, b: _write(r / "usr/sbin/avahi-daemon", "#!/bin/sh\n"), "usr/sbin/avahi-daemon"),
    "avahi's configuration in the rootfs": (
        lambda r, b: _write(r / "etc/avahi/avahi-daemon.conf", "[server]\n"), "etc/avahi/avahi-daemon.conf"),
    "the Bluetooth daemon in the rootfs": (
        lambda r, b: _write(r / "usr/libexec/bluetooth/bluetoothd", "#!/bin/sh\n"), "bluetoothd"),
    "the Bluetooth D-Bus activation file in the rootfs": (
        lambda r, b: _write(r / "usr/share/dbus-1/system-services/org.bluez.service", "[D-BUS Service]\n"),
        "org.bluez.service"),
    "the USB gadget unit in the rootfs": (
        lambda r, b: _write(r / "usr/lib/systemd/system/rpi-usb-gadget-ics.service", "[Unit]\n"),
        "rpi-usb-gadget-ics.service"),
    "the ssh-import-id command in the rootfs": (
        lambda r, b: _write(r / "usr/bin/ssh-import-id-gh", "#!/bin/sh\n"), "usr/bin/ssh-import-id-gh"),
    "the rpi-update command in the rootfs": (
        lambda r, b: _write(r / "usr/bin/rpi-update", "#!/bin/sh\n"), "usr/bin/rpi-update"),
    "the sshd binary in the rootfs": (
        lambda r, b: _write(r / "usr/sbin/sshd", "#!/bin/sh\n"), "usr/sbin/sshd"),
    "the sshd session helper in the rootfs": (
        lambda r, b: _write(r / "usr/lib/openssh/sshd-session", "#!/bin/sh\n"), "usr/lib/openssh/sshd-session"),
    "ssh-keygen in the rootfs": (
        lambda r, b: _write(r / "usr/bin/ssh-keygen", "#!/bin/sh\n"), "usr/bin/ssh-keygen"),
    "avahi enabled through a wants directory": (
        lambda r, b: _write(r / "etc/systemd/system/multi-user.target.wants/avahi-daemon.service", ""),
        "must not run is enabled"),
    "avahi's socket enabled through sockets.target": (
        lambda r, b: _write(r / "usr/lib/systemd/system/sockets.target.wants/avahi-daemon.socket", ""),
        "must not run is enabled"),
    "bluetooth enabled through a requires directory": (
        lambda r, b: _write(r / "etc/systemd/system/bluetooth.target.requires/bluetooth.service", ""),
        "must not run is enabled"),
    "the Bluetooth UART attach unit enabled from /usr/lib": (
        lambda r, b: _write(r / "usr/lib/systemd/system/multi-user.target.wants/hciuart.service", ""),
        "must not run is enabled"),
    "the USB network gadget enabled through an upholds directory": (
        lambda r, b: _write(r / "etc/systemd/system/multi-user.target.upholds/rpi-usb-gadget-ics.service", ""),
        "must not run is enabled"),
    "a mask replaced by a regular unit file": (
        lambda r, b: ((r / "etc/systemd/system/avahi-daemon.service").unlink(),
                      _write(r / "etc/systemd/system/avahi-daemon.service", "[Unit]\n")), "not masked"),
    "a mask pointing at the real unit instead of /dev/null": (
        lambda r, b: ((r / "etc/systemd/system/bluetooth.service").unlink(),
                      (r / "etc/systemd/system/bluetooth.service").symlink_to(
                          "/usr/lib/systemd/system/bluetooth.service")), "not masked"),
    "Bluetooth left on in config.txt": (
        lambda r, b: (b / "config.txt").write_text("dtparam=audio=on\n"), "disable-bt"),
    "the Bluetooth overlay commented out": (
        lambda r, b: (b / "config.txt").write_text("dtparam=audio=on\n#dtoverlay=disable-bt\n"), "disable-bt"),
    "no config.txt at all": (
        lambda r, b: (b / "config.txt").unlink(), "config.txt"),
    "a Bluetooth rfkill state file still un-blocking the radio": (
        lambda r, b: (r / "var/lib/systemd/rfkill/platform-fe215040.serial:bluetooth").write_text("0\n"),
        "un-blocks the Bluetooth radio"),
    "an sshd socket armed from the kernel command line": (
        lambda r, b: (b / "cmdline.txt").write_text(
            (b / "cmdline.txt").read_text().rstrip("\n") + " systemd.ssh_listen=0.0.0.0:22\n"),
        "systemd.ssh_listen"),
    # The general socket rule. None of these names a daemon, which is the
    # point: it is the one rule here that would catch a listener nobody
    # thought to add by name.
    "an enabled socket unit listening on a bare port": (
        lambda r, b: _enable_socket(r, "mystery.socket", "[Socket]\nListenStream=8080\n"),
        "is not a local address"),
    "an enabled socket unit listening on every address": (
        lambda r, b: _enable_socket(r, "mystery.socket", "[Socket]\nListenStream=0.0.0.0:22\n"),
        "is not a local address"),
    "an enabled socket unit listening on every IPv6 address": (
        lambda r, b: _enable_socket(r, "mystery.socket", "[Socket]\nListenDatagram=[::]:5353\n"),
        "is not a local address"),
    "a socket unit dropped straight into /etc/systemd/system": (
        lambda r, b: _write(r / "etc/systemd/system/mystery.socket", "[Socket]\nListenStream=9000\n"),
        "is not a local address"),
    "a drop-in adding a network listener to a stock socket": (
        lambda r, b: _write(r / "etc/systemd/system/dbus.socket.d/50-extra.conf",
                            "[Socket]\nListenStream=1234\n"), "is not a local address"),
}


@pytest.mark.parametrize("name", sorted(BREAKS))
def test_each_assertion_can_fail(tmp_path, name):
    root, boot = clean_image(tmp_path)
    breaker, expected = BREAKS[name]
    breaker(root, boot)
    result = gate(root, boot)
    assert result.returncode == 1, f"{name}: gate passed a broken image\n{result.stdout}"
    assert "image-gate: FAIL:" in result.stderr
    assert expected in result.stderr, f"{name}: {result.stderr}"


@pytest.mark.parametrize("unit", MASKED_UNITS)
def test_each_mask_is_asserted_on_its_own(tmp_path, unit):
    # The mask is the control, so its absence is a finding by itself: a purge
    # that is later undone leaves the unit present, unmasked and unenabled --
    # which passes every other rule, boots perfectly, and is armed for the
    # next thing that enables it.
    root, boot = clean_image(tmp_path)
    (root / "etc/systemd/system" / unit).unlink()
    result = gate(root, boot)
    assert result.returncode == 1, f"{unit}: gate passed an unmasked unit\n{result.stdout}"
    assert "not masked" in result.stderr, result.stderr
    assert unit in result.stderr, result.stderr


def test_openssh_documentation_from_another_package_is_not_a_credential(tmp_path):
    # OpenSSH is purged now, so its own manual page is gone with it -- but
    # documentation naming authorized_keys can arrive from anywhere, and a
    # scan that cannot tell a program from its documentation is the bug that
    # failed a real build (GitHub Actions run 35298398347). The narrowed rule
    # must still wave these through.
    root, boot = clean_image(tmp_path)
    _write_bytes(root / "usr/share/man/man5/authorized_keys.5.gz",
                 b"not really gzipped; the gate only looks at the name")
    _write(root / "usr/share/doc/some-package/authorized_keys.example",
           "ssh-ed25519 AAAAexample this is documentation, not a real key\n")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr


def test_the_serial_getty_mask_does_not_trip_the_autologin_scan(tmp_path):
    # The mask is a symlink named serial-getty@.service sitting directly in
    # /etc/systemd/system -- exactly where the autologin scan looks for a
    # replacement getty unit, and the glob serial-getty@*.service matches the
    # template's name. It must not fail the gate: that scan is -xtype f and
    # the mask points at a character device. The clean fixture already carries
    # the pair; this says out loud that it is deliberate, so a future change
    # from -xtype f to -type l is not made by accident.
    root, boot = clean_image(tmp_path)
    assert (root / "etc/systemd/system/serial-getty@.service").is_symlink()
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr
    # And a real autologin drop-in on the serial console still fails, mask or
    # no mask -- the kernel console is kept, so that scan still has a job.
    _write(root / "etc/systemd/system/serial-getty@ttyAMA0.service.d/autologin.conf",
           "[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin root %I $TERM\n")
    broken = gate(root, boot)
    assert broken.returncode == 1, broken.stdout
    assert "autologin" in broken.stderr


def test_a_socket_listening_only_on_loopback_passes(tmp_path):
    # A daemon that binds 127.0.0.1 is reachable from nowhere but the panel
    # itself, so the rule must not reject it and send someone looking for a
    # listener that does not exist.
    root, boot = clean_image(tmp_path)
    _enable_socket(root, "local-only.socket", "[Socket]\nListenStream=127.0.0.1:9000\n")
    _enable_socket(root, "local-only6.socket", "[Socket]\nListenStream=[::1]:9001\n")
    _enable_socket(root, "runtime-path.socket", "[Socket]\nListenStream=%t/something.sock\n")
    _enable_socket(root, "abstract.socket", "[Socket]\nListenStream=@an-abstract-name\n")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr


def test_a_masked_socket_unit_is_not_read_as_a_listener(tmp_path):
    # An enablement symlink left behind for a unit that is masked starts
    # nothing, so the rule must resolve the mask rather than read the unit it
    # would otherwise have pointed at. Without this, the image's own
    # avahi-daemon.socket mask could fail the build.
    root, boot = clean_image(tmp_path)
    _enable_socket(root, "listening.socket", "[Socket]\nListenStream=4444\n")
    (root / "etc/systemd/system/listening.socket").symlink_to("/dev/null")
    (root / "usr/lib/systemd/system/sockets.target.wants/listening.socket").unlink()
    (root / "usr/lib/systemd/system/sockets.target.wants/listening.socket").symlink_to("/dev/null")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr


def test_an_empty_listen_assignment_resets_rather_than_listens(tmp_path):
    # "ListenStream=" with no value clears the list; it is how a drop-in takes
    # a listener away, and reading it as an address would reject the fix.
    root, boot = clean_image(tmp_path)
    _write(root / "etc/systemd/system/dbus.socket.d/50-reset.conf",
           "[Socket]\nListenStream=\n")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr


def test_a_directory_that_is_not_a_rootfs_is_refused(tmp_path):
    (tmp_path / "boot").mkdir()
    result = gate(tmp_path / "empty", tmp_path / "boot")
    assert result.returncode == 1
    assert "root filesystem" in result.stderr


def test_a_build_identity_without_a_trailing_newline_still_passes(tmp_path):
    # wc -l undercounts a file missing its trailing newline; the gate must
    # not punish a single-line build identity for lacking one.
    root, boot = clean_image(tmp_path)
    (root / "etc/scoreboard-build").write_text("v0.1.0")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr
    assert "image-gate: all checks passed" in result.stdout


def test_a_blank_line_in_passwd_is_skipped_not_flagged(tmp_path):
    # A blank line has no name field; it is not an account, so it must not
    # produce a confusing "account ''" failure -- or any failure at all.
    root, boot = clean_image(tmp_path)
    (root / "etc/passwd").write_text((root / "etc/passwd").read_text() + "\n")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr
    assert "image-gate: all checks passed" in result.stdout


def test_sshd_config_with_the_stock_default_authorized_keys_file_passes(tmp_path):
    # Written out in full, exactly as Debian's own sshd_config sometimes
    # leaves it (uncommented), this must not trip the hole-closing check.
    root, boot = clean_image(tmp_path)
    _write(root / "etc/ssh/sshd_config", "AuthorizedKeysFile\t.ssh/authorized_keys .ssh/authorized_keys2\n")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr
    assert "image-gate: all checks passed" in result.stdout


def test_sshd_config_with_the_default_commented_out_passes(tmp_path):
    # Debian ships this line commented out more often than not; a comment
    # is not an override and must not fail the gate.
    root, boot = clean_image(tmp_path)
    _write(root / "etc/ssh/sshd_config", "#AuthorizedKeysFile\t.ssh/authorized_keys .ssh/authorized_keys2\n")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr
    assert "image-gate: all checks passed" in result.stdout


def test_sshd_config_d_with_the_stock_default_passes(tmp_path):
    root, boot = clean_image(tmp_path)
    _write(root / "etc/ssh/sshd_config.d/50-scoreboard.conf",
           "AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2\n")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr
    assert "image-gate: all checks passed" in result.stdout


def test_sshd_config_with_home_relative_authorized_keys_file_passes(tmp_path):
    # %h expands to the target user's home directory; a token that reduces to
    # .ssh/authorized_keys after stripping it is exactly what the plain
    # .ssh/authorized_keys form already means, so it must pass too.
    root, boot = clean_image(tmp_path)
    _write(root / "etc/ssh/sshd_config", "AuthorizedKeysFile %h/.ssh/authorized_keys\n")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr
    assert "image-gate: all checks passed" in result.stdout


def test_sshd_config_with_multiple_conf_d_files_each_with_the_default_passes(tmp_path):
    # Several sshd_config.d fragments, each spelling only the stock default,
    # must all pass together -- the per-file repeat check must not confuse
    # "the same directive lives in two files" with "the same directive
    # appears twice in one file".
    root, boot = clean_image(tmp_path)
    _write(root / "etc/ssh/sshd_config.d/40-first.conf", "AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2\n")
    _write(root / "etc/ssh/sshd_config.d/50-second.conf", "AuthorizedKeysFile\t.ssh/authorized_keys\t.ssh/authorized_keys2\n")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr
    assert "image-gate: all checks passed" in result.stdout


def test_storage_auto_with_the_journal_directory_present_passes(tmp_path):
    # Storage=auto means "persistent if /var/log/journal exists", so it is as
    # good as persistent here and must not be refused.
    root, boot = clean_image(tmp_path)
    (root / "etc/systemd/journald.conf.d/95-scoreboard-persistent-journal.conf").write_text(
        "[Journal]\nStorage=auto\nSyncIntervalSec=30s\n")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr


def test_a_vendor_drop_in_shadowed_by_an_etc_file_of_the_same_name_does_not_count(tmp_path):
    # systemd reads only the highest-priority file of a given name -- it does
    # not read both -- so an /etc file named 40-rpi-volatile-storage.conf
    # replaces the vendor one outright. With the vendor's Storage=volatile
    # gone, the default (auto) plus the directory is persistent, and the gate
    # must not still be counting the file systemd never read.
    root, boot = clean_image(tmp_path)
    (root / "etc/systemd/journald.conf.d/95-scoreboard-persistent-journal.conf").unlink()
    _write(root / "etc/systemd/journald.conf.d/40-rpi-volatile-storage.conf",
           "[Journal]\nSyncIntervalSec=30s\n")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr


def test_a_vendor_drop_in_disabled_by_a_dev_null_symlink_does_not_count(tmp_path):
    # Symlinking a drop-in name to /dev/null is the documented way to disable a
    # vendor drop-in: that name then contributes nothing at all, rather than
    # falling through to the vendor file it shadows.
    root, boot = clean_image(tmp_path)
    (root / "etc/systemd/journald.conf.d/95-scoreboard-persistent-journal.conf").unlink()
    (root / "etc/systemd/journald.conf.d/40-rpi-volatile-storage.conf").symlink_to("/dev/null")
    _write(root / "etc/systemd/journald.conf.d/96-scoreboard-sync.conf",
           "[Journal]\nSyncIntervalSec=30s\n")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr


def test_a_relative_mask_symlink_is_still_a_mask(tmp_path):
    # ../../../dev/null resolves to /dev/null and systemd treats it as a mask,
    # so the gate must judge the resolved target, not the literal string.
    root, boot = clean_image(tmp_path)
    (root / "etc/systemd/system/userconfig.service").unlink()
    (root / "etc/systemd/system/userconfig.service").symlink_to("../../../dev/null")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr


def test_a_display_library_reached_by_an_absolute_symlink_inside_the_image_passes(tmp_path):
    # The converse of the break fixture above: an absolute link target is
    # legitimate as long as it resolves inside the image. The gate must
    # re-root it rather than hand it to the host, which on a build machine
    # with no aarch64 multiarch directory would fail a perfectly good image.
    root, boot = clean_image(tmp_path)
    (root / ARCH_LIB / "libEGL.so.1").unlink()
    (root / ARCH_LIB / "libEGL.so.1").symlink_to(f"/{ARCH_LIB}/libEGL.so.1.1.0")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr
    assert "image-gate: all checks passed" in result.stdout


def test_a_display_library_reached_through_two_symlink_hops_passes(tmp_path):
    # Nothing in the debs chains twice today, but following links one hop
    # only would be an accident waiting for the first package that does.
    root, boot = clean_image(tmp_path)
    arch = root / ARCH_LIB
    (arch / "libEGL.so.1").unlink()
    (arch / "libEGL.so.1.moved").symlink_to("libEGL.so.1.1.0")
    (arch / "libEGL.so.1").symlink_to("libEGL.so.1.moved")
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr


def test_a_display_library_symlink_loop_fails_rather_than_hanging(tmp_path):
    root, boot = clean_image(tmp_path)
    arch = root / ARCH_LIB
    (arch / "libEGL.so.1").unlink()
    (arch / "libEGL.so.1").symlink_to("libEGL.so.1.loop")
    (arch / "libEGL.so.1.loop").symlink_to("libEGL.so.1")
    result = gate(root, boot)
    assert result.returncode == 1, result.stdout
    assert "libEGL.so.1" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="running as root can read anything, so an unreadable fixture proves nothing")
def test_an_unreadable_directory_fails_closed(tmp_path):
    # A find that cannot read part of the image must fail the gate, not be
    # read as "nothing here" -- the whole point of round 2's fail-closed fix.
    root, boot = clean_image(tmp_path)
    target = root / "var/lib/scoreboard"
    target.chmod(0o000)
    try:
        result = gate(root, boot)
        assert result.returncode == 1, result.stdout
        assert "image-gate: FAIL:" in result.stderr
    finally:
        target.chmod(0o700)
