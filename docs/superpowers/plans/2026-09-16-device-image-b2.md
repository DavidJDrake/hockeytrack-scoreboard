# Device Image B2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a credential-free Raspberry Pi OS image of the scoreboard appliance in CI, publish it only after an approved release, mirror it to a separate CDN, watch the mirror and the image bucket for tampering, and give strangers a page that tells them how to verify it before flashing.

**Architecture:**
- **Build:** a pinned pi-gen recipe runs the existing `tools/pi-setup.sh --appliance` inside the image's chroot. `tools/image-gate.sh` then inspects the mounted filesystem and fails the build on any baked-in secret.
- **Publish:** a tag builds and gates the image. A job in the `image-release` environment waits for the owner's approval, then creates the GitHub Release (the source of truth). It assumes an OIDC role scoped to that environment to mirror the image to `images.scoreboard.davidjdrake.com`.
- **Watch:** a daily Lambda compares the mirror, the manifest and the release checksum. HockeyTrack section 15 pages on any write to the image bucket, its distribution, the publisher role or the OIDC provider that isn't the release itself.

**Tech Stack:** pi-gen (`arm64` branch), GitHub Actions, Bash, Python/pytest, Go (aws-lambda-go, aws-sdk-go-v2), Terraform (AWS provider), Node `node:test`, EventBridge, CloudTrail.

**Spec:** `docs/superpowers/specs/2026-09-12-device-image-design.md` — §9 (revised 2026-09-16) is authoritative for B2; §6 is its background.

## Global Constraints

- **Both repositories are PUBLIC.** Never read, modify or commit `terraform/terraform.tfvars` or `device/config/`. No credentials, private keys, or real email addresses in any file, test fixture or log.
- **Agents never run** `terraform apply`, any mutating AWS or GitHub call (`gh release`, `gh api -X POST/PUT/PATCH/DELETE`, `gh variable set`, `git push`, `git tag`), `make deploy`, `make site`, `make provision`, or `tools/pi-gen/build.sh`. They may run `make test`, `terraform fmt -check`, `terraform validate`, read-only AWS calls with `--region us-east-1`, and `aws events test-event-pattern`.
- **Every AWS CLI command passes `--region us-east-1`.** US spelling throughout.
- **Every GitHub Action is pinned by full commit SHA** with a trailing `# vX.Y.Z` comment:
  - `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1`
  - `actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1`
  - `actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1`
  - `actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8 # v4.2.2`
  - `aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd # v6.3.0`
- **pi-gen is pinned** to `74d08a337bd29da289b9aedbe5b48c79fb2e5a03` on its `arm64` branch.
- **Image credentials stay unset:** `FIRST_USER_PASS`, `DISABLE_FIRST_BOOT_USER_RENAME`, `ENABLE_SSH`, `PUBKEY_SSH_FIRST_USER`, `WPA_PASSWORD`, `WPA_ESSID` must never appear in `tools/pi-gen/config`.
- **Names:**
  - release environment `image-release`;
  - OIDC subject `repo:DavidJDrake/hockeytrack-scoreboard:environment:image-release`;
  - role `scoreboard-image-publisher`;
  - bucket `scoreboard-images-<account id>`;
  - host `images.<site domain>` (= `images.scoreboard.davidjdrake.com`);
  - image file `scoreboard-<version>.img.xz`, with version matching `^v[0-9]+\.[0-9]+\.[0-9]+$`;
  - object keys `images/<version>/<file>` and `latest.json`;
  - Lambda `scoreboard-imagecheck`;
  - HockeyTrack rule `hockeytrack-sec-scoreboard-image`.
- **Scoreboard alarms** keep a literal `alarm_name` starting `scoreboard-` and send to `data.aws_sns_topic.security_alerts.arn`. Go Lambda builds use `-buildvcs=false -trimpath`, and every `archive_file` sets `output_file_mode = "0755"`.
- **Branches:** scoreboard `device-image-b2` (checked out; spec revision committed); HockeyTrack `image-supply-chain-detection`, created from `main` in Task 8.

## File map

**hockeytrack-scoreboard**

| File | Responsibility |
|---|---|
| `tools/image-gate.sh` (create) | Assert a mounted image carries no secret and is wired as an appliance |
| `device/tests/test_image_gate.py` (create) | The gate against a clean fixture rootfs and one broken fixture per assertion |
| `tools/pi-gen/PIGEN_REF`, `config`, `build.sh` (create) | Pinned pi-gen, image config, and the build entry point |
| `tools/pi-gen/stage-scoreboard/…` (create) | The custom stage: packages, install via `pi-setup.sh --appliance`, build identity |
| `device/tests/test_pi_gen_recipe.py` (create) | Tripwires: pin, forbidden settings, package list equals appliance mode's |
| `.github/workflows/image.yml` (create) | Build, gate, attest, and (tag + approval) publish |
| `site/tests/image-workflow.test.js` (create) | Tripwires on the workflow's triggers, permissions, pins and ordering |
| `terraform/images.tf` (create) | Bucket, CloudFront + OAC + cert + alias + CORS, publisher role, outputs |
| `cloud/cmd/imagecheck/{check.go,main.go,check_test.go}` (create) | The divergence monitor |
| `terraform/imagecheck.tf` (create) | Its function, role, schedule and alarms |
| `terraform/scheduler.tf` (modify) | Scheduler role may invoke the monitor too |
| `terraform/site.tf` (modify) | CSP `connect-src` gains the images host |
| `Makefile` (modify) | Build the monitor |
| `site/tests/images-config.test.js` (create) | Terraform tripwires for the mirror, role and monitor |
| `site/tests/signin-config.test.js` (modify) | Alarm-count floor 14 → 16 |
| `site/download/index.html`, `site/assets/download.js` (create) | The download page |
| `site/tests/download.test.js` (create) | Manifest validation, rendering helpers, page and CSP tripwires |
| `site/index.html`, `site/tests/pages.test.js` (modify) | "Prepare an SD card" links to the download page |
| `docs/hardware-checks.md` (modify) | Results of the hardware session (Task 11) |

**hockeytrack** (Task 8)

| File | Responsibility |
|---|---|
| `terraform/cloudtrail.tf` (modify) | Fourth selector: write-only S3 data events on the images bucket |
| `terraform/security-alarms.tf` (modify) | Section 15 and its registry entries |
| `docs/threat-model.md` (modify) | §4 paragraph and §7 recovery entry |

---

### Task 1: The no-secrets gate

> **Superseded in part (final fix wave, 2026-09-16).** Review rounds changed the committed files after this task's code was written. Where this block and `tools/image-gate.sh` and `device/tests/test_image_gate.py` disagree, the committed files are authoritative; this block records the starting point, not the result. Main differences: every probe fails closed; symlinked scan roots, newline paths, SSH via `.wants/`, `.requires/` or `.upholds/`, cloud-init (seed files, dpkg entry, config), PyPI packages in the venv and a pip cache all fail the gate; the call is `image-gate.sh <rootfs> <bootfs> [<repo>]`.

**Files:**
- Create: `tools/image-gate.sh`
- Create: `device/tests/test_image_gate.py`

**Interfaces:**
- **Produces:** `tools/image-gate.sh <rootfs> <bootfs> [<repo>]`. It exits 0 and prints `image-gate: all checks passed` when every assertion holds. Otherwise it exits 1 on the first failure, with one line on stderr starting `image-gate: FAIL: `. Task 3's workflow calls it with sudo against the loop-mounted image.

- [ ] **Step 1: Write the failing tests**

`device/tests/test_image_gate.py`:

```python
"""tools/image-gate.sh against fixture root filesystems.

A gate that cannot fail guards nothing, so every assertion gets a fixture that
breaks exactly it, and the clean fixture must pass. The fixtures are plain
directories; nothing is mounted and nothing needs root.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "tools" / "image-gate.sh"
# Built at runtime so no PEM private-key header sits in the repository.
FAKE_KEY = "-----BEGIN " + "PRIVATE KEY-----\nnot a key\n-----END " + "PRIVATE KEY-----\n"


def clean_image(tmp_path: Path) -> tuple[Path, Path]:
    root, boot = tmp_path / "root", tmp_path / "boot"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "scoreboard:x:996:996::/var/lib/scoreboard:/usr/sbin/nologin\n"
        "pi:x:1000:1000:,,,:/home/pi:/bin/bash\n"
        "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
    )
    (root / "etc" / "shadow").write_text("root:*:20000:0:99999:7:::\nscoreboard:!:20000::::::\npi:!:20000:0:99999:7:::\nnobody:*:20000:0:99999:7:::\n")
    units = root / "etc" / "systemd" / "system"
    wants = units / "multi-user.target.wants"
    wants.mkdir(parents=True)
    for unit in ("scoreboard.service", "scoreboard-netcfg.service"):
        (units / unit).write_text("[Unit]\n")
        (wants / unit).symlink_to(f"/etc/systemd/system/{unit}")
    rules = root / "etc" / "polkit-1" / "rules.d"
    rules.mkdir(parents=True)
    shutil.copy(REPO / "device" / "polkit" / "10-scoreboard-network.rules", rules)
    (root / "etc" / "scoreboard-build").write_text("v0.1.0 · 2026-09-16 · abc1234\n")
    (root / "etc" / "NetworkManager" / "system-connections").mkdir(parents=True)
    (root / "etc" / "ssh").mkdir()
    venv = root / "opt" / "scoreboard" / ".venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\ninclude-system-site-packages = true\nversion = 3.13.5\n")
    (root / "opt" / "scoreboard" / "certs").mkdir()
    shutil.copy(REPO / "device" / "certs" / "AmazonRootCA1.pem", root / "opt" / "scoreboard" / "certs")
    (root / "usr" / "lib" / "python3" / "dist-packages" / "pygame").mkdir(parents=True)
    (root / "var" / "lib" / "scoreboard").mkdir(parents=True)
    (root / "home" / "pi").mkdir(parents=True)
    (root / "root").mkdir()
    boot.mkdir()
    (boot / "config.txt").write_text("dtparam=audio=on\n")
    return root, boot


def gate(root: Path, boot: Path):
    return subprocess.run(["bash", str(GATE), str(root), str(boot), str(REPO)],
                          capture_output=True, text=True, timeout=60)


def test_a_clean_image_passes(tmp_path):
    root, boot = clean_image(tmp_path)
    result = gate(root, boot)
    assert result.returncode == 0, result.stderr
    assert "image-gate: all checks passed" in result.stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


BREAKS = {
    "an identity file under /var/lib/scoreboard": (
        lambda r, b: _write(r / "var/lib/scoreboard/device.json", "{}"), "device.json"),
    "a leftover enrollment": (
        lambda r, b: _write(r / "var/lib/scoreboard/enrollment.json", "{}"), "enrollment.json"),
    "an authorized_keys file": (
        lambda r, b: _write(r / "home/pi/.ssh/authorized_keys", "ssh-ed25519 AAAA test\n"), "authorized_keys"),
    "a first user with a password": (
        lambda r, b: (r / "etc/shadow").write_text((r / "etc/shadow").read_text().replace("pi:!:", "pi:$6$salt$hash:")), "pi"),
    "a first user with an empty password": (
        lambda r, b: (r / "etc/shadow").write_text((r / "etc/shadow").read_text().replace("pi:!:", "pi::")), "pi"),
    "SSH enabled by unit": (
        lambda r, b: (r / "etc/systemd/system/multi-user.target.wants/ssh.service").symlink_to("/lib/systemd/system/ssh.service"), "SSH"),
    "SSH enabled from the boot partition": (
        lambda r, b: _write(b / "ssh", ""), "SSH"),
    "a saved Wi-Fi connection": (
        lambda r, b: _write(r / "etc/NetworkManager/system-connections/home.nmconnection", "[wifi-security]\npsk=hunter2\n"), "Wi-Fi"),
    "a Wi-Fi password in wpa_supplicant": (
        lambda r, b: _write(r / "etc/wpa_supplicant/wpa_supplicant.conf", "network={\n psk=hunter2\n}\n"), "Wi-Fi"),
    "a setup file on the boot partition": (
        lambda r, b: _write(b / "scoreboard-setup.txt", "ssid=x\npsk=y\n"), "Wi-Fi"),
    "a venv without system site packages": (
        lambda r, b: (r / "opt/scoreboard/.venv/pyvenv.cfg").write_text("include-system-site-packages = false\n"), "system site packages"),
    "a venv carrying its own pygame": (
        lambda r, b: (r / "opt/scoreboard/.venv/lib/python3.13/site-packages/pygame").mkdir(parents=True), "pygame"),
    "the scoreboard unit not enabled": (
        lambda r, b: (r / "etc/systemd/system/multi-user.target.wants/scoreboard.service").unlink(), "scoreboard.service"),
    "the network unit not enabled": (
        lambda r, b: (r / "etc/systemd/system/multi-user.target.wants/scoreboard-netcfg.service").unlink(), "scoreboard-netcfg.service"),
    "a polkit rule that differs from the repository's": (
        lambda r, b: _write(r / "etc/polkit-1/rules.d/10-scoreboard-network.rules", "polkit.addRule(function(){return polkit.Result.YES;});\n"), "polkit"),
    "no build identity": (
        lambda r, b: (r / "etc/scoreboard-build").unlink(), "scoreboard-build"),
    "a multi-line build identity": (
        lambda r, b: (r / "etc/scoreboard-build").write_text("v0.1.0\nextra\n"), "scoreboard-build"),
    "a private key under /etc": (
        lambda r, b: _write(r / "etc/ssl/private/ssl-cert-snakeoil.key", FAKE_KEY), "private key"),
    "a private key inside the venv": (
        lambda r, b: _write(r / "opt/scoreboard/.venv/lib/python3.13/site-packages/pkg/tests/key.pem", FAKE_KEY), "private key"),
    "an SSH host key": (
        lambda r, b: _write(r / "etc/ssh/ssh_host_ed25519_key", "binary-ish"), "host key"),
    "an extra certificate beside the CA": (
        lambda r, b: _write(r / "opt/scoreboard/certs/extra.crt", "cert"), "certificate"),
    "a CA that is not Amazon's": (
        lambda r, b: _write(r / "opt/scoreboard/certs/AmazonRootCA1.pem", "not the CA\n"), "AmazonRootCA1"),
}


@pytest.mark.parametrize("name", sorted(BREAKS))
def test_each_assertion_can_fail(tmp_path, name):
    root, boot = clean_image(tmp_path)
    breaker, expected = BREAKS[name]
    breaker(root, boot)
    result = gate(root, boot)
    assert result.returncode == 1, f"{name}: gate passed a broken image\n{result.stdout}"
    assert "image-gate: FAIL:" in result.stderr
    assert expected in result.stderr, f"{name}: {result.stderr}"


def test_a_directory_that_is_not_a_rootfs_is_refused(tmp_path):
    (tmp_path / "boot").mkdir()
    result = gate(tmp_path / "empty", tmp_path / "boot")
    assert result.returncode == 1
    assert "root filesystem" in result.stderr
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd device && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy ../.venv/bin/pytest -q tests/test_image_gate.py`
Expected: every test FAILS, because `tools/image-gate.sh` does not exist (bash: No such file).

- [ ] **Step 3: Write the gate**

`tools/image-gate.sh` (mode 755):

```bash
#!/usr/bin/env bash
# Inspect a built scoreboard image before it is published.
#
#   tools/image-gate.sh <rootfs> <bootfs> [<repo>]
#
# Anyone on the internet can flash this image, so anything baked into it is
# shared by every panel that runs it: a private key, a password, an SSH key or
# a Wi-Fi password in the image is a secret handed to strangers. This turns
# "the image contains no secrets" into a check that fails the build. It
# inspects the filesystem; it does not boot the image. Booting is proven on
# real hardware (docs/hardware-checks.md).
#
# Exits 0 when every assertion holds, 1 on the first that does not.
set -euo pipefail

usage="usage: image-gate.sh <rootfs> <bootfs> [<repo>]"
ROOT="${1:?$usage}"
BOOT="${2:?$usage}"
REPO="${3:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

fail() { echo "image-gate: FAIL: $*" >&2; exit 1; }
ok() { echo "image-gate: ok: $*"; }

[ -f "$ROOT/etc/passwd" ] && [ -f "$ROOT/etc/shadow" ] || fail "$ROOT is not a root filesystem (no /etc/passwd or /etc/shadow)"
[ -d "$BOOT" ] || fail "$BOOT does not exist"

# A panel's identity and its pending enrollment are generated after first
# boot. One baked in here would make every panel the same panel.
for dir in var/lib/scoreboard opt/scoreboard; do
  for name in device.json device.pem.crt private.pem.key enrollment.json; do
    hit="$(find "$ROOT/$dir" -name "$name" -print -quit 2>/dev/null || true)"
    [ -z "$hit" ] || fail "/$dir contains $name, which a panel must generate for itself"
  done
done
ok "no identity or enrollment files"

hit="$(find "$ROOT" -xdev -name authorized_keys -print -quit 2>/dev/null || true)"
[ -z "$hit" ] || fail "authorized_keys present at ${hit#"$ROOT"}"
ok "no authorized_keys"

# root and every human account (uid 1000-65533) must be locked: a '!' or '*'
# hash. An empty hash is a passwordless login and fails too.
while IFS=: read -r name _ uid _; do
  if [ "$name" = root ] || { [ "$uid" -ge 1000 ] && [ "$uid" -lt 65534 ]; }; then
    hash="$(awk -F: -v u="$name" '$1 == u { print $2 }' "$ROOT/etc/shadow")"
    case "$hash" in
      '!'* | '*'*) ;;
      *) fail "account $name has a usable or empty password" ;;
    esac
  fi
done <"$ROOT/etc/passwd"
ok "accounts are locked"

for unit in ssh.service sshd.service ssh.socket; do
  for target in multi-user.target.wants sockets.target.wants; do
    [ ! -e "$ROOT/etc/systemd/system/$target/$unit" ] && [ ! -L "$ROOT/etc/systemd/system/$target/$unit" ] \
      || fail "SSH is enabled ($target/$unit)"
  done
done
for name in ssh ssh.txt; do
  [ ! -e "$BOOT/$name" ] || fail "SSH is enabled by /boot/$name"
done
ok "SSH is not enabled"

conns="$ROOT/etc/NetworkManager/system-connections"
if [ -d "$conns" ] && [ -n "$(find "$conns" -type f -print -quit)" ]; then
  fail "a saved Wi-Fi connection is present in /etc/NetworkManager/system-connections"
fi
if [ -d "$ROOT/etc/wpa_supplicant" ] && grep -rqsE '^[[:space:]]*psk=' "$ROOT/etc/wpa_supplicant"; then
  fail "a Wi-Fi password is present in /etc/wpa_supplicant"
fi
for name in custom.toml firstrun.sh wpa_supplicant.conf scoreboard-setup.txt; do
  [ ! -e "$BOOT/$name" ] || fail "the boot partition carries $name, which can hold Wi-Fi credentials"
done
ok "no Wi-Fi credentials"

cfg="$ROOT/opt/scoreboard/.venv/pyvenv.cfg"
[ -f "$cfg" ] || fail "no virtualenv at /opt/scoreboard/.venv"
grep -qE '^include-system-site-packages[[:space:]]*=[[:space:]]*true' "$cfg" \
  || fail "the virtualenv does not include system site packages, so it cannot see the distribution's pygame"
hit="$(find "$ROOT/opt/scoreboard/.venv" -type d -name pygame -print -quit 2>/dev/null || true)"
[ -z "$hit" ] || fail "the virtualenv carries its own pygame (${hit#"$ROOT"}), which has no kmsdrm driver"
[ -d "$ROOT/usr/lib/python3/dist-packages/pygame" ] || fail "the distribution's pygame is not installed"
ok "the virtualenv uses the distribution's pygame"

for unit in scoreboard.service scoreboard-netcfg.service; do
  [ -f "$ROOT/etc/systemd/system/$unit" ] || fail "$unit is not installed"
  [ -L "$ROOT/etc/systemd/system/multi-user.target.wants/$unit" ] || fail "$unit is not enabled"
done
ok "both units are enabled"

cmp -s "$ROOT/etc/polkit-1/rules.d/10-scoreboard-network.rules" "$REPO/device/polkit/10-scoreboard-network.rules" \
  || fail "the polkit rule is missing or differs from device/polkit/10-scoreboard-network.rules"
ok "the polkit rule matches the repository"

build="$ROOT/etc/scoreboard-build"
[ -f "$build" ] || fail "/etc/scoreboard-build is missing"
[ "$(wc -l <"$build")" -eq 1 ] && [ -n "$(tr -d '[:space:]' <"$build")" ] \
  || fail "/etc/scoreboard-build must be a single non-empty line"
ok "build identity: $(cat "$build")"

# A key found here is shared by every panel that flashes the image, whoever
# put it there -- a package's test fixture or a distribution snakeoil key
# included. The fix is to delete it in the stage, never to loosen this.
for dir in etc opt var home root; do
  [ -d "$ROOT/$dir" ] || continue
  hit="$(grep -rlIs -- '-----BEGIN [A-Z ]*PRIVATE KEY-----' "$ROOT/$dir" | head -n 1 || true)"
  [ -z "$hit" ] || fail "a private key is in the image at ${hit#"$ROOT"}"
done
hit="$(find "$ROOT/etc/ssh" -name 'ssh_host_*_key' -print -quit 2>/dev/null || true)"
[ -z "$hit" ] || fail "an SSH host key is in the image at ${hit#"$ROOT"}; each panel must generate its own"
ok "no private keys"

found="$(cd "$ROOT/opt/scoreboard" && find . -path ./.venv -prune -o -type f \
  \( -name '*.pem' -o -name '*.crt' -o -name '*.key' -o -name '*.p12' -o -name '*.pfx' \) -print | sort)"
[ "$found" = "./certs/AmazonRootCA1.pem" ] \
  || fail "unexpected certificate or key files under /opt/scoreboard: $(echo "$found" | tr '\n' ' ')"
cmp -s "$ROOT/opt/scoreboard/certs/AmazonRootCA1.pem" "$REPO/device/certs/AmazonRootCA1.pem" \
  || fail "/opt/scoreboard/certs/AmazonRootCA1.pem differs from the repository's copy"
ok "the only certificate shipped is Amazon's root CA"

echo "image-gate: all checks passed"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `chmod 755 tools/image-gate.sh && cd device && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy ../.venv/bin/pytest -q tests/test_image_gate.py`
Expected: all tests PASS: 1 clean, 22 breaks and 1 refusal. If a break passes the gate, fix the gate, not the test.

- [ ] **Step 5: Run the whole Python suite and commit**

Run: `make test-py` (expected: all pass)

```bash
git add tools/image-gate.sh device/tests/test_image_gate.py
git commit -m "feat: gate the device image on having no secrets

tools/image-gate.sh inspects a mounted image and fails on identity or
enrollment files, authorized_keys, unlocked accounts, SSH, saved Wi-Fi,
a venv pygame, missing units or polkit rule, a bad build identity, any
private key, or any certificate but Amazon's root CA. Every assertion has
a fixture that breaks it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The pi-gen recipe

> **Superseded in part (final fix wave, 2026-09-16).** Review rounds changed the committed files after this task's code was written. Where this block and `tools/pi-gen/` and `tools/pi-setup.sh` disagree, the committed files are authoritative; this block records the starting point, not the result. Main differences: `build.sh` fetches the pinned commit directly (`git init` + `git fetch --depth 1 origin <sha>`) instead of cloning `--branch arm64`; `config` sets `ENABLE_CLOUD_INIT=0` and `build.sh` skips `stage2/04-cloud-init`; `python3-paho-mqtt` is in both package lists and appliance mode runs pip only as `--no-index --no-cache-dir` (spec §9.2).

**Files:**
- Create: `tools/pi-gen/PIGEN_REF`
- Create: `tools/pi-gen/config`
- Create: `tools/pi-gen/build.sh`
- Create: `tools/pi-gen/stage-scoreboard/prerun.sh`
- Create: `tools/pi-gen/stage-scoreboard/EXPORT_IMAGE`
- Create: `tools/pi-gen/stage-scoreboard/00-packages/00-packages`
- Create: `tools/pi-gen/stage-scoreboard/01-install/00-run.sh`
- Create: `device/tests/test_pi_gen_recipe.py`

**Interfaces:**
- **Consumes:** `tools/pi-setup.sh --appliance` (existing, chroot-safe) and its apt package list in `install_appliance`.
- **Produces:** `tools/pi-gen/build.sh <version> <workdir>`, which leaves exactly one `<workdir>/deploy/*-scoreboard.img.xz`. Task 3 calls it with `<version>` matching `^v[0-9]+\.[0-9]+\.[0-9]+$` or `v0.0.0-dev.<run number>`.

- [ ] **Step 1: Write the failing tripwires**

`device/tests/test_pi_gen_recipe.py`:

```python
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd device && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy ../.venv/bin/pytest -q tests/test_pi_gen_recipe.py`
Expected: FAIL, since `tools/pi-gen/` does not exist.

- [ ] **Step 3: Write the recipe**

`tools/pi-gen/PIGEN_REF`:

```
74d08a337bd29da289b9aedbe5b48c79fb2e5a03
```

`tools/pi-gen/config`:

```
# pi-gen configuration for the scoreboard image. Built from pi-gen's arm64
# branch at the commit in PIGEN_REF.
#
# Deliberately absent, so pi-gen's own defaults keep the image credential-free
# (docs/superpowers/specs/2026-09-12-device-image-design.md, 6.1 and 9.2):
# FIRST_USER_PASS (unset: the first user is locked), DISABLE_FIRST_BOOT_USER_RENAME
# (unset: the user is renamed at first boot), ENABLE_SSH (unset: SSH is off).
# Adding any of them is a security decision, and device/tests/test_pi_gen_recipe.py
# fails the build until the spec says otherwise.
IMG_NAME=scoreboard
RELEASE=trixie
DEPLOY_COMPRESSION=xz
TARGET_HOSTNAME=scoreboard
LOCALE_DEFAULT=en_US.UTF-8
KEYBOARD_KEYMAP=us
KEYBOARD_LAYOUT="English (US)"
TIMEZONE_DEFAULT=America/New_York
STAGE_LIST="stage0 stage1 stage2 stage-scoreboard"
```

`tools/pi-gen/build.sh` (mode 755):

```bash
#!/usr/bin/env bash
# Build the scoreboard image with pi-gen in Docker.
#
#   tools/pi-gen/build.sh <version> <workdir>
#
# Leaves one <workdir>/deploy/*-scoreboard.img.xz. Needs Docker with
# --privileged and about 25 GB free. CI runs this; see
# .github/workflows/image.yml.
set -euo pipefail

VERSION="${1:?usage: build.sh <version> <workdir>}"
WORK="${2:?usage: build.sh <version> <workdir>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
REF="$(tr -d '[:space:]' <"$HERE/PIGEN_REF")"

case "$VERSION" in
  *[!A-Za-z0-9.-]* | "") echo "build.sh: bad version: $VERSION" >&2; exit 2 ;;
esac

rm -rf "$WORK"
git clone --quiet --branch arm64 https://github.com/RPi-Distro/pi-gen.git "$WORK"
git -C "$WORK" checkout --quiet --detach "$REF"
[ "$(git -C "$WORK" rev-parse HEAD)" = "$REF" ] || { echo "build.sh: pi-gen is not at $REF" >&2; exit 1; }

cp "$HERE/config" "$WORK/config"
cp -a "$HERE/stage-scoreboard" "$WORK/stage-scoreboard"
# Stage 2 is Raspberry Pi OS Lite; only the scoreboard stage exports an image.
touch "$WORK/stage2/SKIP_IMAGES"

# What the appliance needs, as an explicit list. The device's config directory
# holds a developer's own panel identity and must never reach an image.
files="$WORK/stage-scoreboard/01-install/files"
mkdir -p "$files/device"
cp -a "$REPO/device/scoreboard" "$REPO/device/requirements.txt" "$REPO/device/certs" \
  "$REPO/device/polkit" "$REPO/device/scoreboard-appliance.service" \
  "$REPO/device/scoreboard-netcfg.service" "$files/device/"
find "$files/device" -name '__pycache__' -type d -prune -exec rm -rf {} +
cp "$REPO/tools/pi-setup.sh" "$files/pi-setup.sh"
printf '%s · %s · %s\n' "$VERSION" "$(date -u +%Y-%m-%d)" "$(git -C "$REPO" rev-parse --short HEAD)" \
  >"$files/scoreboard-build"

(cd "$WORK" && ./build-docker.sh)

count="$(find "$WORK/deploy" -maxdepth 1 -name '*-scoreboard.img.xz' | wc -l)"
[ "$count" -eq 1 ] || { echo "build.sh: expected one image in $WORK/deploy, found $count" >&2; exit 1; }
```

`tools/pi-gen/stage-scoreboard/prerun.sh` (mode 755):

```bash
#!/bin/bash -e
if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi
```

`tools/pi-gen/stage-scoreboard/EXPORT_IMAGE`:

```
IMG_SUFFIX=""
```

`tools/pi-gen/stage-scoreboard/00-packages/00-packages`:

```
python3-pygame
python3-gpiozero
python3-venv
network-manager
polkitd
python3-cryptography
ca-certificates
```

`tools/pi-gen/stage-scoreboard/01-install/00-run.sh` (mode 755):

```bash
#!/bin/bash -e
# Install the appliance with the same script a hand-built panel uses, inside
# the image's chroot, then remove the source and record which build this is.
src="${ROOTFS_DIR}/tmp/scoreboard-src"
install -d "${src}/tools"
cp -a files/device "${src}/device"
install -m 755 files/pi-setup.sh "${src}/tools/pi-setup.sh"

on_chroot <<EOF
/tmp/scoreboard-src/tools/pi-setup.sh --appliance
EOF

rm -rf "${ROOTFS_DIR}/tmp/scoreboard-src"
install -m 644 files/scoreboard-build "${ROOTFS_DIR}/etc/scoreboard-build"
```

- [ ] **Step 4: Run the tripwires to verify they pass**

Run: `chmod 755 tools/pi-gen/build.sh tools/pi-gen/stage-scoreboard/prerun.sh tools/pi-gen/stage-scoreboard/01-install/00-run.sh && cd device && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy ../.venv/bin/pytest -q tests/test_pi_gen_recipe.py`
Expected: PASS, 7 tests. Also run `bash -n tools/pi-gen/build.sh tools/pi-gen/stage-scoreboard/prerun.sh tools/pi-gen/stage-scoreboard/01-install/00-run.sh`, which should print nothing. Do NOT run `build.sh`.

- [ ] **Step 5: Commit**

```bash
make test-py
git add tools/pi-gen device/tests/test_pi_gen_recipe.py
git commit -m "feat: a pinned pi-gen recipe for the scoreboard image

pi-gen arm64 at a pinned commit builds Trixie Lite plus a scoreboard stage
that runs pi-setup.sh --appliance in the chroot and records the build
identity. Tripwires keep credential settings out of the config and the
package list equal to appliance mode's.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: The build and publish workflow

> **Superseded in part (final fix wave, 2026-09-16).** Review rounds changed the committed files after this task's code was written. Where this block and `.github/workflows/image.yml` and `site/tests/image-workflow.test.js` disagree, the committed files are authoritative; this block records the starting point, not the result. Main differences: the build job is `contents: read` only; a separate `attest` job (`needs: build`, tag push only, `id-token`/`attestations: write`, no checkout) re-checks the sha256 and attests; `publish` needs `[build, attest]`, re-resolves the tag, accepts an existing Release only if complete, creates an older release with `--latest=false`, and keeps `latest.json` monotonic; the rootfs mounts `ro,noload`; the artifact is kept 30 days (spec §9.4).

**Files:**
- Create: `.github/workflows/image.yml`
- Create: `site/tests/image-workflow.test.js`

**Interfaces:**
- **Consumes:**
  - `tools/pi-gen/build.sh <version> <workdir>` (Task 2);
  - `tools/image-gate.sh <rootfs> <bootfs> <repo>` (Task 1);
  - repository variables `IMAGE_PUBLISHER_ROLE_ARN`, `IMAGES_BUCKET` and `IMAGES_DISTRIBUTION_ID`, which Task 7 sets from Task 4's Terraform outputs;
  - the `image-release` environment (Task 7).
- **Produces:**
  - GitHub Release `<version>` with `scoreboard-<version>.img.xz` and `scoreboard-<version>.img.xz.sha256`;
  - a build provenance attestation;
  - objects `images/<version>/<both files>` and `latest.json` shaped `{"version","file","sha256","size","released","release"}` (Tasks 5 and 6 read it).

- [ ] **Step 1: Write the failing tripwires**

`site/tests/image-workflow.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The workflow that publishes an image strangers flash. Read as text: these
// catch a widened trigger, a loosened permission, an unpinned action, or a
// publish step that no longer waits for the gate or for approval.
let wf = "";
try {
  wf = readFileSync(new URL("../../.github/workflows/image.yml", import.meta.url), "utf8");
} catch {
  // Reported by the tests below.
}
const job = (name) => {
  const start = wf.indexOf(`\n  ${name}:\n`);
  assert.ok(start >= 0, `job ${name} not found`);
  const next = wf.slice(start + 1).search(/\n  [a-z][a-z-]*:\n/);
  return next < 0 ? wf.slice(start) : wf.slice(start, start + 1 + next);
};

test("the workflow runs only on version tags and by hand", () => {
  const on = wf.slice(wf.indexOf("\non:"), wf.indexOf("\npermissions:"));
  assert.match(on, /tags: \["v\*"\]/);
  assert.match(on, /workflow_dispatch:/);
  assert.doesNotMatch(on, /pull_request|branches:/, "a branch or pull request must never build a publishable image");
});

test("the default token can only read", () => {
  assert.match(wf, /\npermissions:\n  contents: read\n/);
});

test("every action is pinned to a full commit", () => {
  const uses = [...wf.matchAll(/uses: (\S+)/g)].map((m) => m[1]);
  assert.ok(uses.length >= 5, `found only ${uses.length} actions`);
  for (const u of uses) assert.match(u, /@[0-9a-f]{40}$/, `unpinned: ${u}`);
});

test("the image is gated before it is uploaded or attested", () => {
  const build = job("build");
  const gate = build.indexOf("tools/image-gate.sh");
  assert.ok(gate > 0, "the build job never runs the gate");
  for (const later of ["actions/upload-artifact", "actions/attest-build-provenance"]) {
    assert.ok(build.indexOf(later) > gate, `${later} must come after the gate`);
  }
});

test("publishing waits for a tagged, approved release", () => {
  const publish = job("publish");
  assert.match(publish, /needs: build/);
  assert.match(publish, /if: needs\.build\.outputs\.publish == 'true'/);
  assert.match(publish, /environment: image-release/);
  assert.match(publish, /permissions:\n      contents: write\n      id-token: write\n/);
});

test("AWS is reached through OIDC, never a stored key", () => {
  assert.match(job("publish"), /role-to-assume: \$\{\{ vars\.IMAGE_PUBLISHER_ROLE_ARN \}\}/);
  assert.doesNotMatch(wf, /aws-access-key-id|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY/);
});

test("only a strict version tag publishes", () => {
  assert.match(job("build"), /\^v\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+\$/);
});

test("the publish job re-checks the checksum before releasing", () => {
  const publish = job("publish");
  const check = publish.indexOf("sha256sum -c");
  assert.ok(check > 0);
  assert.ok(publish.indexOf("gh release create") > check);
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd site && node --test tests/image-workflow.test.js`
Expected: FAIL, since the workflow file does not exist.

- [ ] **Step 3: Write the workflow**

`.github/workflows/image.yml`:

```yaml
name: Image

# Builds the scoreboard SD card image. A tag builds, gates and attests it,
# then waits for approval of the image-release environment before anything is
# published. A manual run builds and gates only. See
# docs/superpowers/specs/2026-09-12-device-image-design.md, section 9.
on:
  push:
    tags: ["v*"]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: image-${{ github.ref }}
  cancel-in-progress: false

jobs:
  build:
    name: Build, gate and attest
    runs-on: ubuntu-24.04
    timeout-minutes: 300
    permissions:
      contents: read
      id-token: write
      attestations: write
    outputs:
      version: ${{ steps.version.outputs.version }}
      publish: ${{ steps.version.outputs.publish }}
    steps:
      - name: Check out
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Decide the version
        id: version
        env:
          REF_TYPE: ${{ github.ref_type }}
          REF_NAME: ${{ github.ref_name }}
          RUN: ${{ github.run_number }}
        run: |
          if [ "$REF_TYPE" = tag ] && [[ "$REF_NAME" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            echo "version=$REF_NAME" >>"$GITHUB_OUTPUT"
            echo "publish=true" >>"$GITHUB_OUTPUT"
          else
            echo "version=v0.0.0-dev.$RUN" >>"$GITHUB_OUTPUT"
            echo "publish=false" >>"$GITHUB_OUTPUT"
          fi

      # pi-gen needs about 20 GB; the runner image ships toolchains this build
      # never uses. Fail here rather than two hours into the build.
      - name: Free disk space
        run: |
          sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc /opt/hostedtoolcache/CodeQL /usr/local/share/boost
          sudo docker image prune --all --force
          free_gb="$(df --output=avail -BG / | tail -n 1 | tr -dc 0-9)"
          echo "free: ${free_gb} GB"
          [ "$free_gb" -ge 25 ] || { echo "need 25 GB free, have ${free_gb} GB" >&2; exit 1; }

      - name: Build the image
        env:
          VERSION: ${{ steps.version.outputs.version }}
        run: tools/pi-gen/build.sh "$VERSION" "$RUNNER_TEMP/pi-gen"

      - name: Gate the image
        run: |
          set -euo pipefail
          img="$(ls "$RUNNER_TEMP"/pi-gen/deploy/*-scoreboard.img.xz)"
          xz -dc "$img" >"$RUNNER_TEMP/scoreboard.img"
          loop="$(sudo losetup --find --show --partscan --read-only "$RUNNER_TEMP/scoreboard.img")"
          mkdir -p "$RUNNER_TEMP/rootfs" "$RUNNER_TEMP/bootfs"
          sudo mount -o ro "${loop}p2" "$RUNNER_TEMP/rootfs"
          sudo mount -o ro "${loop}p1" "$RUNNER_TEMP/bootfs"
          sudo tools/image-gate.sh "$RUNNER_TEMP/rootfs" "$RUNNER_TEMP/bootfs" "$GITHUB_WORKSPACE"
          sudo umount "$RUNNER_TEMP/rootfs" "$RUNNER_TEMP/bootfs"
          sudo losetup -d "$loop"
          rm "$RUNNER_TEMP/scoreboard.img"

      - name: Name and checksum
        env:
          VERSION: ${{ steps.version.outputs.version }}
        run: |
          mkdir -p out
          mv "$RUNNER_TEMP"/pi-gen/deploy/*-scoreboard.img.xz "out/scoreboard-$VERSION.img.xz"
          (cd out && sha256sum "scoreboard-$VERSION.img.xz" >"scoreboard-$VERSION.img.xz.sha256")
          cat "out/scoreboard-$VERSION.img.xz.sha256"

      - name: Attest build provenance
        if: steps.version.outputs.publish == 'true'
        uses: actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8 # v4.2.2
        with:
          subject-path: out/scoreboard-${{ steps.version.outputs.version }}.img.xz

      - name: Keep the image for the publish job
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: image
          path: out/
          if-no-files-found: error
          retention-days: 7
          compression-level: 0

  publish:
    name: Publish
    needs: build
    if: needs.build.outputs.publish == 'true'
    runs-on: ubuntu-24.04
    timeout-minutes: 60
    environment: image-release
    permissions:
      contents: write
      id-token: write
    env:
      VERSION: ${{ needs.build.outputs.version }}
    steps:
      - name: Fetch the gated image
        uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
        with:
          name: image
          path: out

      - name: Re-check the checksum
        run: cd out && sha256sum -c "scoreboard-$VERSION.img.xz.sha256"

      - name: Create the GitHub Release
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
        run: |
          gh release create "$VERSION" \
            "out/scoreboard-$VERSION.img.xz" "out/scoreboard-$VERSION.img.xz.sha256" \
            --verify-tag \
            --title "Scoreboard image $VERSION" \
            --notes "Verify before you flash: https://scoreboard.davidjdrake.com/download/"

      - name: Assume the publisher role
        uses: aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd # v6.3.0
        with:
          role-to-assume: ${{ vars.IMAGE_PUBLISHER_ROLE_ARN }}
          role-session-name: image-publish-${{ github.run_id }}
          aws-region: us-east-1

      - name: Mirror to the image CDN
        env:
          BUCKET: ${{ vars.IMAGES_BUCKET }}
          DISTRIBUTION: ${{ vars.IMAGES_DISTRIBUTION_ID }}
          RELEASE_URL: ${{ github.server_url }}/${{ github.repository }}/releases/tag/${{ needs.build.outputs.version }}
        run: |
          set -euo pipefail
          file="scoreboard-$VERSION.img.xz"
          sha="$(cut -d' ' -f1 "out/$file.sha256")"
          size="$(stat -c %s "out/$file")"
          aws s3 cp "out/$file" "s3://$BUCKET/images/$VERSION/$file" --region us-east-1 \
            --checksum-algorithm SHA256 --content-type application/x-xz \
            --cache-control "public, max-age=31536000, immutable"
          aws s3 cp "out/$file.sha256" "s3://$BUCKET/images/$VERSION/$file.sha256" --region us-east-1 \
            --content-type text/plain --cache-control "public, max-age=31536000, immutable"
          python3 - "$VERSION" "$file" "$sha" "$size" "$RELEASE_URL" >latest.json <<'PY'
          import datetime, json, sys
          version, file, sha, size, release = sys.argv[1:]
          released = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
          print(json.dumps({"version": version, "file": file, "sha256": sha, "size": int(size),
                            "released": released, "release": release}, indent=2))
          PY
          aws s3 cp latest.json "s3://$BUCKET/latest.json" --region us-east-1 \
            --content-type application/json --cache-control "public, max-age=60"
          aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION" --paths /latest.json
```

**Note on the heredoc:** YAML strips the block's common indentation, so the `python3` heredoc body and its closing `PY` arrive at column 0, which is what bash requires. Keep them at the same indentation as the surrounding shell lines.

- [ ] **Step 4: Run the tripwires to verify they pass**

Run: `cd site && node --test tests/image-workflow.test.js`
Expected: PASS, 8 tests.

Also check the YAML parses: `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/image.yml'))"`. If PyYAML is missing, run `.venv/bin/pip install pyyaml` into the repo's venv for this check only; do not add it to any requirements file.

- [ ] **Step 5: Commit**

```bash
make test-js
git add .github/workflows/image.yml site/tests/image-workflow.test.js
git commit -m "feat: build, gate and publish the image from an approved release

A v* tag builds with pi-gen, gates the mounted image, checksums and
attests it, then waits in the image-release environment before creating
the GitHub Release and mirroring it through an OIDC role. Manual runs
build and gate only. Tripwires hold the triggers, permissions and pins.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: The image mirror and the publisher role

> **Superseded in part (final fix wave, 2026-09-16).** Review rounds changed the committed files after this task's code was written. Where this block and `terraform/images.tf` and `site/tests/images-config.test.js` disagree, the committed files are authoritative; this block records the starting point, not the result. Main differences: the publisher policy also allows `s3:GetObject` on `latest.json` and `s3:ListBucket` conditioned on prefix `latest.json` (spec §9.5), and the tests pin every policy statement exactly.

**Files:**
- Create: `terraform/images.tf`
- Create: `site/tests/images-config.test.js`

**Interfaces:**
- **Consumes:**
  - `data.aws_caller_identity.current`, `data.aws_route53_zone.site`, `var.site_domain` and `data.aws_cloudfront_cache_policy.optimized` (all existing, in `site.tf`);
  - the account's existing GitHub OIDC provider, which is owned by the `davidjdrake.com` repository's Terraform.
- **Produces:**
  - `local.images_domain` (`images.${var.site_domain}`), used by Task 6's CSP;
  - `aws_s3_bucket.images`, `aws_cloudfront_distribution.images` and `aws_iam_role.image_publisher`, used by Task 5;
  - outputs `images_bucket`, `images_distribution_id`, `image_publisher_role_arn` and `images_domain`, which Task 7 sets as repository variables and Task 8 uses.

- [ ] **Step 1: Write the failing tripwires**

`site/tests/images-config.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The mirror strangers download the image from, and the role that writes to
// it. Read as text: these catch a widened trust, a broader grant, a public
// bucket, or a CORS rule that lets any site read the manifest.
let images = "";
try {
  images = readFileSync(new URL("../../terraform/images.tf", import.meta.url), "utf8");
} catch {
  // Reported by the tests below.
}

function block(src, header) {
  const start = src.indexOf(header);
  assert.ok(start >= 0, `could not find ${header}`);
  const end = src.indexOf("\n}\n", start);
  assert.ok(end > start, `could not find the end of ${header}`);
  return src.slice(start, end);
}
const code = (text) => text.split("\n").map((line) => line.replace(/#.*$/, "")).join("\n");

test("the publisher role trusts only the approved release environment", () => {
  const trust = code(block(images, 'data "aws_iam_policy_document" "image_publisher_trust" {'));
  assert.match(trust, /"sts:AssumeRoleWithWebIdentity"/);
  assert.match(trust, /test\s*=\s*"StringEquals"\s*\n\s*variable\s*=\s*"token\.actions\.githubusercontent\.com:sub"\s*\n\s*values\s*=\s*\["repo:DavidJDrake\/hockeytrack-scoreboard:environment:image-release"\]/);
  assert.match(trust, /variable\s*=\s*"token\.actions\.githubusercontent\.com:aud"\s*\n\s*values\s*=\s*\["sts\.amazonaws\.com"\]/);
  assert.doesNotMatch(trust, /StringLike|refs\/heads|refs\/tags/, "the trust must name the environment exactly, not a ref pattern");
});

test("the provider is looked up, not created here", () => {
  assert.match(code(images), /data "aws_iam_openid_connect_provider" "github"/);
  assert.doesNotMatch(code(images), /resource "aws_iam_openid_connect_provider"/);
});

test("the publisher can upload and invalidate, and nothing else", () => {
  const policy = code(block(images, 'data "aws_iam_policy_document" "image_publisher" {'));
  const actions = [...policy.matchAll(/"([a-z0-9]+:[A-Za-z*]+)"/g)].map((m) => m[1]).sort();
  assert.deepEqual(actions, ["cloudfront:CreateInvalidation", "s3:AbortMultipartUpload", "s3:PutObject"]);
  assert.doesNotMatch(policy, /Delete|GetObject|"\*"/);
  assert.match(policy, /\$\{aws_s3_bucket\.images\.arn\}\/images\/\*/);
  assert.match(policy, /\$\{aws_s3_bucket\.images\.arn\}\/latest\.json/);
});

test("the bucket is private, versioned and TLS-only", () => {
  const bpa = code(block(images, 'resource "aws_s3_bucket_public_access_block" "images" {'));
  for (const key of ["block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"]) {
    assert.match(bpa, new RegExp(`${key}\\s*=\\s*true`), key);
  }
  assert.match(code(block(images, 'resource "aws_s3_bucket_versioning" "images" {')), /status\s*=\s*"Enabled"/);
  const policy = code(block(images, 'data "aws_iam_policy_document" "images_bucket" {'));
  assert.match(policy, /aws:SecureTransport/);
  assert.match(policy, /AWS:SourceArn/);
});

test("only the site may read the manifest cross-origin", () => {
  const cors = code(block(images, 'resource "aws_cloudfront_response_headers_policy" "images" {'));
  assert.match(cors, /access_control_allow_origins\s*\{\s*items\s*=\s*\["https:\/\/\$\{var\.site_domain\}"\]/);
  assert.doesNotMatch(cors, /items\s*=\s*\["\*"\]/);
});

test("the distribution reads the bucket through origin access control over HTTPS", () => {
  const dist = code(block(images, 'resource "aws_cloudfront_distribution" "images" {'));
  assert.match(dist, /origin_access_control_id\s*=\s*aws_cloudfront_origin_access_control\.images\.id/);
  assert.match(dist, /viewer_protocol_policy\s*=\s*"redirect-to-https"/);
  assert.match(dist, /path_pattern\s*=\s*"\/latest\.json"/);
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd site && node --test tests/images-config.test.js`
Expected: FAIL, since `terraform/images.tf` does not exist.

- [ ] **Step 3: Write `terraform/images.tf`**

```hcl
# The image mirror: where strangers download the scoreboard SD card image.
# GitHub Releases is the source of truth; this is a copy, in a bucket and
# distribution separate from the website so that compromising the site cannot
# swap the image people flash (docs/superpowers/specs/2026-09-12-device-image-design.md,
# 6.4 and 9.5). HockeyTrack's security-alarms.tf section 15 pages on any write
# to it that is not the release itself, and cmd/imagecheck compares it daily
# against the release.

locals {
  images_domain = "images.${var.site_domain}"
}

resource "aws_s3_bucket" "images" {
  bucket = "scoreboard-images-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "images" {
  bucket                  = aws_s3_bucket.images.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "images" {
  bucket = aws_s3_bucket.images.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# A replaced object keeps its previous version, so an overwritten image can be
# recovered and compared.
resource "aws_s3_bucket_versioning" "images" {
  bucket = aws_s3_bucket.images.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "images" {
  bucket = aws_s3_bucket.images.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_cloudfront_origin_access_control" "images" {
  name                              = "scoreboard-images"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

data "aws_iam_policy_document" "images_bucket" {
  statement {
    sid       = "CloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.images.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.images.arn]
    }
  }
  statement {
    sid       = "TLSOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.images.arn, "${aws_s3_bucket.images.arn}/*"]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "images" {
  bucket     = aws_s3_bucket.images.id
  policy     = data.aws_iam_policy_document.images_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.images]
}

resource "aws_acm_certificate" "images" {
  domain_name       = local.images_domain
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "images_cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.images.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }
  zone_id         = data.aws_route53_zone.site.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "images" {
  certificate_arn         = aws_acm_certificate.images.arn
  validation_record_fqdns = [for r in aws_route53_record.images_cert_validation : r.fqdn]
}

# latest.json changes with every release; a minute is how long a new release
# can take to appear. Everything under images/ is immutable per version.
resource "aws_cloudfront_cache_policy" "images_latest" {
  name        = "scoreboard-images-latest"
  min_ttl     = 0
  default_ttl = 60
  max_ttl     = 60
  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

# The download page on the site reads latest.json cross-origin; nothing else
# needs to.
resource "aws_cloudfront_response_headers_policy" "images" {
  name = "scoreboard-images"
  cors_config {
    access_control_allow_credentials = false
    access_control_allow_headers {
      items = ["Content-Type"]
    }
    access_control_allow_methods {
      items = ["GET", "HEAD"]
    }
    access_control_allow_origins {
      items = ["https://${var.site_domain}"]
    }
    access_control_max_age_sec = 600
    origin_override            = true
  }
  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      override                   = true
    }
    content_type_options {
      override = true
    }
  }
}

resource "aws_cloudfront_distribution" "images" {
  enabled         = true
  is_ipv6_enabled = true
  http_version    = "http2and3"
  aliases         = [local.images_domain]
  price_class     = "PriceClass_100"
  comment         = local.images_domain

  origin {
    domain_name              = aws_s3_bucket.images.bucket_regional_domain_name
    origin_id                = "images-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.images.id
  }

  default_cache_behavior {
    target_origin_id           = "images-s3"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = false
    cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.images.id
  }

  ordered_cache_behavior {
    path_pattern               = "/latest.json"
    target_origin_id           = "images-s3"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = aws_cloudfront_cache_policy.images_latest.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.images.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.images.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}

resource "aws_route53_record" "images_a" {
  zone_id = data.aws_route53_zone.site.zone_id
  name    = local.images_domain
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.images.domain_name
    zone_id                = aws_cloudfront_distribution.images.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "images_aaaa" {
  zone_id = data.aws_route53_zone.site.zone_id
  name    = local.images_domain
  type    = "AAAA"
  alias {
    name                   = aws_cloudfront_distribution.images.domain_name
    zone_id                = aws_cloudfront_distribution.images.hosted_zone_id
    evaluate_target_health = false
  }
}

# The GitHub OIDC provider belongs to the davidjdrake.com repository's
# Terraform (terraform/github_oidc.tf there). It is looked up, not created: a
# second definition would fight over the same provider. Deleting it there
# stops this repository's releases from publishing.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

# GitHub gives a job the subject repo:<owner>/<repo>:environment:<name> only
# when it runs in that environment, and image-release is restricted to v*
# tags behind a required reviewer. So this role trusts an approved release,
# not a tag push, a branch, a pull request, or another repository.
data "aws_iam_policy_document" "image_publisher_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:DavidJDrake/hockeytrack-scoreboard:environment:image-release"]
    }
  }
}

resource "aws_iam_role" "image_publisher" {
  name                 = "scoreboard-image-publisher"
  assume_role_policy   = data.aws_iam_policy_document.image_publisher_trust.json
  max_session_duration = 3600
}

# Upload and invalidate, and nothing else: no delete, no read, no other
# bucket. A compromised release can add a version; it cannot remove history,
# which versioning keeps, or touch the website.
data "aws_iam_policy_document" "image_publisher" {
  statement {
    sid       = "Upload"
    actions   = ["s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${aws_s3_bucket.images.arn}/images/*", "${aws_s3_bucket.images.arn}/latest.json"]
  }
  statement {
    sid       = "Invalidate"
    actions   = ["cloudfront:CreateInvalidation"]
    resources = [aws_cloudfront_distribution.images.arn]
  }
}

resource "aws_iam_role_policy" "image_publisher" {
  name   = "publish-images"
  role   = aws_iam_role.image_publisher.id
  policy = data.aws_iam_policy_document.image_publisher.json
}

output "images_bucket" {
  value = aws_s3_bucket.images.bucket
}

output "images_distribution_id" {
  value = aws_cloudfront_distribution.images.id
}

output "image_publisher_role_arn" {
  value = aws_iam_role.image_publisher.arn
}

output "images_domain" {
  value = local.images_domain
}
```

- [ ] **Step 4: Run the tripwires and the Terraform checks**

Run:
```bash
cd site && node --test tests/images-config.test.js && cd ..
terraform -chdir=terraform fmt -check && terraform -chdir=terraform validate
```
Expected: 6 tests PASS; `fmt` silent (run `terraform -chdir=terraform fmt` and re-check if not); `validate` prints "Success!". Do not run `plan`.

- [ ] **Step 5: Commit**

```bash
git add terraform/images.tf site/tests/images-config.test.js
git commit -m "feat: a separate mirror for the image, and a role only a release can assume

A private, versioned, TLS-only bucket behind its own CloudFront distribution
at images.<site domain>, readable cross-origin only by the site. The
publisher role trusts the image-release environment's OIDC subject exactly
and can only upload and invalidate.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: The divergence monitor

> **Superseded in part (final fix wave, 2026-09-16).** Review rounds changed the committed files after this task's code was written. Where this block and `cloud/cmd/imagecheck/` and `terraform/imagecheck.tf` disagree, the committed files are authoritative; this block records the starting point, not the result. Main differences: it runs twice a day (`cron(0 11,23 * * ? *)`) with no async retries, has a third alarm `scoreboard-imagecheck-not-running`, reports a checksum 404 as a disagreement, and limits redirects to GitHub hosts (spec §9.6).

**Files:**
- Create: `cloud/cmd/imagecheck/check.go`
- Create: `cloud/cmd/imagecheck/github.go`
- Create: `cloud/cmd/imagecheck/main.go`
- Create: `cloud/cmd/imagecheck/check_test.go`
- Create: `terraform/imagecheck.tf`
- Modify: `terraform/scheduler.tf` (`aws_iam_role_policy.scheduler`)
- Modify: `Makefile` (`build` target)
- Modify: `cloud/go.mod`, `cloud/go.sum`
- Modify: `site/tests/images-config.test.js` (append)
- Modify: `site/tests/signin-config.test.js` (`alarms >= 14` → `alarms >= 16`)

**Interfaces:**
- **Consumes:**
  - `aws_s3_bucket.images` (Task 4);
  - `latest.json` and `images/<version>/<file>` as written by Task 3;
  - `data.aws_sns_topic.security_alerts` and `aws_scheduler_schedule_group.main` (existing);
  - GitHub's public API for `DavidJDrake/hockeytrack-scoreboard`.
- **Produces:**
  - Lambda `scoreboard-imagecheck`, daily;
  - alarms `scoreboard-imagecheck-errors` and `scoreboard-imagecheck-throttles`;
  - SNS subject `SCOREBOARD SECURITY: image mirror disagrees with its release` (Task 10 watches for it).

- [ ] **Step 1: Add the SDK modules**

Run: `export PATH=$HOME/.local/share/go/bin:$PATH && cd cloud && go get github.com/aws/aws-sdk-go-v2/service/s3 github.com/aws/aws-sdk-go-v2/service/sns`
Expected: both appear as direct requirements in `go.mod`.

- [ ] **Step 2: Write the failing tests**

`cloud/cmd/imagecheck/check_test.go`:

```go
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type fakeObjects struct {
	objects map[string][]byte
	read    []string
}

func (f *fakeObjects) Get(_ context.Context, key string) (io.ReadCloser, error) {
	f.read = append(f.read, key)
	b, ok := f.objects[key]
	if !ok {
		return nil, ErrNotFound
	}
	return io.NopCloser(bytes.NewReader(b)), nil
}

type fakeReleases struct {
	latest    string
	latestErr error
	sums      map[string]string
	sumErr    error
}

func (f fakeReleases) LatestTag(context.Context) (string, error) { return f.latest, f.latestErr }
func (f fakeReleases) Checksum(_ context.Context, tag, file string) (string, error) {
	if f.sumErr != nil {
		return "", f.sumErr
	}
	return f.sums[tag+"/"+file], nil
}

type fakeNotifier struct{ subjects, messages []string }

func (f *fakeNotifier) Notify(_ context.Context, subject, message string) error {
	f.subjects = append(f.subjects, subject)
	f.messages = append(f.messages, message)
	return nil
}

var image = []byte("pretend this is an xz image")

func sum(b []byte) string { s := sha256.Sum256(b); return hex.EncodeToString(s[:]) }

func manifest(version, file, sha string) []byte {
	return []byte(`{"version":"` + version + `","file":"` + file + `","sha256":"` + sha + `","size":27,"released":"2026-09-16T00:00:00Z","release":"https://github.com/x"}`)
}

func agreeing() (*fakeObjects, fakeReleases) {
	objs := &fakeObjects{objects: map[string][]byte{
		"latest.json": manifest("v0.1.0", "scoreboard-v0.1.0.img.xz", sum(image)),
		"images/v0.1.0/scoreboard-v0.1.0.img.xz": image,
	}}
	rel := fakeReleases{latest: "v0.1.0", sums: map[string]string{"v0.1.0/scoreboard-v0.1.0.img.xz": sum(image)}}
	return objs, rel
}

func TestAMirrorThatAgreesRaisesNothing(t *testing.T) {
	objs, rel := agreeing()
	n := &fakeNotifier{}
	if err := Run(context.Background(), objs, rel, n); err != nil {
		t.Fatal(err)
	}
	if len(n.subjects) != 0 {
		t.Errorf("notified on agreement: %v", n.messages)
	}
}

func TestNothingPublishedYetIsNotAProblem(t *testing.T) {
	n := &fakeNotifier{}
	err := Run(context.Background(), &fakeObjects{objects: map[string][]byte{}}, fakeReleases{latestErr: ErrNoRelease}, n)
	if err != nil || len(n.subjects) != 0 {
		t.Errorf("err %v, notified %v", err, n.messages)
	}
}

func TestProblems(t *testing.T) {
	tampered := []byte("a different image")
	cases := map[string]struct {
		mutate func(*fakeObjects, *fakeReleases)
		want   string
	}{
		"the mirrored image was replaced": {func(o *fakeObjects, _ *fakeReleases) {
			o.objects["images/v0.1.0/scoreboard-v0.1.0.img.xz"] = tampered
		}, "does not hash to the GitHub release's checksum"},
		"latest.json's checksum was rewritten": {func(o *fakeObjects, _ *fakeReleases) {
			o.objects["latest.json"] = manifest("v0.1.0", "scoreboard-v0.1.0.img.xz", sum(tampered))
		}, "differs from the GitHub release's"},
		"the mirror is behind the latest release": {func(_ *fakeObjects, r *fakeReleases) {
			r.latest = "v0.2.0"
		}, "latest GitHub release is v0.2.0"},
		"a release exists but the mirror has no manifest": {func(o *fakeObjects, _ *fakeReleases) {
			delete(o.objects, "latest.json")
		}, "has no latest.json"},
		"latest.json is not JSON": {func(o *fakeObjects, _ *fakeReleases) {
			o.objects["latest.json"] = []byte("<html>")
		}, "not a valid manifest"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			objs, rel := agreeing()
			c.mutate(objs, &rel)
			problems, err := Check(context.Background(), objs, rel)
			if err != nil {
				t.Fatal(err)
			}
			if len(problems) == 0 || !strings.Contains(strings.Join(problems, "\n"), c.want) {
				t.Errorf("problems %q, want one containing %q", problems, c.want)
			}
		})
	}
}

func TestAManifestNamingAnotherObjectIsRefusedWithoutReadingIt(t *testing.T) {
	objs, rel := agreeing()
	objs.objects["latest.json"] = manifest("v0.1.0", "../../site/index.html", sum(image))
	problems, err := Check(context.Background(), objs, rel)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 1 || !strings.Contains(problems[0], "unexpected version, file or checksum") {
		t.Fatalf("problems %q", problems)
	}
	for _, key := range objs.read {
		if key != "latest.json" {
			t.Errorf("read %q after an invalid manifest", key)
		}
	}
}

func TestAnUnreachableGitHubIsAnErrorNotAnAlert(t *testing.T) {
	objs, rel := agreeing()
	rel.latestErr = errors.New("connection reset")
	n := &fakeNotifier{}
	if err := Run(context.Background(), objs, rel, n); err == nil {
		t.Fatal("want an error so the function's error alarm fires")
	}
	if len(n.subjects) != 0 {
		t.Error("an outage was reported as tampering")
	}
}

func TestRunNamesEveryProblemInOneAlert(t *testing.T) {
	objs, rel := agreeing()
	objs.objects["images/v0.1.0/scoreboard-v0.1.0.img.xz"] = []byte("x")
	rel.latest = "v0.2.0"
	n := &fakeNotifier{}
	if err := Run(context.Background(), objs, rel, n); err != nil {
		t.Fatal(err)
	}
	if len(n.subjects) != 1 || n.subjects[0] != alertSubject {
		t.Fatalf("subjects %q", n.subjects)
	}
	for _, want := range []string{"does not hash", "latest GitHub release is v0.2.0"} {
		if !strings.Contains(n.messages[0], want) {
			t.Errorf("message lacks %q:\n%s", want, n.messages[0])
		}
	}
}

func TestGitHubReadsTheLatestTagAndItsChecksum(t *testing.T) {
	good := sum(image)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/repos/DavidJDrake/hockeytrack-scoreboard/releases/latest":
			w.Write([]byte(`{"tag_name":"v0.1.0"}`))
		case "/DavidJDrake/hockeytrack-scoreboard/releases/download/v0.1.0/scoreboard-v0.1.0.img.xz.sha256":
			w.Write([]byte(good + "  scoreboard-v0.1.0.img.xz\n"))
		case "/DavidJDrake/hockeytrack-scoreboard/releases/download/v0.1.0/wrong.img.xz.sha256":
			w.Write([]byte(good + "  scoreboard-v0.1.0.img.xz\n"))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	g := GitHub{Repo: "DavidJDrake/hockeytrack-scoreboard", API: srv.URL, Web: srv.URL, Client: srv.Client()}
	ctx := context.Background()

	if tag, err := g.LatestTag(ctx); err != nil || tag != "v0.1.0" {
		t.Errorf("LatestTag = %q, %v", tag, err)
	}
	if got, err := g.Checksum(ctx, "v0.1.0", "scoreboard-v0.1.0.img.xz"); err != nil || got != good {
		t.Errorf("Checksum = %q, %v", got, err)
	}
	if _, err := g.Checksum(ctx, "v0.1.0", "wrong.img.xz"); err == nil {
		t.Error("a checksum file naming another file was accepted")
	}
	if _, err := g.Checksum(ctx, "v9.9.9", "scoreboard-v9.9.9.img.xz"); err == nil {
		t.Error("a missing checksum file was accepted")
	}
}

func TestGitHubWithNoReleasesSaysSo(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	defer srv.Close()
	g := GitHub{Repo: "DavidJDrake/hockeytrack-scoreboard", API: srv.URL, Web: srv.URL, Client: srv.Client()}
	if _, err := g.LatestTag(context.Background()); !errors.Is(err, ErrNoRelease) {
		t.Errorf("err = %v, want ErrNoRelease", err)
	}
}
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd cloud && go test ./cmd/imagecheck/`
Expected: FAIL to compile (`undefined: Run`, `Check`, `GitHub`, `ErrNotFound`, `ErrNoRelease`, `alertSubject`).

- [ ] **Step 4: Write the monitor**

`cloud/cmd/imagecheck/check.go`:

```go
// Command imagecheck compares, once a day, three accounts of the scoreboard
// image strangers download: the image object in the mirror bucket, the
// checksum in the mirror's latest.json, and the checksum published with the
// GitHub Release, which is the source of truth. Two copies are a liability
// only if nobody notices them disagreeing; any disagreement goes to the
// security topic. See docs/superpowers/specs/2026-09-12-device-image-design.md, 9.6.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"regexp"
	"strings"
)

const alertSubject = "SCOREBOARD SECURITY: image mirror disagrees with its release"

const maxManifest = 64 << 10

var (
	versionRE = regexp.MustCompile(`^v[0-9]+\.[0-9]+\.[0-9]+$`)
	hexRE     = regexp.MustCompile(`^[0-9a-f]{64}$`)

	// ErrNotFound is an absent object in the mirror bucket.
	ErrNotFound = errors.New("object not found")
	// ErrNoRelease means the repository has published no release yet.
	ErrNoRelease = errors.New("no release published")
)

// Manifest is latest.json as the release workflow writes it.
type Manifest struct {
	Version  string `json:"version"`
	File     string `json:"file"`
	SHA256   string `json:"sha256"`
	Size     int64  `json:"size"`
	Released string `json:"released"`
	Release  string `json:"release"`
}

// Objects reads the mirror bucket.
type Objects interface {
	Get(ctx context.Context, key string) (io.ReadCloser, error)
}

// Releases reads the repository's public GitHub releases.
type Releases interface {
	LatestTag(ctx context.Context) (string, error)
	Checksum(ctx context.Context, tag, file string) (string, error)
}

// Notifier raises an alert.
type Notifier interface {
	Notify(ctx context.Context, subject, message string) error
}

// clip keeps attacker-writable text short before it goes into an alert.
func clip(s string) string {
	if len(s) > 80 {
		return s[:80] + "…"
	}
	return s
}

// Check returns every disagreement it finds. An error means the check could
// not be completed, which is not the same as the mirror being wrong.
func Check(ctx context.Context, objs Objects, rel Releases) ([]string, error) {
	latest, err := rel.LatestTag(ctx)
	noRelease := errors.Is(err, ErrNoRelease)
	if err != nil && !noRelease {
		return nil, fmt.Errorf("reading the latest GitHub release: %w", err)
	}

	body, err := objs.Get(ctx, "latest.json")
	if errors.Is(err, ErrNotFound) {
		if noRelease {
			return nil, nil
		}
		return []string{fmt.Sprintf("the mirror has no latest.json, but GitHub has release %s", clip(latest))}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading latest.json: %w", err)
	}
	raw, err := io.ReadAll(io.LimitReader(body, maxManifest+1))
	body.Close()
	if err != nil {
		return nil, fmt.Errorf("reading latest.json: %w", err)
	}

	var m Manifest
	if len(raw) > maxManifest || json.Unmarshal(raw, &m) != nil {
		return []string{"latest.json is not a valid manifest"}, nil
	}
	// The manifest is in a bucket someone may have written to, so nothing in
	// it is used as a key until it has the exact shape a release writes.
	if !versionRE.MatchString(m.Version) || m.File != "scoreboard-"+m.Version+".img.xz" || !hexRE.MatchString(m.SHA256) {
		return []string{fmt.Sprintf("latest.json names an unexpected version, file or checksum (version %q, file %q)", clip(m.Version), clip(m.File))}, nil
	}
	if noRelease {
		return []string{fmt.Sprintf("the mirror serves %s, but GitHub has no release at all", m.Version)}, nil
	}

	var problems []string
	if latest != m.Version {
		problems = append(problems, fmt.Sprintf("the mirror serves %s but the latest GitHub release is %s", m.Version, clip(latest)))
	}
	released, err := rel.Checksum(ctx, m.Version, m.File)
	if err != nil {
		return nil, fmt.Errorf("reading the GitHub release checksum for %s: %w", m.Version, err)
	}
	if released != m.SHA256 {
		problems = append(problems, fmt.Sprintf("latest.json's checksum for %s differs from the GitHub release's", m.Version))
	}

	obj, err := objs.Get(ctx, "images/"+m.Version+"/"+m.File)
	if errors.Is(err, ErrNotFound) {
		return append(problems, fmt.Sprintf("the mirror has no image object for %s", m.Version)), nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading the image object: %w", err)
	}
	h := sha256.New()
	_, err = io.Copy(h, obj)
	obj.Close()
	if err != nil {
		return nil, fmt.Errorf("hashing the image object: %w", err)
	}
	if hex.EncodeToString(h.Sum(nil)) != released {
		problems = append(problems, fmt.Sprintf("the mirrored image for %s does not hash to the GitHub release's checksum", m.Version))
	}
	return problems, nil
}

// Run checks once and raises one alert naming every problem found.
func Run(ctx context.Context, objs Objects, rel Releases, n Notifier) error {
	problems, err := Check(ctx, objs, rel)
	if err != nil {
		return err
	}
	if len(problems) == 0 {
		slog.Info("image mirror agrees with its release")
		return nil
	}
	slog.Warn("image mirror disagrees with its release", "problems", len(problems))
	msg := "The scoreboard image mirror at images.scoreboard.davidjdrake.com disagrees with its GitHub release:\n\n- " +
		strings.Join(problems, "\n- ") +
		"\n\nIf this was not a release in progress, assume the image strangers download may have been replaced. " +
		"See HockeyTrack's docs/threat-model.md, section 7."
	return n.Notify(ctx, alertSubject, msg)
}
```

`cloud/cmd/imagecheck/github.go`:

```go
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// GitHub reads a public repository's releases without credentials. Its
// unauthenticated rate limit is 60 requests an hour; this makes two a day.
type GitHub struct {
	Repo   string // owner/name
	API    string // https://api.github.com
	Web    string // https://github.com
	Client *http.Client
}

func (g GitHub) get(ctx context.Context, url string, limit int64) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "hockeytrack-scoreboard-imagecheck (+https://github.com/DavidJDrake/hockeytrack-scoreboard)")
	resp, err := g.Client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, limit))
	return body, resp.StatusCode, err
}

func (g GitHub) LatestTag(ctx context.Context) (string, error) {
	body, status, err := g.get(ctx, g.API+"/repos/"+g.Repo+"/releases/latest", 1<<20)
	if err != nil {
		return "", err
	}
	if status == http.StatusNotFound {
		return "", ErrNoRelease
	}
	if status != http.StatusOK {
		return "", fmt.Errorf("latest release: status %d", status)
	}
	var out struct {
		Tag string `json:"tag_name"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return "", fmt.Errorf("latest release: %w", err)
	}
	if !versionRE.MatchString(out.Tag) {
		return "", errors.New("latest release has an unexpected tag")
	}
	return out.Tag, nil
}

func (g GitHub) Checksum(ctx context.Context, tag, file string) (string, error) {
	body, status, err := g.get(ctx, g.Web+"/"+g.Repo+"/releases/download/"+tag+"/"+file+".sha256", 4096)
	if err != nil {
		return "", err
	}
	if status != http.StatusOK {
		return "", fmt.Errorf("checksum for %s: status %d", tag, status)
	}
	fields := strings.Fields(string(body))
	if len(fields) != 2 || !hexRE.MatchString(fields[0]) || fields[1] != file {
		return "", fmt.Errorf("checksum for %s is not a sha256sum line for %s", tag, file)
	}
	return fields[0], nil
}
```

`cloud/cmd/imagecheck/main.go`:

```go
package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/aws/aws-sdk-go-v2/service/sns"
)

type bucket struct {
	client *s3.Client
	name   string
}

func (b bucket) Get(ctx context.Context, key string) (io.ReadCloser, error) {
	out, err := b.client.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(b.name), Key: aws.String(key)})
	var missing *types.NoSuchKey
	if errors.As(err, &missing) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return out.Body, nil
}

type topic struct {
	client *sns.Client
	arn    string
}

func (t topic) Notify(ctx context.Context, subject, message string) error {
	_, err := t.client.Publish(ctx, &sns.PublishInput{TopicArn: aws.String(t.arn), Subject: aws.String(subject), Message: aws.String(message)})
	return err
}

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	name, topicARN, repo := os.Getenv("IMAGES_BUCKET"), os.Getenv("TOPIC_ARN"), os.Getenv("GITHUB_REPO")
	if name == "" || topicARN == "" || repo == "" {
		slog.Error("IMAGES_BUCKET, TOPIC_ARN and GITHUB_REPO are required")
		os.Exit(1)
	}
	objs := bucket{client: s3.NewFromConfig(cfg), name: name}
	rel := GitHub{Repo: repo, API: "https://api.github.com", Web: "https://github.com", Client: &http.Client{Timeout: 30 * time.Second}}
	n := topic{client: sns.NewFromConfig(cfg), arn: topicARN}
	lambda.Start(func(ctx context.Context) error { return Run(ctx, objs, rel, n) })
}
```

- [ ] **Step 5: Run the Go tests to verify they pass**

Run: `cd cloud && go vet ./cmd/imagecheck/ && go test ./cmd/imagecheck/`
Expected: PASS.

- [ ] **Step 6: Build it and give it Terraform**

In `Makefile`'s `build` target, add `build/imagecheck` to the `mkdir -p` line, and append these two lines at the end of the target:

```make
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -buildvcs=false -trimpath -ldflags="-s -w" -o ../build/imagecheck/bootstrap ./cmd/imagecheck
	cd build/imagecheck && python3 -m zipfile -c ../imagecheck.zip bootstrap
```

`terraform/imagecheck.tf`:

```hcl
# The daily divergence check between the image mirror and its GitHub Release
# (cmd/imagecheck). A mismatch is published to the security topic by the
# function itself; the alarms below are for the check failing to run at all,
# because a monitor that fails silently reads as "all clear".

data "archive_file" "imagecheck" {
  type             = "zip"
  source_file      = "${path.module}/../build/imagecheck/bootstrap"
  output_path      = "${path.module}/../build/imagecheck.zip"
  output_file_mode = "0755"
}

resource "aws_cloudwatch_log_group" "imagecheck" {
  name              = "/aws/lambda/scoreboard-imagecheck"
  retention_in_days = 30
}

resource "aws_iam_role" "imagecheck" {
  name               = "scoreboard-imagecheck"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

# ListBucket is what makes a missing object a 404 rather than a 403, so "not
# published yet" can be told apart from "access broken".
data "aws_iam_policy_document" "imagecheck" {
  statement {
    actions   = local.logs
    resources = ["${aws_cloudwatch_log_group.imagecheck.arn}:*"]
  }
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.images.arn}/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.images.arn]
  }
  statement {
    actions   = ["sns:Publish"]
    resources = [data.aws_sns_topic.security_alerts.arn]
  }
}

resource "aws_iam_role_policy" "imagecheck" {
  role   = aws_iam_role.imagecheck.id
  policy = data.aws_iam_policy_document.imagecheck.json
}

resource "aws_lambda_function" "imagecheck" {
  function_name    = "scoreboard-imagecheck"
  role             = aws_iam_role.imagecheck.arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = data.archive_file.imagecheck.output_path
  source_code_hash = data.archive_file.imagecheck.output_base64sha256
  timeout          = 300
  memory_size      = 256
  environment {
    variables = {
      IMAGES_BUCKET = aws_s3_bucket.images.bucket
      TOPIC_ARN     = data.aws_sns_topic.security_alerts.arn
      GITHUB_REPO   = "DavidJDrake/hockeytrack-scoreboard"
    }
  }
  depends_on = [aws_cloudwatch_log_group.imagecheck]
}

resource "aws_scheduler_schedule" "imagecheck" {
  name                = "scoreboard-imagecheck"
  group_name          = aws_scheduler_schedule_group.main.name
  schedule_expression = "cron(0 11 * * ? *)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.imagecheck.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

resource "aws_cloudwatch_metric_alarm" "imagecheck_errors" {
  alarm_name          = "scoreboard-imagecheck-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 3600
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions = {
    FunctionName = aws_lambda_function.imagecheck.function_name
  }
  alarm_description = <<-EOT
    The daily check that the image mirror matches its GitHub Release could not
    finish. Until it runs, a replaced image would go unnoticed. Read
    /aws/lambda/scoreboard-imagecheck: a GitHub outage or rate limit clears on
    the next run; an S3 access error means the function's role or the bucket
    policy has changed, and should be compared with the scoreboard repository.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "imagecheck_throttles" {
  alarm_name          = "scoreboard-imagecheck-throttles"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  threshold           = 1
  period              = 3600
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions = {
    FunctionName = aws_lambda_function.imagecheck.function_name
  }
  alarm_description = <<-EOT
    The daily image mirror check was throttled and did not run. The account's
    Lambda concurrency is exhausted, or someone set a reserved concurrency on
    scoreboard-imagecheck; either way the mirror went unchecked.
  EOT
  alarm_actions       = [data.aws_sns_topic.security_alerts.arn]
  treat_missing_data  = "notBreaching"
}
```

In `terraform/scheduler.tf`, change `aws_iam_role_policy.scheduler`'s statement so the scheduler role may invoke both functions:

```hcl
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = [aws_lambda_function.today.arn, aws_lambda_function.imagecheck.arn] }]
```

In `site/tests/signin-config.test.js`, change `alarms >= 14` to `alarms >= 16`.

Append to `site/tests/images-config.test.js`:

```js
let imagecheck = "";
try {
  imagecheck = readFileSync(new URL("../../terraform/imagecheck.tf", import.meta.url), "utf8");
} catch {
  // Reported below.
}

test("the monitor can read the mirror and alert, and nothing else", () => {
  const policy = code(block(imagecheck, 'data "aws_iam_policy_document" "imagecheck" {'));
  const actions = [...policy.matchAll(/"([a-z0-9]+:[A-Za-z*]+)"/g)].map((m) => m[1]).sort();
  assert.deepEqual(actions, ["s3:GetObject", "s3:ListBucket", "sns:Publish"]);
  assert.match(policy, /actions\s*=\s*local\.logs/);
});

test("the monitor's own failures page the security topic", () => {
  for (const name of ["imagecheck_errors", "imagecheck_throttles"]) {
    const alarm = code(block(imagecheck, `resource "aws_cloudwatch_metric_alarm" "${name}" {`));
    assert.match(alarm, /alarm_actions\s*=\s*\[data\.aws_sns_topic\.security_alerts\.arn\]/);
    assert.match(alarm, /FunctionName\s*=\s*aws_lambda_function\.imagecheck\.function_name/);
  }
});

test("the monitor runs daily", () => {
  const schedule = code(block(imagecheck, 'resource "aws_scheduler_schedule" "imagecheck" {'));
  assert.match(schedule, /schedule_expression\s*=\s*"cron\(0 11 \* \* \? \*\)"/);
});
```

- [ ] **Step 7: Run everything**

Run:
```bash
export PATH=$HOME/.local/share/go/bin:$PATH
make test && make build
terraform -chdir=terraform fmt -check && terraform -chdir=terraform validate
```
Expected: everything passes — govulncheck, Go (including the new package), Python, and JS (including the build-config test, which now also sees the imagecheck build line and archive block). `make build` produces `build/imagecheck.zip`, `fmt` is silent and `validate` succeeds.

- [ ] **Step 8: Commit**

```bash
git add cloud/cmd/imagecheck cloud/go.mod cloud/go.sum terraform/imagecheck.tf terraform/scheduler.tf Makefile site/tests/images-config.test.js site/tests/signin-config.test.js
git commit -m "feat: check the image mirror against its release every day

cmd/imagecheck hashes the mirrored image and compares it, latest.json's
checksum and version, and the GitHub Release's published checksum, and
sends one alert naming every disagreement to the security topic. It
validates the manifest before using it as a key, treats a GitHub outage as
an error rather than tampering, and has its own error and throttle alarms.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: The download page

> **Superseded in part (final fix wave, 2026-09-16).** Review rounds changed the committed files after this task's code was written. Where this block and `site/download/index.html`, `site/assets/download.js` and `site/tests/download.test.js` disagree, the committed files are authoritative; this block records the starting point, not the result. Main differences: there are three verify steps: the mirror checksum (described as integrity only), the GitHub Release's own `.sha256` via `gh release download`, and `gh attestation verify <file> --repo DavidJDrake/hockeytrack-scoreboard --signer-workflow DavidJDrake/hockeytrack-scoreboard/.github/workflows/image.yml --source-ref refs/tags/<version>`; the flash steps say to decline Imager's OS customization (spec §9.7).

**Files:**
- Create: `site/download/index.html`
- Create: `site/assets/download.js`
- Create: `site/tests/download.test.js`
- Modify: `terraform/site.tf` (the CSP's `connect-src` line)
- Modify: `site/index.html` (first item of "Set up a new panel")
- Modify: `site/tests/pages.test.js` (append)

**Interfaces:**
- **Consumes:**
  - `local.images_domain` (Task 4);
  - `latest.json` shaped `{"version","file","sha256","size","released","release"}` (Task 3).
- **Produces:** the public page `/download/`, which the home page links to.

- [ ] **Step 1: Write the failing tests**

`site/tests/download.test.js`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  MANIFEST_URL, validManifest, imageUrl, formatSize,
  verifyChecksumCommand, verifyAttestationCommand, loadManifest, render, renderUnavailable,
} from "../assets/download.js";

const sha = "a".repeat(64);
const good = { version: "v0.1.0", file: "scoreboard-v0.1.0.img.xz", sha256: sha, size: 524288000,
  released: "2026-09-16T00:00:00Z", release: "https://github.com/DavidJDrake/hockeytrack-scoreboard/releases/tag/v0.1.0" };

test("a manifest is used only in the exact shape a release writes", () => {
  assert.ok(validManifest(good));
  for (const [why, bad] of Object.entries({
    "no version": { ...good, version: undefined },
    "a version that is not a release tag": { ...good, version: "latest" },
    "a file naming something else": { ...good, file: "../site/index.html" },
    "a file for another version": { ...good, file: "scoreboard-v0.0.9.img.xz" },
    "an uppercase checksum": { ...good, sha256: "A".repeat(64) },
    "a short checksum": { ...good, sha256: "a".repeat(63) },
    "a zero size": { ...good, size: 0 },
    "a string size": { ...good, size: "500" },
    "null": null,
  })) {
    assert.equal(validManifest(bad), false, why);
  }
});

test("the download and verify commands are built from the manifest", () => {
  assert.equal(imageUrl(good), "https://images.scoreboard.davidjdrake.com/images/v0.1.0/scoreboard-v0.1.0.img.xz");
  assert.equal(verifyChecksumCommand(good), `echo "${sha}  scoreboard-v0.1.0.img.xz" | sha256sum -c -`);
  assert.equal(verifyAttestationCommand(good), "gh attestation verify scoreboard-v0.1.0.img.xz --repo DavidJDrake/hockeytrack-scoreboard");
  assert.equal(formatSize(524288000), "500 MB");
});

test("the manifest is fetched without credentials and refused unless it validates", async () => {
  let seen;
  const fetchOk = async (url, init) => { seen = { url, init }; return { ok: true, json: async () => good }; };
  assert.deepEqual(await loadManifest(fetchOk), good);
  assert.equal(seen.url, MANIFEST_URL);
  assert.equal(seen.init.credentials, "omit");
  await assert.rejects(loadManifest(async () => ({ ok: false, status: 404 })));
  await assert.rejects(loadManifest(async () => ({ ok: true, json: async () => ({ ...good, file: "x" }) })));
});

function fakeDoc() {
  const els = {};
  const make = () => {
    const el = { textContent: "", hidden: true, href: "#" };
    Object.defineProperty(el, "innerHTML", { set() { throw new Error("innerHTML was used"); } });
    return el;
  };
  return { els, getElementById: (id) => (els[id] ||= make()) };
}

test("rendering sets text and attributes, never markup", () => {
  const doc = fakeDoc();
  render(doc, good);
  assert.equal(doc.els["image-version"].textContent, "v0.1.0");
  assert.equal(doc.els["image-sha256"].textContent, sha);
  assert.equal(doc.els["image-link"].href, imageUrl(good));
  assert.equal(doc.els["verify-sha"].textContent, verifyChecksumCommand(good));
  assert.equal(doc.els["image-details"].hidden, false);
  assert.equal(doc.els["image-download"].hidden, false);
});

test("an unreachable manifest says so and points at GitHub", () => {
  const doc = fakeDoc();
  renderUnavailable(doc);
  assert.match(doc.els["image-status"].textContent, /could not be loaded/);
});

const page = readFileSync(new URL("../download/index.html", import.meta.url), "utf8");

test("the page loads only its own module, with no inline script or handlers", () => {
  const scripts = [...page.matchAll(/<script\b[^>]*>/g)].map((m) => m[0]);
  assert.deepEqual(scripts, ['<script type="module" src="/assets/download.js">']);
  assert.doesNotMatch(page, /<script[^>]*>\s*\S/);
  assert.doesNotMatch(page, /\son[a-z]+\s*=/i);
  for (const [, url] of page.matchAll(/<(?:link|img|script)[^>]*(?:href|src)="([^"]+)"/g)) {
    assert.ok(url.startsWith("/") || url.startsWith("data:"), `loads from elsewhere: ${url}`);
  }
});

test("the page carries every element the script fills in", () => {
  for (const id of ["image-status", "image-details", "image-version", "image-size", "image-sha256",
    "image-download", "image-link", "verify-sha", "verify-attest"]) {
    assert.match(page, new RegExp(`id="${id}"`), id);
  }
  assert.match(page, /href="https:\/\/github\.com\/DavidJDrake\/hockeytrack-scoreboard\/releases"/);
});

test("the CSP lets the page read the manifest from the image host and nowhere new", () => {
  const tf = readFileSync(new URL("../../terraform/site.tf", import.meta.url), "utf8");
  const connect = (tf.match(/"connect-src ([^;"]*);/) || [])[1] || "";
  assert.match(connect, /https:\/\/\$\{local\.images_domain\}/);
  assert.equal(connect.trim().split(/\s+/).length, 4, `connect-src: ${connect}`);
});
```

Append to `site/tests/pages.test.js`:

```js
test("setting up a panel starts by downloading the image", () => {
  assert.match(index, /<li><a href="\/download\/">Download the scoreboard image<\/a>/);
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd site && node --test tests/download.test.js tests/pages.test.js`
Expected: FAIL (`download.js` and `download/index.html` missing; the home page has no link).

- [ ] **Step 3: Write the page and its module**

`site/assets/download.js`:

```js
// The download page: shows the current scoreboard image from the mirror's
// latest.json, and the commands that verify it before it is flashed. Nothing
// here builds markup from strings; every value is set as text or an
// attribute, and the manifest is used only in the exact shape a release
// writes, because the mirror is a copy that could be tampered with.
export const MANIFEST_URL = "https://images.scoreboard.davidjdrake.com/latest.json";
const IMAGES_BASE = "https://images.scoreboard.davidjdrake.com/images/";
const REPO = "DavidJDrake/hockeytrack-scoreboard";
const VERSION = /^v[0-9]+\.[0-9]+\.[0-9]+$/;
const HEX64 = /^[0-9a-f]{64}$/;

export function validManifest(m) {
  return m !== null && typeof m === "object"
    && typeof m.version === "string" && VERSION.test(m.version)
    && m.file === `scoreboard-${m.version}.img.xz`
    && typeof m.sha256 === "string" && HEX64.test(m.sha256)
    && Number.isSafeInteger(m.size) && m.size > 0;
}

export function imageUrl(m) {
  return `${IMAGES_BASE}${m.version}/${m.file}`;
}

export function formatSize(bytes) {
  return `${Math.round(bytes / (1024 * 1024))} MB`;
}

export function verifyChecksumCommand(m) {
  return `echo "${m.sha256}  ${m.file}" | sha256sum -c -`;
}

export function verifyAttestationCommand(m) {
  return `gh attestation verify ${m.file} --repo ${REPO}`;
}

export async function loadManifest(fetchImpl = fetch) {
  const resp = await fetchImpl(MANIFEST_URL, { cache: "no-store", credentials: "omit" });
  if (!resp.ok) throw new Error(`manifest: status ${resp.status}`);
  const m = await resp.json();
  if (!validManifest(m)) throw new Error("manifest: unexpected shape");
  return m;
}

export function render(doc, m) {
  doc.getElementById("image-version").textContent = m.version;
  doc.getElementById("image-size").textContent = formatSize(m.size);
  doc.getElementById("image-sha256").textContent = m.sha256;
  const link = doc.getElementById("image-link");
  link.href = imageUrl(m);
  link.textContent = `Download ${m.file}`;
  doc.getElementById("verify-sha").textContent = verifyChecksumCommand(m);
  doc.getElementById("verify-attest").textContent = verifyAttestationCommand(m);
  doc.getElementById("image-details").hidden = false;
  doc.getElementById("image-download").hidden = false;
  doc.getElementById("image-status").textContent = `The current image is ${m.version}.`;
}

export function renderUnavailable(doc) {
  doc.getElementById("image-status").textContent =
    "The current image could not be loaded. Every release is also listed on GitHub, linked below.";
}

if (typeof document !== "undefined") {
  loadManifest().then((m) => render(document, m), () => renderUnavailable(document));
}
```

`site/download/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Download the HockeyTrack scoreboard SD card image and verify it before you flash it.">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%8F%92%3C/text%3E%3C/svg%3E">
<link rel="stylesheet" href="/assets/site.css">
<link rel="stylesheet" href="/assets/admin.css">
<title>Download · HockeyTrack Scoreboards</title>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<nav class="nav" aria-label="Site">
  <div class="wrap">
    <a class="wordmark" href="/">HOCKEYTRACK</a>
  </div>
</nav>

<main class="wrap" id="main">
  <h1>Download the scoreboard image</h1>
  <p class="lede">A Raspberry Pi OS image with the scoreboard installed. It carries no passwords, keys or Wi-Fi details: each panel makes its own identity the first time it starts, and an invited account claims it.</p>

  <section class="card" aria-labelledby="current-heading">
    <h2 id="current-heading">Current image</h2>
    <p id="image-status" role="status">Loading the current release…</p>
    <dl id="image-details" hidden>
      <dt>Version</dt><dd id="image-version"></dd>
      <dt>Size</dt><dd id="image-size"></dd>
      <dt>SHA-256</dt><dd><code id="image-sha256"></code></dd>
    </dl>
    <p id="image-download" hidden><a id="image-link" href="#">Download</a></p>
    <p>Every release is also on <a href="https://github.com/DavidJDrake/hockeytrack-scoreboard/releases">GitHub</a>, which is the source of truth for the image and its checksum. This page reads a copy.</p>
  </section>

  <section class="card" aria-labelledby="verify-heading">
    <h2 id="verify-heading">Verify it before you flash it</h2>
    <ol>
      <li>Check the file you downloaded matches the published checksum:
        <pre><code id="verify-sha"></code></pre></li>
      <li>Check it was built from this project's source by its release workflow (needs the <a href="https://cli.github.com/">GitHub CLI</a>):
        <pre><code id="verify-attest"></code></pre></li>
    </ol>
    <p>If either check fails, do not flash the image.</p>
  </section>

  <section class="card" aria-labelledby="flash-heading">
    <h2 id="flash-heading">Flash it</h2>
    <ol>
      <li>Open Raspberry Pi Imager, choose your board, then <strong>Use custom</strong> and pick the downloaded file.</li>
      <li>Write it to the SD card, then follow <a href="/#add">Set up a new panel</a>.</li>
    </ol>
  </section>
</main>
<script type="module" src="/assets/download.js"></script>
</body>
</html>
```

In `site/index.html`, replace `<li>Prepare an SD card with the scoreboard installed.</li>` with:

```html
        <li><a href="/download/">Download the scoreboard image</a>, verify it, and write it to an SD card with Raspberry Pi Imager.</li>
```

In `terraform/site.tf`, change "connect-src names exactly two hosts: the API, and the Cognito hosted-UI domain the PKCE token exchange posts to." in the comment above the headers policy to "connect-src names exactly three hosts: the API, the Cognito hosted-UI domain the PKCE token exchange posts to, and the image mirror the download page reads latest.json from." Then extend the CSP's `connect-src` line to:

```hcl
        "connect-src 'self' ${aws_apigatewayv2_api.admin.api_endpoint} https://${aws_cognito_user_pool_domain.admin.domain} https://${local.images_domain}; ",
```

- [ ] **Step 4: Run the site tests and the Terraform checks**

Run:
```bash
make test-js
terraform -chdir=terraform fmt -check && terraform -chdir=terraform validate
```
Expected: all JS tests pass, including the existing CSP and pages tests, `node --check` on `download.js`, and "no script on this site turns a string into markup". `fmt` is silent and `validate` succeeds.

- [ ] **Step 5: Commit**

```bash
git add site/download/index.html site/assets/download.js site/tests/download.test.js site/tests/pages.test.js site/index.html terraform/site.tf
git commit -m "feat: a download page that says how to verify the image

/download/ reads the mirror's latest.json, uses it only in the exact shape
a release writes, and shows the checksum and gh attestation commands as
text. The CSP gains exactly the image host, and the home page's first setup
step links here instead of assuming a card already exists.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Deploy the mirror and configure the release gate (controller and user)

No code. The agent running the plan does not run `terraform apply`, `make site`, or any mutating AWS or GitHub call on its own. Each apply is a saved plan the user runs. The `gh api` writes run only after the user says go.

- [ ] **Step 1: Push the branch and open the PR**

After the user's go-ahead: `git push -u origin device-image-b2`, then `gh pr create`. CI must pass: Go, Python, JavaScript and Terraform.

- [ ] **Step 2: Build and plan**

```bash
make build
terraform -chdir=terraform plan -out=/tmp/claude-1000/b2-images.tfplan
```

Expected plan: only additions, plus in-place updates to the site response-headers policy (CSP) and the scheduler role's policy:
- bucket and its settings;
- certificate and validation records;
- two CloudFront policies and the distribution;
- A/AAAA aliases;
- publisher role and policy;
- `scoreboard-imagecheck` function, role, schedule and two alarms.

It must show nothing destroyed and no change to any existing function's code. Stop and report on anything else.

Before handing the plan over, tell the user: `scoreboard-imagecheck-not-running` will page the security topic shortly after the apply. It reads a function with no invocations as breaching, and the first scheduled run is not until 11:00 or 23:00 UTC. Step 3b clears it by invoking the monitor once, so the page is expected and not an incident.

- [ ] **Step 3: The user applies**

The user runs `terraform -chdir=terraform apply /tmp/claude-1000/b2-images.tfplan`. CloudFront typically takes 5–10 minutes. Then:

```bash
terraform -chdir=terraform output -raw images_bucket
terraform -chdir=terraform output -raw images_distribution_id
terraform -chdir=terraform output -raw image_publisher_role_arn
curl -sI https://images.scoreboard.davidjdrake.com/latest.json | head -1   # 403 or 404: no object yet, and the bucket is not listable
curl -sI http://images.scoreboard.davidjdrake.com/ | grep -i '^location'    # redirects to https
aws s3api get-public-access-block --bucket "$(terraform -chdir=terraform output -raw images_bucket)" --region us-east-1
```

- [ ] **Step 3b: Invoke the monitor once (after the user's go-ahead)**

The invoke runs the monitor's normal read-only check, and it is still a Lambda invoke, so the user says go first:

```bash
aws lambda invoke --function-name scoreboard-imagecheck --region us-east-1 /tmp/claude-1000/imagecheck-first.json
cat /tmp/claude-1000/imagecheck-first.json
aws logs tail /aws/lambda/scoreboard-imagecheck --since 5m --region us-east-1
```

Expected:
- the invoke's response has no `FunctionError`;
- the log has `image mirror agrees with its release` and no ERROR line.

With no GitHub release and no `latest.json` (a 404 from GitHub's `releases/latest` and a NoSuchKey from S3), `Check` returns no problems and no error. So that line is how this state appears; the code has no separate "no release yet" message. An AccessDenied on `latest.json` or a GitHub error would instead come back as a `FunctionError` with an ERROR log line: stop and report. Then confirm the not-running alarm returns to OK within a few minutes:

```bash
aws cloudwatch describe-alarms --alarm-names scoreboard-imagecheck-not-running --region us-east-1 --query 'MetricAlarms[0].StateValue'
```

- [ ] **Step 4: Publish the site with the download page**

The user runs `make site` (it runs `test-js` first). Check:
- `https://scoreboard.davidjdrake.com/download/` loads and says the image could not be loaded, with the GitHub link;
- the console shows no CSP violation, only a 403/404 on `latest.json`.

- [ ] **Step 5: Set the repository variables (after the user's go-ahead)**

```bash
gh variable set IMAGES_BUCKET --repo DavidJDrake/hockeytrack-scoreboard --body "$(terraform -chdir=terraform output -raw images_bucket)"
gh variable set IMAGES_DISTRIBUTION_ID --repo DavidJDrake/hockeytrack-scoreboard --body "$(terraform -chdir=terraform output -raw images_distribution_id)"
gh variable set IMAGE_PUBLISHER_ROLE_ARN --repo DavidJDrake/hockeytrack-scoreboard --body "$(terraform -chdir=terraform output -raw image_publisher_role_arn)"
```

These are variables, not secrets. None of them grants anything without the OIDC trust, and the account ID they contain already appears in ARNs this public repository documents.

- [ ] **Step 6: Create the `image-release` environment (after the user's go-ahead)**

```bash
OWNER_ID=$(gh api users/DavidJDrake --jq .id)
gh api -X PUT repos/DavidJDrake/hockeytrack-scoreboard/environments/image-release \
  --input - <<JSON
{"wait_timer":0,"prevent_self_review":false,
 "reviewers":[{"type":"User","id":${OWNER_ID}}],
 "deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
JSON
gh api -X POST repos/DavidJDrake/hockeytrack-scoreboard/environments/image-release/deployment-branch-policies \
  -f name='v*' -f type=tag
gh api repos/DavidJDrake/hockeytrack-scoreboard/environments/image-release \
  --jq '{reviewers: [.protection_rules[] | select(.type=="required_reviewers") | .reviewers[].reviewer.login], policy: .deployment_branch_policy}'
gh api repos/DavidJDrake/hockeytrack-scoreboard/environments/image-release/deployment-branch-policies --jq '.branch_policies[] | {name, type}'
```

Expected: reviewers `["DavidJDrake"]`, custom policies on, and exactly one policy, `{"name":"v*","type":"tag"}`.

`prevent_self_review` stays false because the sole maintainer is also the tagger. The approval is a deliberate second action, not a second person, and spec §9.9 says so.

- [ ] **Step 7: Restrict who can create release tags (after the user's go-ahead)**

```bash
gh api -X POST repos/DavidJDrake/hockeytrack-scoreboard/rulesets --input - <<'JSON'
{"name":"release tags","target":"tag","enforcement":"active",
 "conditions":{"ref_name":{"include":["refs/tags/v*"],"exclude":[]}},
 "rules":[{"type":"creation"},{"type":"update"},{"type":"deletion"},{"type":"non_fast_forward"}],
 "bypass_actors":[{"actor_id":5,"actor_type":"RepositoryRole","bypass_mode":"always"}]}
JSON
gh api repos/DavidJDrake/hockeytrack-scoreboard/rulesets --jq '.[] | select(.name=="release tags") | {id, enforcement}'
```

Role 5 is the repository admin role. Anyone else, including a compromised token with only `contents:write`, cannot create, move or delete a `v*` tag, so cannot start a publish. Update and deletion are blocked for admins too, except by bypass, so a published tag cannot be silently re-pointed.

- [ ] **Step 7b: Enable immutable releases (after the user's go-ahead)**

The endpoint was checked against GitHub's REST reference ("Enable immutable releases", `PUT /repos/{owner}/{repo}/immutable-releases`, 204 on success; `GET` on the same path returns 200 with `enabled` and `enforced_by_owner` when enabled, 404 when not):

```bash
gh api -X PUT repos/DavidJDrake/hockeytrack-scoreboard/immutable-releases
gh api repos/DavidJDrake/hockeytrack-scoreboard/immutable-releases
```

Expected: the `PUT` prints nothing, and the `GET` prints `{"enabled":true,"enforced_by_owner":false}`. Any 404 on the `GET` means it is not enabled: stop and report. Once a Release is published, its assets and tag are locked (spec §9.9). `gh release create` uploads assets to a draft before publishing, which immutability permits. A failed publish that leaves a published but incomplete Release can no longer be fixed by editing it, only by deleting it and re-running.

- [ ] **Step 8: Record it**

Append to spec §9 a "Deployed" note, with:
- the apply date;
- the distribution's domain, but not its ID (the ID lives in Terraform outputs and repository variables);
- that the environment, tag ruleset and immutable releases exist, and how they were checked.

Commit on the branch.

---

### Task 8: HockeyTrack section 15, supply-chain detection (subagent, HockeyTrack repo)

**Repo:** `/home/jay/projects/hockeytrack`, new branch `image-supply-chain-detection` from `main`.

**Files:**
- Modify: `terraform/cloudtrail.tf` (a fourth data-event selector)
- Modify: `terraform/security-alarms.tf` (section 15)
- Modify: `terraform/variables.tf` (`scoreboard_images_distribution_id`)
- Modify: whichever file holds the detection registry, and the tests that read it. Find it with `grep -rn "hockeytrack-sec-scoreboard-" terraform tests`.
- Modify: `docs/threat-model.md` (§4 assets, §7 detections)

**Interfaces:**
- **Consumes:** the bucket `scoreboard-images-<account>`, the distribution ID (from Task 7 Step 3, given to the subagent as a value; not a secret), the role `scoreboard-image-publisher`, and the GitHub OIDC provider.
- **Produces:** the rule `hockeytrack-sec-scoreboard-image`, routed like sections 12–14.

**Brief constraints for the subagent:** the standing rules apply. No apply, no push, no mutating AWS call, no `terraform.tfvars` read. Every AWS CLI command passes `--region us-east-1`. It must read `~/.claude/projects/-home-jay-projects-hockeytrack/memory/cloudtrail-alarm-traps.md` before writing a pattern.

- [ ] **Step 1: Measure first**

Read-only, with a 90-day window:

```bash
aws cloudtrail lookup-events --region us-east-1 --lookup-attributes AttributeKey=ResourceName,AttributeValue=scoreboard-images-<account> --max-results 50
aws cloudtrail lookup-events --region us-east-1 --lookup-attributes AttributeKey=EventName,AttributeValue=CreateInvalidation --max-results 50
aws cloudtrail lookup-events --region us-east-1 --lookup-attributes AttributeKey=EventSource,AttributeValue=cloudfront.amazonaws.com --max-results 50 \
  --query 'Events[].CloudTrailEvent' --output text | jq -r '.eventName + " " + ((.requestParameters // {}) | keys | join(","))'
```

Record, in the PR description:
- how many management writes name the bucket or distribution (expected: the Terraform apply only);
- how many `CreateInvalidation` calls other distributions get, since hockeytrack's own and the scoreboard site's deploys invalidate;
- the real request-parameter casing for `CreateInvalidation` and `UpdateDistribution` (`id`, `Id`, `resource`?).

A casing no real record shows is a guess (see the memory file). If the new distribution has no record yet, use the site distribution's records for casing.

- [ ] **Step 2: Write the failing pattern tests**

Follow the section 13/14 test style: patterns rendered from `terraform plan -out` JSON and exercised with `aws events test-event-pattern`. Do not hand-write the pattern into the test.

**Must match (page):**
1. S3 data event `PutObject` on `images/v0.1.0/x.img.xz`, with `sessionIssuer.arn` a different role (for example the funandgames user's session, or `AWSReservedSSO_...`).
2. The same event from an IAM user: `userIdentity.type` `IAMUser`, no `sessionContext.sessionIssuer`. This is the leaf exists:false branch.
3. `DeleteObject` on `latest.json` from root.
4. Management `PutBucketPolicy` naming the bucket via `requestParameters.bucketName`.
5. `DeleteBucketPublicAccessBlock` on the bucket.
6. `UpdateDistribution` naming the distribution ID, in the casing Step 1 found.
7. `UpdateDistribution` naming it only by ARN in `resource`/`Resource`.
8. `AssociateAlias` / `DeleteDistribution` on it.
9. `PutBucketPolicy` naming the bucket only by ARN in `resources` (a management event, `eventCategory` Management).
10. `UpdateAssumeRolePolicy` / `PutRolePolicy` / `AttachRolePolicy` on `roleName` `scoreboard-image-publisher`.
11. `UpdateOpenIDConnectProviderThumbprint`, `AddClientIDToOpenIDConnectProvider` or `DeleteOpenIDConnectProvider` on the GitHub provider ARN.

**Must not match:**
- A. `PutObject` on `images/v0.1.0/x.img.xz` whose `sessionIssuer.arn` is the publisher role.
- B. `CreateInvalidation` on the distribution from the publisher role, and from anyone else. Spec §9.8 leaves invalidations out: they name `distributionId`, and an invalidation only makes CloudFront re-read an origin whose every write already pages.
- C. `GetObject` on the bucket by anyone. Reads are not selected, but check the pattern anyway.
- D. `PutObject` on the scoreboard site bucket.
- E. `UpdateDistribution` on the site distribution.
- F. `PutRolePolicy` on `scoreboard-imagecheck`, the monitor's role.
- G. A read-only management event naming the bucket (`GetBucketPolicy`, `readOnly: true`).

Case A against match 9 is why the ARN branch carries `eventCategory` Management: an S3 data event's `resources` also lists the bucket ARN, so without it every release would page. Repeat each constraint inside its branch, never beside the `$or` (key-order trap).

- [ ] **Step 3: Add the selector**

In `terraform/cloudtrail.tf`:
- add `data "aws_s3_bucket" "scoreboard_images" { bucket = "scoreboard-images-${data.aws_caller_identity.current.account_id}" }`, reusing the existing caller-identity data source name;
- add an advanced event selector: `eventCategory` Data, `resources.type` `AWS::S3::Object`, `resources.ARN` StartsWith `${data.aws_s3_bucket.scoreboard_images.arn}/`, `readOnly` equals `false`.

Write-only keeps the cost to the handful of objects a release writes.

- [ ] **Step 4: Write section 15**

In `terraform/security-alarms.tf`, as flattened `$or` branches in a `local` rendered with `jsonencode`. Every branch repeats its own `eventSource`/`eventName` constraints:
- data, issuer differs: `eventCategory` Data, `eventSource` s3, `resources.ARN` prefix bucket ARN, `userIdentity.sessionContext.sessionIssuer.arn` anything-but the publisher role ARN;
- data, no issuer: the same with `sessionIssuer.arn` exists:false;
- S3 management by name: `readOnly` false, `requestParameters.bucketName` the bucket;
- S3 management by ARN: `eventCategory` Management, `readOnly` false, `resources.ARN` the bucket ARN;
- CloudFront management: `eventSource` cloudfront, `readOnly` false, and either `requestParameters.id` equal to the distribution ID (one branch per casing Step 1 confirmed) or `resource` prefixed by the distribution ARN (both casings, as section 13). If Step 1 shows `CreateInvalidation` also uses `id`, add `eventName` anything-but `CreateInvalidation` to those branches, per spec §9.8;
- IAM role: `eventSource` iam, `readOnly` false, `requestParameters.roleName` `scoreboard-image-publisher`;
- IAM OIDC: `eventSource` iam, `readOnly` false, `requestParameters.openIDConnectProviderArn` the provider ARN.

The distribution ID comes from `var.scoreboard_images_distribution_id` (no default; add it to the variables file with a description). A precondition checks it matches `^E[A-Z0-9]+$`. A second precondition checks the distribution's aliases include `images.scoreboard.davidjdrake.com`, via `data "aws_cloudfront_distribution"`, so a typo'd ID that names another distribution fails at plan. Add `length(local.scoreboard_image_pattern) <= 2048` as for sections 13 and 14.

The rule, target, DLQ and input transformer copy section 14's. Add the rule to the detection registry so the existing registry tests (metric alarm, routing) cover it.

The variable's value is not secret. The subagent must not write it into `terraform.tfvars` (the user adds it there) and must not read that file. Tests and plans pass it with `-var`.

- [ ] **Step 5: Run the tests**

```bash
terraform -chdir=terraform fmt -check
terraform -chdir=terraform validate
terraform -chdir=terraform plan -var scoreboard_images_distribution_id=<ID> -out=/tmp/claude-1000/sec15.tfplan
```

Then run the repo's pattern test runner (the same command sections 13/14 used). Expected:
- the plan shows the trail updated in place (one selector added), the new rule, target and alarm, and registry updates;
- nothing destroyed; all 11 matches and 7 non-match cases pass;
- the rendered pattern is at most 2048 characters;
- one `test-event-pattern` call on the final pattern succeeds, which catches "too complex".

- [ ] **Step 6: Threat model**

`docs/threat-model.md`:
- **§4:** add the image mirror and the release as assets. What an attacker gains is code execution on every panel flashed afterwards.
- **§7:** add section 15, with its honest gaps:
  - the publisher role is excluded by design, so a malicious release through the real workflow is stopped by the environment approval and exposed by attestation, not by this rule;
  - invalidations by anyone do not page (spec §9.8);
  - the daily monitor finds mirror/GitHub disagreement up to 24 hours late;
  - GitHub release asset tampering by a GitHub admin is out of AWS's view.

- [ ] **Step 7: Commit**

One commit: `feat: page on any change to the scoreboard image supply chain outside the release workflow`. The body names the measurement results and the gaps. Do not push; report to the controller.

---

### Task 9: Deploy section 15 and prove it pages (controller and user)

- [ ] **Step 1: Review**

Run the spec and code-quality review of Task 8 per the SDD process. Fix rounds go back to the subagent.

- [ ] **Step 2: Push, PR, plan**

After the user's go-ahead, push and open the PR, and wait for CI. Then the user adds `scoreboard_images_distribution_id = "<ID>"` to their `terraform.tfvars`, and the controller runs `terraform -chdir=terraform plan -out=/tmp/claude-1000/sec15.tfplan` in `/home/jay/projects/hockeytrack`. The plan must match Task 8 Step 5.

- [ ] **Step 3: The user applies**

`terraform -chdir=/home/jay/projects/hockeytrack/terraform apply /tmp/claude-1000/sec15.tfplan`

- [ ] **Step 4: Break it**

After the user's go-ahead, and as the funandgames profile, which is not the publisher role:

```bash
BUCKET=scoreboard-images-<account>
echo break-test > /tmp/claude-1000/break.txt
aws s3 cp /tmp/claude-1000/break.txt "s3://$BUCKET/images/break-test.txt" --region us-east-1
aws s3 rm "s3://$BUCKET/images/break-test.txt" --region us-east-1
```

Expected:
- within about 5 minutes, one alert email for `hockeytrack-sec-scoreboard-image` naming `PutObject` and a second naming `DeleteObject`;
- the rule's `MatchedEvents` metric is 2, the target `FailedInvocations` is 0, and the DLQ is empty.

The delete marker and the old version stay in the versioned bucket. That is expected.

- [ ] **Step 5: Merge and record**

After the user's go-ahead, merge the PR. Record the break test (date, both alerts received, metrics) in the HockeyTrack threat model §7 entry, as sections 13/14 were.

---

### Task 10: The first release, end to end (controller and user)

- [ ] **Step 0: Build and gate on main before tagging (after the user's go-ahead)**

The workflow cannot be dispatched from a branch until it exists on main, so the first real pi-gen build happens after the merge. Run it without publishing before any tag exists:

```bash
gh workflow run image.yml --repo DavidJDrake/hockeytrack-scoreboard --ref main
gh run watch --repo DavidJDrake/hockeytrack-scoreboard $(gh run list --repo DavidJDrake/hockeytrack-scoreboard --workflow image.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh run view --repo DavidJDrake/hockeytrack-scoreboard --log $(gh run list --repo DavidJDrake/hockeytrack-scoreboard --workflow image.yml --limit 1 --json databaseId --jq '.[0].databaseId') | grep 'image-gate:'
```

A dispatch sets `publish=false`, so only the build job runs: no attestation and no publish. Read every gate line against the real trixie rootfs, not just the final result. These rules have only ever met fixtures:
- **the account rule:** every `/etc/passwd` entry has an `x` and a locked shadow hash, including the first user and root;
- **cloud-init:** no seed files, no dpkg entry, no `/etc/cloud/cloud.cfg`;
- **NetworkManager under `/usr/lib`:** no system-provided connection files;
- **pip trees:** the venv holds only `pip-*.dist-info`, `/usr/lib/python3/dist-packages/paho` exists, and there is no `/root/.cache/pip`;
- **runtime:** how long the gate step took, since `grep -a` reads all of `/var`.

A failure here is fixed on a branch and merged before Step 1. It costs one extra CI run of about two hours if nothing fails.

- [ ] **Step 1: Merge B2 and tag**

After Tasks 7 and 9, and the user's go-ahead, merge the scoreboard PR, then tag from main:

```bash
git -C /home/jay/projects/hockeytrack-scoreboard checkout main && git -C /home/jay/projects/hockeytrack-scoreboard pull
git -C /home/jay/projects/hockeytrack-scoreboard tag -a v0.1.0 -m "v0.1.0: first scoreboard image"
git -C /home/jay/projects/hockeytrack-scoreboard push origin v0.1.0
```

- [ ] **Step 2: Watch the build**

`gh run watch --repo DavidJDrake/hockeytrack-scoreboard $(gh run list --workflow image.yml --limit 1 --json databaseId --jq '.[0].databaseId')`

Expected:
- the build job passes the gate and uploads the artifact;
- the attest job verifies the sha256 and attests;
- the publish job waits on `image-release`.

A gate failure is a finding, not a flake: read the gate's output, fix on a branch, and delete and re-create the tag only after that fix merges. The ruleset means the user does that as admin.

- [ ] **Step 3: The user approves**

The user approves the `image-release` deployment in the Actions UI, after checking that the run's commit is the tagged commit on main.

- [ ] **Step 4: Verify what was published**

```bash
cd /tmp/claude-1000 && rm -rf v010 && mkdir v010 && cd v010
gh release download v0.1.0 --repo DavidJDrake/hockeytrack-scoreboard
sha256sum -c scoreboard-v0.1.0.img.xz.sha256
gh attestation verify scoreboard-v0.1.0.img.xz --repo DavidJDrake/hockeytrack-scoreboard \
  --signer-workflow DavidJDrake/hockeytrack-scoreboard/.github/workflows/image.yml --source-ref refs/tags/v0.1.0
curl -s https://images.scoreboard.davidjdrake.com/latest.json | jq .
curl -s https://images.scoreboard.davidjdrake.com/images/v0.1.0/scoreboard-v0.1.0.img.xz | sha256sum
```

Expected:
- the checksum is OK;
- the attestation is verified, naming `.github/workflows/image.yml` at `refs/tags/v0.1.0`;
- `latest.json` is valid, with the release's sha256;
- the mirror's hash equals the release's.

- [ ] **Step 5: Download page and monitor**

`https://scoreboard.davidjdrake.com/download/` shows v0.1.0, the size, the checksum, and both commands. The user checks it on a phone too.

Invoke the monitor once, a read-only Lambda invoke:

```bash
aws lambda invoke --function-name scoreboard-imagecheck --region us-east-1 /tmp/claude-1000/imagecheck.json && cat /tmp/claude-1000/imagecheck.json
aws logs tail /aws/lambda/scoreboard-imagecheck --since 5m --region us-east-1
```

Expected: the log line reports agreement for v0.1.0, and there is no SNS alert.

- [ ] **Step 6: Tamper test (after the user's go-ahead)**

As funandgames:
1. Note the current version ID of `images/v0.1.0/scoreboard-v0.1.0.img.xz`.
2. Overwrite it with a 1-byte file.
3. Invoke the monitor.

Expected:
- a monitor alert email saying the mirror and GitHub disagree for v0.1.0;
- a section 15 page for the `PutObject`.

Then restore:

```bash
aws s3api delete-object --bucket "$BUCKET" --key images/v0.1.0/scoreboard-v0.1.0.img.xz --version-id <tampered version id> --region us-east-1
curl -s https://images.scoreboard.davidjdrake.com/images/v0.1.0/scoreboard-v0.1.0.img.xz | sha256sum   # may be cached; the image path is immutable-cached, so invalidate that one path as funandgames if it differs
aws lambda invoke --function-name scoreboard-imagecheck --region us-east-1 /tmp/claude-1000/imagecheck2.json
```

Expected: the monitor agrees again. The delete also pages section 15, which is expected, and noted.

Note the finding either way: CloudFront served the tampered bytes for as long as the cache held the good ones or the bad ones. This is why the download page tells users to verify, and why the source of truth is the GitHub release.

- [ ] **Step 7: Record**

Add a "Verified 2026-MM-DD" subsection to spec §9 with:
- release URL;
- attestation result;
- monitor agree, mismatch and agree results;
- section 15 alerts;
- the cache observation.

Commit on a branch, and open a PR after the user's go-ahead.

---

### Task 11: The bench session on the flashed image (user, with the controller)

Only the user can do this: it needs the Pi, a panel, an SD card and the user's Wi-Fi.

- [ ] **Step 1: Flash and first boot**

The user downloads from the page, runs all three verify commands, and flashes with Raspberry Pi Imager's **Use custom**, with no OS customization. The Imager's settings would write credentials the gate forbids, and the image has no cloud-init to read them. The user then follows the home page's setup steps, with a real setup file.

During H4 and H5, watch for pi-gen's first-boot user-rename wizard. `DISABLE_FIRST_BOOT_USER_RENAME` is deliberately unset, so `userconf-pi` runs its rename prompt on tty1 at first boot. It may compete with `scoreboard.service`, which also takes tty1 to become kmsdrm's DRM master. If the panel stays black or shows the wizard, record exactly what appears and which of the two holds the console: `journalctl -b -u scoreboard`, plus the rename service's own journal. Find its unit with `systemctl list-units --all 'userconf*'`; the unit name was not checked against `userconf-pi`'s package. That is a B1 finding, not something to work around by hand.

- [ ] **Step 2: Run the checks in order**

From `docs/hardware-checks.md`, in the order spec §9.10 gives: H4, H5, then H1, H2, H7, H3, H8, and H6 if a Zero 2 W is on hand.

The controller reads each check's steps aloud from the file and records the observed result as the user reports it. The controller never infers a pass the user didn't observe.

- [ ] **Step 3: Record**

For each check, update `docs/hardware-checks.md` with:
- date;
- image version;
- board;
- panel;
- result;
- any deviation.

A failure gets a Jira/issue entry and a fix on a branch, with a new patch tag (`v0.1.1`) through Tasks 10's steps. The image that failed stays published, marked as superseded in its release notes, never deleted, so the record stays honest.

- [ ] **Step 4: Close B2**

When H1–H5, H7 and H8 pass (H6 as available), update:
- spec §9, status "B2 complete";
- the top-level README's device section, to link the download page.

Commit, PR, and merge after the user's go-ahead.
