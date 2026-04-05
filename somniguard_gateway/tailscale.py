"""
tailscale.py — SOMNI‑Guard Tailscale VPN integration helpers.

Provides:
  • IP-range checks for the Tailscale CGNAT range (100.64.0.0/10).
  • Tailscale daemon status queries via ``tailscale status --json``.
  • Flask network-policy enforcement (``check_network_policy``).
  • Helpers for local Tailscale IP, hostname, and peer list.

Architecture note
-----------------
The Pico 2 W cannot run Tailscale directly (MicroPython / no native binary).
It sends HMAC-authenticated HTTP POST telemetry to the Pi 5 over the **local
Wi-Fi LAN segment** (same subnet, bedside).  The Tailscale overlay is used for:

  • Clinician / developer laptops ↔ Pi 5 dashboard (encrypted, mutually
    authenticated P2P tunnel).
  • Future networked nodes (e.g. a Linux bridge co-located with the Pico)
    that could relay Pico traffic over Tailscale.

Trust boundaries
----------------
  TB2 (existing): Pico → Pi 5 over local LAN (HMAC-authenticated).
  TB5 (new):      Pi 5 ↔ remote clients over Tailscale mesh VPN.

Educational prototype — not a clinically approved device.
"""

import ipaddress
import json
import subprocess

# ---------------------------------------------------------------------------
# IP range constants
# ---------------------------------------------------------------------------

#: Tailscale CGNAT range — RFC 6598 100.64.0.0/10 is reserved for carrier-grade
#: NAT and repurposed by Tailscale for all tailnet node addresses.  Every device
#: enrolled in any Tailscale tailnet receives an IP in this /10 block, making it
#: a reliable "is this a Tailscale peer?" signal on the local machine.
TAILSCALE_CIDR: ipaddress.IPv4Network = ipaddress.ip_network("100.64.0.0/10")

#: RFC 1918 + loopback ranges used to recognise Pico LAN traffic.
_PRIVATE_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]


# ---------------------------------------------------------------------------
# IP classification
# ---------------------------------------------------------------------------

def is_tailscale_ip(ip_str):
    """
    Return True if *ip_str* is within the Tailscale CGNAT range.

    All devices enrolled in a Tailscale tailnet receive an address in
    100.64.0.0/10 (Tailscale's private CGNAT range, never routed on the
    public internet).  This check is therefore a reliable proxy for
    "is this request from a Tailscale peer?".

    Args:
        ip_str (str): IPv4 or IPv6 address string.

    Returns:
        bool: True if the address is a Tailscale address.
    """
    try:
        return ipaddress.ip_address(ip_str) in TAILSCALE_CIDR
    except ValueError:
        return False


def is_private_lan_ip(ip_str):
    """
    Return True if *ip_str* is a private RFC 1918 or loopback address.

    Used to allow Pico telemetry from the local LAN segment even when
    TAILSCALE_ONLY mode is active for the web dashboard.

    Args:
        ip_str (str): IPv4 or IPv6 address string.

    Returns:
        bool: True if the address is private / loopback.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _PRIVATE_RANGES)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Tailscale daemon queries
# ---------------------------------------------------------------------------

def get_tailscale_status():
    """
    Query the Tailscale daemon via ``tailscale status --json``.

    Runs the Tailscale CLI as a subprocess with a 5-second timeout.
    Returns ``None`` if the binary is absent, the daemon is not running,
    the call times out, or the JSON cannot be parsed.

    Args:
        None

    Returns:
        dict | None: Parsed Tailscale status object, or None on any failure.
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout:
            return json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired,
            json.JSONDecodeError, PermissionError, OSError):
        pass
    return None


def tailscale_running():
    """
    Return True if the Tailscale daemon is running and authenticated.

    Checks that ``BackendState`` is ``"Running"`` and that ``Self`` (the
    local node record) is present in the status output.

    Args:
        None

    Returns:
        bool: True if Tailscale is active.
    """
    status = get_tailscale_status()
    if status is None:
        return False
    return (
        status.get("BackendState") == "Running"
        and status.get("Self") is not None
    )


def get_local_tailscale_ip():
    """
    Return the primary Tailscale IPv4 address assigned to this machine.

    Iterates ``Self.TailscaleIPs`` and returns the first IPv4 address.

    Args:
        None

    Returns:
        str | None: A ``100.x.x.x`` string, or None if Tailscale is not active.
    """
    status = get_tailscale_status()
    if status is None:
        return None
    self_node = status.get("Self") or {}
    for ip in self_node.get("TailscaleIPs", []):
        try:
            if ipaddress.ip_address(ip).version == 4:
                return ip
        except ValueError:
            pass
    return None


def get_tailscale_hostname():
    """
    Return the MagicDNS hostname of this machine in the tailnet.

    Prefers ``DNSName`` (the fully-qualified MagicDNS name) over the raw
    ``HostName``.

    Args:
        None

    Returns:
        str | None: E.g. ``"somni-pi5.your-tailnet.ts.net."``, or None.
    """
    status = get_tailscale_status()
    if status is None:
        return None
    self_node = status.get("Self") or {}
    return self_node.get("DNSName") or self_node.get("HostName")


def list_tailscale_peers():
    """
    Return a list of all currently known Tailscale peers.

    Each peer dict has the keys:
    ``HostName``, ``DNSName``, ``TailscaleIPs``, ``Online``, ``OS``.

    Args:
        None

    Returns:
        list[dict]: Peer info dicts (may be empty if Tailscale is unavailable).
    """
    status = get_tailscale_status()
    if status is None:
        return []
    peers = []
    for _nid, node in (status.get("Peer") or {}).items():
        peers.append({
            "HostName":     node.get("HostName", ""),
            "DNSName":      node.get("DNSName", ""),
            "TailscaleIPs": node.get("TailscaleIPs", []),
            "Online":       node.get("Online", False),
            "OS":           node.get("OS", ""),
        })
    return peers


# ---------------------------------------------------------------------------
# Flask network-access policy
# ---------------------------------------------------------------------------

def check_network_policy(remote_addr, tailscale_only, is_api_path=False,
                         pico_cidrs=None):
    """
    Evaluate the network-access policy for an incoming request.

    Policy rules (evaluated in order):

    1. Loopback (``127.0.0.1`` / ``::1``) — always allowed.
    2. Tailscale IP (``100.64.0.0/10``) — always allowed.
    3. ``tailscale_only = False`` (development mode) — all IPs allowed.
    4. ``tailscale_only = True`` **and** ``is_api_path = True`` —
       private-LAN IPs matching ``pico_cidrs`` are also allowed
       (to permit Pico HMAC-authenticated telemetry from the local LAN).
    5. All other IPs — denied (caller should return HTTP 403).

    Args:
        remote_addr    (str):        The client's remote IP address string.
        tailscale_only (bool):       Whether TAILSCALE_ONLY mode is active.
        is_api_path    (bool):       True when the request path starts with
                                     ``/api/`` (Pico telemetry endpoints).
        pico_cidrs     (list[str]):  CIDRs from which Pico LAN traffic may
                                     arrive (e.g. ``["192.168.0.0/16"]``).

    Returns:
        bool: True if the request should be allowed; False if it should be
              denied with HTTP 403.
    """
    remote = remote_addr or ""

    # Rule 1 — loopback always allowed
    if remote in ("127.0.0.1", "::1"):
        return True

    # Rule 2 — Tailscale peers always allowed
    if is_tailscale_ip(remote):
        return True

    # Rule 3 — development mode: permit everything
    if not tailscale_only:
        return True

    # Rule 4 — Tailscale-only mode, API path: allow Pico LAN CIDRs
    if is_api_path:
        try:
            addr = ipaddress.ip_address(remote)
            for cidr in (pico_cidrs or []):
                try:
                    if addr in ipaddress.ip_network(cidr, strict=False):
                        return True
                except ValueError:
                    pass
        except ValueError:
            pass

    # Rule 5 — deny
    return False
