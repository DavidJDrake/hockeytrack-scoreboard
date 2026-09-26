"""Over-the-air updates: the parts shared by the planner, the write unit and
the health unit. Design: docs/superpowers/specs/2026-09-25-ota-update-design.md
(sections 5 and 7); this package is SCO-68.

What is here is deliberately small and boring: the constants every unit must
agree on, the version rule, which partitions make up a slot, how the three
bootloader values under /proc/device-tree are read, and the records the
units leave for each other on STATE. Anything that touches the network, a
block device or autoboot.txt lives in its own module so that the unit which
may do one of those things is the only one that imports the code for it.

Trust, stated once (design 7.5): TLS gets bytes to the panel; the signature
over the manifest is the only thing the panel trusts; the manifest's hashes
say whether a payload is the one signed for; the health check says whether
it works. latest.json is a hint. Nothing in the records is trusted for more
than it is: a stage is re-hashed from the card before it is armed.
"""
from __future__ import annotations

import json
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

# --- where things are ----------------------------------------------------
# /var/lib/scoreboard-update is a bind of STATE's update/ directory (design
# 4.3). It is the one path any of the three units may write; the identity in
# /var/lib/scoreboard and /state itself are InaccessiblePaths= to all three.
STATE_DIR = Path("/var/lib/scoreboard-update")
SETUP_DIR = Path("/boot/setup")
AUTOBOOT = SETUP_DIR / "autoboot.txt"
# The release public keys, installed by pi-setup.sh --appliance from
# device/certs/release-signing/. The private half never exists outside AWS
# KMS (design 6.3); nothing in this package can make a signature, only check
# one.
KEY_DIR = Path("/opt/scoreboard/certs/release-signing")
BUILD_FILE = Path("/etc/scoreboard-build")
RUN_DIR = Path("/run/scoreboard")
STATUS_FILE = RUN_DIR / "status.json"
HEALTHY_MARKER = RUN_DIR / "healthy"
DEVICE_TREE = Path("/proc/device-tree/chosen/bootloader")
CMDLINE = Path("/proc/cmdline")
MIRROR = "https://images.scoreboard.davidjdrake.com"

# The partition layout generation this build was made for. A manifest for any
# other layout is refused (design 6.2): a future repartition must never be
# applied as an update, because the updater cannot repartition the card it
# runs from.
LAYOUT = 1

# Numbers every unit shares. HEALTH_BOUND_S is the netcfg budget's 91 s
# ceiling, first paint about 21 s after it, a broker connect, and headroom
# (design 5.2); the unit file carries a TimeoutStartSec above it. The trial
# cap and the lead are the owner's defaults (design 13, items 6 and 2).
HEALTH_BOUND_S = 240
QUIET_LEAD_S = 45 * 60
STATUS_FRESH_S = 120
MAX_ARMINGS = 3
MAX_DOWNLOAD_FAILURES = 3
POINTER_LIMIT = 64 * 1024
POINTER_TIMEOUT_S = 15
PAYLOAD_DEADLINE_S = 30 * 60
# The per-socket timeout on a payload fetch, separate from the deadline
# above: the deadline is checked between reads, so a mirror that stalls
# without closing would otherwise hold one read() for the whole 30 minutes
# and carry the unit past its TimeoutStartSec, where SIGTERM skips the
# cleanup that counts the download (design 7.6). A minute with no bytes at
# all is a dead connection, not a slow one.
PAYLOAD_SOCKET_TIMEOUT_S = 60
CHUNK = 4 * 1024 * 1024

# No leading zeros, so that one version has one spelling; fullmatch, because
# re's $ also matches before a trailing newline and a version with one is not
# a version.
VERSION_RE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
CHANNELS = ("stable", "test")


class Refused(Exception):
    """A manifest, a pointer, a record or a request this panel will not act
    on. ``reason`` is one short word the journal line and refused.json
    carry; ``detail`` is for the journal only."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail


# --- versions ------------------------------------------------------------
def parse_version(text) -> tuple[int, int, int]:
    """``vX.Y.Z`` as three integers; anything else is malformed, including a
    leading zero or a suffix. The comparison is numeric so v0.10.0 is newer
    than v0.9.0, which a string compare gets wrong."""
    if not isinstance(text, str):
        raise Refused("malformed", "version is not a string")
    m = VERSION_RE.fullmatch(text)
    if not m:
        raise Refused("malformed", f"version {text!r} is not vX.Y.Z")
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def running_version(path: Path = BUILD_FILE) -> str:
    """The first token of /etc/scoreboard-build (design fact 12)."""
    try:
        first = path.read_text().split()[0]
    except (OSError, IndexError) as e:
        raise Refused("malformed", f"cannot read the running version from {path}: {e}")
    parse_version(first)
    return first


# --- slots ---------------------------------------------------------------
@dataclass(frozen=True)
class Layout:
    """Partition sizes, in one place, defaulting to the six-partition table
    of design 4.1. Tests shrink them; the field never does. ``head`` is the
    part of a boot partition the partition walk needs to find start4.elf,
    which is what the slot invariant (design 5.3) keeps zero."""
    boot_size: int = 256 * 1024 * 1024
    root_size: int = 3 * 1024 * 1024 * 1024
    head: int = 4 * 1024 * 1024


@dataclass(frozen=True)
class Slot:
    name: str            # "a" or "b"
    boot_partition: int  # MBR partition number of the boot FAT
    root_partition: int  # MBR partition number of the root ext4
    boot_dev: Path
    root_dev: Path


def slot(name: str, devices: Path = Path("/dev")) -> Slot:
    """The two partitions of a slot as tools/image-layout.sh builds the card:
    A is 2 and 5, B is 3 and 6. Design 4.1 wrote the roots as 4 and 5, but
    MBR holds four primary partitions, so the layout puts the roots and
    STATE inside an extended container that takes number 4, and they become
    logical partitions 5, 6 and 7. The boot slots keep 2 and 3, which is
    what autoboot.txt and bootloader/partition name. The layout is what
    ships, so these numbers follow it and not the design text; a slot
    numbered by the design would open partition 4, the container's EBR,
    from slot B. The device names are the one place this package depends on
    the kernel calling the card mmcblk0 (design 7.1); the unit's DeviceAllow
    depends on the same names, and a card that appears under another name
    gets no device at all, which fails closed."""
    if name == "a":
        return Slot("a", 2, 5, devices / "mmcblk0p2", devices / "mmcblk0p5")
    if name == "b":
        return Slot("b", 3, 6, devices / "mmcblk0p3", devices / "mmcblk0p6")
    raise Refused("malformed", f"no such slot {name!r}")


def running_slot(boot_partition: int | None) -> str:
    """Which slot the bootloader booted, from bootloader/partition."""
    if boot_partition == 2:
        return "a"
    if boot_partition == 3:
        return "b"
    raise Refused("malformed", f"booted from partition {boot_partition!r}, which is not a slot")


def other_slot(name: str) -> str:
    return "b" if name == "a" else "a"


# --- what the bootloader left in the device tree --------------------------
def read_u32(name: str, tree: Path = DEVICE_TREE) -> int | None:
    """One of the raw 32-bit big-endian values the firmware leaves under
    /proc/device-tree/chosen/bootloader (design fact 3). None when the file
    is missing or not four bytes, which is a board or firmware too old to
    have written it; callers treat None as "cannot tell", never as 0."""
    try:
        raw = (tree / name).read_bytes()
    except OSError:
        return None
    if len(raw) != 4:
        return None
    return struct.unpack(">I", raw)[0]


def cmdline_root_partition(cmdline: Path = CMDLINE) -> int | None:
    """The partition number in ``root=PARTUUID=<disk id>-NN`` on the kernel
    command line. The write unit refuses a root partition with this number
    however it was asked (design 7.1); None means the cmdline does not say,
    and the caller must refuse everything rather than guess."""
    try:
        text = cmdline.read_text()
    except OSError:
        return None
    m = re.search(r"(?:^|\s)root=PARTUUID=[0-9a-fA-F]{8}-([0-9a-fA-F]{2})(?:\s|$)", text)
    return int(m.group(1), 16) if m else None


def refuse_running(target: Slot, booted_partition: int | None, root_partition: int | None) -> None:
    """The write unit's own refusal of the running slot, beside the
    sandbox's DeviceAllow (design 7.1). Both halves are checked separately
    and a value nobody can read refuses, because the one thing this code
    must never do is write the partition it is running from."""
    if booted_partition is None:
        raise Refused("running", "bootloader/partition is unreadable; refusing every slot")
    if root_partition is None:
        raise Refused("running", "root= on the kernel command line is unreadable; refusing every slot")
    if target.boot_partition == booted_partition:
        raise Refused("running", f"partition {booted_partition} is the boot partition this panel booted from")
    if target.root_partition == root_partition:
        raise Refused("running", f"partition {root_partition} is the root this panel is running")


# --- autoboot.txt --------------------------------------------------------------
# Shared by the health unit, which writes the file to commit a trial, and
# the planner, which reads it before staging anything: a file the commit
# would refuse is found before the download, not after two dark reboots.
def render_autoboot(default: int, tryboot: int) -> bytes:
    """The file as design 4.2 writes it. Byte for byte, so that the read-back
    after a commit is an equality and not a parse."""
    return f"[all]\ntryboot_a_b=1\nboot_partition={default}\n[tryboot]\nboot_partition={tryboot}\n".encode()


def parse_autoboot(text: bytes) -> tuple[int, int]:
    """(default partition, tryboot partition), or a refusal for a file this
    code did not write. Sections and keys as the bootloader reads them;
    anything else in the file is a reason not to touch it."""
    section, default, trial, a_b = "all", None, None, False
    for raw in text.decode("ascii", "replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(all|tryboot|none)\]", line)
        if m:
            section = m.group(1)
            continue
        m = re.fullmatch(r"(tryboot_a_b|boot_partition)=([0-9]+)", line)
        if not m:
            raise Refused("malformed", f"autoboot.txt has a line this code does not understand: {line!r}")
        key, value = m.group(1), int(m.group(2))
        if key == "tryboot_a_b":
            if value != 1 or section != "all":
                raise Refused("malformed", "autoboot.txt does not set tryboot_a_b=1 under [all]")
            a_b = True
        elif section == "all":
            default = value
        elif section == "tryboot":
            trial = value
    if not a_b:
        raise Refused("malformed", "autoboot.txt does not set tryboot_a_b=1; without it the [tryboot] section is not an A/B switch")
    if default not in (2, 3) or trial not in (2, 3) or default == trial:
        raise Refused("malformed", f"autoboot.txt names partitions {default!r} and {trial!r}")
    return default, trial


# --- the records on STATE ---------------------------------------------------
class Records:
    """The files under /var/lib/scoreboard-update (design 7.4).

    Every write is to a temporary name, fsynced, renamed over the old name,
    with the directory fsynced afterwards, so a power cut leaves either the
    old record or the new one and never a torn one. The health unit's whole
    verdict rides on trial.json being readable on the next boot.
    """

    def __init__(self, directory: Path = STATE_DIR) -> None:
        self.dir = directory

    def path(self, name: str) -> Path:
        return self.dir / name

    def read_json(self, name: str):
        """The parsed record, or None when it does not exist. A record that
        exists but cannot be parsed raises, because "no record" and "a
        record I cannot read" are different answers for the health unit."""
        try:
            raw = self.path(name).read_bytes()
        except FileNotFoundError:
            return None
        return json.loads(raw)

    def write_json(self, name: str, value) -> None:
        self.write_bytes(name, json.dumps(value, indent=1, sort_keys=True).encode() + b"\n")

    def write_bytes(self, name: str, data: bytes) -> None:
        write_atomically(self.path(name), data)

    def remove(self, name: str) -> None:
        try:
            self.path(name).unlink()
        except FileNotFoundError:
            return
        fsync_dir(self.dir)

    def channel(self) -> str:
        """The channel this panel follows. Absent means stable; the file is
        written by a person on the spare board and by nothing else (design
        6.2). Anything but a known channel name is treated as stable rather
        than as a channel nobody publishes, so a typo cannot silence a
        panel's updates."""
        try:
            name = self.path("channel").read_text().strip()
        except OSError:
            return "stable"
        return name if name in CHANNELS else "stable"

    def failed(self) -> dict:
        return self.read_json("failed.json") or {}

    def mark_failed(self, version: str, reason: str, at: int) -> None:
        failed = self.failed()
        failed[version] = {"at": at, "reason": reason}
        self.write_json("failed.json", failed)

    def discard_stage(self, slot_name: str | None = None) -> None:
        """Forget a staged version: the record and the held-back head. The
        slot's bytes stay where they are, non-bootable, until the next
        stage overwrites them."""
        self.remove("staged.json")
        for name in ("a", "b") if slot_name is None else (slot_name,):
            self.remove(f"head-{name}.bin")


def write_atomically(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write to a temporary name, fsync, rename over ``path``, fsync the
    directory.

    The mode is set on the open descriptor, not left to the create, because
    every updater unit runs under UMask=077 and a record created under it
    is 0600, root-only. scoreboard.service runs as its own user and reads
    failed.json and refused.json to choose its status subscriptions (design
    8.1); a 0600 record is a PermissionError there, a warning every boot,
    and a failed or refused version that never reaches Home. Design 7.4
    says 0644, and nothing in the records is secret: they name versions,
    slots and reasons, and the head file holds the first bytes of a public
    image. On SETUP, a vfat mounted with umask=022, every file is already
    0644, so there the fchmod changes nothing and is not refused."""
    tmp = path.with_name(path.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(fd, mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    fsync_dir(path.parent)


def fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def status_topics(thing: str, version: str, records: Records) -> list[str]:
    """The subscriptions that carry a panel's version to the site (design
    8.1). Nothing publishes to them; AWS IoT's own subscription lifecycle
    event carries the topic names to a rule. Each is scoped to this thing,
    which the IoT policy pins to the certificate's own thing name, so a
    panel can claim a version for itself and for nobody else. The failed
    and refused topics only exist while there is something to say."""
    topics = [f"scoreboard/{thing}/status/running/{version}"]
    failed = records.failed()
    if failed:
        latest = max(failed, key=lambda v: failed[v].get("at", 0))
        if VERSION_RE.fullmatch(latest):
            topics.append(f"scoreboard/{thing}/status/failed/{latest}")
    try:
        refused = records.read_json("refused.json")
    except ValueError:
        refused = None
    if isinstance(refused, dict) and refused.get("reason") in ("signature", "key"):
        topics.append(f"scoreboard/{thing}/status/refused/{refused['reason']}")
    return topics
