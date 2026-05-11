#!/usr/bin/env python3
"""
embed_pico_config.py — update Pico WiFi, HMAC, and gateway settings.

Updates WIFI_SSID, WIFI_PASSWORD, GATEWAY_HMAC_KEY, and GATEWAY_HOST in
somniguard_pico/config.py with actual deployment values.

Usage:
    python3 scripts/embed_pico_config.py \\
        --ssid "MyNetwork" \\
        --password "MyPassword" \\
        --gateway-host "10.42.0.1" \\
        --hmac-key "abc123..." \\
        --generate-hmac

    python3 scripts/embed_pico_config.py --generate-hmac  # just print a new key
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PICO_CONFIG = REPO_ROOT / "somniguard_pico" / "config.py"


def generate_hmac_key() -> str:
    """Generate a cryptographically secure HMAC key (64 hex chars = 32 bytes)."""
    return secrets.token_hex(32)


def _update_config_value(src: str, pattern: str, replacement: str) -> tuple[str, int]:
    """Find and replace a config value using regex.

    Args:
        src: Source config text.
        pattern: Regex pattern to match the assignment (without quotes/values).
        replacement: New value (including quotes if needed).

    Returns:
        (updated_text, count) — count is 1 on success, 0 if not found.
    """
    # Match the pattern and capture everything up to the next assignment or comment
    regex = re.compile(
        rf'({pattern}\s*=\s*)([^\n]+)',
        re.MULTILINE,
    )
    new_src, count = regex.subn(rf'\g<1>{replacement}', src, count=1)
    return new_src, count


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--ssid", help="WiFi network name (WIFI_SSID)")
    p.add_argument("--password", help="WiFi password (WIFI_PASSWORD)")
    p.add_argument("--gateway-host", help="Gateway LAN IP (GATEWAY_HOST, e.g. 10.42.0.1)")
    p.add_argument("--hmac-key", help="HMAC shared secret (GATEWAY_HMAC_KEY)")
    p.add_argument("--generate-hmac", action="store_true",
                   help="Generate and print a new HMAC key, then exit")
    p.add_argument("--config", default=str(PICO_CONFIG),
                   help="Path to the Pico config.py (default: %(default)s).")
    args = p.parse_args()

    # If just generating a key, print it and exit
    if args.generate_hmac:
        key = generate_hmac_key()
        print(f"[+] Generated HMAC key: {key}")
        print("[+] Copy this to:")
        print("[+]   - GATEWAY_HMAC_KEY in somniguard_pico/config.py")
        print("[+]   - SOMNI_HMAC_KEY in /etc/somniguard/env on the gateway")
        return 0

    # If no updates requested, show help
    if not (args.ssid or args.password or args.gateway_host or args.hmac_key):
        p.print_help()
        return 1

    config_path = Path(args.config)
    if not config_path.is_file():
        print(f"[!] Pico config not found: {config_path}")
        return 1

    src = config_path.read_text(encoding="utf-8")
    updated = False

    # Update WIFI_SSID
    if args.ssid:
        new_src, count = _update_config_value(src, "WIFI_SSID", f'"{args.ssid}"')
        if count == 1:
            src = new_src
            print(f"[+] Updated WIFI_SSID to: {args.ssid}")
            updated = True
        else:
            print(f"[!] Could not find WIFI_SSID in config")
            return 1

    # Update WIFI_PASSWORD
    if args.password:
        new_src, count = _update_config_value(src, "WIFI_PASSWORD", f'"{args.password}"')
        if count == 1:
            src = new_src
            print(f"[+] Updated WIFI_PASSWORD (length: {len(args.password)})")
            updated = True
        else:
            print(f"[!] Could not find WIFI_PASSWORD in config")
            return 1

    # Update GATEWAY_HOST
    if args.gateway_host:
        new_src, count = _update_config_value(src, "GATEWAY_HOST", f'"{args.gateway_host}"')
        if count == 1:
            src = new_src
            print(f"[+] Updated GATEWAY_HOST to: {args.gateway_host}")
            updated = True
        else:
            print(f"[!] Could not find GATEWAY_HOST in config")
            return 1

    # Update GATEWAY_HMAC_KEY
    if args.hmac_key:
        new_src, count = _update_config_value(src, "GATEWAY_HMAC_KEY", f'"{args.hmac_key}"')
        if count == 1:
            src = new_src
            print(f"[+] Updated GATEWAY_HMAC_KEY")
            updated = True
        else:
            print(f"[!] Could not find GATEWAY_HMAC_KEY in config")
            return 1

    if not updated:
        print("[!] No changes made")
        return 1

    # Atomic write
    tmp = config_path.with_suffix(".py.tmp")
    tmp.write_text(src, encoding="utf-8")
    os.replace(tmp, config_path)

    print(f"[+] Updated: {config_path}")
    print("[+] Next steps:")
    print("[+]   1. python3 scripts/encrypt_pico_files.py --uid <pico-uid>")
    print("[+]   2. mpremote connect /dev/cu.usbmodem* fs cp -r encrypted_deploy/. :")
    print("[+]   3. Restart the Pico (Ctrl+D or unplug/replug)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
