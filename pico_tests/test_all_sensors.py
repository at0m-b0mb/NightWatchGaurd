"""
test_all_sensors.py — SOMNI-Guard Full System Sensor Test
==========================================================
PURPOSE
    Tests all three sensors (MAX30102, ADXL345, Grove GSR) together,
    exactly as the main application uses them.  Runs a 30-second sampling
    loop at 1 Hz and prints a live status table.

    This test imports from the project drivers/ folder.  Run it AFTER the
    individual sensor tests pass.

PREREQUISITES
    The Pico filesystem must contain:
      drivers/__init__.py
      drivers/max30102.py
      drivers/adxl345.py
      drivers/gsr.py
      config.py

    Copy the entire somniguard_pico/ directory to the Pico, OR copy the
    files listed above individually.

HOW TO USE
    1. Ensure all individual sensor tests (test_max30102.py, test_adxl345.py,
       test_gsr.py) pass first.
    2. Copy this file to the Pico root (same level as config.py / drivers/).
    3. Attach finger to MAX30102, GSR electrodes to fingers.
    4. Run: Thonny F5, or: mpremote run test_all_sensors.py

OUTPUT FORMAT
    Each 1 Hz tick prints one line:
      t=3412ms | IR=123456 Red=89012 SpO2=97.2% HR=62bpm [OK] |
               | X=-0.01g Y=0.00g Z=1.00g [OK] |
               | Cond=12.345µS [OK]

Educational prototype — not a clinically approved device.
"""

import time

# ---------------------------------------------------------------------------
# Import project config and drivers
# ---------------------------------------------------------------------------
try:
    import config
except ImportError:
    print("ERROR: config.py not found!")
    print("HINT:  Copy somniguard_pico/config.py to the Pico root first.")
    raise

try:
    from drivers.max30102 import MAX30102
    from drivers.adxl345  import ADXL345
    from drivers.gsr      import GSRSensor
except ImportError as e:
    print("ERROR: Could not import sensor drivers: {}".format(e))
    print("HINT:  Copy the entire drivers/ folder to the Pico root.")
    raise

try:
    from machine import I2C, Pin
except ImportError:
    print("ERROR: machine module not available — run this on the Pico, not PC.")
    raise

# ---------------------------------------------------------------------------
# Test duration
# ---------------------------------------------------------------------------
TEST_DURATION_S = 30   # run for 30 seconds
SAMPLE_DELAY_MS = 1000  # 1 Hz


def _fmt_bool(b):
    return "OK " if b else "ERR"


def run():
    print("\n" + "=" * 70)
    print("  SOMNI-Guard Full Sensor System Test  ({} seconds)".format(TEST_DURATION_S))
    print("=" * 70)
    print("  MAX30102 : I2C0 SDA=GP{} SCL=GP{}  addr=0x{:02X}".format(
        config.MAX30102_I2C_SDA, config.MAX30102_I2C_SCL, config.MAX30102_ADDR))
    print("  ADXL345  : I2C1 SDA=GP{} SCL=GP{}  addr=0x{:02X}".format(
        config.ADXL345_I2C_SDA, config.ADXL345_I2C_SCL, config.ADXL345_ADDR))
    if getattr(config, "GSR_ENABLED", False):
        print("  GSR      : ADC GP{}".format(config.GSR_ADC_PIN))
    else:
        print("  GSR      : DISABLED (GSR_ENABLED=False in config.py)")
    print("=" * 70)

    # ── Initialise I2C buses ───────────────────────────────────────────────
    print("\nInitialising I2C buses...")
    try:
        i2c_max = I2C(config.MAX30102_I2C_ID,
                      sda=Pin(config.MAX30102_I2C_SDA),
                      scl=Pin(config.MAX30102_I2C_SCL),
                      freq=config.I2C_FREQ)
        print("  I2C0 (MAX30102) OK  — devices: {}".format(
            ["0x{:02X}".format(a) for a in i2c_max.scan()]))
    except Exception as e:
        print("  FAIL I2C0: {}".format(e))
        return

    try:
        i2c_accel = I2C(config.ADXL345_I2C_ID,
                        sda=Pin(config.ADXL345_I2C_SDA),
                        scl=Pin(config.ADXL345_I2C_SCL),
                        freq=config.I2C_FREQ)
        print("  I2C1 (ADXL345)  OK  — devices: {}".format(
            ["0x{:02X}".format(a) for a in i2c_accel.scan()]))
    except Exception as e:
        print("  FAIL I2C1: {}".format(e))
        return

    # ── Initialise drivers ────────────────────────────────────────────────
    print("\nInitialising sensor drivers...")
    max30102 = MAX30102(i2c_max, addr=config.MAX30102_ADDR)
    adxl345  = ADXL345(i2c_accel, addr=config.ADXL345_ADDR)
    gsr      = GSRSensor(adc_pin=config.GSR_ADC_PIN) if getattr(
                   config, "GSR_ENABLED", False) else None

    # ── Self-test check ───────────────────────────────────────────────────
    print("\nRunning sensor self-checks...")
    spo2_ok = max30102.check_sensor()
    accel_ok = adxl345.check_sensor()
    gsr_ok = (gsr is not None and gsr._adc is not None)

    print("  MAX30102  : {}".format("PASS" if spo2_ok  else "FAIL — check wiring"))
    print("  ADXL345   : {}".format("PASS" if accel_ok else "FAIL — check wiring"))
    if gsr is not None:
        print("  GSR       : {}".format("PASS" if gsr_ok else "FAIL — check GP26"))
    else:
        print("  GSR       : DISABLED")

    if not spo2_ok and not accel_ok:
        print("\nFATAL: Both I2C sensors failed.  Check wiring before continuing.")
        return

    # ── Live sampling loop ────────────────────────────────────────────────
    print("\n" + "-" * 70)
    print("Live data — place finger on MAX30102, GSR electrodes on other fingers.")
    print("Ctrl-C to stop early.")
    print("-" * 70)

    sample = 0
    start_ms = time.ticks_ms()

    while True:
        elapsed_ms = time.ticks_diff(time.ticks_ms(), start_ms)
        if elapsed_ms >= TEST_DURATION_S * 1000:
            break

        sample += 1

        # SpO2
        spo2_data  = max30102.read_spo2_hr()
        spo2_valid = spo2_data["valid"]
        spo2_val   = spo2_data.get("spo2")
        hr_val     = spo2_data.get("hr")
        ir_val     = spo2_data.get("ir_raw", 0) or 0
        red_val    = spo2_data.get("red_raw", 0) or 0

        # Accelerometer
        accel_data  = adxl345.read_xyz()
        accel_valid = accel_data["valid"]

        # GSR
        gsr_line = ""
        if gsr is not None:
            gsr_data  = gsr.read_conductance()
            gsr_valid = gsr_data["valid"]
            gsr_cond  = gsr_data["conductance_us"]
            gsr_line = " | Cond={:.3f}µS [{}]".format(gsr_cond, _fmt_bool(gsr_valid))

        spo2_str = "{}%".format(spo2_val) if spo2_val else "---  "
        hr_str   = "{}bpm".format(hr_val)  if hr_val  else "--- bpm"

        accel_str = ""
        if accel_valid:
            accel_str = "X={:.3f}g Y={:.3f}g Z={:.3f}g [OK]".format(
                accel_data["x"], accel_data["y"], accel_data["z"])
        else:
            accel_str = "X=--- Y=--- Z=--- [ERR]"

        print("t={:>5}ms | IR={:>6} Red={:>6} SpO2={} HR={} [{}] | {} {}".format(
            elapsed_ms,
            ir_val, red_val,
            spo2_str, hr_str,
            _fmt_bool(spo2_valid),
            accel_str,
            gsr_line))

        time.sleep_ms(SAMPLE_DELAY_MS)

    # ── Summary ────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  Test complete ({} samples over {} s).".format(sample, TEST_DURATION_S))
    print("  If SpO2 shows 'No finger detected' throughout, run test_max30102.py")
    print("  for detailed diagnostics and follow the HINT messages.")
    print("=" * 70)


run()
