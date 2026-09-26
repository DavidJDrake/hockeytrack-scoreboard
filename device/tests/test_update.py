"""scoreboard.update: the verifier, the write unit, the planner and the
health unit, against temporary files as partitions and a dictionary as the
mirror. The signing key is generated here, in the test process, and never
written to disk: the real private half lives in AWS KMS and nothing in this
repository can make a signature (design 6.3).
"""
import base64
import hashlib
import io
import json
import lzma
import os
import stat
import subprocess
import types
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from scoreboard import update
from scoreboard.update import Layout, Records, Refused, health, planner, verify, writer

# Small enough to run in milliseconds; the field sizes are the defaults.
LAYOUT = Layout(boot_size=16 * 1024, root_size=48 * 1024, head=4 * 1024)
NOW = 1_800_000_000
RUNNING = "v0.1.6"


# --- fixtures ------------------------------------------------------------
class Key:
    def __init__(self, key_id="release-2026-1"):
        self.key_id = key_id
        self._private = ec.generate_private_key(ec.SECP256R1())

    def pem(self) -> bytes:
        return self._private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def sign(self, data: bytes) -> bytes:
        return base64.b64encode(self._private.sign(data, ec.ECDSA(hashes.SHA256())))

    def ring(self):
        return [(self.key_id, self._private.public_key())]


def payload_bytes(seed: bytes, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        out += hashlib.sha256(seed + len(out).to_bytes(8, "big")).digest()
    return bytes(out[:size])


class Release:
    """A version's two payloads and its manifest, as the mirror would hold
    them, signed by ``key``."""

    def __init__(self, key: Key, version="v0.2.0", channel="stable", sizes=LAYOUT, root_raw=None, **overrides):
        self.version, self.sizes = version, sizes
        self.boot_raw = payload_bytes(b"boot" + version.encode(), sizes.boot_size)
        # payload_bytes is incompressible, which is the wrong shape for a
        # test about what xz does with a real root's zeroed free space;
        # those pass root_raw themselves.
        self.root_raw = payload_bytes(b"root" + version.encode(), sizes.root_size) if root_raw is None else root_raw
        self.boot_xz, self.root_xz = lzma.compress(self.boot_raw), lzma.compress(self.root_raw)
        manifest = {
            "version": version, "channel": channel,
            "released": "2026-10-03T02:11:09Z", "expires": "2027-01-31T02:11:09Z", "layout": update.LAYOUT,
            "payloads": {
                "boot": {"file": f"scoreboard-{version}.boot.img.xz", "size": len(self.boot_xz),
                         "sha256": hashlib.sha256(self.boot_xz).hexdigest(), "rawSize": sizes.boot_size,
                         "rawSha256": hashlib.sha256(self.boot_raw).hexdigest()},
                "root": {"file": f"scoreboard-{version}.root.img.xz", "size": len(self.root_xz),
                         "sha256": hashlib.sha256(self.root_xz).hexdigest(), "rawSize": sizes.root_size,
                         "rawSha256": hashlib.sha256(self.root_raw).hexdigest()},
            },
            "keyId": key.key_id,
        }
        for path, value in overrides.items():
            target = manifest
            *parents, last = path.split(".")
            for p in parents:
                target = target[p]
            if value is None:
                del target[last]
            else:
                target[last] = value
        self.manifest = json.dumps(manifest, indent=1).encode()
        self.signature = key.sign(self.manifest)

    def files(self, mirror=update.MIRROR, pointer="latest.json") -> dict:
        base = f"{mirror}/images/{self.version}"
        return {
            f"{mirror}/{pointer}": json.dumps({"version": self.version}).encode(),
            f"{base}/scoreboard-{self.version}.manifest.json": self.manifest,
            f"{base}/scoreboard-{self.version}.manifest.sig": self.signature,
            f"{base}/scoreboard-{self.version}.boot.img.xz": self.boot_xz,
            f"{base}/scoreboard-{self.version}.root.img.xz": self.root_xz,
        }


class FakeTransport:
    def __init__(self, files: dict):
        self.files, self.opened, self.timeouts = files, [], []

    def open(self, url, timeout):
        self.opened.append(url)
        self.timeouts.append(timeout)
        if url not in self.files:
            raise OSError(f"404 {url}")
        return io.BytesIO(self.files[url])


class FakeCard:
    """Slot B's two partitions as files, sized like the layout."""

    def __init__(self, tmp_path: Path, layout=LAYOUT):
        devices = tmp_path / "dev"
        devices.mkdir()
        (devices / "mmcblk0p3").write_bytes(payload_bytes(b"old boot", layout.boot_size))
        (devices / "mmcblk0p6").write_bytes(payload_bytes(b"old root", layout.root_size))
        (devices / "mmcblk0p2").write_bytes(payload_bytes(b"running boot", layout.boot_size))
        (devices / "mmcblk0p5").write_bytes(payload_bytes(b"running root", layout.root_size))
        self.devices = devices

    def slot(self, name="b"):
        return update.slot(name, self.devices)

    def head(self, name="b") -> bytes:
        dev = self.slot(name).boot_dev
        return dev.read_bytes()[:LAYOUT.head]


@pytest.fixture
def key():
    return Key()


@pytest.fixture
def records(tmp_path):
    d = tmp_path / "state" / "update"
    d.mkdir(parents=True)
    return Records(d)


def verified(release: Release, key: Key, **kw):
    key_id = verify.verify_signature(release.manifest, release.signature, key.ring())
    args = dict(verified_key=key_id, expected_version=release.version, channel="stable",
                now=NOW, running=RUNNING, layout=LAYOUT)
    args.update(kw)
    return verify.check_manifest(release.manifest, **args)


def refusal(fn, reason):
    with pytest.raises(Refused) as e:
        fn()
    assert e.value.reason == reason, str(e.value)
    return e.value


# --- versions --------------------------------------------------------------
@pytest.mark.parametrize("text", ["v0.2.0", "v10.0.3"])
def test_versions_are_three_integers(text):
    assert update.parse_version(text) == tuple(int(x) for x in text[1:].split("."))


@pytest.mark.parametrize("text", ["0.2.0", "v0.2", "v0.2.0-test", "v01.2.0", "v0.2.0\n", "", None, 3])
def test_anything_but_vx_y_z_is_malformed(text):
    refusal(lambda: update.parse_version(text), "malformed")


def test_the_comparison_is_numeric_not_lexical():
    assert update.parse_version("v0.10.0") > update.parse_version("v0.9.0")


def test_the_running_version_is_the_first_token_of_the_build_file(tmp_path):
    f = tmp_path / "scoreboard-build"
    f.write_text("v0.1.6 · 2026-09-20 · abc1234\n")
    assert update.running_version(f) == "v0.1.6"
    f.write_text("development build\n")
    refusal(lambda: update.running_version(f), "malformed")


# --- the verifier ----------------------------------------------------------
def test_a_manifest_signed_by_the_installed_key_verifies_and_checks(key):
    m = verified(Release(key), key)
    assert (m.version, m.channel, m.key_id) == ("v0.2.0", "stable", "release-2026-1")
    assert m.boot.raw_size == LAYOUT.boot_size and m.root.raw_size == LAYOUT.root_size


def test_a_tampered_byte_fails_the_signature(key):
    r = Release(key)
    tampered = r.manifest.replace(b'"v0.2.0"', b'"v0.2.1"', 1)
    assert tampered != r.manifest
    refusal(lambda: verify.verify_signature(tampered, r.signature, key.ring()), "signature")


def test_a_truncated_manifest_fails_the_signature(key):
    r = Release(key)
    refusal(lambda: verify.verify_signature(r.manifest[:-1], r.signature, key.ring()), "signature")


def test_an_unsigned_manifest_is_refused(key):
    r = Release(key)
    refusal(lambda: verify.verify_signature(r.manifest, b"", key.ring()), "signature")
    refusal(lambda: verify.verify_signature(r.manifest, b"not base64!", key.ring()), "signature")


def test_a_manifest_signed_by_a_key_the_panel_does_not_hold_is_refused(key):
    other = Key("release-2026-2")
    r = Release(other)
    refusal(lambda: verify.verify_signature(r.manifest, r.signature, key.ring()), "signature")


def test_no_installed_key_is_a_key_refusal_not_a_signature_one(key):
    r = Release(key)
    refusal(lambda: verify.verify_signature(r.manifest, r.signature, []), "key")


def test_a_two_key_keyring_accepts_either_signer_during_a_rotation(key):
    new = Key("release-2026-2")
    ring = key.ring() + new.ring()
    assert verify.verify_signature(Release(key).manifest, Release(key).signature, ring) == "release-2026-1"
    r = Release(new)
    assert verify.verify_signature(r.manifest, r.signature, ring) == "release-2026-2"


def test_key_id_must_name_the_key_that_verified(key):
    r = Release(key, keyId="release-2026-2")
    refusal(lambda: verified(r, key), "key")


def test_keys_are_loaded_from_pem_files_named_by_key_id(tmp_path, key):
    (tmp_path / "release-2026-1.pem").write_bytes(key.pem())
    (tmp_path / "README.txt").write_text("not a key\n")
    (tmp_path / "junk.pem").write_text("-----BEGIN PUBLIC KEY-----\nnope\n-----END PUBLIC KEY-----\n")
    ring = verify.load_keys(tmp_path)
    assert [k for k, _ in ring] == ["release-2026-1"]
    r = Release(key)
    assert verify.verify_signature(r.manifest, r.signature, ring) == "release-2026-1"


def test_a_missing_key_directory_is_an_empty_keyring(tmp_path):
    assert verify.load_keys(tmp_path / "absent") == []


def test_the_repository_ships_no_private_key_beside_the_public_ones():
    d = Path(__file__).resolve().parents[1] / "certs" / "release-signing"
    for p in d.glob("*") if d.exists() else []:
        assert b"PRIVATE KEY" not in p.read_bytes(), p


@pytest.mark.parametrize("field, value, reason", [
    ("version", "v0.1.6", "downgrade"),
    ("version", "v0.1.5", "downgrade"),
    ("channel", "test", "channel"),
    ("channel", "beta", "malformed"),
    ("layout", 2, "layout"),
    ("layout", None, "layout"),
    ("expires", "2026-01-01T00:00:00Z", "expired"),
    ("expires", "soon", "malformed"),
    ("payloads.boot.rawSize", LAYOUT.boot_size - 1, "size"),
    ("payloads.root.rawSize", LAYOUT.root_size + 1, "size"),
    ("payloads.root.sha256", "abc", "malformed"),
    ("payloads.root.file", "other.img.xz", "malformed"),
    ("payloads.boot", None, "malformed"),
    ("payloads", None, "malformed"),
    ("keyId", None, "key"),
])
def test_each_manifest_refusal(key, field, value, reason):
    r = Release(key, **{field: value})
    # The signature still verifies: these are refusals of a manifest the
    # signer really did sign, which is the point of checking them at all.
    refusal(lambda: verified(r, key, expected_version=r.version if field != "version" else "v0.2.0"),
            reason if field != "version" else "malformed")
    if field == "version":
        refusal(lambda: verified(r, key, expected_version=value), reason)


def test_a_manifest_for_another_version_than_the_pointer_named_is_malformed(key):
    refusal(lambda: verified(Release(key), key, expected_version="v0.3.0"), "malformed")


def test_the_panels_channel_is_what_the_manifest_is_held_to(key):
    r = Release(key, channel="test")
    assert verified(r, key, channel="test").channel == "test"
    refusal(lambda: verified(Release(key), key, channel="test"), "channel")


def test_an_untrusted_clock_refuses_before_any_date_is_read(key):
    refusal(lambda: verified(Release(key), key, now=None), "clock")


def test_the_expiry_is_exact_to_the_second(key):
    r = Release(key, expires="2026-10-10T00:00:00Z")
    from datetime import datetime, timezone
    at = int(datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp())
    assert verified(r, key, now=at - 1).version == "v0.2.0"
    refusal(lambda: verified(r, key, now=at), "expired")


def test_the_manifest_is_not_parsed_before_it_verifies(key):
    # json.loads of the bytes is what parsing means; a manifest that is not
    # even JSON must be refused as a signature, never as malformed, because
    # the signature check comes first and sees only bytes.
    refusal(lambda: verify.verify_signature(b"{not json", key.sign(b"other"), key.ring()), "signature")


def test_latest_json_yields_only_a_version():
    assert verify.pointer_version(b'{"version": "v0.2.0", "url": "x"}') == "v0.2.0"
    refusal(lambda: verify.pointer_version(b'{"version": "0.2.0"}'), "malformed")
    refusal(lambda: verify.pointer_version(b"[]"), "malformed")
    refusal(lambda: verify.pointer_version(b"nope"), "malformed")


# --- slots and the bootloader's values ------------------------------------
def test_slot_arithmetic_from_device_tree_bytes(tmp_path):
    (tmp_path / "partition").write_bytes(b"\x00\x00\x00\x02")
    (tmp_path / "tryboot").write_bytes(b"\x00\x00\x00\x00")
    assert update.read_u32("partition", tmp_path) == 2
    assert update.running_slot(update.read_u32("partition", tmp_path)) == "a"
    assert update.other_slot("a") == "b"
    (tmp_path / "partition").write_bytes(b"\x00\x00\x00\x03")
    assert update.running_slot(update.read_u32("partition", tmp_path)) == "b"
    assert update.read_u32("rsts", tmp_path) is None
    (tmp_path / "rsts").write_bytes(b"\x00\x00\x10")
    assert update.read_u32("rsts", tmp_path) is None
    refusal(lambda: update.running_slot(None), "malformed")
    refusal(lambda: update.running_slot(1), "malformed")


def test_the_root_partition_is_read_from_the_cmdlines_partuuid(tmp_path):
    f = tmp_path / "cmdline"
    f.write_text("console=tty1 root=PARTUUID=deadbeef-05 rootfstype=ext4 ro rootwait\n")
    assert update.cmdline_root_partition(f) == 5
    f.write_text("console=tty1 root=/dev/mmcblk0p2 ro\n")
    assert update.cmdline_root_partition(f) is None


def test_the_write_unit_refuses_both_halves_of_the_running_slot(tmp_path):
    a, b = update.slot("a", tmp_path), update.slot("b", tmp_path)
    update.refuse_running(b, 2, 5)
    refusal(lambda: update.refuse_running(a, 2, 5), "running")
    refusal(lambda: update.refuse_running(a, 3, 5), "running")   # the root half alone
    refusal(lambda: update.refuse_running(a, 2, 6), "running")   # the boot half alone
    refusal(lambda: update.refuse_running(b, None, 5), "running")
    refusal(lambda: update.refuse_running(b, 2, None), "running")


def test_the_device_names_are_the_ones_the_unit_drop_ins_allow():
    # The roots are logical partitions 5 and 6 (tools/image-layout.sh), not
    # design 4.1's 4 and 5: partition 4 is the extended container.
    assert (update.slot("a").boot_dev, update.slot("a").root_dev) == (Path("/dev/mmcblk0p2"), Path("/dev/mmcblk0p5"))
    assert (update.slot("b").boot_dev, update.slot("b").root_dev) == (Path("/dev/mmcblk0p3"), Path("/dev/mmcblk0p6"))


# --- records -----------------------------------------------------------------
def test_records_are_written_atomically_and_read_back(records):
    records.write_json("trial.json", {"outcome": "pending"})
    assert records.read_json("trial.json") == {"outcome": "pending"}
    assert not records.path("trial.json.new").exists()
    assert records.read_json("absent.json") is None
    records.path("failed.json").write_text("{not json")
    with pytest.raises(ValueError):
        records.read_json("failed.json")


def test_records_are_readable_by_the_scoreboard_user_under_the_units_umask(records):
    # Every updater unit runs under UMask=077; scoreboard.service runs as
    # another user and reads failed.json and refused.json for its status
    # subscriptions (design 8.1). A record left 0600 is a PermissionError
    # there and a failure that never reaches Home; design 7.4 says 0644.
    old = os.umask(0o077)
    try:
        records.write_json("failed.json", {"v0.2.0": {"at": 1, "reason": "hung"}})
        records.write_bytes("head-b.bin", b"h")
    finally:
        os.umask(old)
    for name in ("failed.json", "head-b.bin"):
        assert stat.S_IMODE(records.path(name).stat().st_mode) == 0o644, name


def test_the_channel_file_is_absent_means_stable_and_only_known_names_count(records):
    assert records.channel() == "stable"
    records.path("channel").write_text("test\n")
    assert records.channel() == "test"
    records.path("channel").write_text("prod\n")
    assert records.channel() == "stable"


def test_status_topics_carry_the_version_the_last_failure_and_a_key_refusal(records):
    assert update.status_topics("panel-1", "v0.1.6", records) == ["scoreboard/panel-1/status/running/v0.1.6"]
    records.mark_failed("v0.2.0", "unhealthy", 10)
    records.mark_failed("v0.2.1", "hung", 20)
    records.write_json("refused.json", {"reason": "key", "at": 30})
    assert update.status_topics("panel-1", "v0.1.6", records) == [
        "scoreboard/panel-1/status/running/v0.1.6",
        "scoreboard/panel-1/status/failed/v0.2.1",
        "scoreboard/panel-1/status/refused/key",
    ]
    records.write_json("refused.json", {"reason": "expired"})
    assert len(update.status_topics("panel-1", "v0.1.6", records)) == 2


# --- the write unit --------------------------------------------------------------
def write_unit(card: FakeCard, records: Records, key: Key, release: Release, *, quiet=True, files=None,
               booted=2, root=5, reboots=None, now=NOW, channel="stable"):
    transport = FakeTransport(release.files() if files is None else files)
    reboots = [] if reboots is None else reboots
    unit = writer.WriteUnit(card.slot("b"), records, transport=transport, keys=key.ring(), running=RUNNING,
                            channel=channel, booted_partition=booted, root_partition=root,
                            quiet=(lambda: quiet) if isinstance(quiet, bool) else quiet,
                            reboot=reboots.append, now=lambda: now, layout=LAYOUT)
    unit.reboots, unit.transport = reboots, transport
    return unit


def request(records, mode, version="v0.2.0", slot="b", release=None):
    records.write_json("request.json", {"mode": mode, "slot": slot, "version": version})
    if release is not None:
        records.write_bytes("manifest.json", release.manifest)
        records.write_bytes("manifest.sig", release.signature)


def test_stage_writes_the_root_then_the_boot_and_keeps_the_head_on_state(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    unit = write_unit(card, records, key, r, quiet=False)
    assert unit.run() == "stage"
    assert card.slot().root_dev.read_bytes() == r.root_raw
    # The invariant: the head on the card is zero, the head file holds the
    # bytes, and the rest of the boot partition is the payload's.
    assert card.head() == bytes(LAYOUT.head)
    assert records.path("head-b.bin").read_bytes() == r.boot_raw[:LAYOUT.head]
    assert card.slot().boot_dev.read_bytes()[LAYOUT.head:] == r.boot_raw[LAYOUT.head:]
    staged = records.read_json("staged.json")
    assert (staged["version"], staged["slot"]) == ("v0.2.0", "b")
    assert unit.reboots == []
    assert not records.path("request.json").exists(), "the request is consumed"
    root_url, boot_url = unit.transport.opened
    assert root_url.endswith("root.img.xz") and boot_url.endswith("boot.img.xz"), "root before boot"


def test_stage_continues_into_arm_while_the_window_is_open(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    unit = write_unit(card, records, key, r)
    unit.run()
    assert card.head() == r.boot_raw[:LAYOUT.head], "after arm the head is the payload's"
    assert card.slot().boot_dev.read_bytes() == r.boot_raw
    trial = records.read_json("trial.json")
    assert (trial["version"], trial["slot"], trial["attempts"], trial["outcome"]) == ("v0.2.0", "b", 1, "pending")
    assert unit.reboots == ["0 tryboot"]


def test_arm_records_the_trial_before_it_writes_the_head(tmp_path, records, key, monkeypatch):
    # Design 7.3 step 7 says head, then trial.json; the code does the
    # reverse, because a power cut between them must leave something the
    # next boot can act on. A pending trial over a zero head is attributed
    # as a power-on return; a bootable head with no record is an untried
    # image the walk can reach until the next day's arm, which breaks the
    # minutes-long window of design 5.3.
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    write_unit(card, records, key, r, quiet=False).run()
    at_the_cut = {}

    def cut(dev, head):
        at_the_cut["trial"] = records.read_json("trial.json")
        at_the_cut["head"] = card.head()
        raise OSError("the card went away")
    monkeypatch.setattr(writer, "write_head", cut)
    request(records, "arm", release=r)
    unit = write_unit(card, records, key, r)
    refusal(unit.run, "device")
    # What a cut at that instant would have left behind.
    assert (at_the_cut["trial"]["outcome"], at_the_cut["trial"]["attempts"]) == ("pending", 1)
    assert at_the_cut["head"] == bytes(LAYOUT.head)
    assert unit.reboots == []
    # The process survived to undo it: the head is zero and the trial is
    # open for a re-arm, counted, rather than pending with nothing to
    # reboot.
    assert card.head() == bytes(LAYOUT.head)
    trial = records.read_json("trial.json")
    assert (trial["outcome"], trial["attempts"]) == ("unattributed", 1)
    assert records.read_json("staged.json")["version"] == "v0.2.0"


class RefusingReboot(list):
    """A systemctl reboot that comes back with an error, as
    subprocess.run(check=True) reports one."""

    def append(self, argument):
        super().append(argument)
        raise subprocess.CalledProcessError(1, ["systemctl", "reboot", f"--reboot-argument={argument}"])


def test_a_reboot_that_fails_disarms_the_slot_and_leaves_the_trial_open(tmp_path, records, key):
    # The head is written and the record is pending when systemctl fails.
    # Left like that the slot is bootable to the walk for as long as nobody
    # reboots, and the pending record stops the planner for as long. It is
    # undone the way a failed head write is: head zero, trial unattributed
    # with the arming counted, the stage kept for a re-arm without a fetch.
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    write_unit(card, records, key, r, quiet=False).run()
    request(records, "arm", release=r)
    unit = write_unit(card, records, key, r, reboots=RefusingReboot())
    e = refusal(unit.run, "reboot")
    assert unit.reboots == ["0 tryboot"], "the reboot was asked for"
    assert isinstance(e.__cause__, subprocess.CalledProcessError)
    assert card.head() == bytes(LAYOUT.head)
    trial = records.read_json("trial.json")
    assert (trial["outcome"], trial["attempts"]) == ("unattributed", 1)
    assert records.read_json("staged.json")["version"] == "v0.2.0"
    assert records.path("head-b.bin").exists()


def test_a_payload_fetch_uses_a_short_socket_timeout_under_the_long_deadline(tmp_path, records, key):
    # The 30 minute deadline is checked between reads; a mirror that stalls
    # without closing must not hold one read() for all of it, or the unit
    # runs past TimeoutStartSec and SIGTERM skips the failure count.
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    unit = write_unit(card, records, key, r, quiet=False)
    unit.run()
    assert unit.transport.timeouts == [update.PAYLOAD_SOCKET_TIMEOUT_S] * 2
    assert update.PAYLOAD_SOCKET_TIMEOUT_S < update.PAYLOAD_DEADLINE_S


def test_arm_rehashes_the_slot_from_the_card_not_the_record(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    write_unit(card, records, key, r, quiet=False).run()
    # A byte of the staged root changes on the card; the record still says
    # it is fine. The bytes win.
    with open(card.slot().root_dev, "r+b") as f:
        f.seek(LAYOUT.root_size // 2)
        f.write(b"\xff")
    request(records, "arm", release=r)
    unit = write_unit(card, records, key, r)
    refusal(unit.run, "stale")
    assert card.head() == bytes(LAYOUT.head)
    assert unit.reboots == []
    assert records.read_json("staged.json") is None and not records.path("head-b.bin").exists()


def test_arm_with_the_head_substituted_matches_the_manifest(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    unit = write_unit(card, records, key, r, quiet=False)
    unit.run()
    assert unit.rehash(verified(r, key))


def test_arm_stands_down_when_the_window_has_closed(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    write_unit(card, records, key, r, quiet=False).run()
    request(records, "arm", release=r)
    unit = write_unit(card, records, key, r, quiet=False)
    unit.run()
    assert card.head() == bytes(LAYOUT.head) and unit.reboots == []
    assert records.read_json("staged.json")["version"] == "v0.2.0"


def test_invalidate_zeroes_the_head_and_touches_nothing_else(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    write_unit(card, records, key, r).run()
    assert card.head() != bytes(LAYOUT.head)
    request(records, "invalidate")
    unit = write_unit(card, records, key, r, files={})
    assert unit.run() == "invalidate"
    assert card.head() == bytes(LAYOUT.head)
    assert card.slot().boot_dev.read_bytes()[LAYOUT.head:] == r.boot_raw[LAYOUT.head:]
    assert card.slot().root_dev.read_bytes() == r.root_raw
    assert records.path("head-b.bin").exists(), "a re-trial is a re-arm, not a download"
    assert unit.transport.opened == []


def test_a_re_arm_counts_the_arming_and_stops_at_the_cap(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    write_unit(card, records, key, r, quiet=False).run()
    for attempt in (1, 2, 3):
        if attempt > 1:
            trial = records.read_json("trial.json")
            trial["outcome"] = "unattributed"
            records.write_json("trial.json", trial)
        request(records, "arm", release=r)
        unit = write_unit(card, records, key, r)
        unit.run()
        assert records.read_json("trial.json")["attempts"] == attempt and unit.reboots == ["0 tryboot"]
    trial = records.read_json("trial.json")
    trial["outcome"] = "unattributed"
    records.write_json("trial.json", trial)
    request(records, "arm", release=r)
    unit = write_unit(card, records, key, r)
    refusal(unit.run, "cap")
    assert unit.reboots == [] and records.failed()["v0.2.0"]["reason"] == "unattributed"


@pytest.mark.parametrize("what", ["short", "long", "corrupt", "wrong_xz_hash", "wrong_raw_hash"])
def test_a_bad_payload_leaves_the_slot_non_bootable_and_counts_a_failure(tmp_path, records, key, what):
    card, r = FakeCard(tmp_path), Release(key)
    files = r.files()
    url = next(u for u in files if u.endswith("root.img.xz"))
    if what == "short":
        files[url] = files[url][:-10]
    elif what == "long":
        files[url] = files[url] + b"\0"
    elif what == "corrupt":
        files[url] = files[url][:40] + bytes(8) + files[url][48:]
    elif what == "wrong_xz_hash":
        r = Release(key, **{"payloads.root.sha256": "0" * 64})
        files = r.files()
    else:
        r = Release(key, **{"payloads.root.rawSha256": "0" * 64})
        files = r.files()
    request(records, "stage", release=r)
    unit = write_unit(card, records, key, r, files=files)
    with pytest.raises(Refused) as e:
        unit.run()
    assert e.value.reason in ("size", "hash", "download"), e.value
    assert card.head() == bytes(LAYOUT.head)
    assert card.slot().root_dev.read_bytes()[:LAYOUT.head] == bytes(LAYOUT.head)
    assert not records.path("head-b.bin").exists() and records.read_json("staged.json") is None
    assert records.read_json("downloads.json") == {"v0.2.0": 1}
    assert unit.reboots == []


def test_three_download_failures_mark_the_version_failed(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    files = r.files()
    del files[next(u for u in files if u.endswith("root.img.xz"))]
    for n in (1, 2, 3):
        request(records, "stage", release=r)
        with pytest.raises(Refused):
            write_unit(card, records, key, r, files=files).run()
        assert records.read_json("downloads.json") == {"v0.2.0": n}
    assert records.failed()["v0.2.0"]["reason"] == "download"


def test_a_payload_longer_than_size_is_a_failure_not_a_truncation(tmp_path, key):
    r = Release(key)
    dev = tmp_path / "dev"
    dev.write_bytes(bytes(LAYOUT.root_size))
    refusal(lambda: writer.stream_payload(io.BytesIO(r.root_xz + b"x"), verified(r, key).root, dev, LAYOUT), "size")


class Measuring:
    """Every os.write and sha256.update the write unit makes, by size."""

    def __init__(self, monkeypatch):
        self.largest = 0
        real_write, real_sha256 = writer.os.write, writer.hashlib.sha256
        measure = self

        def write(fd, data):
            measure.largest = max(measure.largest, len(data))
            return real_write(fd, data)

        class Sha256:
            def __init__(self, *a):
                self._h = real_sha256(*a)

            def update(self, data):
                measure.largest = max(measure.largest, len(data))
                self._h.update(data)

            def hexdigest(self):
                return self._h.hexdigest()

        # os.write is patched where it lives (nothing else in the test
        # calls it); sha256 only as the writer module sees it, since the
        # fixtures hash with the real one.
        monkeypatch.setattr(writer.os, "write", write)
        monkeypatch.setattr(writer, "hashlib", types.SimpleNamespace(sha256=Sha256))


def test_a_mostly_zero_payload_is_decompressed_in_pieces_no_larger_than_a_chunk(tmp_path, records, key, monkeypatch):
    # A real root is 3 GiB of ext4 with about a gigabyte of zeroed free
    # space (design 4.1), and xz packs a gigabyte of zeros into a few
    # kilobytes, so one chunk of the download can stand for all of it.
    # Here the whole root is zeros and the chunk is shrunk to 4 KiB, so
    # that a decompress() with no bound would hand back the entire 48 KiB
    # root from the first read; the test is that nothing that large is
    # ever written or hashed in one call, because on a 1 GB panel the
    # unbounded version is an OOM kill that skips stage()'s failure count.
    monkeypatch.setattr(writer, "CHUNK", 4096)
    measure = Measuring(monkeypatch)
    card, r = FakeCard(tmp_path), Release(key, root_raw=bytes(LAYOUT.root_size))
    assert len(r.root_xz) < 4096, "the whole compressed root fits in one chunk"
    request(records, "stage", release=r)
    write_unit(card, records, key, r, quiet=False).run()
    assert card.slot().root_dev.read_bytes() == r.root_raw
    assert card.slot().boot_dev.read_bytes()[LAYOUT.head:] == r.boot_raw[LAYOUT.head:]
    assert records.read_json("staged.json")["version"] == "v0.2.0"
    assert 0 < measure.largest <= 4096, f"a single write or hash update of {measure.largest} bytes"


def test_a_zero_payload_that_decompresses_past_raw_size_is_refused_a_chunk_at_a_time(tmp_path, key, monkeypatch):
    # The rawSize counter used to run after the allocation it exists to
    # bound; now the refusal comes at the first piece past the size.
    monkeypatch.setattr(writer, "CHUNK", 4096)
    measure = Measuring(monkeypatch)
    r = Release(key, root_raw=bytes(LAYOUT.root_size * 4), **{"payloads.root.rawSize": LAYOUT.root_size})
    dev = tmp_path / "dev"
    dev.write_bytes(bytes(LAYOUT.root_size))
    refusal(lambda: writer.stream_payload(io.BytesIO(r.root_xz), verified(r, key).root, dev, LAYOUT), "size")
    assert measure.largest <= 4096


class RaisingTransport:
    """A response whose read raises something that is neither Refused nor
    OSError, as http.client.IncompleteRead or a MemoryError would."""

    def __init__(self, exc):
        self.exc, self.opened = exc, []

    def open(self, url, timeout):
        self.opened.append(url)
        transport = self

        class Response:
            def read(self, n):
                raise transport.exc

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Response()


def test_a_download_that_fails_in_any_way_is_counted_and_leaves_both_heads_zero(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    unit = write_unit(card, records, key, r)
    unit.transport = RaisingTransport(Exception("the socket vanished mid-body"))
    e = refusal(unit.run, "download")
    assert "the socket vanished" in str(e)
    assert card.head() == bytes(LAYOUT.head)
    assert card.slot().root_dev.read_bytes()[:LAYOUT.head] == bytes(LAYOUT.head)
    assert not records.path("head-b.bin").exists() and records.read_json("staged.json") is None
    assert records.read_json("downloads.json") == {"v0.2.0": 1}
    assert unit.reboots == []


def test_a_memory_error_during_the_download_is_counted_too(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    unit = write_unit(card, records, key, r)
    unit.transport = RaisingTransport(MemoryError())
    refusal(unit.run, "download")
    assert records.read_json("downloads.json") == {"v0.2.0": 1}
    assert card.head() == bytes(LAYOUT.head)


def test_arm_asks_about_the_quiet_window_again_after_the_rehash(tmp_path, records, key):
    # The rehash reads the whole slot, minutes on a real card, and the
    # head write that follows is the one that makes the slot bootable. A
    # game chosen during the rehash closes the window between the two.
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    write_unit(card, records, key, r, quiet=False).run()
    request(records, "arm", release=r)
    answers = iter([True, False])
    unit = write_unit(card, records, key, r, quiet=lambda: next(answers))
    unit.run()
    assert card.head() == bytes(LAYOUT.head) and unit.reboots == []
    assert records.read_json("trial.json") is None
    assert records.read_json("staged.json")["version"] == "v0.2.0" and records.path("head-b.bin").exists()


def test_a_redirect_below_https_is_refused_before_it_is_followed():
    import urllib.request
    handler = writer.HttpsOnlyRedirects()
    req = urllib.request.Request("https://images.example/images/v0.2.0/root.img.xz")
    refusal(lambda: handler.redirect_request(req, None, 302, "Found", {}, "http://images.example/root.img.xz"), "malformed")
    followed = handler.redirect_request(req, None, 302, "Found", {}, "https://cdn.example/root.img.xz")
    assert followed.full_url == "https://cdn.example/root.img.xz"
    refusal(lambda: writer.HttpTransport("t").open("http://images.example/latest.json", 1), "malformed")


def test_the_write_unit_verifies_the_manifest_again_itself(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    records.write_bytes("manifest.sig", Key().sign(r.manifest))
    unit = write_unit(card, records, key, r)
    refusal(unit.run, "signature")
    assert unit.transport.opened == []


def test_the_write_unit_refuses_a_request_for_the_running_slot(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", release=r)
    refusal(write_unit(card, records, key, r, booted=3, root=6).run, "running")
    assert card.head() != bytes(LAYOUT.head), "nothing was touched"


def test_the_write_unit_refuses_a_request_for_the_other_slot(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key)
    request(records, "stage", slot="a", release=r)
    refusal(write_unit(card, records, key, r).run, "malformed")


def test_the_write_unit_refuses_a_manifest_for_another_channel(tmp_path, records, key):
    card, r = FakeCard(tmp_path), Release(key, channel="test")
    request(records, "stage", release=r)
    refusal(write_unit(card, records, key, r).run, "channel")


# --- the planner ---------------------------------------------------------------------
STATUS = {"at": NOW, "showing": "off", "sleeping": False, "nextEventAt": None}


@pytest.mark.parametrize("status, age, active, quiet", [
    (STATUS, 5, True, True),
    (dict(STATUS, showing="game"), 5, True, False),
    (dict(STATUS, showing="message"), 5, True, False),
    (dict(STATUS, showing="countdown"), 5, True, False),
    (dict(STATUS, sleeping=True), 5, True, False),
    (dict(STATUS, nextEventAt=NOW + update.QUIET_LEAD_S - 1), 5, True, False),
    (dict(STATUS, nextEventAt=NOW + update.QUIET_LEAD_S + 1), 5, True, True),
    (dict(STATUS, nextEventAt="soon"), 5, True, False),
    (STATUS, update.STATUS_FRESH_S + 1, True, False),
    (None, None, True, False),
    (None, None, False, True),
    (dict(STATUS, showing="game"), 5, False, True),
    (STATUS, 5, None, False),
    ({}, 5, True, False),
])
def test_the_quiet_window_truth_table(status, age, active, quiet):
    assert planner.quiet_window(status, age, active, NOW)[0] is quiet


def test_the_status_file_is_aged_by_its_own_stamp(tmp_path):
    f = tmp_path / "status.json"
    f.write_text(json.dumps(dict(STATUS, at=NOW - 30)))
    assert planner.read_status_file(f, lambda n: True, NOW)[0]
    f.write_text(json.dumps(dict(STATUS, at=NOW - 300)))
    assert not planner.read_status_file(f, lambda n: True, NOW)[0]
    f.unlink()
    assert not planner.read_status_file(f, lambda n: True, NOW)[0]
    assert planner.read_status_file(f, lambda n: False, NOW)[0]


def make_planner(records, key, release, *, tryboot=0, booted=2, clock=True, units=None, quiet=(True, "quiet"),
                 files=None, requests=None, now=NOW, autoboot=None):
    units = {} if units is None else units
    requests = [] if requests is None else requests
    if autoboot is None:
        # A card whose default is the booted slot, as a committed one is.
        autoboot = setup_dir(records.dir.parent.parent, default=booted, trial=5 - booted)
    p = planner.Planner(records, transport=FakeTransport(release.files() if files is None else files), keys=key.ring(),
                        running=RUNNING, booted_partition=booted, tryboot=tryboot, clock_trusted=clock,
                        unit_active=lambda name: units.get(name, False), read_status=lambda: quiet,
                        now=lambda: now, start_unit=requests.append, layout=LAYOUT, autoboot=autoboot)
    p.requests = requests
    return p


def test_a_newer_release_is_staged_into_the_inactive_slot(records, key):
    p = make_planner(records, key, Release(key))
    assert p.plan().startswith("staging v0.2.0 into slot b")
    assert records.read_json("request.json") == {"mode": "stage", "slot": "b", "version": "v0.2.0"}
    assert p.requests == ["scoreboard-update@b.service"]
    assert records.path("manifest.json").read_bytes() == Release(key).manifest


def test_the_inactive_slot_follows_the_booted_partition(records, key):
    p = make_planner(records, key, Release(key), booted=3)
    assert "slot a" in p.plan()


def test_up_to_date_is_one_line_and_nothing_else(records, key):
    p = make_planner(records, key, Release(key, version="v0.1.6"))
    assert p.plan().startswith("up to date")
    assert p.requests == [] and not records.path("request.json").exists()
    assert len(p.transport.opened) == 1, "one fetch a day"


def test_a_downgrade_in_latest_json_is_not_fetched_further(records, key):
    p = make_planner(records, key, Release(key, version="v0.1.5"))
    assert p.plan().startswith("up to date")
    assert len(p.transport.opened) == 1


@pytest.mark.parametrize("kw, why", [
    (dict(tryboot=1), "trial boot"),
    (dict(tryboot=None), "unreadable"),
    (dict(clock=False), "clock"),
    (dict(units={"scoreboard-update@a.service": True}), "active"),
    (dict(units={"scoreboard-update@b.service": None}), "active"),
    (dict(quiet=(False, "the panel is showing 'game'")), "showing"),
])
def test_each_precondition_stops_the_planner_before_any_fetch(records, key, kw, why):
    p = make_planner(records, key, Release(key), **kw)
    out = p.plan()
    assert out.startswith("not planning") and why in out, out
    assert p.transport.opened == [] and p.requests == []


@pytest.mark.parametrize("content, why", [
    (b"[all]\ntryboot_a_b=1\nboot_part", "cannot be read"),                  # torn by a power cut
    (b"[all]\ntryboot_a_b=1\nboot_partition=3\n[tryboot]\nboot_partition=2\n", "defaults to partition 3"),
    (None, "cannot be read"),                                                # not there at all
])
def test_an_autoboot_the_commit_would_refuse_stops_the_planner_before_any_fetch(records, key, tmp_path, content, why):
    # commit() refuses a file it did not write or one whose default is
    # not the running slot. Found only then, the panel has downloaded the
    # release, gone dark twice, and put an "unhealthy" line in failed.json
    # against a version that was never at fault.
    f = tmp_path / "setup" / "autoboot.txt"
    f.parent.mkdir()
    if content is not None:
        f.write_bytes(content)
    p = make_planner(records, key, Release(key), booted=2, autoboot=f)
    out = p.plan()
    assert out.startswith("not planning") and "autoboot.txt" in out and why in out, out
    assert p.transport.opened == [] and p.requests == []


def test_a_pending_trial_stops_the_planner(records, key):
    records.write_json("trial.json", {"version": "v0.2.0", "slot": "b", "attempts": 1, "outcome": "pending"})
    p = make_planner(records, key, Release(key))
    assert "pending" in p.plan() and p.transport.opened == []


def test_a_failed_version_is_never_fetched_again(records, key):
    records.mark_failed("v0.2.0", "unhealthy", NOW - 100)
    p = make_planner(records, key, Release(key))
    assert "already failed" in p.plan()
    assert len(p.transport.opened) == 1 and p.requests == []


def test_a_staged_version_is_armed_not_downloaded_again(records, key):
    records.write_json("staged.json", {"version": "v0.2.0", "slot": "b", "expires": NOW + 100, "at": NOW - 100})
    p = make_planner(records, key, Release(key))
    assert p.plan().startswith("arming the staged v0.2.0")
    assert records.read_json("request.json")["mode"] == "arm"
    assert len(p.transport.opened) == 1


def test_an_open_unattributed_trial_is_re_armed_without_a_fetch(records, key):
    records.write_json("staged.json", {"version": "v0.2.0", "slot": "b", "expires": NOW + 100})
    records.write_json("trial.json", {"version": "v0.2.0", "slot": "b", "attempts": 1, "outcome": "unattributed"})
    p = make_planner(records, key, Release(key))
    assert p.plan().startswith("re-arming v0.2.0")
    assert p.transport.opened == [] and records.read_json("request.json")["mode"] == "arm"


def test_the_third_unattributed_arming_closes_the_version_instead(records, key):
    records.write_json("staged.json", {"version": "v0.2.0", "slot": "b", "expires": NOW + 100})
    records.write_json("trial.json", {"version": "v0.2.0", "slot": "b", "attempts": 3, "outcome": "unattributed"})
    p = make_planner(records, key, Release(key))
    assert "cap" in p.plan()
    assert records.failed()["v0.2.0"]["reason"] == "unattributed" and p.requests == []


@pytest.mark.parametrize("staged", [
    {"version": "v0.1.6", "slot": "b", "expires": NOW + 100},   # not above the running version
    {"version": "v0.2.0", "slot": "b", "expires": NOW - 1},     # past its manifest's expiry
    {"version": "v0.2.0", "slot": "a", "expires": NOW + 100},   # for the slot that is now running
    {"version": "junk"},
])
def test_a_stale_stage_is_discarded(records, key, staged):
    records.write_json("staged.json", staged)
    records.path("head-b.bin").write_bytes(b"x")
    make_planner(records, key, Release(key)).plan()
    assert records.read_json("staged.json") is None and not records.path("head-b.bin").exists()


@pytest.mark.parametrize("field, value, reason, reported", [
    ("channel", "test", "channel", False),
    ("layout", 2, "layout", False),
    ("expires", "2020-01-01T00:00:00Z", "expired", False),
    ("payloads.boot.rawSize", 1, "size", False),
    ("keyId", "release-2026-9", "key", True),
])
def test_each_refusal_before_a_download(records, key, field, value, reason, reported):
    p = make_planner(records, key, Release(key, **{field: value}))
    out = p.plan()
    assert out == f"refused v0.2.0: {reason}", out
    assert p.requests == [] and not records.path("manifest.json").exists()
    assert (records.read_json("refused.json") is not None) is reported


def test_a_bad_signature_is_refused_and_reported_for_the_site(records, key):
    r = Release(key)
    files = r.files()
    files[next(u for u in files if u.endswith(".sig"))] = Key().sign(r.manifest)
    p = make_planner(records, key, r, files=files)
    assert p.plan() == "refused v0.2.0: signature"
    assert records.read_json("refused.json")["reason"] == "signature"


def test_a_manifest_that_verifies_clears_the_last_signature_or_key_refusal(records, key):
    # Otherwise the panel keeps subscribing to status/refused/<reason> on
    # every connect after the next good release, and Home's rule for a
    # refusal (design 8.3) races two lifecycle events.
    records.write_json("refused.json", {"reason": "signature", "version": "v0.1.9", "at": NOW - 86400})
    p = make_planner(records, key, Release(key))
    assert p.plan().startswith("staging v0.2.0")
    assert records.read_json("refused.json") is None
    # A release that verifies but fails a later check leaves it cleared
    # too: the signature and the key are what the record is about.
    records.write_json("refused.json", {"reason": "key", "version": "v0.1.9", "at": NOW - 86400})
    p = make_planner(records, key, Release(key, version="v0.2.1", channel="test"))
    assert p.plan() == "refused v0.2.1: channel"
    assert records.read_json("refused.json") is None


def test_a_panel_with_no_key_reports_that_and_installs_nothing(records, key):
    p = make_planner(records, key, Release(key))
    p.keys = []
    assert p.plan() == "refused v0.2.0: key"
    assert records.read_json("refused.json")["reason"] == "key" and p.requests == []


def test_the_test_channel_reads_its_own_pointer_and_stable_never_does(records, key):
    records.path("channel").write_text("test\n")
    r = Release(key, channel="test")
    p = make_planner(records, key, r, files=r.files(pointer="latest-test.json"))
    assert p.plan().startswith("staging v0.2.0")
    assert p.transport.opened[0].endswith("/latest-test.json")


def test_an_oversized_pointer_is_refused(records, key):
    r = Release(key)
    files = r.files()
    files[f"{update.MIRROR}/latest.json"] = b" " * (update.POINTER_LIMIT + 1)
    assert make_planner(records, key, r, files=files).plan() == "refused latest.json: malformed"


# --- the health unit --------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 0.0

    def mono(self):
        return self.t

    def sleep(self, s):
        self.t += s


def setup_dir(tmp_path, default=2, trial=3):
    setup = tmp_path / "setup"
    setup.mkdir(exist_ok=True)
    (setup / "autoboot.txt").write_bytes(health.render_autoboot(default, trial))
    return setup / "autoboot.txt"


def health_unit(records, tmp_path, *, tryboot, booted, rsts=None, marker_after=None, state=True, setup=True,
                autoboot=None, bound=update.HEALTH_BOUND_S):
    clock = Clock()
    marker = tmp_path / "run" / "healthy"
    marker.parent.mkdir(exist_ok=True)
    reboots, started = [], []

    def sleep(s):
        clock.sleep(s)
        if marker_after is not None and clock.t >= marker_after:
            marker.write_text("")

    unit = health.HealthUnit(records, tryboot=tryboot, booted_partition=booted, rsts=rsts,
                             state_mounted=lambda: state, setup_mounted=lambda: setup, marker=marker,
                             autoboot=autoboot or setup_dir(tmp_path), now=lambda: NOW, mono=clock.mono,
                             sleep=sleep, reboot=reboots.append, start_unit=started.append, bound_s=bound)
    unit.reboots, unit.started, unit.clock = reboots, started, clock
    return unit


def pending(records, attempts=1, outcome="pending"):
    records.write_json("trial.json", {"version": "v0.2.0", "slot": "b", "startedAt": NOW - 60,
                                      "attempts": attempts, "outcome": outcome})
    records.write_json("staged.json", {"version": "v0.2.0", "slot": "b", "expires": NOW + 100})
    records.path("head-b.bin").write_bytes(b"h")


def test_a_normal_boot_with_no_trial_exits(records, tmp_path):
    u = health_unit(records, tmp_path, tryboot=0, booted=2)
    assert u.run() == "no trial to attribute"
    assert u.reboots == [] and u.started == [] and u.clock.t == 0


def test_a_healthy_trial_commits_the_autoboot_swap_with_a_read_back(records, tmp_path):
    pending(records)
    u = health_unit(records, tmp_path, tryboot=1, booted=3, marker_after=30)
    assert u.run() == "committed v0.2.0"
    assert u.autoboot.read_bytes() == health.render_autoboot(3, 2)
    assert records.read_json("trial.json")["outcome"] == "committed"
    assert records.read_json("staged.json") is None and not records.path("head-b.bin").exists()
    assert u.reboots == []


def test_an_unhealthy_trial_rolls_back_at_the_bound(records, tmp_path):
    pending(records)
    u = health_unit(records, tmp_path, tryboot=1, booted=3)
    out = u.run()
    assert out.startswith("rolled back") and u.reboots == [None]
    assert u.clock.t >= update.HEALTH_BOUND_S
    assert u.autoboot.read_bytes() == health.render_autoboot(2, 3), "autoboot.txt is untouched"
    assert records.read_json("trial.json")["outcome"] == "rolled-back"


def test_the_health_bound_is_240_seconds():
    assert update.HEALTH_BOUND_S == 240


@pytest.mark.parametrize("kw, why", [
    (dict(state=False), "/state"),
    (dict(setup=False), "/boot/setup"),
    (dict(booted=2), "booted slot a"),
])
def test_a_trial_the_unit_cannot_decide_is_a_rollback_never_an_exit(records, tmp_path, kw, why):
    pending(records)
    u = health_unit(records, tmp_path, tryboot=1, marker_after=1, **dict(dict(booted=3), **kw))
    out = u.run()
    assert out.startswith("rolled back") and why in out and u.reboots == [None]


def test_a_trial_boot_with_an_unreadable_or_missing_trial_record_rolls_back(records, tmp_path):
    u = health_unit(records, tmp_path, tryboot=1, booted=3, marker_after=1)
    assert u.run().startswith("rolled back") and u.reboots == [None]
    records.path("trial.json").write_text("{torn")
    u = health_unit(records, tmp_path, tryboot=1, booted=3, marker_after=1)
    assert "cannot be read" in u.run() and u.reboots == [None]


def test_a_failed_commit_read_back_is_a_rollback(records, tmp_path, monkeypatch):
    pending(records)
    u = health_unit(records, tmp_path, tryboot=1, booted=3, marker_after=1)
    real = update.write_atomically

    def torn(path, data):
        real(path, data[:-3])
    monkeypatch.setattr(health, "write_atomically", torn)
    out = u.run()
    assert "commit failed" in out and u.reboots == [None]
    trial = records.read_json("trial.json")
    # The version was healthy; the file was not. The boot after must not
    # write it into failed.json.
    assert (trial["outcome"], trial["undecided"]) == ("rolled-back", True)


def test_a_commit_refuses_an_autoboot_file_it_did_not_write(tmp_path):
    f = tmp_path / "autoboot.txt"
    f.write_bytes(b"[all]\ntryboot_a_b=1\nboot_partition=2\nboot_delay=1\n[tryboot]\nboot_partition=3\n")
    refusal(lambda: health.commit(f, 3), "malformed")
    f.write_bytes(b"[all]\nboot_partition=2\n[tryboot]\nboot_partition=3\n")
    refusal(lambda: health.commit(f, 3), "malformed")
    f.write_bytes(health.render_autoboot(2, 3))
    refusal(lambda: health.commit(f, 2), "malformed")
    assert f.read_bytes() == health.render_autoboot(2, 3)


def test_the_boot_after_a_trial_invalidates_the_slot_before_deciding(records, tmp_path):
    pending(records, outcome="rolled-back")
    u = health_unit(records, tmp_path, tryboot=0, booted=2, marker_after=10)
    u.run()
    assert records.read_json("request.json") == {"mode": "invalidate", "slot": "b", "version": "v0.2.0"}
    assert u.started == ["scoreboard-update@b.service"]


def test_a_rolled_back_trial_with_the_old_slot_healthy_is_the_versions_fault(records, tmp_path):
    pending(records, outcome="rolled-back")
    u = health_unit(records, tmp_path, tryboot=0, booted=2, marker_after=10)
    assert u.run() == "failed v0.2.0: unhealthy"
    assert records.failed()["v0.2.0"]["reason"] == "unhealthy"
    assert records.read_json("staged.json") is None and not records.path("head-b.bin").exists()


def test_a_rolled_back_record_on_its_own_slot_is_a_commit_whose_read_back_failed(records, tmp_path):
    # commit() wrote the swap, the read-back disagreed, the rollback was
    # recorded and the reboot took the default, which the swap had made
    # the trial slot. Attributing that would ask to invalidate the running
    # slot and mark the running, committed version failed; autoboot.txt
    # says it is the default, and it is closed as committed.
    pending(records, outcome="rolled-back")
    u = health_unit(records, tmp_path, tryboot=0, booted=3, autoboot=setup_dir(tmp_path, default=3, trial=2))
    assert u.run() == "committed v0.2.0"
    assert u.started == [] and u.reboots == [] and records.failed() == {}
    assert records.read_json("trial.json")["outcome"] == "committed"
    assert records.read_json("staged.json") is None


def test_a_pending_trial_after_a_watchdog_reset_means_it_hung(records, tmp_path):
    pending(records)
    u = health_unit(records, tmp_path, tryboot=0, booted=2, rsts=health.RSTS_WATCHDOG & 0x40, marker_after=10)
    assert u.run() == "failed v0.2.0: hung"
    assert records.failed()["v0.2.0"]["reason"] == "hung"


def test_a_pending_trial_after_a_power_on_reset_is_an_unattributed_attempt(records, tmp_path):
    pending(records)
    u = health_unit(records, tmp_path, tryboot=0, booted=2, rsts=health.RSTS_POWER_ON, marker_after=10)
    assert u.run().startswith("unattributed attempt 1 of 3")
    assert records.failed() == {}
    trial = records.read_json("trial.json")
    assert (trial["outcome"], trial["attempts"]) == ("unattributed", 1)
    assert records.read_json("staged.json") is not None and records.path("head-b.bin").exists()


def test_an_old_slot_that_is_not_healthy_either_blames_nobody(records, tmp_path):
    pending(records, outcome="rolled-back")
    u = health_unit(records, tmp_path, tryboot=0, booted=2, rsts=health.RSTS_WATCHDOG)
    assert u.run().startswith("unattributed attempt")
    assert records.failed() == {}


def test_the_third_unattributed_return_marks_the_version_failed(records, tmp_path):
    pending(records, attempts=3)
    u = health_unit(records, tmp_path, tryboot=0, booted=2, rsts=health.RSTS_POWER_ON, marker_after=10)
    assert u.run() == "failed v0.2.0: unattributed"
    assert records.failed()["v0.2.0"]["reason"] == "unattributed"
    assert records.read_json("staged.json") is None


def test_the_reset_cause_decode_is_the_documented_bits():
    assert health.reset_cause(health.RSTS_POWER_ON) == "power"
    assert health.reset_cause(0x1020) == "hung"
    assert health.reset_cause(0x0100) == "hung"
    assert health.reset_cause(None) == "power"
    assert health.reset_cause(0) == "power"


def test_a_committed_trial_record_is_not_attributed_again(records, tmp_path):
    records.write_json("trial.json", {"version": "v0.2.0", "slot": "b", "attempts": 1, "outcome": "committed"})
    u = health_unit(records, tmp_path, tryboot=0, booted=3)
    assert u.run() == "no trial to attribute" and u.started == []


def test_an_unreadable_tryboot_on_the_pending_trials_slot_is_a_rollback(records, tmp_path):
    # Design 5.2: a trial boot the unit cannot decide is a rollback, never
    # an exit. With the flag unreadable and a pending trial for the slot
    # this boot is running from, attributing would request an invalidate
    # of the running slot (refused downstream) and leave the panel on the
    # trial slot uncommitted, so that the next power cut changed the
    # version with nobody deciding. The planner already refuses to plan on
    # an unreadable flag; this is the same rule on the other unit.
    pending(records)
    u = health_unit(records, tmp_path, tryboot=None, booted=3, marker_after=1)
    out = u.run()
    assert out.startswith("rolled back") and "tryboot is unreadable" in out
    assert u.reboots == [None] and u.started == [], "no invalidate of the slot it is running"
    assert records.read_json("trial.json")["outcome"] == "rolled-back"
    assert u.autoboot.read_bytes() == health.render_autoboot(2, 3), "autoboot.txt is untouched"


def test_an_unreadable_tryboot_on_the_old_slot_still_attributes(records, tmp_path):
    # The flag says nothing, but the booted partition is the old slot's, so
    # this is the boot after a trial and the trial slot is invalidated and
    # attributed as on any other.
    pending(records, outcome="rolled-back")
    u = health_unit(records, tmp_path, tryboot=None, booted=2, marker_after=10)
    assert u.run() == "failed v0.2.0: unhealthy"
    assert u.started == ["scoreboard-update@b.service"] and u.reboots == []


def test_a_swapped_autoboot_on_the_pending_trials_slot_is_a_commit_that_was_not_recorded(records, tmp_path):
    # commit() rewrote autoboot.txt, then the health unit was cut off
    # before it wrote "committed". This boot is the default boot of the new
    # version: tryboot 0, booted from the trial slot, autoboot.txt naming
    # it. Attributing it would call the running, committed version hung or
    # unattributed; the swap is the evidence and it is closed as committed.
    pending(records)
    u = health_unit(records, tmp_path, tryboot=0, booted=3, autoboot=setup_dir(tmp_path, default=3, trial=2))
    assert u.run() == "committed v0.2.0"
    assert u.started == [] and u.reboots == [] and u.clock.t == 0
    assert records.read_json("trial.json")["outcome"] == "committed"
    assert records.read_json("staged.json") is None and not records.path("head-b.bin").exists()


def test_tryboot_0_on_the_pending_trials_slot_with_autoboot_unswapped_is_a_rollback(records, tmp_path):
    # Booted from the trial slot with the firmware reporting no trial and
    # autoboot.txt still defaulting to the old slot: the only honest
    # reading is a trial boot whose flag was not reported, which is the
    # unreadable case again and gets the same rollback.
    pending(records)
    u = health_unit(records, tmp_path, tryboot=0, booted=3, marker_after=1)
    out = u.run()
    assert out.startswith("rolled back") and "still defaults to the other slot" in out
    assert u.reboots == [None] and u.started == []
    assert records.read_json("trial.json")["outcome"] == "rolled-back"
    assert records.failed() == {}


TORN_AUTOBOOT = b"[all]\ntryboot_a_b=1\nboot_par"


def test_a_torn_autoboot_on_the_trial_slot_is_rewritten_before_the_rollback(records, tmp_path):
    # Booted from the trial slot with tryboot 0 because autoboot.txt is
    # torn and the partition walk landed here (the trial slot is the first
    # bootable partition). A plain reboot would walk here again. The file
    # is rewritten to what it said before the trial, defaulting to the old
    # slot with this one as the tryboot partition, and then the reboot is
    # a rollback; the record says the rollback was not a verdict.
    pending(records)
    torn = setup_dir(tmp_path)
    torn.write_bytes(TORN_AUTOBOOT)
    u = health_unit(records, tmp_path, tryboot=0, booted=3, autoboot=torn, marker_after=1)
    out = u.run()
    assert out.startswith("rolled back") and "autoboot.txt unreadable" in out
    assert u.reboots == [None] and u.started == []
    assert torn.read_bytes() == health.render_autoboot(2, 3)
    trial = records.read_json("trial.json")
    assert (trial["outcome"], trial["undecided"]) == ("rolled-back", True)
    assert records.failed() == {}


def test_the_rollback_on_a_tryboot_0_boot_happens_once_per_record(records, tmp_path, monkeypatch):
    # The rewrite cannot land (SETUP refuses the write), so the reboot
    # walks back onto the trial slot with the same torn file. The unit,
    # run again against it, must not reboot again: that is the loop, one
    # dark reboot per boot until somebody pulls the card. The second run
    # leaves the panel on the trial slot, uncommitted, with the record and
    # failed.json untouched.
    pending(records)
    torn = setup_dir(tmp_path)
    torn.write_bytes(TORN_AUTOBOOT)

    def refused(path, data, mode=0o644):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(health, "write_atomically", refused)
    first = health_unit(records, tmp_path, tryboot=0, booted=3, autoboot=torn, marker_after=1)
    assert first.run().startswith("rolled back")
    assert first.reboots == [None]
    assert torn.read_bytes() == TORN_AUTOBOOT, "the rewrite was refused"
    second = health_unit(records, tmp_path, tryboot=0, booted=3, autoboot=torn, marker_after=1)
    out = second.run()
    assert out.startswith("rollback did not take")
    assert second.reboots == [] and second.started == []
    assert records.read_json("trial.json")["outcome"] == "rolled-back"
    assert records.failed() == {}
    assert torn.read_bytes() == TORN_AUTOBOOT


def test_a_rollback_that_did_not_take_with_autoboot_naming_the_old_slot_is_not_repeated(records, tmp_path):
    # autoboot.txt is whole and names the old slot, the rollback was
    # recorded, and the boot still landed here: the old slot did not boot.
    # Rebooting again would be the same loop with a different cause.
    pending(records, outcome="rolled-back")
    u = health_unit(records, tmp_path, tryboot=0, booted=3, marker_after=1)
    out = u.run()
    assert out.startswith("rollback did not take")
    assert u.reboots == [] and u.started == []
    assert u.autoboot.read_bytes() == health.render_autoboot(2, 3), "a whole file is not rewritten"


def test_an_undecided_rollback_is_counted_against_the_cap_not_the_version(records, tmp_path):
    # The boot after an undecided rollback: the old slot is healthy and
    # rsts reads the software reset the rollback itself caused. Without
    # the flag that reads as "hung"; with it the trial is left open for a
    # re-arm, and the slot is still invalidated first.
    pending(records, outcome="rolled-back")
    trial = records.read_json("trial.json")
    trial["undecided"] = True
    records.write_json("trial.json", trial)
    u = health_unit(records, tmp_path, tryboot=0, booted=2, rsts=health.RSTS_SOFTWARE & 0x100, marker_after=10)
    assert u.run() == "unattributed attempt 1 of 3 for v0.2.0"
    assert u.started == ["scoreboard-update@b.service"]
    assert records.failed() == {}
    assert records.read_json("trial.json")["outcome"] == "unattributed"


def test_the_third_undecided_rollback_closes_the_version_as_unattributed(records, tmp_path):
    pending(records, attempts=3, outcome="rolled-back")
    trial = records.read_json("trial.json")
    trial["undecided"] = True
    records.write_json("trial.json", trial)
    u = health_unit(records, tmp_path, tryboot=0, booted=2, marker_after=10)
    assert u.run() == "failed v0.2.0: unattributed"
    assert records.failed()["v0.2.0"]["reason"] == "unattributed"


# --- the entry point ---------------------------------------------------------------
def test_plan_turns_a_refusal_into_one_journal_line(monkeypatch, caplog, tmp_path):
    from scoreboard.update import __main__ as cli
    monkeypatch.setattr(cli, "Records", lambda: Records(tmp_path))
    monkeypatch.setattr(cli, "state_mounted", lambda: False)
    with caplog.at_level("ERROR", logger="scoreboard.update"):
        assert cli.main(["plan"]) == 1
    assert "STATE is not mounted" in caplog.text
    # STATE mounted, but the bootloader's partition value is unreadable:
    # running_slot's Refused is the same one line, not a traceback.
    monkeypatch.setattr(cli, "state_mounted", lambda: True)
    monkeypatch.setattr(cli, "running_version", lambda path: RUNNING)
    monkeypatch.setattr(cli, "load_keys", lambda: [])
    monkeypatch.setattr(cli, "read_u32", lambda name, tree: {"tryboot": 0}.get(name))
    monkeypatch.setattr(cli, "clock_trusted", lambda: True)
    monkeypatch.setattr(cli, "unit_active", lambda name: False)
    monkeypatch.setattr(cli, "STATUS_FILE", tmp_path / "status.json")
    caplog.clear()
    with caplog.at_level("ERROR", logger="scoreboard.update"):
        assert cli.main(["plan"]) == 1
    assert "not planning: malformed: booted from partition None" in caplog.text


@pytest.mark.parametrize("state, active", [
    ("active", True), ("reloading", True), ("activating", True), ("deactivating", True),
    ("inactive", False), ("failed", False),
])
def test_unit_active_counts_every_state_but_inactive_and_failed(monkeypatch, state, active):
    # A Type=oneshot unit is "activating" for the whole of its ExecStart,
    # which is where a stage, an arm or an invalidate happens; `systemctl
    # is-active` answers no to that state and the planner would plan
    # over a write in progress.
    from scoreboard.update import __main__ as cli
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=state + "\n", stderr="")
    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.unit_active("scoreboard-update@b.service") is active
    assert calls == [["systemctl", "show", "-p", "ActiveState", "--value", "scoreboard-update@b.service"]]


def test_unit_active_is_none_when_systemd_cannot_be_asked(monkeypatch):
    from scoreboard.update import __main__ as cli
    for outcome in (OSError("no systemctl"), subprocess.TimeoutExpired("systemctl", 10)):
        def raises(cmd, **kw):
            raise outcome
        monkeypatch.setattr(cli.subprocess, "run", raises)
        assert cli.unit_active("scoreboard.service") is None
    for rc, out in ((1, "inactive\n"), (0, "")):
        monkeypatch.setattr(cli.subprocess, "run",
                            lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=""))
        assert cli.unit_active("scoreboard.service") is None
