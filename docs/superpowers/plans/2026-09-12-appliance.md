# Device Appliance (B1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the scoreboard from a git checkout in someone's home directory into an appliance that can be baked into a publishable image — one that renders a useful screen before it has an identity, and whose Wi-Fi can be changed without a second computer.

**Architecture:** Four new focused modules in the existing `device/scoreboard/` package — `netcfg` (boot-file parsing and NetworkManager), `screens` (the non-scoreboard panels), `settings` (the keyboard screen's state machine), `reset` (factory reset) — plus a second systemd unit template for the appliance layout and an appliance mode in the one install script. `main.py` keeps its loop and gains a screen selector; no rendering path is duplicated.

**Tech Stack:** Python 3.11+, pygame (distribution `python3-pygame`, for its kmsdrm SDL), NetworkManager via `nmcli`, systemd, polkit, pytest.

**Spec:** `docs/superpowers/specs/2026-09-12-device-image-design.md`

## Global Constraints

Every task's requirements implicitly include this section.

- **Raspberry Pi OS Trixie or later only.** Bookworm's `python3-pygame` is 2.1.2 and fails `device/requirements.txt`; pip would substitute a PyPI wheel with no `kmsdrm` driver and the panel would stay black. `tools/pi-setup.sh` already refuses `buster|bullseye|bookworm` — keep that refusal in both modes.
- **pygame must resolve to `/usr/lib/python3/dist-packages`.** The existing provenance check in `pi-setup.sh` stays in both modes.
- **No credential is ever written into a shipped artifact.** No key, certificate, PSK, or `device/config/` content in any committed file. Both repos are public.
- **The settings screen never displays a stored pre-shared key.** Entering a new one is supported; reading an existing one back is not.
- **Values reach `nmcli` as list arguments** from Python's `subprocess`, never interpolated into a shell string, and never with `shell=True`.
- **The boot-partition file is consumed after it is applied** — overwritten with a comment recording when, so a cleartext PSK does not persist on a partition every OS mounts.
- **Service supplementary groups are `video`, `render`, `input`, `gpio`.** `input` is required: under kmsdrm there is no X server and SDL reads `/dev/input/event*` directly.
- **Hardening directives in the appliance unit are provisional until hardware check H1.** Any directive that prevents the panel rendering is removed, and the removal recorded in `docs/hardware-checks.md` with the symptom that justified it. A hardened unit that does not render is worth less than a plain one that does.
- **Tests run headless:** `SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy`, as `make test` already sets. No test may require a display, a network, or a real `nmcli`.
- **The existing 63 device tests must keep passing** at every commit.
- **Do not run `terraform apply`, any AWS mutating call, `make deploy`, `make provision`, `tools/provision.sh`, or `git push`.** This plan touches no cloud resource.

## File Structure

**New:**

| File | Responsibility |
|---|---|
| `device/scoreboard/netcfg.py` | Parse the boot-partition Wi-Fi file; drive NetworkManager through `nmcli`; the `python -m scoreboard.netcfg` entry point the boot oneshot runs |
| `device/scoreboard/screens.py` | Draw the non-scoreboard panels (unregistered, offline, settings) and decide which is showing |
| `device/scoreboard/settings.py` | The settings screen's state machine — pure logic, no pygame, no subprocess |
| `device/scoreboard/reset.py` | Factory reset: clear the identity directory and forget saved networks |
| `device/scoreboard-appliance.service` | Systemd unit for the appliance layout (hardened, `User=scoreboard`) |
| `device/scoreboard-netcfg.service` | Root oneshot that applies the boot-partition file, ordered before the main service |
| `device/polkit/10-scoreboard-network.rules` | The narrow polkit grant letting the unprivileged service change Wi-Fi |
| `docs/hardware-checks.md` | H1–H7 with pass criteria, filled in on real hardware |
| `device/tests/test_netcfg.py`, `test_screens.py`, `test_settings.py`, `test_reset.py` | Their tests |

**Modified:** `device/scoreboard/config.py`, `device/scoreboard/main.py`, `tools/pi-setup.sh`, `device/tests/test_config.py`, `device/tests/test_main.py`, `device/tests/test_pi_setup.py`, `README.md`.

**Deliberately unchanged:** `device/scoreboard.service` keeps working for checkouts. The appliance gets a *second* template rather than placeholders, because `ProtectHome=yes` and `ProtectSystem=strict` would break a checkout living in a home directory. "One install path" means one script, not one unit file.

---

### Task 1: An unregistered panel is a state, not a crash

**Files:**
- Modify: `device/scoreboard/config.py:1-70`
- Test: `device/tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `NotProvisioned(RuntimeError)`; `default_config_dir() -> Path`; `Config.load(config_dir: Path | None = None) -> Config`.

Today `Config.load` raises a generic `RuntimeError` when `device.json` is missing and `main` exits 1. A flashed image has no `device.json`, so it would crash-loop forever. It also has a real bug: a *corrupt* `device.json` raises an uncaught `json.JSONDecodeError`. Those are different failures and must stay different — an unregistered panel shows a setup screen; a corrupt one must refuse to start, because showing "not registered" for a panel that is in fact already claimed invites someone to register it twice.

- [ ] **Step 1: Write the failing tests**

Add to `device/tests/test_config.py`:

```python
import json
import pytest
from scoreboard.config import Config, NotProvisioned, default_config_dir


def test_missing_identity_raises_not_provisioned(tmp_path):
    with pytest.raises(NotProvisioned):
        Config.load(tmp_path)


def test_not_provisioned_is_a_runtime_error(tmp_path):
    # Existing callers catch RuntimeError; they must keep working.
    with pytest.raises(RuntimeError):
        Config.load(tmp_path)


def test_corrupt_json_is_not_not_provisioned(tmp_path):
    (tmp_path / "device.json").write_text("{not json")
    with pytest.raises(RuntimeError) as caught:
        Config.load(tmp_path)
    assert not isinstance(caught.value, NotProvisioned)


def test_missing_required_key_is_not_not_provisioned(tmp_path):
    (tmp_path / "device.json").write_text(json.dumps({"endpoint": "x"}))  # no thingName
    with pytest.raises(RuntimeError) as caught:
        Config.load(tmp_path)
    assert not isinstance(caught.value, NotProvisioned)


def test_config_dir_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOREBOARD_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert default_config_dir() == tmp_path / "elsewhere"


def test_config_dir_defaults_to_the_checkout(monkeypatch):
    monkeypatch.delenv("SCOREBOARD_CONFIG_DIR", raising=False)
    assert default_config_dir().name == "config"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'NotProvisioned'`.

- [ ] **Step 3: Implement**

In `device/scoreboard/config.py`, add `import os` at the top, replace the `CONFIG_DIR` constant, and rewrite `load`:

```python
def default_config_dir() -> Path:
    """Where this device's identity lives.

    A checkout keeps it in device/config/. An appliance image has no
    checkout, and cannot know what the owner will call their user account,
    so its unit points this at /var/lib/scoreboard instead.
    """
    override = os.environ.get("SCOREBOARD_CONFIG_DIR")
    return Path(override) if override else ROOT / "config"


class NotProvisioned(RuntimeError):
    """This panel has no identity yet.

    Not a failure to exit on: it is the state every freshly flashed device
    starts in. The panel shows its setup screen until someone registers it.
    """
```

Replace the body of `load` down to the `return cls(...)`:

```python
    @classmethod
    def load(cls, config_dir: Path | None = None) -> "Config":
        directory = default_config_dir() if config_dir is None else config_dir
        device_json = directory / "device.json"
        try:
            text = device_json.read_text()
        except OSError as e:
            raise NotProvisioned(
                f"no device identity at {device_json}; this panel is not registered yet"
            ) from e
        try:
            d = json.loads(text)
        except ValueError as e:
            # A corrupt file is not an unregistered device. Refusing to start is
            # right: showing the setup screen for a panel that is already claimed
            # would invite someone to register it a second time.
            raise RuntimeError(f"{device_json}: not valid JSON: {e}") from e
        try:
            endpoint, client_id = d["endpoint"], d["thingName"]
        except (KeyError, TypeError) as e:
            raise RuntimeError(f"{device_json}: missing or malformed {e}") from e
        try:
            rotate = parse_rotate(d.get("rotate"))
        except ValueError as e:
            raise RuntimeError(f"{device_json}: {e}") from e
        return cls(
            endpoint=endpoint, client_id=client_id,
            cert=directory / "device.pem.crt", key=directory / "private.pem.key",
            ca=directory / "AmazonRootCA1.pem", state_file=directory / "state.json",
            brightness=float(d.get("brightness", 1.0)), rotate=rotate,
        )
```

Delete the now-unused module-level `CONFIG_DIR`.

- [ ] **Step 4: Run the whole device suite**

Run: `cd device && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy ../.venv/bin/pytest -q`
Expected: PASS, with the pre-existing tests still green.

- [ ] **Step 5: Commit**

```bash
git add device/scoreboard/config.py device/tests/test_config.py
git commit -m "device: an unregistered panel is a state, not a crash"
```

---

### Task 2: The boot-partition Wi-Fi file

**Files:**
- Create: `device/scoreboard/netcfg.py`
- Test: `device/tests/test_netcfg.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `WifiSettings(ssid: str, psk: str | None, country: str | None, hidden: bool)`; `parse_wifi_file(text: str) -> WifiSettings | None`; `consume(path: Path, when: str) -> None`; `BOOT_FILE: Path`.

This task is parsing only — no NetworkManager, no subprocess. It is the most security-relevant input handling in the plan, because its input is a file edited by strangers on machines we do not control, so it gets its own review gate.

`None` means "nothing to do" (empty, comments only, or no `ssid`). `ValueError` means the file says something and what it says cannot work.

- [ ] **Step 1: Write the failing tests**

Create `device/tests/test_netcfg.py`:

```python
import pytest
from scoreboard.netcfg import WifiSettings, parse_wifi_file, consume


def test_plain_file():
    assert parse_wifi_file("ssid=HomeNet\npsk=supersecret\n") == WifiSettings(
        ssid="HomeNet", psk="supersecret", country=None, hidden=False)


def test_crlf_line_endings():
    # Notepad on Windows. If this fails, most users are locked out.
    assert parse_wifi_file("ssid=HomeNet\r\npsk=supersecret\r\n").ssid == "HomeNet"


def test_utf8_bom():
    # Notepad again: it prefixes a BOM that would otherwise become part of
    # the first key, so "ssid" would never match.
    assert parse_wifi_file("\ufeffssid=HomeNet\npsk=supersecret\n").ssid == "HomeNet"


def test_whitespace_and_key_case():
    got = parse_wifi_file("  SSID = HomeNet  \n\tPsk\t=\tsupersecret\n")
    assert got.ssid == "HomeNet" and got.psk == "supersecret"


def test_comments_and_blank_lines_ignored():
    assert parse_wifi_file("# a comment\n\nssid=HomeNet\npsk=supersecret\n").ssid == "HomeNet"


def test_line_without_equals_is_ignored():
    assert parse_wifi_file("nonsense\nssid=HomeNet\npsk=supersecret\n").ssid == "HomeNet"


def test_password_may_contain_equals():
    assert parse_wifi_file("ssid=HomeNet\npsk=a=b=c=dxyz\n").psk == "a=b=c=dxyz"


@pytest.mark.parametrize("text", ["", "   \n", "# only a comment\n", "psk=supersecret\n"])
def test_nothing_to_do(text):
    assert parse_wifi_file(text) is None


def test_open_network_has_no_psk():
    assert parse_wifi_file("ssid=CoffeeShop\n").psk is None


def test_ssid_too_long():
    with pytest.raises(ValueError, match="33 bytes"):
        parse_wifi_file("ssid=" + "x" * 33 + "\n")


def test_ssid_length_is_counted_in_bytes():
    # 17 three-byte characters is 51 bytes: legal as characters, not as an SSID.
    with pytest.raises(ValueError, match="bytes"):
        parse_wifi_file("ssid=" + "あ" * 17 + "\n")


@pytest.mark.parametrize("psk", ["short", "x" * 64])
def test_psk_length_rejected(psk):
    with pytest.raises(ValueError, match="psk"):
        parse_wifi_file(f"ssid=HomeNet\npsk={psk}\n")


def test_country_is_upper_cased():
    assert parse_wifi_file("ssid=HomeNet\npsk=supersecret\ncountry=us\n").country == "US"


@pytest.mark.parametrize("country", ["USA", "1A", "u"])
def test_bad_country_rejected(country):
    with pytest.raises(ValueError, match="country"):
        parse_wifi_file(f"ssid=HomeNet\npsk=supersecret\ncountry={country}\n")


@pytest.mark.parametrize("value,want", [("yes", True), ("true", True), ("1", True),
                                        ("no", False), ("", False)])
def test_hidden(value, want):
    assert parse_wifi_file(f"ssid=HomeNet\npsk=supersecret\nhidden={value}\n").hidden is want


def test_consume_removes_the_password(tmp_path):
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    consume(path, "2026-09-12 14:05 UTC")
    left = path.read_text()
    assert "supersecret" not in left
    assert "HomeNet" not in left
    assert "2026-09-12 14:05 UTC" in left
    # And what is left must still be a file the parser treats as "nothing to do",
    # or the next boot would re-apply it.
    assert parse_wifi_file(left) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_netcfg.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'scoreboard.netcfg'`.

- [ ] **Step 3: Implement**

Create `device/scoreboard/netcfg.py`:

```python
"""Network configuration: the boot-partition Wi-Fi file, and NetworkManager.

The file exists because Raspberry Pi OS keeps Wi-Fi credentials in
/etc/NetworkManager/system-connections/, on the ext4 root partition, which
Windows and macOS cannot read. /boot/firmware is FAT, so it is the only
part of the card a user with any computer can reach. It is read on every
boot, not only the first, which is what makes it a repair tool rather than
a first-run convenience.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BOOT_FILE = Path("/boot/firmware/scoreboard-wifi.txt")
MAX_SSID_BYTES = 32
MIN_PSK_CHARS, MAX_PSK_CHARS = 8, 63


@dataclass(frozen=True)
class WifiSettings:
    ssid: str
    psk: str | None = None
    country: str | None = None
    hidden: bool = False


def parse_wifi_file(text: str) -> WifiSettings | None:
    """Read the boot-partition file.

    Returns None when there is nothing to do -- an empty file, comments
    only, or no ssid. Raises ValueError when the file says something that
    cannot work, so the caller can leave it in place for the user to fix.

    Written for people editing a FAT partition in Notepad or TextEdit: a
    UTF-8 BOM is stripped, CRLF is handled, surrounding whitespace is
    ignored, keys are case-insensitive, and only the first '=' separates so
    a password may contain more.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().lower()] = value.strip()

    ssid = values.get("ssid", "")
    if not ssid:
        return None
    width = len(ssid.encode("utf-8"))
    if width > MAX_SSID_BYTES:
        raise ValueError(f"ssid is {width} bytes; the maximum is {MAX_SSID_BYTES}")

    psk = values.get("psk") or None
    if psk is not None and not MIN_PSK_CHARS <= len(psk) <= MAX_PSK_CHARS:
        raise ValueError(
            f"psk is {len(psk)} characters; a Wi-Fi password is between "
            f"{MIN_PSK_CHARS} and {MAX_PSK_CHARS}. Leave the line out entirely "
            "for an open network."
        )

    country = values.get("country") or None
    if country is not None:
        country = country.upper()
        if len(country) != 2 or not country.isalpha():
            raise ValueError(f"country must be a two-letter code such as US, got {country!r}")

    return WifiSettings(
        ssid=ssid, psk=psk, country=country,
        hidden=values.get("hidden", "").lower() in ("1", "true", "yes"),
    )


def consume(path: Path, when: str) -> None:
    """Replace the file with a note saying it was applied.

    The password is now in NetworkManager's own store, root-owned on the
    root partition. Leaving a copy here would mean a cleartext Wi-Fi
    password living permanently on the one partition every operating system
    mounts automatically when the card is plugged in.
    """
    path.write_text(
        f"# Applied by the scoreboard on {when}.\n"
        "#\n"
        "# The network details that were here are stored on the device now, and\n"
        "# have been removed from this file, which any computer can read.\n"
        "#\n"
        "# To change networks, replace all of this with:\n"
        "#   ssid=YourNetworkName\n"
        "#   psk=YourWiFiPassword\n"
        "# and reboot the panel.\n"
    )
```

- [ ] **Step 4: Run the tests**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_netcfg.py -q`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
git add device/scoreboard/netcfg.py device/tests/test_netcfg.py
git commit -m "device: read Wi-Fi settings from the one partition every computer can read"
```

---

### Task 3: Drive NetworkManager, and apply the boot file at boot

**Files:**
- Modify: `device/scoreboard/netcfg.py` (append; do not alter Task 2's parser)
- Create: `device/scoreboard-netcfg.service`
- Test: `device/tests/test_netcfg.py` (append)

**Interfaces:**
- Consumes: `WifiSettings`, `parse_wifi_file`, `consume`, `BOOT_FILE` from Task 2.
- Produces: `NetworkError(Exception)`; `split_terse(line: str) -> list[str]`; `Network(ssid: str, signal: int, secured: bool)`; `Status(online: bool, ssid: str | None, ip: str | None)`; `NetworkManager(run=None)` with `.scan() -> list[Network]`, `.apply(settings: WifiSettings) -> None`, `.forget_all() -> None`, `.status() -> Status`; `set_country(code: str, run=None) -> None`; `apply_boot_file(path=BOOT_FILE, nm=None, now=None) -> bool`; `main(argv=None) -> int`.

The runner is injected so no test shells out. Two rules from the Global Constraints apply directly here: arguments are always a list, never a shell string; and the boot file is consumed **only on success**, because a file that failed to parse is the only copy of what the user meant and they need to see it to fix it.

- [ ] **Step 1: Write the failing tests**

Append to `device/tests/test_netcfg.py`:

```python
from scoreboard.netcfg import (NetworkManager, NetworkError, Network, Status,
                               split_terse, apply_boot_file)


class FakeNmcli:
    """Stands in for nmcli. Records calls; returns canned output per subcommand."""

    def __init__(self, outputs=None, fail_on=None):
        self.outputs = outputs or {}
        self.fail_on = fail_on
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if self.fail_on is not None and self.fail_on in args:
            raise NetworkError("nmcli said no")
        for key, value in self.outputs.items():
            if key in args:
                return value
        return ""


def test_split_terse_plain():
    assert split_terse("HomeNet:72:WPA2") == ["HomeNet", "72", "WPA2"]


def test_split_terse_unescapes_colons():
    # An SSID may contain a colon; nmcli -t escapes it. A naive split mangles it.
    assert split_terse(r"Cafe\:Bar:64:WPA2") == ["Cafe:Bar", "64", "WPA2"]


def test_split_terse_unescapes_backslash():
    assert split_terse(r"Home\\Net:64:") == ["Home\\Net", "64", ""]


def test_scan_sorts_by_signal_and_drops_unnamed():
    nm = NetworkManager(run=FakeNmcli({"list": "Weak:20:WPA2\nStrong:88:WPA2\n:55:WPA2\n"}))
    assert [n.ssid for n in nm.scan()] == ["Strong", "Weak"]


def test_scan_keeps_the_strongest_of_a_repeated_ssid():
    nm = NetworkManager(run=FakeNmcli({"list": "HomeNet:20:WPA2\nHomeNet:88:WPA2\n"}))
    assert [(n.ssid, n.signal) for n in nm.scan()] == [("HomeNet", 88)]


def test_scan_marks_open_networks():
    nm = NetworkManager(run=FakeNmcli({"list": "Open:50:\nLocked:50:WPA2\n"}))
    assert {n.ssid: n.secured for n in nm.scan()} == {"Open": False, "Locked": True}


def test_apply_passes_arguments_as_a_list():
    fake = FakeNmcli()
    NetworkManager(run=fake).apply(WifiSettings(ssid="Home Net", psk="supersecret"))
    assert fake.calls == [["device", "wifi", "connect", "Home Net",
                          "password", "supersecret"]]


def test_apply_omits_the_password_for_an_open_network():
    fake = FakeNmcli()
    NetworkManager(run=fake).apply(WifiSettings(ssid="CoffeeShop"))
    assert fake.calls == [["device", "wifi", "connect", "CoffeeShop"]]


def test_apply_marks_a_hidden_network():
    fake = FakeNmcli()
    NetworkManager(run=fake).apply(WifiSettings(ssid="Quiet", psk="supersecret", hidden=True))
    assert fake.calls[0][-2:] == ["hidden", "yes"]


def test_forget_all_deletes_only_wireless_connections_by_uuid():
    fake = FakeNmcli({"show": "aaa:802-11-wireless\nbbb:ethernet\nccc:802-11-wireless\n"})
    NetworkManager(run=fake).forget_all()
    assert [c for c in fake.calls if c[0] == "connection" and c[1] == "delete"] == [
        ["connection", "delete", "uuid", "aaa"],
        ["connection", "delete", "uuid", "ccc"],
    ]


def test_status_reports_the_active_network():
    fake = FakeNmcli({"general": "connected\n",
                      "wifi": "no:Neighbour\nyes:HomeNet\n",
                      "show": "192.168.1.20/24\n"})
    got = NetworkManager(run=fake).status()
    assert got == Status(online=True, ssid="HomeNet", ip="192.168.1.20")


def test_apply_boot_file_applies_then_consumes(tmp_path):
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    fake = FakeNmcli()
    assert apply_boot_file(path, nm=NetworkManager(run=fake), now=lambda: "NOW") is True
    assert ["device", "wifi", "connect", "HomeNet", "password", "supersecret"] in fake.calls
    assert "supersecret" not in path.read_text()


def test_apply_boot_file_does_nothing_when_absent(tmp_path):
    fake = FakeNmcli()
    assert apply_boot_file(tmp_path / "missing.txt", nm=NetworkManager(run=fake)) is False
    assert fake.calls == []


def test_apply_boot_file_leaves_a_broken_file_alone(tmp_path):
    # The user's only copy of what they meant. Consuming it would destroy the
    # evidence they need to fix the typo.
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=short\n")
    with pytest.raises(ValueError):
        apply_boot_file(path, nm=NetworkManager(run=FakeNmcli()))
    assert "psk=short" in path.read_text()


def test_apply_boot_file_leaves_the_file_when_nmcli_fails(tmp_path):
    path = tmp_path / "scoreboard-wifi.txt"
    path.write_text("ssid=HomeNet\npsk=supersecret\n")
    nm = NetworkManager(run=FakeNmcli(fail_on="connect"))
    with pytest.raises(NetworkError):
        apply_boot_file(path, nm=nm)
    assert "psk=supersecret" in path.read_text()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_netcfg.py -q`
Expected: FAIL with `ImportError: cannot import name 'NetworkManager'`.

- [ ] **Step 3: Implement**

Append to `device/scoreboard/netcfg.py` (add `import logging`, `import subprocess`, `import sys`, and `from datetime import datetime, timezone` to the imports):

```python
log = logging.getLogger("scoreboard.netcfg")


class NetworkError(Exception):
    """nmcli refused or failed."""


@dataclass(frozen=True)
class Network:
    ssid: str
    signal: int
    secured: bool


@dataclass(frozen=True)
class Status:
    online: bool
    ssid: str | None
    ip: str | None


def split_terse(line: str) -> list[str]:
    """Split one line of `nmcli -t` output.

    nmcli escapes ':' as '\\:' and '\\' as '\\\\' in terse output, so
    line.split(':') mangles any SSID containing a colon -- which is legal,
    and does happen in the wild.
    """
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


def _run_nmcli(args: list[str]) -> str:
    # A list, never a string, and never shell=True: an SSID is attacker-chosen
    # text from the air, and a password is whatever the user typed.
    result = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise NetworkError((result.stderr or result.stdout).strip() or "nmcli failed")
    return result.stdout


def _run_raspi_config(args: list[str]) -> str:
    result = subprocess.run(["raspi-config", *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise NetworkError((result.stderr or result.stdout).strip() or "raspi-config failed")
    return result.stdout


def set_country(code: str, run=None) -> None:
    """Set the Wi-Fi regulatory domain.

    Without it the radio may refuse 5 GHz channels altogether, which looks
    exactly like "my network isn't in the list" and sends people hunting in
    the wrong place.
    """
    (run or _run_raspi_config)(["nonint", "do_wifi_country", code])


class NetworkManager:
    """nmcli, wrapped. The runner is injected so tests never shell out."""

    def __init__(self, run=None) -> None:
        self._run = run if run is not None else _run_nmcli

    def scan(self) -> list[Network]:
        out = self._run(["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"])
        best: dict[str, Network] = {}
        for line in out.splitlines():
            fields = split_terse(line)
            if len(fields) < 3 or not fields[0]:
                continue  # a hidden network advertises no name
            try:
                signal = int(fields[1])
            except ValueError:
                signal = 0
            found = Network(ssid=fields[0], signal=signal, secured=bool(fields[2].strip()))
            if found.ssid not in best or signal > best[found.ssid].signal:
                best[found.ssid] = found
        return sorted(best.values(), key=lambda n: n.signal, reverse=True)

    def apply(self, settings: WifiSettings) -> None:
        args = ["device", "wifi", "connect", settings.ssid]
        if settings.psk:
            args += ["password", settings.psk]
        if settings.hidden:
            args += ["hidden", "yes"]
        self._run(args)

    def forget_all(self) -> None:
        out = self._run(["-t", "-f", "UUID,TYPE", "connection", "show"])
        for line in out.splitlines():
            fields = split_terse(line)
            if len(fields) >= 2 and fields[1] == "802-11-wireless":
                # By UUID: a connection name can contain anything at all.
                self._run(["connection", "delete", "uuid", fields[0]])

    def status(self) -> Status:
        online = self._run(["-t", "-f", "STATE", "general"]).strip() == "connected"
        ssid = None
        for line in self._run(["-t", "-f", "ACTIVE,SSID", "device", "wifi"]).splitlines():
            fields = split_terse(line)
            if len(fields) >= 2 and fields[0] == "yes":
                ssid = fields[1]
                break
        ip = None
        for line in self._run(["-t", "-f", "IP4.ADDRESS", "device", "show"]).splitlines():
            fields = split_terse(line)
            value = fields[-1] if fields else ""
            if value:
                ip = value.split("/")[0]
                break
        return Status(online=online, ssid=ssid, ip=ip)


def apply_boot_file(path: Path = BOOT_FILE, nm: "NetworkManager | None" = None, now=None) -> bool:
    """Apply the boot-partition file if it has anything to say.

    Returns True if settings were applied and the file consumed. Raises on a
    file that cannot work, leaving it in place: it is the user's only copy of
    what they meant, and they need to read it to fix it.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    settings = parse_wifi_file(text)
    if settings is None:
        return False
    manager = nm if nm is not None else NetworkManager()
    if settings.country:
        set_country(settings.country)
    manager.apply(settings)
    stamp = now() if now is not None else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    consume(path, stamp)
    return True


def main(argv=None) -> int:
    logging.basicConfig(level="INFO")
    try:
        if apply_boot_file():
            log.info("applied Wi-Fi settings from %s", BOOT_FILE)
        return 0
    except ValueError as e:
        # Deliberately not a failure exit: a typo in a user's file must not
        # stop the panel booting. Say so in the journal and carry on.
        log.error("%s: %s -- left in place so it can be corrected", BOOT_FILE, e)
        return 0
    except NetworkError as e:
        log.error("could not apply %s: %s", BOOT_FILE, e)
        return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests**

Run: `cd device && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy ../.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 5: Write the oneshot unit**

Create `device/scoreboard-netcfg.service`:

```ini
# Applies device/config-free Wi-Fi settings left on the boot partition, then
# removes them from it. Runs on every boot, not just the first: this is the
# only way to change a panel's network from a Windows or Mac machine, because
# NetworkManager keeps its connections on the ext4 root partition that neither
# can read.

[Unit]
Description=Apply scoreboard Wi-Fi settings from the boot partition
After=NetworkManager.service
Wants=NetworkManager.service
Before=scoreboard.service

[Service]
Type=oneshot
RemainAfterExit=no
WorkingDirectory=/opt/scoreboard
ExecStart=/opt/scoreboard/.venv/bin/python -m scoreboard.netcfg
# Runs as root: it writes a NetworkManager system connection and rewrites a
# file on the boot partition. It is a few seconds at boot, not a resident
# service, and it exits 0 even when the file is wrong so a typo cannot stop
# the panel starting.

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 6: Commit**

```bash
git add device/scoreboard/netcfg.py device/tests/test_netcfg.py device/scoreboard-netcfg.service
git commit -m "device: apply boot-partition Wi-Fi settings through NetworkManager"
```

---

### Task 4: Screens for a panel with no identity and no network

**Files:**
- Create: `device/scoreboard/screens.py`
- Modify: `device/scoreboard/main.py:40-60` (config load) and `:170-182` (frame selection)
- Test: `device/tests/test_screens.py`, `device/tests/test_main.py` (append)

**Interfaces:**
- Consumes: `NotProvisioned` from Task 1; `NetworkManager`, `Status` from Task 3.
- Produces: `SCOREBOARD`, `UNREGISTERED`, `OFFLINE` (str constants); `screen_for(has_identity: bool, has_network: bool) -> str`; `build_identity(path: Path = BUILD_FILE) -> str`; `draw_message(surface, assets, title: str, lines: list[str]) -> None`; `draw_unregistered(surface, assets, build: str) -> None`; `draw_offline(surface, assets, build: str) -> None`; `BUILD_FILE: Path`.

`build_identity` reads `/etc/scoreboard-build`, which B2's pi-gen stage writes. B1 ships before any image exists, so the file's absence is normal and the text becomes `"development build"`. A device that cannot say which image it is running is a device nobody can support.

- [ ] **Step 1: Write the failing tests**

Create `device/tests/test_screens.py`:

```python
import pygame
import pytest

from scoreboard import screens
from scoreboard.assets import Assets
from scoreboard.render import W, H, BG


@pytest.mark.parametrize("identity,network,want", [
    (False, False, screens.UNREGISTERED),
    (False, True, screens.UNREGISTERED),
    (True, False, screens.OFFLINE),
    (True, True, screens.SCOREBOARD),
])
def test_screen_for(identity, network, want):
    assert screens.screen_for(identity, network) == want


def test_build_identity_reads_the_stamp(tmp_path):
    stamp = tmp_path / "scoreboard-build"
    stamp.write_text("image 2026-09-20, commit abc1234\n")
    assert screens.build_identity(stamp) == "image 2026-09-20, commit abc1234"


def test_build_identity_without_a_stamp_says_development(tmp_path):
    assert screens.build_identity(tmp_path / "absent") == "development build"


@pytest.mark.parametrize("draw", [screens.draw_unregistered, screens.draw_offline])
def test_screens_paint_something(draw):
    pygame.init()
    surface = pygame.Surface((W, H))
    draw(surface, Assets(), "development build")
    blank = pygame.Surface((W, H))
    blank.fill(BG)
    # Not a blank panel. Compared byte-for-byte: an average would round a
    # mostly-dark screen with a little text on it straight back to BG.
    assert pygame.image.tostring(surface, "RGB") != pygame.image.tostring(blank, "RGB")
```

Append to `device/tests/test_main.py`:

```python
def test_an_unregistered_panel_reaches_the_display_instead_of_exiting(tmp_path):
    # Before this change main exited 1 on a missing device.json, never reaching
    # the display. Now it must get past config and fail on the bogus driver
    # instead -- which is how we prove config no longer short-circuits boot.
    env = dict(os.environ,
               SCOREBOARD_CONFIG_DIR=str(tmp_path),
               SDL_VIDEODRIVER="definitelynotadriver")
    env.pop("DISPLAY", None)
    env.pop("SCOREBOARD_FIXTURE", None)
    done = subprocess.run([sys.executable, "-m", "scoreboard.main"],
                          cwd=Path(__file__).resolve().parents[1],
                          env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == EX_CONFIG
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_screens.py tests/test_main.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scoreboard.screens'`, and the main test exits 1 rather than 78.

- [ ] **Step 3: Create the screens module**

Create `device/scoreboard/screens.py`:

```python
"""The panels that are not the scoreboard.

A device fresh from the image has no identity and may have no network, and
until now the service simply exited. These are what it shows instead --
and "Not registered" is where the pairing code will go when the admin site
learns to issue one.
"""
from __future__ import annotations

from pathlib import Path

import pygame

from .assets import Assets
from .render import W, H, BG, INK, MUTED

SCOREBOARD, UNREGISTERED, OFFLINE = "scoreboard", "unregistered", "offline"
BUILD_FILE = Path("/etc/scoreboard-build")


def screen_for(has_identity: bool, has_network: bool) -> str:
    """Which panel is showing. Identity first: a panel nobody has registered
    has nothing to say about hockey even with perfect Wi-Fi."""
    if not has_identity:
        return UNREGISTERED
    if not has_network:
        return OFFLINE
    return SCOREBOARD


def build_identity(path: Path = BUILD_FILE) -> str:
    """Which image this is, for the corner of the setup screens.

    Written by the image build. A checkout has no such file, and that is a
    normal state rather than a fault.
    """
    try:
        return path.read_text().strip() or "development build"
    except OSError:
        return "development build"


def draw_message(surface: pygame.Surface, assets: Assets, title: str, lines: list[str]) -> None:
    surface.fill(BG)
    y = H // 2 - 120
    heading = assets.font(96, True).render(title, True, INK)
    surface.blit(heading, heading.get_rect(midtop=(W // 2, y)))
    y += 118
    for line in lines:
        img = assets.font(44, False).render(line, True, MUTED)
        surface.blit(img, img.get_rect(midtop=(W // 2, y)))
        y += 54


def draw_unregistered(surface: pygame.Surface, assets: Assets, build: str) -> None:
    draw_message(surface, assets, "Not registered", [
        "This panel has no identity yet.",
        "Press S for network settings.",
        build,
    ])


def draw_offline(surface: pygame.Surface, assets: Assets, build: str) -> None:
    draw_message(surface, assets, "No network", [
        "This panel cannot reach Wi-Fi.",
        "Press S for network settings.",
        build,
    ])
```

- [ ] **Step 4: Stop main exiting on a missing identity**

In `device/scoreboard/main.py`, change the import line to
`from .config import Config, NotProvisioned, parse_rotate`, add
`from . import screens` and `from .netcfg import NetworkManager`, and replace the config block (currently lines 43-49):

```python
    cfg = None
    if not fixture:
        try:
            cfg = Config.load()
        except NotProvisioned as e:
            # The normal state of a freshly flashed panel, not a failure.
            log.info("%s", e)
        except RuntimeError as e:
            # A corrupt or incomplete identity. Restarting cannot help.
            log.error("%s", e)
            sys.exit(1)
```

- [ ] **Step 5: Choose a screen each frame**

Still in `main`, after `frame = pygame.Surface((W, H))` add:

```python
    build = screens.build_identity()
    nm = NetworkManager()
    net_ok = False
    last_net_check = 0.0
    NET_POLL_S = 10
```

Replace the blanking/draw block (currently lines 171-174) with:

```python
            # While MQTT is connected there is demonstrably a network, so the
            # scoreboard path costs no nmcli calls at all. Only a panel that
            # isn't working asks the radio, and then only every 10 seconds.
            if link_ok:
                net_ok, last_net_check = True, time.time()
            elif time.time() - last_net_check >= NET_POLL_S:
                last_net_check = time.time()
                try:
                    net_ok = nm.status().online
                except Exception as e:  # nmcli absent on a desktop, or failing
                    log.debug("network status unavailable: %s", e)
                    net_ok = False
            showing = screens.screen_for(cfg is not None or bool(fixture), net_ok or bool(fixture))
            if showing == screens.UNREGISTERED:
                screens.draw_unregistered(frame, assets, build)
            elif showing == screens.OFFLINE:
                screens.draw_offline(frame, assets, build)
            elif should_blank(time.time(), last_update, current, BLANK_AFTER_S):
                frame.fill((0, 0, 0))
            else:
                draw(frame, current, now_ms, assets, link_ok)
```

- [ ] **Step 6: Run the whole suite**

Run: `cd device && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy ../.venv/bin/pytest -q`
Expected: PASS, including the pre-existing tests and the new subprocess test.

- [ ] **Step 7: Commit**

```bash
git add device/scoreboard/screens.py device/scoreboard/main.py device/tests/test_screens.py device/tests/test_main.py
git commit -m "device: show a setup screen instead of exiting when unregistered"
```

---

### Task 5: The settings screen, and factory reset

**Files:**
- Create: `device/scoreboard/settings.py`, `device/scoreboard/reset.py`
- Modify: `device/scoreboard/screens.py` (append `draw_settings`), `device/scoreboard/main.py` (the event loop), `device/scoreboard/buttons.py` (append the hold watcher)
- Test: `device/tests/test_settings.py`, `device/tests/test_reset.py`

**Interfaces:**
- Consumes: `WifiSettings`, `NetworkManager`, `Network` from Tasks 2-3; `draw_message` from Task 4.
- Produces: `Settings()` with `.mode`, `.networks`, `.index`, `.message`, `.masked`, `.reveal`, `.typed`, `.pending`, `.key(name: str, char: str = "") -> None`, `.done(message: str) -> None`, `.closed: bool`; mode constants `LIST`, `PASSWORD`, `WORKING`, `RESULT`, `CONFIRM_RESET`; `factory_reset(config_dir: Path, nm) -> None`; `screens.draw_settings(surface, assets, settings, status, build) -> None`; `buttons.HoldWatcher(seconds: float = 10.0)` with `.update(a_down: bool, b_down: bool, now: float) -> bool`; `buttons.pressed() -> tuple[bool, bool]`.

`Settings` is pure: it is fed key names and produces state plus a `pending` request. The caller does the slow work — scanning, applying, resetting — and calls `done()` with what to show. That keeps every branch of the screen testable with no display and no radio.

**The constraint that shapes the class:** there is no API to load an existing pre-shared key into it. Not "we choose not to display it" — there is no method that could. That is the cheapest way to guarantee the Global Constraint.

- [ ] **Step 1: Write the failing tests**

Create `device/tests/test_settings.py`:

```python
import pytest

from scoreboard.netcfg import Network, WifiSettings
from scoreboard.settings import (Settings, LIST, PASSWORD, WORKING, RESULT,
                                 CONFIRM_RESET)

NETWORKS = [Network("HomeNet", 88, True), Network("CoffeeShop", 40, False)]


def fresh():
    return Settings(networks=list(NETWORKS))


def test_starts_on_the_list():
    assert fresh().mode == LIST


def test_arrow_keys_move_the_selection():
    s = fresh()
    s.key("down")
    assert s.index == 1
    s.key("up")
    assert s.index == 0


def test_selection_does_not_run_off_either_end():
    s = fresh()
    s.key("up")
    assert s.index == 0
    s.key("down"); s.key("down"); s.key("down")
    assert s.index == len(NETWORKS) - 1


def test_a_secured_network_asks_for_a_password():
    s = fresh()
    s.key("return")
    assert s.mode == PASSWORD and s.pending is None


def test_an_open_network_connects_straight_away():
    s = fresh()
    s.key("down")
    s.key("return")
    assert s.pending == ("apply", WifiSettings(ssid="CoffeeShop"))


def test_typing_a_password_then_applying():
    s = fresh()
    s.key("return")
    for ch in "supersecret":
        s.key("character", ch)
    s.key("return")
    assert s.pending == ("apply", WifiSettings(ssid="HomeNet", psk="supersecret"))
    assert s.mode == WORKING


def test_backspace_removes_a_character():
    s = fresh()
    s.key("return")
    for ch in "abc":
        s.key("character", ch)
    s.key("backspace")
    assert len(s.masked) == 2


def test_the_password_is_masked_and_can_be_revealed():
    s = fresh()
    s.key("return")
    for ch in "supersecret":
        s.key("character", ch)
    assert s.masked == "•" * 11
    s.key("tab")
    assert s.masked == "supersecret"


def test_there_is_no_way_to_load_an_existing_password():
    # The Global Constraint, enforced by absence rather than by discipline.
    assert not any("psk" in name.lower() for name in dir(Settings) if not name.startswith("_"))
    assert fresh().masked == ""


def test_escape_from_the_password_returns_to_the_list():
    s = fresh()
    s.key("return")
    s.key("character", "a")
    s.key("escape")
    assert s.mode == LIST and s.masked == ""


def test_done_reports_the_result():
    s = fresh()
    s.key("return"); s.key("return")
    s.done("Connected to HomeNet")
    assert s.mode == RESULT and s.message == "Connected to HomeNet"
    s.key("return")
    assert s.mode == LIST


def test_r_asks_to_confirm_a_factory_reset():
    s = fresh()
    s.key("character", "r")
    assert s.mode == CONFIRM_RESET and s.typed == ""


def test_factory_reset_needs_the_word_typed_exactly():
    s = fresh()
    s.key("character", "r")
    for ch in "RESET":
        s.key("character", ch)
    s.key("return")
    assert s.pending == ("reset", None)


@pytest.mark.parametrize("word", ["reset", "RESE", "RESETT", ""])
def test_the_wrong_word_does_not_reset(word):
    s = fresh()
    s.key("character", "r")
    for ch in word:
        s.key("character", ch)
    s.key("return")
    assert s.pending is None and s.mode == LIST


def test_escape_abandons_the_reset_confirmation():
    s = fresh()
    s.key("character", "r")
    s.key("escape")
    assert s.mode == LIST and s.pending is None


def test_s_closes_the_screen_from_the_list():
    s = fresh()
    s.key("character", "s")
    assert s.closed


def test_rescan_requests_a_scan():
    s = fresh()
    s.key("f5")
    assert s.pending == ("scan", None)
```

Create `device/tests/test_reset.py`:

```python
from scoreboard.reset import factory_reset


class FakeNM:
    def __init__(self):
        self.forgotten = False

    def forget_all(self):
        self.forgotten = True


def test_factory_reset_clears_the_identity_and_forgets_networks(tmp_path):
    for name in ("device.json", "device.pem.crt", "private.pem.key",
                 "AmazonRootCA1.pem", "state.json"):
        (tmp_path / name).write_text("x")
    keep = tmp_path / "something-else.txt"
    keep.write_text("not ours")
    nm = FakeNM()

    factory_reset(tmp_path, nm)

    assert not (tmp_path / "private.pem.key").exists()
    assert not (tmp_path / "device.json").exists()
    assert nm.forgotten
    assert keep.exists(), "must not clear files it does not own"


def test_factory_reset_is_safe_on_an_already_empty_device(tmp_path):
    nm = FakeNM()
    factory_reset(tmp_path, nm)  # must not raise
    assert nm.forgotten
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_settings.py tests/test_reset.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'scoreboard.settings'`.

- [ ] **Step 3: Write the state machine**

Create `device/scoreboard/settings.py`:

```python
"""The settings screen's state machine.

Pure logic: fed key names, it produces its own new state and a ``pending``
request for the caller to carry out. Scanning, connecting and resetting are
slow and involve the outside world, so they belong to the caller; every
branch of the screen is then testable with no display and no radio.

Note what is absent: there is no way to put an existing Wi-Fi password into
this object. The rule that the panel never displays a stored password is
enforced by there being no method that could, rather than by remembering
not to call one.
"""
from __future__ import annotations

from .netcfg import Network, WifiSettings

LIST, PASSWORD, WORKING, RESULT, CONFIRM_RESET = (
    "list", "password", "working", "result", "confirm_reset")
CONFIRM_WORD = "RESET"


class Settings:
    def __init__(self, networks: list[Network] | None = None) -> None:
        self.mode = LIST
        self.networks: list[Network] = networks or []
        self.index = 0
        self.message = ""
        self.reveal = False
        self.typed = ""
        self.closed = False
        self.pending: tuple[str, object] | None = None
        self._entry = ""

    @property
    def masked(self) -> str:
        return self._entry if self.reveal else "•" * len(self._entry)

    @property
    def selected(self) -> Network | None:
        if not self.networks:
            return None
        return self.networks[min(self.index, len(self.networks) - 1)]

    def replace(self, networks: list[Network]) -> None:
        self.networks = networks
        self.index = min(self.index, max(len(networks) - 1, 0))
        self.pending = None
        self.mode = LIST

    def done(self, message: str) -> None:
        """The caller finished what ``pending`` asked for."""
        self.pending = None
        self.message = message
        self.mode = RESULT
        self._entry = ""
        self.reveal = False

    def key(self, name: str, char: str = "") -> None:
        handler = {
            LIST: self._list_key,
            PASSWORD: self._password_key,
            CONFIRM_RESET: self._confirm_key,
            RESULT: self._result_key,
            WORKING: lambda n, c: None,
        }[self.mode]
        handler(name, char)

    def _list_key(self, name: str, char: str) -> None:
        if name == "down":
            self.index = min(self.index + 1, max(len(self.networks) - 1, 0))
        elif name == "up":
            self.index = max(self.index - 1, 0)
        elif name == "f5":
            self.pending = ("scan", None)
        elif name == "return":
            network = self.selected
            if network is None:
                return
            if network.secured:
                self.mode = PASSWORD
            else:
                self.pending = ("apply", WifiSettings(ssid=network.ssid))
                self.mode = WORKING
        elif char.lower() == "r":
            self.mode, self.typed = CONFIRM_RESET, ""
        elif char.lower() == "s" or name == "escape":
            self.closed = True

    def _password_key(self, name: str, char: str) -> None:
        if name == "escape":
            self.mode, self._entry, self.reveal = LIST, "", False
        elif name == "backspace":
            self._entry = self._entry[:-1]
        elif name == "tab":
            self.reveal = not self.reveal
        elif name == "return":
            network = self.selected
            if network is not None:
                self.pending = ("apply", WifiSettings(ssid=network.ssid, psk=self._entry))
                self.mode = WORKING
        elif char and char.isprintable():
            self._entry += char

    def _confirm_key(self, name: str, char: str) -> None:
        if name == "escape":
            self.mode, self.typed = LIST, ""
        elif name == "backspace":
            self.typed = self.typed[:-1]
        elif name == "return":
            if self.typed == CONFIRM_WORD:
                self.pending = ("reset", None)
                self.mode = WORKING
            else:
                self.mode, self.typed = LIST, ""
        elif char and char.isprintable():
            self.typed += char

    def _result_key(self, name: str, char: str) -> None:
        self.mode, self.message = LIST, ""
```

Note for the implementer: `_list_key` checks `char.lower() == "r"` before the `"s"` branch, and both run only when no navigation key matched. The `r` that starts the confirmation is then typed again as part of `RESET` — that is fine, because `CONFIRM_RESET` starts with `typed` empty.

- [ ] **Step 4: Write factory reset**

Create `device/scoreboard/reset.py`:

```python
"""Factory reset: return the panel to its just-flashed state."""
from __future__ import annotations

from pathlib import Path

IDENTITY_FILES = ("device.json", "device.pem.crt", "private.pem.key",
                  "AmazonRootCA1.pem", "state.json")


def factory_reset(config_dir: Path, nm) -> None:
    """Clear this device's identity and forget its saved networks.

    It deliberately does not revoke the certificate in the cloud. It cannot:
    the key it would authenticate with is the very thing being deleted. And
    a revocation any passer-by could trigger by holding two buttons would be
    a denial-of-service switch, not a security control. Revocation belongs
    to the owner, from the site (SCO-24).

    Deleting these files is best effort, not erasure: wear levelling on an
    SD card can leave the old blocks readable. That is why the confirmation
    screen tells the user to remove the device from their account rather
    than implying a cleared panel is safe to pass on.

    Only the files this project put there are removed, so a reset cannot
    take anything else on the device with it.
    """
    for name in IDENTITY_FILES:
        (config_dir / name).unlink(missing_ok=True)
    nm.forget_all()
```

- [ ] **Step 5: Draw the screen**

Append to `device/scoreboard/screens.py`:

```python
def draw_settings(surface, assets: Assets, settings, status, build: str) -> None:
    """The settings screen. ``status`` is a netcfg.Status, or None."""
    from .settings import LIST, PASSWORD, WORKING, RESULT, CONFIRM_RESET, CONFIRM_WORD

    if settings.mode == PASSWORD:
        network = settings.selected
        draw_message(surface, assets, f"Password for {network.ssid if network else ''}", [
            settings.masked or "(type the Wi-Fi password)",
            "Enter to connect, Tab to show it, Esc to go back",
        ])
        return
    if settings.mode == WORKING:
        draw_message(surface, assets, "Working…", ["Talking to the network."])
        return
    if settings.mode == RESULT:
        draw_message(surface, assets, settings.message, ["Press any key."])
        return
    if settings.mode == CONFIRM_RESET:
        draw_message(surface, assets, "Erase this panel?", [
            f"Type {CONFIRM_WORD} and press Enter. Esc cancels.",
            settings.typed,
            "This clears the panel. To stop it connecting, also remove",
            "the device from your account on the website.",
        ])
        return

    surface.fill(BG)
    where = status.ssid if status and status.ssid else "not connected"
    address = status.ip if status and status.ip else "no address"
    heading = assets.font(64, True).render(f"Wi-Fi — {where} — {address}", True, INK)
    surface.blit(heading, heading.get_rect(midtop=(W // 2, 24)))
    y = 120
    for i, network in enumerate(settings.networks[:5]):
        colour = INK if i == settings.index else MUTED
        label = f"{'>' if i == settings.index else ' '} {network.ssid}  {network.signal}%"
        if not network.secured:
            label += "  (open)"
        img = assets.font(48, i == settings.index).render(label, True, colour)
        surface.blit(img, img.get_rect(topleft=(W // 2 - 420, y)))
        y += 56
    footer = "Enter to join · F5 rescan · R factory reset · S or Esc to close · " + build
    img = assets.font(32, False).render(footer, True, MUTED)
    surface.blit(img, img.get_rect(midbottom=(W // 2, H - 18)))
```

- [ ] **Step 6: Wire it into the loop**

In `device/scoreboard/main.py`, add `from .settings import Settings` and `from .reset import factory_reset`, plus `from .config import default_config_dir`. Before the loop:

```python
    panel: Settings | None = None   # not None while the settings screen is open
    status = None
```

In the event handling, replace the `K_ESCAPE` / `K_a` / `K_b` block with:

```python
                if ev.type == pygame.QUIT:
                    return
                if ev.type == pygame.KEYDOWN and panel is not None:
                    panel.key(pygame.key.name(ev.key), ev.unicode)
                    if panel.closed:
                        panel = None
                    continue
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                    return
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_s:
                    try:
                        panel, status = Settings(nm.scan()), nm.status()
                    except Exception as e:
                        log.warning("cannot open settings: %s", e)
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_a:
                    on_a()
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_b:
                    on_b()
```

After the event loop and before choosing a screen, carry out whatever the panel asked for. Scanning and connecting block for a second or two; on a settings screen that is acceptable and simpler than a thread:

```python
            if panel is not None and panel.pending is not None:
                what, payload = panel.pending
                try:
                    if what == "scan":
                        panel.replace(nm.scan())
                    elif what == "apply":
                        nm.apply(payload)
                        status = nm.status()
                        panel.done(f"Connected to {payload.ssid}")
                    elif what == "reset":
                        factory_reset(cfg.state_file.parent if cfg else default_config_dir(), nm)
                        panel.done("Panel erased. Reboot to start again.")
                except Exception as e:
                    panel.done(str(e))
```

And give the settings screen priority when drawing, immediately before the `showing = ...` line:

```python
            if panel is not None:
                screens.draw_settings(frame, assets, panel, status, build)
            elif ...   # the existing screen selection becomes the else-branch
```

- [ ] **Step 7: Factory reset from the buttons, for a panel with no keyboard**

The spec asks for "holding both buttons for ten seconds triggers the same
flow". Taken literally that is incoherent — the typed `RESET` confirmation
cannot be given by a device with no keyboard, which is the only kind of device
this path is for. **Resolution: the ten-second hold of two buttons is itself
the confirmation.** It is not something that happens by accident, and it is
the most deliberate act the hardware can express. It performs the reset and
shows the same result screen.

Write the failing test in `device/tests/test_reset.py`:

```python
from scoreboard.buttons import HoldWatcher


def test_both_buttons_must_be_held_for_the_full_time():
    watcher = HoldWatcher(seconds=10.0)
    assert watcher.update(True, True, 0.0) is False
    assert watcher.update(True, True, 9.9) is False
    assert watcher.update(True, True, 10.0) is True


def test_it_fires_only_once_per_hold():
    watcher = HoldWatcher(seconds=10.0)
    watcher.update(True, True, 0.0)
    assert watcher.update(True, True, 10.0) is True
    assert watcher.update(True, True, 20.0) is False


def test_releasing_either_button_restarts_the_clock():
    watcher = HoldWatcher(seconds=10.0)
    watcher.update(True, True, 0.0)
    watcher.update(True, False, 5.0)
    assert watcher.update(True, True, 11.0) is False
    assert watcher.update(True, True, 21.0) is True


def test_one_button_alone_never_fires():
    watcher = HoldWatcher(seconds=10.0)
    watcher.update(True, False, 0.0)
    assert watcher.update(True, False, 100.0) is False
```

Then append to `device/scoreboard/buttons.py`:

```python
BOTH_HELD_S = 10.0


class HoldWatcher:
    """Decides when both buttons have been held together long enough.

    Separate from gpiozero so the timing rule is testable with no hardware:
    feed it the two button states and a clock. Ten seconds of two buttons is
    not something anyone does by accident, which is what makes it an adequate
    confirmation for a panel that has no keyboard to type one on.
    """

    def __init__(self, seconds: float = BOTH_HELD_S) -> None:
        self.seconds = seconds
        self._since: float | None = None
        self._fired = False

    def update(self, a_down: bool, b_down: bool, now: float) -> bool:
        """True exactly once, on the update where the hold completes."""
        if not (a_down and b_down):
            self._since, self._fired = None, False
            return False
        if self._since is None:
            self._since = now
            return False
        if not self._fired and now - self._since >= self.seconds:
            self._fired = True
            return True
        return False


def pressed() -> tuple[bool, bool]:
    """Which buttons are down. (False, False) where there is no hardware."""
    pair = getattr(attach, "_keep", None)
    if pair is None:
        return (False, False)
    a, b = pair
    try:
        return (bool(a.is_pressed), bool(b.is_pressed))
    except Exception:
        return (False, False)
```

- [ ] **Step 8: Wire the hold into the loop**

In `main`, add `holds = buttons.HoldWatcher()` beside the other setup, and put
this immediately after the pygame event loop:

```python
            if holds.update(*buttons.pressed(), time.time()):
                log.warning("both buttons held: factory reset")
                if panel is None:
                    panel = Settings()
                try:
                    factory_reset(cfg.state_file.parent if cfg else default_config_dir(), nm)
                    panel.done("Panel erased. Reboot to start again.")
                except Exception as e:
                    panel.done(str(e))
```

- [ ] **Step 9: Run the whole suite**

Run: `cd device && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy ../.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add device/scoreboard/settings.py device/scoreboard/reset.py device/scoreboard/screens.py device/scoreboard/main.py device/scoreboard/buttons.py device/tests/test_settings.py device/tests/test_reset.py
git commit -m "device: change Wi-Fi and erase the panel from the panel itself"
```

---

### Task 6: The appliance layout

**Files:**
- Create: `device/scoreboard-appliance.service`, `device/polkit/10-scoreboard-network.rules`
- Modify: `tools/pi-setup.sh`
- Test: `device/tests/test_pi_setup.py` (append)

**Interfaces:**
- Consumes: `device/scoreboard-netcfg.service` from Task 3; `SCOREBOARD_CONFIG_DIR` from Task 1.
- Produces: `tools/pi-setup.sh --appliance` (install) and `--appliance --print-unit` / `--appliance --preflight`; the `scoreboard` system account; `/opt/scoreboard`; `/var/lib/scoreboard`.

Two unit templates, one script. The checkout unit stays exactly as it is, because `ProtectHome=yes` and `ProtectSystem=strict` would break a checkout living in a home directory. "One install path" means one script, so the image build and a manual install cannot drift.

The checkout mode also gains the `input` group, which it needs and never had: without it SDL cannot read `/dev/input/event*` under kmsdrm, so the existing `a` and `b` keys have never worked on a real panel either.

- [ ] **Step 1: Write the failing tests**

Append to `device/tests/test_pi_setup.py`:

```python
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


def test_appliance_unit_grants_the_input_group(checkout):
    out = run(checkout, "--appliance", "--print-unit").stdout
    assert "input" in out.split("SupplementaryGroups=")[1].splitlines()[0]


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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_pi_setup.py -q`
Expected: FAIL — `--appliance` is not a recognised option, so the script exits 2.

- [ ] **Step 3: Write the appliance unit**

Create `device/scoreboard-appliance.service`:

```ini
# The unit for an appliance image. Not a template: an image cannot know the
# owner's username, because Raspberry Pi OS renames the first account on first
# boot precisely so images do not ship a default one. So the service gets an
# account of its own, and the human login account has nothing to do with it.
#
# device/scoreboard.service remains the template for a git checkout, where
# ProtectHome and ProtectSystem=strict would break a checkout under /home.

[Unit]
Description=HockeyTrack scoreboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=scoreboard
Group=scoreboard
SupplementaryGroups=video render input gpio
WorkingDirectory=/opt/scoreboard
Environment=SDL_VIDEODRIVER=kmsdrm
Environment=PYTHONUNBUFFERED=1
Environment=SCOREBOARD_CONFIG_DIR=/var/lib/scoreboard
ExecStart=/opt/scoreboard/.venv/bin/python -m scoreboard.main
Restart=always
RestartSec=3
# 78 is EX_CONFIG: a failure a restart cannot fix, chiefly a pygame whose SDL
# has no kmsdrm driver. Stop, and leave the reason at the end of the journal.
RestartPreventExitStatus=78

# --- Console access for kmsdrm ---
# systemd starts this with no controlling terminal and no logind seat, so
# without a TTY attached SDL's kmsdrm backend cannot become DRM master and the
# panel stays black while the process looks healthy. Do not remove these.
TTYPath=/dev/tty1
StandardInput=tty
StandardOutput=journal
StandardError=journal
TTYReset=yes
TTYVHangup=yes

# --- Hardening ---
# PROVISIONAL until hardware check H1. Sandboxing a process that talks to
# /dev/dri directly is exactly where these break. Any directive that stops the
# panel rendering is removed, and the removal recorded in docs/hardware-checks.md
# with the symptom that justified it.
# MemoryDenyWriteExecute is deliberately absent: SDL and its drivers map
# executable pages, and denying that would trade a working panel for a
# checkbox.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/scoreboard
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
DeviceAllow=char-drm rw
DeviceAllow=char-input r
DeviceAllow=/dev/tty1 rw

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Write the polkit rule**

Create `device/polkit/10-scoreboard-network.rules`:

```javascript
// The scoreboard service runs as an unprivileged system account, but its
// settings screen has to be able to join a Wi-Fi network. polkit grants that
// to active local sessions; a system account has no session, so it has to be
// said explicitly here. Nothing beyond these three actions is granted, and
// nothing at all is granted to any other user.
polkit.addRule(function (action, subject) {
    if (subject.user !== "scoreboard") {
        return polkit.Result.NOT_HANDLED;
    }
    if (action.id === "org.freedesktop.NetworkManager.settings.modify.system" ||
        action.id === "org.freedesktop.NetworkManager.network-control" ||
        action.id === "org.freedesktop.NetworkManager.wifi.scan") {
        return polkit.Result.YES;
    }
    return polkit.Result.NOT_HANDLED;
});
```

- [ ] **Step 5: Teach the script the appliance layout**

In `tools/pi-setup.sh`, add near the top:

```bash
APPLIANCE=0
SERVICE_USER=scoreboard
APP_DIR=/opt/scoreboard
STATE_DIR=/var/lib/scoreboard
```

Make `preflight` skip the identity checks in appliance mode — the OS-codename refusal still applies to both, because it is the check that keeps a broken pygame out of the image:

```bash
preflight() {
  local codename mode f
  codename="$(. "$OS_RELEASE" && echo "${VERSION_CODENAME:-}")"
  case "$codename" in
    buster | bullseye | bookworm)
      die "Raspberry Pi OS $codename is too old. Its python3-pygame fails device/requirements.txt, so pip would install a PyPI wheel instead, and those are built without the kmsdrm driver the panel needs. Flash Raspberry Pi OS Lite (64-bit), Trixie or later." ;;
  esac
  # An image is built before any device has an identity, so there is nothing
  # to check for here; the identity arrives when the panel is registered.
  [ "$APPLIANCE" -eq 1 ] && return 0
  for f in device.json device.pem.crt private.pem.key AmazonRootCA1.pem; do
    [ -f "$CONFIG/$f" ] || die "missing $CONFIG/$f -- copy device/config/ over from the machine that ran make provision"
  done
  mode="$(stat -c %a "$CONFIG/private.pem.key")"
  case "$mode" in
    600 | 400) ;;
    *) die "$CONFIG/private.pem.key is mode $mode. It is this device's identity; make it owner-only: chmod 600 $CONFIG/private.pem.key" ;;
  esac
}
```

Make `render_unit` serve both:

```bash
render_unit() {
  if [ "$APPLIANCE" -eq 1 ]; then
    cat "$DEVICE/scoreboard-appliance.service"
    return
  fi
  case "$USER_NAME$DEVICE" in
    *'|'* | *'&'* | *\\*) die "cannot template a user or path containing | & or \\: $USER_NAME $DEVICE" ;;
  esac
  sed -e "s|@USER@|$USER_NAME|g" -e "s|@DEVICE_DIR@|$DEVICE|g" "$DEVICE/scoreboard.service"
}
```

Add the appliance install, and give the checkout path its missing `input` group:

```bash
install_appliance() {
  preflight
  echo "==> apt packages"
  apt-get update
  apt-get install -y python3-pygame python3-gpiozero python3-venv network-manager policykit-1

  echo "==> service account"
  getent passwd "$SERVICE_USER" >/dev/null || \
    useradd --system --home-dir "$STATE_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  for g in video render input gpio; do
    getent group "$g" >/dev/null && usermod -aG "$g" "$SERVICE_USER"
  done
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$STATE_DIR"

  echo "==> application"
  mkdir -p "$APP_DIR"
  cp -a "$DEVICE/scoreboard" "$DEVICE/requirements.txt" "$APP_DIR/"
  python3 -m venv --system-site-packages "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
  local where
  where="$(PYGAME_HIDE_SUPPORT_PROMPT=1 "$APP_DIR/.venv/bin/python" -c 'import os, pygame; print(os.path.dirname(pygame.__file__))')"
  case "$where" in
    /usr/lib/python3/dist-packages/*) ;;
    *) die "the venv is using pygame from $where, not the system package, so it has no kmsdrm driver." ;;
  esac
  chown -R root:root "$APP_DIR"

  echo "==> units and polkit"
  render_unit > /etc/systemd/system/scoreboard.service
  cp "$DEVICE/scoreboard-netcfg.service" /etc/systemd/system/scoreboard-netcfg.service
  install -D -m 644 "$DEVICE/polkit/10-scoreboard-network.rules" \
    /etc/polkit-1/rules.d/10-scoreboard-network.rules
  # Enabled by symlink rather than `systemctl enable`: this also runs inside a
  # pi-gen chroot, where there is no running systemd to talk to.
  mkdir -p /etc/systemd/system/multi-user.target.wants
  ln -sf /etc/systemd/system/scoreboard.service \
    /etc/systemd/system/multi-user.target.wants/scoreboard.service
  ln -sf /etc/systemd/system/scoreboard-netcfg.service \
    /etc/systemd/system/multi-user.target.wants/scoreboard-netcfg.service
  echo "Appliance installed. It starts on the next boot."
}
```

In the existing checkout `install()`, change the groups line to include `input`:

```bash
  local groups=video,render,input
  if getent group gpio >/dev/null; then groups="$groups,gpio"; fi
```

Replace the argument handling at the bottom:

```bash
for arg in "$@"; do
  case "$arg" in
    --appliance) APPLIANCE=1 ;;
    --print-unit | --preflight) ACTION="$arg" ;;
    *) echo "usage: $0 [--appliance] [--preflight | --print-unit]" >&2; exit 2 ;;
  esac
done

case "${ACTION:-}" in
  --print-unit) render_unit ;;
  --preflight) preflight && echo "preflight ok" ;;
  "")
    if [ "$APPLIANCE" -eq 1 ]; then
      [ "$(id -u)" -eq 0 ] || die "--appliance installs system-wide; run it as root"
      install_appliance
    else
      install
    fi ;;
esac
```

- [ ] **Step 6: Run the suite**

Run: `cd device && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy ../.venv/bin/pytest -q`
Expected: PASS, including the existing `pi-setup.sh` tests.

- [ ] **Step 7: Commit**

```bash
git add tools/pi-setup.sh device/scoreboard-appliance.service device/polkit/10-scoreboard-network.rules device/tests/test_pi_setup.py
git commit -m "device: an appliance layout that does not depend on who owns the Pi"
```

---

### Task 7: The hardware checklist, and the documentation that goes with it

**Files:**
- Create: `docs/hardware-checks.md`
- Modify: `README.md`
- Test: none — this task produces documentation. Its verification is that the commands in it run.

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: `docs/hardware-checks.md` with H1-H7.

Seven things in this plan cannot be proven in CI. Writing them down with pass criteria is what stops them being quietly skipped, and the file is where their results get recorded when they are run on the Pi 4.

- [ ] **Step 1: Write the checklist**

Create `docs/hardware-checks.md`:

```markdown
# Hardware checks

Seven things the test suite cannot prove. Each is run on real hardware and its
result recorded here — including failures, which are the useful ones.

Spec: `docs/superpowers/specs/2026-09-12-device-image-design.md`

| ID | Check | Pass criterion | Result |
|---|---|---|---|
| H1 | systemd hardening against kmsdrm | The panel renders with the hardened unit | not yet run |
| H2 | polkit grant | The `scoreboard` account applies a connection | not yet run |
| H3 | Real `nmcli` scan and apply | Networks list; joining one succeeds | not yet run |
| H4 | Imager customisation on a custom image | The dialog is offered and the settings take effect | not yet run |
| H5 | Image boots | Both boards boot and the panel lights up | not yet run |
| H6 | CMA on the Zero 2 W | 480×1920 renders without CMA exhaustion | not yet run |
| H7 | Keyboard under kmsdrm | A USB keyboard drives the settings screen | not yet run |

H4, H5 and H6 need an image, so they belong to B2. H1, H2, H3 and H7 can be run
as soon as this plan is installed on a Pi.

## H1 — systemd hardening against kmsdrm

    sudo systemctl restart scoreboard && journalctl -u scoreboard -f

Pass: the panel renders. Fail: it stays black, or the journal shows a
permission error opening `/dev/dri/card0` or `/dev/input/event*`.

Bisect by commenting directives out one at a time, starting with
`ProtectSystem=strict` and `DeviceAllow`. **Record every directive removed and
the symptom that justified it** — a hardened unit that does not render is worth
less than a plain one that does, but an undocumented removal is worth least of
all.

## H2 — polkit grant

    sudo -u scoreboard nmcli device wifi list
    sudo -u scoreboard nmcli device wifi connect <ssid> password <password>

Pass: both succeed. Fail: "insufficient privileges" or an authentication
prompt, which means the rule did not match — check the action id in
`journalctl -u polkit` against the three in the rules file, since they are
version-dependent.

## H3 — real nmcli scan and apply

From the panel, press `S`. Pass: the list shows real networks with plausible
signal strengths, arrow keys move the selection, and joining one connects.

Check specifically that an SSID containing a colon appears intact — that is
what `split_terse` exists for, and it is the one case a fake `nmcli` can only
approximate.

## H4 — Imager customisation on a custom image

Open Raspberry Pi Imager, choose "Use custom", select the built `.img.xz`.

Pass: the OS customisation dialog is offered, and hostname, user and Wi-Fi
take effect on first boot. Fail: no dialog — in which case document
`/boot/firmware/scoreboard-wifi.txt` as the flash-time path in the README and
say so plainly on the download page.

## H5 — image boots

Flash and boot on a Pi 4 and a Zero 2 W. Pass: both reach the "Not registered"
screen. Note the time to first pixel on the Zero — it is the number that decides
whether anything needs optimising.

## H6 — CMA on the Zero 2 W

    dmesg | grep -i cma

Pass: the panel renders at 480×1920 with no CMA allocation failures. Fail: add
`dtoverlay=vc4-kms-v3d,cma-128` under a `[pi02]` or `[all]` filter in
`config.txt` and re-check.

## H7 — keyboard under kmsdrm

Plug a USB keyboard into the panel's Pi and press `S`.

Pass: the settings screen opens and typing works. Fail: nothing happens —
confirm the service account is in the `input` group (`id scoreboard`) and that
`DeviceAllow=char-input r` is present.

This is unproven for the existing `a` and `b` keys too: every keyboard path in
this codebase has only ever run in a desktop window or under SDL's dummy
driver, never on a panel.
```

- [ ] **Step 2: Update the README**

In `README.md`, under "Setup / Device", add after the existing numbered steps:

```markdown
### Changing the Wi-Fi later

The panel's settings screen opens with `S` on a USB keyboard: pick a network,
type the password, press Enter. `R` there offers a factory reset, which clears
this panel's identity and saved networks — it does not stop the old certificate
working, so also remove the device from your account.

If you cannot get a keyboard to it, write a file called `scoreboard-wifi.txt`
on the card's boot partition — the one Windows and macOS can see — containing:

    ssid=YourNetworkName
    psk=YourWiFiPassword

and reboot. The panel applies it on every boot, then replaces the file with a
note so your password is not left sitting on a partition any computer can read.
```

Also amend the existing note about `tools/pi-setup.sh` to mention that
`--appliance` installs the system-wide layout used by the image, rather than a
checkout in your home directory.

- [ ] **Step 3: Commit**

```bash
git add docs/hardware-checks.md README.md
git commit -m "docs: the seven things only real hardware can tell us"
```
