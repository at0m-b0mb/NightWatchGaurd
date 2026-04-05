# SOMNI‑Guard Cybersecurity Design Controls

> **Educational prototype — not a clinically approved device.**
> Controls described here represent a defence‑in‑depth architecture
> appropriate for a student project, not a regulated medical device.
> They align with the attack tree (docs/attack_tree.md) and PHA
> (docs/pha.md).

---

## 1. Overview

Controls are organised into three layers:

| Layer | Scope |
|-------|-------|
| **L1 — Device (Pico 2 W)** | Firmware, sensor handling, local fail‑soft behaviour |
| **L2 — Gateway (Pi 5)** | Ingestion, database, OS‑level security |
| **L3 — Dashboard** | Web application, authentication, output integrity |

---

## 2. Device‑Side Controls (Pico 2 W)

### L1‑C1: Fail‑soft sensor handling

**Threat mitigated:** H‑02 (artefact), H‑06 (DoS / gap)

Each I2C and ADC call in every driver is wrapped in `try/except`.  On error
the driver prints a `[SOMNI][<SENSOR>]` warning and returns a result dict
with `valid=False`.  The sampler and main.py never abort on sensor failure.

Key implementation: `drivers/max30102.py`, `drivers/adxl345.py`,
`drivers/gsr.py`, `sampler.py._safe_read()`.

### L1‑C2: Sensor validity flags and plausibility checks

**Threat mitigated:** H‑01, H‑02

- `valid=False` is set whenever raw data is absent or the IR count falls
  below `config.SPO2_IR_MIN_VALID` (no‑finger detection).
- SpO₂ values are clamped to [0, 100] and values outside
  `[config.SPO2_LOW_WARN, config.SPO2_HIGH_WARN]` are flagged in logs.
- HR values outside `[config.HR_LOW_WARN, config.HR_HIGH_WARN]` are
  set to `None`.

### L1‑C3: Top‑level exception catch in main.py

**Threat mitigated:** H‑06 (firmware crash → data gap)

`main.py` wraps the entire flow in a top‑level `try/except`.  Unhandled
exceptions log `[SOMNI][FATAL]` and attempt to restart the sampling loop
or fall back to a blocking poll loop.  A fault LED blink pattern signals
the error state to an observer.

### L1‑C4: Telemetry HMAC (future phase)

**Threat mitigated:** H‑01 (G1.1 replay)

Each telemetry packet will carry an HMAC‑SHA256 tag computed from a
shared secret provisioned at device pairing time.  The gateway ingestion
service verifies the HMAC before writing to the database.  This prevents
replay and injection attacks on the transport channel.

**Implementation note (current phase):** Not yet implemented.
Output is USB‑serial only.  Add in the Wi‑Fi transport phase.

### L1‑C5: Timestamp tagging

**Threat mitigated:** H‑01 (G1.1 replay) — partial

Every reading is tagged with `time.ticks_ms()`.  When HMAC is added, the
timestamp is included in the authenticated payload so replayed packets can
be detected via timestamp staleness checks on the gateway.

### L1-C6: Firmware integrity verification

**Threat mitigated:** H-08 (firmware tampering), H-09 (unauthorized modification)

At boot, `integrity.py` computes SHA-256 hashes of all Python modules on the Pico filesystem and compares them against a signed manifest (`manifest.json`). The manifest itself is signed with HMAC-SHA256 using the shared secret key, preventing an attacker from modifying both the code and the manifest. If any hash mismatch is detected, the device logs a `[SOMNI][INTEGRITY]` warning. In a production deployment, integrity failure should halt execution.

Key implementation: `somniguard_pico/integrity.py`, `scripts/generate_integrity_manifest.py`.

### L1-C7: Secure configuration storage (encrypted secrets on Pico)

**Threat mitigated:** H-04 (credential theft), H-08 (key material exposure)

Sensitive configuration values (HMAC keys, Wi-Fi credentials) are encrypted at rest on the Pico filesystem using XTEA encryption with a key derived from the hardware-unique ID (`machine.unique_id()`). An attacker who obtains the filesystem contents (e.g., via USB access) cannot extract secrets without physical access to the specific Pico device that generated the encryption key.

Key implementation: `somniguard_pico/secure_config.py`.

### L1-C8: Anti-replay with nonce/sequence numbers

**Threat mitigated:** H-01 (G1.1 replay attacks)

Every HMAC-signed telemetry packet includes a monotonically increasing sequence number (`nonce`) and a wall-clock timestamp. The gateway tracks the high-water mark of received nonces per session and rejects any packet with a nonce at or below the mark. Timestamps are validated against a configurable staleness window (default 5 minutes). Together, these fields prevent captured packets from being replayed.

Key implementation: `somniguard_pico/transport.py` (nonce generation), `somniguard_gateway/app.py` (nonce/timestamp validation).

### L1-C9: Hardware watchdog timer

**Threat mitigated:** H-06 (device crash → data gap)

The RP2350 hardware watchdog timer (`machine.WDT`) is configured with an 8-second timeout. The main loop feeds the watchdog on every 1 Hz cycle and during idle. If the firmware hangs or enters an infinite loop, the watchdog resets the device automatically, restoring data collection.

Key implementation: `somniguard_pico/main.py`.

### L1-C10: Encrypted firmware storage

**Threat mitigated:** H-08, H-09 (firmware tampering), H-04 (credential theft via physical access)

All application Python modules on the Pico 2W are encrypted at rest using AES-256-CBC. The encryption key is derived from the device's hardware unique ID (`machine.unique_id()`) combined with a random salt (`_salt.bin`) via SHA-256, binding the encrypted firmware to the specific chip. Only `main.py` (bootstrap) and `crypto_loader.py` (decryption engine) remain as plaintext — neither contains secrets. At boot, `crypto_loader.py` decrypts `.enc` files into memory using MicroPython's built-in `ucryptolib` module and loads them as importable Python modules via `compile()` + `exec()`. Encrypted files copied to a different Pico will fail to decrypt because the hardware UID differs.

Key implementation: `somniguard_pico/crypto_loader.py` (runtime decryption), `scripts/encrypt_pico_files.py` (developer-side encryption tool).

---

## 3. Gateway Controls (Pi 5)

### L2‑C1: Input validation on ingestion

**Threat mitigated:** H‑01 (G1.1, G1.2.4), H‑06 (G1.2.3)

The ingestion service must:
- Reject packets that fail HMAC verification (future).
- Reject packets with timestamps older than a configurable window (e.g., 30 s).
- Apply rate‑limiting (e.g., max 20 packets/s) to mitigate SYN/packet floods.
- Validate packet schema (required fields, value ranges) before inserting into DB.

### L2‑C2: Parameterised SQL queries

**Threat mitigated:** H‑05 (G3.3.1 SQL injection)

All database queries must use parameterised statements (Python `sqlite3`
`?` placeholders or SQLAlchemy bound parameters).  String interpolation
into SQL is prohibited.

### L2‑C3: SQLCipher database encryption

**Threat mitigated:** H‑04 (G2.2 SD removal → data confidentiality)

The SQLite database is encrypted with SQLCipher (AES‑256‑CBC).  The
encryption key is derived from a passphrase stored in a protected keyfile
(`/etc/somniguard/db.key`, mode 0600, owned by the service user).

### L2‑C4: LUKS2 disk encryption

**Threat mitigated:** H‑04 (G2.2 SD removal)

The Pi 5 SD‑card data partition is encrypted with LUKS2 (cryptsetup,
default AES‑XTS‑256).  LUKS2 and SQLCipher are **complementary**:
- LUKS2 protects if the SD card is physically removed.
- SQLCipher protects if the OS is compromised but the DB file is exfiltrated.

### L2‑C5: Report HMAC signing

**Threat mitigated:** H‑03 (G2.1, G2.2), H‑05 (G2.3, G2.4)

Each nightly report is signed with HMAC‑SHA256 using a key stored in the
protected keyfile.  The signature covers the canonical JSON representation
of the report object.  The dashboard verifies the signature before
displaying a report and displays a ⚠️ warning if verification fails.

**Failure mode:** If the signature field is absent the report is treated as
**unverified** (not trusted), mitigating G2.4.1 (delete signature → skip).

### L2‑C6: Least‑privilege service user

**Threat mitigated:** H‑04 (G2.1 privilege escalation)

The ingestion service and Flask app run as a dedicated `somniguard`
system user with no shell, no sudo rights, and read/write access only to
the database directory and report output directory.

### L2-C7: Rate limiting on all endpoints

**Threat mitigated:** H-06 (DoS), H-04 (brute-force)

Flask-Limiter enforces per-IP rate limits: 5 login attempts per minute, 20 API requests per second. Exceeding limits returns HTTP 429. This prevents brute-force password attacks and telemetry flooding.

Key implementation: `somniguard_gateway/security.py`, `somniguard_gateway/app.py`.

### L2-C8: Security headers (HSTS, CSP, X-Frame-Options, etc.)

**Threat mitigated:** H-05 (XSS, clickjacking), H-04 (session hijack)

Every HTTP response includes: Strict-Transport-Security, X-Frame-Options: DENY, X-Content-Type-Options: nosniff, Content-Security-Policy (default-src 'self'), Referrer-Policy, Permissions-Policy, and Cache-Control: no-store for sensitive pages.

Key implementation: `somniguard_gateway/security.py`.

### L2-C9: Audit logging for all access

**Threat mitigated:** H-04, H-05, H-07 (post-incident forensics)

All login attempts (success/failure), data access (patient records, sessions, reports), API access, user management actions, and security events are logged in structured JSON format. Logs are written to rotating files (10 MB, 5 backups) and to the audit_log database table.

Key implementation: `somniguard_gateway/audit.py`, `somniguard_gateway/database.py`.

### L2-C10: TLS/HTTPS for all web traffic

**Threat mitigated:** H-04 (session hijack), H-05 (eavesdropping)

The gateway generates self-signed TLS certificates and serves all dashboard traffic over HTTPS when SOMNI_HTTPS=true. HSTS headers prevent protocol downgrade attacks.

Key implementation: `somniguard_gateway/tls_setup.py`, `somniguard_gateway/run.py`.

### L2-C11: Account lockout after failed attempts

**Threat mitigated:** H-04 (G3.1.1 brute-force)

After 10 consecutive failed login attempts from the same IP, the account is locked for 15 minutes. Lockout status is tracked in-memory and logged to the audit trail.

Key implementation: `somniguard_gateway/security.py`.

### L2-C12: Session timeout and secure cookies

**Threat mitigated:** H-04 (session hijack)

Flask sessions expire after 30 minutes of inactivity. Session cookies are configured with HttpOnly, SameSite=Lax, and Secure (when HTTPS is active) flags.

Key implementation: `somniguard_gateway/app.py`.

---

## 4. Dashboard Controls (Flask App)

### L3‑C1: Authentication with bcrypt and rate‑limiting

**Threat mitigated:** H‑04 (G3.1.1 brute‑force)

- Passwords stored as bcrypt hashes (cost factor ≥ 12).
- Login attempts rate‑limited to 5 per minute per IP (Flask‑Limiter).
- Account lock‑out after 10 consecutive failures (temporary, 15 min).

### L3‑C2: CSRF tokens on all state‑changing forms

**Threat mitigated:** H‑05 (G3.2.2 CSRF)

Flask‑WTF or a custom CSRF middleware generates a per‑session token
embedded in every form.  The token is verified server‑side on POST/DELETE.

### L3‑C3: Output escaping and Content‑Security‑Policy

**Threat mitigated:** H‑05 (G3.2.1 XSS)

- Jinja2 auto‑escaping is enabled for all templates (default in Flask).
- Custom template variables are passed through `|e` filter where auto‑
  escaping might be disabled.
- `Content-Security-Policy: default-src 'self'; script-src 'self'` header
  blocks inline scripts and external script sources.

### L3‑C4: Bind to localhost only

**Threat mitigated:** H‑04 (network exposure)

The Flask application binds to `127.0.0.1:5000` (or a Unix socket).
It is never exposed on `0.0.0.0` or any LAN interface.  Remote access, if
needed, requires an SSH tunnel.

### L3‑C5: HTTPS for any LAN access (future)

**Threat mitigated:** H‑04 (session hijack G1.2.4)

If the dashboard is ever exposed beyond localhost, it must be served over
HTTPS with a self‑signed certificate (or Let's Encrypt on the LAN).
HTTP→HTTPS redirect and `Strict-Transport-Security` header enforced.

### L3-C6: Password complexity requirements

**Threat mitigated:** H-04 (weak passwords)

New user passwords must contain at least 8 characters, including uppercase, lowercase, digit, and special character. Complexity is enforced server-side in the user creation form handler.

Key implementation: `somniguard_gateway/security.py`.

---

## 5. Control ↔ Attack Tree / PHA Alignment

| Control | Attack path(s) mitigated | Hazard(s) addressed |
|---------|--------------------------|---------------------|
| L1‑C1 Fail‑soft sensors | — | H‑02, H‑06 |
| L1‑C2 Validity flags | G1.3.1 | H‑01, H‑02 |
| L1‑C3 Top‑level catch | — | H‑06 |
| L1‑C4 Telemetry HMAC (future) | G1.1 | H‑01 |
| L1‑C5 Timestamp tagging | G1.1 (partial) | H‑01 |
| L2‑C1 Input validation | G1.1, G1.2.3, G1.2.4 | H‑01, H‑06 |
| L2‑C2 Parameterised SQL | G3.3.1 | H‑05 |
| L2‑C3 SQLCipher | G2.2 | H‑04 |
| L2‑C4 LUKS2 | G2.2 | H‑04 |
| L2‑C5 Report signing | G2.1, G2.2, G2.3, G2.4 | H‑03, H‑05 |
| L2‑C6 Least‑privilege | G2.1.1 | H‑04 |
| L3‑C1 bcrypt + rate‑limit | G3.1.1 | H‑04 |
| L3‑C2 CSRF tokens | G3.2.2 | H‑05 |
| L3‑C3 CSP + auto‑escape | G3.2.1 | H‑05 |
| L3‑C4 Localhost bind | network exposure | H‑04 |
| L3‑C5 HTTPS (future) | G1.2.4 | H‑04 |

---

## 6. Explicit Clarification: LUKS2 vs SQLCipher

A common question in medical‑device security is whether disk encryption
(LUKS2) alone is sufficient, or whether application‑level encryption
(SQLCipher) is also needed.  SOMNI‑Guard uses **both** because they protect
against different threat scenarios:

| Scenario | LUKS2 | SQLCipher |
|----------|-------|-----------|
| SD card physically removed from powered‑off Pi 5 | ✅ Protects | ✅ Protects |
| OS‑level compromise (root shell); disk already unlocked at boot | ❌ Does not protect (disk is mounted) | ✅ Still protects (DB key needed) |
| DB file copied via application exploit | ❌ Does not protect | ✅ Protects |
| Key stored insecurely (same file as DB) | — | ❌ Does not protect |

**Conclusion:** LUKS2 is the primary physical‑theft control.  SQLCipher is
the defence‑in‑depth control against OS‑level compromise.  The SQLCipher
key must be stored separately from the database (e.g., in a TPM‑backed
keystore or a protected `/etc/somniguard/db.key` with strict permissions).

---

## 7. Tailscale VPN Controls (Layer 0 — Network Perimeter)

These controls operate below the application layer.  They do not replace
application-level authentication but add a strong network perimeter that
limits which devices can reach the SOMNI-Guard web service.

### L0-C1: Tailscale peer authentication (WireGuard mTLS)

**Threat mitigated:** H-07 (G4.1, G4.2), H-04 (network exposure)

Every device on the SOMNI-Guard tailnet is mutually authenticated by the
Tailscale control plane using device certificates.  WireGuard tunnels use
ChaCha20-Poly1305 authenticated encryption.  An attacker who does not hold
a valid Tailscale node key cannot participate in the tailnet at all.

**Implementation:** `tailscaled` systemd service on the Pi 5;
`tailscale.is_tailscale_ip()` in `tailscale.py` classifies remote IPs.

### L0-C2: TAILSCALE_ONLY network policy on the gateway

**Threat mitigated:** H-07 (G4.2), H-04 (network exposure)

When `SOMNI_TAILSCALE_ONLY=true`, the Flask `before_request` hook
(`app._enforce_network_policy`) rejects any HTTP request whose source IP is
not in the Tailscale CGNAT range (100.64.0.0/10) or loopback.  Pico API
endpoints (`/api/*`) additionally accept private LAN IPs (RFC 1918) because
the Pico 2W cannot run Tailscale.

**Implementation:** `app.py:_enforce_network_policy()`;
`tailscale.check_network_policy()`; `config.TAILSCALE_ONLY` and
`config.PICO_ALLOWED_CIDRS`.

### L0-C3: Tailscale ACL tag policy

**Threat mitigated:** H-07 (G4.2 — excess access within tailnet)

A tag-based Tailscale ACL restricts which tailnet nodes may reach port 5000
on the Pi 5 gateway.  Only devices tagged `somni-clinician` or `somni-dev`
are permitted; new devices enrolled in the account but not yet tagged cannot
reach the dashboard even if they hold a valid tailnet key.

**Implementation:** ACL JSON in the Tailscale admin console (see
`docs/tailscale_setup.md §7`).

### L0-C4: Tailscale account 2FA and device-key expiry

**Threat mitigated:** H-07 (G4.1 — credential theft)

Enabling two-factor authentication on the Tailscale account prevents an
attacker from enrolling a rogue device even if the account password is
compromised.  Device-key expiry (configurable in Tailscale settings) ensures
that stale or lost devices are automatically de-authorised without manual
intervention.

**Implementation:** Tailscale admin console security settings.

### L0-C5: Pico → Pi 5 HMAC authentication (complement to Tailscale)

**Threat mitigated:** TB2 attacks (G1.1 replay, G1.2 Wi-Fi attacks)

The Pico 2W cannot join the Tailscale tailnet.  Its telemetry is authenticated
by HMAC-SHA256 on every packet using a shared key stored in
`somniguard_pico/config.py` (GATEWAY_HMAC_KEY) and `/etc/somniguard/env`
(SOMNI_HMAC_KEY).  This provides packet-level integrity for the local LAN
hop (TB2) that Tailscale covers for the Pi 5 ↔ laptop hop (TB5).

**Implementation:** `somniguard_pico/transport.py:_hmac_sha256()`;
`somniguard_gateway/app.py:_verify_hmac()`.

### L0-C6: Pi 5 Secure Boot

**Threat mitigated:** H-08, H-09 (boot chain tampering)

UEFI Secure Boot on the Pi 5 ensures only signed kernels and bootloaders execute. The signing keys are stored in `/etc/somniguard/secure-boot/keys/` with restricted permissions.

Key implementation: `scripts/setup_secure_boot_pi5.sh`.

---

## 8. Control ↔ Attack Tree / PHA Alignment (updated)

| Control | Attack path(s) mitigated | Hazard(s) addressed |
|---------|--------------------------|---------------------|
| L0-C1 Tailscale WireGuard mTLS | G4.1 (partial), G4.3 (partial) | H-07 |
| L0-C2 TAILSCALE_ONLY policy | G4.2, network exposure | H-04, H-07 |
| L0-C3 Tailscale ACL tags | G4.2 | H-07 |
| L0-C4 2FA + device-key expiry | G4.1 | H-07 |
| L0-C5 Pico HMAC auth | G1.1, G1.2 | H-01, H-06 |
| L0-C6 Pi 5 Secure Boot | boot chain tampering | H-08, H-09 |
| L1-C1 Fail-soft sensors | — | H-02, H-06 |
| L1-C2 Validity flags | G1.3.1 | H-01, H-02 |
| L1-C3 Top-level catch | — | H-06 |
| L1-C4 Telemetry HMAC | G1.1 | H-01 |
| L1-C5 Timestamp tagging | G1.1 (partial) | H-01 |
| L1-C6 Firmware integrity verification | firmware tampering | H-08, H-09 |
| L1-C7 Secure configuration storage | credential/key theft | H-04, H-08 |
| L1-C8 Anti-replay nonce/sequence | G1.1 replay | H-01 |
| L1-C9 Hardware watchdog timer | — | H-06 |
| L1-C10 Encrypted firmware storage | physical flash extraction, firmware cloning | H-04, H-08, H-09 |
| L2-C1 Input validation | G1.1, G1.2.3, G1.2.4 | H-01, H-06 |
| L2-C2 Parameterised SQL | G3.3.1 | H-05 |
| L2-C3 SQLCipher | G2.2 | H-04 |
| L2-C4 LUKS2 | G2.2, G4.4 (partial) | H-04 |
| L2-C5 Report signing | G2.1, G2.2, G2.3, G2.4 | H-03, H-05 |
| L2-C6 Least-privilege | G2.1.1, G4.4 (partial) | H-04 |
| L2-C7 Rate limiting | DoS, brute-force | H-04, H-06 |
| L2-C8 Security headers | XSS, clickjacking, session hijack | H-04, H-05 |
| L2-C9 Audit logging | post-incident forensics | H-04, H-05, H-07 |
| L2-C10 TLS/HTTPS | session hijack, eavesdropping | H-04, H-05 |
| L2-C11 Account lockout | G3.1.1 brute-force | H-04 |
| L2-C12 Session timeout + secure cookies | session hijack | H-04 |
| L3-C1 bcrypt + rate-limit | G3.1.1 | H-04 |
| L3-C2 CSRF tokens | G3.2.2 | H-05 |
| L3-C3 CSP + auto-escape | G3.2.1 | H-05 |
| L3-C4 Tailscale-only bind | G4.2, network exposure | H-04, H-07 |
| L3-C5 HTTPS (future) | G1.2.4 | H-04 |
| L3-C6 Password complexity | weak passwords | H-04 |
