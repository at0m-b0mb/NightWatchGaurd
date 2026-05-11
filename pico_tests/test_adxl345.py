"""
test_adxl345.py — SOMNI-Guard Standalone ADXL345 Accelerometer Test
====================================================================
PURPOSE
    Tests the ADXL345 3-axis accelerometer in isolation.
    All driver code is embedded — no project files required.

HOW TO USE
    1. Wire the ADXL345 to the Pico 2 W (see WIRING below).
    2. Copy ONLY this file to the Pico filesystem root.
    3. Run: Thonny F5, or: mpremote run test_adxl345.py

WIRING
    ADXL345 pin  →  Pico 2 W
      VCC          →  3.3V   (pin 36)
      GND          →  GND    (pin 38)
      SDA          →  GP2    (pin  4)
      SCL          →  GP3    (pin  5)
      SDO          →  GND    (sets I2C address to 0x53)
      CS           →  3.3V   (required to enable I2C mode; floating = SPI mode)
      INT1/INT2    →  (not connected)

    IMPORTANT: If CS is left floating or connected to GND, the chip enters
    SPI mode and will NOT respond on I2C.  Always tie CS → 3.3V.

TESTS PERFORMED
    1. I2C bus initialisation (GP2/GP3)
    2. Device scan — confirm 0x53 on the bus
    3. Device ID check — must read 0xE5
    4. Sensor configuration (±2g, 50 Hz ODR, measurement mode)
    5. Register readback — verify configuration
    6. Live data — 30 samples at ~10 Hz with g-values
    7. Gravity sanity check — Z axis should be ~1g when sensor is flat

Educational prototype — not a clinically approved device.
"""

from machine import I2C, Pin
import time
import math

# ── Configuration ──────────────────────────────────────────────────────────
SDA_PIN   = 2         # GP2  (I2C1 SDA)
SCL_PIN   = 3         # GP3  (I2C1 SCL)
I2C_ADDR  = 0x53      # SDO tied LOW  (0x1D if SDO tied HIGH)
I2C_FREQ  = 400_000
N_SAMPLES = 30        # number of live-data samples

# ── Register addresses ────────────────────────────────────────────────────
_REG_DEVID       = 0x00
_REG_BW_RATE     = 0x2C
_REG_POWER_CTL   = 0x2D
_REG_DATA_FORMAT = 0x31
_REG_DATAX0      = 0x32   # reads 6 bytes: X_LSB, X_MSB, Y_LSB, Y_MSB, Z_LSB, Z_MSB

_DEVID_EXPECTED  = 0xE5
_SCALE_G         = 0.0039  # g per LSB at ±2g range

# Gravity tolerance for sanity check: Z should be 1g ± this value when flat
_GRAVITY_TOLERANCE_G = 0.3


def _wr(i2c, reg, val):
    i2c.writeto_mem(I2C_ADDR, reg, bytes([val]))

def _rd(i2c, reg, n=1):
    return i2c.readfrom_mem(I2C_ADDR, reg, n)


def configure_sensor(i2c):
    _wr(i2c, _REG_BW_RATE,     0x09)  # 50 Hz output data rate
    _wr(i2c, _REG_DATA_FORMAT, 0x00)  # ±2g range, 10-bit, right-justified
    _wr(i2c, _REG_POWER_CTL,   0x08)  # measurement mode (bit 3 = 1)
    time.sleep_ms(10)


def read_xyz_raw(i2c):
    """Read 6 bytes from DATAX0 and return (x, y, z) as signed 16-bit ints."""
    data = _rd(i2c, _REG_DATAX0, 6)
    x = (data[1] << 8) | data[0]
    y = (data[3] << 8) | data[2]
    z = (data[5] << 8) | data[4]
    # Two's complement
    if x > 32767: x -= 65536
    if y > 32767: y -= 65536
    if z > 32767: z -= 65536
    return x, y, z


def read_xyz_g(i2c):
    x, y, z = read_xyz_raw(i2c)
    return x * _SCALE_G, y * _SCALE_G, z * _SCALE_G


def run():
    passed = 0
    failed = 0

    print("\n" + "=" * 58)
    print("  ADXL345 Accelerometer Diagnostic Test")
    print("=" * 58)
    print("  SDA=GP{}  SCL=GP{}  addr=0x{:02X}".format(SDA_PIN, SCL_PIN, I2C_ADDR))
    print("=" * 58)

    # ── Test 1: I2C init ──────────────────────────────────────────────────
    print("\n[1] I2C Bus Initialisation (Bus 1)")
    try:
        i2c = I2C(1, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=I2C_FREQ)
        print("    PASS  I2C-1 ready at {} Hz".format(I2C_FREQ))
        passed += 1
    except Exception as e:
        print("    FAIL  {}".format(e))
        print("    HINT  GP2 (SDA) and GP3 (SCL) must not be used elsewhere.")
        failed += 1
        _summary(passed, failed)
        return

    # ── Test 2: Device scan ───────────────────────────────────────────────
    print("\n[2] I2C Device Scan")
    found = i2c.scan()
    if found:
        print("    Devices: [{}]".format(", ".join("0x{:02X}".format(a) for a in found)))
    else:
        print("    FAIL  No devices found on I2C-1")
        print("    HINT  VCC→3.3V, GND→GND, SDA→GP2, SCL→GP3")
        print("    HINT  CS pin MUST be tied to 3.3V (enables I2C mode).")
        print("    HINT  SDO pin ties LOW for 0x53, HIGH for 0x1D.")
        failed += 1
        _summary(passed, failed)
        return

    if I2C_ADDR in found:
        print("    PASS  ADXL345 found at 0x{:02X}".format(I2C_ADDR))
        passed += 1
    else:
        # Try alternate address
        if 0x1D in found:
            print("    NOTE  Device found at 0x1D (SDO is HIGH — expected LOW/GND).")
            print("    HINT  Tie the SDO pin to GND for address 0x53.")
        print("    FAIL  0x{:02X} not on bus".format(I2C_ADDR))
        failed += 1
        _summary(passed, failed)
        return

    # ── Test 3: Device ID ─────────────────────────────────────────────────
    print("\n[3] Device ID Check")
    try:
        devid = _rd(i2c, _REG_DEVID)[0]
        print("    Read DEVID: 0x{:02X}  (expected 0x{:02X})".format(
            devid, _DEVID_EXPECTED))
        if devid == _DEVID_EXPECTED:
            print("    PASS  Confirmed genuine ADXL345")
            passed += 1
        else:
            print("    WARN  Unexpected DEVID — may be a clone.")
    except Exception as e:
        print("    FAIL  {}".format(e))
        failed += 1

    # ── Test 4: Configure ─────────────────────────────────────────────────
    print("\n[4] Sensor Configuration")
    try:
        configure_sensor(i2c)
        bw_rate    = _rd(i2c, _REG_BW_RATE)[0]
        data_fmt   = _rd(i2c, _REG_DATA_FORMAT)[0]
        power_ctl  = _rd(i2c, _REG_POWER_CTL)[0]
        print("    BW_RATE     = 0x{:02X}  (expect 0x09 = 50 Hz ODR)".format(bw_rate))
        print("    DATA_FORMAT = 0x{:02X}  (expect 0x00 = ±2g range)".format(data_fmt))
        print("    POWER_CTL   = 0x{:02X}  (expect 0x08 = measure mode)".format(power_ctl))
        if bw_rate == 0x09 and power_ctl == 0x08:
            print("    PASS  Configuration verified")
            passed += 1
        else:
            print("    FAIL  Register mismatch")
            failed += 1
    except Exception as e:
        print("    FAIL  {}".format(e))
        failed += 1
        _summary(passed, failed)
        return

    # ── Test 5: Live data ─────────────────────────────────────────────────
    print("\n[5] Live Accelerometer Data ({} samples @ ~10 Hz)".format(N_SAMPLES))
    print("    Place sensor FLAT on a table for the gravity sanity check.")
    print()
    print("    {:>4}   {:>8}  {:>8}  {:>8}   {:>8}".format(
        "No.", "X (g)", "Y (g)", "Z (g)", "Mag (g)"))
    print("    " + "-" * 52)

    z_readings = []
    mag_readings = []
    read_errors = 0

    for i in range(N_SAMPLES):
        try:
            gx, gy, gz = read_xyz_g(i2c)
            mag = math.sqrt(gx*gx + gy*gy + gz*gz)
            z_readings.append(gz)
            mag_readings.append(mag)
            print("    {:>4}   {:>8.4f}  {:>8.4f}  {:>8.4f}   {:>8.4f}".format(
                i+1, gx, gy, gz, mag))
        except Exception as e:
            print("    {:>4}   ERROR: {}".format(i+1, e))
            read_errors += 1
        time.sleep_ms(100)

    print("    " + "-" * 52)

    if read_errors > N_SAMPLES // 2:
        print("    FAIL  Too many read errors ({}/{})".format(read_errors, N_SAMPLES))
        failed += 1
    else:
        passed += 1
        print("    PASS  Data read ({} errors)".format(read_errors))

    # ── Test 6: Gravity sanity check ──────────────────────────────────────
    print("\n[6] Gravity Sanity Check (sensor should be flat on table)")
    if z_readings:
        avg_z   = sum(z_readings) / len(z_readings)
        avg_mag = sum(mag_readings) / len(mag_readings)
        print("    Average Z       : {:.4f} g  (expect ~1.0 g when flat)".format(avg_z))
        print("    Average Mag     : {:.4f} g  (expect ~1.0 g regardless of orientation)".format(avg_mag))

        z_ok   = abs(abs(avg_z)   - 1.0) < _GRAVITY_TOLERANCE_G
        mag_ok = abs(avg_mag      - 1.0) < _GRAVITY_TOLERANCE_G

        if z_ok:
            print("    PASS  Z-axis gravity reading is within tolerance")
            passed += 1
        else:
            print("    WARN  Z-axis {} g — sensor may be tilted or misconfigured.".format(
                round(avg_z, 3)))

        if mag_ok:
            print("    PASS  Total acceleration magnitude is ~1g (good)")
        else:
            print("    WARN  Magnitude {} g — check for vibration or miscalibration.".format(
                round(avg_mag, 3)))
    else:
        print("    SKIP  No readings available")

    _summary(passed, failed)


def _summary(passed, failed):
    total = passed + failed
    print("\n" + "=" * 58)
    print("  RESULT: {}/{} tests passed".format(passed, total))
    if failed == 0:
        print("  ALL TESTS PASSED — ADXL345 is working correctly!")
    else:
        print("  {} test(s) failed — follow the HINT messages above.".format(failed))
    print("=" * 58)


run()
