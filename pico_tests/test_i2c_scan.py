"""
test_i2c_scan.py — SOMNI-Guard I2C Bus Scanner
===============================================
PURPOSE
    Scans both I2C buses on the Raspberry Pi Pico 2 W and reports every
    device address found.  Run this FIRST to verify wiring before running
    any sensor-specific test.

HOW TO USE
    1. Open Thonny (or use mpremote / rshell).
    2. Copy this file to the Pico filesystem root.
    3. Run it (F5 in Thonny, or: mpremote run test_i2c_scan.py).
    4. Check the output against the expected addresses below.

EXPECTED OUTPUT
    === Scanning I2C Bus 0 (SDA=GP4, SCL=GP5) ===
    Found 1 device(s): [0x57]
      0x57 — MAX30102 SpO2/HR sensor  ✓

    === Scanning I2C Bus 1 (SDA=GP2, SCL=GP3) ===
    Found 1 device(s): [0x53]
      0x53 — ADXL345 accelerometer  ✓

WIRING REFERENCE
    MAX30102  →  Pico 2 W
      VIN       →  3.3V  (pin 36)
      GND       →  GND   (pin 38)
      SDA       →  GP4   (pin 6)
      SCL       →  GP5   (pin 7)
      3-bit pad →  solder to 3V3 position (NOT 1V8)

    ADXL345   →  Pico 2 W
      VCC       →  3.3V  (pin 36)
      GND       →  GND   (pin 38)
      SDA       →  GP2   (pin 4)
      SCL       →  GP3   (pin 5)
      SDO       →  GND   (sets address to 0x53)
      CS        →  3.3V  (enables I2C mode)

Educational prototype — not a clinically approved device.
"""

from machine import I2C, Pin
import time

# ---------------------------------------------------------------------------
# Known I2C addresses in the SOMNI-Guard project
# ---------------------------------------------------------------------------
_KNOWN_DEVICES = {
    0x57: "MAX30102 SpO2/HR sensor",
    0x53: "ADXL345 accelerometer",
    0x48: "ADS1115 external ADC (optional)",
    0x68: "MPU-6050 or DS3231 RTC",
    0x76: "BME280 pressure/temperature",
    0x77: "BMP280 pressure/temperature",
}

# ---------------------------------------------------------------------------
# Bus configurations: (bus_id, sda_pin, scl_pin, expected_address, name)
# ---------------------------------------------------------------------------
_BUSES = [
    (0, 4, 5, 0x57, "MAX30102"),
    (1, 2, 3, 0x53, "ADXL345"),
]


def scan_bus(bus_id, sda_pin, scl_pin, freq=400_000):
    """Scan one I2C bus and return list of found addresses."""
    print("\n=== Scanning I2C Bus {} (SDA=GP{}, SCL=GP{}) ===".format(
        bus_id, sda_pin, scl_pin))
    try:
        i2c = I2C(bus_id, sda=Pin(sda_pin), scl=Pin(scl_pin), freq=freq)
    except Exception as exc:
        print("  ERROR: Could not initialise I2C bus {}: {}".format(bus_id, exc))
        print("  HINT: Check that GP{} and GP{} are not being used elsewhere.".format(
            sda_pin, scl_pin))
        return []

    # Small delay to let pull-ups charge the bus
    time.sleep_ms(10)

    devices = i2c.scan()

    if not devices:
        print("  No devices found!")
        print("  HINT: Check wiring, power (3.3 V), and that the 3-bit pad")
        print("        on the MAX30102 module is soldered to the 3V3 position.")
        return []

    print("  Found {} device(s): [{}]".format(
        len(devices), ", ".join("0x{:02X}".format(d) for d in devices)))

    for addr in devices:
        name = _KNOWN_DEVICES.get(addr, "Unknown device")
        print("    0x{:02X}  —  {}".format(addr, name))

    return devices


def main():
    print("=" * 55)
    print("  SOMNI-Guard I2C Bus Scanner")
    print("=" * 55)

    all_ok = True
    for bus_id, sda, scl, expected, sensor_name in _BUSES:
        found = scan_bus(bus_id, sda, scl)
        if expected in found:
            print("  PASS: {} found at 0x{:02X}".format(sensor_name, expected))
        else:
            print("  FAIL: {} (0x{:02X}) NOT found on Bus {}".format(
                sensor_name, expected, bus_id))
            all_ok = False

    print("\n" + "=" * 55)
    if all_ok:
        print("  ALL sensors detected on their correct buses.")
        print("  You can now run the individual sensor tests.")
    else:
        print("  One or more sensors not detected — check wiring.")
        print("  See WIRING REFERENCE at the top of this file.")
    print("=" * 55)


main()
