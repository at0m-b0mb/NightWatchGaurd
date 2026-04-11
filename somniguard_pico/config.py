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
# ---------------------------------------------------------------------------

# Wi‑Fi credentials for the local network shared with the Pi 5 gateway.
# Change these to match your access point.
WIFI_SSID     = "SomniGuard_Net"   # Wi‑Fi network name
WIFI_PASSWORD = "change-me-wifi"   # Wi‑Fi password

# Pi 5 gateway address (LAN IP or hostname).
GATEWAY_HOST = "192.168.1.100"     # Change to the Pi 5's IP address
GATEWAY_PORT = 5000                # Port the Flask gateway listens on

# Patient ID to use when starting a session on the gateway.
# Create the patient record via the web dashboard first, then set this ID.
GATEWAY_PATIENT_ID = 1            # patient.id in the gateway database

# Unique identifier for this Pico device (used in the sessions table).
DEVICE_ID = "pico-01"

# Shared HMAC key — must exactly match PICO_HMAC_KEY in the gateway config.py.
# Generate a strong random key and set it in both places:
#   python3 -c "import secrets; print(secrets.token_hex(32))"
GATEWAY_HMAC_KEY = "dev-hmac-key-change-this-in-production-32chrs!"

# How many full 1 Hz samples to buffer locally before sending to the gateway.
# Increase if the Wi‑Fi connection is unreliable.
TRANSPORT_BATCH_SIZE = 5           # send every 5 seconds worth of 1 Hz readings

# Wi‑Fi connection timeout in seconds.
WIFI_CONNECT_TIMEOUT_S = 30

# Whether to enable the Wi‑Fi transport.  Set to False to run in
# USB‑serial‑only debug mode (no network required).
TRANSPORT_ENABLED = True
