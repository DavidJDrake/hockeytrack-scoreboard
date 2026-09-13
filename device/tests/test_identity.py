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
