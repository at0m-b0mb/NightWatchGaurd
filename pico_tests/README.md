# SOMNI-Guard Pico Sensor Test Suite

> **Educational prototype — not a clinically approved device.**

This folder contains five standalone MicroPython diagnostic scripts for the
Raspberry Pi Pico 2 W.  Run them in the order shown below to verify that
every sensor is wired and working before starting the main application.

---

## Test Files at a Glance

| File | Purpose | Standalone? | Run Order |
|------|---------|-------------|-----------|
| [test_i2c_scan.py](test_i2c_scan.py) | Scan both I2C buses, confirm addresses | Yes | **1st** |
| [test_max30102.py](test_max30102.py) | Full MAX30102 SpO2/HR diagnostic | Yes | 2nd |
| [test_adxl345.py](test_adxl345.py) | Full ADXL345 accelerometer diagnostic | Yes | 3rd |
| [test_gsr.py](test_gsr.py) | Grove GSR v1.2 conductance test | Yes | 4th |
| [test_all_sensors.py](test_all_sensors.py) | All sensors together (1 Hz loop, 30 s) | No* | **Last** |

> *`test_all_sensors.py` requires the project's `config.py` and `drivers/`
> folder to also be on the Pico filesystem.

---

## Prerequisites

### Hardware needed

- Raspberry Pi Pico 2 W (RP2350, with Wi-Fi)
- MAX30102 SpO2/HR module (I2C)
- ADXL345 accelerometer module (I2C)
- Grove GSR v1.2 sensor module (ADC)
- Jumper wires and breadboard
- USB cable (Micro-USB) to connect Pico to PC

### Software needed

- [Thonny IDE](https://thonny.org/) (easiest for beginners)
  — OR — `mpremote` command-line tool (`pip install mpremote`)

---

## Wiring Reference

### MAX30102 SpO2 / HR Sensor

| MAX30102 Pin | Pico 2 W Pin | Notes |
|-------------|-------------|-------|
| VIN | 3.3V (pin 36) | 1.8 V – 5 V range; 3.3 V recommended |
| GND | GND (pin 38) | |
| SDA | GP4 (pin 6) | I2C bus 0 |
| SCL | GP5 (pin 7) | I2C bus 0 |
| INT | — | Not used by this driver |
| RD / IRD | — | LED ground contacts; leave unconnected |

> **3-bit pull-up pad:** On the module PCB there is a 3-bit solder pad that
> selects the I2C pull-up voltage.  **Solder the bridge to the 3V3 position**
> (not 1V8).  The Pico GPIO operates at 3.3 V; using the 1.8 V pull-up
> can cause intermittent communication failures.

### ADXL345 Accelerometer

| ADXL345 Pin | Pico 2 W Pin | Notes |
|------------|-------------|-------|
| VCC | 3.3V (pin 36) | |
| GND | GND (pin 38) | |
| SDA | GP2 (pin 4) | I2C bus 1 |
| SCL | GP3 (pin 5) | I2C bus 1 |
| SDO | GND (pin 38) | Sets I2C address to 0x53 |
| CS | 3.3V (pin 36) | **Must be HIGH** to enable I2C mode |
| INT1/INT2 | — | Not used |

> **CS must be tied HIGH (3.3 V).**  If CS is left floating or pulled LOW,
> the ADXL345 enters SPI mode and will not respond on I2C.

### Grove GSR v1.2

| Grove Cable | Pico 2 W Pin | Notes |
|------------|-------------|-------|
| Red (VCC) | 3.3V (pin 36) | |
| Black (GND) | GND (pin 38) | |
| Yellow (SIG) | GP26 (pin 31) | ADC0 analog input |
| White (NC) | — | Not connected |

---

## How to Run Tests (Thonny)

1. Connect the Pico to your PC via USB.
2. Open **Thonny** → select interpreter **MicroPython (Raspberry Pi Pico)**.
3. In the **Files** panel, navigate to `NightWatchGaurd-main/pico_tests/`.
4. Right-click the desired test file → **Upload to /** (copies to Pico root).
5. Double-click the file in the Pico panel to open it.
6. Press **F5** (or the green Run button) to execute.
7. Read the output in the Shell panel.

## How to Run Tests (mpremote CLI)

```bash
# From the pico_tests/ directory:
mpremote run test_i2c_scan.py
mpremote run test_max30102.py
mpremote run test_adxl345.py
mpremote run test_gsr.py

# For test_all_sensors.py, first upload config.py and drivers/:
mpremote cp ../somniguard_pico/config.py :config.py
mpremote cp -r ../somniguard_pico/drivers :drivers
mpremote run test_all_sensors.py
```

---

## Step-by-Step Troubleshooting Guide

### "No devices found" on I2C scan

1. Check power: VIN/VCC → 3.3 V, GND → GND.
2. Check SDA/SCL wires are not swapped.
3. Check that the I2C bus number matches the pins:
   - Bus 0: SDA=GP4, SCL=GP5
   - Bus 1: SDA=GP2, SCL=GP3
4. For MAX30102: solder the 3-bit pad to 3V3 (not 1V8).
5. For ADXL345: ensure CS → 3.3 V (enabling I2C mode).

### MAX30102 "No finger detected"

This was caused by a **FIFO overflow bug** in driver v0.3 (fixed in v0.4):

**Root cause:** The MAX30102 FIFO samples at 100 sps internally.  The main
application reads at 1 Hz.  By the time `read_fifo()` was called, the FIFO
had overflowed — the write pointer wrapped back to equal the read pointer,
which the old code interpreted as "no data available."

**The fix (already applied in `drivers/max30102.py` v0.4):**
- Reads the `OVF_COUNTER` register.
- When overflow is detected, seeks the read pointer to the latest sample
  (`wr_ptr − 1`) before reading.
- Clears `OVF_COUNTER` after recovery.

**If you still see "No finger detected" after the fix:**

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| IR raw = 0 | LEDs not powered | Check VIN; verify register write |
| IR raw < 1 000 | Very low LED current | Increase `LED_AMP` in test file |
| IR raw 1 000–5 000 | Weak signal | Press finger more firmly; clean sensor window |
| IR raw > 5 000 | Working! | The threshold was previously 50 000 (lowered to 5 000 in v0.4) |
| Works in dark, fails in light | Ambient light interference | Shield sensor from direct light |

To test different LED currents, change `LED_AMP` in `test_max30102.py`:
```python
LED_AMP = 0x7F   # 25.4 mA (default — try this first)
LED_AMP = 0x3F   # 12.6 mA (lower power)
LED_AMP = 0xFF   # 51.0 mA (maximum — for difficult cases)
```

### ADXL345 "CS pin must be HIGH"

The ADXL345 has both SPI and I2C interfaces.  The CS pin selects the mode:
- CS = HIGH (3.3 V) → I2C mode
- CS = LOW (GND) or floating → SPI mode (ADXL345 will not respond on I2C)

Always wire **CS → 3.3 V** in this project.

### GSR shows 0 conductance or extreme values

- **Raw ≈ 0:** SIG wire is shorted to GND, or electrodes are missing.
- **Raw ≈ 65535:** SIG wire is shorted to VCC, or the circuit is open-circuit.
- **Very low conductance (< 0.5 µS):** Electrodes are not properly contacting skin.
- **Very high conductance (> 500 µS):** May be correct if sweating heavily, or
  electrodes are positioned on a damp/wet area.

---

## What the tests verify

### test_i2c_scan.py
- I2C bus 0 initialises (GP4/GP5)
- I2C bus 1 initialises (GP2/GP3)
- MAX30102 (0x57) found on bus 0
- ADXL345 (0x53) found on bus 1

### test_max30102.py
1. I2C bus init (100 kHz for reliability)
2. Device scan confirms 0x57
3. Part ID = 0x15 (genuine MAX30102)
4. Reset and full configuration
5. Register readback (MODE, SPO2_CONFIG, LED amplitudes)
6. 60 live samples with IR/Red raw values and finger detection
7. Educational SpO2/HR estimation (requires ≥ 10 finger-present samples)

### test_adxl345.py
1. I2C bus 1 init (400 kHz)
2. Device scan confirms 0x53
3. Device ID = 0xE5
4. Configuration (50 Hz ODR, ±2g, measurement mode)
5. Register readback
6. 30 live samples with X/Y/Z in g-units
7. Gravity sanity: Z axis ≈ ±1 g when flat (tolerance ±0.3 g)

### test_gsr.py
1. ADC init on GP26
2. Single raw read (check for 0 / 65535 faults)
3. 30 live samples: raw ADC, voltage, conductance (µS)
4. Signal stability: coefficient of variation < 30% = good

### test_all_sensors.py
- I2C buses and all drivers initialise using project config
- 30 × 1 Hz samples with all three sensors simultaneously
- Each line shows SpO2/HR, accelerometer X/Y/Z, and GSR conductance

---

## Known Issues and Fixes Applied

### v0.4 Bug Fix — MAX30102 FIFO Overflow

**File:** `somniguard_pico/drivers/max30102.py`

| Change | Before (v0.3) | After (v0.4) |
|--------|--------------|-------------|
| FIFO overflow handling | `num_samples == 0` → return None, None | Also check `OVF_COUNTER`; seek to latest sample on overflow |
| LED current | 0x24 = 7.2 mA | 0x7F = 25.4 mA |
| No-finger threshold | 50 000 | 5 000 |
| Post-reset delay | 10 ms | 50 ms |

**File:** `somniguard_pico/config.py`

| Constant | Before | After |
|---------|--------|-------|
| `SPO2_IR_MIN_VALID` | 50 000 | 5 000 |
| `MAX30102_LED_AMPLITUDE` | (not present) | 0x7F (25.4 mA) |
