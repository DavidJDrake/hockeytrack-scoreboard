"""The image recipe, read as text.

These catch the quiet regressions: an unpinned pi-gen, a credential setting
creeping into the config, or the image's package list drifting from the one
pi-setup.sh --appliance installs. They do not build an image.
"""
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PIGEN = REPO / "tools" / "pi-gen"


def config() -> dict[str, str]:
    out = {}
    for line in (PIGEN / "config").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"')
    return out


def test_pi_gen_is_pinned_to_a_full_commit():
    ref = (PIGEN / "PIGEN_REF").read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{40}", ref), f"PIGEN_REF must be a full commit SHA, got {ref!r}"


def test_the_config_builds_trixie_into_the_scoreboard_stage():
    cfg = config()
    assert cfg["IMG_NAME"] == "scoreboard"
    assert cfg["RELEASE"] == "trixie"
    assert cfg["STAGE_LIST"] == "stage0 stage1 stage2 stage-scoreboard"
    assert cfg["DEPLOY_COMPRESSION"] == "xz"


def test_no_credential_or_ssh_setting_is_present():
    text = (PIGEN / "config").read_text()
    for forbidden in ("FIRST_USER_PASS", "DISABLE_FIRST_BOOT_USER_RENAME", "ENABLE_SSH",
                      "PUBKEY_SSH_FIRST_USER", "PUBKEY_ONLY_SSH", "WPA_PASSWORD", "WPA_ESSID"):
        assert forbidden not in text, f"{forbidden} must stay unset: the image is flashed by strangers"


def appliance_packages() -> set[str]:
    script = (REPO / "tools" / "pi-setup.sh").read_text()
    body = script[script.index("install_appliance()"):]
    line = next(l for l in body.splitlines() if "apt-get install -y" in l)
    return set(line.split("apt-get install -y", 1)[1].split())


def test_the_image_packages_are_exactly_what_appliance_mode_installs():
    listed = set((PIGEN / "stage-scoreboard" / "00-packages" / "00-packages").read_text().split())
    assert listed == appliance_packages()


def test_only_the_scoreboard_stage_exports_an_image():
    build = (PIGEN / "build.sh").read_text()
    assert "stage2/SKIP_IMAGES" in build
    assert (PIGEN / "stage-scoreboard" / "EXPORT_IMAGE").is_file()


def test_the_build_copies_only_what_the_appliance_needs_from_device():
    build = (PIGEN / "build.sh").read_text()
    # device/config holds a developer's real identity; it must never be copied.
    assert "device/config" not in build
    assert re.search(r'cp -a "\$REPO/device/"\*', build) is None, "copy an explicit list, not device/*"


def test_the_stage_runs_appliance_mode_and_writes_the_build_identity():
    run = (PIGEN / "stage-scoreboard" / "01-install" / "00-run.sh").read_text()
    assert "pi-setup.sh --appliance" in run
    assert "on_chroot" in run
    assert "/etc/scoreboard-build" in run
    assert 'rm -rf "${ROOTFS_DIR}/tmp/scoreboard-src"' in run


def test_cloud_init_is_left_out_of_the_image():
    # pi-gen's stage2/04-cloud-init installs cloud-init and writes a NoCloud
    # seed (user-data, network-config, meta-data) to the boot partition.
    # ENABLE_CLOUD_INIT=0 skips only the seed files at the pinned commit; the
    # sub-stage's 00-packages still runs, so the build also marks the whole
    # sub-stage SKIP. The appliance owns networking through scoreboard-netcfg.
    assert config().get("ENABLE_CLOUD_INIT") == "0"
    build = (PIGEN / "build.sh").read_text()
    assert 'touch "$WORK/stage2/04-cloud-init/SKIP"' in build


def test_pi_gen_is_fetched_by_commit_not_by_branch():
    # Cloning a branch and then checking out the SHA breaks the day upstream
    # force-pushes the branch past it; fetching the commit itself does not.
    build = (PIGEN / "build.sh").read_text()
    assert "--branch" not in build
    assert 'git -C "$WORK" fetch --quiet --depth 1 origin "$REF"' in build
    assert 'git -C "$WORK" checkout --quiet --detach FETCH_HEAD' in build
    assert '[ "$(git -C "$WORK" rev-parse HEAD)" = "$REF" ]' in build


def install_appliance_body() -> str:
    script = (REPO / "tools" / "pi-setup.sh").read_text()
    start = script.index("install_appliance() {")
    return script[start:script.index("\n}\n", start)]


def test_appliance_mode_installs_nothing_from_pypi():
    # The service holding each panel's IoT private key imports paho-mqtt, so
    # it must come from Debian's signed archive. pip may only confirm that
    # what apt installed satisfies requirements.txt: --no-index means it
    # cannot download anything, --no-cache-dir means nothing is cached into
    # the image, and no version check reaches PyPI either.
    assert "python3-paho-mqtt" in appliance_packages()
    body = install_appliance_body()
    pip_lines = [l for l in body.splitlines() if "pip" in l and "install" in l and not l.strip().startswith("#")]
    assert pip_lines, "appliance mode no longer checks requirements.txt at all"
    for line in pip_lines:
        for flag in ("--no-index", "--no-cache-dir", "--disable-pip-version-check"):
            assert flag in line, f"{flag} missing from: {line.strip()}"
    assert "pip download" not in body


WIZARD_RUN = PIGEN / "stage-scoreboard" / "02-no-first-boot-wizard" / "00-run.sh"


def test_the_stage_disarms_the_first_boot_user_creation_wizard():
    # v0.1.0 booted to userconf-pi's whiptail dialog asking for a new username
    # and password, which nobody can answer on a keyboard-less panel
    # (docs/hardware-checks.md, H5). pi-gen arms it AFTER every stage has run
    # -- export-image/01-user-rename runs `rename-user -f -s` against the
    # mounted image -- so deleting the enablement symlink here would be undone.
    # Masking the unit is what survives: systemctl refuses to enable a masked
    # unit and creates no symlink.
    run = WIZARD_RUN.read_text()
    assert "/etc/systemd/system/userconfig.service" in run
    assert "/dev/null" in run
    # pi-gen skips a sub-stage script that is not executable, silently.
    assert os.access(WIZARD_RUN, os.X_OK), f"{WIZARD_RUN} must be executable or pi-gen skips it"


def test_the_wizard_is_disarmed_in_the_stage_not_by_the_config_switch():
    # DISABLE_FIRST_BOOT_USER_RENAME=1 is the switch that would skip
    # rename-user, and pi-gen's build.sh refuses to build with it unless
    # FIRST_USER_PASS is also set. A password baked into a public image is the
    # one thing this image must not carry, so the switch stays unset.
    assert "DISABLE_FIRST_BOOT_USER_RENAME" not in (PIGEN / "config").read_text()
    assert "FIRST_USER_PASS" not in (PIGEN / "config").read_text()


REMOTE_ACCESS_RUN = PIGEN / "stage-scoreboard" / "03-no-remote-access" / "00-run.sh"


def test_the_stage_removes_the_remote_access_agent():
    # pi-gen's stage2/01-sys-tweaks/00-packages installs rpi-connect-lite, a
    # remote-access agent, from the same list that brings ssh, sudo and
    # console-setup -- so the SKIP-the-sub-stage mechanism used for cloud-init
    # is not available, and the package is purged in this stage instead. Purge,
    # not remove: a removed-but-not-purged package keeps its stanza in
    # /var/lib/dpkg/status, which is the signal the gate reads.
    run = REMOTE_ACCESS_RUN.read_text()
    assert "on_chroot" in run
    assert "apt-get purge" in run
    for package in ("rpi-connect", "rpi-connect-lite"):
        assert package in run
    assert "apt-get remove" not in run
    # pi-gen skips a sub-stage script that is not executable, silently.
    assert os.access(REMOTE_ACCESS_RUN, os.X_OK), f"{REMOTE_ACCESS_RUN} must be executable or pi-gen skips it"


JOURNAL_RUN = PIGEN / "stage-scoreboard" / "04-persistent-journal" / "00-run.sh"
VOLATILE_DROPIN = "40-rpi-volatile-storage.conf"


def test_the_stage_keeps_the_journal_on_the_card():
    # Once the panel owns tty1 there is no console to read, so a startup
    # failure shows a black screen and nothing else. raspberrypi-sys-mods ships
    # /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf with
    # Storage=volatile, which would leave nothing on the card either.
    run = JOURNAL_RUN.read_text()
    assert "Storage=persistent" in run
    assert "SystemMaxUse=" in run
    assert "/var/log/journal" in run
    # pi-gen skips a sub-stage script that is not executable, silently.
    assert os.access(JOURNAL_RUN, os.X_OK), f"{JOURNAL_RUN} must be executable or pi-gen skips it"


def test_the_journal_drop_in_sorts_after_the_volatile_one():
    # journald sorts drop-ins by filename across /etc, /run and /usr/lib at
    # once, and the lexicographically last file to set an option wins -- so
    # living under /etc is not enough on its own.
    # Only the files the stage writes into the rootfs; the volatile drop-in is
    # named in the comments too, and it is the thing being beaten, not a file
    # this stage creates.
    names = re.findall(r"\$\{ROOTFS_DIR\}/etc/systemd/journald\.conf\.d/([A-Za-z0-9._-]+\.conf)",
                       JOURNAL_RUN.read_text())
    assert names, "the stage no longer writes a journald drop-in"
    for name in names:
        assert name > VOLATILE_DROPIN, f"{name} does not sort after {VOLATILE_DROPIN}, so volatile would win"


def test_the_stage_documents_its_tmpfs_assumption():
    run = (PIGEN / "stage-scoreboard" / "01-install" / "00-run.sh").read_text()
    assert "tmpfs" in run
