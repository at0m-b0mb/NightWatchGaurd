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
6. [One-Command UF2 Deployment (Recommended)](#6-one-command-uf2-deployment-recommended)
7. [Manual: Encrypt All Firmware Files](#7-manual-encrypt-all-firmware-files)
8. [Manual: Deploy Encrypted Firmware to the Pico](#8-manual-deploy-encrypted-firmware-to-the-pico)
9. [Verify Encrypted Boot](#9-verify-encrypted-boot)
10. [USB Lockdown (Disable USB Access)](#10-usb-lockdown-disable-usb-access)
11. [Custom Firmware Build (Complete USB Removal)](#11-custom-firmware-build-complete-usb-removal)
12. [Raspberry Pi 5 Gateway Setup](#12-raspberry-pi-5-gateway-setup)
13. [Connecting Pico to Gateway](#13-connecting-pico-to-gateway)
14. [Security Reference](#14-security-reference)

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

### Step 4.1 — Edit `scripts/somniguard_pico/config.py`

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
- `scripts/somniguard_pico/config.py` → `GATEWAY_HMAC_KEY`
- The Pi 5 gateway as the `SOMNI_HMAC_KEY` environment variable

---

## 5. Deploy Plaintext (Development / First Test)

Before encrypting, test that everything works with plain `.py` files.
This lets you catch wiring or configuration mistakes before dealing with encryption.

### Step 5.1 — Copy all Pico files

```bash
cd /path/to/NightWatchGaurd-main

# Copy all source files to the Pico
mpremote connect /dev/ttyACM0 cp -r scripts/somniguard_pico/. :
```

Or file by file:

```bash
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/main.py :main.py
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/config.py :config.py
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/crypto_loader.py :crypto_loader.py
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/boot.py :boot.py
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/transport.py :transport.py
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/sampler.py :sampler.py
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/utils.py :utils.py
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/integrity.py :integrity.py
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/secure_config.py :secure_config.py
mpremote connect /dev/ttyACM0 mkdir :drivers
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/drivers/__init__.py :drivers/__init__.py
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/drivers/max30102.py :drivers/max30102.py
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/drivers/adxl345.py :drivers/adxl345.py
mpremote connect /dev/ttyACM0 cp scripts/somniguard_pico/drivers/gsr.py :drivers/gsr.py
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

## 6. One-Command UF2 Deployment (Recommended)

`somni_uf2_tool.py` combines encryption, LittleFS2 image creation, and UF2
packaging into a single command. The output is a single `.uf2` file you
flash by drag-and-drop — no `mpremote` needed.

### Step 6.1 — Install dependencies

```bash
pip install cryptography littlefs-python
```

### Step 6.2 — Run the UF2 tool

You need:
- Your Pico's UID from Step 3 (e.g. `2effff680e87ca96`)
- A base MicroPython UF2 for RP2350 (downloaded in Step 2), **or** the
  custom SOMNI-Guard firmware built in [Section 11](#11-custom-firmware-build-complete-usb-removal)

```bash
cd /path/to/NightWatchGaurd-main

python scripts/somni_uf2_tool.py \
    --uid 2effff680e87ca96 \
    --src scripts/somniguard_pico/ \
    --firmware somni_guard_firmware.uf2 \
    --out somni_guard_complete.uf2
```

The tool will:
1. Derive the AES-256 key from UID + a random salt.
2. Encrypt all `.py` source files to `.enc`.
3. Build a LittleFS2 filesystem image containing all encrypted files.
4. Append the filesystem image to the base firmware UF2 as additional
   UF2 blocks targeting the correct flash address (`0x10180000`).
5. Write `somni_guard_complete.uf2`.

### Step 6.3 — Flash the UF2

1. Hold **BOOTSEL** while plugging the Pico 2W into USB.
2. A drive called `RPI-RP2` (or `RP2350`) appears.
3. Drag `somni_guard_complete.uf2` onto the drive.
4. The Pico reboots automatically with firmware + encrypted files both
   written in a single flash operation.

> **Tip:** After flashing, follow [Section 10](#10-usb-lockdown-disable-usb-access)
> to activate the USB lockdown via `boot.py`, or use the custom firmware from
> [Section 11](#11-custom-firmware-build-complete-usb-removal) for complete
> hardware-level USB removal.

---

## 7. Manual: Encrypt All Firmware Files

If you prefer to manage deployment manually (e.g. to update a single file
without reflashing), use the standalone encryption tool.

### Step 7.1 — Install the encryption tool dependency

```bash
pip install cryptography
```

### Step 7.2 — Run the encryption tool

```bash
cd /path/to/NightWatchGaurd-main

python scripts/encrypt_pico_files.py \
    --uid YOUR_PICO_UID_HERE \
    --src scripts/somniguard_pico/ \
    --out encrypted_deploy/
```

Replace `YOUR_PICO_UID_HERE` with the hex UID from Step 3 (e.g. `e660c0d1c7921e28`).

The `encrypted_deploy/` directory will contain:
- `main.py`, `crypto_loader.py`, `boot.py` — plaintext (no secrets)
- `_salt.bin` — random salt (useless without the hardware UID)
- `config.enc`, `transport.enc`, `sampler.enc`, etc. — AES-256-CBC encrypted
- `drivers/max30102.enc`, `drivers/adxl345.enc`, `drivers/gsr.enc`, `drivers/__init__.enc`

### Step 7.3 — Keep the salt safe

```bash
cp encrypted_deploy/_salt.bin ~/somni_salt_backup.bin
```

> If you lose `_salt.bin`, you cannot re-derive the same key. Store it in a
> password manager or secure location.

---

## 8. Manual: Deploy Encrypted Firmware to the Pico

### Step 8.1 — Wipe plaintext files from the Pico

```bash
mpremote connect /dev/ttyACM0 exec "
import os
for f in os.listdir('/'):
    if f.endswith('.py') and f not in ('main.py', 'crypto_loader.py', 'boot.py'):
        try: os.remove(f)
        except: pass
try:
    for f in os.listdir('/drivers'):
        if f.endswith('.py'):
            os.remove('/drivers/' + f)
except: pass
"
```

### Step 8.2 — Copy encrypted files

```bash
mpremote connect /dev/ttyACM0 cp -r encrypted_deploy/. :
```

Or file by file if the recursive copy is unavailable:

```bash
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

> **Important:** copy `drivers/*.enc` into a flat `drivers/` folder on the
> Pico root — do **not** let `mpremote` create a nested `drivers/drivers/`
> structure. Verify with:
> ```bash
> mpremote connect /dev/ttyACM0 exec "import os; print(os.listdir('/drivers'))"
> ```
> You should see `['max30102.enc', 'adxl345.enc', 'gsr.enc', '__init__.enc']`.

---

## 9. Verify Encrypted Boot

Reset the Pico and monitor the output:

```bash
mpremote connect /dev/ttyACM0 repl
```

Press `Ctrl-D`. Expected output:

```
[SOMNI][BOOT] ============================================
[SOMNI][BOOT] SOMNI-Guard v0.4 — Secure Boot
[SOMNI][BOOT] ============================================
[SOMNI][BOOT] Setup mode — USB fully open.
[SOMNI] Encrypted firmware loader available.
[SOMNI][CRYPTO] Loading encrypted firmware modules...
[SOMNI][CRYPTO] AES engine: ucryptolib
[SOMNI][CRYPTO] Decrypting 'config.enc'... OK
[SOMNI][CRYPTO] Decrypting 'drivers/max30102.enc'... OK
[SOMNI][CRYPTO] Decrypting 'drivers/adxl345.enc'... OK
[SOMNI][CRYPTO] Decrypting 'drivers/gsr.enc'... OK
[SOMNI][CRYPTO] Decrypting 'drivers/__init__.enc'... OK
[SOMNI][CRYPTO] Decrypting 'sampler.enc'... OK
...
[SOMNI] SOMNI-Guard v0.4 — Educational Sleep Monitor
[SOMNI] MAX30102 I2C bus initialised (SDA=GP4, SCL=GP5, 400000Hz).
[SOMNI][WIFI] Connecting to 'YourNetworkName'…
[SOMNI][WIFI] Connected. IP: 192.168.1.x
[SOMNI] Sampling active.
```

If you see `[SOMNI][CRYPTO] Decryption error`, double-check:
- The UID used matches the actual device UID from Step 3.
- The `_salt.bin` on the Pico matches the one used during encryption.
- Driver `.enc` files are in `drivers/` (not `drivers/drivers/`).

---

## 10. USB Lockdown (Disable USB Access)

After confirming the encrypted firmware boots correctly, activate the
three-layer USB lockdown in `boot.py`.

### How it works

`boot.py` checks for a file called `usb_locked.flag` on every boot.
When present, it applies three security layers in order:

| Layer | Mechanism | Stock MicroPython |
|-------|-----------|-------------------|
| 1 | `storage.disable_usb_drive()` — removes USB mass-storage device | CircuitPython only; silent no-op on stock MicroPython |
| 2 | `uos.mount(..., readonly=True)` — remounts LittleFS2 read-only | Works; adversary sees drive but cannot write files |
| 3 | `sys.stdin = _NullReader()` — replaces stdin with null reader, breaking the raw-REPL handshake | Works; `mpremote`/Thonny stall and cannot execute commands |

Layer 3 (stdin blocking) is the key addition over older versions of `boot.py`.
`mpremote` and Thonny both open *raw-REPL mode* by sending `Ctrl-A` over
USB-CDC and waiting for the REPL banner. Replacing `sys.stdin` with a null
reader causes the handshake to stall indefinitely, so no commands can be
injected even if the USB-CDC port is still physically present.

> **For complete hardware-level USB removal** (no USB-CDC port at all), see
> [Section 11](#11-custom-firmware-build-complete-usb-removal).

### Step 10.1 — Activate USB lockdown

Connect to the REPL one final time:

```bash
mpremote connect /dev/ttyACM0 repl
```

Run:

```python
from boot import lock_usb
lock_usb()
```

Expected output:

```
[SOMNI][BOOT] Lockdown flag written.
[SOMNI][BOOT] >>> Reboot now to apply USB lockdown. <<<
[SOMNI][BOOT]     Hold BOOTSEL on next power-on to bypass.
```

### Step 10.2 — Reboot and verify

Disconnect and reconnect the USB cable. On next boot `boot.py` will:
1. Find `usb_locked.flag`
2. Apply all three layers
3. Print `[SOMNI][BOOT] Lockdown applied.`

After lockdown, `mpremote connect` will appear to hang — that is expected
(the raw-REPL handshake stalls). The USB-CDC port may still appear in the
OS device list but no tool can interact with it.

### Step 10.3 — Bypass for maintenance (BOOTSEL escape hatch)

The RP2350 ROM bootloader is independent of MicroPython and ignores `boot.py`:

1. **Hold BOOTSEL** while connecting USB.
2. The Pico appears as `RPI-RP2` drive — full access restored.
3. You can reflash firmware, delete `usb_locked.flag`, or update `.enc` files.

To remove the lockdown without reflashing:
1. Enter BOOTSEL mode as above.
2. Flash standard MicroPython (resets the filesystem).
3. Redeploy your encrypted files (Section 8).
4. Do not call `lock_usb()` until you are ready.

---

## 11. Custom Firmware Build (Complete USB Removal)

> **Full build documentation:** see [`docs/micropython_build.md`](micropython_build.md)
> for prerequisites, a step-by-step explanation of every build stage,
> board config reference, troubleshooting, and recovery instructions.

For the strongest USB lockdown, build a custom MicroPython firmware with
the TinyUSB stack completely removed at compile time
(`MICROPY_HW_ENABLE_USBDEV=0`). This means the Pico never enumerates as
any USB device — no CDC serial port, no mass-storage drive, no WebUSB.

Wi-Fi, Bluetooth, and all sensors continue to work normally.

### Step 11.1 — Prerequisites (macOS)

The build script installs missing tools automatically via Homebrew:

```bash
# Ensure Homebrew is installed: https://brew.sh
brew install cmake arm-none-eabi-gcc
```

Python 3 must also be available (`python3 --version`).

### Step 11.2 — Run the build script

```bash
cd /path/to/NightWatchGaurd-main/scripts/custom_micropython_build
chmod +x build.sh
./build.sh
```

The script will:
1. Clone MicroPython (latest stable, shallow clone) into `scripts/custom_micropython_build/micropython/`
2. Initialise the Pico SDK, TinyUSB, mbedtls, btstack, cyw43-driver, and lwip submodules
3. Build `mpy-cross` (the MicroPython cross-compiler)
4. Install the `SOMNI_GUARD_PICO2W` board config from `mpconfigboard.h` and `mpconfigboard.cmake`
5. Build the firmware (~5 minutes on a modern Mac)
6. Copy the output to `somni_guard_firmware.uf2` in the project root

### Step 11.3 — What the custom board config does

`scripts/custom_micropython_build/mpconfigboard.h`:

```c
// Disables the entire TinyUSB stack — no CDC serial, no MSC drive
#define MICROPY_HW_ENABLE_USBDEV (0)
```

`scripts/custom_micropython_build/mpconfigboard.cmake`:

```cmake
set(PICO_BOARD "pico2_w")
set(MICROPY_PY_LWIP ON)           // Wi-Fi networking stack
set(MICROPY_PY_NETWORK_CYW43 ON)  // CYW43439 Wi-Fi/BT driver
set(MICROPY_PY_BLUETOOTH ON)
```

The RP2350 ROM bootloader (BOOTSEL mode) is in hardware and is **not**
affected by this build — you can always recover by holding BOOTSEL.

### Step 11.4 — Flash the custom firmware + encrypted files

Use the UF2 tool from Section 6 with the custom firmware as the base:

```bash
python scripts/somni_uf2_tool.py \
    --uid YOUR_PICO_UID \
    --src scripts/somniguard_pico/ \
    --firmware somni_guard_firmware.uf2 \
    --out somni_guard_complete.uf2
```

Flash `somni_guard_complete.uf2` via BOOTSEL drag-and-drop.
After this the Pico will never appear as a USB device under normal operation.

---

## 12. Raspberry Pi 5 Gateway Setup

### Step 12.1 — Operating System

Flash Raspberry Pi OS (64-bit, Bookworm) to your Pi 5's SD card.
Enable SSH during imaging.

### Step 12.2 — System dependencies

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
```

### Step 12.3 — Clone and install

```bash
git clone https://github.com/youruser/NightWatchGaurd.git
cd NightWatchGaurd/somniguard_gateway
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Step 12.4 — Configure environment

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

### Step 12.5 — Create data directories

```bash
sudo mkdir -p /var/lib/somniguard/reports
sudo chown -R $USER:$USER /var/lib/somniguard
```

### Step 12.6 — First run (creates admin account)

```bash
cd NightWatchGaurd/somniguard_gateway
source venv/bin/activate
set -a; source /etc/somniguard/env; set +a
python run.py
```

On first run you will be prompted to create an admin account.
The password must contain: uppercase, lowercase, digit, and special character.

### Step 12.7 — Run as a systemd service

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

## 13. Connecting Pico to Gateway

### Step 13.1 — Find the Pi 5's IP address

```bash
hostname -I
```

### Step 13.2 — Update Pico config

Edit `scripts/somniguard_pico/config.py`:

```python
GATEWAY_HOST = "192.168.1.100"   # Your Pi 5's IP
GATEWAY_PORT = 5000
```

Re-encrypt and redeploy (Section 6 or Sections 7–8) after any config change.

### Step 13.3 — Create a patient in the dashboard

1. Open `https://192.168.1.100:5000` in your browser.
2. Log in with the admin account.
3. Go to **Patients → New Patient**.
4. Note the patient ID (usually `1` on a fresh install).
5. Set `GATEWAY_PATIENT_ID = 1` in `config.py`.

### Step 13.4 — Start monitoring

Power on the Pico 2W. The LED will blink once per second when data is flowing.
Check the dashboard — a new session should appear under the patient's profile.

---

## 14. Security Reference

| Layer | Mechanism | Protection |
|-------|-----------|------------|
| Firmware at rest | AES-256-CBC `.enc` files (key = SHA-256(UID + salt)) | Source code & credentials unreadable without the specific Pico chip |
| USB file access (Layer 2) | `boot.py` read-only LittleFS2 remount | Files cannot be overwritten via USB |
| USB REPL access (Layer 3) | `boot.py` stdin null-reader breaks raw-REPL handshake | `mpremote`/Thonny stall; no code can be injected |
| USB hardware removal | Custom `SOMNI_GUARD_PICO2W` firmware (`MICROPY_HW_ENABLE_USBDEV=0`) | No USB device enumerated at all |
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

1. Hold BOOTSEL → plug USB → Pico enters bootloader (bypasses `boot.py`).
2. Edit `scripts/somniguard_pico/config.py` with new credentials.
3. Re-run `somni_uf2_tool.py` (or `encrypt_pico_files.py`) with the same `--uid`.
4. Flash the new UF2 or copy the new `.enc` files to the Pico.
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
