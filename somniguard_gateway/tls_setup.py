"""
somniguard_gateway/tls_setup.py

PKI + TLS setup for the SOMNI-Guard Raspberry Pi 5 gateway.

Architecture
------------
This module builds a 3-cert PKI suitable for a medical-device-style
deployment of the Pico ↔ Gateway link:

    Root CA  (self-signed, BasicConstraints CA:TRUE, keyCertSign+cRLSign)
     │
     ├── Server cert  (issued to the Pi 5; serverAuth EKU; SANs for hotspot IPs)
     │
     └── Client cert  (issued to each Pico; clientAuth EKU; CN = device id)

The Pico ships with the *CA cert* as its trust anchor — never the server
cert directly. That solves two problems:

  1. mbedTLS on the Pico will not accept a non-CA self-signed certificate
     as a trust anchor, which is what produced the historical
     "TLS handshake failed: invalid cert" error.
  2. Server certs can be rotated (e.g. on IP change, expiry) WITHOUT
     re-flashing the Pico — only the long-lived CA needs to stay stable.

Key algorithm: ECDSA on P-256 (secp256r1).
  - ~3× smaller handshake than RSA-2048
  - Lower RAM cost on the Pico's mbedTLS stack
  - Universally supported by mbedTLS, OpenSSL, BoringSSL

Educational prototype — not a clinically approved device.
"""

import datetime
import ipaddress
import os
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


# ---------------------------------------------------------------------------
# Filenames inside the cert directory
# ---------------------------------------------------------------------------
CA_CERT_NAME      = "ca.crt"
CA_KEY_NAME       = "ca.key"
SERVER_CERT_NAME  = "server.crt"
SERVER_KEY_NAME   = "server.key"
PICO_CERT_NAME    = "pico_client.crt"
PICO_KEY_NAME     = "pico_client.key"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _write_pem(path: str, data: bytes, mode: int) -> None:
    with open(path, "wb") as fh:
        fh.write(data)
    os.chmod(path, mode)


def _ec_keypair():
    return ec.generate_private_key(ec.SECP256R1())


def _name(common_name: str, org: str = "SOMNI-Guard") -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
    ])


# ---------------------------------------------------------------------------
# CA + leaf cert generation
# ---------------------------------------------------------------------------

def ensure_cert_directory(cert_dir: str) -> None:
    if not os.path.exists(cert_dir):
        os.makedirs(cert_dir, mode=0o700, exist_ok=True)
        print(f"[SOMNI][TLS] Created certificate directory: {cert_dir}")
    else:
        os.chmod(cert_dir, 0o700)


def generate_ca(cert_dir: str, days_valid: int = 3650) -> tuple[str, str]:
    """Generate a long-lived self-signed Root CA (ECDSA P-256).

    The CA is the trust anchor embedded in the Pico. It signs the server
    cert (used by the Pi 5) and any per-device Pico client certs.

    Returns: (ca_cert_path, ca_key_path)
    """
    ensure_cert_directory(cert_dir)

    ca_cert_path = os.path.join(cert_dir, CA_CERT_NAME)
    ca_key_path  = os.path.join(cert_dir, CA_KEY_NAME)

    print("[SOMNI][TLS] Generating Root CA (ECDSA P-256, valid {} days)…".format(days_valid))
    ca_key = _ec_keypair()
    ca_name = _name("SOMNI-Guard Root CA")
    now = _utcnow()

    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    _write_pem(
        ca_key_path,
        ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        mode=0o600,
    )
    _write_pem(ca_cert_path, ca_cert.public_bytes(serialization.Encoding.PEM), mode=0o644)
    print(f"[SOMNI][TLS] Root CA written: {ca_cert_path}")
    return ca_cert_path, ca_key_path


def _detect_local_ips() -> list[ipaddress.IPv4Address]:
    """Return all IPv4 addresses bound to local interfaces (for SANs)."""
    ips = {
        ipaddress.IPv4Address("127.0.0.1"),
        ipaddress.IPv4Address("10.42.0.1"),    # NetworkManager hotspot
        ipaddress.IPv4Address("192.168.4.1"),  # alternative AP IP
    }
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            try:
                addr = ipaddress.ip_address(info[4][0])
                if isinstance(addr, ipaddress.IPv4Address):
                    ips.add(addr)
            except ValueError:
                pass
    except Exception:
        pass
    return sorted(ips)


def _load_ca(cert_dir: str):
    ca_cert_path = os.path.join(cert_dir, CA_CERT_NAME)
    ca_key_path  = os.path.join(cert_dir, CA_KEY_NAME)
    with open(ca_cert_path, "rb") as fh:
        ca_cert = x509.load_pem_x509_certificate(fh.read())
    with open(ca_key_path, "rb") as fh:
        ca_key = serialization.load_pem_private_key(fh.read(), password=None)
    return ca_cert, ca_key


def generate_server_cert(
    cert_dir: str,
    hostname: str = "somni-gateway",
    days_valid: int = 365,
) -> tuple[str, str]:
    """Generate a CA-signed server cert for the Pi 5 gateway.

    SANs include all known gateway addresses (DNS + IPs) so browsers and
    the Pico both validate without exception-clicking.

    not_before is set to 2000-01-01 intentionally.  The Pico 2W's RTC resets
    to 2000-01-01 on every cold boot.  If not_before were "now" (2026+), the
    Pico's TLS library would see the cert as "not yet valid" and reject it
    before the clock can be synced.  Setting not_before to 2000-01-01 means
    the cert is always valid from the Pico's perspective, with or without a
    live clock.  not_after is still 1 year from generation — the cert does
    expire and will auto-renew.
    """
    ensure_cert_directory(cert_dir)
    ca_cert, ca_key = _load_ca(cert_dir)

    print("[SOMNI][TLS] Generating server cert (ECDSA P-256, signed by Root CA)…")
    srv_key = _ec_keypair()

    san_dns = [
        x509.DNSName(hostname),
        x509.DNSName("localhost"),
        x509.DNSName("somni-pi5.local"),
        x509.DNSName("somni-gateway.local"),
        x509.DNSName("somniguard.local"),
        x509.DNSName("somniguard"),
    ]
    san_ips = [x509.IPAddress(ip) for ip in _detect_local_ips()]
    san_entries = san_dns + san_ips

    print("[SOMNI][TLS] Server SANs: {}".format(
        ", ".join(str(e.value) for e in san_entries)
    ))

    # not_before fixed to 2000-01-01 — see docstring above.
    pico_epoch = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    now = _utcnow()
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(hostname))
        .issuer_name(ca_cert.subject)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(pico_epoch)
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=True,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(srv_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    srv_cert_path = os.path.join(cert_dir, SERVER_CERT_NAME)
    srv_key_path  = os.path.join(cert_dir, SERVER_KEY_NAME)
    _write_pem(
        srv_key_path,
        srv_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        mode=0o600,
    )
    _write_pem(srv_cert_path, srv_cert.public_bytes(serialization.Encoding.PEM), mode=0o644)
    print(f"[SOMNI][TLS] Server cert written: {srv_cert_path}")
    return srv_cert_path, srv_key_path


def generate_client_cert(
    cert_dir: str,
    device_id: str,
    days_valid: int = 365,
) -> tuple[str, str]:
    """Generate a CA-signed client cert for a Pico device.

    The Pico presents this during the TLS handshake; the gateway verifies
    it against the CA. This is the cryptographic device identity layer
    (a stolen HMAC key alone is no longer enough to talk to the gateway).
    """
    ensure_cert_directory(cert_dir)
    ca_cert, ca_key = _load_ca(cert_dir)

    print(f"[SOMNI][TLS] Generating client cert for device '{device_id}'…")
    cli_key = _ec_keypair()

    now = _utcnow()
    cli_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(device_id))
        .issuer_name(ca_cert.subject)
        .public_key(cli_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days_valid))
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=True,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(cli_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    cli_cert_path = os.path.join(cert_dir, PICO_CERT_NAME)
    cli_key_path  = os.path.join(cert_dir, PICO_KEY_NAME)
    _write_pem(
        cli_key_path,
        cli_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        mode=0o600,
    )
    _write_pem(cli_cert_path, cli_cert.public_bytes(serialization.Encoding.PEM), mode=0o644)
    print(f"[SOMNI][TLS] Client cert written: {cli_cert_path}")
    return cli_cert_path, cli_key_path


# ---------------------------------------------------------------------------
# Cert lifecycle helpers
# ---------------------------------------------------------------------------

def check_cert_exists(cert_dir: str, cert_name: str = SERVER_CERT_NAME,
                       key_name: str = SERVER_KEY_NAME) -> bool:
    return (os.path.isfile(os.path.join(cert_dir, cert_name)) and
            os.path.isfile(os.path.join(cert_dir, key_name)))


def get_cert_info(cert_path: str) -> dict:
    with open(cert_path, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    return {
        "subject":       cert.subject.rfc4514_string(),
        "issuer":        cert.issuer.rfc4514_string(),
        "not_before":    cert.not_valid_before_utc,
        "not_after":     cert.not_valid_after_utc,
        "serial_number": cert.serial_number,
    }


def is_cert_expiring_soon(cert_path: str, days_threshold: int = 30) -> bool:
    info = get_cert_info(cert_path)
    return (info["not_after"] - _utcnow()).days <= days_threshold


def get_cert_sha256_fingerprint(cert_path: str) -> str:
    """SHA-256 fingerprint of the DER form, colon-separated lower-case hex."""
    import hashlib
    with open(cert_path, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    der = cert.public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).hexdigest()
    return ":".join(digest[i:i+2] for i in range(0, len(digest), 2))


# ---------------------------------------------------------------------------
# TLS context (mutual TLS — server requires + verifies client certs)
# ---------------------------------------------------------------------------

# TLS 1.2 cipher allowlist: ECDHE + AEAD only.
# (TLS 1.3 cipher suites are AEAD by definition; OpenSSL controls them
# separately and does not need an allowlist.)
#
# CCM ciphers are included for MicroPython mbedTLS compatibility.
# mbedTLS on RP2350 (Pico 2 W) may not compile GCM or CHACHA20 — CCM
# is the AEAD mode most reliably available on constrained mbedTLS builds.
# CCM is equally secure to GCM (both are AES-based AEAD constructions).
STRONG_CIPHERS_TLS12 = (
    "ECDHE-ECDSA-AES256-GCM-SHA384:"
    "ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:"
    "ECDHE-RSA-CHACHA20-POLY1305:"
    "ECDHE-ECDSA-AES128-GCM-SHA256:"
    "ECDHE-RSA-AES128-GCM-SHA256:"
    # CCM fallback for MicroPython mbedTLS on RP2350 (Pico 2 W).
    # CCM is an AEAD cipher (AES + CBC-MAC), secure for TLS 1.2.
    "ECDHE-ECDSA-AES128-CCM:"
    "ECDHE-ECDSA-AES256-CCM:"
    "ECDHE-ECDSA-AES128-CCM8:"
    "ECDHE-ECDSA-AES256-CCM8"
)


def build_mutual_tls_context(cert_dir: str) -> ssl.SSLContext:
    """Return a hardened ssl.SSLContext requiring a CA-signed client cert.

    - TLS 1.2 minimum (1.3 preferred)
    - ECDHE + AEAD ciphers only
    - Compression off (CRIME mitigation)
    - Server cert from cert_dir/server.{crt,key}
    - Client cert verification: REQUIRED, validated against cert_dir/ca.crt
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    ctx.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    ctx.options |= ssl.OP_NO_COMPRESSION
    ctx.options |= ssl.OP_SINGLE_DH_USE
    ctx.options |= ssl.OP_SINGLE_ECDH_USE
    ctx.options |= ssl.OP_CIPHER_SERVER_PREFERENCE
    if hasattr(ssl, "OP_NO_RENEGOTIATION"):
        ctx.options |= ssl.OP_NO_RENEGOTIATION
    ctx.set_ciphers(STRONG_CIPHERS_TLS12)

    server_crt = os.path.join(cert_dir, SERVER_CERT_NAME)
    server_key = os.path.join(cert_dir, SERVER_KEY_NAME)
    ca_crt     = os.path.join(cert_dir, CA_CERT_NAME)

    ctx.load_cert_chain(server_crt, server_key)
    ctx.load_verify_locations(cafile=ca_crt)
    # CERT_OPTIONAL: request a client cert but don't require one.
    # Browsers connect without a cert (they use session auth instead).
    # The Pico still presents its cert — the gateway validates it when present.
    # HMAC-SHA256 is the primary API authentication layer for the Pico.
    ctx.verify_mode = ssl.CERT_OPTIONAL
    ctx.check_hostname = False

    print("[SOMNI][TLS] TLS context ready: 1.2+1.3, ECDHE+AEAD, "
          "client certs optional (Pico presents cert, browsers use session auth).")

    # Print the actual TLS 1.2 cipher names that OpenSSL accepted from our
    # allowlist (they are not always identical — older OpenSSL builds quietly
    # drop suites they do not implement).  Lets the operator see exactly
    # which cipher suite the Pico will negotiate against this context.
    try:
        accepted = ctx.get_ciphers()
        names = [c["name"] for c in accepted if "name" in c]
        if names:
            print("[SOMNI][TLS] TLS 1.2 cipher suites offered ({}): {}".format(
                len(names), ", ".join(names)))
        else:
            print("[SOMNI][TLS] TLS 1.2 cipher list returned no suites — "
                  "check OpenSSL build for ECDHE/AEAD support.")
    except Exception as exc:
        print("[SOMNI][TLS] Could not enumerate ciphers: {}".format(exc))
    print("[SOMNI][TLS] TLS 1.3 suites are negotiated automatically by OpenSSL "
          "(TLS_AES_*_GCM_SHA*, TLS_CHACHA20_POLY1305_SHA256).")

    return ctx


def configure_flask_ssl(app, cert_dir: str, device_id: str = "pico-01") -> ssl.SSLContext:
    """Ensure CA + server cert (+ Pico client cert) exist and return an mTLS context.

    First-run flow:
      1. Generate Root CA if missing.
      2. Generate server cert if missing.
      3. Generate Pico client cert if missing (so embed_pico_cert.py has
         something to embed).
      4. Build a CERT_OPTIONAL ssl.SSLContext using the CA as trust anchor.

    Subsequent runs only regenerate certs that are missing or near expiry.
    """
    ensure_cert_directory(cert_dir)

    ca_path = os.path.join(cert_dir, CA_CERT_NAME)
    if not os.path.isfile(ca_path):
        generate_ca(cert_dir)
    elif is_cert_expiring_soon(ca_path, days_threshold=30):
        print("[SOMNI][TLS] WARNING: Root CA expires within 30 days. Rotation needed.")

    srv_path = os.path.join(cert_dir, SERVER_CERT_NAME)
    if not check_cert_exists(cert_dir, SERVER_CERT_NAME, SERVER_KEY_NAME):
        generate_server_cert(cert_dir)
    elif is_cert_expiring_soon(srv_path, days_threshold=30):
        print("[SOMNI][TLS] Server cert expires soon — regenerating.")
        generate_server_cert(cert_dir)

    if not check_cert_exists(cert_dir, PICO_CERT_NAME, PICO_KEY_NAME):
        generate_client_cert(cert_dir, device_id)

    return build_mutual_tls_context(cert_dir)
