"""tools/pi-setup.sh, run against a throwaway copy of the checkout so the
real script and unit template are exercised without touching this machine.
Only --print-unit and --preflight are run here; both are read-only."""
import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest

from scoreboard.display import EX_CONFIG

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "tools").mkdir()
    shutil.copy(REPO / "tools" / "pi-setup.sh", tmp_path / "tools")
    (tmp_path / "device").mkdir()
    shutil.copy(REPO / "device" / "scoreboard.service", tmp_path / "device")
    cfg = tmp_path / "device" / "config"
    cfg.mkdir()
    for name in ("device.json", "device.pem.crt", "AmazonRootCA1.pem", "private.pem.key"):
        (cfg / name).write_text("placeholder\n")
    (cfg / "private.pem.key").chmod(0o600)
    return tmp_path


def run(checkout, *args, codename="trixie"):
    release = checkout / "os-release"
    release.write_text(f"ID=debian\nVERSION_CODENAME={codename}\n")
    env = dict(os.environ, OS_RELEASE=str(release))
    return subprocess.run(["bash", str(checkout / "tools" / "pi-setup.sh"), *args],
                          env=env, capture_output=True, text=True, timeout=30)


def unit(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))


def test_unit_runs_as_the_invoking_user_from_this_checkout(checkout):
    r = run(checkout, "--print-unit")
    assert r.returncode == 0, r.stderr
    u = unit(r.stdout)
    assert u["User"] == pwd.getpwuid(os.getuid()).pw_name
    assert u["WorkingDirectory"] == f"{checkout}/device"
    assert u["ExecStart"] == f"{checkout}/device/.venv/bin/python -m scoreboard.main"
    assert "@" not in r.stdout, "a template placeholder was left unrendered"


def test_unit_does_not_restart_after_a_permanent_display_failure(checkout):
    r = run(checkout, "--print-unit")
    assert str(EX_CONFIG) in unit(r.stdout).get("RestartPreventExitStatus", "").split()


def test_preflight_accepts_trixie_with_a_provisioned_config(checkout):
    r = run(checkout, "--preflight")
    assert r.returncode == 0, r.stderr


def test_preflight_refuses_bookworm(checkout):
    r = run(checkout, "--preflight", codename="bookworm")
    assert r.returncode != 0
    assert "trixie" in r.stderr.lower()


def test_preflight_refuses_a_private_key_others_can_read(checkout):
    (checkout / "device" / "config" / "private.pem.key").chmod(0o644)
    assert run(checkout, "--preflight").returncode != 0


def test_preflight_refuses_a_checkout_with_no_device_config(checkout):
    shutil.rmtree(checkout / "device" / "config")
    assert run(checkout, "--preflight").returncode != 0
