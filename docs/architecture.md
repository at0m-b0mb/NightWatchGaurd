# SOMNI‑Guard System Architecture

> **Educational prototype — not a clinically approved device.**
> All descriptions, diagrams, and data flows represent a student project
> intended to illustrate medical‑device architecture principles.

---

## 1. Overview

SOMNI‑Guard is a non‑clinical sleep‑monitoring prototype consisting of two
hardware nodes, a local web dashboard, and a Tailscale peer-to-peer VPN
overlay that secures all remote access:

| Component | Hardware | Role |
|-----------|----------|------|
| Pico Node | Raspberry Pi Pico 2 W | Wearable/bedside sensor node |
| Gateway   | Raspberry Pi 5 | Data collection, storage, processing, web UI |
| Dashboard | Browser (via Tailscale) | Clinician‑ / developer‑facing session review |
| Tailscale overlay | Software VPN mesh | Encrypted, mutually‑authenticated P2P tunnels |

---

## 2. High‑Level Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Patient‑side (Pico 2 W)                          │
│                                                                          │
│  [MAX30102]──┐                                                           │
│  (SpO2/HR)   │                                                           │
│              ├──I2C──► [Firmware / sampler.py]                           │
│  [ADXL345]───┘              │                                            │
│  (Accel)                    │  Tagged telemetry packets                  │
│                             │  (timestamp, SpO2, HR, accel, GSR,        │
│  [GSR sensor]──ADC──────────┘   validity flags)                         │
│                             │                                            │
│                             ▼                                            │
│               [Wi-Fi transport -- transport.py]                          │
│               HMAC-SHA256 authenticated HTTP POST                        │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │  TB2 (Local LAN -- HMAC authenticated)
┌─────────────────────────────────▼───────────────────────────────────────┐
│                        Gateway (Raspberry Pi 5)                          │
│                                                                          │
│  [Ingestion API /api/*]                                                  │
│      * Verifies HMAC-SHA256 on every packet                              │
│      * Validates packet schema & value ranges                            │
│      * Accepts connections from local LAN (Pico) + Tailscale peers      │
│      │                                                                   │
│      ▼                                                                   │
│  [Feature extraction / reports.py]                                       │
│      * Count desaturations (SpO2 < 90%, non-clinical threshold)         │
│      * Count arousals (motion magnitude spike)                           │
│      * Aggregate GSR arousal events                                      │
│      │                                                                   │
│      ▼                                                                   │
│  [Report generator]                                                      │
│      * Produces per-night SleepReport (JSON + PDF)                       │
│      * Signs report with HMAC-SHA256                                     │
│      │                                                                   │
│  ┌───┴───────────────────────────┐                                       │
│  │ SQLite database (WAL mode)    │  <-- Report files (JSON/PDF)          │
│  │ + LUKS2 disk encryption       │                                       │
│  └───────────────────────────────┘                                       │
│                             │                                            │
│  [Flask web server / app.py]│                                            │
│      * Requires session authentication (username + bcrypt password)     │
│      * CSRF tokens on all forms                                          │
│      * Network policy: accepts only Tailscale IPs + loopback            │
│        (TAILSCALE_ONLY=true) in production                              │
│                             │                                            │
│  [Tailscale daemon]         │                                            │
│      * Assigns stable 100.x.x.x IP to Pi 5                             │
│      * WireGuard encrypted tunnel to each authorised peer               │
└─────────────────────────────────┬───────────────────────────────────────┘
                                  │  TB5 (Tailscale WireGuard mTLS)
         ┌────────────────────────┼────────────────────────┐
         │                        │                        │
  [Clinician Laptop]      [Developer Laptop]       [Future Pi node]
  (Tailscale installed)   (Tailscale installed)    (Tailscale installed)
  Browser --> dashboard   Browser / curl           Monitoring endpoint
  http://100.x.x.x:5000  http://100.x.x.x:5000
```

---

## 3. Component Details

### 3.1 Pico 2 W Sensor Node (A1)

**Hardware**

| Sensor | Interface | Pin | Rate |
|--------|-----------|-----|------|
| MAX30102 (SpO2/HR) | I2C @ 400 kHz | SDA=GP4, SCL=GP5 | 1 Hz |
| ADXL345 (Accel) | I2C @ 400 kHz | shared | 10 Hz |
| GSR (resistive) | ADC | GP26 (ADC0) | 1 Hz |
| Onboard LED | GPIO | LED | heartbeat |

**Software modules**

```
somniguard_pico/
  main.py          <- application entry point; fail-soft top-level catch
  config.py        <- all pin/rate/threshold constants + Wi-Fi/gateway settings
  sampler.py       <- SensorSampler: timer loop & driver orchestration
  transport.py     <- Wi-Fi connect; HMAC-SHA256 HTTP POST to Pi 5
  utils.py         <- RingBuffer, get_timestamp, format_reading
  drivers/
    __init__.py
    max30102.py    <- MAX30102 driver (SpO2/HR; educational R-ratio approx.)
    adxl345.py     <- ADXL345 driver (+-2g, 50 Hz ODR)
    gsr.py         <- GSR driver (voltage-divider -> uS)
```

**Fail-soft guarantees**

- Each I2C/ADC call is wrapped in try/except; errors return valid=False.
- check_all_sensors() logs missing sensors but does not abort.
- Top-level try/except in main.py catches any unhandled exception and
  either restarts the sampling loop or blinks the LED in a fault pattern.
- Wi-Fi / transport errors are non-fatal: data continues to be logged to
  USB-serial; unsent readings are dropped (RingBuffer overflow).

**Networking note**: The Pico 2 W cannot run Tailscale (MicroPython /
no Tailscale binary). It communicates with the Pi 5 over the **local
Wi-Fi LAN segment** only. HMAC-SHA256 packet authentication replaces
the Tailscale mutual-authentication guarantee for this hop (TB2).

### 3.2 Raspberry Pi 5 Gateway (A2)

| Sub-component | Implementation |
|---------------|---------------|
| Ingestion API | Flask /api/* routes -- HMAC-verified, CSRF-exempt |
| Feature extraction | reports.py -- SpO2/HR/GSR/accel aggregation |
| Report generator | reports.py -- ReportLab PDF + HMAC-SHA256 signing |
| Database | SQLite (WAL mode) -- database.py |
| Web dashboard | Flask + Flask-Login + Flask-WTF |
| Tailscale daemon | tailscaled systemd service |
| Network policy | tailscale.py + app.py before_request |

**Software modules**

```
somniguard_gateway/
  run.py           <- entry point; first-run admin bootstrap
  app.py           <- Flask app: all routes, network-policy before_request
  config.py        <- settings from environment variables
  database.py      <- SQLite schema + parameterised query helpers
  reports.py       <- feature extraction + ReportLab PDF generation
  tailscale.py     <- Tailscale IP checks, daemon queries, policy enforcement
  templates/       <- Jinja2 HTML templates
  requirements.txt <- Python dependencies
```

### 3.3 Tailscale VPN Overlay (A6)

The Pi 5 runs the Tailscale daemon (tailscaled), which:

1. Authenticates the Pi 5 with the Tailscale coordination server (control-plane,
   not on the data path).
2. Assigns a stable 100.x.x.x Tailscale IP to the Pi 5.
3. Provides a MagicDNS hostname (e.g. somni-pi5.your-tailnet.ts.net).
4. Establishes direct WireGuard P2P encrypted tunnels to authorised peers
   (clinician laptops, developer machines).
5. Falls back to Tailscale DERP relay servers if a direct tunnel cannot be
   established (NAT, firewall).

Clinicians and developers install Tailscale on their laptops, sign in to the
same tailnet account, and then browse to http://100.x.x.x:5000/. No
port-forwarding, firewall rules, or VPN certificates need to be managed
manually.

### 3.4 Web Dashboard (A3)

Local Flask application served by the Pi 5, accessible over the Tailscale
mesh:

- Session listing page.
- Per-session: SpO2/HR telemetry table, motion events, GSR trace.
- Report generate / PDF download.
- Authentication: bcrypt-hashed password, session cookie, CSRF tokens.
- **No cloud connectivity** -- all data stays on the Pi 5.

### 3.5 Data Storage (A4)

- **Primary store**: SQLite database (WAL mode, FK enforcement, parameterised
  queries throughout).
- **Disk encryption**: LUKS2 on the SD-card partition (OS-level AES-XTS-256).
- **Report files**: PDF + JSON summaries signed with HMAC-SHA256.

### 3.6 Patient-worn Sensors (A5)

Passive analog and I2C sensors. Trust boundary is physical: an attacker
with physical access can remove or tamper with sensors. Validity flags in
the firmware allow the system to detect absent sensors.

---

## 4. Trust Boundaries

| Boundary | Location | Threats | Mitigation |
|----------|----------|---------|-----------|
| TB1 | Sensor <-> Pico I2C bus | Physical wire tap, sensor removal | Validity flags, plausibility checks |
| TB2 | Pico <-> Pi 5 (local LAN) | Network interception, replay, DoS | HMAC-SHA256 on every packet |
| TB3 | Pi 5 OS <-> Database | Local privilege escalation, SD removal | Least-privilege user, LUKS2, parameterised SQL |
| TB4 | Pi 5 <-> Browser (web) | XSS, CSRF, session hijack | CSRF tokens, Jinja2 auto-escape, bcrypt auth |
| TB5 (new) | Pi 5 <-> Remote clients | Eavesdropping, rogue peer, MITM | Tailscale WireGuard mTLS; TAILSCALE_ONLY=true |

---

## 5. Assumptions and Limitations

1. **Non-clinical**: SpO2 and HR values use simplified educational
   approximations. Do not use for diagnosis.
2. **Pico <-> Pi 5 is LAN-only**: The Pico cannot run Tailscale. The local
   LAN segment is a trusted physical environment (hospital room / home).
   HMAC authentication covers integrity; confidentiality relies on the
   physical LAN security.
3. **Tailscale account trust**: All devices that join the tailnet share the
   same account. Use Tailscale ACL tags to enforce least-privilege between
   nodes within the tailnet.
4. **Single-gateway**: The architecture assumes one Pi 5 gateway. Multiple
   gateways would require database replication (not in scope for this phase).
5. **Prototype hardware**: RP2350 (Pico 2 W) MicroPython; no RTOS or
   hardware watchdog in this phase.
