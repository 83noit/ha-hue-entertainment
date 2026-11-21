"""Bridge-shaped TLS certificate (certificate.py)."""

from __future__ import annotations

import datetime
import importlib.util
import ssl
import stat
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_PATH = Path(__file__).parent.parent / "custom_components" / "hue_entertainment" / "certificate.py"
_spec = importlib.util.spec_from_file_location("hue_entertainment_certificate", _PATH)
certificate = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(certificate)  # type: ignore[union-attr]

BRIDGE_ID = "AABBCCFFFE112233"


@pytest.fixture(scope="module")
def cert() -> x509.Certificate:
    return certificate.build_certificate(BRIDGE_ID)[1]


def _load(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


class TestShape:
    def test_subject_and_issuer(self, cert):
        expected = [("C", "NL"), ("O", "Philips Hue"), ("CN", BRIDGE_ID.lower())]
        for name in (cert.subject, cert.issuer):
            assert [(a.rfc4514_attribute_name, a.value) for a in name] == expected

    def test_serial_is_bridge_id(self, cert):
        assert cert.serial_number == int(BRIDGE_ID, 16)

    def test_validity_backdated_to_2017(self, cert):
        assert cert.not_valid_before_utc == datetime.datetime(2017, 1, 1, tzinfo=datetime.UTC)
        assert cert.not_valid_after_utc - cert.not_valid_before_utc == datetime.timedelta(days=7670)

    def test_ec_p256_sha256(self, cert):
        key = cert.public_key()
        assert isinstance(key, ec.EllipticCurvePublicKey)
        assert key.curve.name == "secp256r1"
        assert cert.signature_hash_algorithm.name == "sha256"

    def test_not_a_ca(self, cert):
        ext = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert ext.critical and ext.value.ca is False

    def test_key_usage(self, cert):
        ext = cert.extensions.get_extension_for_class(x509.KeyUsage)
        assert ext.critical
        assert ext.value.digital_signature and ext.value.key_encipherment
        assert not ext.value.key_cert_sign

    def test_server_auth(self, cert):
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]

    def test_key_identifiers_and_no_san(self, cert):
        ski = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        aki = cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
        assert aki.key_identifier == ski.digest
        with pytest.raises(x509.ExtensionNotFound):
            cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)


class TestEnsure:
    def test_creates_file_with_owner_only_mode(self, tmp_path):
        path = certificate.ensure_certificate(tmp_path / "sub" / "bridge.pem", BRIDGE_ID)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert b"PRIVATE KEY" in path.read_bytes()
        assert _load(path).serial_number == int(BRIDGE_ID, 16)

    def test_idempotent(self, tmp_path):
        path = certificate.ensure_certificate(tmp_path / "bridge.pem", BRIDGE_ID)
        first = path.read_bytes()
        certificate.ensure_certificate(path, BRIDGE_ID)
        assert path.read_bytes() == first

    def test_regenerated_for_another_bridge(self, tmp_path):
        path = certificate.ensure_certificate(tmp_path / "bridge.pem", BRIDGE_ID)
        certificate.ensure_certificate(path, "001788FFFE445566")
        cn = _load(path).subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        assert cn == "001788fffe445566"

    def test_regenerated_when_unreadable(self, tmp_path):
        path = tmp_path / "bridge.pem"
        path.write_bytes(b"garbage")
        path.chmod(0o644)
        certificate.ensure_certificate(path, BRIDGE_ID)
        assert _load(path).serial_number == int(BRIDGE_ID, 16)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_fingerprint(self, tmp_path):
        path = certificate.ensure_certificate(tmp_path / "bridge.pem", BRIDGE_ID)
        fp = certificate.certificate_fingerprint(path)
        assert len(fp.split(":")) == 32 and fp == fp.upper()


def test_ssl_context_loads(tmp_path):
    path = certificate.ensure_certificate(tmp_path / "bridge.pem", BRIDGE_ID)
    ctx = certificate.create_ssl_context(path)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    tls12 = [c["name"] for c in ctx.get_ciphers() if c["protocol"] == "TLSv1.2"]
    assert tls12 == ["ECDHE-ECDSA-AES128-GCM-SHA256"]
