#!/usr/bin/env python3
"""
setup_gateway_certs.py — Build the SOMNI-Guard PKI on the gateway.

Idempotent. Safe to run on every boot via systemd ExecStartPre.

What it does
------------
1. Ensures a long-lived Root CA exists (cert_dir/ca.crt, ca.key).
2. Ensures a server cert signed by the CA exists with SANs that match
   the gateway's current IP addresses; regenerates if SANs drift or
   the cert is near expiry.
3. Ensures a Pico client cert signed by the CA exists, ready to be
   embedded into the Pico firmware via embed_pico_cert.py.

Usage:
    python3 scripts/setup_gateway_certs.py
    python3 scripts/setup_gateway_certs.py --force-regenerate
    python3 scripts/setup_gateway_certs.py --cert-dir /etc/somniguard/certs
    python3 scripts/setup_gateway_certs.py --device-id pico-02
"""

import argparse
import os
import sys
from pathlib import Path

# Make somniguard_gateway/tls_setup importable when run from anywhere
SCRIPT_DIR  = Path(__file__).resolve().parent
REPO_ROOT   = SCRIPT_DIR.parent
GATEWAY_DIR = REPO_ROOT / "somniguard_gateway"
sys.path.insert(0, str(GATEWAY_DIR))

from cryptography import x509  # noqa: E402
from cryptography.x509.oid import ExtensionOID  # noqa: E402

from tls_setup import (  # noqa: E402
    CA_CERT_NAME, CA_KEY_NAME,
    SERVER_CERT_NAME, SERVER_KEY_NAME,
    PICO_CERT_NAME, PICO_KEY_NAME,
    generate_ca, generate_server_cert, generate_client_cert,
    get_cert_sha256_fingerprint, is_cert_expiring_soon,
    _detect_local_ips,
)


def server_cert_needs_regen(cert_path: str, ca_cert_path: str) -> bool:
    """True if server.crt is missing, expiring, has stale SANs, or is not
    signed by the current CA."""
    if not os.path.isfile(cert_path):
        return True
    if is_cert_expiring_soon(cert_path, days_threshold=14):
        print("[SOMNI][TLS] Server cert expires within 14 days — regenerating.")
        return True

    try:
        with open(cert_path, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
        with open(ca_cert_path, "rb") as fh:
            ca = x509.load_pem_x509_certificate(fh.read())

        # Issuer must match the current CA's subject
        if cert.issuer.rfc4514_string() != ca.subject.rfc4514_string():
            print("[SOMNI][TLS] Server cert is not signed by the current CA — regenerating.")
            return True

        # not_before must be 2000-01-01 so the Pico's boot RTC never fails the
        # validity check (Pico RTC resets to 2000-01-01 on cold boot).
        nb = cert.not_valid_before_utc
        if nb.year > 2000 or nb.month > 1 or nb.day > 1:
            print("[SOMNI][TLS] Server cert not_before is after 2000-01-01 — "
                  "regenerating so Pico HTTPS time-sync works without HTTP.")
            return True

        san_ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
        cert_ips = {
            str(name.value)
            for name in san_ext.value
            if isinstance(name, x509.IPAddress)
        }
        required_ips = {str(ip) for ip in _detect_local_ips()}
        missing = required_ips - cert_ips
        if missing:
            print(f"[SOMNI][TLS] Server cert missing SAN IPs: {sorted(missing)} — regenerating.")
            return True
    except x509.ExtensionNotFound:
        return True
    except Exception as exc:
        print(f"[SOMNI][TLS] Could not validate server cert ({exc}) — regenerating.")
        return True
    return False


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--cert-dir", default=str(GATEWAY_DIR / "certs"),
                   help="Certificate directory (default: %(default)s)")
    p.add_argument("--device-id", default="pico-01",
                   help="Device ID for the Pico client cert CN (default: %(default)s)")
    p.add_argument("--force-regenerate", action="store_true",
                   help="Regenerate ALL certs, even if valid (CA included).")
    p.add_argument("--force-server", action="store_true",
                   help="Regenerate only the server cert.")
    p.add_argument("--force-client", action="store_true",
                   help="Regenerate only the Pico client cert.")
    args = p.parse_args()

    cert_dir = args.cert_dir
    os.makedirs(cert_dir, mode=0o700, exist_ok=True)

    ca_cert  = os.path.join(cert_dir, CA_CERT_NAME)
    ca_key   = os.path.join(cert_dir, CA_KEY_NAME)
    srv_cert = os.path.join(cert_dir, SERVER_CERT_NAME)
    srv_key  = os.path.join(cert_dir, SERVER_KEY_NAME)
    pico_crt = os.path.join(cert_dir, PICO_CERT_NAME)
    pico_key = os.path.join(cert_dir, PICO_KEY_NAME)

    print("\n" + "=" * 70)
    print("  SOMNI-Guard PKI setup")
    print("=" * 70 + "\n")

    # 1. Root CA
    if args.force_regenerate or not (os.path.isfile(ca_cert) and os.path.isfile(ca_key)):
        generate_ca(cert_dir)
    else:
        print(f"[SOMNI][TLS] Root CA OK: {ca_cert}")

    # 2. Server cert
    if args.force_regenerate or args.force_server or server_cert_needs_regen(srv_cert, ca_cert):
        generate_server_cert(cert_dir)
    else:
        print(f"[SOMNI][TLS] Server cert OK: {srv_cert}")

    # 3. Pico client cert
    if (args.force_regenerate or args.force_client
            or not (os.path.isfile(pico_crt) and os.path.isfile(pico_key))):
        generate_client_cert(cert_dir, args.device_id)
    else:
        print(f"[SOMNI][TLS] Pico client cert OK: {pico_crt}")

    # Fingerprints — useful for verification on the Pico side
    print("")
    print(f"[SOMNI][TLS] CA SHA-256       : {get_cert_sha256_fingerprint(ca_cert)}")
    print(f"[SOMNI][TLS] Server SHA-256   : {get_cert_sha256_fingerprint(srv_cert)}")
    print(f"[SOMNI][TLS] Pico cert SHA-256: {get_cert_sha256_fingerprint(pico_crt)}")

    print("\n" + "=" * 70)
    print("  ✓ PKI setup complete")
    print("=" * 70 + "\n")
    print("Next: run `python3 scripts/embed_pico_cert.py` to embed the CA")
    print("      and the Pico client cert into somniguard_pico/config.py.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
