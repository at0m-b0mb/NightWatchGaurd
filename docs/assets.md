# SOMNI‑Guard Asset List

> **Educational prototype — not a clinically approved device.**
> This asset list follows the MDC workbook style used in medical‑device
> cybersecurity threat modelling (aligned with AAMI TIR57 / IEC 81001‑5‑1
> concepts, adapted for a student project).

---

## Asset Table

| ID | Asset Name | Type | Role |
|----|-----------|------|------|
| A1 | Pico 2 W Wearable Node | Hardware + Firmware | Patient‑side sensor collection |
| A2 | Pi 5 Gateway / Compute Platform | Hardware + Software | Data ingestion, storage, processing |
| A3 | SOMNI‑Guard Web Dashboard | Software (Flask app) | Clinician‑facing session review UI |
| A4 | SOMNI‑Guard Data Storage | Data (SQLite DB + files) | Persistent telemetry and reports |
| A5 | Patient‑Worn Sensors | Hardware (sensors + cables) | Physiological signal acquisition |

---

## A1 — Pico 2 W Wearable Node

**Type:** Embedded hardware + MicroPython firmware

**Role:**  
The Pico 2 W is the patient‑side sensing unit.  It reads the MAX30102
(SpO₂/HR), ADXL345 (accelerometer), and GSR sensor, timestamps each
reading, applies basic validity checks, and streams telemetry to the Pi 5
gateway.

**Rich description:**  
This asset runs `main.py` as its entry point.  All sensor drivers are
isolated in `somniguard_pico/drivers/`.  The `sampler.py` module drives a
hardware timer loop at 10 Hz (accelerometer) and 1 Hz (SpO₂/GSR).
Fail‑soft error handling ensures that a single sensor failure does not
crash the device.  In Phase 1 output is via USB‑serial; Wi‑Fi transport
will be added in a later phase.

**Trust boundary:**  
TB1 (sensors ↔ Pico I2C) and TB2 (Pico ↔ Pi 5 transport).

**Why it must be threat‑modelled:**  
If an attacker can inject false data onto the I2C bus or replay stale
telemetry to the gateway, the gateway may compute a sleep report based on
manipulated data, potentially leading to a misdiagnosis.  Physical access
to the Pico also allows firmware replacement.

---

## A2 — Pi 5 Gateway / Compute Platform

**Type:** Single‑board computer + Python services

**Role:**  
The Pi 5 receives telemetry from the Pico, stores it in the database, runs
feature extraction (counting desaturations, arousals, motion events), and
serves the web dashboard.

**Rich description:**  
Runs a Python ingestion service (daemon), SQLite/SQLCipher database,
Flask web server, and report generator.  The web server binds to
`127.0.0.1` only and requires authentication.  LUKS2 disk encryption
protects the SD card at rest.

**Trust boundary:**  
TB2 (Pico ↔ Pi 5), TB3 (OS ↔ database), TB4 (Pi 5 ↔ browser).

**Why it must be threat‑modelled:**  
The Pi 5 is the central data aggregator.  Compromise of this node (remote
shell, physical SD‑card removal, or privilege escalation) allows an
attacker to read, alter, or delete all patient data and reports.  Tampered
reports could cause misdiagnosis of sleep‑apnea severity.

---

## A3 — SOMNI‑Guard Web Dashboard

**Type:** Local web application (Flask + Jinja2)

**Role:**  
Provides an authorised clinician with a browser‑based interface to list
sleep sessions, open a session, and view nightly summary graphs and tables.

**Rich description:**  
Single‑user Flask application bound to `127.0.0.1`.  Authentication uses a
bcrypt‑hashed password stored on the Pi 5.  All forms include CSRF tokens.
Output is HTML only; no cloud or external API calls.

**Trust boundary:**  
TB4 (Pi 5 ↔ browser).

**Why it must be threat‑modelled:**  
If the dashboard is compromised (XSS, CSRF, session hijack, brute‑force
login), an attacker could view confidential patient data, alter displayed
reports, or inject false information into the clinician's view without
touching the database directly.

---

## A4 — SOMNI‑Guard Data Storage

**Type:** Data asset (SQLite/SQLCipher database + JSON/PDF report files)

**Role:**  
Stores all raw telemetry, computed features, and nightly sleep reports on
the Pi 5's SD card.

**Rich description:**  
- **Database**: SQLite with optional SQLCipher encryption (AES‑256 at the
  application layer).  Tables: `sessions`, `telemetry`, `reports`.
- **Report files**: JSON files signed with HMAC‑SHA256 to detect tampering.
- **Disk layer**: LUKS2 encryption on the SD‑card partition (OS level).
  SQLCipher and LUKS2 are complementary; LUKS2 protects against physical
  SD removal while SQLCipher protects if the OS is compromised.

**Trust boundary:**  
TB3 (OS ↔ database/files).

**Why it must be threat‑modelled:**  
Raw telemetry and reports are the primary output of the system.  Tampering
with stored data (inserting, deleting, or modifying rows) directly corrupts
the nightly sleep report, the core artefact used for clinical
decision‑support.

---

## A5 — Patient‑Worn Sensors

**Type:** Hardware (passive sensors, cables, electrodes)

**Role:**  
Acquire physiological signals: SpO₂/HR (MAX30102), motion (ADXL345), and
skin conductance (GSR resistive sensor).  These are the primary inputs to
the entire system.

**Rich description:**  
- **MAX30102**: Optical pulse oximeter and heart‑rate sensor.  Placed on
  fingertip or earlobe.  Outputs raw IR and Red photoplethysmography counts
  over I2C.
- **ADXL345**: ±16g MEMS accelerometer.  Placed on wrist or chest strap.
  Detects sleep movement and arousals.
- **GSR electrodes**: Two skin electrodes connected to a voltage‑divider
  circuit.  Conductance increases during sympathetic arousal.

**Trust boundary:**  
TB1 (sensors ↔ Pico I2C/ADC).

**Why it must be threat‑modelled:**  
An attacker with physical access can remove sensors (causing false "no
contact" readings), replace sensors with signal generators (injecting
arbitrary waveforms), or manipulate electrode placement.  Validity flags
in the firmware provide limited detection, but physical security is the
primary control.
