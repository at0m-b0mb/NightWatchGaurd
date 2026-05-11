"""
config.py — SOMNI‑Guard Pico 2 W firmware configuration.

All hardware pin assignments, I2C settings, sensor addresses, sampling
rates, and named thresholds live here.  Import this module from every
other module that needs device‑level constants so there is a single
source of truth.

Educational prototype — not a clinically approved device.
"""

# ---------------------------------------------------------------------------
# Hardware pin assignments
# ---------------------------------------------------------------------------

# MAX30102 (SpO₂/HR) — dedicated I2C bus 0
MAX30102_I2C_ID  = 0        # machine.I2C(0, ...) → GP4/GP5 on RP2350
MAX30102_I2C_SDA = 4        # GP4  (physical pin 6)
MAX30102_I2C_SCL = 5        # GP5  (physical pin 7)

# ADXL345 (accelerometer) — dedicated I2C bus 1 (separate pins from MAX30102)
ADXL345_I2C_ID   = 1        # machine.I2C(1, ...) → GP2/GP3 on RP2350
ADXL345_I2C_SDA  = 2        # GP2  (physical pin 4)
ADXL345_I2C_SCL  = 3        # GP3  (physical pin 5)

# I2C bus frequency used by both sensors
I2C_FREQ = 400_000          # 400 kHz fast‑mode

# GSR (galvanic skin response) — direct ADC input on GP26
# The Grove GSR v1.2 sensor module connects its SIG output directly to
# the Pico's built-in ADC on GP26 (ADC0).  No external ADC is needed.
# Set GSR_ENABLED to True only when the Grove GSR sensor is physically
# wired.  When False the GSR driver is never initialised and no ADC reads
# are attempted on GP26, so a floating unconnected pin cannot produce
# spurious conductance values.
GSR_ENABLED = True          # ← set False if Grove GSR v1.2 not wired
GSR_ADC_PIN = 26            # GP26 = ADC0 (physical pin 31)

# Optional: ADS1115 external 16-bit ADC for higher-resolution GSR.
# Uncomment these if upgrading from the Pico's built-in 12-bit ADC to
# an ADS1115 over I2C.  Requires rewriting the GSR driver to use ADS1115.
# ADS1115_I2C_ADDR  = 0x48    # ADDR pin → GND (default address)
# ADS1115_GAIN      = 1       # PGA gain: ±4.096 V full-scale
# ADS1115_DATA_RATE = 4       # 128 SPS (index into DR table: 0=8..7=860)
# GSR_ADS_CHANNEL   = 0       # ADS1115 input: AIN0 (Grove GSR SIG pin)

# Onboard LED (RP2350 uses "LED" as the pin identifier in MicroPython)
LED_PIN = "LED"

# ---------------------------------------------------------------------------
# Sensor I2C addresses
# ---------------------------------------------------------------------------

MAX30102_ADDR = 0x57  # default address for MAX30102 SpO₂/HR module
ADXL345_ADDR  = 0x53  # default SDO‑low address for ADXL345

# ---------------------------------------------------------------------------
# Sampling rates (Hz)
# ---------------------------------------------------------------------------

ACCEL_RATE_HZ  = 10   # ADXL345 — 10 Hz is enough to detect arousals
SPO2_RATE_HZ   = 1    # MAX30102 — 1 Hz for sleep monitoring
GSR_RATE_HZ    = 1    # GSR — 1 Hz

# Derived intervals in milliseconds (used by timer / sleep logic)
ACCEL_INTERVAL_MS = 1000 // ACCEL_RATE_HZ   # 100 ms
SPO2_INTERVAL_MS  = 1000 // SPO2_RATE_HZ    # 1000 ms
GSR_INTERVAL_MS   = 1000 // GSR_RATE_HZ     # 1000 ms

# ---------------------------------------------------------------------------
# GSR configuration
# ---------------------------------------------------------------------------

# Reference resistor in the GSR voltage‑divider (ohms).
# The Grove GSR sensor module has a built-in 10 kΩ reference resistor.
GSR_REF_RESISTOR_OHMS = 10_000   # 10 kΩ

# Number of ADC samples averaged in read_smoothed()
GSR_SMOOTH_WINDOW = 5

# ---------------------------------------------------------------------------
# GSR electrode-contact detection thresholds
# ---------------------------------------------------------------------------
# The Grove GSR v1.2 wired to Pico ADC GP26 produces three distinct
# conductance ranges depending on the physical state of the electrodes.
# These thresholds let the firmware distinguish between:
#
#   State              Observed range   Root cause
#   ─────────────────  ───────────────  ─────────────────────────────────────
#   Sensor NOT wired   > 250 µS         GP26 is floating (no pull to VCC/GND
#                                       through the module's voltage divider).
#                                       The ADC reads an undefined low voltage
#                                       (~0.7 V) which the formula maps to a
#                                       high conductance value.
#   Wearing (correct)   80 – 250 µS     Electrodes are on skin.  Actual skin
#                                       resistance drives the voltage divider
#                                       to a mid-range voltage, producing a
#                                       physiologically plausible µS value.
#   Wired, not on skin  < 80 µS         Electrodes are in the air.  The open
#                                       circuit has very high effective R_skin,
#                                       so V_adc approaches VCC and the formula
#                                       gives a near-zero conductance.
#
# Adjust only if your specific hardware gives different baseline readings
# (use --check mode or the serial log to see raw conductance values first).
GSR_DISCONNECTED_THRESHOLD_US = 250.0  # above this → floating ADC, sensor not wired
GSR_CONTACT_THRESHOLD_US      = 80.0   # below this → open circuit, not touching skin

# ADC reference voltage for RP2350
ADC_VREF = 3.3        # volts

# ADC full‑scale count (16‑bit via read_u16())
ADC_FULL_SCALE = 65535

# ---------------------------------------------------------------------------
# MAX30102 LED amplitude
# ---------------------------------------------------------------------------

# LED pulse amplitude for both Red and IR LEDs on the MAX30102.
# Each register step = 200 µA.  Increase if "No finger detected" persists.
#   0x24 = 7.2 mA  (original — too low for some modules)
#   0x3F = 12.6 mA (conservative sleep‑monitoring value)
#   0x7F = 25.4 mA (recommended default — reliable for most skin tones)
#   0xFF = 51.0 mA (maximum — use only for short diagnostic checks)
MAX30102_LED_AMPLITUDE = 0x7F   # 25.4 mA

# ---------------------------------------------------------------------------
# SpO₂ / HR thresholds (non‑clinical, educational reference values only)
# ---------------------------------------------------------------------------

# Minimum valid raw IR value; below this the finger is likely absent.
# Lowered from 50 000 → 5 000 (v0.4) so that valid low‑signal readings
# (darker skin tones, light finger pressure) are not misclassified.
# An uncovered sensor typically reads < 1 000.
# This is an empirical threshold — NOT a clinical diagnostic limit.
SPO2_IR_MIN_VALID = 5_000

# Nominal SpO₂ bounds for educational plausibility checks.
# Values outside this range are flagged but still forwarded.
SPO2_LOW_WARN  = 90.0  # % — below this is flagged as low (non‑clinical)
SPO2_HIGH_WARN = 100.0 # % — above 100 is physically impossible; clamp

# HR plausibility bounds (beats per minute).
HR_LOW_WARN  = 30    # bpm — very slow; possible in deep sleep but flag it
HR_HIGH_WARN = 200   # bpm — very fast; flag as suspect

# ---------------------------------------------------------------------------
# ADXL345 configuration
# ---------------------------------------------------------------------------

# Data‑rate code for ~10 Hz output (ADXL345 datasheet Table 7: 0x0A = 100 Hz,
# 0x09 = 50 Hz, 0x08 = 25 Hz, 0x07 = 12.5 Hz).
# We use 0x09 (50 Hz) and subsample in software to 10 Hz, giving some
# anti‑aliasing headroom.
ADXL345_DATA_RATE_CODE = 0x09   # 50 Hz hardware output rate

# Sensitivity for ±2g range: 3.9 mg/LSB (from ADXL345 datasheet)
ADXL345_SCALE_G = 0.0039        # g per count

# Movement magnitude threshold for arousal detection (non‑clinical heuristic)
ACCEL_MOVEMENT_THRESHOLD_G = 0.05   # 0.05 g RMS change = minor movement

# ---------------------------------------------------------------------------
# Wi‑Fi and gateway transport settings
# ===========================================================================
# *** REQUIRED BEFORE DEPLOYMENT — CHANGE ALL FOUR VALUES BELOW ***
#
#  1. WIFI_SSID / WIFI_PASSWORD  — your access point credentials
#  2. GATEWAY_HOST               — LAN IP of the Pi 5 (run `hostname -I` on Pi 5)
#  3. GATEWAY_HMAC_KEY           — shared secret; must match SOMNI_HMAC_KEY on
#                                  the gateway (generate with the command below)
#
#  Generate a new HMAC key (run once on any machine with Python 3):
#      python3 -c "import secrets; print(secrets.token_hex(32))"
#  Copy the output into GATEWAY_HMAC_KEY here AND into SOMNI_HMAC_KEY in
#  /etc/somniguard/env on the Pi 5 gateway.
#
#  Deploying without changing these values leaves the device using well-known
#  placeholder credentials that provide NO security.
# ===========================================================================

WIFI_SSID     = "SomniGuard_Net"
WIFI_PASSWORD = "FO51l8VaxxKiGSYz"

# Pi 5 gateway address.
# IMPORTANT: Use the IP address (not somniguard.local) on the Pico.
# MicroPython's mDNS resolution is unreliable; the IP is fixed by the
# hotspot's NetworkManager config (10.42.0.1). The cert SANs include both
# the IP and somniguard.local, so browsers can use either.
# Use the fixed hotspot IP, not somniguard.local — MicroPython's mDNS
# resolver is unreliable. The hotspot always assigns 10.42.0.1 to the Pi.
GATEWAY_HOST = "10.42.0.1"
# Port the Flask gateway listens on.
#   5443 = HTTPS (default — TLS 1.2/1.3, ECDHE+AEAD only)
#   5000 = HTTP  (legacy / debug only — DO NOT use over real Wi-Fi)
GATEWAY_PORT = 5443

# Patient ID to use when starting a session on the gateway.
# Create the patient record via the web dashboard first, then set this ID.
GATEWAY_PATIENT_ID = 1            # patient.id in the gateway database

# Unique identifier for this Pico device (used in the sessions table).
DEVICE_ID = "pico-01"

# ← CHANGE — generate a new key and set it here AND as SOMNI_HMAC_KEY on gateway.
GATEWAY_HMAC_KEY = "e493ce13428190f3b6b8ff4cbd417b4412f62a64972729406c562d0a896ff09f"

# ===========================================================================
# Mutual TLS (mTLS) — Pico ↔ Gateway authenticated transport
# ===========================================================================
# The Pico talks to the Pi 5 gateway over TLS 1.2 / 1.3.  Both sides
# authenticate, and HMAC-SHA256 over the body is layered on top:
#
#   - Server identity (mandatory).  The Pico validates the gateway cert
#     against GATEWAY_CA_CERT_PEM (SOMNI-Guard Root CA).  Any cert not
#     signed by that CA is rejected — defeats rogue APs and on-path
#     attackers.  The Pico's client-side ssl context is CERT_REQUIRED.
#
#   - Pico identity (presented, validated when present).  The Pico sends
#     PICO_CLIENT_CERT_PEM (signed by the same Root CA) during the
#     handshake.  The gateway's server-side ssl context is CERT_OPTIONAL:
#     it cryptographically validates any client cert that is presented,
#     but does NOT require one.  Browsers reach the dashboard with no
#     client cert and authenticate via session + MFA instead.
#
#   - HMAC-SHA256 over the request body (mandatory for every /api/* call).
#     Independent of TLS — defeats a stolen TLS key and an unsigned-but-
#     networked rogue device.  The gateway rejects every /api/* request
#     whose HMAC does not match SOMNI_HMAC_KEY.
#
# Provisioning workflow (run on the Pi 5):
#   1.  python3 scripts/setup_gateway_certs.py
#       Builds: certs/ca.crt, certs/server.{crt,key}, certs/pico_client.{crt,key}
#   2.  python3 scripts/embed_pico_cert.py
#       Rewrites the three PEM blocks below with the freshly generated CA,
#       client cert, and client private key.
#   3.  (Optional, recommended)  python3 scripts/encrypt_pico_files.py
#       Encrypts config.py → config.enc with an AES-256 key derived from
#       the Pico's flash UID, so the client private key is encrypted at
#       rest.  crypto_loader.py loads config.enc on boot.
#   4.  Copy somniguard_pico/* to the Pico (mpremote / Thonny) and reboot.
# ===========================================================================

GATEWAY_USE_TLS = True

# Server name used for SNI during the TLS handshake.
# A DNS hostname (not an IP) is required by RFC 6066.  "somniguard" is
# included in the server cert's SANs so TLS validation succeeds.
# Using an IP address as SNI can cause some TLS stacks to reject the
# ClientHello or fail hostname matching.
GATEWAY_TLS_SNI = "somniguard"

# Root CA — the long-lived trust anchor. Server certs signed by this CA
# are accepted; everything else is rejected. Replaced by embed_pico_cert.py.
GATEWAY_CA_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIBrzCCAVWgAwIBAgIUYqTX4V4E3b7uH47VDooiYO6lTT8wCgYIKoZIzj0EAwIw
NDEcMBoGA1UEAwwTU09NTkktR3VhcmQgUm9vdCBDQTEUMBIGA1UECgwLU09NTkkt
R3VhcmQwHhcNMjYwNTA2MTI0OTQzWhcNMzYwNTAzMTI0OTQzWjA0MRwwGgYDVQQD
DBNTT01OSS1HdWFyZCBSb290IENBMRQwEgYDVQQKDAtTT01OSS1HdWFyZDBZMBMG
ByqGSM49AgEGCCqGSM49AwEHA0IABANVs66RvuCXg1+R7pBiks0qAxP1aKPW+YSU
sXsxXeunismeqZ54WG8mspHGcHpgcL11RWCSeRwQRRprc3U7yw+jRTBDMBIGA1Ud
EwEB/wQIMAYBAf8CAQAwDgYDVR0PAQH/BAQDAgEGMB0GA1UdDgQWBBTlYlXxs2Yp
6wLNKPbvBRgUSa8cfzAKBggqhkjOPQQDAgNIADBFAiAf0ty0QMNm8UzgrC+yvmWQ
o4UpFEpaiGVovQp9ruwS4QIhAIqCnUUhv7ISpxlqjNBhTbF+2dMsiBHpFh4DriN4
aCj4
-----END CERTIFICATE-----
"""

# Pico client cert (CA-signed, clientAuth EKU). Presented during the TLS
# handshake so the gateway can cryptographically identify this device.
PICO_CLIENT_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIB1DCCAXmgAwIBAgIUCnX0o+mKYj2q0ClZYAAixi4r7UIwCgYIKoZIzj0EAwIw
NDEcMBoGA1UEAwwTU09NTkktR3VhcmQgUm9vdCBDQTEUMBIGA1UECgwLU09NTkkt
R3VhcmQwHhcNMjYwNTA2MTI0OTQzWhcNMjcwNTA2MTI0OTQzWjAoMRAwDgYDVQQD
DAdwaWNvLTAxMRQwEgYDVQQKDAtTT01OSS1HdWFyZDBZMBMGByqGSM49AgEGCCqG
SM49AwEHA0IABB5fSZTKEPciSlUS9DkAjrdMydD39ED/zc+iXrtQTQohKSM87gTr
wRU6Eneg6g4VQHTo0wzgiJXu61p7wasg/pKjdTBzMAwGA1UdEwEB/wQCMAAwDgYD
VR0PAQH/BAQDAgOIMBMGA1UdJQQMMAoGCCsGAQUFBwMCMB0GA1UdDgQWBBRqfNdL
DVux+VKgRgYMN+sH12bTvDAfBgNVHSMEGDAWgBTlYlXxs2Yp6wLNKPbvBRgUSa8c
fzAKBggqhkjOPQQDAgNJADBGAiEAraQ3C3H6dvnbjsgzugTrirLB1RKK3YNL+EeB
qw/MWO8CIQCOyXgKcBIpAqXrYxe+Btlb3t1z+MHRNX75jV4z9GGF6A==
-----END CERTIFICATE-----
"""

# Pico client private key (ECDSA P-256, PKCS8 PEM, no passphrase).
# This file should ALWAYS be encrypted at rest via crypto_loader (config.enc).
PICO_CLIENT_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgX2Vz54iQq2zTkz6m
P31f07LBb6824W86vGQTUgYpkuyhRANCAAQeX0mUyhD3IkpVEvQ5AI63TMnQ9/RA
/83Pol67UE0KISkjPO4E68EVOhJ3oOoOFUB06NMM4IiV7utae8GrIP6S
-----END PRIVATE KEY-----
"""

# How many full 1 Hz samples to buffer locally before sending to the gateway.
# Increase if the Wi‑Fi connection is unreliable.
TRANSPORT_BATCH_SIZE = 5           # send every 5 seconds worth of 1 Hz readings

# Wi‑Fi connection timeout in seconds (per attempt).
WIFI_CONNECT_TIMEOUT_S = 30

# Maximum number of Wi‑Fi connection attempts before falling back to
# local-only mode.  Each attempt includes a fresh scan + connect cycle.
WIFI_MAX_RETRIES = 5

# TLS handshake retry settings.  mbedTLS on RP2350 sometimes needs
# a second attempt after gc.collect() frees enough RAM.
TLS_HANDSHAKE_RETRIES = 3
TLS_RETRY_DELAY_S = 2

# Whether to enable the Wi‑Fi transport.  Set to False to run in
# USB‑serial‑only debug mode (no network required).
TRANSPORT_ENABLED = True
