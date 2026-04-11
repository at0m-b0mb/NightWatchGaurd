# NightWatchGaurd — SOMNI‑Guard Sleep Monitor

> **Educational prototype — not a clinically approved device.**
> This project is a student exercise in medical‑device architecture,
> security thinking, and embedded firmware design.

**Repository:** [github.com/at0m-b0mb/NightWatchGaurd](https://github.com/at0m-b0mb/NightWatchGaurd)

---

## What is SOMNI‑Guard?

SOMNI‑Guard is a non‑clinical sleep‑monitoring prototype with three components:

| Component | Hardware | Role |
|-----------|----------|------|
| **Pico Node** | Raspberry Pi Pico 2 W | Wearable sensor node (SpO₂, motion, GSR) |
| **Gateway** | Raspberry Pi 5 | Data ingestion, storage, feature extraction, web UI |
| **Dashboard** | Browser (local only) | Clinician‑facing session review |

---

## Security Features

SOMNI‑Guard implements defence‑in‑depth security across all components:

### Device Security (Pico 2 W)
- **Encrypted firmware at rest** — All application modules AES‑256‑CBC encrypted; decrypted at runtime by hardware‑bound key
- **Firmware integrity verification** — SHA‑256 manifest checked at every boot
- **Encrypted configuration storage** — HMAC keys and Wi‑Fi credentials encrypted with hardware‑derived XTEA key
- **Anti‑replay protection** — Monotonic nonces and timestamps on every HMAC‑signed packet
- **Hardware watchdog timer** — Automatic device reset on firmware hang (8‑second timeout)
- **Secure memory wiping** — Key material zeroed after use

### Gateway Security (Pi 5)
- **UEFI Secure Boot** — Only signed kernels and bootloaders execute
- **TLS/HTTPS** — Self‑signed certificate generation for encrypted dashboard access
- **Rate limiting** — Flask‑Limiter protects login (5/min) and API (20/sec) endpoints
- **Security headers** — HSTS, CSP, X‑Frame‑Options, X‑Content‑Type‑Options on all responses
- **Audit logging** — Structured JSON logs for all access with rotation
- **Account lockout** — 15‑minute lockout after 10 failed login attempts
- **Session timeout** — 30‑minute session expiry with secure cookie flags
- **Password complexity** — Enforced minimum complexity for all accounts

### Network Security
- **Tailscale VPN** — Encrypted peer‑to‑peer mesh for remote dashboard access
- **HMAC‑SHA256 authentication** — Every Pico→Gateway packet is cryptographically signed
- **Network access policy** — Configurable Tailscale‑only mode with LAN exceptions for Pico

---

## Repository Structure

```
NightWatchGaurd/
├── somniguard_pico/          ← MicroPython firmware (Pico 2 W)
│   ├── main.py               ← Application entry point (watchdog, integrity check)
│   ├── crypto_loader.py      ← AES‑256‑CBC encrypted module loader
│   ├── config.py             ← Pin constants, rates, thresholds
│   ├── sampler.py            ← SensorSampler: timer loop & driver orchestration
│   ├── transport.py          ← Wi‑Fi HMAC transport with anti‑replay nonces
│   ├── utils.py              ← RingBuffer, get_timestamp, format_reading
│   ├── integrity.py          ← SHA‑256 firmware integrity verification
│   ├── secure_config.py      ← XTEA‑encrypted configuration storage
│   ├── manifest.json         ← Signed firmware hash manifest
│   └── drivers/
│       ├── __init__.py
│       ├── max30102.py       ← SpO₂/HR driver (educational R‑ratio approx.)
│       ├── adxl345.py        ← Accelerometer driver (±2g, 50 Hz ODR)
│       ├── ads1115.py        ← Optional: ADS1115 16‑bit ADC driver (upgrade path)
│       └── gsr.py            ← Galvanic skin response driver (built‑in ADC on GP26)
│
├── somniguard_gateway/       ← CPython gateway (Pi 5)
│   ├── run.py                ← Entry point with TLS support
│   ├── app.py                ← Flask app with security middleware
│   ├── config.py             ← Settings from environment variables
│   ├── database.py           ← SQLite schema + connection pooling + audit log
│   ├── reports.py            ← Feature extraction + ReportLab PDF generation
│   ├── tailscale.py          ← Tailscale VPN integration
│   ├── security.py           ← Rate limiting, headers, lockout, validation
│   ├── audit.py              ← Structured audit logging with rotation
│   ├── tls_setup.py          ← TLS certificate generation
│   ├── templates/            ← Jinja2 HTML templates
│   └── requirements.txt      ← Python dependencies
│
├── scripts/
│   ├── setup_secure_boot_pi5.sh     ← Pi 5 UEFI Secure Boot setup
│   ├── generate_integrity_manifest.py ← Firmware hash manifest generator
│   ├── encrypt_pico_files.py         ← AES‑256 firmware encryption tool
│   └── setup_tailscale_pi5.sh       ← Tailscale VPN setup
│
└── docs/
    ├── architecture.md       ← System architecture diagram & text
    ├── assets.md             ← Asset list A1–A9
    ├── attack_tree.md        ← Attack tree (G0–G5)
    ├── pha.md                ← Preliminary Hazard Analysis (H‑01 – H‑09)
    ├── security_controls.md  ← Defence‑in‑depth controls (L0–L3)
    ├── secure_boot.md        ← Pi 5 Secure Boot guide
    ├── encrypted_storage.md  ← Pico encrypted storage guide
    ├── encrypted_firmware.md ← Pico AES‑256 firmware encryption guide
    ├── security_hardening.md ← Comprehensive hardening checklist
    ├── developer_guide.md    ← Code documentation & team assignments
    ├── hardware_setup.md     ← BOM, wiring, installation, verification
    └── tailscale_setup.md    ← Tailscale VPN setup guide
```

---

## Quick‑start

### Clone the repository

```bash
git clone https://github.com/at0m-b0mb/NightWatchGaurd.git
cd NightWatchGaurd
```

### Pico Firmware — Hardware

| Sensor | Interface | Pins |
|--------|-----------|------|
| MAX30102 (SpO₂/HR) | I2C @ 400 kHz | SDA=GP4, SCL=GP5 (bus 0) |
| ADXL345 (Accel) | I2C @ 400 kHz | SDA=GP2, SCL=GP3 (bus 1) |
| Grove GSR v1.2 | ADC | GP26 (ADC0, Pin 31) |

### Deployment

1. Flash MicroPython (RP2350 build) onto the Pico 2 W.
2. Copy the entire `somniguard_pico/` directory to the Pico's filesystem
   (using Thonny, `mpremote`, or `rshell`).
3. Reset the board — `main.py` runs automatically.
4. Monitor USB‑serial output for `[SOMNI][DATA]` lines.

### Sample output

```
[SOMNI] SOMNI-Guard v0.3 — Educational Sleep Monitor
[SOMNI] NOT a clinically approved device.
[SOMNI][ADXL345] check_sensor: OK (DEVID 0xE5).
[SOMNI][MAX30102] check_sensor: OK (part ID 0x15).
[SOMNI][GSR] ADC initialised on pin 26.
[SOMNI][SAMPLER] Sampling loop started (accel@10Hz, SpO2@1Hz/GSR).
[SOMNI][DATA] t=1023ms SpO2=98.2% HR=62.0bpm accel=(0.01,-0.02,1.00)g GSR=12.3uS
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [User Guide](docs/user_guide.md) | **Start here** — how to set up and use the software: gateway, dashboard, patients, sessions, reports |
| [Hardware Setup Guide](docs/hardware_setup.md) | Bill of materials, pin diagrams, wiring, firmware flash, and gateway install |
| [Developer Guide](docs/developer_guide.md) | Every file, function, input/output, cross‑file interactions, and team work assignment |
| [Architecture](docs/architecture.md) | Data flow, trust boundaries, component details |
| [Assets](docs/assets.md) | Asset list A1–A9 with threat‑modelling rationale |
| [Attack Tree](docs/attack_tree.md) | Text attack tree rooted at misdiagnosis (G0–G5) |
| [Attack Tree DOT](docs/attack_tree.dot) | Graphviz source (`dot -Tsvg` to render) |
| [PHA](docs/pha.md) | Preliminary Hazard Analysis (9 hazards, S×L scoring) |
| [Security Controls](docs/security_controls.md) | Defence‑in‑depth controls (L0–L3, 24 controls) |
| [Secure Boot](docs/secure_boot.md) | Pi 5 UEFI Secure Boot setup guide |
| [Encrypted Storage](docs/encrypted_storage.md) | Pico 2W encrypted configuration storage guide |
| [Encrypted Firmware](docs/encrypted_firmware.md) | Pico 2W AES-256 encrypted firmware at-rest guide |
| [Security Hardening](docs/security_hardening.md) | Comprehensive security hardening checklist |
| [Tailscale Setup](docs/tailscale_setup.md) | Remote dashboard access via Tailscale VPN |

---

## Disclaimer

This project is an **educational prototype** produced as a student exercise.
It is **not** a regulated medical device, has **not** been clinically
validated, and must **not** be used for diagnosis, treatment, or any
patient‑safety purpose.  SpO₂ and heart‑rate values are approximations
using simplified algorithms and are not accurate enough for clinical use.