# NightWatchGaurd — SOMNI‑Guard Sleep Monitor

> **Educational prototype — not a clinically approved device.**
> This project is a student exercise in medical‑device architecture,
> security thinking, and embedded firmware design.

---

## What is SOMNI‑Guard?

SOMNI‑Guard is a non‑clinical sleep‑monitoring prototype with three components:

| Component | Hardware | Role |
|-----------|----------|------|
| **Pico Node** | Raspberry Pi Pico 2 W | Wearable sensor node (SpO₂, motion, GSR) |
| **Gateway** | Raspberry Pi 5 | Data ingestion, storage, feature extraction, web UI |
| **Dashboard** | Browser (local only) | Clinician‑facing session review |

---

## Repository Structure

```
NightWatchGaurd/
├── somniguard_pico/          ← MicroPython firmware (Pico 2 W)
│   ├── main.py               ← Application entry point (fail‑soft)
│   ├── config.py             ← Pin constants, rates, thresholds
│   ├── sampler.py            ← SensorSampler: timer loop & driver orchestration
│   ├── utils.py              ← RingBuffer, get_timestamp, format_reading
│   └── drivers/
│       ├── __init__.py
│       ├── max30102.py       ← SpO₂/HR driver (educational R‑ratio approx.)
│       ├── adxl345.py        ← Accelerometer driver (±2g, 50 Hz ODR)
│       └── gsr.py            ← Galvanic skin response driver
│
└── docs/
    ├── architecture.md       ← System architecture diagram & text
    ├── assets.md             ← Asset list (A1–A5, MDC workbook style)
    ├── attack_tree.md        ← Attack tree (text, G0–G3)
    ├── attack_tree.dot       ← Graphviz DOT source for attack tree
    ├── pha.md                ← Preliminary Hazard Analysis (H‑01 – H‑06)
    └── security_controls.md  ← Cybersecurity design controls (L1–L3)
```

---

## Pico Firmware Quick‑start

### Hardware

| Sensor | Interface | Pins |
|--------|-----------|------|
| MAX30102 (SpO₂/HR) | I2C @ 400 kHz | SDA=GP4, SCL=GP5 |
| ADXL345 (Accel) | I2C @ 400 kHz | shared |
| GSR sensor | ADC | GP26 (ADC0) |

### Deployment

1. Flash MicroPython (RP2350 build) onto the Pico 2 W.
2. Copy the entire `somniguard_pico/` directory to the Pico's filesystem
   (using Thonny, `mpremote`, or `rshell`).
3. Reset the board — `main.py` runs automatically.
4. Monitor USB‑serial output for `[SOMNI][DATA]` lines.

### Sample output

```
[SOMNI] SOMNI-Guard v0.1 — Educational Sleep Monitor
[SOMNI] NOT a clinically approved device.
[SOMNI][ADXL345] check_sensor: OK (DEVID 0xE5).
[SOMNI][MAX30102] check_sensor: OK (part ID 0x15).
[SOMNI][GSR] ADC initialised on pin 26.
[SOMNI][SAMPLER] Sampling loop started (accel@10Hz, SpO2/GSR@1Hz).
[SOMNI][DATA] t=1023ms SpO2=98.2% HR=62.0bpm accel=(0.01,-0.02,1.00)g GSR=12.3uS
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/architecture.md) | Data flow, trust boundaries, component details |
| [Assets](docs/assets.md) | Asset list A1–A5 with threat‑modelling rationale |
| [Attack Tree](docs/attack_tree.md) | Text attack tree rooted at misdiagnosis (G0) |
| [Attack Tree DOT](docs/attack_tree.dot) | Graphviz source (`dot -Tsvg` to render) |
| [PHA](docs/pha.md) | Preliminary Hazard Analysis (6 hazards, S×L scoring) |
| [Security Controls](docs/security_controls.md) | Defence‑in‑depth controls (L1 device, L2 gateway, L3 dashboard) |

---

## Disclaimer

This project is an **educational prototype** produced as a student exercise.
It is **not** a regulated medical device, has **not** been clinically
validated, and must **not** be used for diagnosis, treatment, or any
patient‑safety purpose.  SpO₂ and heart‑rate values are approximations
using simplified algorithms and are not accurate enough for clinical use.