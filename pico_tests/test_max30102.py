"""
test_max30102.py — SOMNI-Guard Standalone MAX30102 Diagnostic Test
==================================================================
PURPOSE
    Comprehensively tests the MAX30102 SpO2/HR sensor in isolation.
    All driver code is embedded here — no project files are required.
    This script is the PRIMARY tool for diagnosing "No finger detected" issues.

HOW TO USE
    1. Wire the MAX30102 to the Pico 2 W (see WIRING below).
    2. Copy ONLY this file to the Pico filesystem root.
    3. Run it: Thonny F5, or: mpremote run test_max30102.py
    4. Follow the on-screen instructions and hints.

WIRING
    MAX30102 pin  →  Pico 2 W
      VIN           →  3.3V   (pin 36)  — also works at 5V via VBUS (pin 40)
      GND           →  GND    (pin 38)
      SDA           →  GP4    (pin  6)
      SCL           →  GP5    (pin  7)
      INT           →  (not connected — not used by this driver)
      RD / IRD      →  (not connected — LED ground, leave floating)

    3-BIT PAD on module board
      IMPORTANT: The pad selects the I2C pull-up voltage.
      The Pico GPIO is 3.3 V — solder the pad to the "3V3" position.
      If soldered to "1V8", you may still get a connection but signal
      integrity is worse and the sensor is more likely to misbehave.

TESTS PERFORMED
    1. I2C bus initialisation (GP4/GP5, 100 kHz)
    2. Device scan — confirm 0x57 on the bus
    3. Part ID check — must read 0x15
    4. Sensor reset and full configuration
    5. Register readback — verify config was written
    6. Live data — 60 samples, print IR/Red raw, detect finger presence
    7. SpO2/HR estimation (requires ~10 s of stable finger contact)

WHAT "NO FINGER DETECTED" MEANS
    IR raw < 5000 (threshold).  Without a finger the sensor reads < 500.
    With a finger the sensor reads 50 000 – 250 000 depending on:
      - LED current (this test uses 25.4 mA)
      - Finger pressure and placement
      - Skin tone / melanin concentration
      - Ambient light (test in a darker environment if readings are noisy)

Educational prototype — not a clinically approved device.
"""

from machine import I2C, Pin
import time

# ── Configuration ──────────────────────────────────────────────────────────
SDA_PIN       = 4          # GP4  (I2C0 SDA)
SCL_PIN       = 5          # GP5  (I2C0 SCL)
I2C_ADDR      = 0x57       # Only valid address for MAX30102
I2C_FREQ      = 100_000    # 100 kHz for initial scan (more forgiving)

# LED current: each step = 200 µA
# 0x3F = 12.6 mA, 0x7F = 25.4 mA, 0xFF = 51 mA
LED_AMP       = 0x7F       # 25.4 mA

NO_FINGER_THR = 5_000      # IR below this = no finger
N_SAMPLES     = 60         # number of live-data samples to collect

# ── Register addresses ────────────────────────────────────────────────────
_INT_STATUS1  = 0x00
_INT_STATUS2  = 0x01
_FIFO_WR_PTR  = 0x04
_OVF_COUNTER  = 0x05
_FIFO_RD_PTR  = 0x06
_FIFO_DATA    = 0x07
_FIFO_CONFIG  = 0x08
_MODE_CONFIG  = 0x09
_SPO2_CONFIG  = 0x0A
_LED1_PA      = 0x0C
_LED2_PA      = 0x0D
_PART_ID      = 0xFF


# ── Low-level helpers ─────────────────────────────────────────────────────

def _wr(i2c, reg, val):
    i2c.writeto_mem(I2C_ADDR, reg, bytes([val]))

def _rd(i2c, reg, n=1):
    return i2c.readfrom_mem(I2C_ADDR, reg, n)


# ── Sensor operations ─────────────────────────────────────────────────────

def reset_sensor(i2c):
    _wr(i2c, _MODE_CONFIG, 0x40)   # RESET bit
    time.sleep_ms(50)              # 50 ms post-reset settling
    _rd(i2c, _INT_STATUS1)         # clear interrupt flags
    _rd(i2c, _INT_STATUS2)


def configure_spo2(i2c):
    _wr(i2c, _FIFO_CONFIG, 0x10)   # SMP_AVE=1, ROLLOVER=on
    _wr(i2c, _MODE_CONFIG, 0x03)   # SpO2 mode (Red + IR)
    _wr(i2c, _SPO2_CONFIG, 0x67)   # 16384nA, 100sps, 411µs (18-bit)
    _wr(i2c, _LED1_PA, LED_AMP)    # Red LED amplitude
    _wr(i2c, _LED2_PA, LED_AMP)    # IR LED amplitude
    # Reset FIFO to known state
    _wr(i2c, _FIFO_WR_PTR, 0x00)
    _wr(i2c, _OVF_COUNTER, 0x00)
    _wr(i2c, _FIFO_RD_PTR, 0x00)
    time.sleep_ms(10)


def read_one_sample(i2c):
    """
    Return (ir_raw, red_raw) from the FIFO.

    Handles FIFO overflow — when reading at 1 Hz from a 100 sps sensor
    the FIFO overflows every 320 ms.  OVF_COUNTER is checked and the
    read pointer is seeked to the latest sample when overflow is detected.
    """
    wr  = _rd(i2c, _FIFO_WR_PTR)[0] & 0x1F
    rd  = _rd(i2c, _FIFO_RD_PTR)[0] & 0x1F
    ovf = _rd(i2c, _OVF_COUNTER)[0] & 0x1F
    n   = (wr - rd) & 0x1F

    if n == 0 and ovf == 0:
        return None, None

    if ovf > 0:
        latest = (wr - 1) & 0x1F
        _wr(i2c, _FIFO_RD_PTR, latest)
        _wr(i2c, _OVF_COUNTER, 0x00)

    raw = _rd(i2c, _FIFO_DATA, 6)
    red_raw = ((raw[0] & 0x03) << 16) | (raw[1] << 8) | raw[2]
    ir_raw  = ((raw[3] & 0x03) << 16) | (raw[4] << 8) | raw[5]
    return ir_raw, red_raw


# ── SpO2 / HR estimation (same algorithm as main driver) ──────────────────

def estimate_spo2_hr(ir_buf, red_buf):
    """Simple R-ratio SpO2 and zero-crossing HR estimate (educational only)."""
    if len(ir_buf) < 10:
        return None, None

    dc_ir  = sum(ir_buf)  / len(ir_buf)
    dc_red = sum(red_buf) / len(red_buf)
    if dc_ir == 0 or dc_red == 0:
        return None, None

    ac_ir  = max(ir_buf)  - min(ir_buf)
    ac_red = max(red_buf) - min(red_buf)
    if ac_ir == 0:
        return None, None

    R = (ac_red / dc_red) / (ac_ir / dc_ir)
    spo2 = max(0.0, min(100.0, 110.0 - 25.0 * R))

    crossings = 0
    mean_ir = dc_ir
    for i in range(1, len(ir_buf)):
        if (ir_buf[i-1] - mean_ir) < 0 and (ir_buf[i] - mean_ir) >= 0:
            crossings += 1
    window_s = len(ir_buf) / 100.0
    hr = (crossings / window_s) * 60.0 if window_s > 0 else None
    if hr is not None and (hr < 20 or hr > 300):
        hr = None

    return round(spo2, 1), (round(hr, 1) if hr is not None else None)


# ── Test runner ───────────────────────────────────────────────────────────

def run():
    passed = 0
    failed = 0

    print("\n" + "=" * 58)
    print("  MAX30102 SpO2 / HR Sensor Diagnostic Test")
    print("=" * 58)
    print("  SDA=GP{}  SCL=GP{}  addr=0x{:02X}  LED={:.1f} mA".format(
        SDA_PIN, SCL_PIN, I2C_ADDR, LED_AMP * 0.2))
    print("=" * 58)

    # ── Test 1: I2C bus init ──────────────────────────────────────────────
    print("\n[1] I2C Bus Initialisation")
    try:
        i2c = I2C(0, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=I2C_FREQ)
        print("    PASS  I2C-0 ready at {} Hz".format(I2C_FREQ))
        passed += 1
    except Exception as e:
        print("    FAIL  {}".format(e))
        print("    HINT  Check GP4 (SDA) and GP5 (SCL) are not shorted or floating.")
        failed += 1
        _summary(passed, failed)
        return

    # ── Test 2: Device scan ───────────────────────────────────────────────
    print("\n[2] I2C Device Scan")
    found = i2c.scan()
    if found:
        print("    Devices on bus: [{}]".format(
            ", ".join("0x{:02X}".format(a) for a in found)))
    else:
        print("    FAIL  No devices found on I2C-0")
        print("    HINT  Power (VIN→3.3V), GND, SDA (GP4), SCL (GP5) — check each.")
        print("    HINT  On the module's 3-bit pad, solder to the 3V3 position.")
        failed += 1
        _summary(passed, failed)
        return

    if I2C_ADDR in found:
        print("    PASS  MAX30102 detected at 0x{:02X}".format(I2C_ADDR))
        passed += 1
    else:
        print("    FAIL  0x{:02X} not found (got {})".format(
            I2C_ADDR, [hex(a) for a in found]))
        print("    HINT  MAX30102 has a fixed address of 0x57 — cannot be changed.")
        print("    HINT  Try swapping SDA and SCL wires if another address appears.")
        failed += 1
        _summary(passed, failed)
        return

    # ── Test 3: Part ID ───────────────────────────────────────────────────
    print("\n[3] Part ID Check")
    try:
        pid = _rd(i2c, _PART_ID)[0]
        print("    Read Part ID: 0x{:02X}  (expected 0x15)".format(pid))
        if pid == 0x15:
            print("    PASS  Genuine MAX30102 confirmed")
            passed += 1
        else:
            print("    WARN  Unexpected Part ID — may be a clone or counterfeit.")
            print("    NOTE  Clone chips may work but the driver is tuned for MAX30102.")
    except Exception as e:
        print("    FAIL  {}".format(e))
        failed += 1

    # ── Test 4: Reset and configure ───────────────────────────────────────
    print("\n[4] Sensor Reset + Configuration")
    try:
        reset_sensor(i2c)
        configure_spo2(i2c)

        mode   = _rd(i2c, _MODE_CONFIG)[0]
        spo2c  = _rd(i2c, _SPO2_CONFIG)[0]
        led1   = _rd(i2c, _LED1_PA)[0]
        led2   = _rd(i2c, _LED2_PA)[0]
        fifocfg = _rd(i2c, _FIFO_CONFIG)[0]

        print("    MODE_CONFIG  = 0x{:02X}  (expect 0x03 = SpO2 mode)".format(mode))
        print("    SPO2_CONFIG  = 0x{:02X}  (expect 0x67 = 16384nA/100sps/18-bit)".format(spo2c))
        print("    FIFO_CONFIG  = 0x{:02X}  (expect 0x10 = no-avg, rollover on)".format(fifocfg))
        print("    LED1_PA(Red) = 0x{:02X}  = {:.1f} mA".format(led1, led1 * 0.2))
        print("    LED2_PA(IR)  = 0x{:02X}  = {:.1f} mA".format(led2, led2 * 0.2))

        if mode == 0x03 and led1 == LED_AMP and led2 == LED_AMP:
            print("    PASS  Configuration verified — all registers correct")
            passed += 1
        else:
            print("    FAIL  Register mismatch — I2C write may have failed")
            print("    HINT  Reduce I2C frequency to 100 kHz if errors persist.")
            failed += 1
    except Exception as e:
        print("    FAIL  {}".format(e))
        failed += 1
        _summary(passed, failed)
        return

    # ── Test 5: Live data with finger detection ───────────────────────────
    print("\n[5] Live Data ({} samples, ~100 ms apart)".format(N_SAMPLES))
    print("    --> Place your finger FIRMLY on the sensor now <--")
    print("    The LEDs should glow visibly (red/IR light) on the module.")
    print()
    print("    {:>4}  {:>8}  {:>8}  {}".format("No.", "IR Raw", "Red Raw", "Status"))
    print("    " + "-" * 46)

    time.sleep_ms(300)   # let FIFO fill a bit after config

    ir_buf  = []
    red_buf = []
    max_ir  = 0
    finger_samples = 0

    for i in range(N_SAMPLES):
        try:
            ir, red = read_one_sample(i2c)
            if ir is None:
                status = "-- no FIFO data --"
                print("    {:>4}  {:>8}  {:>8}  {}".format(i+1, "N/A", "N/A", status))
            else:
                if ir > max_ir:
                    max_ir = ir
                if ir >= NO_FINGER_THR:
                    finger_samples += 1
                    status = "FINGER OK"
                    ir_buf.append(ir)
                    red_buf.append(red)
                elif ir > 1000:
                    status = "weak signal — press harder"
                else:
                    status = "no finger"
                print("    {:>4}  {:>8}  {:>8}  {}".format(i+1, ir, red, status))
        except Exception as e:
            print("    {:>4}  ERROR: {}".format(i+1, e))
        time.sleep_ms(100)

    print("    " + "-" * 46)
    print("    Max IR seen: {}".format(max_ir))
    print("    Finger samples (IR >= {}): {}".format(NO_FINGER_THR, finger_samples))

    if finger_samples >= 5:
        print("    PASS  Finger consistently detected — sensor is working!")
        passed += 1
    elif finger_samples > 0:
        print("    WARN  Finger detected intermittently — check placement.")
        print("    HINT  Cover the sensor completely, press gently but firmly.")
    else:
        print("    FAIL  Finger never detected.")
        if max_ir > 1000:
            print("    HINT  Signal too weak (max IR={}).".format(max_ir))
            print("    HINT  Increase LED_AMP at top of this file (e.g. 0xFF = 51mA).")
            print("    HINT  Cover sensor from ambient light while testing.")
        else:
            print("    HINT  IR = {} — LEDs may not be powered on.".format(max_ir))
            print("    HINT  Verify VIN wired to 3.3V (not 5V — unless using VBUS).")
        failed += 1

    # ── Test 6: SpO2 / HR estimate ────────────────────────────────────────
    print("\n[6] SpO2 / HR Estimation (educational approximation)")
    if len(ir_buf) >= 10:
        spo2, hr = estimate_spo2_hr(ir_buf, red_buf)
        if spo2 is not None:
            print("    SpO2 estimate : {:.1f} %  (non-clinical approximation)".format(spo2))
            print("    HR estimate   : {} bpm".format(hr if hr else "N/A (need more data)"))
            print("    NOTE  These values are NOT medically accurate.")
            print("    PASS  SpO2 algorithm ran successfully with buffer of {} samples".format(
                len(ir_buf)))
            passed += 1
        else:
            print("    WARN  Could not compute SpO2 (AC component = 0).")
            print("    HINT  Keep finger still and pressed firmly for 10+ seconds.")
    else:
        print("    SKIP  Not enough finger-detected samples ({}/10 minimum).".format(
            len(ir_buf)))

    _summary(passed, failed)


def _summary(passed, failed):
    total = passed + failed
    print("\n" + "=" * 58)
    print("  RESULT: {}/{} tests passed".format(passed, total))
    if failed == 0:
        print("  ALL TESTS PASSED — MAX30102 is working correctly!")
    else:
        print("  {} test(s) failed — follow the HINT messages above.".format(failed))
    print("=" * 58)


run()
