#!/usr/bin/env python3
"""
debug_tls.py — Diagnose TLS handshake failures between Pico and gateway.

This script:
  1. Reads the certificate from the gateway
  2. Reads the certificate embedded in the Pico's config.py
  3. Compares them (must be IDENTICAL byte-for-byte)
  4. Verifies certificate validity (dates, SANs)
  5. Tests TLS connection to gateway using OpenSSL
  6. Auto-fixes if mismatch detected

Usage:
    python3 scripts/debug_tls.py
    python3 scripts/debug_tls.py --gateway-ip 10.42.0.1
    python3 scripts/debug_tls.py --auto-fix
"""

import argparse
import hashlib
import os
import re
import socket
import ssl
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_CERT = REPO_ROOT / "somniguard_gateway" / "certs" / "server.crt"
PICO_CONFIG = REPO_ROOT / "somniguard_pico" / "config.py"
ENCRYPTED_DIR = REPO_ROOT / "encrypted_deploy"


def color(text, code):
    return f"\033[{code}m{text}\033[0m"


def red(text): return color(text, "31")
def green(text): return color(text, "32")
def yellow(text): return color(text, "33")
def blue(text): return color(text, "34")
def bold(text): return color(text, "1")


def print_header(text):
    print()
    print(bold(blue("=" * 70)))
    print(bold(blue(f"  {text}")))
    print(bold(blue("=" * 70)))


def print_section(text):
    print()
    print(bold(f"▶ {text}"))
    print("-" * 70)


def fingerprint(pem_bytes):
    """Calculate SHA-256 fingerprint of PEM cert (DER encoded)."""
    import base64
    body = b"".join(
        line for line in pem_bytes.splitlines()
        if line and not line.startswith(b"-----")
    )
    der = base64.b64decode(body)
    digest = hashlib.sha256(der).hexdigest()
    return ":".join(digest[i:i+2] for i in range(0, len(digest), 2))


def normalize_pem(pem_text):
    """Normalize PEM by extracting only the cert content."""
    match = re.search(
        r'-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----',
        pem_text,
        re.DOTALL
    )
    if not match:
        return None
    body = match.group(1)
    # Strip whitespace and rebuild
    clean = ''.join(body.split())
    return f"-----BEGIN CERTIFICATE-----\n{clean}\n-----END CERTIFICATE-----\n"


def get_gateway_cert():
    """Read the certificate from the gateway."""
    if not GATEWAY_CERT.exists():
        return None
    return GATEWAY_CERT.read_bytes()


def get_pico_cert():
    """Extract the certificate from somniguard_pico/config.py."""
    if not PICO_CONFIG.exists():
        return None
    src = PICO_CONFIG.read_text(encoding="utf-8")
    match = re.search(
        r'GATEWAY_CA_CERT_PEM\s*=\s*"""(.*?)"""',
        src,
        re.DOTALL
    )
    if not match:
        return None
    pem_text = match.group(1).strip() + "\n"
    return pem_text.encode("utf-8")


def get_pico_gateway_host():
    """Extract GATEWAY_HOST from Pico config."""
    if not PICO_CONFIG.exists():
        return None
    src = PICO_CONFIG.read_text(encoding="utf-8")
    match = re.search(r'GATEWAY_HOST\s*=\s*"([^"]+)"', src)
    return match.group(1) if match else None


def get_pico_gateway_port():
    """Extract GATEWAY_PORT from Pico config."""
    if not PICO_CONFIG.exists():
        return 5443
    src = PICO_CONFIG.read_text(encoding="utf-8")
    match = re.search(r'GATEWAY_PORT\s*=\s*(\d+)', src)
    return int(match.group(1)) if match else 5443


def parse_cert_info(pem_bytes):
    """Parse certificate using cryptography library."""
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(pem_bytes)
        info = {
            "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
            "not_before": cert.not_valid_before_utc,
            "not_after": cert.not_valid_after_utc,
            "serial": cert.serial_number,
            "sans": [],
        }
        try:
            san_ext = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            )
            for entry in san_ext.value:
                info["sans"].append(str(entry.value))
        except Exception:
            pass
        return info
    except Exception as e:
        return {"error": str(e)}


def test_tls_connection(host, port, ca_pem_bytes):
    """Test TLS connection to gateway with the given CA cert."""
    # Write cert to temp file
    import tempfile
    with tempfile.NamedTemporaryFile(mode='wb', suffix='.crt', delete=False) as f:
        f.write(ca_pem_bytes)
        ca_file = f.name

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=ca_file)

        sock = socket.create_connection((host, port), timeout=10)
        wrapped = ctx.wrap_socket(sock, server_hostname=host)
        peer_cert = wrapped.getpeercert(binary_form=True)
        wrapped.close()

        return True, "TLS handshake succeeded ✓", peer_cert
    except ssl.SSLError as e:
        return False, f"SSL Error: {e}", None
    except socket.gaierror as e:
        return False, f"DNS Error: {e}", None
    except socket.timeout:
        return False, f"Connection timeout (host {host}:{port} unreachable)", None
    except ConnectionRefusedError:
        return False, f"Connection refused (gateway not running at {host}:{port}?)", None
    except OSError as e:
        return False, f"Network error: {e}", None
    finally:
        os.unlink(ca_file)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--gateway-ip", help="Gateway IP to test (default: from config)")
    p.add_argument("--gateway-port", type=int, default=None,
                   help="Gateway port (default: from config)")
    p.add_argument("--auto-fix", action="store_true",
                   help="Auto-fix mismatches by re-running embed_pico_cert.py")
    args = p.parse_args()

    print_header("SOMNI-Guard TLS Debug Tool")

    # Step 1: Check gateway cert exists
    print_section("Step 1: Gateway Certificate")
    gateway_cert = get_gateway_cert()
    if not gateway_cert:
        print(red(f"✗ Gateway certificate NOT FOUND at:"))
        print(f"  {GATEWAY_CERT}")
        print()
        print(yellow("FIX: Run on gateway:"))
        print("  python3 scripts/setup_gateway_certs.py")
        return 1

    print(green(f"✓ Found at: {GATEWAY_CERT}"))
    print(f"  Size: {len(gateway_cert)} bytes")
    gw_fp = fingerprint(gateway_cert)
    print(f"  SHA-256: {gw_fp}")

    gw_info = parse_cert_info(gateway_cert)
    if "error" not in gw_info:
        print(f"  Subject: {gw_info['subject']}")
        print(f"  Valid:   {gw_info['not_before']} → {gw_info['not_after']}")
        print(f"  SANs:    {', '.join(gw_info['sans'])}")

        # Check expiry
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if gw_info['not_after'] < now:
            print(red(f"  ⚠ EXPIRED on {gw_info['not_after']}"))
        elif gw_info['not_before'] > now:
            print(red(f"  ⚠ NOT YET VALID until {gw_info['not_before']}"))
        else:
            print(green(f"  ✓ Currently valid"))

    # Step 2: Check Pico config cert
    print_section("Step 2: Pico Config Certificate (in config.py)")
    pico_cert = get_pico_cert()
    if not pico_cert:
        print(red(f"✗ No GATEWAY_CA_CERT_PEM found in:"))
        print(f"  {PICO_CONFIG}")
        return 1

    print(green(f"✓ Found in: {PICO_CONFIG}"))
    print(f"  Size: {len(pico_cert)} bytes")

    try:
        pico_fp = fingerprint(pico_cert)
        print(f"  SHA-256: {pico_fp}")
    except Exception as e:
        print(red(f"  ✗ Could not parse: {e}"))
        pico_fp = None

    pico_info = parse_cert_info(pico_cert)
    if "error" not in pico_info:
        print(f"  Subject: {pico_info['subject']}")
        print(f"  Valid:   {pico_info['not_before']} → {pico_info['not_after']}")
        print(f"  SANs:    {', '.join(pico_info['sans'])}")

    # Step 3: Compare
    print_section("Step 3: Certificate Comparison")
    if pico_fp == gw_fp:
        print(green("✓ Certificates MATCH (SHA-256 fingerprints identical)"))
    else:
        print(red("✗ Certificates DO NOT MATCH"))
        print(f"  Gateway:  {gw_fp}")
        print(f"  Pico:     {pico_fp}")
        print()
        print(yellow("This is why TLS handshake fails!"))
        print()

        if args.auto_fix:
            print(yellow("Auto-fixing..."))
            result = subprocess.run(
                ["python3", str(REPO_ROOT / "scripts" / "embed_pico_cert.py")],
                capture_output=True, text=True
            )
            print(result.stdout)
            if result.returncode == 0:
                print(green("✓ Fixed! config.py now has correct cert."))
            else:
                print(red("✗ Auto-fix failed:"))
                print(result.stderr)
                return 1
        else:
            print(yellow("FIX: Run:"))
            print("  python3 scripts/embed_pico_cert.py")
            print()

    # Step 4: Check encrypted deploy directory
    print_section("Step 4: Check Encrypted Deploy")
    if ENCRYPTED_DIR.exists():
        config_enc = ENCRYPTED_DIR / "config.enc"
        if config_enc.exists():
            mtime_config = PICO_CONFIG.stat().st_mtime
            mtime_enc = config_enc.stat().st_mtime
            if mtime_enc < mtime_config:
                print(red("✗ encrypted_deploy/config.enc is OLDER than config.py"))
                print(yellow("This means the Pico has the OLD cert!"))
                print()
                print(yellow("FIX:"))
                print("  1. python3 scripts/encrypt_pico_files.py --uid <pico-uid>")
                print("  2. mpremote connect /dev/cu.usbmodem* fs cp -r encrypted_deploy/. :")
                print("  3. mpremote connect /dev/cu.usbmodem* reset")
            else:
                print(green("✓ encrypted_deploy/config.enc is up to date"))
        else:
            print(yellow("⚠ No config.enc in encrypted_deploy/"))
    else:
        print(yellow("⚠ No encrypted_deploy/ directory yet"))
        print("  Run: python3 scripts/encrypt_pico_files.py --uid <pico-uid>")

    # Step 5: Test TLS connection
    print_section("Step 5: Live TLS Test")
    host = args.gateway_ip or get_pico_gateway_host()
    port = args.gateway_port or get_pico_gateway_port()

    if not host:
        print(yellow("⚠ Could not determine GATEWAY_HOST from config"))
        print("  Pass --gateway-ip <ip> to test")
        return 0

    print(f"Testing TLS connection to {host}:{port}...")
    print(f"Using gateway cert as CA (cert pinning, like the Pico does)")
    print()

    success, msg, peer_cert = test_tls_connection(host, port, gateway_cert)

    if success:
        print(green(f"✓ {msg}"))
        if peer_cert:
            peer_pem = ssl.DER_cert_to_PEM_cert(peer_cert).encode()
            peer_fp = fingerprint(peer_pem)
            print(f"  Peer cert SHA-256: {peer_fp}")
            if peer_fp == gw_fp:
                print(green("  ✓ Peer cert matches local cert"))
            else:
                print(red("  ✗ Peer cert DIFFERS from local cert!"))
                print(yellow("  The gateway is serving a DIFFERENT cert than what's on disk"))
                print(yellow("  FIX: Restart the gateway service to reload certs"))
                print("    sudo systemctl restart somniguard")
    else:
        print(red(f"✗ {msg}"))

    # Step 6: Final summary
    print_section("Summary & Next Steps")

    # Check if Pico needs reupload
    needs_reupload = False
    if ENCRYPTED_DIR.exists():
        config_enc = ENCRYPTED_DIR / "config.enc"
        if config_enc.exists():
            if config_enc.stat().st_mtime < PICO_CONFIG.stat().st_mtime:
                needs_reupload = True

    if pico_fp != gw_fp or needs_reupload:
        print(red(bold("ACTION REQUIRED:")))
        print()
        print("Run these commands in order:")
        print()
        print(green("  # 1. Update config.py with correct cert"))
        print("  python3 scripts/embed_pico_cert.py")
        print()
        print(green("  # 2. Get Pico's unique ID"))
        print("  mpremote connect /dev/cu.usbmodem* run -c \"import machine; print(machine.unique_id().hex())\"")
        print()
        print(green("  # 3. Re-encrypt firmware"))
        print("  python3 scripts/encrypt_pico_files.py --uid <pico-id>")
        print()
        print(green("  # 4. Upload to Pico"))
        print("  mpremote connect /dev/cu.usbmodem* fs cp -r encrypted_deploy/. :")
        print()
        print(green("  # 5. Restart Pico"))
        print("  mpremote connect /dev/cu.usbmodem* reset")
        print()
        print(green("  # 6. Watch for success"))
        print("  mpremote connect /dev/cu.usbmodem* monitor")
    else:
        print(green(bold("✓ Everything looks good!")))
        print()
        print("If TLS still fails on Pico, the issue may be:")
        print("  - Gateway needs restart (cert reloaded)")
        print("  - Pico has stale encrypted files")
        print("  - Pico time is wrong")
        print()
        print("Try:")
        print("  1. sudo systemctl restart somniguard  # on gateway")
        print("  2. Re-upload encrypted files to Pico")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
