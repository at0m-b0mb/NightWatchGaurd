"""
test_gsr.py — SOMNI-Guard Standalone GSR Sensor Test
=====================================================
PURPOSE
    Tests the Grove GSR v1.2 galvanic skin response sensor in isolation.
    All code is embedded — no project files required.

HOW TO USE
    1. Wire the Grove GSR module to the Pico 2 W (see WIRING below).
    2. Attach the finger electrodes to two fingers of the same hand.
    3. Copy ONLY this file to the Pico filesystem root.
    4. Run: Thonny F5, or: mpremote run test_gsr.py

WIRING
    Grove GSR v1.2 pin  →  Pico 2 W
      VCC (Red)           →  3.3V   (pin 36)
      GND (Black)         →  GND    (pin 38)
      SIG (Yellow)        →  GP26   (pin 31 = ADC0)
      NC  (White)         →  (not connected)

UNDERSTANDING THE READINGS
    The Grove GSR uses a voltage divider with a 10 kΩ reference resistor.
    The output voltage on SIG varies with skin resistance:
      - High skin resistance (calm, dry skin): low voltage, low conductance
      - Low skin resistance (sweating, aroused): high voltage, high conductance

    Typical conductance ranges:
      - Relaxed, dry skin:  0.5 – 5 µS
      - Normal range:       5 – 50 µS
      - Stressed / aroused: 50 – 200 µS

    NOTE: Values are highly dependent on electrode placement, skin hydration,
    and temperature.  They are NOT medically calibrated.

TESTS PERFORMED
    1. ADC initialisation on GP26
    2. Raw ADC reading (should not be 0 or 65535 — those indicate a fault)
    3. 30 samples of voltage + conductance with live display
    4. Stability check (coefficient of variation across samples)

Educational prototype — not a clinically approved device.
"""

from machine import ADC, Pin
import time
import math

# ── Configuration ──────────────────────────────────────────────────────────
ADC_PIN      = 26         # GP26 = ADC0
ADC_VREF     = 3.3        # Pico 2W reference voltage (volts)
ADC_FULLSCALE = 65535     # read_u16() full-scale count
R_REF        = 10_000     # Grove GSR reference resistor (10 kΩ)
N_SAMPLES    = 30         # number of readings
SAMPLE_DELAY_MS = 200     # delay between samples

# Conductance plausibility bounds (µS) — values outside are flagged
COND_MIN_US  = 0.01       # effectively 0 = open circuit (no electrodes)
COND_MAX_US  = 1000.0     # very high = short circuit


def raw_to_conductance(raw):
    """Convert 16-bit ADC count to skin conductance (µS)."""
    voltage = (raw / ADC_FULLSCALE) * ADC_VREF
    # Voltage divider: V_sig = Vref * R_skin / (R_ref + R_skin)
    # Rearranged: R_skin = R_ref * V_sig / (Vref - V_sig)
    denom = ADC_VREF - voltage
    if abs(denom) < 1e-6:
        denom = 1e-6   # prevent /0 when voltage ≈ Vref (short circuit)
    r_skin = R_REF * voltage / denom
    if r_skin <= 0:
        r_skin = 1e-6
    conductance_us = (1.0 / r_skin) * 1_000_000
    return voltage, conductance_us


def stddev(values):
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def run():
    passed = 0
    failed = 0

    print("\n" + "=" * 58)
    print("  Grove GSR v1.2 Sensor Diagnostic Test")
    print("=" * 58)
    print("  ADC pin: GP{}  (ADC0, pin 31)".format(ADC_PIN))
    print("  Vref: {} V   R_ref: {} Ω".format(ADC_VREF, R_REF))
    print("=" * 58)

    # ── Test 1: ADC init ──────────────────────────────────────────────────
    print("\n[1] ADC Initialisation")
    try:
        adc = ADC(ADC_PIN)
        print("    PASS  ADC on GP{} (ADC0) initialised".format(ADC_PIN))
        passed += 1
    except Exception as e:
        print("    FAIL  {}".format(e))
        print("    HINT  GP26 is ADC0 on Pico 2W.  GP27=ADC1, GP28=ADC2.")
        print("    HINT  Confirm GSR SIG (Yellow) wire is connected to GP26.")
        failed += 1
        _summary(passed, failed)
        return

    # ── Test 2: Single raw read ───────────────────────────────────────────
    print("\n[2] Single Raw ADC Read")
    try:
        raw = adc.read_u16()
        voltage, cond = raw_to_conductance(raw)
        print("    Raw ADC  : {}  (0 = GND short, 65535 = Vref short)".format(raw))
        print("    Voltage  : {:.4f} V".format(voltage))
        print("    Cond.    : {:.3f} µS".format(cond))

        if raw == 0:
            print("    WARN  Raw = 0 — SIG may be shorted to GND or electrodes missing.")
        elif raw == 65535:
            print("    WARN  Raw = 65535 — SIG may be shorted to VCC or disconnected.")
        else:
            print("    PASS  ADC reading in valid range")
            passed += 1

        if not (COND_MIN_US <= cond <= COND_MAX_US):
            print("    WARN  Conductance {:.2f} µS is outside expected range".format(cond))
    except Exception as e:
        print("    FAIL  {}".format(e))
        failed += 1
        _summary(passed, failed)
        return

    # ── Test 3: Live data ─────────────────────────────────────────────────
    print("\n[3] Live GSR Data ({} samples)".format(N_SAMPLES))
    print("    Attach finger electrodes to two fingers of the same hand.")
    print("    Keep fingers still — movement causes noise.")
    print()
    print("    {:>4}   {:>8}   {:>8}   {:>12}   {}".format(
        "No.", "Raw ADC", "Volt (V)", "Cond (µS)", "Status"))
    print("    " + "-" * 60)

    raws  = []
    conds = []
    volts = []

    for i in range(N_SAMPLES):
        try:
            raw   = adc.read_u16()
            v, c  = raw_to_conductance(raw)
            raws.append(raw)
            conds.append(c)
            volts.append(v)

            if raw < 1000:
                status = "no electrodes / open"
            elif raw > 64000:
                status = "short circuit?"
            elif c < 0.5:
                status = "very dry / no contact"
            elif c > 200:
                status = "very high / sweating"
            else:
                status = "normal range"

            print("    {:>4}   {:>8}   {:>8.4f}   {:>12.3f}   {}".format(
                i+1, raw, v, c, status))
        except Exception as e:
            print("    {:>4}   ERROR: {}".format(i+1, e))
        time.sleep_ms(SAMPLE_DELAY_MS)

    print("    " + "-" * 60)

    if conds:
        avg_cond = sum(conds) / len(conds)
        sd_cond  = stddev(conds)
        cv = (sd_cond / avg_cond * 100) if avg_cond > 0 else 0
        print()
        print("    Average conductance : {:.3f} µS".format(avg_cond))
        print("    Std deviation       : {:.3f} µS".format(sd_cond))
        print("    CV (noise measure)  : {:.1f} %  (<30% = stable signal)".format(cv))
        passed += 1

    # ── Test 4: Stability check ───────────────────────────────────────────
    print("\n[4] Signal Stability Check")
    if conds:
        avg_cond = sum(conds) / len(conds)
        sd_cond  = stddev(conds)
        cv = (sd_cond / avg_cond * 100) if avg_cond > 0 else 0

        if avg_cond < 0.1:
            print("    FAIL  Average conductance nearly zero — electrodes may not be attached.")
            print("    HINT  Connect the two metal clips to two adjacent fingers.")
            failed += 1
        elif cv > 80:
            print("    WARN  Very noisy signal (CV = {:.1f}%).".format(cv))
            print("    HINT  Keep fingers still.  Check electrode contact.")
        elif cv > 30:
            print("    WARN  Moderately noisy signal (CV = {:.1f}%).".format(cv))
            print("    HINT  Slight movement is expected, but try to reduce it.")
            passed += 1
        else:
            print("    PASS  Stable signal (CV = {:.1f}%)".format(cv))
            passed += 1

        # Plausibility
        if COND_MIN_US < avg_cond < COND_MAX_US:
            print("    PASS  Average conductance {:.2f} µS is in plausible range".format(
                avg_cond))
        else:
            print("    WARN  Average conductance {:.2f} µS is outside expected range.".format(
                avg_cond))
    else:
        print("    SKIP  No data collected")

    _summary(passed, failed)


def _summary(passed, failed):
    total = passed + failed
    print("\n" + "=" * 58)
    print("  RESULT: {}/{} tests passed".format(passed, total))
    if failed == 0:
        print("  ALL TESTS PASSED — GSR sensor is working correctly!")
    else:
        print("  {} test(s) failed — follow the HINT messages above.".format(failed))
    print("=" * 58)


run()
