# SOMNI-Guard — User Guide

> **Educational prototype — not a clinically approved device.**
> SpO₂ and HR values are approximations and must not be used for diagnosis or treatment.

This guide explains how to set up and use the SOMNI-Guard software end-to-end:
from first boot to viewing a completed sleep-monitoring session report.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [First-Time Setup — Gateway (Pi 5)](#2-first-time-setup--gateway-pi-5)
3. [First-Time Setup — Pico Node](#3-first-time-setup--pico-node)
4. [Starting the System](#4-starting-the-system)
5. [Using the Web Dashboard](#5-using-the-web-dashboard)
   - 5.1 [Logging In](#51-logging-in)
   - 5.2 [Dashboard Home](#52-dashboard-home)
   - 5.3 [Adding a Patient](#53-adding-a-patient)
   - 5.4 [Viewing a Patient's Sessions](#54-viewing-a-patients-sessions)
   - 5.5 [Viewing a Session](#55-viewing-a-session)
   - 5.6 [Generating a PDF Report](#56-generating-a-pdf-report)
   - 5.7 [Managing Users (Admin)](#57-managing-users-admin)
6. [Running a Monitoring Session](#6-running-a-monitoring-session)
7. [Stopping the System](#7-stopping-the-system)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. System Overview

SOMNI-Guard has three parts that work together:

```
[Pico 2W node]  ──Wi-Fi/HMAC──►  [Pi 5 gateway]  ◄──Browser──  [You]
  Wearable                         Flask + SQLite     Dashboard
  SpO₂, accel, GSR                 stores data,       web UI
                                   generates reports
```

| Part | What it does |
|------|-------------|
| **Pico 2W** | Reads sensors every second, signs the data with HMAC, sends to the Pi 5 |
| **Pi 5 gateway** | Receives and stores sensor data, serves the web dashboard, generates PDF reports |
| **Your browser** | You use the dashboard to create patients, view sessions, and download reports |

---

## 2. First-Time Setup — Gateway (Pi 5)

Do this **once** when you first set up the Pi 5.

### Step 1 — Install Python dependencies

```bash
cd NightWatchGaurd/somniguard_gateway
pip install -r requirements.txt
```

This installs Flask, Flask-Login, Flask-WTF, Flask-Limiter, bcrypt, ReportLab, and other dependencies.

### Step 2 — Set environment variables (recommended)

These variables configure secrets. You can skip this for a quick test (defaults work), but **you must set them before any real deployment**.

```bash
# Generate a strong Flask secret key
export SOMNI_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

# HMAC key — this MUST match GATEWAY_HMAC_KEY in somniguard_pico/config.py
export SOMNI_HMAC_KEY="your-shared-secret-key-32-chars-min"

# Optional: custom database location
export SOMNI_DB_PATH="/home/pi/somniguard.db"
```

> **Tip:** Save these to a file like `~/somniguard.env` and run `source ~/somniguard.env` each time, or add them to `/etc/somniguard/env` for a permanent setup.

### Step 3 — First run and admin account creation

```bash
cd NightWatchGaurd/somniguard_gateway
python run.py
```

On the very first run (no database exists yet), you are prompted to create an admin account:

```
[SOMNI] No users found. Creating initial admin account.
[SOMNI] Leave blank to use the default (shown in brackets).

Admin username [admin]: admin
Admin email [admin@localhost]: admin@localhost
Admin password (min 8 chars): ••••••••

[SOMNI] Admin user 'admin' created.
[SOMNI] Starting gateway on 0.0.0.0:5000
[SOMNI] Dashboard: http://localhost:5000/
```

Open `http://<pi5-ip-address>:5000` in your browser. You should see the login page.

---

## 3. First-Time Setup — Pico Node

Do this **once** when you first set up the Pico.

### Step 1 — Edit `somniguard_pico/config.py`

Open [somniguard_pico/config.py](../somniguard_pico/config.py) and change the following:

```python
# Your Wi-Fi network name and password
WIFI_SSID     = "YourNetworkName"
WIFI_PASSWORD = "YourWiFiPassword"

# IP address of your Pi 5 on the local network
# Find it with: hostname -I   (run this on the Pi 5)
GATEWAY_HOST = "192.168.1.100"   # ← change to your Pi 5's actual IP

# Must exactly match SOMNI_HMAC_KEY set on the gateway
GATEWAY_HMAC_KEY = "your-shared-secret-key-32-chars-min"

# Patient ID — create the patient in the dashboard first (Section 5.3),
# then come back and set this to that patient's ID number
GATEWAY_PATIENT_ID = 1
```

### Step 2 — Flash MicroPython firmware

If MicroPython is not already on the Pico 2W:

1. Hold the **BOOTSEL** button, plug in the USB cable, then release BOOTSEL.
2. A USB drive called `RPI-RP2` appears on your computer.
3. Download the **RP2350** MicroPython `.uf2` from [micropython.org/download](https://micropython.org/download/).
4. Drag the `.uf2` onto the `RPI-RP2` drive. The Pico reboots automatically.

### Step 3 — Copy firmware to the Pico

**Using Thonny (easiest):**
1. Open Thonny. In the bottom-right, select `MicroPython (Raspberry Pi Pico)`.
2. Go to **File → Open** and open the `somniguard_pico/` folder.
3. Select all files in `somniguard_pico/`, right-click → **Upload to /**.
4. Do the same for the `somniguard_pico/drivers/` folder → **Upload to /drivers/**.

**Using mpremote (command line):**
```bash
cd NightWatchGaurd
mpremote connect auto cp -r somniguard_pico/. :
```

### Step 4 — Verify the sensors are wired

See [hardware_setup.md](hardware_setup.md) for the full wiring diagram. Quick reference:

| Sensor | Bus | SDA | SCL | Address |
|--------|-----|-----|-----|---------|
| MAX30102 (SpO₂/HR) | I2C 0 | GP4 (pin 6) | GP5 (pin 7) | 0x57 |
| ADXL345 (accel) | I2C 1 | GP2 (pin 4) | GP3 (pin 5) | 0x53 |
| Grove GSR v1.2 | ADC | GP26 (pin 31) | — | — |

---

## 4. Starting the System

Every time you want to run a monitoring session, do this in order:

### Step 1 — Start the gateway

```bash
cd NightWatchGaurd/somniguard_gateway
python run.py
```

You should see:
```
[SOMNI] Starting gateway on 0.0.0.0:5000
[SOMNI] Dashboard: http://localhost:5000/
```

Leave this terminal open — the gateway must stay running during the session.

### Step 2 — Open the dashboard in a browser

On any computer on the same network, open:
```
http://<pi5-ip-address>:5000
```

Log in with your admin account (or another account if one was created for you).

### Step 3 — Power on the Pico

Connect the Pico to a USB power source (power bank, USB charger, or the Pi 5's USB port). The `main.py` script starts automatically.

Watch the serial output in Thonny or via mpremote to confirm the Pico connects:
```
[SOMNI] SOMNI-Guard v0.4 — Educational Sleep Monitor
[SOMNI][ADXL345] check_sensor: OK (DEVID 0xE5).
[SOMNI][MAX30102] check_sensor: OK (part ID 0x15).
[SOMNI][GSR] ADC initialised on pin 26.
[SOMNI][WIFI] Connected. IP: 192.168.1.42
[SOMNI][TRANSPORT] Session started. session_id=3
[SOMNI][SAMPLER] Sampling loop started (accel@10Hz, SpO2@1Hz/GSR).
[SOMNI][DATA] t=1023ms SpO2=98.2% HR=62.0bpm accel=(0.01,-0.02,1.00)g GSR=12.3uS
```

The Pico automatically starts a new session on the gateway. Data appears in the dashboard.

---

## 5. Using the Web Dashboard

### 5.1 Logging In

Go to `http://<pi5-ip>:5000/login`.

| Field | What to enter |
|-------|--------------|
| Username | The username you created (default: `admin`) |
| Password | The password you chose on first run |

**Security notes:**
- After **10 failed login attempts** from an IP, that IP is locked out for 15 minutes.
- Sessions expire after **30 minutes of inactivity** — you will be returned to the login page.

---

### 5.2 Dashboard Home

After logging in you land at `/dashboard`. This shows:

- **System status** — whether the gateway is running and Tailscale VPN status (if configured)
- **Recent sessions** — a list of the most recent monitoring sessions across all patients
- Quick links to **Patients** and **User Management**

---

### 5.3 Adding a Patient

Before running a monitoring session, create a patient record:

1. Click **Patients** in the navigation bar → goes to `/patients`.
2. Fill in the **New Patient** form on that page:
   - **Name** — patient's name (or study ID for anonymity)
   - **Date of birth** — used in PDF reports
   - **Notes** — any relevant clinical notes (optional)
3. Click **Add Patient**.

The patient appears in the list with an auto-assigned **ID number** (e.g. `1`).

> **Important:** Copy this ID number into `GATEWAY_PATIENT_ID` in `somniguard_pico/config.py` so the Pico links its sessions to the correct patient.

---

### 5.4 Viewing a Patient's Sessions

Click on a patient's name in the Patients list → goes to `/patients/<id>`.

This page shows:

| Column | Meaning |
|--------|---------|
| Session ID | Unique session number |
| Device | Which Pico sent the data (e.g. `pico-01`) |
| Start time | When the Pico connected and started sending |
| End time | When the session was closed (blank = still running) |
| Readings | How many data points were received |
| Actions | View session detail or generate a report |

---

### 5.5 Viewing a Session

Click **View** on any session → goes to `/sessions/<id>`.

You see:

- **Session summary** — device ID, start/end times, total readings
- **SpO₂ & HR table** — timestamped readings with SpO₂ (%), HR (bpm), accel (g), GSR (µS)
- **Desaturation events** — readings where SpO₂ dropped below 90% (non-clinical threshold)
- **Movement events** — readings where motion exceeded 0.05g (possible arousals)

The raw data table shows one row per second of monitoring.

---

### 5.6 Generating a PDF Report

On a session detail page or patient page:

1. Click **Generate Report**.
2. The gateway runs feature extraction — it calculates averages, counts desaturation events, and identifies arousal periods.
3. Click **Download Report** to save the PDF to your computer.

The PDF includes:
- Patient name and session date/time
- Mean ± SD for SpO₂, HR, and GSR
- Count and duration of desaturation events (SpO₂ < 90%)
- Count of movement/arousal events
- **Disclaimer** that all values are educational approximations

> Reports are saved on the Pi 5 in `somniguard_gateway/reports/` as well.

---

### 5.7 Managing Users (Admin)

Go to **Admin → Users** (`/admin/users`). This page is only visible to admin accounts.

**Add a user:**
1. Fill in username, email, password, and role (`admin` or `viewer`).
2. Click **Add User**.

**Delete a user:**
- Click **Delete** next to a user. You cannot delete your own account.

**Password requirements:**
- Minimum 8 characters
- Must include at least one uppercase letter, one digit, and one special character

| Role | Can do |
|------|--------|
| `admin` | Everything: create/delete users, view all patients and sessions, generate reports |
| `viewer` | View patients and sessions, generate reports — cannot manage users |

---

## 6. Running a Monitoring Session

A complete session from start to finish:

```
1. Start gateway        →  python run.py
2. Open dashboard       →  http://<pi5-ip>:5000
3. Create patient       →  Patients → New Patient (if not done already)
4. Set patient ID       →  Edit GATEWAY_PATIENT_ID in somniguard_pico/config.py
5. Power on Pico        →  Connect USB power — it auto-connects and starts session
6. Place sensors        →  Finger on MAX30102, Pico strapped to wrist/chest for accel,
                           GSR electrodes on fingers
7. Monitor              →  Watch dashboard for incoming data
8. End monitoring       →  Power off Pico (or Ctrl+C its serial console)
9. View session         →  Dashboard → Patient → View session
10. Generate report     →  Session detail → Generate Report → Download
```

**How long does a session run?**
The Pico runs continuously until power is removed or the USB cable is disconnected. There is no built-in stop button — just power off the Pico when the monitoring period ends. The gateway automatically closes the session when the Pico stops sending data (the session's `end_time` is set when `/api/session/end` is called or when the connection drops).

**What if Wi-Fi drops?**
The Pico buffers readings locally (5 seconds by default — set by `TRANSPORT_BATCH_SIZE`). If the connection is lost, it retries. If the retry fails, readings are discarded and a warning is printed. The session continues on reconnection.

---

## 7. Stopping the System

**Stop the Pico:**
- Unplug the USB power cable, or press Ctrl+C in the Thonny serial console.
- The Pico sends a `/api/session/end` request to the gateway before shutting down (if the network is still up).

**Stop the gateway:**
- Press **Ctrl+C** in the terminal where `python run.py` is running.
- All data is already saved to the SQLite database — nothing is lost.

---

## 8. Troubleshooting

### Gateway won't start

| Symptom | Fix |
|---------|-----|
| `ModuleNotFoundError: No module named 'flask'` | Run `pip install -r requirements.txt` |
| `Address already in use` | Another process is using port 5000. Kill it: `lsof -ti:5000 \| xargs kill` or change `SOMNI_PORT` |
| Can't reach dashboard from another computer | Make sure you're using the Pi 5's IP, not `localhost`. Find it with `hostname -I` on the Pi 5 |

### Pico won't connect to Wi-Fi

| Symptom | Fix |
|---------|-----|
| `[WIFI] Connection timed out` | Check `WIFI_SSID` and `WIFI_PASSWORD` in `config.py` — they must match exactly (case-sensitive) |
| `[WIFI] No AP found` | The 2.4 GHz network may be out of range. Pico 2W is 2.4 GHz only — 5 GHz networks won't work |
| IP assigned but can't reach gateway | Check `GATEWAY_HOST` is set to the Pi 5's IP. Ping it from another device to confirm |

### Sensor issues

| Symptom | Fix |
|---------|-----|
| `[MAX30102] No finger detected` | Place finger flat on the sensor, apply gentle pressure. LED amplitude is set to 25.4 mA (0x7F) — if still failing, run `pico_tests/test_max30102.py` to diagnose |
| SpO₂ always `None` | Run `pico_tests/test_i2c_scan.py` — check MAX30102 shows at address 0x57. Verify SDA=GP4, SCL=GP5 |
| ADXL345 not found | Run `pico_tests/test_i2c_scan.py` — check ADXL345 shows at address 0x53. Verify SDA=GP2, SCL=GP3. If CS pin is floating, pull it HIGH |
| GSR reads 0 or 65535 | Check sensor cable. 0 = open circuit (wire not connected). 65535 = short circuit |

### Dashboard / login issues

| Symptom | Fix |
|---------|-----|
| Locked out (too many failed logins) | Wait 15 minutes, or restart the gateway (resets the in-memory lockout counter) |
| "Session expired" redirecting to login | Sessions expire after 30 minutes. Log in again |
| Report download fails | Check the Pi 5 has disk space: `df -h`. Reports are saved to `somniguard_gateway/reports/` |

### Data not appearing in dashboard

| Symptom | Fix |
|---------|-----|
| Session never appears | Check `GATEWAY_PATIENT_ID` in Pico config matches a patient that exists in the dashboard |
| Session exists but no readings | Check HMAC keys match: `GATEWAY_HMAC_KEY` (Pico) = `SOMNI_HMAC_KEY` (gateway). Mismatch = all packets rejected |
| Readings show stale/old values | Check the Pico's clock (`import time; print(time.time())`) — if it returns 0 the RTC is not set; this can affect timestamps |

---

*For hardware wiring details, see [hardware_setup.md](hardware_setup.md).*
*For sensor test scripts, see [pico_tests/README.md](../pico_tests/README.md).*
*For the full developer reference, see [developer_guide.md](developer_guide.md).*
