"""
hotspot.py — Raspberry Pi 5 Wi-Fi hotspot management for SOMNI-Guard.

Creates a WPA2 access point (SSID: SomniGuard_Net) using NetworkManager
(nmcli).  A random 16-character password is generated on first run and saved
to a credentials file so the Pico 2W can be configured with matching details.

Environment overrides:
    SOMNI_HOTSPOT        Set to "false" to skip hotspot startup (default: true)
    SOMNI_HOTSPOT_SSID   Override the SSID (default: SomniGuard_Net)
    SOMNI_HOTSPOT_IFACE  Wi-Fi interface name (default: wlan0)
    SOMNI_HOTSPOT_CREDS  Path to the credentials JSON file

Requires NetworkManager running on the Pi (default on Raspberry Pi OS Bookworm).
"""

import json
import os
import secrets
import string
import subprocess

SSID      = os.environ.get("SOMNI_HOTSPOT_SSID",  "SomniGuard_Net")
IFACE     = os.environ.get("SOMNI_HOTSPOT_IFACE", "wlan0")
CON_NAME  = "SomniGuard_Hotspot"
CREDS_PATH = os.environ.get(
    "SOMNI_HOTSPOT_CREDS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "hotspot_credentials.json"),
)


def _generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _load_or_create_credentials() -> tuple:
    if os.path.exists(CREDS_PATH):
        with open(CREDS_PATH) as f:
            data = json.load(f)
        print("[SOMNI][HOTSPOT] Credentials loaded from {}".format(CREDS_PATH))
        return data["ssid"], data["password"]

    password = _generate_password()
    os.makedirs(os.path.dirname(os.path.abspath(CREDS_PATH)), exist_ok=True)
    with open(CREDS_PATH, "w") as f:
        json.dump({"ssid": SSID, "password": password}, f, indent=2)
    os.chmod(CREDS_PATH, 0o600)
    print("[SOMNI][HOTSPOT] New credentials generated → {}".format(CREDS_PATH))
    return SSID, password


def _nmcli(*args) -> tuple:
    result = subprocess.run(
        ["nmcli"] + list(args),
        capture_output=True, text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _connection_exists() -> bool:
    rc, _, _ = _nmcli("con", "show", CON_NAME)
    return rc == 0


def start_hotspot() -> tuple:
    """
    Start the SomniGuard Wi-Fi hotspot via NetworkManager.

    Generates a WPA2 password on first run (saves it to CREDS_PATH).
    On subsequent runs the saved password is reused so the Pico does not need
    to be reconfigured.  If nmcli is unavailable (e.g. running on a dev Mac)
    the error is printed and the function returns without raising.

    Returns:
        tuple[str, str]: (ssid, password) — empty strings if startup failed.
    """
    ssid, password = _load_or_create_credentials()

    # Remove any stale connection so settings are always applied fresh.
    if _connection_exists():
        _nmcli("con", "delete", CON_NAME)

    steps = [
        # Create the Wi-Fi connection entry
        ["con", "add", "type", "wifi", "ifname", IFACE,
         "con-name", CON_NAME, "autoconnect", "no", "ssid", ssid],
        # Put it in AP (access-point) mode and share the Pi's internet
        ["con", "modify", CON_NAME,
         "802-11-wireless.mode", "ap",
         "802-11-wireless.band", "bg",
         "ipv4.method", "shared"],
        # Apply WPA2 password
        ["con", "modify", CON_NAME,
         "wifi-sec.key-mgmt", "wpa-psk",
         "wifi-sec.psk", password],
        # Bring it up
        ["con", "up", CON_NAME],
    ]

    for step in steps:
        rc, _, err = _nmcli(*step)
        if rc != 0:
            print("[SOMNI][HOTSPOT][WARN] nmcli '{}' failed: {}".format(
                " ".join(step[:3]), err or "(no output)"))
            print("[SOMNI][HOTSPOT][WARN] Hotspot not started — continuing without Wi-Fi AP.")
            print("[SOMNI][HOTSPOT][WARN] On the Pi, ensure NetworkManager is running: "
                  "sudo systemctl start NetworkManager")
            return "", ""

    _print_hotspot_banner(ssid, password)
    return ssid, password


def stop_hotspot() -> None:
    """Bring down the SomniGuard hotspot connection if it is active."""
    if _connection_exists():
        _nmcli("con", "down", CON_NAME)
        print("[SOMNI][HOTSPOT] Hotspot stopped.")


def _print_hotspot_banner(ssid: str, password: str) -> None:
    border = "=" * 50
    print("")
    print(border)
    print("  SOMNI-Guard Hotspot Active")
    print("  SSID    : {}".format(ssid))
    print("  Password: {}".format(password))
    print("  Pi IP   : 10.42.0.1  (typically)")
    print("")
    print("  Connect the Pico 2W to this network.")
    print("  Credentials file: {}".format(CREDS_PATH))
    print(border)
    print("")
