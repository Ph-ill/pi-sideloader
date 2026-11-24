"""Development key and CSR. The private key stays on the Pi, mode 0600.

A password-protected p12 would have to put that password on zsign's command
line, where ``ps`` can see it. The key is an unencrypted PEM instead, and
zsign is given ``-k`` and ``-c`` with no password.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from sideload_pi.errors import SideloadError


def generate_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def key_pem(key) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_key(blob: bytes):
    return serialization.load_pem_private_key(blob, password=None)


def make_csr(key, apple_id: str) -> bytes:
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"Apple Development: {apple_id}")]
    )
    csr = x509.CertificateSigningRequestBuilder().subject_name(name).sign(key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.DER)


def csr_b64(csr_der: bytes) -> str:
    return base64.b64encode(csr_der).decode("ascii")


def certificate_not_after(blob: bytes) -> datetime:
    try:
        if b"-----BEGIN" in blob:
            cert = x509.load_pem_x509_certificate(blob)
        else:
            cert = x509.load_der_x509_certificate(blob)
    except ValueError as exc:
        raise SideloadError("stored development certificate could not be read") from exc
    expiry = getattr(cert, "not_valid_after_utc", None)
    if expiry is None:
        expiry = cert.not_valid_after
    if expiry.tzinfo is None:
        return expiry.replace(tzinfo=UTC)
    return expiry


def certificate_pem(blob: bytes) -> bytes:
    if b"-----BEGIN" in blob:
        return blob
    cert = x509.load_der_x509_certificate(blob)
    return cert.public_bytes(serialization.Encoding.PEM)
