#!/usr/bin/env python3
"""The signed per-version release manifest: build it, and check a signature.

    release-manifest.py make --payloads <payloads.json> --version <vX.Y.Z> \\
        --channel stable|test --key-id <id> [--released <RFC 3339>] > manifest.json
    release-manifest.py verify --manifest <file> --signature <file> --keys <dir>

The manifest is what a panel trusts (design 6.2): the version, a channel, an
expiry, the partition layout generation, and the size and sha256 of each
update payload both as downloaded (.xz) and as written (the raw partition).
latest.json stays unsigned data. The payload facts come from
tools/image-layout.sh's payloads.json, produced in the build job; this tool
adds the release-time facts and is run by the publish job from the build
artifact, because that job checks out nothing.

`expires` bounds REPLAY, not a freeze: an on-path attacker can serve a
lagging panel a signed but superseded release only until it expires, and a
test-channel manifest is a usable asset for hours, not months. A panel that
is already on the newest version never reads a manifest, so an attacker who
can serve it its own latest.json forever holds it there; the freeze detector
is the owner reading "behind for N days" on Home (design 6.2, decision 16).

`verify` checks a DER ECDSA-P256/SHA-256 signature over the manifest's EXACT
bytes against every public key in a directory and reports which verified,
then checks the parsed keyId names that key. It is the same rule the panel
will apply (SCO-68), used here so the publish job can prove KMS signed with
the key the repository carries before anything is released; the workflow's
openssl call does the same check, and the tests here prove the two agree.
No private key is ever read by this file: signing happens in KMS.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
# Fixed by the design (6.2): stable manifests expire 120 days after publish,
# test manifests 6 hours after, so a deliberately broken test release stops
# being verifiable the same day.
EXPIRY = {"stable": dt.timedelta(days=120), "test": dt.timedelta(hours=6)}
LAYOUT = 1


def rfc3339(when: dt.datetime) -> str:
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make(payloads: dict, version: str, channel: str, key_id: str, released: dt.datetime) -> dict:
    if not VERSION_RE.match(version):
        raise ValueError(f"version {version!r} is not vX.Y.Z")
    if channel not in EXPIRY:
        raise ValueError(f"channel {channel!r} is not one of {sorted(EXPIRY)}")
    if payloads.get("layout") != LAYOUT:
        raise ValueError(f"payloads.json is layout {payloads.get('layout')!r}, not {LAYOUT}")
    out_payloads = {}
    for name in ("boot", "root"):
        p = payloads[name]
        for field in ("file", "size", "sha256", "rawSize", "rawSha256"):
            if field not in p:
                raise ValueError(f"payload {name} lacks {field}")
        if not p["file"].startswith(f"scoreboard-{version}.") or not p["file"].endswith(f".{name}.img.xz"):
            raise ValueError(f"payload {name} file {p['file']!r} is not this version's")
        if not (isinstance(p["size"], int) and p["size"] > 0 and isinstance(p["rawSize"], int) and p["rawSize"] > 0):
            raise ValueError(f"payload {name} sizes must be positive integers")
        for field in ("sha256", "rawSha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", p[field]):
                raise ValueError(f"payload {name} {field} is not a sha256")
        out_payloads[name] = {k: p[k] for k in ("file", "size", "sha256", "rawSize", "rawSha256")}
    return {
        "version": version,
        "channel": channel,
        "released": rfc3339(released),
        "expires": rfc3339(released + EXPIRY[channel]),
        "layout": LAYOUT,
        "payloads": out_payloads,
        "keyId": key_id,
    }


def verify(manifest_bytes: bytes, signature_der: bytes, keys_dir: Path) -> str:
    """The name of the key that verified, or a ValueError naming why not.

    Every key is tried against the exact bytes before anything is parsed:
    reading keyId first would mean parsing the thing the signature protects.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pems = sorted(p for p in keys_dir.iterdir() if p.suffix == ".pem")
    if not pems:
        raise ValueError(f"no public key in {keys_dir}")
    verified = None
    for pem in pems:
        key = serialization.load_pem_public_key(pem.read_bytes())
        if not isinstance(key, ec.EllipticCurvePublicKey):
            raise ValueError(f"{pem.name} is not an EC public key")
        try:
            key.verify(signature_der, manifest_bytes, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            continue
        verified = pem.stem
        break
    if verified is None:
        raise ValueError("no key verifies the signature")
    manifest = json.loads(manifest_bytes)
    if manifest.get("keyId") != verified:
        raise ValueError(f"keyId {manifest.get('keyId')!r} is not the key that verified ({verified})")
    return verified


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("make")
    m.add_argument("--payloads", required=True, type=Path)
    m.add_argument("--version", required=True)
    m.add_argument("--channel", required=True)
    m.add_argument("--key-id", required=True)
    m.add_argument("--released", help="RFC 3339 UTC; defaults to now")
    v = sub.add_parser("verify")
    v.add_argument("--manifest", required=True, type=Path)
    v.add_argument("--signature", required=True, type=Path, help="base64 or raw DER ECDSA signature")
    v.add_argument("--keys", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "make":
            released = (dt.datetime.strptime(args.released, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
                        if args.released else dt.datetime.now(dt.timezone.utc))
            manifest = make(json.loads(args.payloads.read_text()), args.version, args.channel, args.key_id, released)
            sys.stdout.write(json.dumps(manifest, indent=2) + "\n")
        else:
            # KMS returns the signature base64-encoded; a raw DER file (as
            # the tests and openssl write it) has bytes outside the base64
            # alphabet, which is how the two are told apart.
            raw = args.signature.read_bytes()
            stripped = raw.strip()
            if stripped and re.fullmatch(rb"[A-Za-z0-9+/=]+", stripped):
                signature = base64.b64decode(stripped, validate=True)
            else:
                signature = raw
            which = verify(args.manifest.read_bytes(), signature, args.keys)
            print(f"verified by {which}")
    except (ValueError, KeyError, OSError) as e:
        print(f"release-manifest: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
