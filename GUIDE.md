# SOMNI-Guard — Operator & Developer Guide

The single source of truth for installing, operating, and developing on the
SOMNI-Guard sleep-monitoring prototype. **All other docs are pointers to this
file.**

> ⚠️ **Educational prototype.** SOMNI-Guard is **not** a regulated medical
> device and must **not** be used for diagnosis or treatment.

---

## Table of contents

1. [What it is](#1-what-it-is)
2. [System architecture](#2-system-architecture)
3. [Install — Raspberry Pi 5 gateway](#3-install--raspberry-pi-5-gateway)
   - [3.8 Step 7 — Enable the secrets vault](#38-step-7--enable-the-secrets-vault-strongly-recommended)
4. [Install — Pico 2 W sensor](#4-install--pico-2-w-sensor)
5. [First-run admin bootstrap (mandatory MFA)](#5-first-run-admin-bootstrap-mandatory-mfa)
6. [Web interface — feature tour](#6-web-interface--feature-tour)
7. [Authentication & MFA](#7-authentication--mfa)
8. [User management](#8-user-management)
9. [Patient & session workflow](#9-patient--session-workflow)
10. [Clinical alerts](#10-clinical-alerts)
11. [Device fleet](#11-device-fleet)
12. [Audit log](#12-audit-log)
13. [REST API reference](#13-rest-api-reference)
14. [Configuration & environment variables](#14-configuration--environment-variables)
15. [Security model](#15-security-model)
16. [Troubleshooting](#16-troubleshooting)
17. [Project layout](#17-project-layout)

---

## 1. What it is

SOMNI-Guard is a two-piece teaching prototype for an at-home sleep-screening
gateway:

- **Gateway** — a Flask + gunicorn web app on a Raspberry Pi 5, with a Wi-Fi
  hotspot, mTLS-secured REST API, SQLite storage, PDF reports, structured
  audit logging, two-factor authentication, threshold-based clinical alerts,
  a live multi-patient monitor, and full user / patient / device fleet
  management.
- **Sensor** — a Raspberry Pi Pico 2 W running MicroPython with a
  pulse-oximeter (MAX30102), an accelerometer (MPU6050) and a galvanic skin
  response amplifier. The Pico authenticates every API packet with
  HMAC-SHA256 and replay-resistant nonces.

Two clear non-goals: this code is **not** clinically validated, and it does
**not** exfiltrate data over the public internet — everything stays on the Pi
unless an operator explicitly enables Tailscale.

---

## 2. System architecture

```
                ┌──────────────────┐                  ┌──────────────────┐
 Pico 2 W       │  HMAC-SHA256     │   mTLS HTTPS     │ Pi 5 Gateway     │
 (sensor) ──── ▶│  per-packet auth │ ──5443──────────▶│ Flask + gunicorn │
                │  monotonic nonce │                  │ SQLite (WAL)     │
                └──────────────────┘                  │ rotating audit   │
                                                     │ TOTP MFA          │
                                                     └────────┬─────────┘
                                                              │ Web UI
                                                              ▼
                                                ┌─────────────────────────┐
                                                │ Browser on hotspot      │
                                                │ SomniGuard_Net          │
                                                │ https://10.42.0.1:5443  │
                                                └─────────────────────────┘
```

Key facts:

- The Pi 5 hosts a NetworkManager Wi-Fi hotspot named **`SomniGuard_Net`**
  (`10.42.0.1/24`).
- The Pico connects to the hotspot via WPA2 and reaches the gateway at
  `https://10.42.0.1:5443/`. mDNS (`somniguard.local`) is also published
  through dnsmasq for clients that prefer hostnames.
- TLS is mandatory. The gateway cert is auto-regenerated on each boot to
  match the current IP. The CA cert is downloadable at
  `https://10.42.0.1:5443/ca.crt` for installing in a browser trust store.
- Every `/api/*` call carries an HMAC-SHA256 tag computed over the JSON body
  (sorted-keys, no whitespace, `hmac` field excluded). HMAC keys live in
  `SOMNI_HMAC_KEY` on the Pi and `GATEWAY_HMAC_KEY` on the Pico.
- The web dashboard requires **two factors**: bcrypt-hashed password
  + TOTP (RFC 6238) authenticator-app code, with single-use backup codes.

---

## 3. Install — Raspberry Pi 5 gateway

Tested on **Raspberry Pi OS Bookworm (64-bit)**, Python 3.11+. The whole
install takes 5–10 minutes on a Pi 5.

### 3.1 What you need

- Raspberry Pi 5 (4 GB or 8 GB) with Pi OS Bookworm flashed and booted
- Internet during install (apt + pip)
- An external Wi-Fi adapter is **not** required — the Pi 5's built-in Wi-Fi
  becomes the SOMNI-Guard hotspot (clients connect to *it*)
- A keyboard + monitor *or* SSH access for the first run (the admin
  bootstrap is interactive)

### 3.2 Step 1 — Clone and run the installer

```bash
git clone https://github.com/at0m-b0mb/NightWatchGaurd.git
cd NightWatchGaurd
sudo bash scripts/setup_gateway_pi5.sh
```

The installer is **idempotent** — re-run it any time to repair a
deployment. In one shot it:

1. installs apt packages (`python3-dev`, `python3-pip`, `python3-venv`,
   `libssl-dev`, `libffi-dev`, `build-essential`, `fonts-dejavu-core`,
   `dnsmasq-base`, `avahi-daemon`)
2. creates `somniguard_gateway/.venv/` and `pip install -r requirements.txt`
   (Flask, gunicorn, bcrypt, cryptography, reportlab, **pyotp**,
   **qrcode\[pil\]**, …)
3. seeds an empty SQLite database (no users yet — see Step 4)
4. brings up the **`SomniGuard_Net`** Wi-Fi hotspot at `10.42.0.1/24`
   (NetworkManager profile + dnsmasq override that resolves
   `somniguard.local`)
5. generates a fresh PKI via `scripts/setup_gateway_certs.py` —
   `certs/ca.crt`, `certs/server.{crt,key}`, `certs/pico_client.{crt,key}`
6. writes `/etc/somniguard/env` with placeholder secrets
7. installs and enables the **`somniguard-gateway.service`** systemd unit
8. (optional) basic OS hardening — disables Bluetooth, tightens sshd

### 3.3 Step 2 — Set the gateway secrets

Both `SOMNI_SECRET_KEY` and `SOMNI_HMAC_KEY` are **mandatory**. The gateway
refuses to start if either is missing or empty — there is no default
fallback. At import time `somniguard_gateway/config.py` prints a one-line
confirmation showing which env file the key came from and a SHA-256
fingerprint of it (the key itself never appears in any log):

```
[SOMNI][CONFIG] Loaded /etc/somniguard/env: SOMNI_HMAC_KEY (sha256[:8]=977b7b05, len=64)  — must match GATEWAY_HMAC_KEY on the Pico.
```

You have two ways to populate the file.

**Option A — let the helper script do it (recommended).** Once the Pico
config has a real `GATEWAY_HMAC_KEY` set (§4.4), run:

```bash
sudo bash scripts/sync_gateway_env.sh
```

The script reads `GATEWAY_HMAC_KEY` from `somniguard_pico/config.py`, writes
it byte-for-byte to `/etc/somniguard/env` as `SOMNI_HMAC_KEY=`, generates a
fresh `SOMNI_SECRET_KEY` if one is not already there, locks the file to
`0640 root:somniguard`, and restarts `somniguard-gateway.service`. It is
idempotent — re-run any time the two keys drift apart.

**Option B — edit by hand.**

```bash
sudo $EDITOR /etc/somniguard/env
```

Generate strong values with:

```bash
python3 -c "import secrets; print('SOMNI_SECRET_KEY=' + secrets.token_hex(32))"
python3 -c "import secrets; print('SOMNI_HMAC_KEY='   + secrets.token_hex(32))"
```

Minimum required keys:

```bash
SOMNI_SECRET_KEY=<from the command above>     # Flask cookie + MFA wrap
SOMNI_HMAC_KEY=<from the command above>       # Pico ↔ gateway shared HMAC
SOMNI_TAILSCALE_ONLY=false                    # set true to lock dashboard to Tailscale CGNAT
```

> ⚠ **Both keys are critical.**  `SOMNI_HMAC_KEY` must equal the Pico's
> `GATEWAY_HMAC_KEY` byte-for-byte — a mismatch fails every `/api/*` call
> with `403 HMAC verification failed`. Compare the `sha256[:8]` fingerprints
> printed by the gateway and the Pico (§4.7) to confirm they are aligned.
> If `SOMNI_SECRET_KEY` is rotated after MFA enrolment, every user will
> need to re-enrol.

### 3.4 Step 3 — Start the service

```bash
sudo systemctl daemon-reload
sudo systemctl restart somniguard-gateway
sudo systemctl status somniguard-gateway        # should be "active (running)"
sudo journalctl -u somniguard-gateway -f        # tail logs
```

**Autostart on boot** is wired up by `setup_gateway.sh`:

- `systemctl enable somniguard-gateway` is run once during install, so the
  service starts on every reboot.
- The unit has `After=network-online.target NetworkManager.service` and
  `Wants=network-online.target NetworkManager.service`, so it waits for
  NetworkManager to bring up the AP profile before Python starts.
- `Restart=always RestartSec=5 TimeoutStartSec=120` brings the service
  back up if anything in the call graph dies (lost TLS cert, kernel panic
  during nmcli probe, etc). `RestartPreventExitStatus=0` keeps a clean
  `systemctl stop` from triggering a restart loop.
- `StartLimitIntervalSec=300 StartLimitBurst=10` widens the boot-loop
  guard. The systemd default (5 starts in 10 s) is too tight for a Pi 5
  cold boot — cert regeneration, the first import of `cryptography`, and
  the `nmcli` probe each take a few seconds and can chain into a false
  "give up" state.
- The service user is `somniguard`, added to the `netdev` group during
  install. NetworkManager's default polkit rules let `netdev` members run
  `nmcli` without `sudo` — that's important, because the unit's sandbox
  (`NoNewPrivileges` / `RestrictSUIDSGID`) would block any setuid escalation.
- `run.py` calls `start_hotspot()` at the top of `main()`. After the first
  successful run, NetworkManager stores the AP profile with `autoconnect
  yes`, so even before Python starts, the hotspot is already up. The
  Python call becomes a no-op (`Hotspot already active — skipping setup.`).
- Hotspot credentials live at `/var/lib/somniguard/hotspot_credentials.json`
  (set via `SOMNI_HOTSPOT_CREDS` in `/etc/somniguard/env`). The original
  in-tree path under the project directory is unreachable under
  `ProtectHome=read-only`; the install scripts migrate the file
  automatically on first run.

To verify:

```bash
systemctl is-enabled somniguard-gateway        # should print: enabled
systemctl is-active  somniguard-gateway        # should print: active
nmcli con show --active | grep SomniGuard      # should list the hotspot
id somniguard | grep -o 'netdev'               # should print: netdev
```

If `is-enabled` reports anything other than `enabled`, run:

```bash
sudo systemctl enable --now somniguard-gateway
```

…or re-run `sudo bash setup_gateway.sh` (idempotent — safe to run again).
Reboot the Pi (`sudo reboot`) and confirm the dashboard at
`https://10.42.0.1:5443/` answers without any manual intervention.

> **If the service fails to start after reboot**, the fastest fix is to run
> the dedicated repair script — it diagnoses each common failure mode and
> rewrites the unit file with the correct paths for *this* install:
>
> ```bash
> sudo bash scripts/fix_autostart.sh
> ```
>
> The script checks venv, env file contents, service user + `netdev`
> membership, writable state dirs, then rewrites
> `/etc/systemd/system/somniguard-gateway.service`, runs `daemon-reload`,
> `enable`, and `restart`, and finally verifies the autostart symlink at
> `/etc/systemd/system/multi-user.target.wants/somniguard-gateway.service`
> actually exists (its absence is the literal reason a unit doesn't come
> up on reboot even when `is-enabled` reports `enabled`).
>
> Specific signatures to look for in `sudo journalctl -u somniguard-gateway -b`:
>
> - `Failed to execute command: Permission denied` — usually means
>   `ProtectHome=true` is hiding `/home/pi/NightWatchGaurd/`. The unit
>   now uses `ProtectHome=read-only`. `fix_autostart.sh` handles this.
> - `not authorized to perform this operation` from `nmcli` — the service
>   user is not in `netdev`. `fix_autostart.sh` handles this.
> - `Failed at step EXEC spawning ...: No such file or directory` — the
>   venv path in the unit file doesn't match the actual install location.
>   `fix_autostart.sh` handles this.

### 3.5 Step 4 — First-run admin bootstrap (interactive)

Stop the systemd service and run the gateway in the foreground once so
the bootstrap prompt can read your terminal:

```bash
sudo systemctl stop somniguard-gateway
cd somniguard_gateway
sudo .venv/bin/python run.py
```

You will see:

```
[SOMNI] No users found. Creating initial admin account.
Admin username [admin]:
Admin email [admin@localhost]:
Admin password: ***********
```

The password must satisfy: 14–128 chars, upper + lower + digit + symbol,
not in the deny-list, no run of 4+ identical characters. After the prompt
the gateway keeps running. Press **Ctrl-C**, then re-enable the service:

```bash
sudo systemctl start somniguard-gateway
```

### 3.6 Step 5 — Sign in and enrol MFA

1. Connect a laptop / phone to the **`SomniGuard_Net`** Wi-Fi (the password
   is in `/etc/somniguard/env`'s `SOMNI_HOTSPOT_PASSWORD`, or auto-generated
   into `somniguard_gateway/hotspot_credentials.json` — `sudo cat` it).
2. Browse to `https://10.42.0.1:5443/`. The browser will warn about the
   self-signed cert.
3. Either accept the warning **or** install the CA cert into your trust
   store: visit `https://10.42.0.1:5443/ca.crt` to download
   `somniguard-ca.crt`, then add it to your OS / browser as a trusted
   authority. After that the warning is gone.
4. Sign in with the admin credentials from Step 4.
5. The dashboard immediately redirects to **`/mfa/setup`** — open an
   authenticator app (Google Authenticator, Microsoft Authenticator, Aegis,
   1Password, Bitwarden, Authy), scan the QR, type the 6-digit code,
   **save the 10 backup codes**.
6. From the **Patients** page, create at least one patient — note its
   `id` (it will appear in the URL `https://10.42.0.1:5443/patients/<id>`).
   You'll need the id for the Pico's `GATEWAY_PATIENT_ID`.

### 3.7 Step 6 — Verify the gateway is healthy

Tail the service log and look for three confirmation lines printed at
startup. They tell you (a) which env file the HMAC key came from, (b)
which TLS 1.2 cipher suites OpenSSL accepted from the allowlist, and (c)
the CA / server fingerprints the Pico will pin against:

```bash
sudo journalctl -u somniguard-gateway -n 120 --no-pager | grep -E "\[CONFIG\]|\[TLS\]"
```

Expected (truncated):

```
[SOMNI][CONFIG] Loaded /etc/somniguard/env: SOMNI_HMAC_KEY (sha256[:8]=977b7b05, len=64)  — must match GATEWAY_HMAC_KEY on the Pico.
[SOMNI][TLS] TLS context ready: 1.2+1.3, ECDHE+AEAD, client certs optional…
[SOMNI][TLS] TLS 1.2 cipher suites offered (10): ECDHE-ECDSA-AES256-GCM-SHA384, ECDHE-RSA-AES256-GCM-SHA384, ECDHE-ECDSA-CHACHA20-POLY1305, …, ECDHE-ECDSA-AES256-CCM8
[SOMNI][TLS] TLS 1.3 suites are negotiated automatically by OpenSSL (TLS_AES_*_GCM_SHA*, TLS_CHACHA20_POLY1305_SHA256).
[SOMNI][TLS] CA SHA-256:     93:63:ef:e9:…
[SOMNI][TLS] Server SHA-256: 60:4f:8a:fa:…
```

Note the `sha256[:8]` HMAC fingerprint — you'll match it against the Pico
boot log in §4.7. Then sanity-check the TLS endpoint:

```bash
# TLS handshake works and /api/time responds:
curl --cacert somniguard_gateway/certs/ca.crt https://10.42.0.1:5443/api/time
# → {"t":1715062800}

# CA + server fingerprints (write these down — the Pico will pin the CA):
python3 -c "
import sys; sys.path.insert(0,'somniguard_gateway')
from tls_setup import get_cert_sha256_fingerprint
print('CA:    ', get_cert_sha256_fingerprint('somniguard_gateway/certs/ca.crt'))
print('Server:', get_cert_sha256_fingerprint('somniguard_gateway/certs/server.crt'))
"
```

If both succeed, the gateway is ready and the Pico-side install can begin.

---

### 3.8 Step 7 — Enable the secrets vault (strongly recommended)

The secrets vault encrypts the four most sensitive files on the Pi —
the Flask/HMAC keys and all TLS private keys — so that if someone
physically steals the SD card they cannot read any of them.

After setup you use `sudo somniguard-start` instead of
`systemctl start somniguard-gateway`. That command asks for your
passphrase, decrypts the secrets into RAM only (never back to disk),
starts the gateway, and wipes everything from memory when you Ctrl-C.

#### Prerequisites

The gateway must be fully installed and working (Steps 1–6 above)
before you run the vault setup.

#### One-time setup

```bash
# On the Pi 5, from the project directory:
cd ~/NightWatchGaurd
sudo bash scripts/setup_file_encryption_pi5.sh
```

The script will:

1. Ask you to choose a **vault passphrase** (16+ characters, write it down).
   - This is NOT your Linux login, dashboard password, or HMAC key.
   - There is no recovery if you forget it.
2. Encrypt `/etc/somniguard/env`, `ca.key`, `server.key`, and
   `pico_client.key` with AES-256-CBC into `/var/lib/somniguard-vault/`.
3. Shred the plaintext originals from disk.
4. Install the `somniguard-start` command to `/usr/local/bin/`.
5. Disable the gateway autostart (it can no longer start without the passphrase).

Expected output (last few lines):

```
[INFO   ] =================================================================
[INFO   ]  Setup complete.
[INFO   ]
[INFO   ]  ENCRYPTED:
[INFO   ]    /etc/somniguard/env
[INFO   ]    → /var/lib/somniguard-vault/env.enc
[INFO   ]    /etc/somniguard/certs/ca.key
[INFO   ]    → /var/lib/somniguard-vault/certs/ca.key.enc
[INFO   ]  ...
[INFO   ]  TO START THE GATEWAY:
[INFO   ]    sudo somniguard-start
[INFO   ] =================================================================
```

#### Immediately after setup — back up the vault

```bash
sudo tar czf somniguard-vault-backup.tar.gz /var/lib/somniguard-vault
```

Copy `somniguard-vault-backup.tar.gz` to a USB stick or password manager
attachment. Without this backup, a corrupted SD card = **all secrets
lost permanently**.

#### Verify the vault is set up correctly

```bash
sudo bash scripts/setup_file_encryption_pi5.sh --status
```

You should see every secret listed as `ENCRYPTED`, the drop-in file
present, and the startup command installed. Any `MISSING` line means
something went wrong — re-run the setup script.

#### Starting the gateway (from now on)

**Every time you want to use the gateway, run:**

```bash
sudo somniguard-start
```

You will see:

```
  SOMNI-Guard Gateway — Encrypted Mode

Enter SOMNI-Guard passphrase: ••••••••
[INFO   ] Passphrase accepted.
[INFO   ] Decrypting secrets to RAM...
[INFO   ]   Decrypted: ca.key
[INFO   ]   Decrypted: server.key
[INFO   ]   Decrypted: pico_client.key
[INFO   ] Certs bound from RAM at /etc/somniguard/certs
[INFO   ] Starting somniguard-gateway.service ...
[INFO   ] Gateway started.

  SOMNI-Guard is running.
  Dashboard : https://10.42.0.1:5443/
  Logs      : sudo journalctl -u somniguard-gateway -f
  Stop      : Ctrl-C  (secrets wiped from memory automatically)
```

Press **Ctrl-C** to stop the gateway. The tmpfs is wiped and unmounted —
no secret remains in memory or on disk after that point.

#### All available options

| Command | What it does |
|---------|-------------|
| `sudo bash scripts/setup_file_encryption_pi5.sh` | Full setup (run once) |
| `sudo bash scripts/setup_file_encryption_pi5.sh --status` | Show what is encrypted, drop-in state, startup command |
| `sudo bash scripts/setup_file_encryption_pi5.sh --dry-run` | Preview what setup would do without changing anything |
| `sudo bash scripts/setup_file_encryption_pi5.sh --rotate-key` | Change the vault passphrase (requires the current one) |
| `sudo bash scripts/setup_file_encryption_pi5.sh --remove` | Decrypt all files back to plaintext and undo setup |
| `sudo bash scripts/setup_file_encryption_pi5.sh --help` | Show full usage |
| `sudo somniguard-start` | Decrypt secrets to RAM and start the gateway |

#### Changing the passphrase

```bash
sudo bash scripts/setup_file_encryption_pi5.sh --rotate-key
# → Enter CURRENT passphrase
# → Enter NEW passphrase (twice)
# Re-encrypts every vault file with the new key.
```

#### Undoing the vault (restoring plaintext)

```bash
sudo bash scripts/setup_file_encryption_pi5.sh --remove
# → Enter vault passphrase
# Decrypts all files back to their original paths, removes the vault,
# re-enables the gateway autostart, and removes somniguard-start.
```

#### Checking gateway logs while it is running

In a second terminal window:

```bash
sudo journalctl -u somniguard-gateway -f
```

---

## 4. Install — Pico 2 W sensor

The Pico install is run **on your development laptop** (not on the Pico
itself). You connect the Pico over USB-C, push firmware, embed the
gateway's PKI material, set the HMAC key, and reboot. Done in 3–5 minutes.

### 4.1 What you need

- A **Raspberry Pi Pico 2 W** (RP2350 with on-board Wi-Fi)
- USB-C cable
- A laptop with Python 3.8+ and `mpremote`:
  ```bash
  pip3 install --user mpremote
  ```
- The MAX30102 / ADXL345 / Grove GSR sensors wired to the Pico per the
  pin-map at the top of [`somniguard_pico/config.py`](somniguard_pico/config.py)
- The gateway from §3 already running, so we can fetch its CA cert

### 4.2 Step 1 — Flash the firmware

1. Disconnect the Pico from USB.
2. **Hold the BOOTSEL button**, then plug the Pico into your laptop. It
   mounts as a USB mass-storage volume named `RPI-RP2`.
3. Drag-and-drop **`somni_guard_complete.uf2`** (at the project root) onto
   the volume. The Pico reboots automatically into MicroPython +
   SOMNI-Guard.

> The complete UF2 bundles MicroPython for RP2350 plus the SOMNI-Guard
> firmware. If you only want a clean MicroPython build use
> `somni_guard_firmware.uf2` — but then you need to copy
> `somniguard_pico/*.py` over yourself.

### 4.3 Step 2 — Embed the gateway's PKI material into the Pico config

On the Pi 5 gateway you already generated the PKI in §3. Now copy the
**CA certificate**, the **Pico client cert**, and the **Pico client key**
into the Pico's `config.py`. Run, on the Pi 5:

```bash
cd ~/NightWatchGaurd
python3 scripts/embed_pico_cert.py
```

This atomically rewrites the three PEM blocks at the bottom of
`somniguard_pico/config.py`:

```
GATEWAY_CA_CERT_PEM   = "..."     # gateway's Root CA (trust anchor)
PICO_CLIENT_CERT_PEM  = "..."     # CA-signed cert with CN=pico-01
PICO_CLIENT_KEY_PEM   = "..."     # ECDSA P-256 private key
```

To preview the fingerprints without writing:

```bash
python3 scripts/embed_pico_cert.py --check
```

### 4.4 Step 3 — Set Wi-Fi, gateway, patient, and HMAC key

Edit `somniguard_pico/config.py` (still on the Pi 5 / your laptop) and set
the four required values:

```python
WIFI_SSID            = "SomniGuard_Net"
WIFI_PASSWORD        = "<the hotspot password>"      # see §3.6 step 1
GATEWAY_HOST         = "10.42.0.1"                   # leave as default
GATEWAY_PORT         = 5443                          # leave as default
GATEWAY_PATIENT_ID   = 1                             # the patient id from §3.6 step 6
DEVICE_ID            = "pico-01"                     # match the client-cert CN
GATEWAY_HMAC_KEY     = "<copy SOMNI_HMAC_KEY from /etc/somniguard/env on the Pi 5>"
```

The HMAC key **must** be identical on both sides — different bytes will
fail every `/api/*` call with `403 HMAC verification failed`. Two helpers
keep them aligned:

- `scripts/embed_pico_config.py` pushes `WIFI_*`, `GATEWAY_HOST`, and
  `GATEWAY_HMAC_KEY` from the Pi into the Pico firmware in one go.
- `scripts/sync_gateway_env.sh` (run on the Pi 5) reads the key out of
  `somniguard_pico/config.py` and writes it to `/etc/somniguard/env`,
  then restarts the gateway service. Run this any time you regenerate
  the Pico key — the gateway and Pico must agree byte-for-byte.

The fingerprint trick: the gateway prints
`[SOMNI][CONFIG] … sha256[:8]=977b7b05`, the Pico prints
`[SOMNI][HMAC] … sha256[:8]=977b7b05`. If those 8 hex chars match, the
keys match; if they don't, run one of the helpers above.

### 4.5 Step 4 — Push the Pico source to the device

From the project root, with the Pico plugged in:

```bash
# Find the Pico's serial port:
mpremote connect list
# Then push every file (you may need to substitute the port)
cd somniguard_pico
mpremote cp boot.py main.py config.py transport.py sampler.py utils.py \
            integrity.py crypto_loader.py secure_config.py :
mpremote cp -r drivers :
```

Or, if you have the convenience wrapper:

```bash
bash setup_pico.sh 10.42.0.1
```

### 4.6 Step 5 — (Optional) encrypt the firmware at rest

For deployments where the Pico might leave your control:

```bash
python3 scripts/encrypt_pico_files.py
mpremote cp config.enc :
mpremote rm :config.py            # leave only the encrypted copy
mpremote exec "from boot import lock_usb; lock_usb()"
mpremote reset
```

`crypto_loader.py` decrypts `config.enc` at boot using a key derived from
the RP2350 flash UID. `boot.py` then blocks the USB-CDC REPL and remounts
the filesystem read-only.

**Two escape hatches** (the old "hold BOOTSEL at power-on" soft path no
longer works on the Pico 2 W — see micropython#16908, where
`rp2.bootsel_button()` always returns 1 on RP2350):

1. **Soft bypass — `maintenance.flag` file.** Run this *before* the
   device is locked (or from a host that can still reach the unlocked
   device):
   ```bash
   mpremote connect /dev/cu.usbmodem2101 fs touch :maintenance.flag
   mpremote connect /dev/cu.usbmodem2101 reset
   ```
   The next boot prints `maintenance.flag present — USB lockdown skipped`
   and leaves USB open. Remove the file and reset to re-arm:
   ```bash
   mpremote connect /dev/cu.usbmodem2101 fs rm :maintenance.flag
   mpremote connect /dev/cu.usbmodem2101 reset
   ```

2. **Hard recovery — physical BOOTSEL + power cycle.** Hold the BOOTSEL
   button on the board *while plugging the USB cable in*. The RP2350
   *ROM bootloader* takes over (entirely below MicroPython — this is
   hardware and the #16908 bug doesn't affect it) and the device mounts
   as a USB mass-storage drive named `RP2350`. Drag any MicroPython
   `.uf2` onto it to wipe the filesystem and reflash. This is the
   ultimate recovery path; you'll have to repeat `setup_pico.sh` after.

### 4.7 Step 6 — Verify the Pico is talking to the gateway

Reset the Pico and watch its serial console:

```bash
mpremote reset
mpremote
```

You should see, in order:

```
[SOMNI][BOOT]   ... Secure Boot
[SOMNI][WIFI]   Connecting to 'SomniGuard_Net'…
[SOMNI][WIFI]   Connected. IP: 10.42.0.x
[SOMNI][HMAC]   GATEWAY_HMAC_KEY sha256[:8]=977b7b05 len=64  — must match gateway's [SOMNI][CONFIG] line.
[SOMNI][TRANSPORT]   Clock synced via HTTPS: Unix=1715062800
[SOMNI][TRANSPORT][TLS] Connected (DER, SSLContext + load_cert_chain).
[SOMNI][TRANSPORT][TLS] DER via SSLContext+load_cert_chain → TLSv1.3 / TLS_AES_128_GCM_SHA256
[SOMNI][TRANSPORT]   Session started: ID 1
[SOMNI][SAMPLER] tx batch: 5 readings → HTTP 200
```

The two lines to eyeball:

- **`[SOMNI][HMAC]`** — the `sha256[:8]` value must match the gateway's
  `[SOMNI][CONFIG] Loaded /etc/somniguard/env: SOMNI_HMAC_KEY (sha256[:8]=…)`
  line from §3.7. A mismatch is the only cause of `403 HMAC verification
  failed`; fix it with `scripts/sync_gateway_env.sh` on the Pi 5.
- **`[SOMNI][TRANSPORT][TLS] … → TLSv1.3 / TLS_AES_128_GCM_SHA256`** — the
  negotiated TLS version + cipher suite, printed on every successful
  handshake so you know which suite the Pico actually negotiated. The
  exact suite depends on what your MicroPython mbedTLS build exposes
  (typically `TLS_AES_128_GCM_SHA256` for TLS 1.3, or one of the
  `ECDHE-ECDSA-AES*-GCM` / `ECDHE-ECDSA-AES*-CCM` suites for TLS 1.2).
  If the line reads `<cipher info unavailable on this MicroPython build>`
  the handshake still succeeded — the binding just doesn't expose
  `sock.cipher()`.

> **About the DER label.** The Pico's `transport.py` tries DER credentials
> before PEM because the RP2350 MicroPython mbedTLS build's PEM parser is
> brittle (micropython#14371) and rejects valid PEM blobs with
> `invalid cert` / `invalid key`. DER bypasses that path entirely, so a
> healthy boot now logs **one** `Connected (DER, …)` line instead of three
> `[PEM-bytes] … failed` lines followed by a DER fallback. PEM is still
> kept as a fallback for builds where DER for some reason isn't available.

Then on the gateway dashboard:

- **Devices** page shows `pico-01` as **online**
- **Live monitor** renders SpO₂ and HR sparklines for the active session
- The session is visible under the patient detail page

#### MAX30102 sensor tuning notes

The driver in `somniguard_pico/drivers/max30102.py` is tuned for stable
sleep-monitoring readings:

- **SpO₂ formula** — uses the calibrated polynomial
  `SpO₂ ≈ -45.060·R² + 30.354·R + 94.845` from the Maxim MAX30102
  reference design (replaces the cruder linear `110 − 25R` from v0.4,
  which over-predicted by 2–4 percentage points in the 90–100 % range).
  The output is clamped to a physiologically plausible 70–100 %.
- **AC/DC window** — computed over the most-recent **1-second** slice
  of the buffer rather than the full 6 s, so AC reflects the actual
  pulsatile amplitude instead of baseline drift over many cycles.
- **HR algorithm** — peaks are detected on a 50 ms-smoothed copy of the
  IR PPG, and HR is computed from the **median** of inter-peak intervals
  (not the mean) so a single spurious peak doesn't drag the result.
- **Output smoothing** — both HR and SpO₂ are passed through an
  exponential moving average (α = 0.3) across reads. New estimates are
  weighted 30 %, the previous smoothed value 70 %. The visible numbers
  no longer flicker; real changes still propagate in 2–3 reads.
- **Buffer length** — 6 seconds at 100 sps (= 600 samples). Gives
  4–6 cardiac cycles in the buffer at typical sleep heart rates.
- **No-finger detection** — bails out when raw IR < 5 000 counts. Clears
  both the buffer *and* the EMA state, so the first reading after a
  re-applied finger isn't averaged with stale numbers.

If HR still looks jumpy in your environment:

- Confirm the finger is fully covering the sensor with light steady
  pressure (over-pressing collapses the capillary bed and kills AC).
- Increase `MAX30102_LED_AMPLITUDE` in `somniguard_pico/config.py` from
  `0x7F` (25.4 mA) to `0xFF` (51.0 mA) for diagnostics — only for short
  spot-checks; this dissipates significant heat.
- Watch the raw `IR=…` values printed on the serial console. Stable
  finger contact should show IR in the 50 000–250 000 range.

### 4.8 What the Pico does on every boot

```
power on
  ├─► boot.py (USB lockdown if locked, BOOTSEL bypass)
  ├─► main.py
  │     ├─► (optional) crypto_loader.py    decrypts config.enc
  │     ├─► transport.connect_wifi()       WPA2 to SomniGuard_Net
  │     ├─► transport.sync_time_from_gateway()
  │     │       └─► HTTPS GET /api/time    (TLS works even before clock sync —
  │     │                                    server cert not_before = 2000-01-01)
  │     ├─► transport.start_session()
  │     │       └─► HTTPS POST /api/session/start  (mTLS + HMAC)
  │     └─► loop:
  │           ├─► sampler.read()           MAX30102 + ADXL345 + GSR
  │           └─► transport.send_api()     HTTPS POST /api/ingest (mTLS + HMAC)
  └─► on shutdown / patient finished:
        └─► transport.end_session()        HTTPS POST /api/session/end
```

Every `/api/*` packet carries an HMAC over the JSON body and a strictly
increasing nonce — replays and stale timestamps are dropped with
`403 replay detected`.

---

## 5. First-run admin bootstrap (mandatory MFA)

When the gateway starts with an empty database **and** stdin is a terminal,
it prompts for an initial admin account:

```
[SOMNI] No users found. Creating initial admin account.
Admin username [admin]: <name or default>
Admin email [admin@localhost]: <email>
Admin password: ********
```

Passwords must satisfy the NIST-aligned complexity rules in
[`somniguard_gateway/security.py`](somniguard_gateway/security.py): 14-128
characters, upper + lower + digit + symbol, not in the common-password
deny-list, no run of 4+ identical characters.

> **MFA is mandatory for every account, including the bootstrap admin.**
> The first time the admin signs in, the dashboard immediately redirects to
> `/mfa/setup` and refuses to render any other page until enrolment is
> complete. Have an authenticator app
> (Google Authenticator, Microsoft Authenticator, Aegis, 1Password,
> Bitwarden, Authy, …) ready before you log in.

If the gateway was started under systemd / gunicorn (no terminal), bootstrap
is skipped — create the admin manually with the helper script:

```bash
cd somniguard_gateway
SOMNI_DB_PATH=/var/lib/somniguard/somni.db \
python3 ../scripts/seed_db.py --admin-username admin --admin-email admin@local
```

### 5.1 Walking the admin through MFA enrolment

1. Browse to `https://10.42.0.1:5443/`.
2. Enter username + password.
3. The dashboard recognises the admin has no MFA secret, logs the user in,
   and forwards to **`/mfa/setup`**.
4. Open the authenticator app, scan the QR code (the secret is also shown
   as a manual-entry string — useful for headless devices).
5. Enter the 6-digit TOTP. The gateway flips the secret to `enabled=1` and
   immediately renders the **backup codes** page.
6. Save the ten one-time backup codes in a password manager. Click
   **"I have saved my backup codes"** to land on the dashboard.

Subsequent logins follow the standard two-step flow:

```
/login (username + password) → /mfa/verify (TOTP or backup code) → /dashboard
```

If the admin loses their authenticator they can still consume one of the
backup codes; an admin (themselves or another) can reset MFA from
**Users → Reset MFA** which forces a fresh enrolment on next login.

---

## 6. Web interface — feature tour

The navigation bar shows the active page and a red badge with the count of
**unacknowledged alerts**.

| Page | Route | Purpose |
|------|-------|---------|
| Dashboard | `/dashboard` | Stats grid, recent sessions, recent audit (admin) |
| Live monitor | `/live` | Real-time vitals tiles for every active session, auto-refreshing every 4 s, with inline SVG sparklines and per-tile alert highlights |
| Patients | `/patients` | List, create, archive, restore. Extended demographics (MRN, sex, height, weight, contact, allergies) |
| Patient detail | `/patients/<id>` | Demographics + edit + sessions list + archive |
| Session detail | `/sessions/<id>` | Telemetry table, sleep summary, alerts, clinical notes, discharge, PDF report |
| Alerts | `/alerts` | Filter unacknowledged vs all; acknowledge inline |
| Devices | `/devices` | Pico fleet — online/offline state, battery, RSSI, IP, firmware, last patient, last session |
| Users (admin) | `/admin/users` | Create user, reset password, reset MFA, activate / deactivate, delete |
| Audit (admin) | `/admin/audit` | Filter by event type / username / limit; **CSV export** at `/admin/audit.csv` |
| Account | `/account` | Self-service profile, password change, MFA backup-code regeneration |

All clinically sensitive routes (everything except `/login`, `/mfa/*`,
`/api/*`, `/static/*`, `/ca.crt`) are gated by `_enforce_mfa_and_password_gates`
— users without MFA are redirected to `/mfa/setup`; users whose
`must_change_password` flag is set go to `/account/password`. There is no
back-door.

---

## 7. Authentication & MFA

### 7.1 Login flow

```
┌────────┐  password ok   ┌──────────┐  TOTP / backup ok   ┌───────────┐
│ /login │ ─────────────▶ │ /mfa/    │ ──────────────────▶ │ /dashboard│
└────────┘                │ verify   │                     └───────────┘
                          └──────────┘
   wrong pw → 0.5 s delay + lockout counter increments
   wrong code → 0.5 s delay + lockout counter increments
   10 wrong attempts in 15 min → IP locked for 15 min
```

Password and MFA failures use a constant-time delay and bump the same
[`LoginTracker`](somniguard_gateway/security.py) lockout counter. After
**10** consecutive failures from a single IP the IP is locked for **15
minutes**.

### 7.2 Pre-auth session ticket

Between Phase 1 and Phase 2 the gateway stores only the user's primary key
in the Flask session:

```
session["mfa_pending_user_id"]   # who you said you were
session["mfa_pending_issued_at"] # epoch — TTL 5 minutes
```

Flask-Login is **not** engaged until Phase 2 completes, so an attacker who
captured the cookie at Phase 1 cannot reach any authenticated route.

### 7.3 TOTP details

- 30-second period, ±1-step verification window.
- `mfa_secrets.last_used_step` records the most recent step that succeeded;
  re-submitting the same code in the same window is rejected.
- TOTP secrets are stored encrypted at rest with Fernet, derived from
  `SOMNI_MFA_KEY` (defaults to `SOMNI_SECRET_KEY`).
- Backup codes: ten 10-digit single-use codes per enrolment, stored as
  bcrypt hashes; can be regenerated from `/account` (invalidates the prior
  set).

### 7.4 Resetting a lost authenticator

- **Self-service** — consume one backup code at `/mfa/verify`.
- **Admin** — Users → **Reset MFA** for the user. They will be asked to
  enrol again on next login.

---

## 8. User management

### 8.1 Roles

| Role | Powers |
|------|--------|
| `admin` | Everything: user management, MFA reset, audit log, all clinical actions |
| `doctor` | Manage patients, generate / download reports, add / delete clinical notes, discharge sessions |
| `clinician` | Manage patients, generate reports, add notes, discharge sessions |
| `nurse` | Manage patients, add notes, acknowledge alerts |
| `viewer` | Read-only |

### 8.2 Admin actions

- **Create user** — username, email, role, 14+ char password.
- **Reset password** — generates a strong 18-character temporary password,
  shows it once, marks `must_change_password=1`. The user is forced through
  `/account/password` on their next sign-in.
- **Reset MFA** — clears the user's TOTP secret + backup codes. They go
  through `/mfa/setup` again on next login.
- **Activate / deactivate** — flips `is_active`. A deactivated user cannot
  log in even with valid credentials.
- **Delete** — permanent removal (cascades to MFA tables).

You cannot delete or deactivate yourself.

---

## 9. Patient & session workflow

### 9.1 Patient demographics

Patient records carry a Medical Record Number (MRN), DOB, sex, contact
phone / email, allergies, height, weight, free-text notes, and a soft-delete
**archive** flag. Archived patients are hidden by default; toggle "Show
archived too" on the patient list.

### 9.2 Session lifecycle

```
Pico powers on
   └─► /api/session/start  → INSERT sessions row, started_at = now
       │   (HMAC + nonce)
       ▼
   /api/ingest (loop)       → INSERT telemetry rows, evaluate alerts,
       │   (HMAC + nonce)     upsert device fleet status
       ▼
   /api/session/end         → UPDATE ended_at = now
       (HMAC + nonce)

   Clinician opens /sessions/<id>:
       - "Generate report"  → SHA-256 + HMAC-signed PDF saved to REPORT_DIR
       - "Discharge"        → stamps discharged_at + notes, ends ongoing session
       - Add clinical notes → free-text observations, author + timestamp recorded
```

Clinical notes are CRUD'd from the session detail page (admin/doctor can
delete; admin/doctor/clinician/nurse can add).

---

## 10. Clinical alerts

Alerts are evaluated inside `/api/ingest` against the thresholds in
[`somniguard_gateway/app.py`](somniguard_gateway/app.py):

| Key | Severity | Default threshold |
|-----|----------|-------------------|
| `spo2_critical_low` | critical | SpO₂ &lt; 85 % |
| `spo2_warning_low`  | warning  | SpO₂ &lt; 90 % |
| `hr_critical_low`   | critical | HR &lt; 40 bpm |
| `hr_warning_low`    | warning  | HR &lt; 50 bpm |
| `hr_warning_high`   | warning  | HR &gt; 120 bpm |
| `hr_critical_high`  | critical | HR &gt; 140 bpm |

Duplicate alerts are suppressed within a 60-second window per
`(session_id, key)`, so a steady-state desat does not generate a storm.

Alert lifecycle:

```
Threshold breach in ingest
   └─► db.insert_alert(...)
       │
       ▼
   Visible immediately in:
     • /alerts
     • /sessions/<id> (Alerts panel)
     • /live (red border on the affected tile, plus a list of open alerts)
     • Nav badge "Alerts (N)"

Clinician clicks "Acknowledge" (admin/doctor/clinician/nurse only)
   └─► alerts.acknowledged_at + acknowledged_by stamped
   └─► AUDIT: ALERT_ACKNOWLEDGED
```

These thresholds are educational defaults and are **not** validated
against any clinical guideline. Tune `ALERT_THRESHOLDS` in `app.py` for your
deployment.

---

## 11. Device fleet

Every Pico that sends a `session/start` or `ingest` packet upserts a row in
the `devices` table. The `/devices` page surfaces:

- last-seen timestamp + colour-coded online / offline (90-second window),
- battery percentage (red &lt; 15 %, amber &lt; 30 %),
- Wi-Fi RSSI in dBm,
- last known IP, firmware version,
- the most recent patient + session associated with the device.

To populate battery / RSSI / firmware fields, include `battery_pct`,
`rssi_dbm`, and `fw_version` in the JSON body of `/api/ingest`. They are
optional — when absent the device is still tracked, just without those
fields.

---

## 12. Audit log

Every security-relevant event is written to:

1. The console (human-readable: `[SOMNI][AUDIT] ...`).
2. A rotating JSON file (`audit.log`, 10 MB × 5 backups) next to the
   SQLite database.
3. The `audit_log` SQL table (paginated UI viewer).

Event types include:

```
LOGIN_SUCCESS  LOGIN_FAILURE  LOGIN_LOCKOUT  LOGOUT
MFA_SUCCESS    MFA_FAILURE    MFA_ENROLLED   MFA_RESET_BY_ADMIN
MFA_BACKUP_CODES_REGENERATED
PASSWORD_CHANGED  ADMIN_PASSWORD_RESET
USER_CREATED  USER_DELETED  USER_ACTIVATED  USER_DEACTIVATED
DATA_ACCESS  REPORT_GENERATED  REPORT_DOWNLOADED
API_ACCESS  REPLAY_DETECTED  INGEST_REJECTED  ACCESS_DENIED  PATH_TRAVERSAL_ATTEMPT
ALERT_ACKNOWLEDGED  AUDIT_LOG_EXPORTED
```

Admin → **Audit** filters by event type, username and row limit; the
**Export CSV** button emits `audit_log_<timestamp>.csv` (capped at 10 000
rows; itself logged as `AUDIT_LOG_EXPORTED`).

---

## 13. REST API reference

All `/api/*` endpoints expect HTTP `POST` with a JSON body that includes a
`hmac` field. The HMAC is `HMAC-SHA256(SOMNI_HMAC_KEY, canonical_payload)`
where `canonical_payload` is the JSON of the body **without the `hmac`
key**, sorted by key, with **compact** separators (`","`, `":"`) — match
that exactly or verification fails.

### 13.1 `POST /api/session/start`

```json
{
  "patient_id": 1,
  "device_id":  "pico-01",
  "nonce":      1,
  "timestamp":  1715062800,
  "fw_version": "0.4.1",
  "hmac":       "…"
}
```

Response: `201 {"session_id": <int>}`.

### 13.2 `POST /api/ingest`

```json
{
  "session_id":   42,
  "device_id":    "pico-01",
  "timestamp_ms": 1715062801234,
  "nonce":        2,
  "timestamp":    1715062801,
  "spo2":  {"valid": true, "spo2": 96.4, "hr": 71.0,
            "ir_raw": 12345, "red_raw": 9876},
  "accel": {"valid": true, "x": 0.01, "y": -0.02, "z": 0.99},
  "gsr":   {"valid": true, "raw": 1900, "voltage": 1.21,
            "conductance_us": 3.7},
  "battery_pct": 84.0,
  "rssi_dbm":    -52,
  "fw_version":  "0.4.1",
  "hmac":        "…"
}
```

Response: `200 {"ok": true}`.

Order of validation (cheap → expensive):

1. body size ≤ 8 KB
2. JSON parse
3. schema + numeric bounds (rejects fuzz before HMAC)
4. HMAC verify
5. nonce strictly increasing within a session
6. timestamp within ±5 minutes
7. DB insert
8. alert evaluation (best-effort)
9. device-fleet upsert (best-effort)

### 13.3 `POST /api/session/end`

```json
{"session_id": 42, "nonce": 999, "timestamp": 1715063000, "hmac": "…"}
```

Response: `200 {"ok": true}`. Cleans up nonce tracking.

### 13.4 `GET /api/time`

Unauthenticated. Returns `{"t": <unix-seconds>}` so the Pico can correct
its 2000-epoch clock without public NTP.

### 13.5 `GET /api/tailscale/status`

Authenticated, **admin only**. Returns the local Tailscale IP, hostname,
peers, and whether Tailscale-only mode is on.

### 13.6 Complete URL map

Every route the gateway exposes, grouped by purpose:

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/` | none | Redirect to dashboard or login |
| GET | `/ca.crt` | none | Download the CA cert for browser trust |
| GET, POST | `/login` | none | Phase 1 — username + password |
| GET, POST | `/mfa/verify` | pre-auth ticket | Phase 2 — TOTP / backup code |
| GET | `/logout` | login | Clear session and pre-auth ticket |
| GET, POST | `/mfa/setup` | login | Enrol TOTP (mandatory) |
| GET | `/mfa/qr.png` | login | PNG QR code for the provisioning URI |
| POST | `/mfa/backup-codes` | login | Regenerate backup codes |
| POST | `/mfa/disable` | admin | Reset MFA for a target user |
| GET, POST | `/account/password` | login | Self-service password change |
| GET | `/account` | login | User profile + MFA management |
| GET | `/dashboard` | login | Stat grid + recent sessions |
| GET | `/live` | login | Real-time multi-patient monitor |
| GET | `/live/data` | login | JSON snapshot used by the live monitor poller |
| GET | `/patients` | login | Patient list (active / archived) |
| POST | `/patients/new` | clinician+ | Create a patient |
| GET | `/patients/<id>` | login | Patient detail + sessions |
| POST | `/patients/<id>/edit` | clinician+ | Update demographics |
| POST | `/patients/<id>/archive` | doctor+ | Archive or restore (`?restore=1`) |
| GET | `/sessions/<id>` | login | Session detail (telemetry, alerts, notes) |
| POST | `/sessions/<id>/notes/add` | clinician+ | Add a clinical observation |
| POST | `/sessions/<id>/notes/<n>/delete` | doctor+ | Remove a clinical observation |
| POST | `/sessions/<id>/discharge` | doctor+ | Stamp `discharged_at` |
| POST | `/sessions/<id>/report` | doctor+ | (Re-)generate the PDF report |
| GET | `/sessions/<id>/report/download` | doctor+ | Download the signed PDF |
| GET | `/alerts` | login | Filterable alert queue (`?filter=unack|all`) |
| POST | `/alerts/<id>/ack` | clinician+ | Acknowledge an alert |
| GET | `/devices` | login | Pico fleet — online / offline / battery / RSSI |
| GET | `/admin/users` | admin | User-management page |
| POST | `/admin/users/new` | admin | Create user |
| POST | `/admin/users/<id>/delete` | admin | Permanently delete (cannot self-delete) |
| POST | `/admin/users/<id>/reset-password` | admin | Issue temp password + force change |
| POST | `/admin/users/<id>/toggle-active` | admin | Activate / deactivate (cannot self-deactivate) |
| GET | `/admin/audit` | admin | Audit log viewer |
| GET | `/admin/audit.csv` | admin | Audit log CSV export |
| POST | `/api/session/start` | HMAC | Pico — open a session |
| POST | `/api/ingest` | HMAC | Pico — telemetry packet |
| POST | `/api/session/end` | HMAC | Pico — close a session |
| GET | `/api/time` | none | Wall-clock for Pico clock sync |
| GET | `/api/tailscale/status` | admin | Local Tailscale daemon state |
| GET | `/static/<path>` | none | Bundled JS / CSS (`script-src 'self'`) |

"clinician+" = `admin`, `doctor`, `clinician`, `nurse`. "doctor+" = `admin`,
`doctor`, `clinician`. "admin" = `admin` only.

---

## 14. Configuration & environment variables

| Variable | Default | Effect |
|----------|---------|--------|
| `SOMNI_DB_PATH`            | `<gateway>/somniguard.db` | SQLite file |
| `SOMNI_REPORT_DIR`         | `<gateway>/reports`       | PDF output dir |
| `SOMNI_SECRET_KEY`         | **required, no default**  | Flask cookie + WTF CSRF + MFA wrap key. Gateway refuses to start if missing. |
| `SOMNI_CSRF_KEY`           | = `SOMNI_SECRET_KEY`      | Override CSRF key |
| `SOMNI_MFA_KEY`            | = `SOMNI_SECRET_KEY`      | Wrap key for stored TOTP secrets |
| `SOMNI_MFA_ISSUER`         | `SOMNI-Guard`             | Authenticator-app issuer label |
| `SOMNI_HMAC_KEY`           | **required, no default**  | Pico ↔ gateway HMAC key. Must equal the Pico's `GATEWAY_HMAC_KEY`. Gateway refuses to start if missing. |
| `SOMNI_HTTPS`              | `true`                    | TLS on; set `false` only for local debug |
| `SOMNI_HOST` / `SOMNI_PORT`| `0.0.0.0` / `5443`        | Bind |
| `SOMNI_DEBUG`              | `false`                   | Flask debug mode |
| `SOMNI_WORKERS`            | `2`                       | gunicorn workers |
| `SOMNI_THREADS`            | `4`                       | gunicorn threads per worker |
| `SOMNI_TAILSCALE_ONLY`     | `false`                   | Restrict dashboard to Tailscale CGNAT |
| `SOMNI_PICO_CIDRS`         | `10.42.0.0/24,127.0.0.0/8`| LAN CIDRs allowed to hit `/api/*` |
| `SOMNI_HOTSPOT`            | `true`                    | Auto-start NetworkManager hotspot |
| `SOMNI_HOTSPOT_SSID`       | `SomniGuard_Net`          | Hotspot SSID |
| `SOMNI_HOTSPOT_IFACE`      | `wlan0`                   | Wi-Fi interface name |
| `SOMNI_HOTSPOT_CREDS`      | `<gateway>/hotspot_credentials.json` | Path to JSON file storing the auto-generated WPA2 password (created with mode 0600) |
| `SOMNI_HOTSPOT_PASSWORD`   | (random)                  | Fixed WPA2 password — overrides random generation |

**`SOMNI_SECRET_KEY` and `SOMNI_HMAC_KEY` are mandatory** —
`somniguard_gateway/config.py` calls `_required(...)` for both and the
process aborts with a clear error if either is missing or empty in
`/etc/somniguard/env`. There is no "dev placeholder" fallback. On a
successful start the gateway logs one `[SOMNI][CONFIG] Loaded
/etc/somniguard/env: SOMNI_HMAC_KEY (sha256[:8]=…)` line so you can
verify which env file the key came from and confirm it matches the
Pico's `[SOMNI][HMAC]` fingerprint without exposing the key itself.

---

## 15. Security model

Defence-in-depth layers, in roughly the order an attacker would hit them:

1. **Wi-Fi.** WPA2-PSK on the hotspot. Run `iwconfig` to see clients.
2. **Network policy.** When `SOMNI_TAILSCALE_ONLY=true`, the dashboard is
   only reachable from `100.64.0.0/10` (Tailscale CGNAT) or loopback. The
   `/api/*` paths are additionally allowed from `SOMNI_PICO_CIDRS`.
3. **TLS 1.2 + 1.3 only.** Strict cipher allowlist
   (`ECDHE-ECDSA-AES256-GCM-SHA384` …; see
   [`tls_setup.py`](somniguard_gateway/tls_setup.py)). The gateway logs
   the full accepted cipher list at startup
   (`[SOMNI][TLS] TLS 1.2 cipher suites offered (n): …`); the Pico logs
   the negotiated suite per connection
   (`[SOMNI][TRANSPORT][TLS] DER via … → TLSv1.3 / TLS_AES_128_GCM_SHA256`).
4. **mTLS — optional.** The gateway requests the Pico's client cert
   (`CERT_OPTIONAL`); the Pico presents one. Browsers don't, and use session
   auth instead.
5. **HMAC-SHA256 per packet.** Independent of TLS; keys never leave the
   device. Stops a compromised CA from injecting telemetry. The shared
   key is loaded from `/etc/somniguard/env` (no fallback) and a
   `sha256[:8]` fingerprint of it is logged at gateway and Pico startup,
   so a key drift between the two ends is visible at a glance without
   exposing the key.
6. **Anti-replay.** Strictly increasing nonce per session, ±5-minute
   timestamp window, in-memory HWM table evicted at 1 000 sessions.
7. **Input fuzzing defence.** Hard 8 KB body cap on `/api/*`, schema +
   numeric bounds check before the HMAC verify call (cheaper rejection).
8. **Two-factor authentication.** Mandatory for every account; primary
   login does not engage Flask-Login.
9. **Rate limiting.** `5 per minute` on `/login` and `/mfa/verify`,
   `20 per second` on `/api/*`, `200 per day / 50 per hour` everywhere
   else.
10. **Account lockout.** 10 failed attempts in 15 minutes → IP locked.
11. **Password policy.** 14-128 chars, four character classes, deny-list,
    no 4-run repeats. Bcrypt @ rounds=12.
12. **Session cookies.** `HttpOnly`, `SameSite=Lax`, `Secure` (when HTTPS),
    30-minute idle timeout, `session_protection="strong"`.
13. **CSRF.** Flask-WTF on every state-changing form; `/api/*` is exempt
    (HMAC is stronger).
14. **Security headers.** HSTS, CSP (`default-src 'self'`,
    `script-src 'self'` — no inline JS, no CDN), X-Frame-Options DENY,
    Permissions-Policy with everything off, COOP/COEP/CORP set, no Server
    or X-Powered-By leakage.
15. **Audit log.** Append-only console + rotating JSON file + DB table.
16. **PDF report integrity.** Each report's JSON summary is HMAC-signed
    and stored alongside the PDF path.
17. **Encryption (optional, recommended).**
    Two independent layers — pick one or both.

    | Layer | Script | What is protected | When passphrase is required |
    |-------|--------|-------------------|-----------------------------|
    | **(a) File-level secrets** | `scripts/setup_file_encryption_pi5.sh` | Gateway secrets only (Flask key, HMAC key, TLS private keys) — encrypted with AES-256-CBC, decrypted into RAM at runtime | When you run `sudo somniguard-start` — never at raw boot |
    | **(b) Full-disk** | `scripts/setup_full_disk_encryption_pi5.sh` | Entire root partition — every byte of the OS, /home, /var, /tmp | initramfs asks "Please unlock disk cryptroot:" *before* `init` runs |

    They are independent and complement each other.  Layer (a) is
    the recommended starting point: simpler to set up, no boot-time
    crypttab complexity, and secrets never touch the SD card in
    plaintext.  Layer (b) encrypts everything but requires a passphrase
    at the physical console on every boot.

    **The passphrases are completely independent** — do not reuse them.

    #### Layer (a) — File-level secrets encryption (`setup_file_encryption_pi5.sh`)

    Encrypts the four most sensitive gateway files with
    **AES-256-CBC + PBKDF2-SHA256 (600 000 iterations)**:

    | File | What it contains |
    |------|-----------------|
    | `/etc/somniguard/env` | Flask secret key, HMAC key, hotspot password |
    | `/etc/somniguard/certs/ca.key` | CA private key (signs cert renewals) |
    | `/etc/somniguard/certs/server.key` | TLS server private key |
    | `/etc/somniguard/certs/pico_client.key` | Pico mTLS client private key |

    Encrypted copies live at `/var/lib/somniguard-vault/` (mode 700,
    root-only). Public certificates (`*.crt`, `*.pem`), the SQLite
    database, and audit logs stay on disk — they are not secret.

    **Runtime flow** — when you run `sudo somniguard-start`:

    1. You type the vault passphrase.
    2. A RAM-only tmpfs is created at `/run/somniguard-secrets/` —
       never touches the SD card.
    3. Each vault file is decrypted into the tmpfs.
    4. The certs directory is bind-mounted from tmpfs (gateway reads
       private keys from RAM only).
    5. A systemd drop-in points `EnvironmentFile` at the tmpfs env file.
    6. `somniguard-gateway.service` starts.
    7. Ctrl-C → tmpfs is shredded and unmounted. No secret survives.

    **Full setup guide with all commands: see §3.8.**

    #### Layer (b) — TRUE full-disk encryption (`setup_full_disk_encryption_pi5.sh`)

    LUKS2-encrypts the **entire root partition** in three phases. Based
    on the proven Bookworm + Pi 5 workflow at
    [jollycar/LUKS-on-Raspberry-Pi-5](https://github.com/jollycar/LUKS-on-Raspberry-Pi-5)
    and the discussion at
    [forums.raspberrypi.com/viewtopic.php?t=363826](https://forums.raspberrypi.com/viewtopic.php?t=363826).
    The script wraps that workflow with strong pre-flight checks,
    automatic device detection, file-by-file backup, idempotency, and a
    machine-generated Phase-2 cheatsheet so you never have to memorise
    cryptsetup flags at the initramfs prompt.

    **Hardware prerequisite.** Pi 5 with the **4 KB-page kernel
    (`kernel8.img`).** The 16 KB-page kernel that ships in some Trixie
    builds breaks LUKS device-mapper bring-up
    ([raspberrypi/trixie-feedback#5](https://github.com/raspberrypi/trixie-feedback/issues/5)).
    The script aborts if it detects 16 KB pages.

    Phase 1 — preparation (run from the booted Pi):
    ```bash
    sudo bash scripts/setup_full_disk_encryption_pi5.sh --check     # pre-flight
    sudo bash scripts/setup_full_disk_encryption_pi5.sh --prepare   # do it
    ```
    This installs `cryptsetup-initramfs busybox initramfs-tools`,
    patches `/etc/initramfs-tools/{modules,hooks/luks_hooks}`,
    rebuilds `/boot/firmware/initramfs.gz` with the
    `cryptsetup`/`resize2fs`/`fdisk` binaries, appends `initramfs
    initramfs.gz followkernel` to `/boot/firmware/config.txt`,
    rewrites `/boot/firmware/cmdline.txt` so `root=` points at
    `/dev/mapper/cryptroot` and the kernel drops into a busybox
    `(initramfs)` shell on next boot (`break=init`), and writes
    `/boot/firmware/SOMNI_FDE_PHASE2.txt` with the verbatim commands
    you need from that shell. Files mutated are backed up under
    `/var/lib/somniguard/fde-state/backups.<timestamp>/`.

    Phase 2 — encryption (run from the initramfs shell after first reboot):

    Power-cycle the Pi. The next boot pauses at `(initramfs) _`.
    Read `SOMNI_FDE_PHASE2.txt` (it lives on the FAT boot partition,
    so you can mount the SD card in your laptop and `cat` it if you
    don't have a screen). The exact commands depend on the mode:

    - **Default — dd round-trip via USB stick.** Plug a USB stick at
      least as large as the *used* space on `/`. The cheatsheet walks
      you through `e2fsck`, `resize2fs -M`, the `dd ... if=<root>
      of=/dev/sda` backup, `cryptsetup luksFormat`, `cryptsetup
      luksOpen`, `dd ... of=/dev/mapper/cryptroot` restore, and
      `resize2fs` back up. Roughly 5–15 minutes on a Pi 5 / NVMe.
    - **`--in-place` (no USB needed).** Run Phase 1 with
      `--in-place` so the cheatsheet uses
      `cryptsetup reencrypt --encrypt --reduce-device-size 32M`. No
      second device required, but slower (often 30+ minutes) and
      every byte that fails to be encrypted is unrecoverable, so
      reliable power is mandatory — connect the Pi to a UPS first.

    Either path culminates in a single `cryptsetup luksFormat` prompt:

    ```
    Verify passphrase:
    ```

    **That passphrase is the BOOT PASSPHRASE.** Every subsequent boot
    will display:

    ```
    Please unlock disk cryptroot:
    ```

    …and refuse to continue until you type the same passphrase.
    Choose carefully:

    - **At least 16 characters.** This passphrase guards everything;
      anything shorter falls to GPU brute-force.
    - **Memorable but not guessable.** There is **no recovery**.
    - **Distinct from your Linux login, your dashboard admin
      password, and the SOMNI-Guard SECURE VOLUME passphrase from
      Layer (a).** Reuse means one breach unlocks the lot.
    - **Write it down on paper and store it in a safe** (or seal a
      password-manager attachment). This is industry-standard
      operational practice for any LUKS deployment.

    Once Phase 2 finishes, `exit` the initramfs shell. The kernel
    completes boot, prompts you for the passphrase a second time
    (this is `/etc/crypttab` mounting the same volume for the live
    system — same passphrase, same prompt format), and you reach the
    login.

    Phase 3 — finalisation (run from the booted, encrypted Pi):
    ```bash
    sudo bash scripts/setup_full_disk_encryption_pi5.sh --finalize
    ```
    Strips `break=init` from `cmdline.txt` so future boots skip the
    rescue shell, rebuilds the initramfs cleanly, deletes the
    Phase-2 cheatsheet (no longer needed; it doesn't contain the
    passphrase but it does describe device layout), and records
    `phase=finalized` in `/var/lib/somniguard/fde-state/phase`.

    **Roll back BEFORE Phase 2** if you change your mind:
    ```bash
    sudo bash scripts/setup_full_disk_encryption_pi5.sh --rollback
    ```
    Restores cmdline.txt, config.txt, fstab, crypttab, and
    initramfs-tools/modules from the timestamped backup directory.
    Refuses to run after Phase 2 succeeds (because `/` is now on
    `/dev/mapper/*` and rolling back would unboot the system).

    **Rolling back AFTER Phase 2** requires a different procedure:
    boot from a second SD card / USB, mount the LUKS volume,
    `dd` it back to a fresh partition, repair the bootloader. There
    is no `--rollback` for this case — the script intentionally
    refuses rather than pretending it can.

    Status at any time:
    ```bash
    sudo bash scripts/setup_full_disk_encryption_pi5.sh --status
    ```
    Prints the recorded phase, whether the root device is LUKS,
    whether the mapper is open, the current cmdline.txt, the
    crypttab/fstab rows, and whether the initramfs contains
    `sbin/cryptsetup`. Anything in YELLOW is a misconfiguration that
    will manifest as a boot failure.

    #### Credential quick reference

    | Prompt / where | Which credential | Set by |
    |----------------|-----------------|--------|
    | `sudo somniguard-start` — `Enter SOMNI-Guard passphrase:` | **Vault passphrase** | `setup_file_encryption_pi5.sh` setup |
    | `Please unlock disk cryptroot:` (initramfs, before login) | **FDE boot passphrase** | `setup_full_disk_encryption_pi5.sh` Phase 2 |
    | `username:` / `Password:` at Linux login | **Linux user password** | `passwd` / Pi-OS imager |
    | `https://10.42.0.1:5443/` dashboard | **Dashboard admin password** | First-run admin bootstrap (§3.5) |
    | TOTP code on `/mfa/verify` | **Authenticator app code** | First-run MFA enrolment (§3.6) |

    Every credential is intentionally independent. Reusing one for
    another collapses the whole layered model to a single point of failure.
18. **UEFI Secure Boot on the Pi 5 (optional, advanced).**
    `scripts/setup_secure_boot_pi5.sh` installs the
    [`worproject/rpi5-uefi`](https://github.com/worproject/rpi5-uefi)
    firmware (v0.3 — the project is archived, so v0.3 is the terminal
    release) and enrols a SOMNI-Guard-owned PK/KEK/db hierarchy so only
    your signed kernel + bootloader will boot. Run it in stages:
    ```bash
    sudo bash scripts/setup_secure_boot_pi5.sh --verify-only   # status check
    sudo bash scripts/setup_secure_boot_pi5.sh --dry-run       # safe preview
    sudo bash scripts/setup_secure_boot_pi5.sh                 # real run
    ```
    The script bails out with a clear error if `/sys/firmware/efi/efivars`
    is missing — that means the kernel was NOT booted via UEFI yet
    (still on the stock Pi bootloader). Install the firmware first,
    reboot, re-run. **Move `/etc/somniguard/secure-boot/keys/PK.key`
    off the device after enrolment** — the script will print this
    reminder; an attacker with the PK private key can sign anything.
19. **Pico-side USB lockdown.** `somniguard_pico/boot.py` (see §4.6)
    blocks the USB-CDC REPL and remounts the filesystem read-only on
    every boot once the lockdown flag is set. Soft bypass via a
    `maintenance.flag` file; hard recovery via the RP2350 ROM bootloader
    (physical BOOTSEL + power cycle).

The `docs/pha.md` and `docs/attack_tree.md` files are the preliminary
hazard analysis and STRIDE-style attack tree underlying these choices —
read them before tuning thresholds or relaxing any layer.

---

## 16. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Browser cert warning | CA not trusted | Visit `https://10.42.0.1:5443/ca.crt`, install in trust store |
| `MFA secret cannot be decrypted` | `SOMNI_SECRET_KEY` rotated without re-enrolling | Admin → Reset MFA for affected users, or set `SOMNI_MFA_KEY` to a stable value |
| TOTP code rejected | Phone clock drift | The gateway accepts ±1 step (30 s); sync the phone's time |
| Cannot log in even after correct password | `is_active = 0` | Ask another admin to enable the account |
| `403 HMAC verification failed` from Pico | `SOMNI_HMAC_KEY` ≠ `GATEWAY_HMAC_KEY` | Compare the gateway's `[SOMNI][CONFIG] … sha256[:8]=…` line with the Pico's `[SOMNI][HMAC] … sha256[:8]=…` line — if they differ, run `sudo bash scripts/sync_gateway_env.sh` on the Pi 5 to copy the Pico key into `/etc/somniguard/env` and restart the service |
| Old log says `SOMNI_HMAC_KEY is not set — using well-known default key` | Stale gateway code still has a default-key fallback | Pull the latest repo (`git pull`) — the current `config.py` calls `_required()` and refuses to start without the key, then re-run `sync_gateway_env.sh` |
| Pico log spams `[PEM-bytes] … invalid cert` / `invalid key` before connecting | RP2350 mbedTLS PEM parser quirk; older `transport.py` tried PEM first | Update `somniguard_pico/transport.py` from the repo — it tries DER first now, so a healthy boot prints one `Connected (DER, …)` line plus the negotiated cipher (`→ TLSv1.3 / TLS_AES_128_GCM_SHA256`). The PEM warnings only fire if DER itself fails. |
| Pico TLS line says `<cipher info unavailable on this MicroPython build>` | mbedtls binding doesn't expose `sock.cipher()` | Cosmetic only — the handshake still succeeded. The gateway-side log lists the offered TLS 1.2 suites, and TLS 1.3 always negotiates one of `TLS_AES_*_GCM_SHA*` / `TLS_CHACHA20_POLY1305_SHA256`. |
| Gateway log shows `event_type: REPLAY_DETECTED` with `age_s` ≈ 946684800 | Pico's `time.time()` is in Unix epoch but `_get_timestamp_s` was adding 30 years on top | Pull latest `somniguard_pico/transport.py` — it auto-detects whether `time.time()` is Unix or 2000-epoch and stops double-adding. Re-flash the Pico. |
| Pico log: `could not fingerprint key: 'sha256' object has no attribute 'hexdigest'` | MicroPython `hashlib.sha256` exposes only `.digest()`, not `.hexdigest()` | Pull latest `somniguard_pico/main.py` — fingerprint computation now uses `.digest()` + manual hex encode. |
| `replay detected: stale nonce` | Pico restarted without resetting nonce HWM | End-session at gateway or restart the gateway to clear the in-memory HWM |
| Gateway log: `WARNING: /etc/somniguard/env is group/world-accessible (mode 640)` | Env file is readable by the `somniguard` group, not just root | Cosmetic on a single-user Pi 5 (the systemd unit runs as the `somniguard` user, which needs read access). To silence, run `sudo chmod 600 /etc/somniguard/env` AND adjust the unit to run as root, OR ignore — group-only access (mode 640) is the default `sync_gateway_env.sh` writes for a reason. |
| Gateway log: `[SSL: SSLV3_ALERT_CERTIFICATE_UNKNOWN]` from a browser IP | The browser doesn't trust the SOMNI-Guard CA yet | Cosmetic — the dashboard still loaded (the next line shows `200`). Install `https://10.42.0.1:5443/ca.crt` in your OS trust store to silence it. Not a Pico issue. |
| `setup_secure_boot_pi5.sh` fails downloading UEFI firmware with 404 | Older script revisions pointed at the non-existent `pftf/RPi5` repo | Pull the latest repo — the URL now correctly points at `worproject/rpi5-uefi` v0.3. Override with `SOMNI_UEFI_VERSION=…` if a community fork resumes maintenance. |
| `setup_secure_boot_pi5.sh` aborts with "/sys/firmware/efi/efivars is not present" | Pi 5 booted via the stock Raspberry Pi bootloader, not via UEFI | Expected on first install — reboot through the UEFI firmware first (press ESC at the rainbow splash), then re-run the script. Existing keys/signing/EEPROM steps are skipped on the second run. |
| Pico boot prints `BOOTSEL held — maintenance mode` on every cold boot even when BOOTSEL isn't pressed | micropython#16908 — `rp2.bootsel_button()` always returns 1 on RP2350/Pico 2 W | Pull the latest `boot.py` — it now uses a `maintenance.flag` file instead of polling the broken button. Lockdown will start applying again on the next boot. |
| Locked Pico is bricked / can't reach REPL | Lockdown is doing exactly what it's supposed to | Hold the physical **BOOTSEL** button while plugging USB → ROM mass-storage drive appears → drag a fresh MicroPython UF2 onto it to wipe and reflash. Then re-run `setup_pico.sh`. |
| `somniguard-start` says "Wrong passphrase or corrupted vault" | Wrong passphrase typed, OR vault files corrupted | Retype carefully. If genuinely forgotten, restore from the vault backup you made after setup (`sudo tar xzf somniguard-vault-backup.tar.gz -C /`) and run `--rotate-key` with the known old passphrase. |
| `somniguard-start` says "Secrets tmpfs already mounted" | A previous `somniguard-start` session is still active | Check `sudo systemctl status somniguard-gateway`. If the gateway is stopped but the tmpfs is still mounted (e.g., after a crash), run `sudo umount /run/somniguard-secrets/certs; sudo umount /run/somniguard-secrets` then retry. |
| `somniguard-start` says "Vault not found" | `setup_file_encryption_pi5.sh` not run yet, or vault was deleted | Run `sudo bash scripts/setup_file_encryption_pi5.sh` to set up the vault. |
| Gateway starts but `[SOMNI][CONFIG]` shows "SOMNI_HMAC_KEY not set" | The `somniguard-start` certs bind-mount worked but the env file in the vault is missing `SOMNI_HMAC_KEY` | Run `sudo bash scripts/setup_file_encryption_pi5.sh --remove` to restore plaintext env, add the key with `sudo bash scripts/sync_gateway_env.sh`, then re-run setup. |
| After running `setup_file_encryption_pi5.sh`, the gateway starts by itself on reboot without asking for a passphrase | Autostart was re-enabled manually or by another script | Run `sudo systemctl disable somniguard-gateway`. With the drop-in in place the service would fail anyway (no tmpfs env), but disabling prevents the failed-start noise in logs. |
| `somniguard-start` times out waiting for the gateway to start | ExecStartPre cert-regen failed (can't read CA key from tmpfs certs) | Check `sudo journalctl -u somniguard-gateway -n 50`. Most often the bind-mount over `/etc/somniguard/certs` raced with systemd — re-run `somniguard-start`. |
| `setup_full_disk_encryption_pi5.sh --check` aborts with "Kernel page size is 16K" | You are on the 16 KB-page Trixie kernel which breaks LUKS device-mapper bring-up ([trixie-feedback#5](https://github.com/raspberrypi/trixie-feedback/issues/5)) | Switch to the 4 KB-page kernel: edit `/boot/firmware/config.txt`, add `kernel=kernel8.img` (and remove any `kernel=kernel_2712.img`), reboot, re-run `--check`. |
| Phase 1 finished but the next reboot drops you straight back to the (initramfs) prompt instead of asking for a passphrase | `cryptsetup luksFormat` was never run — Phase 2 is incomplete | Read `/boot/firmware/SOMNI_FDE_PHASE2.txt` from another machine (mount the SD card or scp it before the reboot) and run the commands in order. Don't `exit` the initramfs until the cheatsheet's last step. |
| Boot prompt says `cryptsetup: ERROR: cryptroot: cryptsetup failed, bad password or options` even though the passphrase is correct | initramfs was rebuilt without crypto modules (often by a kernel upgrade that bypassed our `/etc/kernel/postinst.d/zz-somniguard-fde` hook) | Boot a rescue image, chroot, run `CRYPTSETUP=y mkinitramfs -o /boot/firmware/initramfs.gz $(uname -r)`, verify with `lsinitramfs /boot/firmware/initramfs.gz \| grep -E 'sbin/cryptsetup\|aes_arm64'`, reboot. |
| After full-disk encryption, the Pi boots fine but `somniguard-gateway.service` is in `activating (auto-restart)` for several minutes | `Restart=always` plus an exception in early startup (e.g., `/etc/somniguard/env` not readable yet) keeps the unit in a restart loop within `StartLimitBurst`. The unit will eventually settle once the env file is readable. | `sudo systemctl status somniguard-gateway --no-pager` will show the actual exception. Most often the fix is `sudo bash scripts/sync_gateway_env.sh` followed by `sudo systemctl reset-failed somniguard-gateway && sudo systemctl restart somniguard-gateway`. |
| `systemctl is-enabled somniguard-gateway` reports `enabled` but the gateway never starts on reboot | The autostart symlink in `/etc/systemd/system/multi-user.target.wants/` was deleted (or never created — common after a manual `systemctl edit` that broke the `[Install]` section) | `sudo bash scripts/fix_autostart.sh`. The script now uses `systemctl reenable` (not just `enable`), runs `unmask` and `reset-failed` defensively, and verifies the symlink target points at `/etc/systemd/system/somniguard-gateway.service` rather than just existing. |
| Gateway service fails immediately with "Failed to load environment files" | The vault drop-in (`somni-vault.conf`) points at `/run/somniguard-secrets/env` but `somniguard-start` was not used | Only start the gateway with `sudo somniguard-start` — do not use `systemctl start somniguard-gateway` directly when the vault is enabled. |
| `pyotp` / `qrcode` import error | Old `requirements.txt` | `pip install -r somniguard_gateway/requirements.txt` |
| Dashboard immediately redirects to `/mfa/setup` | Account has no enrolled authenticator | Complete enrolment; this is expected — MFA is mandatory |
| Live monitor shows "Update failed" | Logged-out (cookie expired) | Refresh the page and re-authenticate |
| `/admin/audit.csv` returns nothing | Filters too narrow | Reset filters and try again |

For verbose logs:

```bash
sudo journalctl -u somniguard-gateway -f
tail -f /var/lib/somniguard/audit.log | jq .
```

---

## 17. Project layout

```
NightWatchGaurd/
├── GUIDE.md                       ← this file (the single source of truth)
├── README.md                      ← brief project landing → points here
├── SETUP.md                       ← one-line pointer → points here
├── somniguard_gateway/
│   ├── app.py                     ← Flask app, MFA gates, all web routes,
│   │                                clinical alerts, live monitor JSON
│   ├── audit.py                   ← rotating JSON + console + DB audit log
│   ├── config.py                  ← env-driven configuration
│   ├── database.py                ← schema + helpers (incl. live migrations)
│   ├── hotspot.py                 ← NetworkManager wifi hotspot bring-up
│   ├── mfa.py                     ← TOTP + backup codes + Fernet wrap
│   ├── reports.py                 ← compute_summary + ReportLab PDF
│   ├── run.py                     ← entry point (gunicorn / Flask dev)
│   ├── security.py                ← rate limit, headers, lockout, sanitize
│   ├── tailscale.py               ← network-policy helper
│   ├── tls_setup.py               ← cert auto-regeneration
│   ├── requirements.txt           ← includes pyotp + qrcode[pil]
│   ├── static/js/live.js          ← CSP-compliant live monitor poller
│   ├── templates/                 ← Jinja templates (16 files)
│   └── certs/                     ← gateway CA + server cert
├── somniguard_pico/               ← MicroPython firmware
│   ├── boot.py / main.py
│   ├── transport.py               ← TLS + HMAC client
│   ├── sampler.py                 ← MAX30102 / MPU6050 / GSR sampling
│   └── …
├── scripts/
│   ├── setup_gateway_pi5.sh       ← installer
│   ├── setup_gateway_certs.py     ← cert regeneration
│   ├── embed_pico_cert.py         ← push CA + Pico client cert into firmware
│   ├── embed_pico_config.py       ← push HMAC key + host/port into Pico
│   ├── seed_db.py                 ← non-interactive admin bootstrap
│   ├── harden_pi5.sh              ← OS hardening
│   ├── setup_file_encryption_pi5.sh        ← AES file-level encryption of gateway secrets
│   ├── setup_full_disk_encryption_pi5.sh   ← LUKS2 root partition (TRUE FDE, boot prompt)
│   ├── setup_secure_boot_pi5.sh            ← UEFI Secure Boot enrolment
│   ├── fix_autostart.sh                    ← repair somniguard-gateway autostart
│   └── …
├── pico_tests/                    ← MicroPython unit tests
└── docs/
    ├── attack_tree.md             ← appendix — STRIDE attack tree
    ├── pha.md                     ← appendix — preliminary hazard analysis
    ├── assets.md                  ← appendix — bill of materials
    └── MASTER_GUIDE.md            ← legacy doc → see this file instead
```
