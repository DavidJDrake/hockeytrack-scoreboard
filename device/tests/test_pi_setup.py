"""tools/pi-setup.sh, run against a throwaway copy of the checkout so the
real script and unit template are exercised without touching this machine.
Only --print-unit and --preflight are run here; both are read-only."""
import os
import pwd
import shlex
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
    shutil.copy(REPO / "device" / "scoreboard-appliance.service", tmp_path / "device")
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


def test_appliance_unit_runs_as_its_own_account(checkout):
    done = run(checkout, "--appliance", "--print-unit")
    assert done.returncode == 0
    fields = unit(done.stdout)
    assert fields["User"] == "scoreboard"
    assert fields["WorkingDirectory"] == "/opt/scoreboard"
    assert fields["Environment"].endswith("/var/lib/scoreboard") or \
        "SCOREBOARD_CONFIG_DIR=/var/lib/scoreboard" in done.stdout


def test_appliance_unit_keeps_the_tty_grab(checkout):
    # Without a controlling TTY, kmsdrm cannot become DRM master and the panel
    # stays black even though the service is "running".
    out = run(checkout, "--appliance", "--print-unit").stdout
    assert "TTYPath=/dev/tty1" in out and "StandardInput=tty" in out


def test_appliance_unit_can_write_its_identity_directory(checkout):
    out = run(checkout, "--appliance", "--print-unit").stdout
    assert "ProtectSystem=strict" in out
    assert "ReadWritePaths=/var/lib/scoreboard" in out


def test_appliance_unit_does_not_declare_supplementary_groups(checkout):
    # Group membership belongs to install_appliance now, not the unit: the
    # two used to list the same four groups and disagree about which were
    # optional, and systemd fails a unit outright (216/GROUP) if a name it's
    # told to resolve does not exist. Removing it here means the unit can no
    # longer make a promise the installer might not keep.
    out = run(checkout, "--appliance", "--print-unit").stdout
    assert "SupplementaryGroups" not in unit(out)


def test_appliance_preflight_does_not_demand_a_device_identity(checkout, tmp_path):
    # An image is built before any device has an identity. Requiring one would
    # make the image unbuildable.
    shutil.rmtree(checkout / "device" / "config")
    done = run(checkout, "--appliance", "--preflight")
    assert done.returncode == 0, done.stderr


def test_checkout_preflight_still_demands_one(checkout):
    shutil.rmtree(checkout / "device" / "config")
    done = run(checkout, "--preflight")
    assert done.returncode != 0
    assert "make provision" in done.stderr


def test_appliance_still_refuses_bookworm(checkout):
    done = run(checkout, "--appliance", "--preflight", codename="bookworm")
    assert done.returncode != 0
    assert "kmsdrm" in done.stderr


def _definitions_only(script_text):
    """The function and variable definitions at the top of pi-setup.sh,
    without the dispatch loop at the bottom -- so sourcing this cannot
    itself run install()/install_appliance() (which need apt-get, sudo,
    useradd and usermod, none of which this suite may invoke)."""
    marker = 'for arg in "$@"'
    assert marker in script_text, "pi-setup.sh no longer has the expected dispatch loop"
    return script_text.split(marker, 1)[0]


def _run_probe(tmp_path, script_text, tail):
    # Written to a real file, not `bash -c`, so ${BASH_SOURCE[0]} resolves
    # normally under `set -u` and REPO/DEVICE compute to paths under
    # tmp_path -- never the real checkout's device/config.
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    probe = tools / "probe.sh"
    probe.write_text(_definitions_only(script_text) + "\n" + tail + "\n")
    return subprocess.run(["bash", str(probe)], capture_output=True, text=True, timeout=30)


def test_appliance_install_dash_d_reaches_coreutils_install(tmp_path):
    """Runs tools/pi-setup.sh:121 for real: `install -d -o ... -m 700
    "$STATE_DIR"`, the line install_appliance uses to create the service's
    state directory. tools/pi-setup.sh also defines a shell function named
    install() (the checkout-mode installer at line 57), which shadows the
    coreutils binary for this exact call.

    This reproduces the failure from GitHub Actions run 35295966741: with
    the shadow in place, `install -d ...` re-enters the checkout install()
    function instead of creating the directory. That function immediately
    calls preflight(), which -- since no device/config exists under this
    throwaway root -- dies with "missing .../device.json", so the state
    directory is never created and this test fails. Fixed, `install -d`
    must reach coreutils and actually create it.
    """
    target = tmp_path / "var-lib-scoreboard"
    tail = (
        'SERVICE_USER="$(id -un)"\n'
        f'install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 {shlex.quote(str(target))}\n'
    )
    r = _run_probe(tmp_path, (REPO / "tools" / "pi-setup.sh").read_text(), tail)
    assert r.returncode == 0, (
        "install -d did not run coreutils install(1) -- it almost certainly "
        f"re-entered the checkout install() function instead; stderr: {r.stderr}"
    )
    assert target.is_dir(), f"the state directory was never created; stderr: {r.stderr}"
    assert oct(target.stat().st_mode & 0o777) == "0o700"


def test_appliance_install_dash_D_reaches_coreutils_install(tmp_path):
    """Same shadowing bug, same shell function, the other call site: line
    148's `install -D -m 644 "$DEVICE/polkit/..." /etc/polkit-1/rules.d/...`
    installs the polkit rule. The real destination is under /etc and needs
    root, which this suite cannot use, so this probes the identical `install
    -D -m 644 SRC DST` shape against a writable DST instead -- proving the
    same shadow affects this call site too, without touching the real
    filesystem.
    """
    src = tmp_path / "10-scoreboard-network.rules"
    src.write_text("polkit.addRule(function(){});\n")
    dst = tmp_path / "installed" / "10-scoreboard-network.rules"
    tail = f"install -D -m 644 {shlex.quote(str(src))} {shlex.quote(str(dst))}\n"
    r = _run_probe(tmp_path, (REPO / "tools" / "pi-setup.sh").read_text(), tail)
    assert r.returncode == 0, (
        "install -D did not run coreutils install(1) -- it almost certainly "
        f"re-entered the checkout install() function instead; stderr: {r.stderr}"
    )
    assert dst.is_file(), f"the polkit rule was never installed; stderr: {r.stderr}"
    assert oct(dst.stat().st_mode & 0o777) == "0o644"
    assert dst.read_text() == src.read_text()


def test_no_function_shadows_a_command_this_script_calls():
    """Backstop for the whole class of bug, not just install(): if any
    function this script defines has the same name as a real command it (or
    a function it calls) invokes unqualified, the function silently wins,
    the same way install() ate `install -d` at line 121. Anything found here
    should either be renamed or called via `command <name>`.
    """
    import shutil as _shutil

    text = (REPO / "tools" / "pi-setup.sh").read_text()
    lines = text.splitlines()
    func_names = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("function "):
            func_names.add(stripped.split()[1].split("(")[0])
            continue
        head = stripped.split("(")[0].strip()
        if stripped.endswith("{") and stripped[len(head):].replace(" ", "").startswith("()") and head.isidentifier():
            func_names.add(head)

    offenders = []
    for name in sorted(func_names):
        if _shutil.which(name) is None:
            continue  # not a real command on this system, so it cannot be shadowed
        for line in lines:
            s = line.strip()
            if s.startswith(f"{name}(") or s.startswith(f"function {name}"):
                continue  # the definition itself
            if f"command {name}" in s:
                continue  # already qualified
            # A call is this name as the first word of a simple command,
            # optionally after `sudo`.
            words = s.split()
            first = words[0] if words else ""
            if first == "sudo" and len(words) > 1:
                first = words[1]
            if first == name:
                offenders.append((name, line))
    assert not offenders, (
        "function name(s) collide with a real command this script calls "
        f"unqualified, and will shadow it: {offenders}"
    )


# The two checks below read the script's source rather than running
# install_appliance, which needs root, apt-get, useradd and usermod -- none
# of which this suite is allowed to invoke. They are weaker than an
# end-to-end run, but install_appliance can only be exercised on real
# hardware or inside pi-gen (see docs/hardware-checks.md).

def test_appliance_apt_packages_use_polkitd_not_policykit1(checkout):
    # policykit-1 was a transitional package that Debian dropped after
    # Bookworm; Trixie, the only release preflight accepts, has only
    # polkitd. Without this, --appliance fails at apt-get on every release
    # the script supports.
    text = (checkout / "tools" / "pi-setup.sh").read_text()
    assert "policykit-1" not in text
    assert "polkitd" in text


def test_appliance_installer_requires_video_render_input_but_not_gpio(checkout):
    # The unit no longer declares SupplementaryGroups=, so the installer is
    # the only place group membership comes from. video, render and input
    # must resolve or the panel cannot open /dev/dri or the input devices at
    # all; gpio stays optional, since the buttons it gates are optional
    # hardware that already degrades cleanly without it.
    text = (checkout / "tools" / "pi-setup.sh").read_text()
    assert 'for g in video render input; do' in text
    assert 'die "required group' in text
    assert 'getent group gpio >/dev/null && usermod -aG gpio' in text


def test_the_installer_installs_the_crypto_package():
    text = (REPO / "tools" / "pi-setup.sh").read_text()
    # Enrollment generates a P-256 key on the device; the PyPI wheel is not
    # what this venv sees, the distribution package is.
    assert text.count("python3-cryptography") == 2, \
        "both the checkout and the appliance apt lines need python3-cryptography"


def test_the_installer_installs_a_trust_store(checkout):
    text = (checkout / "tools" / "pi-setup.sh").read_text()
    # ssl.create_default_context() needs a system trust store to verify the
    # enrollment endpoint's certificate against (M-6). Present by default on
    # Raspberry Pi OS, but the installer should not silently depend on that.
    assert text.count("ca-certificates") == 2, \
        "both the checkout and the appliance apt lines need ca-certificates"


def test_the_root_ca_travels_with_the_code():
    ca = REPO / "device" / "certs" / "AmazonRootCA1.pem"
    assert ca.exists(), "an enrolling appliance has no provisioning step to download this"
    assert "BEGIN CERTIFICATE" in ca.read_text()
