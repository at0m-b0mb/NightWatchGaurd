# SOMNI‑Guard Asset List

*A medical-device security project by Team NightWatchLabs.*

> **Educational prototype — not a clinically approved device.**
> This asset list, produced by Team NightWatchLabs, follows the MDC
> workbook style used in medical‑device cybersecurity threat modelling
> (aligned with AAMI TIR57 / IEC 81001‑5‑1 concepts, adapted for a
> research project).

---

## Asset Table

| ID | Asset Name | Type | Role |
|----|-----------|------|------|
| A1 | Pico 2 W Wearable Node | Hardware + Firmware | Patient‑side sensor collection |
| A2 | Pi 5 Gateway / Compute Platform | Hardware + Software | Data ingestion, storage, processing |
| A3 | SOMNI‑Guard Web Dashboard | Software (Flask app) | Clinician‑facing session review UI |
| A4 | SOMNI‑Guard Data Storage | Data (SQLite DB + files) | Persistent telemetry and reports |
| A5 | Patient‑Worn Sensors | Hardware (sensors + cables) | Physiological signal acquisition |
| A7 | Firmware Integrity Manifest | Data (JSON + HMAC) | Boot-time firmware verification |
| A8 | Secure Boot Chain | Infrastructure (UEFI keys) | Signed kernel/bootloader verification |
| A9 | Audit Log Store | Data (logs + DB table) | Security event forensic trail |

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

---

## A6 — Tailscale Tailnet (VPN Overlay)

**Type:** Network infrastructure (software VPN mesh)

**Role:**
The Tailscale tailnet provides a peer-to-peer WireGuard encrypted mesh
network between the Pi 5 gateway and authorised clinician / developer
laptops.  It is the secure communication channel for all remote dashboard
access in production.

**Rich description:**
- **Control plane**: Tailscale's coordination server authenticates devices
  and distributes WireGuard public keys.  It is operated by Tailscale Inc.
  and is **not** on the data path (no patient data transits Tailscale's servers).
- **Data plane**: Direct WireGuard P2P tunnels between enrolled nodes,
  using ChaCha20-Poly1305 authenticated encryption.  Falls back to Tailscale
  DERP relay servers if direct tunnel is blocked by NAT or firewall.
- **MagicDNS**: Assigns stable hostnames (e.g. `somni-pi5.ts.net`) to each
  node, removing the need for static IPs.
- **Tailscale ACLs**: JSON policy rules in the Tailscale admin console
  restrict which nodes can reach port 5000 on the Pi 5.
- **Tailscale SSH**: Enables SSH to the Pi 5 through the tailnet without
  opening port 22 to the LAN.

**Trust boundary:**
TB5 (Pi 5 ↔ remote clients over Tailscale WireGuard tunnel).

**Why it must be threat-modelled:**
If the Tailscale tailnet is compromised (rogue device enrolled, ACL policy
misconfigured, or Tailscale coordination server compromised), an unauthorised
party may gain access to the SOMNI-Guard web dashboard and all patient data.
A misconfigured ACL that leaves port 5000 open to all tailnet nodes (rather
than only clinical-staff nodes) would undermine the access-control goal even
while using Tailscale.

---

## A7 — Firmware Integrity Manifest

**Type:** Data asset (JSON file + HMAC signature)

**Role:**
The integrity manifest (`manifest.json`) contains SHA-256 hashes of all Python firmware modules on the Pico 2W. It is signed with HMAC-SHA256 using the shared secret key and verified at every boot by `integrity.py`.

**Rich description:**
- **Content**: JSON file mapping relative file paths to their SHA-256 hex digests.
- **Signature**: HMAC-SHA256 of the canonical JSON representation of the file hash dictionary.
- **Generation**: Produced by `scripts/generate_integrity_manifest.py` on the development machine.
- **Storage**: Stored alongside firmware files on the Pico filesystem.

**Trust boundary:**
TB6 (Pico filesystem).

**Why it must be threat-modelled:**
If an attacker can forge the manifest (by obtaining the HMAC key), they can replace firmware files and create a matching manifest, bypassing the integrity check entirely. The manifest's security depends on the secrecy of the HMAC key, which is protected by the encrypted configuration storage (secure_config.py).

---

## A8 — Secure Boot Chain

**Type:** Infrastructure asset (UEFI keys + signed binaries)

**Role:**
The Pi 5 UEFI Secure Boot chain ensures that only cryptographically signed kernels and bootloaders can execute on the gateway. This prevents boot-time firmware tampering.

**Rich description:**
- **Platform Key (PK)**: Root of trust for the UEFI Secure Boot chain.
- **Key Exchange Key (KEK)**: Authorises changes to the signature database.
- **Signature Database (db)**: Contains the signing certificate used to verify kernel and bootloader signatures.
- **Key storage**: `/etc/somniguard/secure-boot/keys/` with mode 0600.
- **Setup**: `scripts/setup_secure_boot_pi5.sh` automates key generation, signing, and enrollment.

**Trust boundary:**
TB3 (Pi 5 OS).

**Why it must be threat-modelled:**
If the secure boot keys are compromised (stolen via G2.1 or G2.2), an attacker can sign a malicious kernel that includes backdoors or disables security controls. The keys must be stored with strict filesystem permissions and protected by LUKS2 disk encryption.

---

## A9 — Audit Log Store

**Type:** Data asset (rotating log files + database table)

**Role:**
The audit log records all security-relevant events on the gateway: login attempts, data access, API access, user management, and security events. It provides a forensic trail for post-incident analysis.

**Rich description:**
- **File logs**: Structured JSON format, rotating (10 MB max, 5 backups) in the gateway log directory.
- **Database table**: `audit_log` table in the SQLite database for queryable access.
- **Events logged**: LOGIN_SUCCESS, LOGIN_FAILURE, LOGIN_LOCKOUT, LOGOUT, DATA_ACCESS, API_ACCESS, REPORT_GENERATED, REPORT_DOWNLOADED, USER_CREATED, USER_DELETED, SECURITY_EVENT.

**Trust boundary:**
TB3 (Pi 5 OS ↔ database/files).

**Why it must be threat-modelled:**
If an attacker can modify or delete audit logs after compromising the system, they can cover their tracks. Log integrity depends on filesystem permissions and LUKS2 encryption. In a production deployment, logs should be forwarded to an external SIEM for tamper-evident storage.
