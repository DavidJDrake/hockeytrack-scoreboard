"""tools/release-manifest.py: the manifest's shape, its expiry per channel,
and signature verification against a keyring.

The signing key here is a throwaway generated in the test process and never
written to disk as a private key: the real one lives in KMS and its private
half never exists anywhere else.
"""
import base64
import datetime as dt
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "release-manifest.py"

spec = importlib.util.spec_from_file_location("release_manifest", TOOL)
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)

PAYLOADS = {
    "layout": 1,
    "boot": {"file": "scoreboard-v0.2.0.boot.img.xz", "size": 41234567, "sha256": "a" * 64,
             "rawSize": 268435456, "rawSha256": "b" * 64},
    "root": {"file": "scoreboard-v0.2.0.root.img.xz", "size": 612345678, "sha256": "c" * 64,
             "rawSize": 3221225472, "rawSha256": "d" * 64},
}
RELEASED = dt.datetime(2026, 10, 3, 2, 11, 9, tzinfo=dt.timezone.utc)


def test_the_manifest_carries_what_the_design_names():
    m = rm.make(PAYLOADS, "v0.2.0", "stable", "release-2026-1", RELEASED)
    assert m == {
        "version": "v0.2.0", "channel": "stable",
        "released": "2026-10-03T02:11:09Z", "expires": "2027-01-31T02:11:09Z",
        "layout": 1,
        "payloads": {"boot": PAYLOADS["boot"], "root": PAYLOADS["root"]},
        "keyId": "release-2026-1",
    }


def test_a_test_channel_manifest_expires_six_hours_after_publish():
    m = rm.make(PAYLOADS, "v0.2.0", "test", "release-2026-1", RELEASED)
    assert m["channel"] == "test"
    assert m["expires"] == "2026-10-03T08:11:09Z"


@pytest.mark.parametrize("version", ["0.2.0", "v0.2", "v0.2.0-test", "v1.2.3.4", ""])
def test_a_malformed_version_is_refused(version):
    with pytest.raises(ValueError, match="vX.Y.Z"):
        rm.make(PAYLOADS, version, "stable", "k", RELEASED)


def test_an_unknown_channel_is_refused():
    with pytest.raises(ValueError, match="channel"):
        rm.make(PAYLOADS, "v0.2.0", "beta", "k", RELEASED)


def test_another_layout_generation_is_refused():
    with pytest.raises(ValueError, match="layout"):
        rm.make(dict(PAYLOADS, layout=2), "v0.2.0", "stable", "k", RELEASED)


def test_a_payload_from_another_version_is_refused():
    bad = json.loads(json.dumps(PAYLOADS))
    bad["root"]["file"] = "scoreboard-v0.1.9.root.img.xz"
    with pytest.raises(ValueError, match="not this version"):
        rm.make(bad, "v0.2.0", "stable", "k", RELEASED)


def test_a_payload_without_a_raw_hash_is_refused():
    bad = json.loads(json.dumps(PAYLOADS))
    del bad["boot"]["rawSha256"]
    with pytest.raises(ValueError, match="rawSha256"):
        rm.make(bad, "v0.2.0", "stable", "k", RELEASED)


# --- Verification against a keyring -----------------------------------------

def keyring(tmp_path: Path, *names: str):
    """Throwaway P-256 keys; only the public halves touch the disk."""
    keys = tmp_path / "keys"
    keys.mkdir()
    private = {}
    for name in names:
        key = ec.generate_private_key(ec.SECP256R1())
        private[name] = key
        (keys / f"{name}.pem").write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return keys, private


def signed(tmp_path: Path, key, key_id: str) -> tuple[bytes, bytes]:
    manifest = json.dumps(rm.make(PAYLOADS, "v0.2.0", "stable", key_id, RELEASED), indent=2).encode() + b"\n"
    return manifest, key.sign(manifest, ec.ECDSA(hashes.SHA256()))


def test_a_signature_by_a_key_in_the_ring_verifies_and_names_the_key(tmp_path):
    keys, private = keyring(tmp_path, "release-2026-1")
    manifest, sig = signed(tmp_path, private["release-2026-1"], "release-2026-1")
    assert rm.verify(manifest, sig, keys) == "release-2026-1"


def test_a_two_key_ring_accepts_either_key(tmp_path):
    keys, private = keyring(tmp_path, "release-2026-1", "release-2026-2")
    manifest, sig = signed(tmp_path, private["release-2026-2"], "release-2026-2")
    assert rm.verify(manifest, sig, keys) == "release-2026-2"


def test_a_tampered_byte_fails(tmp_path):
    keys, private = keyring(tmp_path, "release-2026-1")
    manifest, sig = signed(tmp_path, private["release-2026-1"], "release-2026-1")
    tampered = manifest.replace(b'"v0.2.0"', b'"v0.9.0"')
    with pytest.raises(ValueError, match="no key verifies"):
        rm.verify(tampered, sig, keys)


def test_a_key_not_in_the_ring_fails(tmp_path):
    keys, _ = keyring(tmp_path, "release-2026-1")
    stranger = ec.generate_private_key(ec.SECP256R1())
    manifest, sig = signed(tmp_path, stranger, "release-2026-1")
    with pytest.raises(ValueError, match="no key verifies"):
        rm.verify(manifest, sig, keys)


def test_a_key_id_that_is_not_the_verifying_key_fails(tmp_path):
    # The signature is checked before the manifest is parsed; keyId is only a
    # consistency check afterwards, and a mismatch is malformed.
    keys, private = keyring(tmp_path, "release-2026-1", "release-2026-2")
    manifest, sig = signed(tmp_path, private["release-2026-1"], "release-2026-2")
    with pytest.raises(ValueError, match="keyId"):
        rm.verify(manifest, sig, keys)


def test_an_empty_ring_fails(tmp_path):
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "README.md").write_text("no keys yet\n")
    key = ec.generate_private_key(ec.SECP256R1())
    manifest, sig = signed(tmp_path, key, "release-2026-1")
    with pytest.raises(ValueError, match="no public key"):
        rm.verify(manifest, sig, tmp_path / "keys")


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_the_workflow_s_openssl_check_agrees_with_verify(tmp_path):
    # The publish job checks out nothing and runs openssl rather than this
    # module; the command it runs must accept exactly what verify() accepts.
    keys, private = keyring(tmp_path, "release-2026-1")
    manifest, sig = signed(tmp_path, private["release-2026-1"], "release-2026-1")
    (tmp_path / "manifest.json").write_bytes(manifest)
    (tmp_path / "manifest.sig").write_bytes(sig)
    good = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(keys / "release-2026-1.pem"),
                           "-signature", str(tmp_path / "manifest.sig"), str(tmp_path / "manifest.json")],
                          capture_output=True, text=True)
    assert good.returncode == 0 and "Verified OK" in good.stdout, good.stderr
    (tmp_path / "tampered.json").write_bytes(manifest + b" ")
    bad = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(keys / "release-2026-1.pem"),
                          "-signature", str(tmp_path / "manifest.sig"), str(tmp_path / "tampered.json")],
                         capture_output=True, text=True)
    assert bad.returncode != 0


def test_the_command_line_verify_reads_a_base64_signature_as_kms_returns_it(tmp_path):
    keys, private = keyring(tmp_path, "release-2026-1")
    manifest, sig = signed(tmp_path, private["release-2026-1"], "release-2026-1")
    (tmp_path / "manifest.json").write_bytes(manifest)
    (tmp_path / "manifest.sig").write_text(base64.b64encode(sig).decode() + "\n")
    result = subprocess.run([sys.executable, str(TOOL), "verify", "--manifest", str(tmp_path / "manifest.json"),
                             "--signature", str(tmp_path / "manifest.sig"), "--keys", str(keys)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "verified by release-2026-1" in result.stdout


def test_the_command_line_make_writes_the_manifest(tmp_path):
    (tmp_path / "payloads.json").write_text(json.dumps(PAYLOADS))
    result = subprocess.run([sys.executable, str(TOOL), "make", "--payloads", str(tmp_path / "payloads.json"),
                             "--version", "v0.2.0", "--channel", "test", "--key-id", "release-2026-1",
                             "--released", "2026-10-03T02:11:09Z"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["expires"] == "2026-10-03T08:11:09Z"
