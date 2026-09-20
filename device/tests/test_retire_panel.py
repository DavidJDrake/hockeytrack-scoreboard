"""tools/retire-panel.sh against a fake `aws`.

The script removes a panel's cloud identity. Everything it does is
irreversible, so what is pinned here is the order, the dry run, and every
reason it should refuse.
"""
import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "retire-panel.sh"
THING = "scoreboard-973pe43q585t"
CERT = "a" * 64
ARN = f"arn:aws:iot:us-east-1:111122223333:cert/{CERT}"

FAKE = r"""#!/usr/bin/env bash
# Logs every call; answers the reads from files in $FAKE_DIR.
echo "$*" >> "$FAKE_DIR/calls"
case "$1 $2" in
  "iot describe-thing") [ -e "$FAKE_DIR/no-thing" ] && { echo ResourceNotFoundException >&2; exit 254; }; echo ok ;;
  "iot list-thing-principals") cat "$FAKE_DIR/principals" ;;
  "iot list-attached-policies") echo scoreboard-device ;;
  "iot list-principal-things") cat "$FAKE_DIR/cert-things" ;;
  "dynamodb get-item") cat "$FAKE_DIR/row" ;;
  *) [ -e "$FAKE_DIR/fail-$2" ] && { echo boom >&2; exit 1; }; : ;;
esac
"""


def run(tmp_path, *args, principals=ARN, cert_things=THING, row=THING, flags=()):
    fake = tmp_path / "aws"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "principals").write_text(principals + "\n" if principals else "")
    (tmp_path / "cert-things").write_text(cert_things + "\n")
    (tmp_path / "row").write_text(row + "\n" if row else "")
    for flag in flags:
        (tmp_path / flag).touch()
    env = {**os.environ, "AWS": str(fake), "FAKE_DIR": str(tmp_path)}
    result = subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True)
    calls_file = tmp_path / "calls"
    calls = calls_file.read_text().splitlines() if calls_file.exists() else []
    return result, calls


def writes(calls):
    reads = ("describe-", "list-", "get-")
    return [c for c in calls if not c.split()[1].startswith(reads)]


def test_every_call_names_the_region(tmp_path):
    _, calls = run(tmp_path, THING, "--apply", THING)
    assert calls and all("--region us-east-1" in c for c in calls)


def test_without_apply_nothing_is_written(tmp_path):
    result, calls = run(tmp_path, THING)
    assert result.returncode == 0
    assert writes(calls) == []
    assert "dry run" in result.stdout
    # Found on the first real dry run: the fake answered "yes" where AWS
    # answers with the thing name, and the line printed both.
    assert "devices row:  yes\n" in result.stdout
    assert CERT[:12] in result.stdout and CERT not in result.stdout, "certificate id is abbreviated"


def test_apply_needs_the_name_typed_a_second_time(tmp_path):
    result, calls = run(tmp_path, THING, "--apply")
    assert result.returncode != 0 and writes(calls) == []
    result, calls = run(tmp_path, THING, "--apply", "scoreboard-000000000000")
    assert result.returncode != 0 and writes(calls) == []


def test_access_is_cut_first_and_the_record_goes_last(tmp_path):
    result, calls = run(tmp_path, THING, "--apply", THING)
    assert result.returncode == 0, result.stderr
    order = [c.split()[1] for c in writes(calls)]
    assert order == [
        "update-certificate",      # INACTIVE: the panel is off the broker from here
        "detach-policy",
        "detach-thing-principal",
        "delete-certificate",
        "delete-thing",
        "delete-item",             # the owner's row, last: until then the site still shows it
    ]
    assert any("update-certificate" in c and "--new-status INACTIVE" in c for c in calls)


def test_names_that_are_not_a_panel_are_refused_before_any_call(tmp_path):
    for bad in ["", "scoreboard-", "scoreboard-ABC", "prod-db", "scoreboard-973pe43q585t;rm", "*"]:
        result, calls = run(tmp_path, bad, "--apply", bad)
        assert result.returncode != 0, bad
        assert calls == [], bad


def test_a_certificate_shared_with_another_thing_is_not_touched(tmp_path):
    result, calls = run(tmp_path, THING, "--apply", THING, cert_things=f"{THING}\nscoreboard-8qzexpa5xf7e")
    assert result.returncode != 0
    assert writes(calls) == []
    assert "another thing" in result.stderr


def test_a_failed_step_stops_the_run(tmp_path):
    result, calls = run(tmp_path, THING, "--apply", THING, flags=["fail-update-certificate"])
    assert result.returncode != 0
    assert [c.split()[1] for c in writes(calls)] == ["update-certificate"]


def test_a_thing_already_gone_still_clears_the_row(tmp_path):
    # A run interrupted after delete-thing must be finishable.
    result, calls = run(tmp_path, THING, "--apply", THING, principals="", flags=["no-thing"])
    assert result.returncode == 0, result.stderr
    assert [c.split()[1] for c in writes(calls)] == ["delete-item"]


def test_nothing_left_at_all_says_so(tmp_path):
    result, calls = run(tmp_path, THING, "--apply", THING, principals="", row="", flags=["no-thing"])
    assert result.returncode == 0
    assert writes(calls) == []
    assert "nothing to remove" in result.stdout
