"""
somniguard_gateway/tls_setup.py

TLS/HTTPS setup for the SOMNI-Guard Raspberry Pi 5 gateway.

Handles self-signed certificate generation, certificate management helpers,
and HTTPS configuration for Flask.

Educational prototype — not a clinically approved device.
"""

import datetime
import ipaddress
import os
import stat

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime.datetime:
    """Return timezone-aware UTC datetime (compatible with cryptography >= 42)."""
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_cert_directory(cert_dir: str) -> None:
    """Create *cert_dir* if it does not exist and set permissions to 0o700.

    Educational prototype — not a clinically approved device.
    """
    if not os.path.exists(cert_dir):
        os.makedirs(cert_dir, mode=0o700, exist_ok=True)
        print(f"[SOMNI][TLS] Created certificate directory: {cert_dir}")
    else:
        os.chmod(cert_dir, 0o700)
        print(f"[SOMNI][TLS] Certificate directory already exists: {cert_dir}")


def generate_self_signed_cert(
    cert_dir: str,
    hostname: str = "somni-gateway",
    days_valid: int = 365,
) -> tuple[str, str]:
    """Generate an RSA-2048 self-signed X.509 certificate and private key.

    Saves:
      - ``<cert_dir>/server.key``  (PEM, no passphrase, mode 0o600)
      - ``<cert_dir>/server.crt``  (PEM, mode 0o644)

    Subject Alternative Names include:
      - DNS:<hostname>
      - DNS:localhost
      - IP:127.0.0.1

    Returns:
        (cert_path, key_path) tuple of absolute paths.

    Educational prototype — not a clinically approved device.
    """
    ensure_cert_directory(cert_dir)

    print(f"[SOMNI][TLS] Generating RSA-2048 private key …")
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Build the certificate subject / issuer (self-signed, so they are equal)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SOMNI-Guard"),
        ]
    )

    now = _utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(hostname),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    key_path = os.path.join(cert_dir, "server.key")
    cert_path = os.path.join(cert_dir, "server.crt")

    # Write private key — no encryption, tight permissions
    with open(key_path, "wb") as fh:
        fh.write(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    os.chmod(key_path, 0o600)
    print(f"[SOMNI][TLS] Private key written to {key_path} (mode 0o600)")

    # Write certificate
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(cert_path, 0o644)
    print(f"[SOMNI][TLS] Certificate written to {cert_path} (mode 0o644)")

    return cert_path, key_path


def check_cert_exists(cert_dir: str) -> bool:
    """Return True if both server.crt and server.key exist inside *cert_dir*.

    Educational prototype — not a clinically approved device.
    """
    cert_path = os.path.join(cert_dir, "server.crt")
    key_path = os.path.join(cert_dir, "server.key")
    exists = os.path.isfile(cert_path) and os.path.isfile(key_path)
    print(f"[SOMNI][TLS] Certificate present: {exists}")
    return exists


def get_cert_info(cert_path: str) -> dict:
    """Load and parse the PEM certificate at *cert_path*.

    Returns a dict with keys:
      - ``subject``       – str representation of the subject name
      - ``issuer``        – str representation of the issuer name
      - ``not_before``    – datetime (UTC)
      - ``not_after``     – datetime (UTC)
      - ``serial_number`` – int

    Educational prototype — not a clinically approved device.
    """
    with open(cert_path, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())

    info = {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "not_before": cert.not_valid_before_utc,
        "not_after": cert.not_valid_after_utc,
        "serial_number": cert.serial_number,
    }
    print(
        f"[SOMNI][TLS] Certificate info loaded — subject: {info['subject']}, "
        f"expires: {info['not_after'].date()}"
    )
    return info


def is_cert_expiring_soon(cert_path: str, days_threshold: int = 30) -> bool:
    """Return True if the certificate at *cert_path* expires within *days_threshold* days.

    Educational prototype — not a clinically approved device.
    """
    info = get_cert_info(cert_path)
    remaining = info["not_after"] - _utcnow()
    expiring = remaining.days <= days_threshold
    print(
        f"[SOMNI][TLS] Days until expiry: {remaining.days} "
        f"(threshold {days_threshold}) — expiring soon: {expiring}"
    )
    return expiring


def configure_flask_ssl(app, cert_dir: str) -> tuple[str, str]:
    """Ensure TLS certificates exist and return an ssl_context tuple for Flask.

    Usage::

        ssl_context = configure_flask_ssl(app, "/etc/somniguard/certs")
        app.run(host="0.0.0.0", port=443, ssl_context=ssl_context)

    If the certificates do not yet exist they are generated automatically.
    If the existing certificate is expiring within 30 days a warning is printed
    but the existing certificate is still used (renewal is left to the operator).

    Returns:
        (cert_path, key_path) ready to pass as ``ssl_context`` to ``app.run()``.

    Educational prototype — not a clinically approved device.
    """
    if not check_cert_exists(cert_dir):
        print("[SOMNI][TLS] No certificate found — generating a new self-signed cert …")
        cert_path, key_path = generate_self_signed_cert(cert_dir)
    else:
        cert_path = os.path.join(cert_dir, "server.crt")
        key_path = os.path.join(cert_dir, "server.key")
        print(f"[SOMNI][TLS] Using existing certificate: {cert_path}")
        if is_cert_expiring_soon(cert_path):
            print(
                "[SOMNI][TLS] WARNING: Certificate is expiring soon. "
                "Consider renewing or regenerating."
            )

    return cert_path, key_path
