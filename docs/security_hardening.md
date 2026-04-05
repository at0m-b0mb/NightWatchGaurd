# SOMNI-Guard Security Hardening Checklist

> **Educational prototype — not a clinically approved device.**
> The hardening steps described here represent a defence-in-depth architecture
> appropriate for a student research project. They are not a substitute for
> formal security review, regulatory validation, or the controls required for
> a clinically approved device under IEC 62443, IEC 62304, or applicable FDA
> cybersecurity guidance. Do not use SOMNI-Guard for clinical diagnosis,
> treatment, or any patient-safety purpose.

This document provides a comprehensive security hardening guide for deploying
the SOMNI-Guard sleep monitoring system. Follow this guide in order — the
earlier sections address the highest-impact items.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Pico 2W Hardening Checklist](#2-pico-2w-hardening-checklist)
3. [Pi 5 Gateway Hardening Checklist](#3-pi-5-gateway-hardening-checklist)
4. [Network Hardening](#4-network-hardening)
5. [Operational Security](#5-operational-security)
6. [Verification Commands](#6-verification-commands)

---

## 1. Overview

### Defence-in-depth architecture

SOMNI-Guard uses a layered security model. No single control is sufficient on
its own; the goal is to ensure that a successful attack on one layer still
requires defeating multiple additional layers before sensitive data is
accessible or patient-safety functions are compromised.

```
┌──────────────────────────────────────────────────────────────────┐
│  Layer 0 — Network Perimeter                                     │
│  Tailscale VPN, WPA3 Wi-Fi, firewall (ufw), VLAN isolation       │
├──────────────────────────────────────────────────────────────────┤
│  Layer 1 — Platform / OS                                         │
│  UEFI Secure Boot, LUKS2 disk encryption, dedicated service user │
│  hardware watchdog, firmware integrity check                      │
├──────────────────────────────────────────────────────────────────┤
│  Layer 2 — Application                                           │
│  HMAC-SHA256 telemetry auth, HTTPS/TLS, rate limiting            │
│  bcrypt passwords, CSRF protection, security headers             │
├──────────────────────────────────────────────────────────────────┤
│  Layer 3 — Data                                                  │
│  XTEA encrypted Pico config, SQLCipher database encryption       │
│  anti-replay nonces, audit logging                               │
├──────────────────────────────────────────────────────────────────┤
│  Layer 4 — Operational                                           │
│  Key rotation, log review, backup/recovery, incident response    │
└──────────────────────────────────────────────────────────────────┘
```

### How to use this document

Work through the checklists in order. Items marked with a checkbox (`[ ]`) are
tasks to perform. Items in tables are for reference. The
[Verification Commands](#6-verification-commands) section provides one-liners
you can paste into a terminal to confirm each hardening step is in effect.

---

## 2. Pico 2W Hardening Checklist

### 2.1 Change the default HMAC key

The HMAC key shared between the Pico and the Pi 5 gateway must be unique per
deployment. The default placeholder value (`CHANGE-ME-...`) in `config.py`
provides **no security**.

**Why it matters:** The HMAC key authenticates every telemetry packet. An
attacker who knows the key can inject or replay fabricated sensor readings.

**Steps:**

```python
# 1. Generate a strong key on your development machine:
python3 -c "import secrets; print(secrets.token_hex(32))"
# Example output: a3f9c218e7b1d4...  (64-character hex string)

# 2. Update /etc/somniguard/env on the Pi 5:
#    SOMNI_HMAC_KEY=<your-new-key>

# 3. Store the same key in encrypted Pico config (Step 2.2 below)
```

Checklist:
- [ ] Generated a cryptographically random HMAC key (32+ bytes).
- [ ] Updated `SOMNI_HMAC_KEY` in `/etc/somniguard/env` on the Pi 5.
- [ ] Stored the matching key in the Pico's encrypted configuration.
- [ ] Verified telemetry is accepted by the gateway after the change.
- [ ] Removed the placeholder default from any committed `config.py`.

### 2.2 Enable encrypted configuration storage

Store the HMAC key and Wi-Fi credentials in the XTEA-encrypted config rather
than as plaintext in `config.py`. See `docs/encrypted_storage.md` for full
instructions.

**Why it matters:** Plaintext credentials on the Pico filesystem can be
extracted by anyone with a USB cable and 30 seconds of physical access.

**Steps:**

```python
# On the Pico REPL (via Thonny or mpremote):
import secure_config
secrets = {
    "GATEWAY_HMAC_KEY": "your-64-char-hex-key",
    "WIFI_SSID":        "YourNetworkName",
    "WIFI_PASSWORD":    "YourNetworkPassword"
}
secure_config.save_secure_config(secrets, "/secure_config.json")
print("Done.")
```

Checklist:
- [ ] `secure_config.py` deployed to Pico.
- [ ] Encrypted config written with production HMAC key and Wi-Fi credentials.
- [ ] `config.py` updated to load secrets from `secure_config.json` at boot.
- [ ] Plaintext secrets removed from the committed version of `config.py`.
- [ ] Pico boots and connects to Wi-Fi using the encrypted credentials.

### 2.3 Generate and deploy the integrity manifest

The firmware integrity check (`integrity.py`) compares SHA-256 hashes of all
Python modules against a signed manifest at boot. Without regenerating the
manifest after deploying production code, the check will fail or be bypassed.

**Why it matters:** A tampered firmware file (e.g., a backdoored driver) will
have a different hash. The integrity check acts as a canary for firmware
tampering.

**Steps:**

```bash
# On your development machine:
cd NightWatchGaurd
python3 scripts/generate_integrity_manifest.py \
    --key YOUR_HMAC_KEY \
    --output manifest.json \
    somniguard_pico/

# Copy the manifest to the Pico:
mpremote connect /dev/ttyACM0 cp manifest.json :manifest.json
```

Checklist:
- [ ] `generate_integrity_manifest.py` run against the production codebase.
- [ ] Manifest generated with the production HMAC key (not a test key).
- [ ] `manifest.json` copied to the Pico filesystem.
- [ ] Boot log shows `[SOMNI][INTEGRITY] All hashes matched.`
- [ ] Manifest is regenerated every time Pico firmware is updated.

### 2.4 Verify firmware integrity at boot

Confirm that the integrity check runs on every boot and that you have a
procedure for responding to a failed check.

**Steps:**

```python
# Connect to the Pico REPL and check the boot log for:
# [SOMNI][INTEGRITY] Checking 12 modules...
# [SOMNI][INTEGRITY] All hashes matched.

# If you see:
# [SOMNI][INTEGRITY] HASH MISMATCH: drivers/max30102.py
# — investigate immediately: this indicates firmware tampering or
#   accidental file modification.
```

Checklist:
- [ ] Integrity check log line appears on every boot.
- [ ] Procedure documented for responding to hash mismatch events.
- [ ] Integrity failures are logged to serial console.
- [ ] (Future) Integrity failure halts operation rather than continuing.

### 2.5 Enable the hardware watchdog

The RP2350 hardware watchdog is enabled by default in `main.py` with an
8-second timeout. Verify it is active and feeding correctly.

**Why it matters:** If the firmware hangs (e.g., stuck in an I2C read), the
watchdog resets the device automatically, restoring data collection within
seconds.

**Steps:**

```python
# Boot log should contain:
# [SOMNI][MAIN] Hardware watchdog enabled (8000 ms timeout).

# Test by simulating a hang:
# (Development only — do NOT test on a device with a patient attached)
import machine
wdt = machine.WDT(timeout=8000)
# Do NOT call wdt.feed() — device should reset within 8 seconds.
```

Checklist:
- [ ] Boot log confirms watchdog is enabled.
- [ ] Main loop feeds watchdog on every iteration.
- [ ] Watchdog feeds are present in the idle loop and interrupt callbacks.

---

## 3. Pi 5 Gateway Hardening Checklist

### 3.1 Change all default secrets

The `/etc/somniguard/env` file must contain unique, randomly generated values
for all secret keys before the gateway is deployed.

**Steps:**

```bash
# Generate strong random values:
SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
HMAC_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

# Write to /etc/somniguard/env (mode 640, owned by root:somniguard):
sudo tee /etc/somniguard/env > /dev/null <<EOF
SOMNI_SECRET_KEY=${SECRET_KEY}
SOMNI_HMAC_KEY=${HMAC_KEY}
SOMNI_HTTPS=true
SOMNI_TAILSCALE_ONLY=true
EOF

sudo chmod 640 /etc/somniguard/env
sudo chown root:somniguard /etc/somniguard/env
```

Checklist:
- [ ] `SOMNI_SECRET_KEY` is 32+ random bytes (not the `dev-only-...` default).
- [ ] `SOMNI_HMAC_KEY` matches `GATEWAY_HMAC_KEY` in the Pico encrypted config.
- [ ] `/etc/somniguard/env` has permissions 640 and is owned by `root:somniguard`.
- [ ] Default admin password changed on first login to the dashboard.
- [ ] SQLCipher database key stored in `/etc/somniguard/db.key` (mode 600).

### 3.2 Enable HTTPS (SOMNI_HTTPS=true)

Enable HTTPS so that all dashboard traffic is encrypted in transit.

**Why it matters:** Without HTTPS, session cookies and health data are
transmitted in cleartext over the LAN. Any device on the same network can
read them.

**Steps:**

```bash
# In /etc/somniguard/env:
SOMNI_HTTPS=true

# The gateway generates a self-signed certificate automatically on first run.
# Certificates are stored in /etc/somniguard/tls/ (mode 700).

# Restart the gateway service:
sudo systemctl restart somniguard-gateway.service

# Verify TLS is active:
curl -k https://localhost:5000/health
# -k ignores the self-signed cert warning; omit in production with a valid cert.
```

Checklist:
- [ ] `SOMNI_HTTPS=true` in `/etc/somniguard/env`.
- [ ] Gateway starts without TLS errors.
- [ ] Browser confirms HTTPS in address bar (accept the self-signed cert warning).
- [ ] `Strict-Transport-Security` header present in response.
- [ ] HTTP requests redirect to HTTPS (or return 400).

### 3.3 Enable Tailscale-only mode (SOMNI_TAILSCALE_ONLY=true)

Restrict the dashboard to Tailscale-authenticated connections. This is the
most impactful single step for reducing network attack surface.

**Why it matters:** Without this, the dashboard is reachable by any device on
the local LAN — or even the internet if port 5000 is accidentally forwarded.
With Tailscale-only mode, only devices enrolled in your tailnet can connect.

**Steps:**

```bash
# In /etc/somniguard/env:
SOMNI_TAILSCALE_ONLY=true

# Ensure Tailscale is running:
sudo systemctl enable tailscaled
sudo tailscale up --ssh --hostname=somni-pi5

# Restart the gateway:
sudo systemctl restart somniguard-gateway.service
```

Checklist:
- [ ] `SOMNI_TAILSCALE_ONLY=true` in `/etc/somniguard/env`.
- [ ] `tailscaled` is running and enabled at boot.
- [ ] Pi 5 is enrolled in your Tailscale account.
- [ ] Tailscale ACL policy restricts port 5000 to authorised tags only.
- [ ] Verify: a device NOT on the tailnet receives HTTP 403 when accessing the dashboard.
- [ ] Verify: a Tailscale-connected device can access the dashboard.

### 3.4 Set up Secure Boot

Enable UEFI Secure Boot to protect the boot chain from tampering. See
`docs/secure_boot.md` for a complete guide.

**Steps:**

```bash
sudo chmod +x scripts/setup_secure_boot_pi5.sh

# Dry-run first to verify what will happen:
sudo bash scripts/setup_secure_boot_pi5.sh --dry-run

# Full setup (requires reboot):
sudo bash scripts/setup_secure_boot_pi5.sh

# After reboot, verify:
sudo bash scripts/setup_secure_boot_pi5.sh --verify-only
```

Checklist:
- [ ] pftf/RPi5 UEFI firmware installed on the boot partition.
- [ ] PK, KEK, and db key pairs generated under `/etc/somniguard/secure-boot/keys/`.
- [ ] Bootloader (GRUB) and kernel signed with the db key.
- [ ] Keys enrolled in UEFI NVRAM.
- [ ] System rebooted and `mokutil --sb-state` shows `SecureBoot enabled`.
- [ ] PK private key (`PK.key`) moved off the device to secure offline storage.
- [ ] `somniguard-secure-boot-verify.service` enabled and passing.

### 3.5 Enable LUKS2 disk encryption

Encrypt the Raspberry Pi 5 SD card data partition with LUKS2.

**Why it matters:** If the SD card is physically removed from the Pi 5, an
attacker can mount it on another machine and read all data. LUKS2 prevents
this even if the card is removed from a powered-off device.

**Note:** LUKS2 must be set up when first installing Raspberry Pi OS, or by
resizing and re-encrypting an existing partition — this is an irreversible
operation that requires backing up all data first.

**Steps (overview — perform before first data collection):**

```bash
# 1. Back up all data from the SD card.
# 2. Boot a live Linux environment (e.g., Raspberry Pi Imager → Ubuntu).
# 3. Encrypt the partition:
sudo cryptsetup luksFormat --type luks2 /dev/mmcblk0p2

# 4. Open the encrypted partition:
sudo cryptsetup luksOpen /dev/mmcblk0p2 somniguard-data

# 5. Format and restore data.
# 6. Configure /etc/crypttab for automatic unlock at boot (with a keyfile
#    stored on a USB token or typed passphrase).
```

Checklist:
- [ ] Data partition encrypted with LUKS2 before first patient data collected.
- [ ] Unlock passphrase or keyfile stored securely (not on the same SD card).
- [ ] Verified system boots and unlocks correctly.
- [ ] Recovery procedure documented and tested.

### 3.6 Configure the firewall (ufw)

Restrict inbound connections to only those required for SOMNI-Guard operation.

**Why it matters:** The Pi 5 may be connected to a LAN with other devices. A
firewall ensures that only the specific ports needed for the gateway are
reachable, reducing exposure to unrelated services.

**Steps:**

```bash
# Install ufw if not already present:
sudo apt-get install -y ufw

# Set default policies:
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Allow SSH (adjust if using Tailscale SSH exclusively):
sudo ufw allow ssh

# Allow SOMNI-Guard gateway (only from LAN and Tailscale ranges):
sudo ufw allow from 192.168.0.0/16 to any port 5000 proto tcp
sudo ufw allow from 10.0.0.0/8    to any port 5000 proto tcp
sudo ufw allow from 100.64.0.0/10 to any port 5000 proto tcp  # Tailscale

# Enable the firewall:
sudo ufw enable

# Verify rules:
sudo ufw status verbose
```

Checklist:
- [ ] `ufw` installed and enabled at boot.
- [ ] Default incoming policy: deny.
- [ ] Port 5000 allowed from LAN and Tailscale ranges only.
- [ ] SSH allowed (restrict to Tailscale-only once remote access is confirmed).
- [ ] All other ports blocked.
- [ ] Verified: external port scan shows only expected open ports.

### 3.7 Run as a dedicated service user

The SOMNI-Guard gateway must run as a dedicated `somniguard` system user, not
as root.

**Why it matters:** If the Flask application is exploited (e.g., via an
unpatched vulnerability), the `somniguard` user's limited privileges contain
the impact. Root compromise would affect the entire system.

**Steps:**

```bash
# Create the service user (no login shell, no home directory):
sudo useradd --system --no-create-home --shell /usr/sbin/nologin somniguard

# Set ownership of application files:
sudo chown -R somniguard:somniguard /opt/somniguard/
sudo chown -R somniguard:somniguard /var/lib/somniguard/

# Create the systemd service (see the somniguard-gateway.service unit file):
# User=somniguard
# Group=somniguard
# NoNewPrivileges=true
# PrivateTmp=true

sudo systemctl daemon-reload
sudo systemctl restart somniguard-gateway.service
```

Checklist:
- [ ] `somniguard` system user created (no shell, no sudo).
- [ ] Gateway process runs as `somniguard` (verify: `ps aux | grep run.py`).
- [ ] `/etc/somniguard/` owned by `root:somniguard`, mode 750.
- [ ] Database directory owned by `somniguard:somniguard`.
- [ ] Systemd unit includes `NoNewPrivileges=true` and `PrivateTmp=true`.

### 3.8 Enable audit logging

Verify that the audit logging subsystem is active and that logs are being
written to both the file and database audit trail.

**Why it matters:** Audit logs are essential for detecting brute-force attacks,
investigating security incidents, and demonstrating compliance with data
protection policies.

**Steps:**

```bash
# Audit logs are written automatically when the gateway runs.
# Verify the log file exists and is being written:
sudo tail -f /var/log/somniguard/audit.log

# Log entries are also in the database:
# Access at http://somni-pi5:5000/admin/audit (admin login required)
```

Checklist:
- [ ] `/var/log/somniguard/audit.log` exists and contains entries.
- [ ] Login success and failure events appear in the log.
- [ ] Database `audit_log` table is populated.
- [ ] Log rotation is configured (max 10 MB, 5 backups).
- [ ] Log directory is not world-readable.
- [ ] Procedure in place for periodic log review (see Section 5).

### 3.9 Review rate limiting configuration

The default rate limits (5 login attempts/min, 20 API requests/sec) are
appropriate for most deployments. Review and tighten them if needed.

**Steps:**

```bash
# View current rate limit settings in the gateway config:
grep -i rate /opt/somniguard/somniguard_gateway/config.py

# Test rate limiting on the login endpoint:
# (Do this with a test account, not the admin account)
for i in $(seq 1 7); do
    curl -s -o /dev/null -w "%{http_code}\n" \
        -X POST https://somni-pi5:5000/login \
        -d "username=test&password=wrong" -k
done
# Expected: 200 (or 401) for first ~5, then 429 (Too Many Requests)
```

Checklist:
- [ ] Login rate limit set to 5 per minute per IP.
- [ ] API rate limit set to 20 per second.
- [ ] HTTP 429 returned when limits are exceeded.
- [ ] Rate limit violations appear in audit log.
- [ ] Account lockout triggers after 10 consecutive failed logins.

---

## 4. Network Hardening

### 4.1 Tailscale ACL configuration

Configure Tailscale Access Control Lists (ACLs) to ensure only authorised
devices can reach the gateway dashboard, even within your tailnet.

**Recommended ACL policy** (apply in the
[Tailscale admin console](https://login.tailscale.com/admin/acls)):

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

- Apply the `somni-gateway` tag to the Pi 5 in the Tailscale admin console.
- Apply `somni-clinician` or `somni-dev` to authorised laptops.
- New devices enrolled in the tailnet but not yet tagged cannot reach port 5000.

Checklist:
- [ ] Tailscale ACL policy applied in admin console.
- [ ] Pi 5 tagged with `somni-gateway`.
- [ ] All authorised clinician/developer machines tagged.
- [ ] Test: an untagged tailnet device cannot reach the dashboard.
- [ ] Tailscale account has 2FA enabled.
- [ ] Device key expiry configured (prevents stale devices).

### 4.2 Wi-Fi security (WPA3 if available)

The Pico 2W connects to the Pi 5 gateway over the local Wi-Fi network. Secure
this link at the Wi-Fi layer.

Checklist:
- [ ] **WPA3** enabled on the wireless access point (if supported by your hardware).
  If WPA3 is not available, use WPA2-AES (not TKIP).
- [ ] Wi-Fi password is 16+ characters and not guessable.
- [ ] Access point admin credentials changed from default.
- [ ] Consider using **MAC address filtering** to restrict which devices can
  connect to the LAN segment.
- [ ] Pico 2W assigned a static DHCP lease (prevents IP changes disrupting
  connectivity if the gateway uses a hostname allowlist).
- [ ] Consider placing the Pico on an **isolated IoT VLAN** with no internet
  access and direct routing only to the Pi 5 gateway.

### 4.3 Disable unnecessary services

Remove or disable any services on the Pi 5 that are not required for
SOMNI-Guard operation.

**Steps:**

```bash
# List all enabled services:
systemctl list-unit-files --state=enabled --type=service

# Disable services not needed (examples — your system may differ):
sudo systemctl disable --now bluetooth.service
sudo systemctl disable --now avahi-daemon.service
sudo systemctl disable --now cups.service

# Check for open ports:
sudo ss -tlnp
# Expected: only port 22 (SSH) and port 5000 (gateway) should be open
```

Checklist:
- [ ] Bluetooth disabled if not used.
- [ ] Avahi/mDNS disabled if not needed.
- [ ] Printing services disabled.
- [ ] No unexpected listening ports (`ss -tlnp` review).
- [ ] `sshd` access restricted to Tailscale IP range or key-only auth.

---

## 5. Operational Security

### 5.1 Key rotation schedule

| Key / Secret | Rotation trigger | Rotation method |
|-------------|-----------------|----------------|
| `SOMNI_HMAC_KEY` (gateway) + `GATEWAY_HMAC_KEY` (Pico) | Annually, or immediately on suspected compromise | Generate new 32-byte hex key; update both `/etc/somniguard/env` and Pico encrypted config; restart gateway |
| `SOMNI_SECRET_KEY` (Flask session) | Annually, or when admin staff change | Update in `/etc/somniguard/env`; restart gateway (invalidates all current sessions) |
| Secure Boot db key | Annually, or if db.key is suspected compromised | Generate new db key pair; re-sign kernel and bootloader; update db EFI variable using KEK-signed payload |
| TLS certificate | Before expiry (default 365 days); monitor with: `openssl x509 -in /etc/somniguard/tls/cert.pem -noout -dates` | Regenerate by deleting `/etc/somniguard/tls/` and restarting gateway |
| User passwords | When a user leaves the project, or on security incident | Use the admin dashboard user management interface |
| LUKS2 passphrase | If passphrase is believed compromised | `sudo cryptsetup luksChangeKey /dev/mmcblk0p2` |

**Rotation procedure for HMAC key:**

```bash
# 1. Generate new key:
NEW_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

# 2. Update the gateway:
sudo sed -i "s/^SOMNI_HMAC_KEY=.*/SOMNI_HMAC_KEY=${NEW_KEY}/" /etc/somniguard/env
sudo systemctl restart somniguard-gateway.service

# 3. Update the Pico (via REPL):
#    import secure_config
#    c = secure_config.load_secure_config("/secure_config.json")
#    c["GATEWAY_HMAC_KEY"] = "<NEW_KEY>"
#    secure_config.save_secure_config(c, "/secure_config.json")

# 4. Verify telemetry resumes successfully.
```

### 5.2 Log review procedures

Regular log review is the primary mechanism for detecting active attacks.

**Weekly review:**

```bash
# Review recent login attempts (look for unusual volume or times):
sudo grep '"event": "login' /var/log/somniguard/audit.log | tail -50

# Check for account lockouts (indicates brute-force attempts):
sudo grep '"event": "lockout' /var/log/somniguard/audit.log

# Check for HMAC verification failures (potential injection or replay):
sudo grep 'HMAC' /var/log/somniguard/audit.log | tail -20

# Review Secure Boot status:
sudo cat /var/log/somniguard/secure_boot_status.log | tail -5
```

**After any security incident:**

```bash
# Get all events from a specific IP in the last 7 days:
sudo grep '"ip": "192.168.1.X"' /var/log/somniguard/audit.log \
    | awk -F'"timestamp": "' '{print $2}' | cut -d'"' -f1 \
    | while read ts; do date -d "$ts" +%s; done | sort | head -20

# Check database audit trail (requires admin login):
# http://somni-pi5:5000/admin/audit
```

Checklist:
- [ ] Weekly log review scheduled (calendar reminder or automated alert).
- [ ] Procedure documented for escalating suspicious events.
- [ ] Logs backed up to a location the gateway service user cannot modify.
- [ ] Log retention policy defined (recommend: 90 days minimum).

### 5.3 Backup and recovery

Protect the data collected by SOMNI-Guard and ensure the system can be
restored after hardware failure.

**Database backup:**

```bash
# Backup the SQLite database (run as somniguard user or via sudo):
BACKUP_DIR="/var/backups/somniguard"
sudo mkdir -p "$BACKUP_DIR"
sudo -u somniguard sqlite3 /var/lib/somniguard/somniguard.db \
    ".backup ${BACKUP_DIR}/somniguard_$(date +%Y%m%d).db"

# Verify backup integrity:
sqlite3 "${BACKUP_DIR}/somniguard_$(date +%Y%m%d).db" "PRAGMA integrity_check;"
```

**Configuration backup:**

```bash
# Back up the environment file and TLS certificates:
sudo tar -czf "/var/backups/somniguard/config_$(date +%Y%m%d).tar.gz" \
    /etc/somniguard/env \
    /etc/somniguard/tls/ \
    /etc/somniguard/secure-boot/keys/
# WARNING: This archive contains private keys. Encrypt it before offsite storage.
```

Checklist:
- [ ] Database backed up daily (automated cron or systemd timer).
- [ ] Backups stored on a different physical device (USB drive or NAS).
- [ ] Backup restoration tested at least once.
- [ ] Pico encrypted config backed up (note: only decryptable on the same Pico).
- [ ] Recovery procedure documented and accessible without the gateway running.

### 5.4 Incident response basics

If you suspect a security incident (unusual log entries, unexpected Secure Boot
failure, unrecognised device on the tailnet):

**Immediate containment:**

```bash
# 1. Isolate the gateway from the network:
sudo ufw deny incoming    # block all new inbound connections
sudo tailscale down       # disconnect from tailnet temporarily

# 2. Preserve evidence (do not restart services yet):
sudo journalctl --since "1 hour ago" > /tmp/incident_journal.txt
sudo cp /var/log/somniguard/audit.log /tmp/incident_audit.log

# 3. Check for unexpected processes:
ps aux | grep -v "^somniguard\|^root\|^www-data"

# 4. Check for unexpected listening ports:
sudo ss -tlnp

# 5. Check for recent file modifications in the application directory:
find /opt/somniguard/ -newer /opt/somniguard/last_deploy -type f
```

**After investigation:**

- Rotate all secrets (HMAC key, Flask secret key, passwords).
- Regenerate TLS certificates.
- Review and regenerate Pico firmware integrity manifest.
- Consider re-flashing both the Pi 5 and the Pico from known-good images.
- Document the incident timeline, impact, and remediation.

---

## 6. Verification Commands

Use these one-liners to confirm each hardening item is in effect. Run from
the Pi 5 unless otherwise noted.

### Pico 2W verification

```bash
# Check encrypted config exists on Pico (run from development machine):
mpremote connect /dev/ttyACM0 exec "import os; print(os.stat('/secure_config.json'))"
# Expected: non-zero file size

# Check firmware integrity manifest exists:
mpremote connect /dev/ttyACM0 exec "import os; print(os.stat('/manifest.json'))"

# Check boot log for integrity and watchdog confirmation:
mpremote connect /dev/ttyACM0 exec "
import sys
# Boot messages already printed — check terminal output for:
# [SOMNI][INTEGRITY] All hashes matched.
# [SOMNI][MAIN] Hardware watchdog enabled
print('Check boot log above for these strings.')
"
```

### Pi 5 gateway verification

```bash
# Verify HTTPS is active:
curl -sk https://localhost:5000/health | python3 -m json.tool

# Verify Secure Boot status:
mokutil --sb-state
# Expected: SecureBoot enabled

# Verify signed kernel:
sbverify --cert /etc/somniguard/secure-boot/keys/db.crt /boot/vmlinuz.signed
# Expected: Signature verification OK

# Verify EFI keys are enrolled:
efi-readvar -v PK | head -5

# Verify gateway runs as somniguard user (not root):
ps aux | grep run.py | awk '{print $1}'
# Expected: somniguard

# Verify no unexpected open ports:
sudo ss -tlnp | awk '{print $4, $NF}'
# Expected: only :22 (ssh) and :5000 (gateway)

# Verify security headers:
curl -skI https://localhost:5000/ | grep -E 'Strict|X-Frame|Content-Security|X-Content'
# Expected: all four headers present

# Verify SOMNI_TAILSCALE_ONLY is set:
sudo grep TAILSCALE_ONLY /etc/somniguard/env
# Expected: SOMNI_TAILSCALE_ONLY=true

# Verify audit logging is active:
ls -la /var/log/somniguard/audit.log && sudo wc -l /var/log/somniguard/audit.log
# Expected: file exists with non-zero line count

# Verify firewall is enabled:
sudo ufw status | head -3
# Expected: Status: active

# Verify env file has restricted permissions:
ls -la /etc/somniguard/env
# Expected: -rw-r----- root somniguard (mode 640)

# Verify service user has no shell:
getent passwd somniguard | cut -d: -f7
# Expected: /usr/sbin/nologin

# Verify Tailscale is running:
tailscale status | head -5
# Expected: shows Pi 5 and any connected peers

# Verify LUKS2 encryption (if configured):
sudo cryptsetup status somniguard-data 2>/dev/null || \
    lsblk -o NAME,TYPE,FSTYPE | grep crypto
```

### Rate limiting verification

```bash
# Test login rate limiting (use a non-critical test account):
for i in $(seq 1 8); do
    CODE=$(curl -sk -o /dev/null -w "%{http_code}" \
        -X POST https://localhost:5000/login \
        -d "username=testuser&password=wrongpassword")
    echo "Attempt $i: HTTP $CODE"
done
# Expected: HTTP 200 or 401 for first 5, then HTTP 429

# Test API rate limiting:
for i in $(seq 1 25); do
    CODE=$(curl -sk -o /dev/null -w "%{http_code}" https://localhost:5000/api/ingest)
    echo "Request $i: HTTP $CODE"
done
# Expected: HTTP 405 or 401 for first ~20, then HTTP 429
```

---

## Related Documents

- [Security Controls](security_controls.md) — Detailed control descriptions and threat mappings
- [Secure Boot](secure_boot.md) — Pi 5 UEFI Secure Boot complete guide
- [Encrypted Storage](encrypted_storage.md) — Pico 2W XTEA encryption guide
- [Tailscale Setup](tailscale_setup.md) — Tailscale VPN configuration
- [Architecture](architecture.md) — System architecture and data flow
- [Attack Tree](attack_tree.md) — Threat analysis and attack paths
- [PHA](pha.md) — Preliminary Hazard Analysis
- [Developer Guide](developer_guide.md) — Module reference and API documentation

---

## Legacy: Implementation Status Reference

The tables below record the current implementation status of all hardening
controls. They are provided for audit purposes; the checklists in Sections
2–4 describe the operational steps to activate each control.

### Pico 2W Controls

| # | Control | Status | Module |
|---|---------|--------|--------|
| P-01 | SHA-256 firmware integrity check at boot | Implemented | `integrity.py` |
| P-02 | HMAC-SHA256 signed manifest verification | Implemented | `integrity.py` |
| P-03 | Fail-soft behaviour on integrity failure (log + continue) | Implemented | `main.py` |
| P-04 | Manifest generated with `generate_integrity_manifest.py` | Implemented | `scripts/` |
| P-05 | XTEA-encrypted config storage at rest | Implemented | `secure_config.py` |
| P-06 | Hardware-derived encryption key (SHA-256 of `machine.unique_id()`) | Implemented | `secure_config.py` |
| P-07 | Key never stored on filesystem | Implemented | `secure_config.py` |
| P-08 | HMAC-SHA256 authentication on all API payloads | Implemented | `transport.py` |
| P-09 | Anti-replay nonce (monotonic sequence number) | Implemented | `transport.py` |
| P-10 | Hardware watchdog timer (8-second timeout) | Implemented | `main.py` |

### Pi 5 Gateway Controls

| # | Control | Status | Module |
|---|---------|--------|--------|
| G-01 | CSRF protection via Flask-WTF | Implemented | `app.py` |
| G-02 | Rate limiting on login (5/min) and API (20/sec) | Implemented | `security.py` |
| G-03 | Account lockout after 10 failed attempts | Implemented | `security.py` |
| G-04 | bcrypt password hashing (cost ≥ 12) | Implemented | `security.py` |
| G-05 | Parameterised SQL queries | Implemented | `database.py` |
| G-06 | HMAC-SHA256 ingest payload verification | Implemented | `app.py` |
| G-07 | Anti-replay nonce + timestamp validation | Implemented | `app.py` |
| G-08 | HTTPS with self-signed TLS (RSA-4096) | Implemented | `tls_setup.py` |
| G-09 | Security headers (HSTS, CSP, X-Frame-Options, etc.) | Implemented | `security.py` |
| G-10 | Structured JSON audit logging with rotation | Implemented | `audit.py` |
| G-11 | Least-privilege service user | Implemented | `scripts/` |
| G-12 | UEFI Secure Boot key hierarchy | Implemented | `scripts/setup_secure_boot_pi5.sh` |

### 1.1 Firmware Integrity

| # | Control | Status | Module |
|---|---------|--------|--------|
| P-01 | SHA-256 firmware integrity check at boot | Implemented | `integrity.py` |
| P-02 | HMAC-SHA256 signed manifest verification | Implemented | `integrity.py` |
| P-03 | Fail-soft behaviour on integrity failure (log + continue) | Implemented | `main.py` |
| P-04 | Manifest generated with `generate_integrity_manifest.py` | Implemented | `scripts/` |
| P-05 | Manifest signature uses shared HMAC key | Implemented | `integrity.py` |

### 1.2 Encrypted Configuration

| # | Control | Status | Module |
|---|---------|--------|--------|
| P-06 | XTEA-encrypted config storage at rest | Implemented | `secure_config.py` |
| P-07 | Hardware-derived encryption key (SHA-256 of `machine.unique_id()`) | Implemented | `secure_config.py` |
| P-08 | Key never stored on filesystem | Implemented | `secure_config.py` |
| P-09 | Secure memory wiping of key material after use | Implemented | `secure_config.py` |
| P-10 | PKCS7 padding validation on decryption | Implemented | `secure_config.py` |
| P-11 | Device-bound encryption (non-portable between units) | Implemented | `secure_config.py` |

### 1.3 Transport Security

| # | Control | Status | Module |
|---|---------|--------|--------|
| P-12 | HMAC-SHA256 authentication on all API payloads | Implemented | `transport.py` |
| P-13 | Anti-replay nonce (monotonic sequence number) | Implemented | `transport.py` |
| P-14 | Timestamp in every payload for freshness checking | Implemented | `transport.py` |
| P-15 | Secure memory wiping of HMAC intermediate values | Implemented | `transport.py` |
| P-16 | Socket resource cleanup in `finally` blocks | Implemented | `transport.py` |
| P-17 | Fail-soft on transport errors (data logged locally) | Implemented | `main.py` |

### 1.4 Hardware Watchdog

| # | Control | Status | Module |
|---|---------|--------|--------|
| P-18 | Hardware watchdog timer (8-second timeout) | Implemented | `main.py` |
| P-19 | Watchdog fed in idle loop, callback, and setup | Implemented | `main.py` |
| P-20 | Automatic device reset on software hang | Implemented | `main.py` |

### 1.5 Production Hardening (Not Yet Implemented)

| # | Recommendation | Priority |
|---|---------------|----------|
| P-21 | Replace XTEA-ECB with XTEA-CBC or use hardware AES | High |
| P-22 | Add authenticated encryption (HMAC over ciphertext) | High |
| P-23 | Use hardware secure element (ATECC608A) for key storage | High |
| P-24 | Disable USB debug interface in production builds | Medium |
| P-25 | Implement secure firmware OTA update mechanism | Medium |
| P-26 | Add monotonic counter in flash to prevent rollback attacks | Medium |

---

## 2. Pi 5 Gateway

### 2.1 Web Application Security

| # | Control | Status | Module |
|---|---------|--------|--------|
| G-01 | CSRF protection via Flask-WTF on all forms | Implemented | `app.py` |
| G-02 | Rate limiting on login (5/min) and API (20/sec) | Implemented | `security.py` |
| G-03 | Account lockout after 10 failed attempts (15-min duration) | Implemented | `security.py` |
| G-04 | Password complexity validation (8+ chars, upper, lower, digit, special) | Implemented | `security.py` |
| G-05 | Input sanitisation on all user-supplied strings | Implemented | `security.py` |
| G-06 | Parameterised SQL queries (no string interpolation) | Implemented | `database.py` |
| G-07 | Integer range validation on numeric inputs | Implemented | `security.py` |

### 2.2 Session Security

| # | Control | Status | Module |
|---|---------|--------|--------|
| G-08 | HttpOnly session cookies | Implemented | `app.py` |
| G-09 | SameSite=Lax cookie attribute | Implemented | `app.py` |
| G-10 | Secure cookie flag (HTTPS only) | Implemented | `app.py` |
| G-11 | 30-minute session timeout | Implemented | `app.py` |
| G-12 | Strong session protection (Flask-Login) | Implemented | `app.py` |

### 2.3 Security Headers

| # | Header | Value | Module |
|---|--------|-------|--------|
| G-13 | `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | `security.py` |
| G-14 | `X-Frame-Options` | `DENY` | `security.py` |
| G-15 | `X-Content-Type-Options` | `nosniff` | `security.py` |
| G-16 | `Content-Security-Policy` | `default-src 'self'; script-src 'self'; ...` | `security.py` |
| G-17 | `Referrer-Policy` | `strict-origin-when-cross-origin` | `security.py` |
| G-18 | `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` | `security.py` |
| G-19 | `Cache-Control` | `no-store` on sensitive pages | `security.py` |

### 2.4 TLS/HTTPS

| # | Control | Status | Module |
|---|---------|--------|--------|
| G-20 | Self-signed TLS certificate generation | Implemented | `tls_setup.py` |
| G-21 | RSA 4096-bit private key | Implemented | `tls_setup.py` |
| G-22 | Certificate directory permissions (0o700) | Implemented | `tls_setup.py` |
| G-23 | Private key file permissions (0o600) | Implemented | `tls_setup.py` |
| G-24 | HTTPS enabled via `SOMNI_HTTPS=true` environment variable | Implemented | `run.py` |

### 2.5 API Security

| # | Control | Status | Module |
|---|---------|--------|--------|
| G-25 | HMAC-SHA256 verification on all ingest payloads | Implemented | `app.py` |
| G-26 | Anti-replay nonce validation (monotonic per session) | Implemented | `app.py` |
| G-27 | Timestamp freshness check (5-minute window) | Implemented | `app.py` |
| G-28 | Rate limiting on API endpoints (20/sec) | Implemented | `security.py` |
| G-29 | Login required for all dashboard and management routes | Implemented | `app.py` |

### 2.6 Audit Logging

| # | Control | Status | Module |
|---|---------|--------|--------|
| G-30 | Structured JSON audit log with rotation | Implemented | `audit.py` |
| G-31 | Database-backed audit trail | Implemented | `database.py` |
| G-32 | Login success/failure events logged | Implemented | `app.py` |
| G-33 | Data access events logged | Implemented | `app.py` |
| G-34 | Administrative action events logged | Implemented | `app.py` |
| G-35 | IP address recorded in all audit events | Implemented | `audit.py` |

### 2.7 Database Security

| # | Control | Status | Module |
|---|---------|--------|--------|
| G-36 | Thread-local connection pooling with health checks | Implemented | `database.py` |
| G-37 | WAL journal mode for concurrency | Implemented | `database.py` |
| G-38 | Foreign key enforcement | Implemented | `database.py` |
| G-39 | Busy timeout (5000 ms) | Implemented | `database.py` |
| G-40 | Query timeout (30 seconds) | Implemented | `database.py` |
| G-41 | All queries use parameterised statements | Implemented | `database.py` |

### 2.8 Platform Hardening

| # | Control | Status | Module |
|---|---------|--------|--------|
| G-42 | UEFI Secure Boot key enrollment | Implemented | `scripts/setup_secure_boot_pi5.sh` |
| G-43 | PK → KEK → db key hierarchy (UEFI spec) | Implemented | `scripts/setup_secure_boot_pi5.sh` |
| G-44 | Signed kernel and bootloader verification | Implemented | `scripts/setup_secure_boot_pi5.sh` |

### 2.9 Production Hardening (Not Yet Implemented)

| # | Recommendation | Priority |
|---|---------------|----------|
| G-45 | Replace self-signed TLS with CA-signed certificates | High |
| G-46 | Enable full-disk encryption (LUKS) | High |
| G-47 | Deploy with a reverse proxy (nginx) for TLS termination | Medium |
| G-48 | Add fail2ban for IP-based blocking | Medium |
| G-49 | Enable AppArmor or SELinux profiles | Medium |
| G-50 | Implement centralized log shipping (syslog/ELK) | Medium |
| G-51 | Add database encryption at rest | Low |
| G-52 | Implement automated security scanning in CI/CD | Low |

---

## 3. Network Security

### 3.1 Implemented Controls

| # | Control | Description |
|---|---------|-------------|
| N-01 | HMAC-SHA256 payload signing | All Pico → Gateway traffic is authenticated |
| N-02 | Anti-replay protection | Nonce + timestamp validation prevents replay attacks |
| N-03 | TLS encryption (optional) | HTTPS with self-signed certificates |
| N-04 | Tailscale integration | Zero-trust overlay network support |

### 3.2 Recommended Network Architecture

```
┌──────────────┐       ┌──────────────────┐       ┌─────────────┐
│  Pico 2W     │──────►│  Isolated VLAN   │──────►│  Pi 5       │
│  (sensor)    │ Wi-Fi │  (medical IoT)   │       │  (gateway)  │
│              │       │  No internet     │       │             │
└──────────────┘       │  HMAC+nonce auth │       └─────────────┘
                       └──────────────────┘
```

- Deploy on an **isolated VLAN** with no internet access
- Use **MAC address filtering** on the access point
- Enable **WPA3** on the wireless network
- Consider **Tailscale** for secure remote access

---

## 4. Operational Security

### 4.1 Credential Management

- [ ] Change default admin credentials on first login
- [ ] Use strong passwords (enforced by complexity validator)
- [ ] Rotate HMAC keys periodically
- [ ] Store HMAC keys in encrypted Pico config (not plaintext)
- [ ] Use environment variables for gateway secrets (not hardcoded)

### 4.2 Monitoring and Alerting

- [ ] Review audit logs regularly (`/admin/audit`)
- [ ] Monitor for login lockout events (potential brute-force)
- [ ] Check firmware integrity logs on each Pico boot
- [ ] Set up alerting for failed integrity checks

### 4.3 Maintenance

- [ ] Keep Raspberry Pi OS updated (`apt update && apt upgrade`)
- [ ] Update Python dependencies regularly (`pip install --upgrade`)
- [ ] Regenerate firmware manifest after any Pico code changes
- [ ] Rotate TLS certificates before expiry (365-day default)
- [ ] Back up the SQLite database regularly

---

## 5. Pre-Deployment Checklist

Use this checklist before deploying a SOMNI-Guard system:

### Pico 2W

- [ ] Flash latest MicroPython firmware for RP2350
- [ ] Deploy all source files to Pico filesystem
- [ ] Run `generate_integrity_manifest.py` with production HMAC key
- [ ] Copy `manifest.json` to Pico filesystem
- [ ] Encrypt configuration with `save_secure_config()`
- [ ] Verify encrypted config loads correctly on boot
- [ ] Confirm watchdog timer is active in boot log
- [ ] Test fail-soft behaviour (remove a sensor, verify graceful degradation)

### Pi 5 Gateway

- [ ] Run `setup_secure_boot_pi5.sh` (if using UEFI Secure Boot)
- [ ] Set `SOMNI_SECRET_KEY` to a strong random value
- [ ] Set `SOMNI_HMAC_KEY` to match Pico shared secret
- [ ] Set `SOMNI_HTTPS=true` and verify TLS works
- [ ] Create initial admin account with strong password
- [ ] Verify rate limiting is active (test with rapid login attempts)
- [ ] Verify audit logging is writing to file and database
- [ ] Confirm security headers in browser developer tools
- [ ] Test account lockout (10 failed logins → 15-min lock)

### Network

- [ ] Configure isolated VLAN for medical IoT devices
- [ ] Enable WPA3 on the wireless access point
- [ ] Verify Pico can reach gateway but not the internet
- [ ] Test Tailscale connectivity (if using remote access)

---

## Defence-in-Depth Layers

The SOMNI-Guard security architecture follows a defence-in-depth approach
with four control layers:

```
┌─────────────────────────────────────────────────────┐
│  L3 — Physical/Platform                             │
│  UEFI Secure Boot, hardware watchdog, device binding│
├─────────────────────────────────────────────────────┤
│  L2 — Application                                   │
│  Rate limiting, lockout, CSRF, session security,    │
│  password policy, input sanitisation, audit logging  │
├─────────────────────────────────────────────────────┤
│  L1 — Data-at-Rest & Data-in-Transit                │
│  XTEA encryption, TLS, HMAC-SHA256, anti-replay,    │
│  parameterised queries, firmware integrity           │
├─────────────────────────────────────────────────────┤
│  L0 — Operational                                   │
│  Credential rotation, monitoring, patching,          │
│  audit review, network isolation                     │
└─────────────────────────────────────────────────────┘
```

---

## Related Documents

- [Security Controls](security_controls.md) — Detailed control descriptions
- [Architecture](architecture.md) — System architecture and data flow
- [Attack Tree](attack_tree.md) — Threat analysis
- [PHA](pha.md) — Preliminary Hazard Analysis
- [Assets](assets.md) — Asset inventory
- [Encrypted Storage](encrypted_storage.md) — XTEA implementation details
- [Secure Boot](secure_boot.md) — Pi 5 UEFI Secure Boot guide
- [Developer Guide](developer_guide.md) — Module reference and API docs
