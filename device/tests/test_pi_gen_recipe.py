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


PACKAGES_FILE = PIGEN / "stage-scoreboard" / "00-packages" / "00-packages"


def image_packages() -> set[str]:
    # pi-gen runs scripts/remove-comments.sed over an NN-packages file before
    # apt ever sees it -- the substitution is s/#[^\n]*//g -- so a comment in
    # that file is not a package. Read it the same way pi-gen does, or the
    # agreement test below would compare comment words against pi-setup.sh's
    # apt line and fail on two lists that are in fact identical.
    return set(re.sub(r"#[^\n]*", "", PACKAGES_FILE.read_text()).split())


def test_the_image_packages_are_exactly_what_appliance_mode_installs():
    assert image_packages() == appliance_packages()


def test_the_image_package_list_is_read_the_way_pi_gen_reads_it():
    # The list carries comments now -- they say why each runtime-loaded
    # graphics package is there. If this repository read the file without
    # stripping them, every comment word would look like a package name.
    assert "#" in PACKAGES_FILE.read_text(), "the list no longer explains itself"
    assert not any(name.startswith("#") for name in image_packages())


# SDL's kmsdrm backend dlopens its graphics libraries by soname at runtime
# instead of linking them, so nothing in the image Depends on them and apt
# never pulls them in -- not even with Recommends honored, since
# libsdl2-2.0-0 (2.32.4+dfsg-1) has no Recommends at all. v0.1.1 shipped
# without them and scoreboard.service crash-looped on "EGL not initialized"
# (docs/hardware-checks.md, H5).
DISPLAY_PACKAGES = {"libegl1", "libegl-mesa0", "libgles2", "libgl1-mesa-dri"}


def test_the_display_libraries_are_in_both_package_lists():
    for where, packages in (("the image list", image_packages()),
                            ("pi-setup.sh --appliance", appliance_packages())):
        missing = DISPLAY_PACKAGES - packages
        assert not missing, f"{where} is missing {sorted(missing)}; the panel would stay black"


def test_the_display_libraries_are_explained_where_they_are_listed():
    # A package nothing depends on, with no comment saying why it is there, is
    # the first thing a future cleanup deletes -- and this set is invisible to
    # every dependency the image has. So the explanation has to be AT the
    # package names, not merely somewhere in the same file: a reader deleting
    # the line has to be looking at the reason.
    # Anchored on libegl1 itself, not on "apt-get install -y": pi-setup.sh has
    # two apt lines and only the appliance one carries these packages.
    for name, text in (("the image list", PACKAGES_FILE.read_text()),
                       ("pi-setup.sh", (REPO / "tools" / "pi-setup.sh").read_text())):
        lines = text.splitlines()
        where = next(i for i, l in enumerate(lines)
                     if "libegl1" in l and not l.lstrip().startswith("#"))
        # The comment block immediately above the packages, with no blank line
        # or unrelated code between it and them.
        block, i = [], where - 1
        while i >= 0 and (lines[i].lstrip().startswith("#") or not lines[i].strip()):
            block.append(lines[i])
            i -= 1
        block = "\n".join(block)
        assert "runtime" in block, f"{name}: no runtime-loading explanation above the packages"
        for soname in ("libEGL.so.1", "libGLESv2.so.2"):
            assert soname in block, f"{name}: the block above the packages does not name {soname}"


def test_the_display_rationale_does_not_claim_the_dri_drivers_were_missing():
    # Round-1 correction. The original rationale said mesa-libgallium ships no
    # *_dri.so so the Pi had "no DRI driver at all". Inspecting the 26.2.2
    # debs disproved it: libEGL_mesa.so.0 and gbm/dri_gbm.so import no dlopen
    # and both DT_NEEDED libgallium, which has vc4 and v3d compiled in. The
    # drivers were always present. Keep the corrected story from regrowing the
    # old one.
    for name, path in (("the image list", PACKAGES_FILE),
                       ("pi-setup.sh", REPO / "tools" / "pi-setup.sh")):
        text = path.read_text()
        for claim in ("no DRI driver", "every *_dri.so entry point lives here",
                      "has no DRI driver whatsoever"):
            assert claim not in text, f"{name} still claims: {claim}"


def test_only_the_scoreboard_stage_exports_an_image():
    build = (PIGEN / "build.sh").read_text()
    assert "stage2/SKIP_IMAGES" in build
    assert (PIGEN / "stage-scoreboard" / "EXPORT_IMAGE").is_file()


def test_the_build_copies_only_what_the_appliance_needs_from_device():
    build = (PIGEN / "build.sh").read_text()
    # device/config holds a developer's real identity; it must never be copied.
    assert "device/config" not in build
    assert re.search(r'cp -a "\$REPO/device/"\*', build) is None, "copy an explicit list, not device/*"
    # What the read-only root needs travels with the rest (OTA design 4.3):
    # pi-setup.sh --appliance installs each of these by path inside the
    # chroot, and dies there if the copy list falls behind it.
    assert '"$REPO/device/generators"' in build
    assert '"$REPO/device/system.conf.d"' in build
    assert '"$REPO/device/NetworkManager.service.d"' in build
    assert '"$REPO/device/scoreboard-journal-prune"' in build
    assert '"$REPO/device/scoreboard-journal-prune.service"' in build
    setup = (REPO / "tools" / "pi-setup.sh").read_text()
    appliance = setup[setup.index("install_appliance() {"):]
    appliance = appliance[:appliance.index("\n}\n")]
    for name in re.findall(r'"\$DEVICE/([^"]+)"', appliance):
        top = name.split("/")[0]
        assert f'"$REPO/device/{top}"' in build, f"pi-setup.sh installs $DEVICE/{name} but build.sh does not copy device/{top}"


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


def test_the_journal_drop_in_shortens_the_sync_interval():
    # journald's default SyncIntervalSec is 5 minutes for ERR and below, and
    # the scoreboard's startup failures are logged at ERR. Someone watching a
    # black screen pulls the power long before five minutes are up, losing
    # exactly the line this feature exists to capture.
    assert "SyncIntervalSec=30s" in JOURNAL_RUN.read_text()


GATE = REPO / "tools" / "image-gate.sh"
WIZARD_STAGE = PIGEN / "stage-scoreboard" / "02-no-first-boot-wizard" / "00-run.sh"
# The quoted find predicates both files use to name a getty-ish unit or its
# drop-in directory: -name 'getty@*.service', -path '*/getty@*.service.d/*'.
#
# Anchored on the -name/-path that precedes them, not on the quotes alone.
# Without that anchor any other quoted mention of one of these unit names
# counts as a find predicate -- which it is not. The case that found it:
# tools/image-gate.sh grew an assert_masked 'serial-getty@.service' line, and
# this test then reported "the gate's unit set changed" for a file whose
# autologin scan had not changed at all. (It reads the GATE and
# 02-no-first-boot-wizard; 05-no-listeners, which does the masking, is never
# read here.)
UNIT_GLOB = re.compile(
    r"-(?:name|path)\s+'(?:\*/)?"
    r"((?:serial-getty|autovt|getty)@\*?\.service(?:\.d)?|console-getty\.service(?:\.d)?)(?:/\*)?'")


def autologin_unit_globs(text: str) -> set[str]:
    return {m.group(1) for m in UNIT_GLOB.finditer(text)}


def test_the_stage_and_the_gate_refuse_the_same_autologin_shapes():
    # The stage cleans what the gate refuses. If the two drift, a pi-gen bump
    # shipping a newly covered shape fails a release build thirty-five minutes
    # in rather than being cleaned by the stage that exists to clean it.
    expected = {
        "getty@*.service", "serial-getty@*.service", "autovt@*.service", "console-getty.service",
        "getty@*.service.d", "serial-getty@*.service.d", "autovt@*.service.d", "console-getty.service.d",
    }
    gate_globs = autologin_unit_globs(GATE.read_text())
    stage_globs = autologin_unit_globs(WIZARD_STAGE.read_text())
    assert gate_globs == expected, f"the gate's unit set changed: {gate_globs ^ expected}"
    assert stage_globs == gate_globs, f"the stage and the gate disagree: {stage_globs ^ gate_globs}"


def test_the_stage_and_the_gate_match_the_same_autologin_spellings():
    # --autologin is what raspi-config writes; the short form must be matched
    # attached (-api) as well as detached (-a pi), so neither pattern may
    # require a space after -a.
    for path in (WIZARD_STAGE, GATE):
        text = path.read_text()
        assert "--autologin" in text
        assert "agetty.*[[:space:]]-a" in text
        assert "agetty.*[[:space:]]-a[[:space:]]" not in text, \
            f"{path} still requires a space after -a, so the attached form (-api) slips through"


def test_the_stage_reads_drop_ins_the_way_systemd_does():
    # -xtype f so a drop-in symlinked to a real file is read, and -type l to
    # find the symlinked drop-in directory that find -P will not descend into.
    text = WIZARD_STAGE.read_text()
    assert "-xtype f" in text
    assert "-type l" in text


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


LISTENERS_RUN = PIGEN / "stage-scoreboard" / "05-no-listeners" / "00-run.sh"

# What the panel needs from a network is DHCP, DNS, NTP and outbound TLS. Each
# of these answered, radiated or armed something beyond that on a real v0.1.2
# boot or in the v0.1.3 build log. `ssh` is the metapackage, named so that
# purging openssh-server does not leave apt to decide.
PURGED_FOR_NETWORK_SURFACE = (
    "avahi-daemon", "libnss-mdns", "bluez", "bluez-firmware", "rpi-usb-gadget",
    "ssh-import-id", "rpi-update", "openssh-server", "openssh-sftp-server",
    "openssh-client", "ssh",
)
MASKED_FOR_NETWORK_SURFACE = (
    "avahi-daemon.service", "avahi-daemon.socket", "bluetooth.service",
    "sshswitch.service", "ssh.service", "ssh.socket", "sshd.service", "sshd.socket",
    # The template, not an instance: disable-bt turns GPIO 14/15 into a live
    # kernel console and systemd-getty-generator puts a login prompt on it.
    "serial-getty@.service",
)


def test_the_stage_purges_every_listener_package():
    # Purge, not remove, for the same reason 03-no-remote-access gives: a
    # removed-but-not-purged package keeps its stanza in /var/lib/dpkg/status,
    # which is the signal tools/image-gate.sh reads.
    run = LISTENERS_RUN.read_text()
    assert "on_chroot" in run
    assert "apt-get purge" in run
    assert "DEBIAN_FRONTEND=noninteractive" in run
    assert "apt-get remove" not in run
    for package in PURGED_FOR_NETWORK_SURFACE:
        assert re.search(rf"(?<![\w.+-]){re.escape(package)}(?![\w.+-])", run), \
            f"{package} is no longer purged"
    # pi-gen skips a sub-stage script that is not executable, silently.
    assert os.access(LISTENERS_RUN, os.X_OK), f"{LISTENERS_RUN} must be executable or pi-gen skips it"


def test_the_stage_masks_what_it_cannot_purge_away_for_good():
    # A mask on a purged package is not redundant: it is what stops the unit
    # being enabled if the package ever returns, because systemctl refuses to
    # enable a masked unit. sshswitch.service is the one whose package stays --
    # raspberrypi-sys-mods is load-bearing -- and it reads the boot partition.
    run = LISTENERS_RUN.read_text()
    assert "/dev/null" in run
    for unit in MASKED_FOR_NETWORK_SURFACE:
        assert unit in run, f"{unit} is no longer masked"


def test_the_stage_and_the_gate_agree_on_the_purged_and_masked_sets():
    # If the two drift, the stage stops removing something the gate still
    # refuses -- which fails a release build thirty-five minutes in rather
    # than being cleaned by the stage that exists to clean it.
    gate = GATE.read_text()
    for package in PURGED_FOR_NETWORK_SURFACE:
        assert package in gate, f"the gate no longer checks {package}"
    for unit in MASKED_FOR_NETWORK_SURFACE:
        assert unit in gate, f"the gate no longer asserts the mask on {unit}"


RESIZE_RUN = PIGEN / "stage-scoreboard" / "06-fixed-layout" / "00-run.sh"


def test_the_stage_disarms_the_first_boot_resize_in_both_halves():
    # pi-gen's cmdline carries `resize` (the initramfs grows the partition)
    # and stage2 enables rpi-resize.service (grows the filesystem). The
    # layout script drops the token; this stage masks the unit and removes
    # the enablement stage2 wrote, and the gate asserts both, so a pi-gen
    # bump that re-arms either fails the build rather than growing ROOT-A
    # on a card whose two roots must stay identical.
    run = RESIZE_RUN.read_text()
    assert os.access(RESIZE_RUN, os.X_OK), f"{RESIZE_RUN} must be executable or pi-gen skips it"
    assert 'ln -sfn /dev/null "${ROOTFS_DIR}/etc/systemd/system/rpi-resize.service"' in run
    assert "-name 'rpi-resize.service' -delete" in run
    assert "ConditionFirstBoot" in run, "the reason it is a mask and not a disable has to be written down"
    gate = GATE.read_text()
    assert "assert_masked rpi-resize.service" in gate
    assert "-name 'rpi-resize.service' -print -quit" in gate
    layout = (REPO / "tools" / "image-layout.sh").read_text()
    assert "resize$" in layout and '" resize "' in gate, "the token is dropped by the layout and refused by the gate"


def test_the_stage_turns_the_bluetooth_radio_off_in_the_device_tree():
    # Purging bluez stops the daemon, not the radio: the kernel attaches the
    # adapter from the device tree over HCI UART, and pi-bluetooth (which
    # would ship hciuart.service) is not installed, so there is no attach unit
    # to mask. disable-bt sets the &bt node to disabled, which is the only
    # place the radio can actually be switched off.
    run = LISTENERS_RUN.read_text()
    assert "dtoverlay=disable-bt" in run
    assert "boot/firmware/config.txt" in run
    # pi-gen's stage2/02-net-tweaks writes 0 (unblocked) into a systemd-rfkill
    # state file per known on-board address; 1 is blocked.
    assert ":bluetooth" in run
    assert "echo 1 >" in run


def test_the_wifi_firmware_is_not_what_the_stage_removes():
    # bluez-firmware ships Bluetooth HCI patch files only. The firmware the
    # panel cannot join a network without -- brcmfmac43455-sdio on the Pi 4,
    # brcmfmac43436-sdio on the Zero 2 W -- is in firmware-brcm80211, and
    # removing it would brick every panel.
    run = LISTENERS_RUN.read_text()
    assert "firmware-brcm80211" not in PURGED_FOR_NETWORK_SURFACE
    assert "firmware-brcm80211" in run, "the stage no longer says which firmware must stay"
    for load_bearing in ("firmware-brcm80211", "raspberrypi-sys-mods", "network-manager",
                         "libbluetooth3"):
        assert load_bearing not in PURGED_FOR_NETWORK_SURFACE


def test_the_stage_keeps_the_serial_console_but_not_its_login_prompt():
    # disable-bt makes the PL011 the primary UART, which makes enable_uart
    # default to 1, which turns the console=serial0,115200 already in
    # cmdline.txt into a live kernel console on GPIO 14/15. That console is
    # kept on purpose -- it is the diagnosis path a panel with no login and a
    # black screen has never had -- so the stage must NOT strip console= from
    # cmdline.txt, and must mask the login prompt instead.
    run = LISTENERS_RUN.read_text()
    assert "serial-getty@.service" in run, "the serial login prompt is no longer masked"
    assert "enable_uart" in run, "the stage no longer explains why a console appears"
    # Option B -- dropping console=serial0,115200 -- would mean editing
    # cmdline.txt. The stage only ever mentions that file in prose, so a
    # redirect or a sed against it means the decision changed and the comment
    # above it no longer describes the image.
    code = "\n".join(l for l in run.splitlines() if not l.lstrip().startswith("#"))
    assert "cmdline.txt" not in code, \
        "the stage now writes cmdline.txt; the serial-console decision changed"


def test_the_stage_reads_find_output_without_swallowing_a_failure():
    # `while ... done < <(find ...)` hides find's exit status from `bash -e`,
    # so a find that failed part-way would read as "nothing to rewrite" and
    # the stage would succeed having changed nothing.
    run = LISTENERS_RUN.read_text()
    assert "done < <(find" not in run, "a process substitution hides find's exit status"
    assert "$(find " in run


def test_the_stage_recreates_the_sshd_config_directory_pi_gen_writes_into():
    # export-image/01-user-rename runs rename-user AFTER this stage, and it
    # unconditionally writes /etc/ssh/sshd_config.d/rename_user.conf. Purging
    # openssh-server takes that directory away with it.
    run = LISTENERS_RUN.read_text()
    assert "/etc/ssh/sshd_config.d" in run
    assert "rename-user" in run
