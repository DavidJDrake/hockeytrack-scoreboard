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
