"""tools/image-layout.sh against a small fixture root and boot partition.

The script never mounts anything, so neither does this: the assembled image
is read back with sfdisk, mtools and debugfs, the same tools the gate uses.
The image is sparse (about 100 MB on disk for a 6,992 MiB file) and is built
once per session because the xz of a 3 GiB root takes some seconds.
"""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LAYOUT = REPO / "tools" / "image-layout.sh"
MIB = 1024 * 1024

needs_tools = pytest.mark.skipif(
    any(shutil.which(t) is None for t in ("sfdisk", "mkfs.vfat", "mcopy", "mdir", "mkfs.ext4", "debugfs", "xz")),
    reason="needs util-linux, dosfstools, mtools, e2fsprogs and xz on PATH (CI installs them)")


def run_print(what: str) -> str:
    return subprocess.run(["bash", str(LAYOUT), "--print", what], capture_output=True, text=True,
                          check=True, timeout=10).stdout


PIGEN_CMDLINE = ("console=serial0,115200 console=tty1 root=PARTUUID=abc-02 rootfstype=ext4 "
                 "fsck.repair=yes rootwait resize\n")


def make_fixture(base: Path) -> tuple[Path, Path]:
    """A root and a boot partition with the shape of pi-gen's output."""
    root, boot = base / "root", base / "boot"
    (root / "etc").mkdir(parents=True)
    (root / "boot" / "firmware").mkdir(parents=True)
    (root / "etc" / "passwd").write_text("root:x:0:0:root:/root:/bin/bash\n")
    # pi-gen's fstab: it is what mounts the root read-write, and it has to go.
    (root / "etc" / "fstab").write_text(
        "proc /proc proc defaults 0 0\n"
        "PARTUUID=abc-01 /boot/firmware vfat defaults 0 2\n"
        "PARTUUID=abc-02 / ext4 defaults,noatime 0 1\n")
    (root / "etc" / "machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    (root / "opt").mkdir()
    (root / "opt" / "data.bin").write_bytes(os.urandom(4096))
    (boot / "overlays").mkdir(parents=True)
    # pi-gen's cmdline at the pinned commit (stage1/00-boot-files/files/
    # cmdline.txt, ROOTDEV substituted by export-image/04-set-partuuid),
    # verbatim: no `ro`, no `init=`, and the `resize` token that arms the
    # first-boot resize. An earlier fixture invented ` ro` and an `init=`, so
    # the build passed here and would have died at "Lay out the A/B card".
    (boot / "cmdline.txt").write_text(PIGEN_CMDLINE)
    (boot / "config.txt").write_text("dtparam=audio=on\n\n[all]\ndtoverlay=disable-bt\n")
    (boot / "start4.elf").write_bytes(b"firmware")
    (boot / "overlays" / "disable-bt.dtbo").write_bytes(b"overlay")
    return root, boot


def layout(root: Path, boot: Path, out: Path, version: str = "v0.0.1"):
    return subprocess.run(["bash", str(LAYOUT), str(root), str(boot), version, str(out)],
                          capture_output=True, text=True, timeout=600)


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    base = tmp_path_factory.mktemp("layout")
    root, boot = make_fixture(base)
    out = base / "out"
    result = layout(root, boot, out)
    assert result.returncode == 0, result.stderr + result.stdout
    return {"root": root, "boot": boot, "out": out, "image": out / "scoreboard-v0.0.1.img"}


def table(image: Path) -> dict:
    return json.loads(subprocess.run(["sfdisk", "--json", str(image)], capture_output=True,
                                     text=True, check=True).stdout)["partitiontable"]


def partition_bytes(image: Path, number: int) -> bytes:
    part = table(image)["partitions"][number - 1]
    with image.open("rb") as f:
        f.seek(part["start"] * 512)
        return f.read(part["size"] * 512)


def fat_read(image: Path, number: int, name: str) -> bytes:
    part = table(image)["partitions"][number - 1]
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1", MTOOLSRC="/dev/null")
    return subprocess.run(["mcopy", "-i", f"{image}@@{part['start'] * 512}", f"::{name}", "-"],
                          capture_output=True, check=True, env=env).stdout


def fat_list(image: Path, number: int) -> set[str]:
    part = table(image)["partitions"][number - 1]
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1", MTOOLSRC="/dev/null")
    out = subprocess.run(["mdir", "-i", f"{image}@@{part['start'] * 512}", "-b", "::"],
                         capture_output=True, text=True, check=True, env=env).stdout
    return {line.strip().removeprefix("::/").rstrip("/") for line in out.splitlines() if line.strip()}


def ext4_cat(image: Path, number: int, path: str) -> bytes:
    return _debugfs_on_partition(image, table(image)["partitions"][number - 1], f"cat {path}")


def _debugfs_on_partition(image: Path, part: dict, request: str) -> bytes:
    # debugfs cannot take an offset, so the partition is copied out first;
    # the copies are sparse and cheap.
    tmp = image.parent / f"part-{part['start']}.img"
    if not tmp.exists():
        with image.open("rb") as src, tmp.open("wb") as dst:
            src.seek(part["start"] * 512)
            remaining = part["size"] * 512
            while remaining:
                chunk = src.read(min(remaining, 4 * MIB))
                if chunk.strip(b"\0"):
                    dst.write(chunk)
                else:
                    dst.seek(len(chunk), 1)
                remaining -= len(chunk)
            dst.truncate(part["size"] * 512)
    return subprocess.run(["debugfs", "-R", request, str(tmp)], capture_output=True, check=False).stdout


def ext4_ls(image: Path, number: int, path: str) -> list[tuple[str, int, int, int]]:
    """(name, mode, uid, gid) for each entry, from debugfs's long listing."""
    part = table(image)["partitions"][number - 1]
    out = _debugfs_on_partition(image, part, f"ls -l {path}").decode()
    rows = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) < 8 or not fields[0].isdigit():
            continue
        rows.append((fields[-1], int(fields[1], 8), int(fields[3]), int(fields[4])))
    return rows


# --- The constants, printed for the gate ------------------------------------

def test_the_minimum_card_size_is_the_sum_of_the_table():
    # The number the download page quotes must be the image's actual size.
    starts = [int(l.split("start=")[1].split(",")[0]) for l in run_print("table").splitlines() if "start=" in l]
    sizes = [int(l.split("size=")[1].split(",")[0]) for l in run_print("table").splitlines() if "start=" in l]
    end_mib = max((s + z) for s, z in zip(starts, sizes)) // 2048
    assert end_mib == int(run_print("min-card-mib"))
    # Every partition starts on a 4 MiB boundary (SD cards erase in 4 MiB).
    assert all(s % (4 * 2048) == 0 for s in starts)


def test_the_table_has_the_six_partitions_in_the_design_s_order():
    # SETUP, BOOT-A, BOOT-B, the extended container MBR needs, then ROOT-A,
    # ROOT-B, STATE as logical partitions 5, 6 and 7.
    types = [l.split("type=")[1] for l in run_print("table").splitlines() if "type=" in l]
    assert types == ["c", "c", "c", "5", "83", "83", "83"]
    assert run_print("partitions").split() == [
        "setup=1", "boot-a=2", "boot-b=3", "root-a=5", "root-b=6", "state=7"]


def test_the_fstab_mounts_the_shared_partitions_nofail_and_persists_exactly_five_things():
    fstab = run_print("fstab")
    disk = run_print("disk-id").strip()
    lines = [l.split() for l in fstab.splitlines()]
    by_target = {l[1]: l for l in lines}
    # No root entry: the root stays as read-only as its cmdline says, and
    # nothing is there for systemd-remount-fs to make writable.
    assert "/" not in by_target
    assert "/boot/firmware" not in by_target, "the running slot's FAT is mounted by the generator, not fstab"
    assert by_target["/state"][0] == f"PARTUUID={disk}-07"
    assert by_target["/boot/setup"][0] == f"PARTUUID={disk}-01"
    for target in ("/state", "/boot/setup"):
        opts = by_target[target][3].split(",")
        assert "nofail" in opts and "x-systemd.device-timeout=10s" in opts, target
        assert "noexec" in opts and "nodev" in opts and "nosuid" in opts, target
    binds = [l for l in lines if l[2] == "none"]
    assert [l[1] for l in binds] == [
        "/var/lib/scoreboard", "/var/lib/scoreboard-update", "/etc/NetworkManager/system-connections",
        "/var/lib/NetworkManager", "/var/log/journal", "/etc/fake-hwclock.data"]
    for l in binds:
        assert l[0].startswith("/state/")
        assert "nofail" in l[3].split(",") and "x-systemd.requires-mounts-for=/state" in l[3].split(",")
    assert all("overlay" not in l[2] for l in lines), "no overlay: a stale /etc file must never shadow a new root's"
    assert {l[1] for l in lines if l[2] == "tmpfs"} == {"/tmp", "/var/tmp", "/var/log", "/var/lib/systemd"}


def test_autoboot_starts_on_slot_a_and_tries_slot_b():
    assert run_print("autoboot") == "[all]\ntryboot_a_b=1\nboot_partition=2\n[tryboot]\nboot_partition=3\n"


def test_the_state_skeleton_is_exactly_the_five_things_and_scoreboard_is_owned_by_900():
    rows = [l.split() for l in run_print("state-skeleton").splitlines()]
    assert [r[0] for r in rows] == [
        "scoreboard", "update", "network", "network/connections", "network/lib", "journal", "fake-hwclock.data"]
    assert ("scoreboard", "d", "0700", "900") in [tuple(r) for r in rows]
    assert "channel" not in {r[0] for r in rows}, "the channel file is written by hand on the spare board, never shipped"


# --- The built image --------------------------------------------------------

@needs_tools
def test_the_image_has_the_printed_table_and_disk_identifier(built):
    t = table(built["image"])
    assert t["id"] == "0x" + run_print("disk-id").strip()
    got = [(p["start"], p["size"], p["type"]) for p in t["partitions"]]
    want = [(int(l.split("start=")[1].split(",")[0]), int(l.split("size=")[1].split(",")[0]), l.split("type=")[1])
            for l in run_print("table").splitlines() if "start=" in l]
    assert got == want
    assert built["image"].stat().st_size == int(run_print("min-card-mib")) * MIB


@needs_tools
def test_both_slots_are_byte_identical_to_the_payloads(built):
    image, out = built["image"], built["out"]
    boot = (out / "scoreboard-v0.0.1.boot.img").read_bytes()
    root_sha = hashlib.sha256((out / "scoreboard-v0.0.1.root.img").read_bytes()).hexdigest()
    assert partition_bytes(image, 2) == boot
    assert partition_bytes(image, 3) == boot
    assert hashlib.sha256(partition_bytes(image, 5)).hexdigest() == root_sha
    assert hashlib.sha256(partition_bytes(image, 6)).hexdigest() == root_sha
    payloads = json.loads((out / "scoreboard-v0.0.1.payloads.json").read_text())
    assert payloads["layout"] == 1
    assert payloads["boot"]["rawSha256"] == hashlib.sha256(boot).hexdigest()
    assert payloads["root"]["rawSha256"] == root_sha
    assert payloads["boot"]["rawSize"] == 256 * MIB and payloads["root"]["rawSize"] == 3072 * MIB
    for name in ("boot", "root"):
        xz = out / payloads[name]["file"]
        assert payloads[name]["size"] == xz.stat().st_size
        assert payloads[name]["sha256"] == hashlib.sha256(xz.read_bytes()).hexdigest()


@needs_tools
def test_each_slot_cmdline_names_its_own_root_says_ro_and_drops_the_first_boot_resize(built):
    disk = run_print("disk-id").strip()
    a = fat_read(built["image"], 2, "cmdline-a.txt").decode().split()
    b = fat_read(built["image"], 2, "cmdline-b.txt").decode().split()
    assert f"root=PARTUUID={disk}-05" in a
    assert f"root=PARTUUID={disk}-06" in b
    for cmdline in (a, b):
        assert cmdline.count("ro") == 1, "pi-gen's line has no ro; the layout adds exactly one"
        assert "rw" not in cmdline
        assert "resize" not in cmdline, "the token the initramfs grows the root partition on"
        assert not any(t.startswith("init=") for t in cmdline), "the root must never resize"
        assert "rootwait" in cmdline and "fsck.repair=yes" in cmdline, "the rest of pi-gen's cmdline is kept"
    assert a[:2] == ["console=serial0,115200", "console=tty1"], "pi-gen's order is kept; ro is appended"
    assert "cmdline.txt" not in fat_list(built["image"], 2), "a cmdline.txt naming a root this card lacks must not remain"


@needs_tools
def test_config_txt_selects_the_cmdline_by_the_partition_it_was_loaded_from(built):
    config = fat_read(built["image"], 2, "config.txt").decode()
    assert config.startswith(run_print("config-head"))
    assert config.endswith("dtparam=audio=on\n\n[all]\ndtoverlay=disable-bt\n"), "pi-gen's config.txt follows the block unchanged"


@needs_tools
def test_the_boot_slot_carries_pi_gen_s_files_and_a_readme(built):
    names = fat_list(built["image"], 2)
    assert {"start4.elf", "overlays", "config.txt", "cmdline-a.txt", "cmdline-b.txt", "README.txt"} <= names
    assert fat_read(built["image"], 2, "overlays/disable-bt.dtbo") == b"overlay"
    assert b"SETUP" in fat_read(built["image"], 2, "README.txt")


@needs_tools
def test_setup_holds_only_autoboot_and_a_readme_and_no_firmware(built):
    # SETUP must never be bootable: with firmware on it, a lost autoboot.txt
    # would boot from it with no root to go with it.
    assert fat_list(built["image"], 1) == {"autoboot.txt", "README.txt"}
    assert fat_read(built["image"], 1, "autoboot.txt").decode() == run_print("autoboot")


@needs_tools
def test_setup_s_readme_says_it_is_the_right_drive(built):
    # The boot slots' README sends the person to SETUP. The same text on
    # SETUP itself would tell whoever found the right drive that it is the
    # wrong one.
    readme = fat_read(built["image"], 1, "README.txt").decode()
    assert readme == run_print("setup-readme")
    assert "scoreboard-setup.txt" in readme and "SETUP" in readme
    assert "not on" not in readme
    assert "autoboot.txt" in readme, "the one other file here is named, so it is not deleted as clutter"
    assert "not on" in fat_read(built["image"], 2, "README.txt").decode()


@needs_tools
def test_the_root_slot_has_the_fstab_the_mount_points_and_an_empty_machine_id(built):
    image = built["image"]
    assert ext4_cat(image, 5, "/etc/fstab").decode() == run_print("fstab")
    assert ext4_cat(image, 5, "/etc/machine-id") == b"", "a per-boot machine id on a read-only root"
    assert ext4_cat(image, 5, "/opt/data.bin") == (built["root"] / "opt" / "data.bin").read_bytes()
    root_entries = {name: (mode, uid, gid) for name, mode, uid, gid in ext4_ls(image, 5, "/")}
    assert root_entries["state"] == (0o40755, 0, 0)
    assert {name for name, *_ in ext4_ls(image, 5, "/boot")} >= {"firmware", "setup"}
    fstab_entry = [e for e in ext4_ls(image, 5, "/etc") if e[0] == "fstab"][0]
    assert fstab_entry[1:] == (0o100644, 0, 0)


@needs_tools
def test_state_holds_the_skeleton_and_nothing_else(built):
    entries = {name: (mode, uid, gid) for name, mode, uid, gid in ext4_ls(built["image"], 7, "/")}
    entries.pop(".", None)
    entries.pop("..", None)
    assert set(entries) == {"lost+found", "scoreboard", "update", "network", "journal", "fake-hwclock.data"}
    assert entries["scoreboard"] == (0o40700, 900, 900)
    assert entries["update"] == (0o40755, 0, 0)
    assert entries["fake-hwclock.data"] == (0o100644, 0, 0)
    network = {name: (mode, uid, gid) for name, mode, uid, gid in ext4_ls(built["image"], 7, "/network")}
    assert network["connections"] == (0o40700, 0, 0)
    assert network["lib"] == (0o40755, 0, 0)
    assert b"channel" not in _debugfs_on_partition(built["image"], table(built["image"])["partitions"][6], "ls -l /update")


# --- Refusals ---------------------------------------------------------------

@needs_tools
def test_a_boot_partition_fuller_than_the_limit_fails_the_build(tmp_path):
    root, boot = make_fixture(tmp_path)
    (boot / "kernel_big.img").write_bytes(os.urandom(160 * MIB))
    result = layout(root, boot, tmp_path / "out")
    assert result.returncode != 0
    assert "boot slot is" in result.stderr and "% full" in result.stderr


@needs_tools
def test_a_cmdline_without_ro_gets_one_and_rw_and_init_are_dropped(tmp_path):
    # A pi-gen bump that adds `rw`, or brings the old first-boot init= back,
    # must not reach a card: the kernel's default is ro and nothing remounts
    # the root, but the gate wants the flag stated. The one line that lands
    # on the card is read back from the image, not from the script's output.
    root, boot = make_fixture(tmp_path)
    (boot / "cmdline.txt").write_text(
        "console=tty1 root=PARTUUID=abc-02 rootwait init=/usr/lib/raspberrypi-sys-mods/firstboot rw resize\n")
    result = layout(root, boot, tmp_path / "out")
    assert result.returncode == 0, result.stderr + result.stdout
    disk = run_print("disk-id").strip()
    a = fat_read(tmp_path / "out" / "scoreboard-v0.0.1.img", 2, "cmdline-a.txt").decode()
    assert a == f"console=tty1 root=PARTUUID={disk}-05 rootwait ro\n"


def test_a_bad_version_is_refused(tmp_path):
    root, boot = make_fixture(tmp_path)
    result = layout(root, boot, tmp_path / "out", version="v0.1; rm -rf /")
    assert result.returncode != 0
    assert "bad version" in result.stderr


def test_something_that_is_not_a_root_is_refused(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "boot").mkdir()
    result = layout(tmp_path / "root", tmp_path / "boot", tmp_path / "out")
    assert result.returncode != 0
    assert "not a root filesystem" in result.stderr
