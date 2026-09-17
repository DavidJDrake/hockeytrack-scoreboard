"""The image recipe, read as text.

These catch the quiet regressions: an unpinned pi-gen, a credential setting
creeping into the config, or the image's package list drifting from the one
pi-setup.sh --appliance installs. They do not build an image.
"""
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


def test_the_stage_documents_its_tmpfs_assumption():
    run = (PIGEN / "stage-scoreboard" / "01-install" / "00-run.sh").read_text()
    assert "tmpfs" in run
