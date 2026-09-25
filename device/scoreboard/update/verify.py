"""The manifest: its signature, then its contents, in that order.

The signature is over the manifest file's exact bytes and is checked against
every public key in the keyring before the manifest is parsed, not even for
its keyId (design 6.3, 7.3 step 4): reading keyId first would mean parsing
the thing the signature is supposed to protect. Only a manifest that some
key verifies is parsed, and only then is keyId compared to the key that
verified, as a consistency check rather than a lookup.

The keyring is whatever /opt/scoreboard/certs/release-signing/ holds:
SubjectPublicKeyInfo PEM files of P-256 keys, one, or two during a rotation.
The private halves live in AWS KMS and nowhere else. This module can only
say yes or no to a signature; it cannot make one, and the tests sign with a
throwaway key generated in the test process and never written to disk.
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import CHANNELS, KEY_DIR, LAYOUT, Layout, Refused, parse_version


def load_keys(directory: Path = KEY_DIR) -> list[tuple[str, ec.EllipticCurvePublicKey]]:
    """Every P-256 public key in the directory, named by its file stem, which
    is the key id the manifest carries (release-2026-1.pem is the key
    release-2026-1). A file that is not such a key is skipped with its name
    in the journal, not treated as a key that verifies nothing: the gate
    keeps the directory equal to the repository's, so an odd file here is a
    build problem, and the refusal that follows says so louder."""
    keys = []
    try:
        entries = sorted(p for p in directory.iterdir() if p.suffix == ".pem")
    except OSError:
        return keys
    for path in entries:
        try:
            key = serialization.load_pem_public_key(path.read_bytes())
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1):
            keys.append((path.stem, key))
    return keys


def verify_signature(manifest: bytes, signature_text: bytes, keys) -> str:
    """The id of the key that verifies ``manifest``'s bytes, or a refusal.

    ``signature_text`` is the .sig file: base64 of the DER ECDSA signature
    KMS returned for ECDSA_SHA_256 over the raw message. Two refusal words,
    because the site shows them differently (design 8.1): ``key`` when the
    panel holds no key at all, which is the rotation case, and
    ``signature`` when it holds keys and none of them verifies, which is
    tampering or a wrong key.
    """
    if not keys:
        raise Refused("key", "no release public key is installed; this panel cannot verify any release")
    try:
        signature = base64.b64decode(b"".join(signature_text.split()), validate=True)
    except (binascii.Error, ValueError):
        raise Refused("signature", "the signature file is not base64")
    for key_id, key in keys:
        try:
            key.verify(signature, manifest, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            continue
        return key_id
    raise Refused("signature", f"no installed key verifies the manifest ({len(keys)} tried)")


@dataclass(frozen=True)
class Payload:
    file: str
    size: int
    sha256: str
    raw_size: int
    raw_sha256: str


@dataclass(frozen=True)
class Manifest:
    version: str
    channel: str
    released: str
    expires: int   # epoch seconds
    layout: int
    key_id: str
    boot: Payload
    root: Payload


def _payload(obj, name: str, expected_raw: int, version: str) -> Payload:
    if not isinstance(obj, dict):
        raise Refused("malformed", f"payloads.{name} is not an object")
    file, size, sha, raw_size, raw_sha = (obj.get(k) for k in ("file", "size", "sha256", "rawSize", "rawSha256"))
    if not (isinstance(file, str) and file == f"scoreboard-{version}.{name}.img.xz"):
        raise Refused("malformed", f"payloads.{name}.file is not the file for {version}")
    for label, value in (("size", size), ("rawSize", raw_size)):
        if not (isinstance(value, int) and not isinstance(value, bool) and value > 0):
            raise Refused("malformed", f"payloads.{name}.{label} is not a positive integer")
    for label, value in (("sha256", sha), ("rawSha256", raw_sha)):
        if not (isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)):
            raise Refused("malformed", f"payloads.{name}.{label} is not a lowercase sha256")
    if raw_size != expected_raw:
        raise Refused("size", f"payloads.{name}.rawSize is {raw_size}, the {name} partition is {expected_raw}")
    return Payload(file, size, sha, raw_size, raw_sha)


def _instant(text, label: str) -> int:
    from datetime import datetime, timezone
    if not isinstance(text, str) or not text.endswith("Z"):
        raise Refused("malformed", f"{label} is not a UTC timestamp")
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise Refused("malformed", f"{label} is not a UTC timestamp")
    return int(when.astimezone(timezone.utc).timestamp())


def check_manifest(manifest_bytes: bytes, *, verified_key: str, expected_version: str,
                   channel: str, now: int | None, running: str,
                   layout: Layout = Layout(), layout_generation: int = LAYOUT) -> Manifest:
    """Parse a manifest that has already been verified and apply every
    refusal of design 7.4 that can be applied before a download. ``now`` is
    None when the clock is not trusted, which refuses outright: every date
    check below would be against a made-up clock."""
    try:
        obj = json.loads(manifest_bytes)
    except ValueError as e:
        raise Refused("malformed", f"manifest is not JSON: {e}")
    if not isinstance(obj, dict):
        raise Refused("malformed", "manifest is not an object")
    version = obj.get("version")
    if version != expected_version:
        raise Refused("malformed", f"manifest is for {version!r}, latest.json named {expected_version!r}")
    if obj.get("keyId") != verified_key:
        raise Refused("key", f"manifest names key {obj.get('keyId')!r} but {verified_key!r} verified it")
    if obj.get("channel") not in CHANNELS:
        raise Refused("malformed", f"channel {obj.get('channel')!r} is not one this panel knows")
    if obj["channel"] != channel:
        raise Refused("channel", f"manifest is for the {obj['channel']} channel; this panel follows {channel}")
    if obj.get("layout") != layout_generation:
        raise Refused("layout", f"manifest is for layout {obj.get('layout')!r}; this panel has layout {layout_generation}")
    if now is None:
        raise Refused("clock", "the clock is not trusted, so the expiry cannot be judged")
    expires = _instant(obj.get("expires"), "expires")
    released = obj.get("released")
    _instant(released, "released")
    if expires <= now:
        raise Refused("expired", f"manifest expired at {obj['expires']}")
    if parse_version(version) <= parse_version(running):
        raise Refused("downgrade", f"{version} is not newer than the running {running}")
    payloads = obj.get("payloads")
    if not isinstance(payloads, dict):
        raise Refused("malformed", "payloads is not an object")
    boot = _payload(payloads.get("boot"), "boot", layout.boot_size, version)
    root = _payload(payloads.get("root"), "root", layout.root_size, version)
    return Manifest(version, obj["channel"], released, expires, layout_generation, verified_key, boot, root)


def pointer_version(pointer_bytes: bytes) -> str:
    """The only field of latest.json the panel reads. It is data: a hint
    about which manifest to fetch, trusted for nothing (design 7.5)."""
    try:
        obj = json.loads(pointer_bytes)
    except ValueError as e:
        raise Refused("malformed", f"latest.json is not JSON: {e}")
    if not isinstance(obj, dict):
        raise Refused("malformed", "latest.json is not an object")
    version = obj.get("version")
    parse_version(version)
    return version
