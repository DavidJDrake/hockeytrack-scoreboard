"""tools/pi-setup.sh, run against a throwaway copy of the checkout so the
real script and unit template are exercised without touching this machine.
Only --print-unit and --preflight are run here; both are read-only."""
import os
import pwd
import re
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


NETCFG_UNIT = REPO / "device" / "scoreboard-netcfg.service"


# systemd time spans are a sequence of value+unit pairs, and a bare number is
# seconds. Only the units that could sensibly appear here are handled -- if
# somebody writes "1h" this raises rather than silently mis-measuring, which
# is the failure mode the round-1 review caught in the old `int(...rstrip("s"))`.
SPAN_UNITS = {"": 1, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
              "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60}


def systemd_seconds(span: str) -> float:
    total, number = 0.0, ""
    for part in re.findall(r"\d+(?:\.\d+)?|[A-Za-z]+", span.strip()):
        if part[0].isdigit():
            if number:
                total += float(number)  # a bare number already seen: seconds
            number = part
        else:
            assert part in SPAN_UNITS, f"unhandled systemd time unit {part!r} in {span!r}"
            total += float(number or 0) * SPAN_UNITS[part]
            number = ""
    return total + float(number or 0)


def test_systemd_seconds_reads_the_spans_this_file_accepts():
    # The helper above is the thing being trusted by the next test, so it is
    # checked rather than assumed.
    assert systemd_seconds("120") == 120
    assert systemd_seconds("120s") == 120
    assert systemd_seconds("2min") == 120
    assert systemd_seconds("1min 30s") == 90
    with pytest.raises(AssertionError):
        systemd_seconds("1h")


def test_the_network_unit_states_its_own_start_budget():
    # It is Before=scoreboard.service, so everything it does is time the panel
    # spends showing nothing.
    #
    # netcfg.BOOT_BUDGET_S is the whole of it now: one monotonic deadline that
    # raspi-config and every nmcli call draw from, each given timeout=min(its
    # own cap, time remaining). That replaced a column of intentions added up
    # to "87 s" which was not a bound -- it left out radio_on() and the
    # rescans, and counted the device wait as six two-second sleeps when each
    # of those six iterations first ran a query that could itself take
    # QUERY_TIMEOUT_S. The honest ceiling of that version was about 195 s,
    # which is past this very timeout, so systemd would have killed the unit
    # rather than the budget stopping it.
    #
    # Read from netcfg's own constant rather than repeated here, so the unit
    # and the code cannot drift.
    from scoreboard import netcfg

    text = NETCFG_UNIT.read_text()
    fields = unit(text)
    assert "TimeoutStartSec" in fields, "the unit inherits DefaultTimeoutStartSec without saying so"
    budget = systemd_seconds(fields["TimeoutStartSec"])
    worst_case = netcfg.BOOT_BUDGET_S
    # The number systemd has to clear is the ABSOLUTE ceiling, not the soft
    # budget. joined() is allowed VERIFY_OVERRUN_S past the deadline -- once,
    # deliberately, because answering "no" without asking strands a cleartext
    # password on the boot partition -- and systemd does not know or care that
    # the overrun is intentional. The unit's own comment has always said this
    # test asserts the headroom "above even that absolute ceiling"; it
    # asserted it above the soft budget, which is a different and easier
    # number. Corrected here rather than in the comment: the comment was
    # describing the test worth having.
    ceiling = netcfg.ABSOLUTE_CEILING_S
    assert ceiling > worst_case, "the absolute ceiling is not above the budget it extends"
    assert budget > ceiling, \
        f"TimeoutStartSec={budget}s cannot cover the {ceiling}s absolute ceiling"
    assert budget <= 300, f"TimeoutStartSec={budget}s leaves the panel dark too long when Wi-Fi fails"
    # Room for systemd's own overhead above a ceiling the code enforces,
    # rather than a figure that merely happens to clear it by a second.
    # systemd's own start-up accounting, a slow SD card and the exec of a
    # Python interpreter all land in this gap.
    assert budget - ceiling >= 25, \
        f"only {budget - ceiling}s between the absolute ceiling and the unit's timeout"
    # And the numbers the unit's own comment states, so the comment is a
    # tripwire rather than a decoration. BOTH headrooms are named there,
    # because quoting only one of them is how the unit came to say 32 s while
    # the spec said 38 s about the same TimeoutStartSec.
    assert f"{worst_case} s" in text, \
        f"the unit's comment no longer states the {worst_case}s budget it is sized against"
    assert f"{ceiling} s" in text, \
        f"the unit's comment no longer states the {ceiling}s absolute ceiling"
    assert f"{int(budget - worst_case)} s of headroom" in text, \
        f"the unit's comment no longer states the {int(budget - worst_case)}s above the soft budget"
    assert f"{int(budget - ceiling)} s above" in text, \
        f"the unit's comment no longer states the {int(budget - ceiling)}s above the absolute ceiling"


def test_the_network_unit_can_write_only_the_setup_file_and_the_country():
    # scoreboard-netcfg is one of the two units the OTA design (4.3) allows to
    # write SETUP, and it runs as root. The gate's "no unit names
    # /boot/firmware in ReadWritePaths=" rule is vacuous for a unit with no
    # ProtectSystem= at all: such a process can write the running slot's FAT,
    # the read-only root's remounted paths and every file on STATE. So the
    # unit pins the whole tree read-only and opens exactly the two places
    # netcfg writes -- the setup file it consumes and the country it saves.
    # The NetworkManager profile goes over D-Bus and iw over netlink, so
    # nothing under /etc or /var/lib is on the list, and nothing should be.
    #
    # The STATE path carries the `-` prefix and the SETUP path does not.
    # /boot/setup is a mount point on the read-only root, present on every
    # boot; /state/network is inside STATE, and on the torn-STATE boot the
    # design (4.3) promises to survive it does not exist. An undashed path
    # that is missing fails the unit's namespace setup (226/NAMESPACE), so
    # netcfg would never run on exactly the boot the SETUP-file repair path
    # is for.
    text = NETCFG_UNIT.read_text()
    fields = unit(text)
    assert fields.get("ProtectSystem") == "strict", \
        "scoreboard-netcfg runs as root with the whole card writable"
    assert fields.get("ReadWritePaths", "").split() == ["/boot/setup", "-/state/network"], \
        f"ReadWritePaths={fields.get('ReadWritePaths')!r}; only the setup file and the country are written, and the STATE path must be optional"


def test_appliance_unit_runs_as_its_own_account(checkout):
    done = run(checkout, "--appliance", "--print-unit")
    assert done.returncode == 0
    fields = unit(done.stdout)
    assert fields["User"] == "scoreboard"
    assert fields["WorkingDirectory"] == "/opt/scoreboard"
    assert fields["Environment"].endswith("/var/lib/scoreboard") or \
        "SCOREBOARD_CONFIG_DIR=/var/lib/scoreboard" in done.stdout


def test_the_program_uses_no_sound_at_all(checkout):
    # The tripwire for the setting below. If the scoreboard ever does play a
    # sound, SDL_AUDIODRIVER=dummy would silence it on every panel and the
    # only symptom would be silence -- so the setting is only defensible
    # while this holds. Read from the real package, not the throwaway copy.
    source = "\n".join(p.read_text() for p in sorted((REPO / "device" / "scoreboard").glob("*.py")))
    for api in ("mixer", "Sound(", "sndarray", "pygame.music"):
        assert api not in source, \
            f"device/scoreboard/ now uses {api!r}; remove SDL_AUDIODRIVER=dummy from the units first"


def test_both_units_ask_sdl_for_the_dummy_audio_driver(checkout):
    # The panel has no sound hardware and the program plays nothing (the test
    # above), but pygame.init() initializes the mixer regardless, and ALSA
    # then prints about fifteen "cannot find card '0'" lines on every single
    # start. That noise goes into a journal which is now persistent and is
    # the only diagnosis surface a panel has once it owns tty1 -- and the
    # service restarts every 3 s while it is failing, so the one line worth
    # reading is buried under hundreds of lines that are not.
    #
    # Both units, because both run the same soundless program: the appliance
    # unit on an image, and the checkout template on a developer's Pi, where
    # the same fifteen lines bury the same failures.
    for args in (("--print-unit",), ("--appliance", "--print-unit")):
        done = run(checkout, *args)
        assert done.returncode == 0, done.stderr
        assert "Environment=SDL_AUDIODRIVER=dummy" in done.stdout, f"missing from {args}"


def test_both_units_name_the_render_driver_for_the_window_surface(checkout):
    # v0.1.2 booted, ran stably for fifteen minutes, logged every frame it
    # drew -- and showed solid black. The second boot changed nothing but
    # these two variables, passed on the kernel command line with
    # systemd.setenv=, and the panel painted.
    #
    # Why, from SDL 2.32.4's source. pygame's non-OpenGL set_mode() ends in
    # SDL_GetWindowSurface, which calls SDL_CreateWindowFramebuffer
    # (SDL_video.c:2708). On kmsdrm ShouldAttemptTextureFramebuffer() is true
    # -- the driver is not the dummy one and none of the x11/windows/
    # emscripten special cases apply -- so the window surface is emulated with
    # a 2D renderer by SDL_CreateWindowTexture (SDL_video.c:230). With no hint
    # set that function walks render_drivers[] in order (SDL_render.c:100) and
    # takes the first accelerated non-"software" one. GL_RenderDriver
    # ("opengl") is listed before GLES2_RenderDriver ("opengles2"), so
    # "opengl" is always tried first.
    #
    # It cannot succeed on this image, and failing is not free. The image
    # ships no libGL.so.1 on purpose, so SDL_EGL_LoadLibraryInternal
    # (SDL_egl.c:370) cannot load DEFAULT_OGL and KMSDRM_CreateWindow retries
    # as GLES 2.0 (SDL_kmsdrmvideo.c:1552), leaving gl_config.profile_mask at
    # SDL_GL_CONTEXT_PROFILE_ES. GL_CreateRenderer tests exactly that
    # (SDL_render_gl.c:1717) and calls SDL_RecreateWindow to ask for a desktop
    # context -- which destroys the kmsdrm window: KMSDRM_DestroySurfaces
    # points the CRTC back at the original TTY buffer and KMSDRM_GBMDeinit
    # drops DRM master. It fails anyway, and its error path recreates the
    # window a second time (SDL_render_gl.c:1938). Only then does the loop
    # reach "opengles2".
    #
    # Naming the driver skips the whole attempt: SDL_CreateWindowTexture reads
    # SDL_FRAMEBUFFER_ACCELERATION first and falls back to SDL_RENDER_DRIVER,
    # so either one alone would satisfy that code. Both are set because both
    # together are what was observed to work, and checking an untested subset
    # costs a 35-minute build and a reflash.
    #
    # Both units, because both run the same program against the same SDL on
    # the same hardware: the appliance unit on an image, the checkout template
    # on a developer's Pi, which is where H1, H3 and H7 are run.
    for args in (("--print-unit",), ("--appliance", "--print-unit")):
        done = run(checkout, *args)
        assert done.returncode == 0, done.stderr
        assert "Environment=SDL_FRAMEBUFFER_ACCELERATION=opengles2" in done.stdout, \
            f"missing from {args}"
        assert "Environment=SDL_RENDER_DRIVER=opengles2" in done.stdout, \
            f"missing from {args}"


def test_appliance_unit_keeps_the_tty_grab(checkout):
    # Without a controlling TTY, kmsdrm cannot become DRM master and the panel
    # stays black even though the service is "running".
    out = run(checkout, "--appliance", "--print-unit").stdout
    assert "TTYPath=/dev/tty1" in out and "StandardInput=tty" in out


def test_appliance_unit_can_write_its_identity_directory(checkout):
    out = run(checkout, "--appliance", "--print-unit").stdout
    assert "ProtectSystem=strict" in out
    assert "ReadWritePaths=/var/lib/scoreboard" in out


def test_the_appliance_unit_lets_the_gpio_buttons_reach_the_gpio(checkout):
    # Seen in v0.1.2's journal on every start, four times:
    #
    #   xCreatePipe: Can't set permissions (436) for /opt/scoreboard/.lgd-nfy0,
    #       No such file or directory
    #   PinFactoryFallback: Falling back from lgpio: [Errno 2] No such file or
    #       directory: '.lgd-nfy-3'
    #   ... rpigpio ... pigpio ... native: unable to open /dev/gpiomem or /dev/mem
    #
    # Nothing visible broke, because this panel has no buttons. But the
    # hardened unit had silently switched them off, and the journal is now the
    # panel's only diagnosis surface, so four warnings a start is a real cost
    # even when the hardware is absent.
    #
    # Two causes, both read from source rather than guessed at.
    #
    # 1. lgpio makes a notification FIFO in its working directory:
    #    lgNotify.c:131 builds "%s/.lgd-nfy%d" from lguGetWorkDir(), which
    #    (lgUtil.c:181) returns getenv(LG_WD) and otherwise falls back to
    #    getcwd(). LG_WD is the literal "LG_WD" (lgpio.h:39). With nothing
    #    set, getcwd() here is WorkingDirectory=/opt/scoreboard, which
    #    ProtectSystem=strict makes read-only -- hence the exact permission
    #    in the message, 436 == 0664, which is the mode xCreatePipe passes.
    #
    # 2. Even with somewhere to write, the process could not open the chip:
    #    lgpio opens /dev/gpiochip%d (lgGpio.c:724) and gpiozero's factory
    #    picks chip 0 on a Pi 4 (gpiozero/pins/lgpio.py:67). The unit's
    #    DeviceAllow list named char-drm, char-input and /dev/tty1 and
    #    nothing else. The kernel registers that char device class as
    #    "gpiochip" (drivers/gpio/gpiolib.h, GPIOCHIP_NAME -- no line
    #    number, it moves between trees), which is the name systemd
    #    matches against /proc/devices, so char-gpiochip is the class.
    #
    # The group half was already right: raspberrypi-sys-mods' 99-com.rules
    # has SUBSYSTEM=="gpio", GROUP="gpio", MODE="0660", and pi-setup.sh's
    # install_appliance adds the service account to gpio when it exists.
    out = run(checkout, "--appliance", "--print-unit").stdout
    fields = unit(out)
    assert "DeviceAllow=char-gpiochip rw" in out, \
        "lgpio cannot open /dev/gpiochip0 under this unit"
    assert "Environment=LG_WD=" in out, \
        "lgpio will fall back to getcwd(), which ProtectSystem=strict has made read-only"
    # And wherever it is pointed has to be somewhere the service can write,
    # or the setting moves the failure rather than fixing it.
    lg_wd = next(line.split("=", 2)[2] for line in out.splitlines()
                 if line.startswith("Environment=LG_WD="))
    writable = [line.split("=", 1)[1] for line in out.splitlines()
                if line.startswith("ReadWritePaths=")]
    assert any(lg_wd == p or lg_wd.startswith(p.rstrip("/") + "/") for p in writable), \
        f"LG_WD={lg_wd} is not under any ReadWritePaths= ({writable}); the FIFO still cannot be made"
    # And it must not be pointed back at the working directory, which is the
    # default lgpio would have used by itself and the one place we know is
    # read-only under ProtectSystem=strict. Setting LG_WD to that would look
    # like a fix and change nothing.
    assert lg_wd != fields["WorkingDirectory"], \
        f"LG_WD={lg_wd} is the working directory, which is exactly where this failed"


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


# --- The A/B card's read-only root (OTA design 4.3) --------------------------

def test_the_appliance_pins_the_scoreboard_uid_and_gid():
    # The identity on STATE is owned by this number, and a new root can read
    # it only if its scoreboard user has the same one. Left floating, one
    # release that adds a system user would strand the fleet.
    script = (REPO / "tools" / "pi-setup.sh").read_text()
    assert re.search(r"^SERVICE_ID=900$", script, re.M)
    body = install_appliance_body()
    assert re.search(r'groupadd --system --gid "\$SERVICE_ID" "\$SERVICE_USER"', body)
    assert re.search(r'useradd --system --uid "\$SERVICE_ID" --gid "\$SERVICE_ID"', body)
    assert 'id -u "$SERVICE_USER")" = "$SERVICE_ID"' in body, "a pre-existing account with another number must fail the install"
    # The layout script carries the same number, and the gate reads it from
    # there rather than keeping a third copy.
    assert re.search(r"^SCOREBOARD_ID=900$", (REPO / "tools" / "image-layout.sh").read_text(), re.M)
    gate = (REPO / "tools" / "image-gate.sh").read_text()
    assert 'SCOREBOARD_ID="$("$LAYOUT" --print scoreboard-id)"' in gate
    assert not re.search(r"^SCOREBOARD_ID=[0-9]+$", gate, re.M), "the gate must not carry its own copy of the number"
    printed = subprocess.run(["bash", str(REPO / "tools" / "image-layout.sh"), "--print", "scoreboard-id"],
                             capture_output=True, text=True, check=True).stdout
    assert printed == "900\n"


def install_appliance_body() -> str:
    script = (REPO / "tools" / "pi-setup.sh").read_text()
    start = script.index("install_appliance() {")
    return script[start:script.index("\n}\n", start)]


def test_the_appliance_installs_what_a_read_only_root_needs():
    body = install_appliance_body()
    assert "ln -sfn /run/NetworkManager/resolv.conf /etc/resolv.conf" in body
    assert ":>/etc/fake-hwclock.data" in body.replace(" ", "") or "/etc/fake-hwclock.data" in body
    assert 'install -d -m 755 "$UPDATE_DIR"' in body
    assert '"$DEVICE/generators/scoreboard-bootfs"' in body
    assert "/usr/lib/systemd/system-generators/scoreboard-bootfs" in body
    assert '"$DEVICE/system.conf.d/10-scoreboard-watchdog.conf"' in body
    assert "/etc/systemd/system.conf.d/10-scoreboard-watchdog.conf" in body
    # The journal prune: without it the transient machine id fills STATE
    # with one journal directory per boot (see device/scoreboard-journal-prune).
    assert '"$DEVICE/scoreboard-journal-prune"' in body
    assert "/usr/local/sbin/scoreboard-journal-prune" in body
    assert "/etc/systemd/system/scoreboard-journal-prune.service" in body
    assert "/etc/systemd/system/sysinit.target.wants/scoreboard-journal-prune.service" in body
    # NetworkManager's ordering after its two nofail binds from STATE.
    assert '"$DEVICE/NetworkManager.service.d/10-scoreboard-state.conf"' in body
    assert "/etc/systemd/system/NetworkManager.service.d/10-scoreboard-state.conf" in body


def after_units(text: str) -> set[str]:
    return {name for line in text.splitlines() if line.startswith("After=") for name in line[6:].split()}


def directive(text: str, name: str) -> list[str]:
    # Directive lines only; the comments are allowed to name what they reject.
    return [line for line in text.splitlines() if line.startswith(name + "=")]


def test_the_units_that_read_state_and_setup_are_ordered_after_their_mounts():
    # STATE, SETUP and the binds are nofail, and systemd.mount(5) says a
    # nofail mount is not ordered before local-fs.target -- so nothing waits
    # for them unless the unit that reads them says so. After= only; a
    # RequiresMountsFor= would stop the unit on a torn partition, and the
    # design wants that panel to boot to its help screen.
    appliance = (REPO / "device" / "scoreboard-appliance.service").read_text()
    assert after_units(appliance) >= {"state.mount", "var-lib-scoreboard.mount", "var-lib-scoreboard\\x2dupdate.mount"}, \
        "the panel would start against an empty /var/lib/scoreboard on a slow STATE"
    assert not directive(appliance, "RequiresMountsFor")
    netcfg = NETCFG_UNIT.read_text()
    assert after_units(netcfg) >= {"boot-setup.mount", "state.mount", "NetworkManager.service"}
    assert not directive(netcfg, "RequiresMountsFor")
    dropin = (REPO / "device" / "NetworkManager.service.d" / "10-scoreboard-state.conf").read_text()
    assert after_units(dropin) == {"etc-NetworkManager-system\\x2dconnections.mount", "var-lib-NetworkManager.mount"}
    assert dropin.count("[Unit]") == 1 and "[Service]" not in dropin, "an ordering drop-in changes nothing else"
    # The escaped names are what systemd derives from the paths, and a typo
    # here is a silent no-op on the panel: After= on a unit that does not
    # exist orders nothing. systemd-escape is the reference where it exists.
    if shutil.which("systemd-escape"):
        for path, name in (("/var/lib/scoreboard-update", "var-lib-scoreboard\\x2dupdate.mount"),
                           ("/etc/NetworkManager/system-connections", "etc-NetworkManager-system\\x2dconnections.mount"),
                           ("/boot/setup", "boot-setup.mount"), ("/state", "state.mount")):
            escaped = subprocess.run(["systemd-escape", "-p", "--suffix=mount", path],
                                     capture_output=True, text=True, check=True).stdout.strip()
            assert escaped == name, f"{path} escapes to {escaped}, and the units name {name}"


def test_the_watchdog_drop_in_arms_sixty_seconds_and_records_its_caveat():
    conf = (REPO / "device" / "system.conf.d" / "10-scoreboard-watchdog.conf").read_text()
    assert re.search(r"^RuntimeWatchdogSec=60$", conf, re.M)
    # The hardware counts to about 16 s; 60 is valid only on a kernel whose
    # watchdog core re-pings underneath, so the file has to say which.
    assert "16 s" in conf and "verified on:" in conf


def test_the_bootfs_generator_mounts_the_slot_the_firmware_booted(tmp_path):
    generator = REPO / "device" / "generators" / "scoreboard-bootfs"
    assert os.access(generator, os.X_OK)
    # Run it against a fake device tree and cmdline for slot B.
    dt = tmp_path / "partition"
    dt.write_bytes(b"\x00\x00\x00\x03")
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=tty1 root=PARTUUID=5c0ab0ad-06 rootfstype=ext4 rootwait ro\n")
    script = generator.read_text().replace("/proc/device-tree/chosen/bootloader/partition", str(dt)).replace("/proc/cmdline", str(cmdline))
    out = tmp_path / "generator.d"
    out.mkdir()
    r = subprocess.run(["sh", "-c", script, "scoreboard-bootfs", str(out)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    unit = (out / "boot-firmware.mount").read_text()
    assert "What=/dev/disk/by-partuuid/5c0ab0ad-03" in unit
    assert "Where=/boot/firmware" in unit
    # systemd.mount(5): nofail and x-systemd.device-timeout are read only
    # from /etc/fstab and ignored in a unit's Options=. The wants link is
    # what makes the mount optional, and the bound on the wait is a drop-in
    # on the device unit, as systemd-fstab-generator writes it.
    assert not re.search(r"^Options=.*\b(nofail|x-systemd\.device-timeout)", unit, re.M), \
        "these options are ignored in a unit file and would claim a bound that does not exist"
    assert (out / "local-fs.target.wants" / "boot-firmware.mount").is_symlink()
    timeout = out / "dev-disk-by\\x2dpartuuid-5c0ab0ad\\x2d03.device.d" / "50-device-timeout.conf"
    assert timeout.is_file(), sorted(p.name for p in out.iterdir())
    assert "JobRunningTimeoutSec=10s" in timeout.read_text()


def test_the_bootfs_generator_writes_nothing_on_a_card_that_is_not_this_layout(tmp_path):
    generator = REPO / "device" / "generators" / "scoreboard-bootfs"
    dt = tmp_path / "partition"
    dt.write_bytes(b"\x00\x00\x00\x01")  # pi-gen's two-partition card boots from 1
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("root=PARTUUID=abc-02 rw\n")
    script = generator.read_text().replace("/proc/device-tree/chosen/bootloader/partition", str(dt)).replace("/proc/cmdline", str(cmdline))
    out = tmp_path / "generator.d"
    out.mkdir()
    r = subprocess.run(["sh", "-c", script, "scoreboard-bootfs", str(out)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert list(out.iterdir()) == []
    # And no device-tree entry at all (not a Pi): the same.
    script = generator.read_text().replace("/proc/device-tree/chosen/bootloader/partition", str(tmp_path / "none"))
    r = subprocess.run(["sh", "-c", script, "scoreboard-bootfs", str(out)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and list(out.iterdir()) == []
