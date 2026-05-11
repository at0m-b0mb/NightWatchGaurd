"""
hotspot.py — Raspberry Pi 5 Wi-Fi hotspot management for SOMNI-Guard.

Creates a WPA2 access point (SSID: SomniGuard_Net) using NetworkManager
(nmcli).  A random 16-character password is generated on first run and saved
to a credentials file so the Pico 2W can be configured with matching details.

Privilege handling
------------------
nmcli requires root to create or modify AP connections.  This module detects
whether the current process is running as root (e.g. via sudo) and, if not,
automatically prefixes nmcli calls with ``sudo``.  The recommended setup is a
narrow sudoers rule that allows the gateway user to run nmcli without a
password prompt:

    # /etc/sudoers.d/somniguard-nmcli
    pi ALL=(ALL) NOPASSWD: /usr/bin/nmcli

Run ``sudo bash scripts/setup_gateway_pi5.sh`` to install this rule and the
systemd service in one step.

Auto-start on reboot
--------------------
After the first successful run, the hotspot connection is stored in
NetworkManager with ``autoconnect yes``.  On every subsequent reboot,
NetworkManager brings the AP up automatically — no Python code is needed.
``start_hotspot()`` checks whether the AP is already active and returns
immediately if so, avoiding unnecessary teardown and recreation.

Environment overrides
---------------------
    SOMNI_HOTSPOT        Set to "false" to skip hotspot startup (default: true)
    SOMNI_HOTSPOT_SSID   Override the SSID (default: SomniGuard_Net)
    SOMNI_HOTSPOT_IFACE  Wi-Fi interface name (default: wlan0)
    SOMNI_HOTSPOT_CREDS  Path to the credentials JSON file
    SOMNI_HOTSPOT_PASSWORD  Fixed password — overrides random generation so
                            Pico config and hotspot share the same credential
                            without manual editing.

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


def _default_creds_path() -> str:
    """Pick a credentials path that is writable under a hardened systemd unit.

    ``/var/lib/somniguard/hotspot_credentials.json`` is already in the
    unit's ``ReadWritePaths``, so the service user can read/write it on
    every boot.  If that directory does not exist (dev install on a
    non-Pi machine), fall back to the legacy in-tree path.
    """
    legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "hotspot_credentials.json")
    sysdir = "/var/lib/somniguard"
    if os.path.isdir(sysdir):
        # Migrate an existing legacy file once so we don't roll a new
        # password and break the Pico's saved WIFI_PASSWORD.
        sys_path = os.path.join(sysdir, "hotspot_credentials.json")
        if os.path.isfile(legacy) and not os.path.isfile(sys_path):
            try:
                import shutil
                shutil.copy2(legacy, sys_path)
                os.chmod(sys_path, 0o600)
            except Exception:
                pass
        return sys_path
    return legacy


CREDS_PATH = os.environ.get("SOMNI_HOTSPOT_CREDS", _default_creds_path())
_FIXED_PASSWORD = os.environ.get("SOMNI_HOTSPOT_PASSWORD", "")


def _generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _load_or_create_credentials() -> tuple:
    if _FIXED_PASSWORD:
        print("[SOMNI][HOTSPOT] Using fixed password from SOMNI_HOTSPOT_PASSWORD.")
        return SSID, _FIXED_PASSWORD

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
    print("[SOMNI][HOTSPOT] Update WIFI_PASSWORD in somniguard_pico/config.py to: {}".format(password))
    return SSID, password


def _nmcli(*args) -> tuple:
    """Run ``nmcli`` and return ``(rc, stdout, stderr)``.

    We deliberately do NOT prefix with ``sudo``.  Under a hardened systemd
    unit (``NoNewPrivileges=true``, ``RestrictSUIDSGID=true``) the kernel
    refuses to honour setuid bits, so ``sudo nmcli`` would fail with
    "effective uid is not 0".  Instead, the gateway's service user
    (``somniguard``) is added to the ``netdev`` group by
    ``setup_gateway.sh`` — NetworkManager's default polkit rules grant
    that group both ``network-control`` and ``system-connection-modify``,
    so plain ``nmcli`` works without escalation.
    """
    cmd = ["nmcli"] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _connection_exists() -> bool:
    rc, _, _ = _nmcli("con", "show", CON_NAME)
    return rc == 0


def _hotspot_is_active() -> bool:
    """Return True if the hotspot connection is currently activated."""
    rc, out, _ = _nmcli("con", "show", "--active", CON_NAME)
    return rc == 0 and bool(out)


def start_hotspot() -> tuple:
    """
    Start the SomniGuard Wi-Fi hotspot via NetworkManager.

    Behaviour
    ---------
    - If the hotspot is already active, returns immediately with the saved
      credentials (NetworkManager auto-started it on boot — no work needed).
    - If the connection profile exists but is down, brings it up.
    - If the connection profile does not exist, creates it with WPA2-PSK /
      AES-CCMP settings (PMF disabled for CYW43 compatibility) and sets
      ``autoconnect yes`` so it comes up automatically on every reboot.

    Privilege
    ---------
    Calls ``sudo nmcli`` automatically when not running as root.
    Requires the gateway user to have passwordless sudo for nmcli:

        sudo bash scripts/setup_gateway_pi5.sh

    Returns
    -------
    tuple[str, str]: (ssid, password) — empty strings if startup failed.
    """
    ssid, password = _load_or_create_credentials()

    # Fast path: hotspot is already up (NetworkManager auto-started it on boot).
    if _hotspot_is_active():
        print("[SOMNI][HOTSPOT] Hotspot already active — skipping setup.")
        _print_hotspot_banner(ssid, password)
        return ssid, password

    # Slow path: profile exists but is down — just bring it up.
    if _connection_exists():
        print("[SOMNI][HOTSPOT] Bringing existing hotspot connection up…")
        rc, _, err = _nmcli("con", "up", CON_NAME)
        if rc == 0:
            _print_hotspot_banner(ssid, password)
            return ssid, password
        _print_nmcli_error("con up", err)
        return "", ""

    # First run: create the connection profile and bring it up.
    print("[SOMNI][HOTSPOT] Creating hotspot connection profile…")
    steps = [
        # Create the Wi-Fi connection entry with autoconnect so NetworkManager
        # brings the AP up on every boot without any Python code.
        ["con", "add", "type", "wifi", "ifname", IFACE,
         "con-name", CON_NAME, "autoconnect", "yes", "ssid", ssid],
        # AP mode — share the Pi's uplink (if any) via NAT.
        # ipv4.method=shared is what tells NetworkManager to spawn dnsmasq-base
        # for DHCP+DNS on this interface.  We ALSO set an explicit address so
        # the gateway IP is deterministic across NM versions (some Bookworm
        # builds have flaky default-subnet selection in shared mode).
        # ipv6.method=ignore avoids RA/DHCPv6 surprises on the hotspot.
        ["con", "modify", CON_NAME,
         "802-11-wireless.mode", "ap",
         "802-11-wireless.band", "bg",
         "ipv4.method",     "shared",
         "ipv4.addresses",  "10.42.0.1/24",
         "ipv4.gateway",    "",
         "ipv6.method",     "ignore"],
        # WPA2-only, AES-CCMP, PMF disabled.  Without these, NetworkManager
        # may negotiate WPA3-transition or PMF-optional modes that the
        # Pico 2W's CYW43 chip cannot authenticate against.
        ["con", "modify", CON_NAME,
         "wifi-sec.key-mgmt", "wpa-psk",
         "wifi-sec.psk", password,
         "wifi-sec.proto", "rsn",
         "wifi-sec.pairwise", "ccmp",
         "wifi-sec.group", "ccmp",
         "wifi-sec.pmf", "disable"],
        # Bring it up for this session (future reboots: NetworkManager handles it).
        ["con", "up", CON_NAME],
    ]

    for step in steps:
        rc, _, err = _nmcli(*step)
        if rc != 0:
            _print_nmcli_error(" ".join(step[:3]), err)
            return "", ""

    # Sanity-check the DHCP path that hotspot clients depend on.  A silent
    # missing dnsmasq-base is the #1 cause of "I joined the SSID but my
    # phone got 169.254.x.x" reports.
    _check_dhcp_health()

    # Write the dnsmasq DNS override so that every hotspot client can resolve
    # somniguard.local via regular DNS (not just mDNS which is unreliable on
    # Windows / Android).  NetworkManager reads from this directory when it
    # spawns its internal dnsmasq for shared-mode hotspots.
    _write_dnsmasq_dns_config()

    _print_hotspot_banner(ssid, password)
    return ssid, password


def _check_dhcp_health() -> None:
    """Diagnose the most common reasons a hotspot client never gets a lease.

    NetworkManager's shared mode quietly relies on:
      1. The ``dnsmasq-base`` package being installed.
      2. No conflicting standalone ``dnsmasq`` service occupying UDP 67.
      3. The firewall not dropping inbound DHCP/DNS on the AP interface.

    We don't try to *fix* these — we just print actionable warnings so the
    operator knows exactly what to do.  This runs once at hotspot creation
    and is cheap enough to be harmless.
    """
    # 1. dnsmasq-base — NM's shared mode binary
    rc = subprocess.run(
        ["dpkg", "-s", "dnsmasq-base"],
        capture_output=True, text=True,
    ).returncode
    if rc != 0:
        print("[SOMNI][HOTSPOT][WARN] dnsmasq-base is NOT installed — "
              "shared mode will not serve DHCP and clients will get APIPA "
              "(169.254.x.x).  Fix:  sudo apt install -y dnsmasq-base")

    # 2. Conflicting standalone dnsmasq.service
    rc = subprocess.run(
        ["systemctl", "is-active", "--quiet", "dnsmasq.service"],
    ).returncode
    if rc == 0:
        print("[SOMNI][HOTSPOT][WARN] A standalone dnsmasq.service is RUNNING — "
              "it competes with NetworkManager's hotspot DHCP and prevents "
              "leases.  Fix:  sudo systemctl disable --now dnsmasq")

    # 3. UFW with default-deny that forgot to allow the AP interface.
    rc = subprocess.run(
        ["ufw", "status"], capture_output=True, text=True,
    )
    if rc.returncode == 0 and "Status: active" in rc.stdout:
        if (f"Anywhere on {IFACE}" not in rc.stdout
                and "67/udp" not in rc.stdout):
            print("[SOMNI][HOTSPOT][WARN] UFW is active and does not appear "
                  "to allow DHCP (UDP 67) on '{}'.  Hotspot clients will not "
                  "get an IP.  Fix:".format(IFACE))
            print("[SOMNI][HOTSPOT][WARN]   sudo ufw allow in on {}".format(IFACE))
            print("[SOMNI][HOTSPOT][WARN]   sudo ufw reload")


_DNSMASQ_CONF_DIR  = "/etc/NetworkManager/dnsmasq-shared.d"
_DNSMASQ_CONF_FILE = _DNSMASQ_CONF_DIR + "/somniguard.conf"
_DNSMASQ_CONF_CONTENT = (
    "# SOMNI-Guard: resolve somniguard.local → gateway for ALL hotspot clients.\n"
    "# This makes the .local name work on Windows/Android (no mDNS needed).\n"
    "address=/somniguard.local/10.42.0.1\n"
)


def _write_dnsmasq_dns_config() -> None:
    """Write a dnsmasq address record so somniguard.local resolves for all clients.

    NetworkManager's shared-mode dnsmasq reads config files from
    /etc/NetworkManager/dnsmasq-shared.d/ before starting.  Writing one
    address record here means every device that joins SomniGuard_Net can
    resolve somniguard.local via normal DNS — no Bonjour/mDNS required.

    Requires root (or the process running as root).  Prints a warning and
    continues if the write fails (e.g. non-root dev environment).
    """
    try:
        os.makedirs(_DNSMASQ_CONF_DIR, mode=0o755, exist_ok=True)
        existing = ""
        if os.path.isfile(_DNSMASQ_CONF_FILE):
            with open(_DNSMASQ_CONF_FILE) as fh:
                existing = fh.read()
        if existing == _DNSMASQ_CONF_CONTENT:
            return  # already correct — nothing to do
        with open(_DNSMASQ_CONF_FILE, "w") as fh:
            fh.write(_DNSMASQ_CONF_CONTENT)
        os.chmod(_DNSMASQ_CONF_FILE, 0o644)
        print("[SOMNI][HOTSPOT] dnsmasq DNS config written: somniguard.local → 10.42.0.1")
    except PermissionError:
        print("[SOMNI][HOTSPOT][WARN] Could not write dnsmasq config (not root). "
              "Run the setup script once as root to enable somniguard.local DNS.")
    except Exception as exc:
        print("[SOMNI][HOTSPOT][WARN] dnsmasq config write failed: {}".format(exc))


def stop_hotspot() -> None:
    """Bring down the SomniGuard hotspot connection if it is active."""
    if _hotspot_is_active():
        _nmcli("con", "down", CON_NAME)
        print("[SOMNI][HOTSPOT] Hotspot stopped.")


def _print_nmcli_error(step: str, err: str) -> None:
    print("[SOMNI][HOTSPOT][ERROR] nmcli '{}' failed: {}".format(
        step, err or "(no output)"))
    if "insufficient privileges" in err.lower() or "not authorized" in err.lower():
        print("[SOMNI][HOTSPOT][ERROR] nmcli needs root privileges.")
        print("[SOMNI][HOTSPOT][ERROR] Fix: run the setup script once:")
        print("[SOMNI][HOTSPOT][ERROR]   sudo bash scripts/setup_gateway_pi5.sh")
        print("[SOMNI][HOTSPOT][ERROR] Or run the gateway with sudo for a one-off test:")
        print("[SOMNI][HOTSPOT][ERROR]   sudo python run.py")
    else:
        print("[SOMNI][HOTSPOT][ERROR] Hotspot not started — continuing without Wi-Fi AP.")
        print("[SOMNI][HOTSPOT][ERROR] Ensure NetworkManager is running:")
        print("[SOMNI][HOTSPOT][ERROR]   sudo systemctl start NetworkManager")


def _print_hotspot_banner(ssid: str, password: str) -> None:
    border = "=" * 54
    print("")
    print(border)
    print("  SOMNI-Guard Hotspot Active")
    print("  SSID    : {}".format(ssid))
    print("  Password: {}".format(password))
    print("  Pi IP   : 10.42.0.1")
    print("")
    print("  Dashboard (HTTPS only):")
    print("    https://10.42.0.1:5443/")
    print("    https://somniguard.local:5443/")
    print("")
    print("  First-time: click 'Advanced > Proceed' on the browser warning,")
    print("  or download the CA cert from https://10.42.0.1:5443/ca.crt")
    print("  and install it in your OS trust store to remove the warning.")
    print("")
    print("  Credentials file: {}".format(CREDS_PATH))
    print(border)
    print("")
