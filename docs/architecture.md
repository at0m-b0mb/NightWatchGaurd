# SOMNI‑Guard System Architecture

> **Educational prototype — not a clinically approved device.**
> All descriptions, diagrams, and data flows represent a student project
> intended to illustrate medical‑device architecture principles.

---

## 1. Overview

SOMNI‑Guard is a non‑clinical sleep‑monitoring prototype consisting of two
hardware nodes and a local web dashboard:

| Component | Hardware | Role |
|-----------|----------|------|
| Pico Node | Raspberry Pi Pico 2 W | Wearable/bedside sensor node |
| Gateway   | Raspberry Pi 5 | Data collection, storage, processing, web UI |
| Dashboard | Browser (local) | Clinician‑facing session review |

---

## 2. High‑Level Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Patient‑side (Pico 2 W)                          │
│                                                                          │
│  [MAX30102]──┐                                                           │
│  (SpO₂/HR)   │                                                          │
│              ├──I2C──► [Firmware / sampler.py]                           │
│  [ADXL345]───┘              │                                            │
│  (Accel)                    │  Tagged telemetry packets                  │
│                             │  (timestamp, SpO₂, HR, accel, GSR,        │
│  [GSR sensor]──ADC──────────┘   validity flags)                         │
│                             │                                            │
│                             ▼                                            │
│                    [UART / Wi‑Fi transport]                              │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │  (Trust Boundary — physical/network)
┌─────────────────────────────────▼───────────────────────────────────────┐
│                        Gateway (Raspberry Pi 5)                          │
│                                                                          │
│  [Ingestion service]                                                     │
│      • Validates packet schema & HMAC (future phase)                    │
│      • Writes raw telemetry to SQLite/SQLCipher DB                       │
│      │                                                                   │
│      ▼                                                                   │
│  [Feature extraction]                                                    │
│      • Count desaturations (SpO₂ < 90 %, non‑clinical threshold)        │
│      • Count arousals (motion magnitude spike)                           │
│      • Aggregate GSR arousal events                                      │
│      │                                                                   │
│      ▼                                                                   │
│  [Report generator]                                                      │
│      • Produces per‑night SleepReport object (JSON + DB row)            │
│      • Signs report (HMAC‑SHA256) — future phase                        │
│      │                                                                   │
│  ┌───┴───────────────────────────┐                                       │
│  │ SQLite/SQLCipher database     │  ◄── Report files (JSON/PDF)          │
│  │ (encrypted at rest)           │                                       │
│  └───────────────────────────────┘                                       │
│                             │                                            │
│  [Flask / local web server] │                                            │
│      • Serves dashboard on localhost only (no external interface)       │
│      • Requires session authentication (username + bcrypt password)     │
│      • Rate‑limited login; CSRF tokens on all forms                     │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │  (Trust Boundary — local HTTP)
┌─────────────────────────────────▼───────────────────────────────────────┐
│                        Clinician Dashboard (Browser)                     │
│                                                                          │
│  • List sessions                                                         │
│  • Open session → view nightly summary table + SpO₂/HR chart            │
│  • No cloud; everything stays on the Pi 5.                               │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Component Details

### 3.1 Pico 2 W Sensor Node (A1)

**Hardware**

| Sensor | Interface | Pin | Rate |
|--------|-----------|-----|------|
| MAX30102 (SpO₂/HR) | I2C @ 400 kHz | SDA=GP4, SCL=GP5 | 1 Hz |
| ADXL345 (Accel) | I2C @ 400 kHz | shared | 10 Hz |
| GSR (resistive) | ADC | GP26 (ADC0) | 1 Hz |
| Onboard LED | GPIO | LED | heartbeat |

**Software modules**

```
somniguard_pico/
  main.py          ← application entry point; fail‑soft top‑level catch
  config.py        ← all pin/rate/threshold constants
  sampler.py       ← SensorSampler: timer loop & driver orchestration
  utils.py         ← RingBuffer, get_timestamp, format_reading
  drivers/
    __init__.py
    max30102.py    ← MAX30102 driver (SpO₂/HR; educational R‑ratio approx.)
    adxl345.py     ← ADXL345 driver (±2g, 50 Hz ODR)
    gsr.py         ← GSR driver (voltage‑divider → µS)
```

**Fail‑soft guarantees**

- Each I2C/ADC call is wrapped in `try/except`; errors return `valid=False`.
- `check_all_sensors()` logs missing sensors but does not abort.
- Top‑level `try/except` in `main.py` catches any unhandled exception and
  either restarts the sampling loop or blinks the LED in a fault pattern.

### 3.2 Raspberry Pi 5 Gateway (A2)

*Planned for a future phase.*  Will include:

- Serial/Wi‑Fi ingestion service (Python daemon).
- SQLite/SQLCipher database with schema: `sessions`, `telemetry`, `reports`.
- Feature‑extraction pipeline.
- Report generator with HMAC signing.
- Flask web server bound to `127.0.0.1` only.

### 3.3 Web Dashboard (A3)

*Planned for a future phase.*  Local Flask application:

- Session listing page.
- Per‑session summary: SpO₂/HR time‑series chart, motion events, GSR trace.
- Authentication: bcrypt‑hashed password, session cookie, CSRF tokens.
- No cloud connectivity.

### 3.4 Data Storage (A4)

- **Primary store**: SQLite database, optionally encrypted with SQLCipher
  (application‑level AES‑256).
- **Disk encryption**: LUKS2 on the SD‑card partition (OS‑level, separate
  from SQLCipher).  Both layers are complementary.
- **Report files**: JSON summaries signed with HMAC‑SHA256 to detect tampering.

### 3.5 Patient‑worn Sensors (A5)

Passive analog and I2C sensors.  Trust boundary is physical: an attacker
with physical access can remove or tamper with sensors.  Validity flags in
the firmware allow the system to detect absent sensors.

---

## 4. Trust Boundaries

| Boundary | Location | Threats |
|----------|----------|---------|
| TB1 | Sensor ↔ Pico I2C bus | Physical wire tap, sensor removal |
| TB2 | Pico ↔ Pi 5 transport | Network interception, replay, DoS |
| TB3 | Pi 5 OS ↔ Database | Local privilege escalation, SD removal |
| TB4 | Pi 5 ↔ Browser | XSS, CSRF, session hijack |

---

## 5. Assumptions and Limitations

1. **Phase 1 (implemented)**: Sensor layer only — no network transport.
   Data is printed to USB‑serial (`[SOMNI][DATA]` prefix).
2. **Non‑clinical**: SpO₂ and HR values use simplified educational
   approximations.  Do not use for diagnosis.
3. **Single‑user**: The dashboard is designed for one authorised clinician
   on a local network.
4. **Prototype hardware**: RP2350 (Pico 2 W) MicroPython; no RTOS or
   hardware watchdog in this phase.
