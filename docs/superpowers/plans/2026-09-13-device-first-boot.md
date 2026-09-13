# Device First Boot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A freshly flashed panel with no identity generates its own key, asks
the enrollment API for a pairing code, shows that code on screen until somebody
claims it, and installs the certificate it is handed — without anyone running a
provisioning script.

**Architecture:** A background thread runs a small state machine
(`Enroller.step()`) that owns exactly one network call per tick and returns a
value the render loop draws. The private key is generated once and reused
across every retry and reboot; the collection token is persisted beside it for
the same reason. On success the identity is written atomically, device.json
last, and the process exits cleanly so systemd restarts it into the normal
provisioned path.

**Tech Stack:** Python 3.11+, `cryptography` (P-256 keys and CSRs), stdlib
`urllib.request` for HTTP, pygame for the screens, pytest headless.

**Spec:** `docs/superpowers/specs/2026-09-12-device-enrollment-design.md`
(§9 is this plan; §5, §7 and §11 constrain it)

## Global Constraints

- **The CSR key must be ECDSA P-256.** `cloud/internal/enroll/csr.go` rejects
  anything else with "key must be ECDSA P-256". Nothing else will enroll.
- **CSR bodies are capped at 4096 bytes** (`MaxCSRBytes`). A P-256 CSR is a few
  hundred; do not pad it.
- **The keypair is generated once and reused across retries.** Regenerating per
  attempt orphans a certificate every time somebody claims a code the panel had
  already given up on.
- **Every file write on the device is atomic** — temp file, fsync, rename,
  fsync the directory. A power cut mid-write must never leave a half-written
  file. `Config.load` refuses to start on a corrupt `device.json` rather than
  re-enrolling, which is correct and would strand the panel.
- **`device.json` is written last**, after the certificate and CA are on disk.
  Its presence is what `Config.load` treats as "this panel is provisioned".
- **The private key file is mode 0600** and is never logged, never sent, and
  never leaves the card. Only the CSR goes over the network.
- **No secret reaches a log line.** Not the collection token, not the pairing
  code. The code is shown on the panel's own screen and nowhere else.
- **US spelling**: enroll, enrollment, enrolled. Never enrol/enrolment.
- **The enrollment API base URL is baked into the image**, overridable only by
  the `SCOREBOARD_API` environment variable for tests and desktop runs. It is
  never read from the boot partition: a panel that took its endpoint from a FAT
  file any passer-by can edit would hand its CSR and its owner's email hint to
  whoever edited it, and the image already knows where home is.
- **Do not commit `device/config/`**, any certificate, any private key. This
  repo is public.
- The device talks to `https://dk3k7p41e2.execute-api.us-east-1.amazonaws.com`
  (`terraform output api_endpoint`), and the site it names on screen is
  `https://scoreboard.davidjdrake.com` (`terraform output site_url`).

## The API this is a client for

Built by plan 1, live in `cloud/cmd/enroll/handler.go`. Exact shapes:

**`POST /api/enroll`** — anonymous. Body `{"csr": "<PEM>", "owner": "<hint>"}`;
`owner` is optional and omitted entirely when the card carried no owner line.
On success **201**:

```json
{"code": "7K4M9QX2", "display": "7K4M-9QX2", "token": "<44 chars>", "pollSeconds": 5}
```

`token` and `code` are returned exactly once and never stored server-side in
raw form. Losing the token means the row can never be collected.

**`GET /api/enroll`** — anonymous, `Authorization: Bearer <token>`.

- **202** while nobody has claimed it:
  `{"status": "waiting to be claimed", "codeExpiresAt": 1757800000}` — and
  `"code"` / `"display"` **only when the code just rotated**. A 202 without a
  code means "keep showing the one you have".
- **200** once claimed:
  `{"certificatePem": "<PEM>", "thingName": "scoreboard-...", "endpoint": "a1zdtkqjv3icja-ats.iot.us-east-1.amazonaws.com"}`.
  The row is deleted server-side as it responds, so **this response arrives
  exactly once**. Losing it means the panel must enroll again from scratch and
  the certificate it never received is orphaned.
- **404** for an unknown token, a wrong token, and an expired row alike —
  deliberately indistinguishable.

## File Structure

- **`device/scoreboard/identity.py`** (new) — key generation, CSR building,
  atomic writes, and installing a collected identity. Crypto and filesystem
  only; no network, no screens.
- **`device/scoreboard/enroll.py`** (new) — the HTTP client and the polling
  state machine. Consumes `identity.py`; knows nothing about pygame.
- **`device/certs/AmazonRootCA1.pem`** (new, committed) — the root the device
  needs to reach IoT. `tools/provision.sh` downloads it at provisioning time;
  an appliance has no provisioning step, so it ships in the image.
- **`device/scoreboard/netcfg.py`** (modify) — the boot file gains an `owner=`
  line and a new name.
- **`device/scoreboard/screens.py`** (modify) — two new screens.
- **`device/scoreboard/main.py`** (modify) — run the enroller, draw its state,
  exit when it succeeds.
- **`tools/pi-setup.sh`**, **`README.md`**, **`docs/hardware-checks.md`**
  (modify) — the dependency, the file's new name, and the check that proves
  the whole path works on real hardware.

---

### Task 1: The owner hint travels on the boot partition

The setup file is renamed from `scoreboard-wifi.txt` to `scoreboard-setup.txt`
because it now carries more than Wi-Fi, and it gains an `owner=` line. This is
what makes a code claimable by exactly one person: the panel asks for a code
*for that owner*, so someone who photographs the screen cannot claim it — they
would have to sign in to the admin site as the owner.

**Files:**
- Modify: `device/scoreboard/netcfg.py`
- Test: `device/tests/test_netcfg.py`
- Modify: `README.md:121` (the flash-time instructions)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `netcfg.BOOT_FILE` (now `scoreboard-setup.txt`),
  `netcfg.LEGACY_BOOT_FILE`, `netcfg.boot_file() -> Path`,
  `netcfg.parse_owner(text: str) -> str | None`,
  `netcfg.owner_hint(path: Path | None = None) -> str | None`.
  Task 5 calls `owner_hint()`.

- [ ] **Step 1: Write the failing tests**

Add to `device/tests/test_netcfg.py`:

```python
def test_owner_line_is_read_from_the_setup_file():
    assert netcfg.parse_owner("ssid=Home\nowner=friend@example.com\n") == "friend@example.com"


def test_owner_is_optional_and_its_absence_is_not_an_error():
    assert netcfg.parse_owner("ssid=Home\npsk=password123\n") is None
    assert netcfg.parse_owner("") is None


def test_owner_is_read_the_same_forgiving_way_as_the_wifi_lines():
    # Written in Notepad on a FAT partition: BOM, CRLF, stray spaces, any case.
    text = "\ufeffSSID=Home\r\n  Owner = Friend@Example.com  \r\n"
    assert netcfg.parse_owner(text) == "Friend@Example.com"


def test_applying_wifi_keeps_the_owner_line(tmp_path):
    # consume() wipes the password, which is the point. It must not wipe the
    # owner: the panel may not enroll until a later boot, and after a factory
    # reset this file is the only record of who the card belongs to.
    path = tmp_path / "scoreboard-setup.txt"
    path.write_text("ssid=Home\npsk=password123\nowner=friend@example.com\n")

    class FakeNM:
        def apply(self, settings): self.applied = settings

    assert netcfg.apply_boot_file(path, nm=FakeNM(), now=lambda: "2026-09-13 10:00 UTC") is True
    left = path.read_text()
    assert "password123" not in left
    assert netcfg.parse_owner(left) == "friend@example.com"


def test_the_legacy_wifi_filename_is_still_read(tmp_path):
    legacy = tmp_path / "scoreboard-wifi.txt"
    legacy.write_text("ssid=Home\n")
    chosen = netcfg.boot_file(primary=tmp_path / "scoreboard-setup.txt", legacy=legacy)
    assert chosen == legacy


def test_the_new_filename_wins_when_both_exist(tmp_path):
    primary = tmp_path / "scoreboard-setup.txt"
    primary.write_text("ssid=New\n")
    legacy = tmp_path / "scoreboard-wifi.txt"
    legacy.write_text("ssid=Old\n")
    assert netcfg.boot_file(primary=primary, legacy=legacy) == primary
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_netcfg.py -q`
Expected: FAIL — `AttributeError: module 'scoreboard.netcfg' has no attribute 'parse_owner'`.

- [ ] **Step 3: Implement**

In `device/scoreboard/netcfg.py`, replace the `BOOT_FILE` constant (line 19):

```python
BOOT_FILE = Path("/boot/firmware/scoreboard-setup.txt")
# The name this file had when it carried only Wi-Fi. Cards written before the
# rename still work: a panel that refused to read the file the user was told
# to write last month is a support call, and the file's contents are
# unambiguous either way.
LEGACY_BOOT_FILE = Path("/boot/firmware/scoreboard-wifi.txt")
```

Extract the tolerant parsing already inside `parse_wifi_file` into a helper so
the owner line is read exactly the same way, and add the two new functions:

```python
def _values(text: str) -> dict[str, str]:
    """Key/value lines from a file a person typed on a FAT partition.

    A UTF-8 BOM is stripped, CRLF is handled, surrounding whitespace is
    ignored, keys are case-insensitive, and only the first '=' separates so a
    password may contain more.
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
    return values


def parse_owner(text: str) -> str | None:
    """Who this panel belongs to, if the card says.

    Never raises. An absent or empty owner line is a normal state: the panel
    enrolls without a hint and its code is claimable by any invited user. Case
    is preserved rather than folded -- the server normalizes before hashing,
    and a mangled address here would silently produce a code its owner cannot
    claim.
    """
    return _values(text).get("owner") or None


def boot_file(primary: Path = BOOT_FILE, legacy: Path = LEGACY_BOOT_FILE) -> Path:
    """The setup file to read. The new name wins; the old one is a fallback."""
    if primary.exists():
        return primary
    if legacy.exists():
        return legacy
    return primary


def owner_hint(path: Path | None = None) -> str | None:
    """The owner line from the boot partition, or None.

    Unreadable file, unreadable bytes, no owner line -- all None. Nothing
    about enrollment should fail because of what somebody typed here.
    """
    target = boot_file() if path is None else path
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_owner(text)
```

Then make `parse_wifi_file` use the helper — replace its inline BOM/loop block
(lines 44-54) with `values = _values(text)` and leave the rest untouched.

Teach `consume` to carry the owner through:

```python
def consume(path: Path, when: str, owner: str | None = None) -> None:
    """Replace the file with a note saying it was applied.

    The password is now in NetworkManager's own store, root-owned on the
    root partition. Leaving a copy here would mean a cleartext Wi-Fi
    password living permanently on the one partition every operating system
    mounts automatically when the card is plugged in.

    The owner line is deliberately kept. It is not a secret in the way a
    password is -- it is the address of the person holding the card -- and
    the panel may not enroll until a later boot, or may be factory reset,
    at which point this file is the only record of who it belongs to.
    """
    kept = f"owner={owner}\n\n" if owner else ""
    path.write_text(
        kept +
        f"# Wi-Fi settings applied by the scoreboard on {when}.\n"
        "#\n"
        "# The network details that were here are stored on the device now, and\n"
        "# have been removed from this file, which any computer can read.\n"
        "#\n"
        "# To change networks, replace the lines below with:\n"
        "#   ssid=YourNetworkName\n"
        "#   psk=YourWiFiPassword\n"
        "# and reboot the panel. Leave the owner line alone.\n"
    )
```

And in `apply_boot_file`, read the owner before consuming and pass it — the
one-line change is `consume(path, stamp, parse_owner(text))`.

- [ ] **Step 4: Run the tests**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_netcfg.py -q`
Expected: PASS, with the existing Wi-Fi tests still green.

- [ ] **Step 5: Update the README**

In `README.md` around line 121, change the filename and add the owner line:

```markdown
If you cannot get a keyboard to it, write a file called `scoreboard-setup.txt`
on the card's boot partition (the one Windows and macOS can see):

    ssid=YourNetworkName
    psk=YourWiFiPassword
    owner=you@example.com

The panel reads it on every boot, connects, and then removes the password from
the file — it is the one partition any computer mounts automatically. The owner
line stays, and is what makes the pairing code claimable by you and nobody
else. Leave it out and any invited user can claim the panel.
```

- [ ] **Step 6: Commit**

```bash
git add device/scoreboard/netcfg.py device/tests/test_netcfg.py README.md
git commit -m "device: the setup file carries who the panel belongs to"
```

---

### Task 2: The key, the CSR, and installing an identity

**Files:**
- Create: `device/scoreboard/identity.py`
- Create: `device/certs/AmazonRootCA1.pem`
- Modify: `device/requirements.txt`
- Test: `device/tests/test_identity.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces, all used by Task 3:
  - `identity.ensure_keypair(config_dir: Path) -> ec.EllipticCurvePrivateKey`
  - `identity.build_csr(key) -> bytes` (PEM bytes)
  - `identity.write_identity(config_dir: Path, cert_pem: str, thing_name: str, endpoint: str) -> None`
  - `identity.write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None`
  - `identity.KEY_NAME`, `CERT_NAME`, `CA_NAME`, `BUNDLED_CA`

- [ ] **Step 1: Add the dependency and the root CA**

`cryptography` is needed for P-256 key generation and CSR building. On the Pi it
comes from Debian (`python3-cryptography`, installed by `tools/pi-setup.sh` in
Task 6) and is seen through the `--system-site-packages` venv, the same
arrangement pygame already uses: a distribution package gets security updates
from Debian, where a pinned wheel ages in place.

Append to `device/requirements.txt`:

```
# Enrollment generates a P-256 key and a CSR on the device. On the Pi this is
# the distribution's python3-cryptography, seen through the same
# --system-site-packages venv as pygame; the floor here is what pip installs on
# a development machine.
cryptography>=42
```

Fetch the root CA and commit it — `tools/provision.sh` downloads this at
provisioning time, and an appliance has no provisioning step. Pinning a public
root at build time also beats trusting a download on a stranger's first boot:

```bash
mkdir -p device/certs
curl -sS https://www.amazontrust.com/repository/AmazonRootCA1.pem -o device/certs/AmazonRootCA1.pem
grep -c "BEGIN CERTIFICATE" device/certs/AmazonRootCA1.pem   # expect 1
openssl x509 -in device/certs/AmazonRootCA1.pem -noout -subject
# expect: subject=C = US, O = Amazon, CN = Amazon Root CA 1
.venv/bin/pip install -r device/requirements-dev.txt
```

- [ ] **Step 2: Write the failing tests**

Create `device/tests/test_identity.py`:

```python
import json
import os
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from scoreboard import identity


def test_the_generated_key_is_p256(tmp_path):
    key = identity.ensure_keypair(tmp_path)
    assert isinstance(key.curve, ec.SECP256R1)


def test_the_key_is_generated_once_and_reused(tmp_path):
    first = identity.ensure_keypair(tmp_path)
    second = identity.ensure_keypair(tmp_path)
    assert first.private_numbers() == second.private_numbers()


def test_the_private_key_is_not_readable_by_anyone_else(tmp_path):
    identity.ensure_keypair(tmp_path)
    mode = (tmp_path / identity.KEY_NAME).stat().st_mode & 0o777
    assert mode == 0o600, f"private key is {oct(mode)}"


def test_the_csr_is_a_p256_request_that_verifies(tmp_path):
    key = identity.ensure_keypair(tmp_path)
    csr = x509.load_pem_x509_csr(identity.build_csr(key))
    assert csr.is_signature_valid
    assert isinstance(csr.public_key().curve, ec.SECP256R1)


def test_the_csr_fits_the_servers_size_cap(tmp_path):
    # cloud/internal/enroll/csr.go: MaxCSRBytes = 4096.
    assert len(identity.build_csr(identity.ensure_keypair(tmp_path))) < 4096


def test_writing_an_identity_leaves_every_file_the_config_expects(tmp_path):
    identity.ensure_keypair(tmp_path)
    identity.write_identity(tmp_path, "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n",
                            "scoreboard-abc123", "a1.iot.us-east-1.amazonaws.com")
    for name in ("device.json", identity.CERT_NAME, identity.KEY_NAME, identity.CA_NAME):
        assert (tmp_path / name).exists(), name
    d = json.loads((tmp_path / "device.json").read_text())
    assert d == {"thingName": "scoreboard-abc123", "endpoint": "a1.iot.us-east-1.amazonaws.com"}
    assert "BEGIN CERTIFICATE" in (tmp_path / identity.CA_NAME).read_text()


def test_device_json_is_written_last(tmp_path, monkeypatch):
    # Config.load treats device.json as proof the panel is provisioned. If it
    # landed before the certificate and the write then failed, the panel would
    # refuse to start AND refuse to re-enroll -- stranded, needing a reflash.
    written: list[str] = []
    real = identity.write_atomic

    def spy(path, data, mode=0o644):
        written.append(Path(path).name)
        real(path, data, mode)

    monkeypatch.setattr(identity, "write_atomic", spy)
    identity.ensure_keypair(tmp_path)
    identity.write_identity(tmp_path, "cert", "thing", "endpoint")
    assert written[-1] == "device.json", written


def test_an_atomic_write_leaves_no_temporary_file_behind(tmp_path):
    identity.write_atomic(tmp_path / "x.json", b"{}")
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_a_failed_atomic_write_does_not_damage_the_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "device.json"
    target.write_text("the good one")

    def boom(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        identity.write_atomic(target, b"the bad one")
    assert target.read_text() == "the good one"
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())
```

- [ ] **Step 3: Run them and watch them fail**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_identity.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scoreboard.identity'`.

- [ ] **Step 4: Implement**

Create `device/scoreboard/identity.py`:

```python
"""This panel's own identity: the key it generates, the request it sends, and
the certificate it is eventually handed.

The private key is created here and never leaves the card. Only the signing
request goes over the network, so nothing an eavesdropper or the server ever
sees can impersonate this panel.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .config import ROOT

KEY_NAME = "private.pem.key"
CERT_NAME = "device.pem.crt"
CA_NAME = "AmazonRootCA1.pem"
# Shipped in the image rather than downloaded on first boot: provision.sh
# fetches this at provisioning time, and an appliance has no provisioning step.
BUNDLED_CA = ROOT / "certs" / CA_NAME


def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write a file that a power cut cannot leave half-written.

    Temp file in the same directory, fsync, rename, then fsync the directory
    so the rename itself is durable. os.replace is atomic within a filesystem,
    so a reader sees either the old file or the new one, never a prefix of the
    new one. The mode is set at open() rather than afterwards, so a private key
    is never briefly world-readable.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def ensure_keypair(config_dir: Path) -> ec.EllipticCurvePrivateKey:
    """This panel's key, generated on first call and reused forever after.

    Reused deliberately: a new key per attempt would orphan a certificate every
    time somebody claimed a code the panel had already given up on, and the
    orphan would sit in IoT Core attached to a thing nobody owns.

    P-256 because that is what the server accepts; see csr.go.
    """
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    key_path = config_dir / KEY_NAME
    try:
        existing = key_path.read_bytes()
    except OSError:
        pass
    else:
        return serialization.load_pem_private_key(existing, password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    write_atomic(key_path, key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ), mode=0o600)
    return key


def build_csr(key: ec.EllipticCurvePrivateKey) -> bytes:
    """A signing request: a public key, plus proof this panel holds its pair.

    The subject is a fixed placeholder and means nothing. The server never
    reads it -- the thing name is assigned server-side precisely so nothing a
    stranger sends can name a device or collide with one that exists -- so
    putting anything identifying here would be a privacy leak for no gain.
    """
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "scoreboard")]))
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )


def write_identity(config_dir: Path, cert_pem: str, thing_name: str,
                   endpoint: str, ca_source: Path = BUNDLED_CA) -> None:
    """Install a collected certificate, in the order that cannot strand a panel.

    device.json goes last. Config.load treats its presence as proof this panel
    is provisioned, and on a corrupt or incomplete identity it refuses to start
    rather than re-enrolling -- correct, because showing the setup screen for a
    panel somebody already claimed would invite a second registration. So if
    device.json landed first and the certificate write then failed, the panel
    would neither start nor re-enroll. It would need a reflash.
    """
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    write_atomic(config_dir / CERT_NAME, cert_pem.encode("utf-8"))
    write_atomic(config_dir / CA_NAME, Path(ca_source).read_bytes())
    write_atomic(config_dir / "device.json", json.dumps(
        {"thingName": thing_name, "endpoint": endpoint}, indent=2).encode("utf-8") + b"\n")
```

- [ ] **Step 5: Run the tests**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_identity.py -q`
Expected: PASS, 9 tests.

- [ ] **Step 6: Prove the CSR is one the real server accepts**

The Python and Go halves have never met. Check the actual interface rather than
assuming it:

```bash
cd device && ../.venv/bin/python -c "
from pathlib import Path; import tempfile
from scoreboard import identity
d = Path(tempfile.mkdtemp())
Path('/tmp/csr.pem').write_bytes(identity.build_csr(identity.ensure_keypair(d)))
print('csr bytes:', len(Path('/tmp/csr.pem').read_bytes()))
"
cd ../cloud && cat > /tmp/csr_check_test.go <<'EOF'
package enroll

import (
	"os"
	"testing"
)

func TestARealDeviceCSRIsAccepted(t *testing.T) {
	pem, err := os.ReadFile("/tmp/csr.pem")
	if err != nil {
		t.Fatal(err)
	}
	if err := ParseCSR(pem); err != nil {
		t.Fatalf("the device's own CSR was rejected: %v", err)
	}
}
EOF
cp /tmp/csr_check_test.go internal/enroll/csr_check_test.go
go test ./internal/enroll/ -run TestARealDeviceCSRIsAccepted -v
rm internal/enroll/csr_check_test.go /tmp/csr_check_test.go /tmp/csr.pem
```

Expected: PASS. If it fails, the device's key type or PEM encoding is wrong and
nothing later in this plan can work. Report it rather than working around it.
This is a throwaway check, not a committed test — the committed equivalent is
`test_the_csr_is_a_p256_request_that_verifies`.

- [ ] **Step 7: Commit**

```bash
git add device/scoreboard/identity.py device/tests/test_identity.py \
        device/certs/AmazonRootCA1.pem device/requirements.txt
git commit -m "device: generate a key that never leaves the card"
```

---

### Task 3: The enrollment client

**Files:**
- Create: `device/scoreboard/enroll.py`
- Test: `device/tests/test_enroll.py`

**Interfaces:**
- Consumes: `identity.ensure_keypair`, `identity.build_csr`,
  `identity.write_identity`, `identity.write_atomic` (Task 2).
- Produces, used by Tasks 4 and 5:
  - `enroll.Waiting(display: str, expires_at: int, owner: str | None)`
  - `enroll.Problem(detail: str)`
  - `enroll.Ready(thing_name: str)`
  - `enroll.Enroller(config_dir, owner=None, api_base=API_BASE, transport=None)`
    with `.step() -> Waiting | Problem | Ready` and `.delay` (seconds to wait
    before the next `step()`)
  - `enroll.API_BASE`, `enroll.BACKOFF`

- [ ] **Step 1: Write the failing tests**

Create `device/tests/test_enroll.py`:

```python
import json

import pytest

from scoreboard import enroll


class FakeAPI:
    """Stands in for the network. Records what was sent, replies from a script."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent: list[tuple[str, str, dict, dict]] = []

    def __call__(self, method, url, body, headers):
        self.sent.append((method, url, json.loads(body) if body else None, headers))
        if not self.replies:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


CREATED = (201, {"code": "7K4M9QX2", "display": "7K4M-9QX2", "token": "tok", "pollSeconds": 5})
WAITING = (202, {"status": "waiting to be claimed", "codeExpiresAt": 1757800000})
ROTATED = (202, {"status": "waiting to be claimed", "codeExpiresAt": 1757800900,
                 "code": "P9RT2WXY", "display": "P9RT-2WXY"})
CLAIMED = (200, {"certificatePem": "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n",
                 "thingName": "scoreboard-abc123", "endpoint": "a1.iot.us-east-1.amazonaws.com"})


def test_the_first_step_submits_a_csr_and_shows_the_code(tmp_path):
    api = FakeAPI(CREATED)
    e = enroll.Enroller(tmp_path, owner="friend@example.com", transport=api)
    state = e.step()
    assert isinstance(state, enroll.Waiting)
    assert state.display == "7K4M-9QX2"
    assert state.owner == "friend@example.com"
    method, url, body, _ = api.sent[0]
    assert method == "POST"
    assert url.endswith("/api/enroll")
    assert "BEGIN CERTIFICATE REQUEST" in body["csr"]
    assert body["owner"] == "friend@example.com"


def test_a_panel_with_no_owner_line_sends_no_owner(tmp_path):
    api = FakeAPI(CREATED)
    enroll.Enroller(tmp_path, owner=None, transport=api).step()
    assert "owner" not in api.sent[0][2]


def test_polling_carries_the_token_as_a_bearer_and_keeps_the_code(tmp_path):
    api = FakeAPI(CREATED, WAITING)
    e = enroll.Enroller(tmp_path, transport=api)
    e.step()
    state = e.step()
    # A 202 with no code means "keep showing the one you have".
    assert isinstance(state, enroll.Waiting)
    assert state.display == "7K4M-9QX2"
    assert api.sent[1][3]["Authorization"] == "Bearer tok"
    assert api.sent[1][0] == "GET"


def test_a_rotated_code_replaces_the_one_on_screen(tmp_path):
    api = FakeAPI(CREATED, ROTATED)
    e = enroll.Enroller(tmp_path, transport=api)
    e.step()
    assert e.step().display == "P9RT-2WXY"


def test_a_claimed_enrollment_installs_the_identity(tmp_path):
    api = FakeAPI(CREATED, CLAIMED)
    e = enroll.Enroller(tmp_path, transport=api)
    e.step()
    state = e.step()
    assert isinstance(state, enroll.Ready)
    assert state.thing_name == "scoreboard-abc123"
    assert json.loads((tmp_path / "device.json").read_text())["thingName"] == "scoreboard-abc123"
    assert (tmp_path / "device.pem.crt").exists()


def test_the_key_survives_a_reboot_mid_enrollment(tmp_path):
    api = FakeAPI(CREATED)
    first = enroll.Enroller(tmp_path, transport=api)
    first.step()
    key_before = (tmp_path / "private.pem.key").read_bytes()
    # New process, same card.
    enroll.Enroller(tmp_path, transport=FakeAPI(WAITING)).step()
    assert (tmp_path / "private.pem.key").read_bytes() == key_before


def test_the_token_survives_a_reboot_so_a_claimed_code_is_not_orphaned(tmp_path):
    # Losing the token means the row can never be collected: somebody claims
    # the code, a certificate is minted, and the panel that asked for it can
    # never pick it up. The server would have no way to know.
    enroll.Enroller(tmp_path, transport=FakeAPI(CREATED)).step()
    api = FakeAPI(WAITING)
    resumed = enroll.Enroller(tmp_path, transport=api)
    state = resumed.step()
    assert isinstance(state, enroll.Waiting)
    assert api.sent[0][0] == "GET", "a resumed panel must poll, not submit again"
    assert api.sent[0][3]["Authorization"] == "Bearer tok"


def test_a_404_starts_over_rather_than_polling_a_dead_row_forever(tmp_path):
    enroll.Enroller(tmp_path, transport=FakeAPI(CREATED)).step()
    api = FakeAPI((404, {"error": "no such enrollment"}), CREATED)
    resumed = enroll.Enroller(tmp_path, transport=api)
    state = resumed.step()
    assert isinstance(state, enroll.Waiting)
    assert [s[0] for s in api.sent] == ["GET", "POST"]


def test_a_network_failure_is_a_problem_the_screen_can_explain(tmp_path):
    api = FakeAPI(OSError("Name or service not known"))
    state = enroll.Enroller(tmp_path, transport=api).step()
    assert isinstance(state, enroll.Problem)
    assert state.detail


def test_a_server_error_is_a_problem_and_never_a_traceback(tmp_path):
    state = enroll.Enroller(tmp_path, transport=FakeAPI((500, {"error": "enrollment failed"}))).step()
    assert isinstance(state, enroll.Problem)


def test_the_backoff_settles_at_thirty_seconds(tmp_path):
    # The person on the other end is signing into a website and hunting for a
    # password. A panel that hammers the endpoint trips its own rate limit.
    api = FakeAPI(CREATED, WAITING, WAITING, WAITING, WAITING, WAITING, WAITING)
    e = enroll.Enroller(tmp_path, transport=api)
    delays = []
    for _ in range(7):
        e.step()
        delays.append(e.delay)
    assert delays[-1] == 30
    assert max(delays) == 30
    assert delays == sorted(delays), f"backoff must not go backwards: {delays}"


def test_a_problem_backs_off_too(tmp_path):
    e = enroll.Enroller(tmp_path, transport=FakeAPI(OSError("down"), OSError("down")))
    e.step()
    first = e.delay
    e.step()
    assert e.delay >= first


def test_no_secret_is_ever_logged(tmp_path, caplog):
    caplog.set_level("DEBUG")
    api = FakeAPI(CREATED, WAITING, CLAIMED)
    e = enroll.Enroller(tmp_path, transport=api)
    e.step(); e.step(); e.step()
    text = caplog.text
    assert "tok" not in text
    assert "7K4M9QX2" not in text and "7K4M-9QX2" not in text
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_enroll.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scoreboard.enroll'`.

- [ ] **Step 3: Implement**

Create `device/scoreboard/enroll.py`:

```python
"""Asking for an identity, and waiting to be claimed.

One network call per step(), so the caller decides the pace and the render loop
never blocks. The private key is made once by identity.py and reused; the
collection token is persisted beside it, because losing the token means a
certificate somebody claimed can never be collected by the panel that asked
for it.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import identity

log = logging.getLogger("scoreboard.enroll")

# Baked into the image. Deliberately NOT read from the boot partition: a panel
# that took its endpoint from a FAT file any passer-by can edit would hand its
# CSR and its owner's email hint to whoever edited it, and the image already
# knows where home is. The environment override exists for tests and desktop
# runs, where nothing is at stake.
API_BASE = os.environ.get(
    "SCOREBOARD_API", "https://dk3k7p41e2.execute-api.us-east-1.amazonaws.com")
SITE = "scoreboard.davidjdrake.com"
STATE_NAME = "enrollment.json"
HTTP_TIMEOUT_S = 15
# Quick at first, because most claims happen while the owner is standing there
# with the panel in front of them; then slow, because the rest are somebody
# hunting for a password.
BACKOFF = (5, 5, 10, 15, 30)


@dataclass(frozen=True)
class Waiting:
    """A code is on screen and nobody has claimed it yet."""
    display: str
    expires_at: int
    owner: str | None = None


@dataclass(frozen=True)
class Problem:
    """Enrollment is failing. Said plainly, and never the same as 'waiting'."""
    detail: str


@dataclass(frozen=True)
class Ready:
    """The certificate is on the card."""
    thing_name: str


def _transport(method: str, url: str, body: bytes | None, headers: dict[str, str]):
    """One HTTPS request. Returns (status, payload dict)."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S, context=ctx) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        # A 404 and a 500 are answers, not failures; the caller decides.
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


class Enroller:
    """The first-boot state machine. Call step(), draw what it returns, sleep
    for .delay, repeat."""

    def __init__(self, config_dir: Path, owner: str | None = None,
                 api_base: str = API_BASE, transport=None):
        self.config_dir = Path(config_dir)
        self.owner = owner
        self.api_base = api_base.rstrip("/")
        self.transport = transport or _transport
        self.delay = BACKOFF[0]
        self._polls = 0
        self._state = self._load_state()

    # --- persisted enrollment state -------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads((self.config_dir / STATE_NAME).read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        self._state = state
        identity.write_atomic(self.config_dir / STATE_NAME,
                              json.dumps(state).encode("utf-8"), mode=0o600)

    def _forget(self) -> None:
        self._state = {}
        (self.config_dir / STATE_NAME).unlink(missing_ok=True)
        self._polls = 0

    # --- the machine ----------------------------------------------------

    def step(self) -> Waiting | Problem | Ready:
        try:
            state = self._poll() if self._state.get("token") else self._submit()
        except OSError as e:
            # Includes every socket and DNS failure urllib raises. The message
            # is the operating system's, which is safe to show: it says "Name
            # or service not known", not anything about this panel.
            log.info("enrollment unreachable: %s", e)
            self._slow_down()
            return Problem(str(e) or "cannot reach the enrollment service")
        return state

    def _submit(self) -> Waiting | Problem:
        key = identity.ensure_keypair(self.config_dir)
        payload: dict[str, str] = {"csr": identity.build_csr(key).decode("ascii")}
        if self.owner:
            payload["owner"] = self.owner
        status, out = self.transport(
            "POST", f"{self.api_base}/api/enroll",
            json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})
        if status != 201:
            log.warning("enrollment request refused: HTTP %d", status)
            self._slow_down()
            return Problem(f"the enrollment service refused this panel (HTTP {status})")
        self._save_state({"token": out["token"], "display": out["display"],
                          "expiresAt": out.get("codeExpiresAt", 0)})
        self._polls = 0
        self.delay = int(out.get("pollSeconds", BACKOFF[0]))
        return Waiting(out["display"], self._state["expiresAt"], self.owner)

    def _poll(self) -> Waiting | Problem | Ready:
        status, out = self.transport(
            "GET", f"{self.api_base}/api/enroll", None,
            {"Authorization": f"Bearer {self._state['token']}"})
        if status == 404:
            # The row expired, or was collected by something else. Polling it
            # forever would leave a panel showing a code nobody can claim.
            log.info("this enrollment is gone; starting a new one")
            self._forget()
            return self._submit()
        if status == 200:
            identity.write_identity(self.config_dir, out["certificatePem"],
                                    out["thingName"], out["endpoint"])
            self._forget()
            log.info("claimed as %s", out["thingName"])
            return Ready(out["thingName"])
        if status != 202:
            log.warning("unexpected reply while waiting: HTTP %d", status)
            self._slow_down()
            return Problem(f"the enrollment service is failing (HTTP {status})")
        if out.get("display"):
            # The code rotated. Everything after the quarter hour is a fresh
            # code, which is what makes one photographed off a screen useless.
            self._save_state({**self._state, "display": out["display"],
                              "expiresAt": out.get("codeExpiresAt", 0)})
        self._polls += 1
        self.delay = BACKOFF[min(self._polls, len(BACKOFF) - 1)]
        return Waiting(self._state["display"],
                       out.get("codeExpiresAt", self._state.get("expiresAt", 0)), self.owner)

    def _slow_down(self) -> None:
        self._polls += 1
        self.delay = BACKOFF[min(self._polls, len(BACKOFF) - 1)]
```

- [ ] **Step 4: Run the tests**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_enroll.py -q`
Expected: PASS, 12 tests.

- [ ] **Step 5: Prove two of these tests are real**

A test that passes against a broken implementation is worse than no test.
Mutate, confirm red, restore, confirm green:

```bash
cd device
cp scoreboard/enroll.py /tmp/enroll.bak
# 1. Make a resumed panel submit again instead of polling.
sed -i 's/if self._state.get("token") else self._submit()/if False else self._submit()/' scoreboard/enroll.py
SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_enroll.py -q 2>&1 | tail -3
# expect: test_the_token_survives_a_reboot... FAILS
cp /tmp/enroll.bak scoreboard/enroll.py
# 2. Make the backoff constant.
sed -i 's/BACKOFF = (5, 5, 10, 15, 30)/BACKOFF = (5, 5, 5, 5, 5)/' scoreboard/enroll.py
SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_enroll.py -q 2>&1 | tail -3
# expect: test_the_backoff_settles_at_thirty_seconds FAILS
cp /tmp/enroll.bak scoreboard/enroll.py && rm /tmp/enroll.bak
git diff --stat scoreboard/enroll.py   # expect: no output
```

Report the result in your task report. If either mutation leaves the suite
green, the test is not testing what it claims — fix the test, not the output.

- [ ] **Step 6: Commit**

```bash
git add device/scoreboard/enroll.py device/tests/test_enroll.py
git commit -m "device: ask for an identity and wait to be claimed"
```

---

### Task 4: The screens a stranger reads

Three states get screens rather than silence, because the alternative is a
friend staring at a dead panel with no way to tell whose fault it is. "No
network" already exists. These are the other two, and the important property is
that **"waiting to be claimed" and "enrollment is broken" must not look
alike** — one means "go and type this code", the other means "something is
wrong at our end".

**Files:**
- Modify: `device/scoreboard/screens.py`
- Test: `device/tests/test_screens.py`

**Interfaces:**
- Consumes: `enroll.Waiting`, `enroll.Problem` (Task 3).
- Produces, used by Task 5: `screens.WAITING`, `screens.ENROLL_PROBLEM`,
  `screens.screen_for(has_identity, has_network, enrollment=None) -> str`,
  `screens.draw_waiting(surface, assets, code, site, owner, build)`,
  `screens.draw_enroll_problem(surface, assets, detail, build)`.

- [ ] **Step 1: Write the failing tests**

Add to `device/tests/test_screens.py`:

```python
def test_a_panel_with_a_code_shows_the_code_screen():
    from scoreboard import enroll
    state = enroll.Waiting("7K4M-9QX2", 1757800000, "friend@example.com")
    assert screens.screen_for(False, True, state) == screens.WAITING


def test_a_failing_enrollment_does_not_look_like_waiting():
    from scoreboard import enroll
    assert screens.screen_for(False, True, enroll.Problem("down")) == screens.ENROLL_PROBLEM


def test_no_network_still_wins_over_enrollment():
    # A panel that cannot reach Wi-Fi must say so, not show a stale code.
    from scoreboard import enroll
    state = enroll.Waiting("7K4M-9QX2", 1757800000, None)
    assert screens.screen_for(False, False, state) == screens.OFFLINE


def test_a_panel_that_has_not_asked_yet_shows_the_old_unregistered_screen():
    assert screens.screen_for(False, True, None) == screens.UNREGISTERED


def test_an_identity_beats_everything():
    from scoreboard import enroll
    state = enroll.Waiting("7K4M-9QX2", 1757800000, None)
    assert screens.screen_for(True, True, state) == screens.SCOREBOARD


def _painted(draw_call) -> bool:
    """Did anything actually reach the panel? Byte-for-byte against a blank
    fill, the way test_screens_paint_something already does it -- an average
    would round a mostly-dark screen with a little text on it back to BG."""
    pygame.init()
    surface = pygame.Surface((W, H))
    draw_call(surface)
    blank = pygame.Surface((W, H))
    blank.fill(BG)
    return pygame.image.tostring(surface, "RGB") != pygame.image.tostring(blank, "RGB")


def test_the_code_screen_draws_the_code_and_the_owner():
    assert _painted(lambda s: screens.draw_waiting(
        s, Assets(), "7K4M-9QX2", "scoreboard.example.com", "friend@example.com", "test build"))


def test_the_code_screen_works_without_an_owner():
    assert _painted(lambda s: screens.draw_waiting(
        s, Assets(), "7K4M-9QX2", "scoreboard.example.com", None, "test build"))


def test_the_problem_screen_draws():
    assert _painted(lambda s: screens.draw_enroll_problem(
        s, Assets(), "cannot reach the service", "test build"))
```

`pygame`, `Assets`, `W`, `H` and `BG` are already imported at the top of
`device/tests/test_screens.py`. Do not add a second import block.

- [ ] **Step 2: Run them and watch them fail**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_screens.py -q`
Expected: FAIL — `AttributeError: module 'scoreboard.screens' has no attribute 'WAITING'`.

- [ ] **Step 3: Implement**

In `device/scoreboard/screens.py`, extend the constants line:

```python
SCOREBOARD, UNREGISTERED, OFFLINE = "scoreboard", "unregistered", "offline"
WAITING, ENROLL_PROBLEM = "waiting", "enroll-problem"
```

Replace `screen_for` with:

```python
def screen_for(has_identity: bool, has_network: bool, enrollment=None) -> str:
    """Which panel is showing.

    Identity first: a panel nobody has registered has nothing to say about
    hockey even with perfect Wi-Fi. That ordering predates enrollment and the
    four existing cases keep it exactly -- do not "tidy" them, there is a
    parametrized test on all four.

    The network only outranks enrollment once there IS an enrollment to show,
    because a code the panel cannot refresh is worse than useless: it may have
    rotated already, and "no network" is the thing the person standing there
    can actually fix.
    """
    if has_identity:
        return SCOREBOARD if has_network else OFFLINE
    if enrollment is None:
        return UNREGISTERED
    if not has_network:
        return OFFLINE
    return WAITING if getattr(enrollment, "display", None) else ENROLL_PROBLEM
```

Add the two drawing functions after `draw_offline`:

```python
def draw_waiting(surface: pygame.Surface, assets: Assets, code: str, site: str,
                 owner: str | None, build: str) -> None:
    """The pairing code, big enough to read across a room and type on a phone.

    The owner line is here because it is otherwise invisible until something
    goes wrong: "Waiting for friend@example.com" tells whoever is standing
    there that the setup file was read and who the panel expects to claim it.
    """
    surface.fill(BG)
    y = H // 2 - 170
    heading = assets.font(44, False).render("Add this panel at", True, MUTED)
    surface.blit(heading, heading.get_rect(midtop=(W // 2, y)))
    y += 56
    where = assets.font(56, True).render(site, True, INK)
    surface.blit(where, where.get_rect(midtop=(W // 2, y)))
    y += 92
    shown = assets.font(140, True).render(code, True, INK)
    surface.blit(shown, shown.get_rect(midtop=(W // 2, y)))
    y += 168
    for line in ([f"Waiting for {owner}"] if owner else ["Waiting to be claimed"]) + [build]:
        img = assets.font(38, False).render(line, True, MUTED)
        surface.blit(img, img.get_rect(midtop=(W // 2, y)))
        y += 48


def draw_enroll_problem(surface: pygame.Surface, assets: Assets, detail: str,
                        build: str) -> None:
    """Enrollment is failing, said plainly.

    Deliberately unlike draw_waiting: somebody looking at this panel must be
    able to tell "go and type this code" from "this is broken at our end"
    without knowing anything about how either works.
    """
    draw_message(surface, assets, "Cannot register", [
        "This panel could not reach the scoreboard service.",
        detail,
        "It will keep trying. Press S for network settings.",
        build,
    ])
```

- [ ] **Step 4: Run the tests**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_screens.py -q`
Expected: PASS, with the existing screen tests still green.

- [ ] **Step 5: Look at them**

These are the only two screens a stranger sees, and a layout bug in them is a
support call nobody can diagnose remotely. Render both to PNGs and look:

```bash
cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/python -c "
import pygame
from scoreboard import screens
from scoreboard.assets import Assets
from scoreboard.render import W, H
pygame.init()
s = pygame.Surface((W, H)); a = Assets()
screens.draw_waiting(s, a, '7K4M-9QX2', 'scoreboard.davidjdrake.com', 'friend@example.com', 'dev build')
pygame.image.save(s, '/tmp/waiting.png')
screens.draw_enroll_problem(s, a, 'cannot reach the enrollment service', 'dev build')
pygame.image.save(s, '/tmp/problem.png')
print('wrote /tmp/waiting.png /tmp/problem.png')
"
```

Check that nothing overlaps or runs off the edge, and that the code is the
largest thing on the panel. If `Assets()` needs arguments, match how
`device/scoreboard/main.py` constructs it. Note what you saw in your report.

- [ ] **Step 6: Commit**

```bash
git add device/scoreboard/screens.py device/tests/test_screens.py
git commit -m "device: show the pairing code, and say so when enrollment fails"
```

---

### Task 5: Run it on first boot

**Files:**
- Modify: `device/scoreboard/main.py`
- Test: `device/tests/test_main.py`

**Interfaces:**
- Consumes: `enroll.Enroller`, `enroll.Waiting`, `enroll.Problem`,
  `enroll.Ready`, `enroll.SITE` (Task 3); `netcfg.owner_hint` (Task 1);
  `screens.screen_for`, `screens.draw_waiting`, `screens.draw_enroll_problem`
  (Task 4).
- Produces: `main.enrollment_thread(config_dir, owner, events, stop) -> threading.Thread`.

- [ ] **Step 1: Write the failing test**

Add to `device/tests/test_main.py`:

```python
def test_the_enrollment_thread_reports_each_state_and_stops_when_ready(tmp_path):
    import queue
    import threading
    from scoreboard import enroll, main as m

    class Scripted:
        def __init__(self):
            self.delay = 0
            self._steps = [enroll.Waiting("7K4M-9QX2", 0, None),
                           enroll.Problem("down"),
                           enroll.Ready("scoreboard-abc123")]

        def step(self):
            return self._steps.pop(0)

    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = m.enrollment_thread(tmp_path, None, events, stop, enroller=Scripted())
    t.join(timeout=5)
    assert not t.is_alive(), "the thread must stop once the panel is claimed"
    seen = []
    while not events.empty():
        seen.append(events.get())
    assert [kind for kind, _ in seen] == ["enroll", "enroll", "enroll"]
    assert isinstance(seen[-1][1], enroll.Ready)


def test_the_enrollment_thread_stops_when_asked(tmp_path):
    import queue
    import threading
    from scoreboard import enroll, main as m

    class Forever:
        delay = 0

        def step(self):
            return enroll.Waiting("7K4M-9QX2", 0, None)

    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = m.enrollment_thread(tmp_path, None, events, stop, enroller=Forever())
    events.get(timeout=5)
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_main.py -q`
Expected: FAIL — `AttributeError: module 'scoreboard.main' has no attribute 'enrollment_thread'`.

- [ ] **Step 3: Implement the thread**

Add to `device/scoreboard/main.py`, near `carry_out`:

```python
def enrollment_thread(config_dir, owner, events: queue.Queue,
                      stop: threading.Event, enroller=None) -> threading.Thread:
    """Run the enrollment state machine off the render loop.

    One network call per step, with the machine's own backoff between them, so
    a panel waiting for somebody to find their password is not hammering the
    endpoint -- and the display keeps redrawing throughout, because nothing
    here blocks the loop.
    """
    machine = enroller if enroller is not None else enroll.Enroller(config_dir, owner=owner)

    def run() -> None:
        while not stop.is_set():
            state = machine.step()
            events.put(("enroll", state))
            if isinstance(state, enroll.Ready):
                return
            if stop.wait(machine.delay):
                return

    t = threading.Thread(target=run, name="enrollment", daemon=True)
    t.start()
    return t
```

Three imports are missing from `device/scoreboard/main.py` and must be added.
It currently has `import queue` but not `threading`, and it imports *names*
from netcfg (`from .netcfg import NetworkError, NetworkManager, Status`) rather
than the module:

```python
import threading                      # alongside the existing `import queue`
from . import enroll                  # alongside `from . import screens`
from .netcfg import NetworkError, NetworkManager, Status, owner_hint
```

Then call it as `owner_hint()`, not `netcfg.owner_hint()`.

- [ ] **Step 4: Run the test**

Run: `cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/pytest tests/test_main.py -q`
Expected: PASS.

- [ ] **Step 5: Wire it into the loop**

In `main()`, after `cfg` is resolved and `events` is created, start the thread
when this panel has no identity:

```python
    enroll_state = None
    enroll_stop = threading.Event()
    if cfg is None and not fixture:
        # The owner line rides on the boot partition beside the Wi-Fi settings,
        # so the code this panel asks for is claimable by that person alone.
        owner = owner_hint()
        log.info("no identity yet; enrolling%s", " for a named owner" if owner else "")
        enrollment_thread(default_config_dir(), owner, events, enroll_stop)
```

In the event-draining section where `("state", ...)`, `("today", ...)` and
`("link", ...)` are handled, add:

```python
            elif kind == "enroll":
                enroll_state = payload
                if isinstance(enroll_state, enroll.Ready):
                    # Config, Link and the display were all built at startup
                    # from an identity that did not exist then. Restarting is
                    # how they pick it up: systemd's Restart=always brings the
                    # panel straight back, now provisioned. Cheaper and far
                    # less error-prone than rebuilding half of main() in place.
                    log.info("registered; restarting into the scoreboard")
                    enroll_stop.set()
                    pygame.quit()
                    sys.exit(0)
```

Replace the screen-choosing block (around `main.py:285`) with:

```python
                showing = screens.screen_for(cfg is not None or bool(fixture),
                                             net_ok or bool(fixture), enroll_state)
                if showing == screens.WAITING:
                    screens.draw_waiting(frame, assets, enroll_state.display,
                                         enroll.SITE, enroll_state.owner, build)
                elif showing == screens.ENROLL_PROBLEM:
                    screens.draw_enroll_problem(frame, assets, enroll_state.detail, build)
                elif showing == screens.UNREGISTERED:
                    screens.draw_unregistered(frame, assets, build)
                elif showing == screens.OFFLINE:
                    screens.draw_offline(frame, assets, build)
                elif should_blank(time.time(), last_update, current, BLANK_AFTER_S):
                    frame.fill((0, 0, 0))
                else:
                    draw(frame, current, now_ms, assets, link_ok)
```

- [ ] **Step 6: Run the whole device suite**

Run: `make test-py`
Expected: every test passes, including the 173 that existed before this plan.

- [ ] **Step 7: See it end to end against a fake server**

No AWS, no hardware — a local HTTP server playing the API, and the real loop
drawing the real screens:

```bash
cd device && SDL_VIDEODRIVER=dummy ../.venv/bin/python - <<'EOF'
import http.server, json, threading, tempfile, time
from pathlib import Path

claimed = {"yet": False}

class H(http.server.BaseHTTPRequestHandler):
    def _json(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self._json(201, {"code": "7K4M9QX2", "display": "7K4M-9QX2", "token": "t", "pollSeconds": 1})
    def do_GET(self):
        if not claimed["yet"]:
            claimed["yet"] = True
            return self._json(202, {"status": "waiting to be claimed", "codeExpiresAt": 0})
        self._json(200, {"certificatePem": "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n",
                         "thingName": "scoreboard-fake", "endpoint": "fake.iot.amazonaws.com"})
    def log_message(self, *a): pass

srv = http.server.HTTPServer(("127.0.0.1", 8799), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

import os
os.environ["SCOREBOARD_API"] = "http://127.0.0.1:8799"
import importlib
from scoreboard import enroll
importlib.reload(enroll)
d = Path(tempfile.mkdtemp())
e = enroll.Enroller(d, owner="friend@example.com")
for _ in range(3):
    print(e.step())
    time.sleep(0.1)
print("files:", sorted(p.name for p in d.iterdir()))
EOF
```

Expected: `Waiting(...)`, `Waiting(...)`, `Ready(thing_name='scoreboard-fake')`,
then a directory holding `device.json`, `device.pem.crt`, `private.pem.key` and
`AmazonRootCA1.pem` — and **no `enrollment.json`**, which is deleted on
success. Note the output in your report.

- [ ] **Step 8: Commit**

```bash
git add device/scoreboard/main.py device/tests/test_main.py
git commit -m "device: enroll on first boot and restart into the scoreboard"
```

---

### Task 6: The installer, the docs, and the check that proves it

**Files:**
- Modify: `tools/pi-setup.sh:65`, `tools/pi-setup.sh:102`
- Modify: `device/tests/test_pi_setup.py`
- Modify: `docs/hardware-checks.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing code depends on.

- [ ] **Step 1: Install the dependency on the Pi**

`cryptography` must come from Debian for the same reason pygame does: the
distribution patches it, and the venv is built with `--system-site-packages`.
In `tools/pi-setup.sh`, add `python3-cryptography` to both apt lines:

```bash
  sudo apt-get install -y python3-pygame python3-gpiozero python3-venv python3-cryptography
```

```bash
  apt-get install -y python3-pygame python3-gpiozero python3-venv network-manager polkitd python3-cryptography
```

- [ ] **Step 2: Ship the root CA with the appliance**

Find the block in `tools/pi-setup.sh` near line 47 that copies
`device.json device.pem.crt private.pem.key AmazonRootCA1.pem` into the
appliance's config directory. That loop is for a checkout that was already
provisioned by hand. An enrolling appliance has none of those files yet, and
`identity.write_identity` reads the CA from `device/certs/`, which travels with
the code — so confirm the installer copies the `device/` tree wholesale (it
installs to `/opt/scoreboard`) and that `device/certs/AmazonRootCA1.pem` is
therefore present at `/opt/scoreboard/certs/AmazonRootCA1.pem`. If the
installer copies a file list rather than the tree, add `certs/` to it. Record
in your report which of the two it was.

- [ ] **Step 3: Add the installer test**

In `device/tests/test_pi_setup.py`, add:

```python
def test_the_installer_installs_the_crypto_package():
    text = (ROOT / "tools" / "pi-setup.sh").read_text()
    # Enrollment generates a P-256 key on the device; the PyPI wheel is not
    # what this venv sees, the distribution package is.
    assert text.count("python3-cryptography") == 2, \
        "both the checkout and the appliance apt lines need python3-cryptography"


def test_the_root_ca_travels_with_the_code():
    ca = ROOT / "device" / "certs" / "AmazonRootCA1.pem"
    assert ca.exists(), "an enrolling appliance has no provisioning step to download this"
    assert "BEGIN CERTIFICATE" in ca.read_text()
```

Match `ROOT` to however `test_pi_setup.py` already locates the repo root.

- [ ] **Step 4: Run the tests**

Run: `make test-py`
Expected: PASS.

- [ ] **Step 5: Write the hardware check**

Add to `docs/hardware-checks.md`, following the existing H1–H7 format:

```markdown
## H8 — A panel enrolls itself

The check this whole plan exists for, and the one no CI can do: the Python and
Go halves have never spoken over a real network, and §11 of the enrollment spec
lists exactly this as untestable in CI.

1. Flash a card. Write `scoreboard-setup.txt` on the boot partition with
   `ssid=`, `psk=` and `owner=` set to the email address of a user who exists
   in the Cognito pool.
2. Boot with the panel connected. Within about a minute it should show
   **Add this panel at scoreboard.davidjdrake.com**, a code in the form
   `XXXX-XXXX`, and `Waiting for <that address>`.
3. Leave it for twenty minutes without claiming it. The code must change. The
   old one must then be refused.
4. Sign in to the admin site **as a different invited user** and try the code.
   Expect a refusal that does not reveal whether the code was real.
5. Sign in as the owner and claim it. Within about thirty seconds the panel
   should restart itself and come up on the scoreboard.
6. Check `/var/lib/scoreboard`: `device.json`, `device.pem.crt`,
   `private.pem.key` (mode 0600) and `AmazonRootCA1.pem` present, and
   **`enrollment.json` gone**.
7. Confirm in the AWS console that the thing exists, has one certificate, and
   that the certificate has the `scoreboard-device` policy attached.

**The one to watch:** step 5 is the first time `Dynamo.ByCodeHash` runs against
real DynamoDB. It has no test coverage and neither alarm would catch an
inverted comparison there — a 404 does not trip the 5xx alarm, and the 4xx
alarm needs twenty in five minutes, which a three-panel fleet will never
reach. If the claim 404s with everything else correct, suspect that line first.
```

- [ ] **Step 6: Commit**

```bash
git add tools/pi-setup.sh device/tests/test_pi_setup.py docs/hardware-checks.md
git commit -m "device: install the crypto the appliance enrolls with"
```

---

## Notes for whoever executes this

**What is deliberately not here.** The site half — signing in, typing the code,
producing the setup file — is plan 3. Until it exists, H8 step 4 cannot be done
through a browser; a claim can be driven with an access token from the Cognito
hosted UI and `curl` instead. Do not build any part of the site here.

**A ticket this plan generates, rather than fixes.** `API_BASE` bakes an
`execute-api` hostname into an image that will be published for download. That
ID is stable for the life of the API, but if the API is ever recreated, every
flashed card in the world stops enrolling. A custom domain for the API
(`api.hockeyscoreboard.davidjdrake.com`) fixes it properly and belongs with the
image build, sub-project B2 — file it before the first image is published, not
after.
