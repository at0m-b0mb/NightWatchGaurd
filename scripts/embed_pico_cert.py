"""
embed_pico_cert.py — copy gateway PKI material into the Pico config.

Reads:
    somniguard_gateway/certs/ca.crt
    somniguard_gateway/certs/pico_client.crt
    somniguard_gateway/certs/pico_client.key

Writes (in place, atomic):
    somniguard_pico/config.py
        GATEWAY_CA_CERT_PEM    = "..."   # trust anchor
        PICO_CLIENT_CERT_PEM   = "..."   # mTLS client cert
        PICO_CLIENT_KEY_PEM    = "..."   # mTLS client private key

After running this, encrypt the firmware (scripts/encrypt_pico_files.py)
so the client private key is encrypted at rest under the device's
hardware-derived AES-256 key.

Usage:
    python3 scripts/embed_pico_cert.py
    python3 scripts/embed_pico_cert.py --check     # print fingerprints, do not write
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import sys
from pathlib import Path

REPO_ROOT   = Path(__file__).resolve().parent.parent
CERTS_DIR   = REPO_ROOT / "somniguard_gateway" / "certs"
PICO_CONFIG = REPO_ROOT / "somniguard_pico" / "config.py"

CA_CERT      = CERTS_DIR / "ca.crt"
CLIENT_CERT  = CERTS_DIR / "pico_client.crt"
CLIENT_KEY   = CERTS_DIR / "pico_client.key"

_BLOCK_RES = {
    "GATEWAY_CA_CERT_PEM":  re.compile(r'GATEWAY_CA_CERT_PEM\s*=\s*"""(.*?)"""', re.DOTALL),
    "PICO_CLIENT_CERT_PEM": re.compile(r'PICO_CLIENT_CERT_PEM\s*=\s*"""(.*?)"""', re.DOTALL),
    "PICO_CLIENT_KEY_PEM":  re.compile(r'PICO_CLIENT_KEY_PEM\s*=\s*"""(.*?)"""', re.DOTALL),
}


def _sha256_fingerprint(pem_bytes: bytes) -> str:
    body = b"".join(
        line for line in pem_bytes.splitlines()
        if line and not line.startswith(b"-----")
    )
    der = base64.b64decode(body)
    digest = hashlib.sha256(der).hexdigest()
    return ":".join(digest[i:i+2] for i in range(0, len(digest), 2))


def _read_pem(path: Path, expect: str) -> bytes:
    if not path.is_file():
        print(f"[!] Missing: {path}")
        print("[!] Run `python3 scripts/setup_gateway_certs.py` first.")
        sys.exit(1)
    data = path.read_bytes()
    if expect.encode() not in data:
        print(f"[!] {path} does not look like {expect}.")
        sys.exit(1)
    return data


def _replace_block(src: str, name: str, pem_bytes: bytes) -> str:
    pem_text = pem_bytes.decode("utf-8").rstrip() + "\n"
    new_block = f'{name} = """{pem_text}"""'
    pattern = _BLOCK_RES[name]
    if not pattern.search(src):
        print(f"[!] Could not find {name} block in Pico config.py.")
        print(f"[!] Add a placeholder:  {name} = \"\"\"\"\"\"")
        sys.exit(1)
    new_src, n = pattern.subn(new_block, src, count=1)
    if n != 1:
        print(f"[!] Unexpected replacement count for {name}: {n}")
        sys.exit(1)
    return new_src


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--ca-cert",     default=str(CA_CERT))
    p.add_argument("--client-cert", default=str(CLIENT_CERT))
    p.add_argument("--client-key",  default=str(CLIENT_KEY))
    p.add_argument("--config",      default=str(PICO_CONFIG))
    p.add_argument("--check", action="store_true",
                   help="Print fingerprints; do not modify config.")
    args = p.parse_args()

    ca_pem  = _read_pem(Path(args.ca_cert),     "BEGIN CERTIFICATE")
    cli_pem = _read_pem(Path(args.client_cert), "BEGIN CERTIFICATE")
    key_pem = _read_pem(Path(args.client_key),  "BEGIN")

    print(f"[+] CA cert      : {args.ca_cert}")
    print(f"[+]   SHA-256    : {_sha256_fingerprint(ca_pem)}")
    print(f"[+] Client cert  : {args.client_cert}")
    print(f"[+]   SHA-256    : {_sha256_fingerprint(cli_pem)}")
    print(f"[+] Client key   : {args.client_key} ({len(key_pem)} bytes)")

    if args.check:
        return 0

    config_path = Path(args.config)
    if not config_path.is_file():
        print(f"[!] Pico config not found: {config_path}")
        return 1

    src = config_path.read_text(encoding="utf-8")
    src = _replace_block(src, "GATEWAY_CA_CERT_PEM",  ca_pem)
    src = _replace_block(src, "PICO_CLIENT_CERT_PEM", cli_pem)
    src = _replace_block(src, "PICO_CLIENT_KEY_PEM",  key_pem)

    tmp = config_path.with_suffix(".py.tmp")
    tmp.write_text(src, encoding="utf-8")
    os.replace(tmp, config_path)

    print(f"[+] Updated      : {config_path}")
    print("[+] Next:")
    print("[+]   1. python3 scripts/encrypt_pico_files.py     # encrypt config.py → config.enc")
    print("[+]   2. mpremote connect /dev/cu.usbmodem* fs cp -r somniguard_pico/. :")
    return 0


if __name__ == "__main__":
    sys.exit(main())
