"""The updater's units, held to the sandbox table in design 7.1. Each
assertion is a directive whose removal would widen what a unit can reach:
the write unit's two devices, the identity boundary, the absence of any
capability, and the health unit's lack of a network.
"""
import re
from pathlib import Path

import pytest

DEVICE = Path(__file__).resolve().parents[1]
UNITS = {
    "planner": DEVICE / "scoreboard-update.service",
    "writer": DEVICE / "scoreboard-update@.service",
    "health": DEVICE / "scoreboard-health.service",
}
COMMON = {
    "ProtectSystem": "strict", "ProtectHome": "yes", "PrivateTmp": "yes", "NoNewPrivileges": "yes",
    "ProtectKernelTunables": "yes", "ProtectKernelModules": "yes", "ProtectControlGroups": "yes",
    "ProtectClock": "yes", "RestrictNamespaces": "yes", "RestrictRealtime": "yes", "RestrictSUIDSGID": "yes",
    "LockPersonality": "yes", "MemoryDenyWriteExecute": "yes", "SystemCallFilter": "@system-service",
    "CapabilityBoundingSet": "", "DevicePolicy": "closed", "UMask": "077",
    "InaccessiblePaths": "/state /var/lib/scoreboard",
}


def directives(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#") and not line.startswith("["):
            k, v = line.split("=", 1)
            out.setdefault(k, []).append(v)
    return out


@pytest.mark.parametrize("name", sorted(UNITS))
def test_every_updater_unit_carries_the_common_sandbox(name):
    d = directives(UNITS[name])
    for key, value in COMMON.items():
        assert d.get(key) == [value], f"{UNITS[name].name}: {key}={value}"
    assert "User" not in d, "root, narrowed by the sandbox, not a user in group disk"
    assert d["Type"] == ["oneshot"]


def test_the_write_unit_may_write_only_its_own_directory_and_the_planner_the_same():
    for name in ("planner", "writer"):
        assert directives(UNITS[name])["ReadWritePaths"] == ["/var/lib/scoreboard-update"], name


def test_the_health_unit_may_write_setup_and_its_records_and_has_no_network():
    d = directives(UNITS["health"])
    assert d["ReadWritePaths"] == ["/boot/setup /var/lib/scoreboard-update"]
    assert d["PrivateNetwork"] == ["yes"]
    assert d["RestrictAddressFamilies"] == ["AF_UNIX"]
    assert "DeviceAllow" not in d
    assert d["WantedBy"] == ["multi-user.target"]
    assert "After" in d and not any("scoreboard.service" in a for a in d["After"]), \
        "the thing it judges may not start; it must not wait for it"
    assert int(d["TimeoutStartSec"][0]) > 240


def test_the_network_units_have_outbound_sockets_and_unix_and_nothing_else():
    for name in ("planner", "writer"):
        d = directives(UNITS[name])
        assert d["RestrictAddressFamilies"] == ["AF_INET AF_INET6 AF_UNIX"], name
        assert "PrivateNetwork" not in d, name


def test_the_write_units_devices_come_only_from_the_per_slot_drop_ins():
    assert "DeviceAllow" not in directives(UNITS["writer"])
    assert "DeviceAllow" not in directives(UNITS["planner"])
    a = directives(DEVICE / "scoreboard-update@a.service.d" / "slot.conf")
    b = directives(DEVICE / "scoreboard-update@b.service.d" / "slot.conf")
    # The roots are 5 and 6: tools/image-layout.sh puts them inside the
    # extended container at 4, so design 4.1's 4 and 5 would be the EBR and
    # slot A's own root.
    assert a["DeviceAllow"] == ["/dev/mmcblk0p2 rw", "/dev/mmcblk0p5 rw"]
    assert b["DeviceAllow"] == ["/dev/mmcblk0p3 rw", "/dev/mmcblk0p6 rw"]
    # The slots the code names are the ones the sandbox allows.
    from scoreboard import update
    assert [str(update.slot("a").boot_dev), str(update.slot("a").root_dev)] == ["/dev/mmcblk0p2", "/dev/mmcblk0p5"]
    assert [str(update.slot("b").boot_dev), str(update.slot("b").root_dev)] == ["/dev/mmcblk0p3", "/dev/mmcblk0p6"]


def test_nothing_of_ours_names_boot_firmware_writable():
    # rpi-eeprom-update is the one writer of /boot/firmware (design 4.3);
    # the gate asserts the same over every unit on the image.
    for path in DEVICE.glob("scoreboard*.service"):
        for value in directives(path).get("ReadWritePaths", []):
            assert "/boot/firmware" not in value, path.name


def test_the_exec_lines_are_the_three_modes():
    assert directives(UNITS["planner"])["ExecStart"] == ["/opt/scoreboard/.venv/bin/python -m scoreboard.update plan"]
    assert directives(UNITS["writer"])["ExecStart"] == ["/opt/scoreboard/.venv/bin/python -m scoreboard.update run %i"]
    assert directives(UNITS["health"])["ExecStart"] == ["/opt/scoreboard/.venv/bin/python -m scoreboard.update health"]


def test_the_timer_is_daily_randomized_and_not_persistent():
    d = directives(DEVICE / "scoreboard-update.timer")
    assert d["OnBootSec"] == ["20min"] and d["OnUnitActiveSec"] == ["24h"]
    assert d["RandomizedDelaySec"] == ["1h"]
    assert "Persistent" not in d, "its stamp would live on the tmpfs /var/lib/systemd"
    assert d["WantedBy"] == ["timers.target"]


def test_the_planner_is_started_by_the_timer_alone():
    assert "WantedBy" not in directives(UNITS["planner"])
    assert "Install" not in UNITS["planner"].read_text()


def test_both_scoreboard_units_provide_the_runtime_directory():
    for name in ("scoreboard.service", "scoreboard-appliance.service"):
        d = directives(DEVICE / name)
        assert d["RuntimeDirectory"] == ["scoreboard"], name
        assert d["RuntimeDirectoryMode"] == ["0755"], name


def test_pi_setup_installs_and_enables_the_updater():
    text = (DEVICE.parent / "tools" / "pi-setup.sh").read_text()
    for unit in ("scoreboard-update.timer", "scoreboard-update.service", "scoreboard-update@.service",
                 "scoreboard-health.service"):
        assert unit in text, unit
    # One line per slot, not a loop over $s: test_pi_gen_recipe pairs each
    # "$DEVICE/<top>" install_appliance names with build.sh's copy list.
    for slot_ in ("a", "b"):
        assert f'"$DEVICE/scoreboard-update@{slot_}.service.d/slot.conf"' in text, slot_
    assert "multi-user.target.wants/scoreboard-health.service" in text
    assert "timers.target.wants/scoreboard-update.timer" in text
    assert "multi-user.target.wants/scoreboard-update.service" not in text


def test_the_image_build_copies_every_updater_unit_pi_setup_installs():
    # tools/pi-gen/build.sh hands pi-setup.sh an explicit list of device
    # files, never the directory, so that a developer's identity cannot
    # reach an image. Every unit install_appliance copies must be on it, or
    # the chroot install fails thirty minutes into a build.
    build = (DEVICE.parent / "tools" / "pi-gen" / "build.sh").read_text()
    for name in ("scoreboard-update.timer", "scoreboard-update.service", "scoreboard-update@.service",
                 "scoreboard-update@a.service.d", "scoreboard-update@b.service.d", "scoreboard-health.service"):
        assert f'"$REPO/device/{name}"' in build, name
