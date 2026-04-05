# SOMNI‑Guard Tailscale Setup Guide

> **Educational prototype — not a clinically approved device.**

This guide explains how to set up the Tailscale peer-to-peer VPN overlay for
SOMNI‑Guard so that clinician laptops and developer machines can securely
reach the Pi 5 gateway dashboard — without opening firewall ports, configuring
NAT, or exposing the Pi 5 on the public internet.

---

## 1. Why Tailscale?

| Requirement | Tailscale solution |
|-------------|-------------------|
| Dashboard must only be reachable by authorised devices | Only devices enrolled in your tailnet can connect |
| No cloud — all data stays on the Pi 5 | Tailscale does **not** route traffic through its servers; it uses direct WireGuard P2P tunnels |
| Encrypted in transit | WireGuard — ChaCha20-Poly1305 authenticated encryption |
| No static IPs or port-forwarding on the hospital/home LAN | Stable Tailscale IPs (100.x.x.x) regardless of LAN topology |
| Works through firewalls and CGNAT | Tailscale's DERP relay infrastructure handles NAT traversal |
| Simple certificate management | MagicDNS provides stable hostnames; mTLS between all nodes is automatic |

### What Tailscale does **not** replace

- **HMAC-authenticated Pico → Pi 5 telemetry**: The Pico 2 W cannot run
  Tailscale (MicroPython, no native binary).  It sends data to the Pi 5 over
  the **local LAN** with HMAC-SHA256 packet authentication.  This is TB2 in
  the SOMNI‑Guard trust boundary model.
- **Database encryption**: SQLCipher and LUKS2 still protect data at rest.
- **Flask authentication**: Username + bcrypt password is still required even
  after entering the tailnet.

---

## 2. Network Architecture

```
┌────────────────────────────────────────────────────────────────────────────┐
│          Bedside LAN segment (e.g. home Wi-Fi or hospital VLAN)            │
│                                                                             │
│  [Pico 2 W]──Wi-Fi──HMAC-HTTP──►[Pi 5 Gateway]                             │
│                                       │                                    │
│                                 tailscaled                                 │
│                          Tailscale IP: 100.x.x.x                           │
└───────────────────────────────────────┼────────────────────────────────────┘
                    (encrypted WireGuard P2P tunnels)
         ┌─────────────────────┬─────────────────────┐
         │                     │                     │
 [Developer Laptop 1]  [Developer Laptop 2]  [Future Pi 5 replica]
  Tailscale installed    Tailscale installed    Tailscale installed
  hits 100.x.x.x:5000   hits 100.x.x.x:5000
```

**Trust boundaries:**

| Boundary | Path | Protection |
|----------|------|-----------|
| TB2 | Pico → Pi 5 | Local LAN + HMAC-SHA256 |
| TB5 (new) | Pi 5 ↔ Clinician/Developer | Tailscale WireGuard mTLS |

---

## 3. Prerequisites

- A Tailscale account (free tier supports up to 100 devices).
  Sign up at <https://tailscale.com>.
- Raspberry Pi 5 running Raspberry Pi OS (Bookworm recommended).
- Internet access on the Pi 5 **during setup** (the install script fetches
  Tailscale binaries).  After setup, Tailscale P2P works without internet if
  both peers are on the same LAN.
- A developer/clinician laptop (Windows, macOS, or Linux).

---

## 4. Pi 5 Setup (one time)

### 4a. Run the automated setup script

```bash
# Clone the repo or copy the scripts/ directory to the Pi 5
cd NightWatchGaurd

sudo chmod +x scripts/setup_tailscale_pi5.sh
sudo ./scripts/setup_tailscale_pi5.sh
```

The script will:
1. Install Tailscale via the official install script.
2. Enable the `tailscaled` systemd service.
3. Run `tailscale up --ssh --hostname=somni-pi5` — a browser URL will be printed.
4. Write `/etc/somniguard/env` with generated secret keys and `SOMNI_TAILSCALE_ONLY=true`.
5. Print the Pi 5's Tailscale IP and MagicDNS hostname.

### 4b. Manual setup (if you prefer not to run the script)

```bash
# 1. Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sh

# 2. Authenticate (opens a browser URL)
sudo tailscale up --ssh --hostname=somni-pi5

# 3. Note your Tailscale IP
tailscale ip -4          # e.g. 100.104.32.11

# 4. Start the gateway in Tailscale-only mode
export SOMNI_TAILSCALE_ONLY=true
export SOMNI_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export SOMNI_HMAC_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
cd somniguard_gateway
python run.py
```

### 4c. Verify Tailscale is running

```bash
tailscale status          # shows all connected peers
tailscale ping <peer-ip>  # round-trip latency to a peer
```

---

## 5. Developer / Clinician Laptop Setup

Install Tailscale on every laptop that needs access to the dashboard.

| Platform | Install |
|----------|---------|
| macOS | Download from <https://tailscale.com/download> or `brew install tailscale` |
| Windows | Download installer from <https://tailscale.com/download> |
| Ubuntu/Debian | `curl -fsSL https://tailscale.com/install.sh \| sh` |

Sign in with the **same Tailscale account** as the Pi 5.  Both devices will
now appear in your tailnet and can reach each other directly.

```bash
# Verify connectivity to the Pi 5
tailscale ping somni-pi5        # by MagicDNS hostname
tailscale ping 100.x.x.x        # by Tailscale IP
```

---

## 6. Accessing the Dashboard

Once both the Pi 5 and your laptop are on the same tailnet:

```
http://100.x.x.x:5000/          # using Tailscale IP
http://somni-pi5.your-tailnet.ts.net:5000/   # using MagicDNS
```

Sign in with your SOMNI‑Guard username and password.  No VPN client separate
from Tailscale is needed; the WireGuard tunnel is established automatically.

> **TAILSCALE_ONLY mode**: When `SOMNI_TAILSCALE_ONLY=true`, the gateway
> rejects HTTP requests from any IP outside `100.64.0.0/10` (the Tailscale
> CGNAT range) or `127.0.0.1`.  Pico telemetry API endpoints (`/api/*`)
> additionally accept private LAN IPs (RFC 1918) because the Pico cannot
> run Tailscale.

---

## 7. Tailscale ACL Policy (recommended)

By default all tailnet nodes can reach all others.  For SOMNI‑Guard, apply
a narrower policy in the [Tailscale admin console](https://login.tailscale.com/admin/acls):

```json
{
  "acls": [
    {
      "action": "accept",
      "src": ["tag:somni-clinician", "tag:somni-dev"],
      "dst": ["tag:somni-gateway:5000"]
    }
  ],
  "tagOwners": {
    "tag:somni-gateway":   ["autogroup:admin"],
    "tag:somni-clinician": ["autogroup:admin"],
    "tag:somni-dev":       ["autogroup:admin"]
  }
}
```

Apply the `somni-gateway` tag to the Pi 5 node, and `somni-clinician` /
`somni-dev` tags to the appropriate laptops.  This ensures that only tagged
devices can reach port 5000 on the Pi 5, even within the tailnet.

---

## 8. Configuring the Pico (no Tailscale needed)

The Pico 2 W connects to the Pi 5 over the **local Wi-Fi LAN**, not over
Tailscale.  In `somniguard_pico/config.py`:

```python
# Use the Pi 5's LAN IP (not the Tailscale IP) for Pico → Gateway telemetry
GATEWAY_HOST = "192.168.1.100"   # Pi 5's local LAN IP
GATEWAY_PORT = 5000

# This key must match SOMNI_HMAC_KEY in /etc/somniguard/env on the Pi 5
GATEWAY_HMAC_KEY = "paste-your-hmac-key-here"
```

> **Why not the Tailscale IP?** The Pico 2 W uses MicroPython and cannot
> run the Tailscale daemon.  Its traffic to the Pi 5 stays on the local
> LAN segment and is authenticated by HMAC-SHA256, providing integrity
> without needing Tailscale.

---

## 9. Security Checklist

- [ ] `SOMNI_TAILSCALE_ONLY=true` set in `/etc/somniguard/env`
- [ ] `SOMNI_SECRET_KEY` is 32+ random bytes (not the default dev value)
- [ ] `SOMNI_HMAC_KEY` matches `GATEWAY_HMAC_KEY` in Pico config
- [ ] Tailscale ACL policy restricts port 5000 to authorised tags only
- [ ] Tailscale SSH enabled (`tailscale up --ssh`) so developers don't need password-based SSH
- [ ] LUKS2 disk encryption enabled on the Pi 5 SD card
- [ ] Gateway runs as a dedicated `somniguard` system user (not root)

---

## 10. Troubleshooting

| Problem | Fix |
|---------|-----|
| `tailscale status` shows "Stopped" | `sudo systemctl start tailscaled` |
| Dashboard returns HTTP 403 "connect via Tailscale" | Your laptop is not on the tailnet, or `TAILSCALE_ONLY=false` is not set for dev mode |
| Pico telemetry rejected (HTTP 403) | Add the Pico's LAN CIDR to `SOMNI_PICO_CIDRS` env var (default covers 192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12) |
| MagicDNS name does not resolve | Run `tailscale up --accept-dns=true` on the Pi 5 |
| Pi 5 and laptop on same LAN but Tailscale ping fails | Check that `tailscaled` is running on both and both are in the same tailnet account |
