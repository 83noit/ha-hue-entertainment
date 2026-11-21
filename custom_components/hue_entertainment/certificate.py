"""Bridge-shaped TLS certificate for the HTTPS (CLIP v2) listener.

Official Hue clients check the bridge's certificate. The shape follows the
diyHue emulator, which the Samsung Hue Sync app is reported to accept: a
self-signed EC P-256 certificate whose subject CN is the bridge id and whose
serial number is that id read as hex, backdated to 2017.

Uses ``cryptography``, a Home Assistant core dependency (no extra
requirement). Everything here is blocking: call it from the executor.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_LOGGER = logging.getLogger(__name__)

NOT_BEFORE = datetime.datetime(2017, 1, 1, tzinfo=datetime.UTC)
VALIDITY = datetime.timedelta(days=7670)
TLS12_CIPHERS = "ECDHE-ECDSA-AES128-GCM-SHA256"


def build_certificate(bridge_id: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """Return a new key and a self-signed certificate for ``bridge_id``."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "NL"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Philips Hue"),
            x509.NameAttribute(NameOID.COMMON_NAME, bridge_id.lower()),
        ]
    )
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(int(bridge_id, 16))
        .not_valid_before(NOT_BEFORE)
        .not_valid_after(NOT_BEFORE + VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(ski, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _usable(pem: bytes, bridge_id: str) -> bool:
    """The file holds a key and a certificate for ``bridge_id`` that belong together."""
    try:
        cert = x509.load_pem_x509_certificate(pem)
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError):
        return False
    cns = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not cns or cns[0].value != bridge_id.lower():
        return False
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) == cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def ensure_certificate(path: Path, bridge_id: str, *, regenerate: bool = False) -> Path:
    """Make sure ``path`` holds a key + certificate for ``bridge_id``.

    Reuses an existing file whose certificate is for this bridge and matches
    its key; otherwise (missing, damaged, other bridge, or ``regenerate``)
    writes a new one atomically, mode 0600.
    """
    if not regenerate and path.exists() and _usable(path.read_bytes(), bridge_id):
        return path
    key, cert = build_certificate(bridge_id)
    data = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ) + cert.public_bytes(serialization.Encoding.PEM)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a temporary file and rename it over the old one: a crash or full
    # disk mid-write never leaves a half-written certificate behind.
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)  # O_CREAT's mode is ignored when the file already existed
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _LOGGER.info("Generated bridge certificate for %s", bridge_id)
    return path


def certificate_fingerprint(path: Path) -> str:
    """SHA-256 fingerprint of the certificate in ``path`` (colon-separated hex)."""
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()


def create_ssl_context(path: Path, key_path: Path | None = None) -> ssl.SSLContext:
    """Server-side TLS context presenting the bridge certificate.

    ``path`` holds the certificate (plus any chain), and the key too unless
    ``key_path`` is given.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers(TLS12_CIPHERS)  # TLS 1.2 suite; TLS 1.3 suites keep their defaults
    ctx.set_ecdh_curve("prime256v1")
    ctx.load_cert_chain(path, key_path)
    return ctx
