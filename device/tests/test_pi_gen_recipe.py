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
