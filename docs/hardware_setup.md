# SOMNI‑Guard Hardware Setup & Installation Guide

> **Educational prototype — not a clinically approved device.**
> All sensor readings are approximations for educational purposes only.
> Do **not** use this device for clinical diagnosis, treatment, or any
> patient‑safety purpose.

---

## Table of Contents

1. [Bill of Materials](#1-bill-of-materials)
2. [Raspberry Pi Pico 2 W Pinout Reference](#2-raspberry-pi-pico-2-w-pinout-reference)
3. [Sensor Wiring Diagrams](#3-sensor-wiring-diagrams)
   - 3.1 [MAX30102 — SpO₂ / Heart‑Rate Sensor](#31-max30102--spo-heart-rate-sensor)
   - 3.2 [ADXL345 — Accelerometer](#32-adxl345--accelerometer)
   - 3.3 [GSR — Galvanic Skin Response Sensor](#33-gsr--galvanic-skin-response-sensor)
4. [Complete Wiring Summary](#4-complete-wiring-summary)
5. [Pico Firmware Installation](#5-pico-firmware-installation)
6. [Gateway (Raspberry Pi 5) Installation](#6-gateway-raspberry-pi-5-installation)
7. [Configuration Guide](#7-configuration-guide)
8. [First‑Run Verification](#8-first-run-verification)
9. [Troubleshooting](#9-troubleshooting)
10. [Security Setup](#10-security-setup)

---

## 1. Bill of Materials

### Sensor Node (Pico 2 W)

| # | Part | Notes |
|---|------|-------|
| 1 | Raspberry Pi Pico 2 W | RP2350 chip, with Wi‑Fi |
| 2 | MAX30102 pulse‑oximetry module | SpO₂ and heart‑rate (I2C) |
| 3 | ADXL345 accelerometer module | 3‑axis motion (I2C) |
| 4 | Grove GSR v1.2 Sensor module | Galvanic skin response — 3 wires (VCC, GND, SIG → ADC) |
| 5 | Breadboard (half‑size or full) | For prototyping connections |
| 6 | Jumper wires (male–male) | Assorted |
| 7 | Micro‑USB cable | Power + USB‑serial |

> **Tip:** Most MAX30102 and ADXL345 breakout boards already include 3.3 V
> regulators and I2C pull‑up resistors, so no additional pull‑ups are needed.
> If you are using bare chips, add 4.7 kΩ pull‑ups from SDA and SCL to 3.3 V.

### Gateway Node (Raspberry Pi 5)

| # | Part | Notes |
|---|------|-------|
| 1 | Raspberry Pi 5 (4 GB or 8 GB) | Runs Python / Flask gateway |
| 2 | MicroSD card (≥ 32 GB, Class 10) | Raspberry Pi OS |
| 3 | USB‑C power supply (≥ 5 V / 3 A) | Official Pi 5 supply recommended |
| 4 | Ethernet cable **or** Wi‑Fi | Must be on the same LAN as the Pico |
| 5 | Monitor + keyboard (first setup) | Or use SSH headlessly |

---

## 2. Raspberry Pi Pico 2 W Pinout Reference

The diagram below shows the **physical pin numbers** (1–40) and the GPIO /
function labels relevant to SOMNI‑Guard.  Pins in use are marked with `◄`.

```
                    ┌───────────────┐
                    │   USB  port   │
                    └───────┬───────┘
                            │
         ┌──────────────────┴──────────────────┐
    GP0  │ 1                                40 │ VBUS (5 V from USB)
    GP1  │ 2                                39 │ VSYS (raw power in)
    GND  │ 3                                38 │ GND
 ◄ GP2  │ 4  I2C1 SDA                      37 │ 3V3_EN
 ◄ GP3  │ 5  I2C1 SCL                      36 │ 3V3(OUT) ◄─ 3.3 V power rail
 ◄ GP4  │ 6  I2C0 SDA                      35 │ ADC_VREF
 ◄ GP5  │ 7  I2C0 SCL                      34 │ GP28 (ADC2)
    GND  │ 8                                33 │ GND / AGND
    GP6  │ 9                                32 │ GP27 (ADC1)
    GP7  │ 10                               31 │ GP26 (ADC0) ◄─ GSR input
    GP8  │ 11                               30 │ RUN
    GP9  │ 12                               29 │ GP22
    GND  │ 13                               28 │ GND
   GP10  │ 14                               27 │ GP21
   GP11  │ 15                               26 │ GP20
   GP12  │ 16                               25 │ GP19
   GP13  │ 17                               24 │ GP18
    GND  │ 18                               23 │ GND
   GP14  │ 19                               22 │ GP17
   GP15  │ 20                               21 │ GP16
         └─────────────────────────────────────┘

  ◄  = pin used by SOMNI‑Guard firmware
```

### Key pins at a glance

| GPIO | Physical Pin | Function in SOMNI‑Guard |
|------|-------------|--------------------------|
| GP2 | Pin 4 | I2C1 SDA — ADXL345 accelerometer (dedicated bus) |
| GP3 | Pin 5 | I2C1 SCL — ADXL345 accelerometer (dedicated bus) |
| GP4 | Pin 6 | I2C0 SDA — MAX30102 SpO₂/HR sensor (dedicated bus) |
| GP5 | Pin 7 | I2C0 SCL — MAX30102 SpO₂/HR sensor (dedicated bus) |
| GP26 | Pin 31 | ADC0 — Grove GSR v1.2 SIG output (analogue input) |
| 3V3(OUT) | Pin 36 | 3.3 V supply for all sensors |
| GND | Pins 3, 8, 13, 18, 23, 28, 33, 38 | Ground (use any one) |
| LED | onboard | Heartbeat indicator (no external wiring) |

Each sensor uses a completely separate set of signal pins, so there is no
shared wiring between sensors other than the common power (3V3) and ground rails.

---

## 3. Sensor Wiring Diagrams

### 3.1 MAX30102 — SpO₂ / Heart‑Rate Sensor

The MAX30102 communicates over I2C at 400 kHz.  Its I2C address is **0x57**
(fixed by the hardware; the INT pin is not used by this firmware).

#### Pin connections

| MAX30102 module pin | Connects to | Pico physical pin |
|---------------------|-------------|-------------------|
| VIN / VCC | 3V3(OUT) | Pin 36 |
| GND | GND | Pin 38 |
| SDA | GP4 | Pin 6 |
| SCL | GP5 | Pin 7 |
| INT | *(not connected)* | — |

#### Wiring diagram

```
Pico 2 W                         MAX30102 module
─────────                         ───────────────
3V3(OUT) [Pin 36] ────────────── VIN / VCC
GND      [Pin 38] ────────────── GND
GP4      [Pin  6] ────────────── SDA
GP5      [Pin  7] ────────────── SCL
```

> **Note:** Most MAX30102 breakout boards include built‑in 3.3 V regulation
> and I2C pull‑ups (4.7 kΩ to VIN).  If yours does not, add 4.7 kΩ pull‑ups
> from SDA → 3.3 V and SCL → 3.3 V.

#### Sensor placement

Place the MAX30102 module so that its LED window faces the fingertip.  The
sensor must be in close contact with skin.  Even small gaps or movement will
cause invalid readings (`valid: False` in the firmware output).

---

### 3.2 ADXL345 — Accelerometer

The ADXL345 is on its own dedicated **I2C bus 1** (GP2 SDA / GP3 SCL), completely
separate from the MAX30102.  Its I2C address is **0x53**, selected by tying the
**SDO pin LOW (to GND)**.

> **Important:** The ADXL345's **CS pin must be tied HIGH (to VCC)** to
> enable I2C mode.  In SPI mode (CS = LOW), I2C is disabled.  Most breakout
> boards wire CS to VCC by default, but check your specific module.

#### Pin connections

| ADXL345 module pin | Connects to | Pico physical pin |
|--------------------|-------------|-------------------|
| VCC / VS | 3V3(OUT) | Pin 36 |
| GND | GND | Pin 38 |
| SDA | GP2 | Pin 4 |
| SCL | GP3 | Pin 5 |
| SDO | GND | Pin 38 (any GND) |
| CS | VCC (on module) | — (usually internal) |
| INT1 | *(not connected)* | — |
| INT2 | *(not connected)* | — |

#### Wiring diagram

```
Pico 2 W                         ADXL345 module
─────────                         ──────────────
3V3(OUT) [Pin 36] ────────────── VCC / VS
GND      [Pin 38] ────────────── GND
GP2      [Pin  4] ────────────── SDA     ← I2C1 (dedicated to ADXL345)
GP3      [Pin  5] ────────────── SCL     ← I2C1 (dedicated to ADXL345)
GND      [Pin 38] ────────────── SDO     ← pulls address to 0x53
                                 CS ──── VCC (internal on most boards)
```

#### Sensor placement

Mount the ADXL345 flat and secure.  For wrist‑worn sleep monitoring, attach
it with medical tape so it moves with the patient.  Ensure the board is not
loose — mechanical vibration from loose wiring will produce false motion events.

---

### 3.3 Grove GSR v1.2 — Galvanic Skin Response Sensor

The **Grove GSR v1.2** is a simple 3‑wire analogue sensor module.  Its SIG
pin outputs a voltage (0–3.3 V) that is read directly by the Pico's
built‑in ADC on **GP26 (ADC0)**.  No external ADC is needed.

The module contains an internal 10 kΩ reference resistor and a voltage
divider driven by the skin resistance between the two finger electrodes.

#### Circuit schematic

```
  Grove GSR v1.2 (internal)
  ┌───────────────────────────────────────────────┐
  │                                               │
  │  VCC (3.3 V) → [10 kΩ ref] → SIG → [skin] → GND │
  │                                               │
  │  SIG voltage = 3.3 V × R_skin / (R_ref + R_skin) │
  └───────────────────────────────────────────────┘
```

**How it works:**

The voltage at the SIG pin follows the voltage‑divider formula:

```
V_adc = 3.3 V × R_skin / (R_ref + R_skin)
```

The firmware inverts this to compute skin conductance (µS):

```
R_skin        = R_ref × V_adc / (3.3 − V_adc)     [R_ref = 10 kΩ]
Conductance   = 1 / R_skin × 1,000,000             (µS)
```

Higher skin conductance (lower skin resistance) = increased sympathetic
nervous system arousal — a proxy for sleep disturbance events.

#### Pin connections

| Grove GSR v1.2 pin | Connects to | Pico physical pin |
|---------------------|-------------|-------------------|
| VCC (Red) | 3V3(OUT) | Pin 36 |
| GND (Black) | GND | Pin 38 |
| SIG (Yellow) | GP26 (ADC0) | Pin 31 |
| NC (White) | *(not connected)* | — |

#### Wiring diagram

```
Pico 2 W                         Grove GSR v1.2
─────────                         ──────────────
3V3(OUT) [Pin 36] ────────────── VCC (Red)
GND      [Pin 38] ────────────── GND (Black)
GP26     [Pin 31] ────────────── SIG (Yellow)
```

That's it — 3 wires.  No external ADC, pull‑up resistors, or additional
components are needed.

#### Electrode placement

The Grove GSR v1.2 comes with finger‑strap electrodes.  Place them on
two adjacent fingers (e.g. index and middle finger) for best signal.  Areas
with higher sweat‑gland density (fingertips, inner wrist, palm) give
stronger GSR signals.

> **Safety note:** The maximum current through the skin is < 1 mA
> (3.3 V / 10 kΩ minimum), well below the pain threshold.  This is an
> **educational prototype** and should not be used on patients with
> pacemakers, skin conditions, or other contraindications.

#### Optional: ADS1115 upgrade for higher resolution

The Pico's built‑in ADC is 12‑bit (~9 effective bits of noise‑free
resolution).  For research requiring finer GSR granularity, you can upgrade
to an **ADS1115 16‑bit external ADC** on I2C bus 0 (shared with MAX30102).

To do this:
1. Wire the ADS1115 module to I2C bus 0 (GP4/GP5) with ADDR → GND (address 0x48).
2. Connect the Grove GSR SIG pin to ADS1115 AIN0 (instead of Pico GP26).
3. Uncomment the `ADS1115_*` settings in `config.py`.
4. Modify `drivers/gsr.py` to accept an ADS1115 instance.
5. See `drivers/ads1115.py` for the ready‑made ADS1115 driver.

---

## 4. Complete Wiring Summary

All connections in one table.  Use this as your build checklist.

| Wire | From (Pico pin) | To (Sensor pin) | Notes |
|------|----------------|-----------------|-------|
| 3.3 V power | Pin 36 (3V3 OUT) | MAX30102 VIN | |
| 3.3 V power | Pin 36 (3V3 OUT) | ADXL345 VCC | Share from same rail |
| 3.3 V power | Pin 36 (3V3 OUT) | Grove GSR VCC (Red) | Share from same rail |
| GND | Pin 38 (GND) | MAX30102 GND | |
| GND | Pin 38 (GND) | ADXL345 GND | |
| GND | Pin 38 (GND) | ADXL345 SDO | Sets I2C addr to 0x53 |
| GND | Pin 38 (GND) | Grove GSR GND (Black) | |
| MAX30102 SDA | Pin 6 (GP4) | MAX30102 SDA | I2C0 — dedicated to MAX30102 |
| MAX30102 SCL | Pin 7 (GP5) | MAX30102 SCL | I2C0 — dedicated to MAX30102 |
| ADXL345 SDA | Pin 4 (GP2) | ADXL345 SDA | I2C1 — dedicated to ADXL345 |
| ADXL345 SCL | Pin 5 (GP3) | ADXL345 SCL | I2C1 — dedicated to ADXL345 |
| ADC input | Pin 31 (GP26) | Grove GSR SIG (Yellow) | Direct analogue input |

### At‑a‑glance overview

```
                          ┌──────────────────────────────────┐
                          │       Raspberry Pi Pico 2 W      │
                          │                                  │
     ADXL345  SDA ────────┤ GP2      (Pin  4)  I2C1 SDA     │
     ADXL345  SCL ────────┤ GP3      (Pin  5)  I2C1 SCL     │
                          │                                  │
     MAX30102 SDA ────────┤ GP4      (Pin  6)  I2C0 SDA     │
     MAX30102 SCL ────────┤ GP5      (Pin  7)  I2C0 SCL     │
                          │                                  │
     MAX30102 VIN ────────┤ 3V3 OUT  (Pin 36)               │
     ADXL345  VCC ────────┤                                  │
     Grove GSR VCC ───────┤                                  │
                          │                                  │
     MAX30102 GND ────────┤ GND      (Pin 38)               │
     ADXL345  GND ────────┤                                  │
     ADXL345  SDO ────────┤                                  │
     Grove GSR GND ───────┤                                  │
                          │                                  │
     Grove GSR SIG ───────┤ GP26     (Pin 31)  ADC0         │
                          └──────────────────────────────────┘
```

---

## 5. Pico Firmware Installation

### Step 1 — Download MicroPython for RP2350

1. Go to <https://micropython.org/download/RPI_PICO2_W/>
2. Download the latest stable `.uf2` file for the **Pico 2 W** (RP2350 with
   Wi‑Fi, e.g. `RPI_PICO2_W-20241025-v1.24.0.uf2`).

### Step 2 — Flash MicroPython onto the Pico 2 W

1. Hold the **BOOTSEL** button on the Pico.
2. While holding BOOTSEL, connect the Pico to your computer with a Micro‑USB
   cable.
3. Release BOOTSEL.  A new USB drive named **RP2350** (or **RPI‑RP2**) will
   appear.
4. Drag and drop the `.uf2` file onto the drive.
5. The Pico will reboot automatically and the drive will disappear — MicroPython
   is now installed.

### Step 3 — Configure the firmware before copying

Before copying the firmware, edit **`somniguard_pico/config.py`** on your
computer.  You must set at minimum:

```python
# Wi‑Fi credentials
WIFI_SSID     = "YourNetworkName"
WIFI_PASSWORD = "YourNetworkPassword"

# Pi 5 gateway address (LAN IP — find it with `hostname -I` on the Pi 5)
GATEWAY_HOST = "192.168.1.100"    # ← change to your Pi 5's actual LAN IP
GATEWAY_PORT = 5000

# Patient ID (create the patient record in the dashboard first, note the ID)
GATEWAY_PATIENT_ID = 1

# Shared HMAC key — must EXACTLY match SOMNI_HMAC_KEY on the gateway
# Generate a new one: python3 -c "import secrets; print(secrets.token_hex(32))"
GATEWAY_HMAC_KEY = "paste-your-generated-key-here"
```

See [Section 7 — Configuration Guide](#7-configuration-guide) for full details.

### Step 4 — Copy the firmware to the Pico

Choose **one** of the following tools:

#### Option A: Thonny IDE (recommended for beginners)

1. Install Thonny from <https://thonny.org>.
2. Open Thonny; go to **Tools → Options → Interpreter** and select
   **MicroPython (Raspberry Pi Pico)**.
3. In the file browser panel, navigate to `somniguard_pico/` on your computer.
4. Select all files and folders (`config.py`, `main.py`, `sampler.py`,
   `transport.py`, `utils.py`, and the entire `drivers/` subfolder).
5. Right‑click → **Upload to /**.
6. Thonny will copy everything to the Pico's root filesystem.

#### Option B: `mpremote` (command‑line)

```bash
# Install mpremote
pip install mpremote

# Copy the entire firmware directory to the Pico
mpremote connect auto fs cp -r somniguard_pico/. :

# Verify files on the Pico
mpremote connect auto fs ls
```

#### Option C: `rshell` (command‑line)

```bash
pip install rshell
rshell --port /dev/ttyACM0       # Windows: COM3, macOS: /dev/cu.usbmodem*

# Inside rshell prompt:
cp -r somniguard_pico/* /pyboard/
ls /pyboard/
exit
```

### Step 5 — Reset and verify

1. Press the **RESET** button on the Pico (or unplug and replug the USB
   cable).
2. Open a serial monitor.  In Thonny use the Shell panel; on the command line:

   ```bash
   # Linux / macOS
   screen /dev/ttyACM0 115200

   # Windows (PowerShell with PuTTY or using mpremote)
   mpremote connect auto
   ```

3. You should see output like:

   ```
   [SOMNI] ================================================
   [SOMNI] SOMNI-Guard v0.3 — Educational Sleep Monitor
   [SOMNI] NOT a clinically approved device.
   [SOMNI] ================================================
   [SOMNI] Onboard LED initialised.
   [SOMNI] MAX30102 I2C bus initialised (SDA=GP4, SCL=GP5, 400000Hz).
   [SOMNI] ADXL345 I2C bus initialised (SDA=GP2, SCL=GP3, 400000Hz).
   [SOMNI][ADXL345] Sensor configured (±2g, 50 Hz ODR, measurement mode).
   [SOMNI][ADXL345] check_sensor: OK (DEVID 0xE5).
   [SOMNI][MAX30102] Sensor configured (SpO₂ mode).
   [SOMNI][MAX30102] check_sensor: OK (part ID 0x15).
   [SOMNI][GSR] ADC initialised on pin 26.
   [SOMNI][SAMPLER] Sensor check — MAX30102:True ADXL345:True GSR:True
   [SOMNI][WIFI] Connecting to 'YourNetworkName'…
   [SOMNI][WIFI] Connected. IP: 192.168.1.42
   [SOMNI] Gateway session started: ID 1.
   [SOMNI][SAMPLER] Sampling loop started (accel@10Hz, SpO2@1Hz/GSR).
   [SOMNI][DATA] t=1023ms SpO2=98.2% HR=62.0bpm accel=(0.01,-0.02,1.00)g GSR=12.3uS
   ```

If any sensor shows `False` in the sensor check, see
[Section 9 — Troubleshooting](#9-troubleshooting).

---

## 6. Gateway (Raspberry Pi 5) Installation

### Step 1 — Prepare Raspberry Pi OS

1. Flash **Raspberry Pi OS Lite** (64‑bit, Bookworm) to the SD card using
   [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
2. In Imager's advanced settings (gear icon), enable SSH and set a hostname
   (e.g. `somni-pi5`).
3. Insert the SD card, boot the Pi 5, and SSH in:
   ```bash
   ssh pi@somni-pi5.local
   ```

### Step 2 — Install system dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git
```

### Step 3 — Clone the repository

```bash
cd ~
git clone https://github.com/at0m-b0mb/NightWatchGaurd.git
cd NightWatchGaurd
```

### Step 4 — Create a Python virtual environment and install dependencies

```bash
cd somniguard_gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 5 — Generate secrets and create the environment file

```bash
# Generate strong random keys (Linux / macOS)
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
HMAC_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

> **Windows (PowerShell):** Use the following instead:
> ```powershell
> $SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
> $HMAC_KEY   = python -c "import secrets; print(secrets.token_hex(32))"
> ```
> Then paste the printed values directly into `/etc/somniguard/env` on the Pi 5.

```bash

# Create the environment file
sudo mkdir -p /etc/somniguard
sudo tee /etc/somniguard/env > /dev/null <<EOF
SOMNI_SECRET_KEY=${SECRET_KEY}
SOMNI_HMAC_KEY=${HMAC_KEY}
SOMNI_TAILSCALE_ONLY=false
SOMNI_DB_PATH=/home/pi/NightWatchGaurd/somniguard_gateway/somniguard.db
SOMNI_REPORT_DIR=/home/pi/NightWatchGaurd/somniguard_gateway/reports
EOF

sudo chmod 600 /etc/somniguard/env
echo "Generated HMAC key (copy this to Pico config.py GATEWAY_HMAC_KEY):"
echo "$HMAC_KEY"
```

> **Important:** Copy the printed HMAC key into `GATEWAY_HMAC_KEY` in
> `somniguard_pico/config.py` before deploying the Pico firmware.

### Step 6 — Start the gateway

```bash
# Load the environment and start
source /etc/somniguard/env
export $(grep -v '^#' /etc/somniguard/env | xargs)

cd ~/NightWatchGaurd/somniguard_gateway
source .venv/bin/activate
python run.py
```

On first run, `run.py` will:
- Create the SQLite database and tables.
- Create an **admin** user and print the temporary password to the terminal.
  Change it immediately after first login.

You should see:

```
[SOMNI-GW] Database initialised.
[SOMNI-GW] Admin user created — username: admin  password: <printed here>
 * Running on http://0.0.0.0:5000
```

### Step 7 — (Optional) Install as a systemd service

To start the gateway automatically on boot:

```bash
sudo tee /etc/systemd/system/somniguard.service > /dev/null <<'EOF'
[Unit]
Description=SOMNI-Guard Gateway
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/NightWatchGaurd/somniguard_gateway
EnvironmentFile=/etc/somniguard/env
ExecStart=/home/pi/NightWatchGaurd/somniguard_gateway/.venv/bin/python run.py
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

### Step 8 — (Optional) Set up Tailscale VPN

For remote dashboard access, follow the full
[Tailscale Setup Guide](tailscale_setup.md).  The short version:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=somni-pi5
# Set SOMNI_TAILSCALE_ONLY=true in /etc/somniguard/env for production
```

---

## 7. Configuration Guide

### 7.1 Pico firmware — `somniguard_pico/config.py`

| Setting | Default | What to change |
|---------|---------|----------------|
| `WIFI_SSID` | `"SomniGuard_Net"` | Your Wi‑Fi network name |
| `WIFI_PASSWORD` | `"change-me-wifi"` | Your Wi‑Fi password |
| `GATEWAY_HOST` | `"192.168.1.100"` | Pi 5's **LAN** IP address (not Tailscale IP) |
| `GATEWAY_PORT` | `5000` | Leave as 5000 unless you changed it on the gateway |
| `GATEWAY_PATIENT_ID` | `1` | Patient ID from the web dashboard |
| `GATEWAY_HMAC_KEY` | *(dev placeholder)* | **Must match** `SOMNI_HMAC_KEY` on the Pi 5 |
| `DEVICE_ID` | `"pico-01"` | Unique name for this Pico (used in session records) |
| `TRANSPORT_ENABLED` | `True` | Set to `False` for USB‑serial‑only debug mode |

> **Finding the Pi 5's LAN IP:** On the Pi 5, run `hostname -I`.
> Use the first IP shown (e.g. `192.168.1.42`).

### 7.2 Gateway — environment variables (`/etc/somniguard/env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `SOMNI_SECRET_KEY` | *(dev placeholder)* | Flask session signing key — **must be random in production** |
| `SOMNI_HMAC_KEY` | *(dev placeholder)* | Shared key for Pico↔Gateway authentication — **must match Pico** |
| `SOMNI_TAILSCALE_ONLY` | `false` | Set `true` in production to restrict dashboard to Tailscale peers |
| `SOMNI_HOST` | `0.0.0.0` | IP address the Flask server binds to |
| `SOMNI_PORT` | `5000` | TCP port for the web dashboard |
| `SOMNI_DB_PATH` | *(relative)* | Absolute path to the SQLite database file |
| `SOMNI_REPORT_DIR` | *(relative)* | Absolute path where PDF reports are stored |
| `SOMNI_PICO_CIDRS` | `192.168.0.0/16,...` | LAN CIDRs from which Pico telemetry is accepted |

### 7.3 Adding a patient record

Before the Pico can send data, you must create a patient record in the
gateway database:

1. Open the dashboard at `http://<Pi 5 LAN IP>:5000/`.
2. Log in with the admin credentials printed at first startup.
3. Navigate to **Patients → Add Patient** and fill in the details.
4. Note the **Patient ID** shown in the patient list.
5. Set `GATEWAY_PATIENT_ID = <that ID>` in `somniguard_pico/config.py` and
   re‑copy the firmware to the Pico.

---

## 8. First‑Run Verification

Work through this checklist top to bottom before a full recording session.

### Hardware checklist

- [ ] MAX30102 is wired: VCC → 3V3, GND → GND, SDA → **GP4** (Pin 6), SCL → **GP5** (Pin 7)
- [ ] ADXL345 is wired: VCC → 3V3, GND → GND, SDA → **GP2** (Pin 4), SCL → **GP3** (Pin 5),
      SDO → GND, CS → VCC (usually on‑board)
- [ ] Grove GSR v1.2 is wired: VCC (Red) → 3V3, GND (Black) → GND, SIG (Yellow) → **GP26** (Pin 31)
- [ ] All connections are firm (no loose jumper wires)
- [ ] MAX30102 and ADXL345 signal wires are on **different** Pico pins (they do not share SDA/SCL)

### Firmware / sensor check

- [ ] Serial monitor shows `check_sensor: OK` for both MAX30102 and ADXL345
- [ ] Serial monitor shows `ADC initialised on pin 26` for the GSR
- [ ] `[SOMNI][DATA]` lines appear every second with non‑zero values
- [ ] Covering the MAX30102 with a finger makes the IR raw value jump above
      50,000 and SpO₂ / HR values appear

### Network check

- [ ] Serial monitor shows `Connected. IP: 192.168.x.x` for Wi‑Fi
- [ ] Serial monitor shows `Gateway session started: ID <n>`
- [ ] Gateway log (on the Pi 5) shows incoming `POST /api/ingest` requests
- [ ] The dashboard shows new readings under the patient's session

### Security check (production)

- [ ] `GATEWAY_HMAC_KEY` in Pico config matches `SOMNI_HMAC_KEY` on gateway
- [ ] Both keys are **not** the default `"dev-hmac-key-change-this..."` value
- [ ] `SOMNI_SECRET_KEY` is a random 32‑byte hex string
- [ ] Admin dashboard password has been changed from the generated default

---

## 9. Troubleshooting

### Pico / sensor issues

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `ADXL345 check_sensor: False` | Wiring error or wrong address | Verify SDA=**GP2** (Pin 4), SCL=**GP3** (Pin 5), SDO=GND. Run the I2C1 bus scan below. |
| `MAX30102 check_sensor: False` | Wiring error or wrong address | Verify SDA=**GP4** (Pin 6), SCL=**GP5** (Pin 7). Run the I2C0 bus scan below — should see `0x57`. |
| SpO₂ always `None` / "No finger detected" | FIFO overflow bug (fixed v0.4) or no finger | Update to v0.4 driver (see MAX30102 FIFO fix below). Place finger firmly on LED window; IR raw must exceed 5 000. |
| GSR value always `0` or very high | Electrode not in contact | Press electrodes firmly to skin; check that Grove GSR SIG wire is connected to GP26 (Pin 31). |
| `ADXL345 I2C init failed` | GP2/GP3 wiring error | Re‑check ADXL345 SDA→GP2 and SCL→GP3. Check for short circuits. |
| `MAX30102 I2C init failed` | GP4/GP5 wiring error | Re‑check MAX30102 SDA→GP4 and SCL→GP5. Check for short circuits. |
| `Wi‑Fi connection timeout` | Wrong SSID/password or signal too weak | Verify `WIFI_SSID` and `WIFI_PASSWORD` in config.py. Move Pico closer to AP. |
| Onboard LED blinks rapidly (100 ms) | Fatal firmware crash | Connect serial monitor; read the `[SOMNI][FATAL]` error message. |

#### I2C bus scan (diagnostic)

Each sensor is on its own bus, so scan them independently in the Thonny REPL:

```python
from machine import I2C, Pin

# Scan I2C0 — should find MAX30102 at 0x57 (decimal 87)
i2c0 = I2C(0, sda=Pin(4), scl=Pin(5), freq=400_000)
print("I2C0:", i2c0.scan())   # Expected: [87]

# Scan I2C1 — should find ADXL345 at 0x53 (decimal 83)
i2c1 = I2C(1, sda=Pin(2), scl=Pin(3), freq=400_000)
print("I2C1:", i2c1.scan())   # Expected: [83]
```

If a list is empty, the sensor is not detected — re‑check the wiring for that bus.
If an address does not match, verify the SDO wiring for ADXL345 (see table below).

> **Tip:** Use the dedicated test scripts in `pico_tests/` instead of typing
> REPL commands manually.  `test_i2c_scan.py` does the above scan for you
> and shows friendly diagnostic messages.

---

### MAX30102 "No finger detected" — Detailed Fix (v0.4)

**Root cause (bug in v0.3 and earlier):**

The MAX30102 FIFO samples at 100 sps internally.  The main application reads
at 1 Hz.  After 320 ms the FIFO fills its 32-sample depth and — with
`FIFO_ROLLOVER_EN = 1` — the write pointer wraps back to the current read
pointer position.  The old `read_fifo()` code calculated
`num_samples = (wr_ptr - rd_ptr) & 0x1F`.  When the FIFO had overflowed,
this computed **0**, which the code incorrectly interpreted as *"FIFO empty"*
and returned `(None, None)`.  Every single 1 Hz read hit this path, so the
sensor appeared to detect no finger regardless of what was placed on it.

**What the v0.4 fix does:**

```
read_fifo() now reads OVF_COUNTER in addition to WR_PTR and RD_PTR.
If OVF_COUNTER > 0, there IS data — it seeks RD_PTR to (WR_PTR - 1)
to get the freshest sample, clears OVF_COUNTER, then reads one sample.
```

**All three changes made in v0.4:**

| Setting | v0.3 (old) | v0.4 (fixed) | Reason |
|---------|-----------|-------------|--------|
| FIFO overflow handling | `return (None, None)` when `wr_ptr == rd_ptr` | Check `OVF_COUNTER`; seek to latest sample | Core bug fix |
| LED current | 0x24 = 7.2 mA | 0x7F = 25.4 mA | More reliable across skin tones |
| No-finger threshold | 50 000 | 5 000 | Prevent valid low-signal reads being misclassified |
| Post-reset delay | 10 ms | 50 ms | Gives cheap module boards time to POR |

**If "No finger detected" persists after the v0.4 fix:**

1. Run `pico_tests/test_max30102.py` — it shows raw IR/Red counts and tells
   you exactly what the sensor is seeing.
2. Check that the red and IR LEDs glow visibly when you look at the module
   face-on (the two small dots next to the black window).
3. Try a higher LED current — edit `_LED_AMPLITUDE` at the top of
   `drivers/max30102.py`:
   ```python
   _LED_AMPLITUDE = 0xFF   # 51 mA — maximum, for difficult cases
   ```
4. Shield the sensor from strong ambient light (sunlight saturates the ADC).
5. On the module's 3-bit solder pad, confirm the bridge is on the **3V3**
   position, not 1V8.

---

### Gateway / network issues

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Gateway returns HTTP 401/403 for Pico data | HMAC key mismatch | Ensure `GATEWAY_HMAC_KEY` (Pico) exactly matches `SOMNI_HMAC_KEY` (gateway env). |
| `Could not start gateway session` | Wrong `GATEWAY_HOST` or gateway not running | Check `GATEWAY_HOST` in Pico config is the Pi 5's **LAN** IP. Start the gateway. |
| Dashboard not reachable from browser | `SOMNI_TAILSCALE_ONLY=true` but Tailscale not set up | Set `SOMNI_TAILSCALE_ONLY=false` for local dev, or follow the [Tailscale guide](tailscale_setup.md). |
| `ModuleNotFoundError` on gateway startup | Dependencies not installed | Run `pip install -r requirements.txt` inside the `.venv`. |
| Database permission error | Wrong `SOMNI_DB_PATH` or wrong file owner | Set an absolute writable path in `SOMNI_DB_PATH`. |

### Checking which I2C address the ADXL345 will use

| SDO pin | I2C address |
|---------|-------------|
| Connected to GND | **0x53** ← firmware default |
| Connected to VCC | 0x1D |

The firmware uses `ADXL345_ADDR = 0x53` (set in `config.py`).  If your
breakout board has SDO pulled to VCC internally, change `ADXL345_ADDR = 0x1D`
in `somniguard_pico/config.py`.

---

*For network / VPN configuration see [Tailscale Setup Guide](tailscale_setup.md).*
*For code architecture see [Architecture](architecture.md) and [Developer Guide](developer_guide.md).*

---

## 10. Security Setup

### 10.1 Secure Boot (Pi 5)

Secure Boot ensures the Pi 5 will only boot firmware signed with your private
key.  See [docs/secure_boot.md](secure_boot.md) for full background, risks, and
recovery procedures before proceeding.  The one-time OTP fuse programming is
**irreversible**.

```bash
cd scripts
sudo bash setup_secure_boot_pi5.sh
```

### 10.2 Encrypted Configuration Storage (Pico 2W)

Migrate from plaintext `config.py` to encrypted storage so that Wi-Fi
credentials and the HMAC key are not stored in cleartext on the Pico filesystem.

1. **Generate the encrypted config** from your existing `config.py` values.
   Run this on your development machine (not on the Pico itself):

   ```bash
   python scripts/generate_integrity_manifest.py \
       --pico-dir somniguard_pico/ \
       --output somniguard_pico/manifest.json
   ```

   Then, using a MicroPython REPL connected to the Pico, generate and save the
   encrypted config:

   ```python
   from secure_config import get_hardware_key, save_secure_config
   import config

   key = get_hardware_key()
   cfg = {
       "WIFI_SSID":         config.WIFI_SSID,
       "WIFI_PASSWORD":     config.WIFI_PASSWORD,
       "GATEWAY_HMAC_KEY":  config.GATEWAY_HMAC_KEY,
       "GATEWAY_HOST":      config.GATEWAY_HOST,
       "GATEWAY_PORT":      config.GATEWAY_PORT,
   }
   save_secure_config(cfg, "/secure_config.bin", key)
   print("Encrypted config saved.")
   ```

2. **Deploy `secure_config.py` to the Pico** alongside the rest of the firmware
   (it is already included in `somniguard_pico/`).

3. **Verify the encrypted config loads correctly** at boot.  The serial monitor
   should show `[SOMNI] Secure config loaded.` rather than the plaintext config
   warning.  If loading fails, `config.py` plaintext values are used as fallback
   and a warning is printed.

### 10.3 Firmware Integrity Manifest

Generate a signed manifest of all Pico firmware files and deploy it alongside
the firmware.  `integrity.py` verifies every file hash at boot and aborts if
tampering is detected.

```bash
python scripts/generate_integrity_manifest.py \
    --pico-dir somniguard_pico/ \
    --output somniguard_pico/manifest.json
```

Copy `manifest.json` to the Pico's root filesystem using Thonny or `mpremote`:

```bash
mpremote connect auto fs cp somniguard_pico/manifest.json :manifest.json
```

The manifest must be regenerated and re-deployed any time a firmware file is
modified.

### 10.4 Security Verification Checklist

In addition to the checks in [Section 8](#8-firstrun-verification), verify the
following security controls before a production deployment:

- [ ] Firmware integrity check passes at boot (`[SOMNI][INTEGRITY] All firmware files verified OK.`)
- [ ] Encrypted config loads successfully (`[SOMNI] Secure config loaded.`)
- [ ] Hardware watchdog is enabled (`Hardware watchdog enabled`)
- [ ] Gateway HTTPS is working (if `SOMNI_HTTPS=true` — browse to `https://<Pi5-IP>:5000/` and confirm a TLS connection)
- [ ] Rate limiting is active (`Flask-Limiter initialised` in gateway startup log)
- [ ] Audit logging is active (`Audit logging initialised` in gateway startup log)
- [ ] Security headers present (open browser dev tools → Network tab → inspect any response for `X-Frame-Options`, `X-Content-Type-Options`, `Content-Security-Policy`)
