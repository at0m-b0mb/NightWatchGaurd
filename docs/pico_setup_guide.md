# SOMNI-Guard Raspberry Pi Pico 2W — Complete Setup, Encryption & USB Lockdown Guide

> **Educational prototype — not a clinically approved device.**

This guide walks you through every step from an out-of-the-box Pico 2W to a fully
secured, encrypted, USB-locked SOMNI-Guard sensor node.

---

## Table of Contents

1. [What You Need](#1-what-you-need)
2. [Flash MicroPython Firmware](#2-flash-micropython-firmware)
3. [Read Your Pico's Hardware Unique ID](#3-read-your-picos-hardware-unique-id)
4. [Configure Credentials](#4-configure-credentials)
5. [Deploy Plaintext (Development / First Test)](#5-deploy-plaintext-development--first-test)
6. [Encrypt All Firmware Files](#6-encrypt-all-firmware-files)
7. [Deploy Encrypted Firmware to the Pico](#7-deploy-encrypted-firmware-to-the-pico)
8. [Verify Encrypted Boot](#8-verify-encrypted-boot)
9. [USB Lockdown (Disable USB Drive Access)](#9-usb-lockdown-disable-usb-drive-access)
10. [Raspberry Pi 5 Gateway Setup](#10-raspberry-pi-5-gateway-setup)
11. [Connecting Pico to Gateway](#11-connecting-pico-to-gateway)
12. [Security Reference](#12-security-reference)

---

## 1. What You Need

| Item | Notes |
|------|-------|
| Raspberry Pi Pico 2W | RP2350 chip, Wi-Fi built in |
| MAX30102 SpO₂/HR sensor | I2C address 0x57 |
| ADXL345 accelerometer | I2C address 0x53 (SDO → GND) |
| Grove GSR v1.2 sensor | Optional; connects to GP26 (ADC0) |
| Computer with Python 3.10+ | macOS, Linux, or Windows |
| Micro-USB cable | Data cable (not charge-only) |
| `mpremote` tool | `pip install mpremote` |
| `cryptography` library | `pip install cryptography` |

### Wiring

```
MAX30102                       Pico 2W
  VCC  ──────────────────────  3.3V  (pin 36)
  GND  ──────────────────────  GND   (pin 38)
  SDA  ──────────────────────  GP4   (pin 6)   [I2C0 SDA]
  SCL  ──────────────────────  GP5   (pin 7)   [I2C0 SCL]

ADXL345                        Pico 2W
  VCC  ──────────────────────  3.3V  (pin 36)
  GND  ──────────────────────  GND   (pin 38)
  SDA  ──────────────────────  GP2   (pin 4)   [I2C1 SDA]
  SCL  ──────────────────────  GP3   (pin 5)   [I2C1 SCL]
  SDO  ──────────────────────  GND   (sets address to 0x53)

Grove GSR v1.2 (optional)      Pico 2W
  VCC  (Red)    ─────────────  3.3V  (pin 36)
  GND  (Black)  ─────────────  GND   (pin 38)
  SIG  (Yellow) ─────────────  GP26  (pin 31) [ADC0]
```

---

## 2. Flash MicroPython Firmware

The Pico 2W must run **MicroPython for RP2350** (version ≥ 1.23 recommended).

### Step 2.1 — Download firmware

Go to [micropython.org/download/RPI_PICO2_W](https://micropython.org/download/RPI_PICO2_W/)
and download the latest `.uf2` file.

### Step 2.2 — Enter bootloader mode

1. Hold the **BOOTSEL** button on the Pico 2W.
2. While holding BOOTSEL, connect the Micro-USB cable to your computer.
3. Release BOOTSEL. A drive called **`RPI-RP2`** (or `RP2350`) will appear.

### Step 2.3 — Flash firmware

Drag and drop the `.uf2` file onto the `RPI-RP2` drive.
The Pico will reboot automatically and disappear as a drive.
A new serial port will appear (e.g. `/dev/ttyACM0` on Linux, `COM3` on Windows).

### Step 2.4 — Verify firmware

```bash
mpremote connect /dev/ttyACM0 repl
```

You should see the MicroPython REPL (`>>>` prompt).
Press `Ctrl-D` to soft-reset and confirm the firmware banner shows `RP2350`.

---

## 3. Read Your Pico's Hardware Unique ID

The encryption key for all firmware files is derived from your Pico's
factory-programmed 8-byte hardware unique ID (`machine.unique_id()`).
This means encrypted `.enc` files **only work on the specific Pico they were
encrypted for** — copying them to a different Pico will fail decryption.

### Step 3.1 — Connect to REPL and read UID

```bash
mpremote connect /dev/ttyACM0 repl
```

At the `>>>` prompt:

```python
import machine
uid = machine.unique_id()
print(uid.hex())
```

Example output: `e660c0d1c7921e28`

### Step 3.2 — Save the UID

Write it down and keep it safe. You will need it every time you re-encrypt
the firmware (e.g. after a code change). Store it in a secure location such
as a password manager — **losing the UID means you cannot re-encrypt for this device**.

Press `Ctrl-X` to exit the REPL.

---

## 4. Configure Credentials

Before encrypting, edit the Pico configuration with your real Wi-Fi and gateway settings.

### Step 4.1 — Edit `somniguard_pico/config.py`

```python
# Wi-Fi credentials
WIFI_SSID     = "YourNetworkName"
WIFI_PASSWORD = "YourWifiPassword"

# Pi 5 gateway address
GATEWAY_HOST = "192.168.1.100"    # Change to your Pi 5's IP address
GATEWAY_PORT = 5000

# Patient ID (create the patient in the web dashboard first)
GATEWAY_PATIENT_ID = 1

# Device identifier
DEVICE_ID = "pico-01"

# Shared HMAC key — MUST match SOMNI_HMAC_KEY on the Pi 5 gateway.
# Generate a strong key:
#   python3 -c "import secrets; print(secrets.token_hex(32))"
GATEWAY_HMAC_KEY = "your-64-char-hex-secret-key-here"
```

> **Security note:** After encryption, `config.py` will be stored as
> `config.enc` on the Pico. The plaintext file is only used during the
> encryption step and should not be left on the Pico.

### Step 4.2 — Generate a strong HMAC key

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output into both:
- `somniguard_pico/config.py` → `GATEWAY_HMAC_KEY`
- The Pi 5 gateway as the `SOMNI_HMAC_KEY` environment variable

---

## 5. Deploy Plaintext (Development / First Test)

Before encrypting, test that everything works with plain `.py` files.
This lets you catch wiring or configuration mistakes before dealing with encryption.

### Step 5.1 — Copy all Pico files

```bash
cd /path/to/NightWatchGaurd-main

# Copy all source files to the Pico
mpremote connect /dev/ttyACM0 cp -r somniguard_pico/. :
```

Or file by file:

```bash
mpremote connect /dev/ttyACM0 cp somniguard_pico/main.py :main.py
mpremote connect /dev/ttyACM0 cp somniguard_pico/config.py :config.py
mpremote connect /dev/ttyACM0 cp somniguard_pico/crypto_loader.py :crypto_loader.py
mpremote connect /dev/ttyACM0 cp somniguard_pico/boot.py :boot.py
mpremote connect /dev/ttyACM0 cp somniguard_pico/transport.py :transport.py
mpremote connect /dev/ttyACM0 cp somniguard_pico/sampler.py :sampler.py
mpremote connect /dev/ttyACM0 cp somniguard_pico/utils.py :utils.py
mpremote connect /dev/ttyACM0 cp somniguard_pico/integrity.py :integrity.py
mpremote connect /dev/ttyACM0 cp somniguard_pico/secure_config.py :secure_config.py
mpremote connect /dev/ttyACM0 mkdir :drivers
mpremote connect /dev/ttyACM0 cp somniguard_pico/drivers/__init__.py :drivers/__init__.py
mpremote connect /dev/ttyACM0 cp somniguard_pico/drivers/max30102.py :drivers/max30102.py
mpremote connect /dev/ttyACM0 cp somniguard_pico/drivers/adxl345.py :drivers/adxl345.py
mpremote connect /dev/ttyACM0 cp somniguard_pico/drivers/gsr.py :drivers/gsr.py
```

### Step 5.2 — Monitor serial output

```bash
mpremote connect /dev/ttyACM0 repl
```

Press `Ctrl-D` to soft-reset. You should see:

```
[SOMNI][BOOT] ========================================
[SOMNI][BOOT] SOMNI-Guard v0.4 — Secure Boot
[SOMNI][BOOT] Setup mode: USB access enabled.
[SOMNI] ================================================
[SOMNI] SOMNI-Guard v0.4 — Educational Sleep Monitor
[SOMNI] NOT a clinically approved device.
[SOMNI] Hardware watchdog enabled (timeout=8000ms).
[SOMNI] MAX30102 I2C bus initialised (SDA=GP4, SCL=GP5, 400000Hz).
[SOMNI] ADXL345 I2C bus initialised (SDA=GP2, SCL=GP3, 400000Hz).
[SOMNI][MAX30102] Sensor configured (SpO₂ mode, LED=25.4mA, 100sps, 18-bit).
[SOMNI][ADXL345] Sensor configured (±2g, 50 Hz ODR, measurement mode).
[SOMNI][SAMPLER] Sampling loop started (accel@10Hz, SpO2@1Hz/GSR).
```

Fix any sensor errors before proceeding to encryption.

---

## 6. Encrypt All Firmware Files

Once testing is complete, encrypt the firmware so that the source code and
credentials are protected on the Pico's flash.

### Step 6.1 — Install the encryption tool dependencies

```bash
pip install cryptography
```

### Step 6.2 — Run the encryption tool

```bash
cd /path/to/NightWatchGaurd-main

python scripts/encrypt_pico_files.py \
    --uid YOUR_PICO_UID_HERE \
    --src somniguard_pico/ \
    --out encrypted_deploy/
```

Replace `YOUR_PICO_UID_HERE` with the hex UID from Step 3 (e.g. `e660c0d1c7921e28`).

Example output:

```
[SOMNI][ENCRYPT] SOMNI-Guard Firmware Encryption Tool
[SOMNI][ENCRYPT] Source dir : somniguard_pico
[SOMNI][ENCRYPT] Output dir : encrypted_deploy
[SOMNI][ENCRYPT] UID        : E660C0D1C7921E28 (8 bytes)
[SOMNI][ENCRYPT] AES lib    : cryptography
[SOMNI][ENCRYPT] Generated new random salt (16 bytes).
[SOMNI][ENCRYPT] AES-256 key derived (SHA-256 of UID + salt).
[SOMNI][ENCRYPT] ENC  config.py → config.enc (3421 → 3440 bytes)
[SOMNI][ENCRYPT] ENC  utils.py  → utils.enc  (...)
...
[SOMNI][ENCRYPT] COPY main.py (plaintext bootstrap)
[SOMNI][ENCRYPT] COPY crypto_loader.py (plaintext bootstrap)
[SOMNI][ENCRYPT] COPY boot.py (plaintext bootstrap)
[SOMNI][ENCRYPT] Encrypted   : 11 files
[SOMNI][ENCRYPT] Plaintext   : 3 files
[SOMNI][ENCRYPT] Errors      : 0 files
```

The `encrypted_deploy/` directory now contains:
- `main.py` — plaintext (no secrets)
- `crypto_loader.py` — plaintext (no secrets)
- `boot.py` — plaintext (no secrets)
- `_salt.bin` — random salt (useless without the hardware UID)
- `config.enc`, `transport.enc`, `sampler.enc`, etc. — AES-256-CBC encrypted

### Step 6.3 — Keep the salt safe

The `_salt.bin` file is part of the key derivation. Back it up:

```bash
cp encrypted_deploy/_salt.bin ~/somni_salt_backup.bin
```

> Store it securely. If you lose the salt, you cannot re-derive the same key
> and must re-encrypt everything with a new salt.

---

## 7. Deploy Encrypted Firmware to the Pico

### Step 7.1 — Wipe the Pico filesystem

Remove the plaintext `.py` source files that were deployed during testing:

```bash
# Connect to REPL and delete plaintext files
mpremote connect /dev/ttyACM0 exec "
import os
for f in os.listdir('/'):
    if f.endswith('.py') and f not in ('main.py', 'crypto_loader.py', 'boot.py'):
        try:
            os.remove(f)
            print('Removed:', f)
        except:
            pass

# Remove drivers directory
try:
    for f in os.listdir('/drivers'):
        if f.endswith('.py') and f != '__init__.py':
            os.remove('/drivers/' + f)
            print('Removed: drivers/' + f)
except:
    pass
"
```

### Step 7.2 — Copy encrypted files to the Pico

```bash
# Copy all encrypted files
mpremote connect /dev/ttyACM0 cp encrypted_deploy/main.py :main.py
mpremote connect /dev/ttyACM0 cp encrypted_deploy/crypto_loader.py :crypto_loader.py
mpremote connect /dev/ttyACM0 cp encrypted_deploy/boot.py :boot.py
mpremote connect /dev/ttyACM0 cp encrypted_deploy/_salt.bin :_salt.bin
mpremote connect /dev/ttyACM0 cp encrypted_deploy/config.enc :config.enc
mpremote connect /dev/ttyACM0 cp encrypted_deploy/transport.enc :transport.enc
mpremote connect /dev/ttyACM0 cp encrypted_deploy/sampler.enc :sampler.enc
mpremote connect /dev/ttyACM0 cp encrypted_deploy/utils.enc :utils.enc
mpremote connect /dev/ttyACM0 cp encrypted_deploy/integrity.enc :integrity.enc
mpremote connect /dev/ttyACM0 cp encrypted_deploy/secure_config.enc :secure_config.enc
mpremote connect /dev/ttyACM0 mkdir :drivers
mpremote connect /dev/ttyACM0 cp encrypted_deploy/drivers/__init__.enc :drivers/__init__.enc
mpremote connect /dev/ttyACM0 cp encrypted_deploy/drivers/max30102.enc :drivers/max30102.enc
mpremote connect /dev/ttyACM0 cp encrypted_deploy/drivers/adxl345.enc :drivers/adxl345.enc
mpremote connect /dev/ttyACM0 cp encrypted_deploy/drivers/gsr.enc :drivers/gsr.enc
```

Or use the bulk recursive copy:

```bash
mpremote connect /dev/ttyACM0 cp -r encrypted_deploy/. :
```

### Step 7.3 — Verify the filesystem

```bash
mpremote connect /dev/ttyACM0 exec "
import os
print('Root:', os.listdir('/'))
print('drivers:', os.listdir('/drivers'))
"
```

You should see `.enc` files and no plaintext `.py` source files
(only `main.py`, `crypto_loader.py`, and `boot.py` remain as plain text).

---

## 8. Verify Encrypted Boot

Reset the Pico and monitor the output:

```bash
mpremote connect /dev/ttyACM0 repl
```

Press `Ctrl-D`. Expected output:

```
[SOMNI][BOOT] ========================================
[SOMNI][BOOT] SOMNI-Guard v0.4 — Secure Boot
[SOMNI][BOOT] Setup mode: USB access enabled.
[SOMNI] Encrypted firmware loader available.
[SOMNI][CRYPTO] ========================================
[SOMNI][CRYPTO] Loading encrypted firmware modules...
[SOMNI][CRYPTO] AES engine: ucryptolib
[SOMNI][CRYPTO] Decrypting 'config.enc'...
[SOMNI][CRYPTO] Decrypted 'config.enc' OK (3421 bytes source).
...
[SOMNI] SOMNI-Guard v0.4 — Educational Sleep Monitor
[SOMNI] MAX30102 I2C bus initialised (SDA=GP4, SCL=GP5, 400000Hz).
[SOMNI][WIFI] Connecting to 'YourNetworkName'…
[SOMNI][WIFI] Connected. IP: 192.168.1.x
[SOMNI] Gateway session started: ID 1.
[SOMNI] Sampling active.
```

If you see `[SOMNI][CRYPTO] Decryption error`, double-check:
- The UID used in Step 6.2 matches the actual device UID from Step 3.
- The `_salt.bin` on the Pico matches the one in `encrypted_deploy/`.

---

## 9. USB Lockdown (Disable USB Drive Access)

After confirming the encrypted firmware boots and connects to the gateway
successfully, you can prevent anyone with physical access from browsing or
modifying the Pico's filesystem via USB.

### How it works

The `boot.py` file (which runs before `main.py`) checks for a file called
`usb_locked.flag`. When present, it attempts to:

1. Call `storage.disable_usb_drive()` (CircuitPython API — removes the drive entirely)
2. Or remount the filesystem as **read-only** (MicroPython fallback — drive is visible
   but no file modifications are possible)

Either way, the encrypted `.enc` files cannot be replaced or tampered with.

> **Note on stock MicroPython:** The `storage.disable_usb_drive()` API is a
> CircuitPython feature. With stock MicroPython firmware, only the read-only
> remount is achieved. For complete drive removal, flash **CircuitPython**
> instead of MicroPython — see [circuitpython.org/board/raspberry_pi_pico2_w](https://circuitpython.org/board/raspberry_pi_pico2_w/).
> CircuitPython is fully compatible with this project's Python code.

### Step 9.1 — Activate USB lockdown

Connect to the REPL one final time:

```bash
mpremote connect /dev/ttyACM0 repl
```

Run:

```python
from boot import lock_usb
lock_usb()
```

Output:

```
[SOMNI][BOOT] USB lockdown flag created.
[SOMNI][BOOT] IMPORTANT: Reboot to apply USB lockdown.
[SOMNI][BOOT]            Hold BOOTSEL during reset to bypass.
```

### Step 9.2 — Reboot and verify

Disconnect and reconnect the USB cable (or press the reset button).
The Pico should **not** appear as a USB drive this time.

If it still appears, your MicroPython build does not include the `storage`
module and the read-only remount may not have applied. See the note above.

### Step 9.3 — Bypass for maintenance

To re-access the Pico filesystem for firmware updates:

1. **Hold the BOOTSEL button**.
2. While holding BOOTSEL, **connect USB**.
3. The RP2350 enters its ROM bootloader — `boot.py` is not executed.
4. The Pico appears as `RPI-RP2` drive.
5. You can now reflash or delete `usb_locked.flag`.

To remove the lockdown flag without reflashing:
1. Flash MicroPython again (which wipes the filesystem).
2. Redeploy your encrypted firmware.
3. Do not call `lock_usb()` this time until ready.

---

## 10. Raspberry Pi 5 Gateway Setup

### Step 10.1 — Operating System

Flash Raspberry Pi OS (64-bit, Bookworm) to your Pi 5's SD card.
Enable SSH during imaging.

### Step 10.2 — System dependencies

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
```

### Step 10.3 — Clone and install

```bash
git clone https://github.com/youruser/NightWatchGaurd.git
cd NightWatchGaurd/somniguard_gateway
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Step 10.4 — Configure environment

Create `/etc/somniguard/env`:

```bash
sudo mkdir -p /etc/somniguard
sudo tee /etc/somniguard/env <<'EOF'
# Generate a new secret key: python3 -c "import secrets; print(secrets.token_hex(32))"
SOMNI_SECRET_KEY=your-32-byte-hex-flask-secret
SOMNI_CSRF_KEY=your-32-byte-hex-csrf-secret

# Must match GATEWAY_HMAC_KEY in the Pico config
SOMNI_HMAC_KEY=your-64-char-hex-hmac-key

# Absolute path to SQLite database
SOMNI_DB_PATH=/var/lib/somniguard/somni.db

# Report output directory
SOMNI_REPORT_DIR=/var/lib/somniguard/reports

# Set to true to require Tailscale VPN for web dashboard access
SOMNI_TAILSCALE_ONLY=false

# Set to true to enable HTTPS (self-signed cert)
SOMNI_HTTPS=true
EOF
sudo chmod 600 /etc/somniguard/env
```

### Step 10.5 — Create data directories

```bash
sudo mkdir -p /var/lib/somniguard/reports
sudo chown -R $USER:$USER /var/lib/somniguard
```

### Step 10.6 — First run (creates admin account)

```bash
cd NightWatchGaurd/somniguard_gateway
source venv/bin/activate
set -a; source /etc/somniguard/env; set +a
python run.py
```

On first run you will be prompted to create an admin account.
The password must contain: uppercase, lowercase, digit, and special character.

### Step 10.7 — Run as a systemd service

```bash
sudo tee /etc/systemd/system/somniguard.service <<'EOF'
[Unit]
Description=SOMNI-Guard Gateway
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/NightWatchGaurd/somniguard_gateway
EnvironmentFile=/etc/somniguard/env
ExecStart=/home/pi/NightWatchGaurd/somniguard_gateway/venv/bin/python run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable somniguard
sudo systemctl start somniguard
sudo systemctl status somniguard
```

---

## 11. Connecting Pico to Gateway

### Step 11.1 — Find the Pi 5's IP address

```bash
hostname -I
```

### Step 11.2 — Update Pico config

Edit `somniguard_pico/config.py`:

```python
GATEWAY_HOST = "192.168.1.100"   # Your Pi 5's IP
GATEWAY_PORT = 5000
```

Re-encrypt and redeploy (Section 6–7) after any config change.

### Step 11.3 — Create a patient in the dashboard

1. Open `https://192.168.1.100:5000` in your browser.
2. Log in with the admin account.
3. Go to **Patients → New Patient**.
4. Note the patient ID (usually `1` on a fresh install).
5. Set `GATEWAY_PATIENT_ID = 1` in `config.py`.

### Step 11.4 — Start monitoring

Power on the Pico 2W. The LED will blink once per second when data is flowing.
Check the dashboard — a new session should appear under the patient's profile.

---

## 12. Security Reference

| Layer | Mechanism | Protection |
|-------|-----------|------------|
| Firmware at rest | AES-256-CBC `.enc` files (key = SHA-256(UID + salt)) | Source code & credentials unreadable without the specific Pico chip |
| USB file access | `boot.py` lockdown + read-only remount | Adversary cannot replace or read files via USB cable |
| Wire transmission | HMAC-SHA256 signed JSON, anti-replay nonce + timestamp | Prevents message forgery, replay, and tampering in transit |
| Gateway login | bcrypt (rounds=12), account lockout (10 fails → 15 min) | Brute-force and credential-stuffing resistance |
| Gateway sessions | HTTP-only, SameSite=Lax, 30-min timeout | Session hijacking resistance |
| CSRF | Flask-WTF CSRF tokens on all web forms | Cross-site request forgery prevention |
| Security headers | HSTS, CSP, X-Frame-Options, Referrer-Policy | XSS, clickjacking, and sniffing resistance |
| Audit trail | Structured JSON rotating log + SQLite table | Forensic evidence for all access events |
| Integrity check | SHA-256 file hashes + HMAC-signed manifest | Detects firmware tampering at boot |
| Report signing | HMAC-SHA256 over summary JSON | Detects post-generation report tampering |

### Changing credentials without reflashing

To update the Wi-Fi password or HMAC key after deployment:

1. Hold BOOTSEL → plug USB → Pico enters bootloader.
2. Edit `config.py` with new credentials.
3. Re-run `encrypt_pico_files.py` with the same `--uid` and `--salt`.
4. Copy the new `.enc` files to the Pico via mpremote.
5. Run `lock_usb()` again when done.

### Generating fresh keys

```bash
# Flask secret key
python3 -c "import secrets; print(secrets.token_hex(32))"

# HMAC key (shared between Pico and gateway)
python3 -c "import secrets; print(secrets.token_hex(32))"

# CSRF secret key
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

*SOMNI-Guard v0.4 — Educational prototype. Not a clinically approved device.*
