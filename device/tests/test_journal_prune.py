"""The journal prune (device/scoreboard-journal-prune and its unit).

The machine id is transient on the read-only root, so every boot leaves a
journal directory on STATE that journald's own SystemMaxUse never touches.
The script keeps the newest few earlier boots and removes the rest; the
tests run it against a temporary tree with a fake machine id.
"""
import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "device" / "scoreboard-journal-prune"
UNIT = REPO / "device" / "scoreboard-journal-prune.service"

CURRENT = "0123456789abcdef0123456789abcdef"


def ids(n: int) -> list[str]:
    return [f"{i:032x}" for i in range(1, n + 1)]


def make_boot(journal: Path, name: str, age_s: int) -> Path:
    d = journal / name
    d.mkdir()
    (d / "system.journal").write_bytes(b"\0" * 16)
    stamp = time.time() - age_s
    os.utime(d, (stamp, stamp))
    return d


def prune(journal: Path, id_file: Path, keep: int | None = None) -> subprocess.CompletedProcess:
    args = ["sh", str(SCRIPT), str(journal), str(id_file)]
    if keep is not None:
        args.append(str(keep))
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def test_the_script_is_executable_posix_sh():
    assert os.access(SCRIPT, os.X_OK)
    assert SCRIPT.read_text().startswith("#!/bin/sh\n")


def test_keeps_the_running_id_and_the_newest_two_earlier_boots(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    id_file = tmp_path / "machine-id"
    id_file.write_text(CURRENT + "\n")
    old = ids(5)
    for i, name in enumerate(old):
        make_boot(journal, name, age_s=(i + 1) * 3600)  # old[0] is the newest
    make_boot(journal, CURRENT, age_s=0)
    r = prune(journal, id_file)
    assert r.returncode == 0, r.stderr
    left = {p.name for p in journal.iterdir()}
    assert left == {CURRENT, old[0], old[1]}, left
    for name in old[2:]:
        assert name in r.stdout, "each removal is said in the journal"


def test_keep_is_the_default_two_and_the_third_argument_overrides_it(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    id_file = tmp_path / "machine-id"
    id_file.write_text(CURRENT)
    old = ids(4)
    for i, name in enumerate(old):
        make_boot(journal, name, age_s=(i + 1) * 60)
    assert prune(journal, id_file, keep=0).returncode == 0
    assert {p.name for p in journal.iterdir()} == set(), "keep=0 removes every earlier boot"


def test_removes_nothing_when_the_machine_id_is_unreadable(tmp_path):
    # The safe direction is to keep: with no id there is no way to tell this
    # boot's directory from an earlier one.
    journal = tmp_path / "journal"
    journal.mkdir()
    for i, name in enumerate(ids(4)):
        make_boot(journal, name, age_s=(i + 1) * 60)
    for bad in ("", "not-an-id\n", "0123456789abcdef0123456789abcde\n", "0123456789ABCDEF0123456789ABCDEF\n"):
        id_file = tmp_path / "machine-id"
        id_file.write_text(bad)
        r = prune(journal, id_file)
        assert r.returncode == 0, r.stderr
        assert "removing nothing" in r.stderr, bad
        assert len(list(journal.iterdir())) == 4, bad
    r = prune(journal, tmp_path / "does-not-exist")
    assert r.returncode == 0 and len(list(journal.iterdir())) == 4


def test_touches_only_names_shaped_like_a_machine_id(tmp_path):
    # journald's remote/ directory, a stray file, and a name one character
    # short are all left alone, however old they are.
    journal = tmp_path / "journal"
    journal.mkdir()
    id_file = tmp_path / "machine-id"
    id_file.write_text(CURRENT + "\n")
    make_boot(journal, "remote", age_s=10 * 86400)
    make_boot(journal, "0123456789abcdef0123456789abcde", age_s=10 * 86400)
    make_boot(journal, "0123456789abcdef0123456789abcdeg", age_s=10 * 86400)
    stray = journal / "stray.journal"
    stray.write_bytes(b"\0")
    old = ids(3)
    for i, name in enumerate(old):
        make_boot(journal, name, age_s=(i + 1) * 60)
    assert prune(journal, id_file).returncode == 0
    left = {p.name for p in journal.iterdir()}
    assert left == {"remote", "0123456789abcdef0123456789abcde", "0123456789abcdef0123456789abcdeg",
                    "stray.journal", old[0], old[1]}, left


def test_a_missing_journal_directory_is_not_an_error(tmp_path):
    # STATE is nofail; on a boot where it did not mount there is nothing
    # under /var/log/journal and nothing to do.
    id_file = tmp_path / "machine-id"
    id_file.write_text(CURRENT)
    assert prune(tmp_path / "missing", id_file).returncode == 0


def test_the_unit_runs_early_before_the_flush_and_can_write_only_the_journal():
    text = UNIT.read_text()
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))
    assert fields["DefaultDependencies"] == "no"
    assert fields["RequiresMountsFor"] == "/var/log/journal", "the bind from STATE must be up first"
    assert "systemd-journal-flush.service" in fields["Before"], "it must run before this boot's directory is created"
    assert fields["ExecStart"] == "/usr/local/sbin/scoreboard-journal-prune"
    assert fields["Type"] == "oneshot"
    assert fields["ProtectSystem"] == "strict", "an rm -rf as root gets the rest of the card read-only"
    assert fields["ReadWritePaths"] == "/var/log/journal"
    assert fields["WantedBy"] == "sysinit.target", "multi-user.target would be too late for a unit with DefaultDependencies=no"
    assert "Conflicts=shutdown.target" in text
