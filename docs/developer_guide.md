# SOMNI‑Guard Developer Guide

> **Educational prototype — not a clinically approved device.**
> This document describes every code file in the SOMNI‑Guard repository,
> listing module‑level variables, every function with its inputs and expected
> outputs, and the cross‑file interactions between them.
> Work is divided equally among four team members.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Team Work Assignment](#2-team-work-assignment)
3. [Pico Firmware — `somniguard_pico/`](#3-pico-firmware--somniguard_pico)
   - 3.1 [config.py](#31-configpy)
   - 3.2 [utils.py](#32-utilspy)
   - 3.3 [drivers/\_\_init\_\_.py](#33-driversinipy)
   - 3.4 [drivers/max30102.py](#34-driversmax30102py)
   - 3.5 [drivers/adxl345.py](#35-driversadxl345py)
   - 3.6 [drivers/gsr.py](#36-driversgsrpy)
   - 3.7 [sampler.py](#37-samplerpy)
   - 3.8 [transport.py](#38-transportpy)
   - 3.9 [main.py](#39-mainpy)
4. [Gateway — `somniguard_gateway/`](#4-gateway--somniguard_gateway)
   - 4.1 [config.py](#41-configpy)
   - 4.2 [database.py](#42-databasepy)
   - 4.3 [reports.py](#43-reportspy)
   - 4.4 [tailscale.py](#44-tailscalepy)
   - 4.5 [app.py](#45-apppy)
   - 4.6 [run.py](#46-runpy)
5. [Cross‑File Interaction Map](#5-crossfile-interaction-map)

---

## 1. System Overview

SOMNI‑Guard consists of two physical nodes:

| Node | Hardware | Codebase |
|------|----------|----------|
| **Pico Node** | Raspberry Pi Pico 2 W (MicroPython) | `somniguard_pico/` |
| **Gateway** | Raspberry Pi 5 (CPython / Flask) | `somniguard_gateway/` |

The Pico reads three sensors (SpO₂/HR via MAX30102, acceleration via ADXL345,
galvanic skin response via an ADC voltage‑divider) and streams HMAC‑SHA256‑signed
telemetry to the Gateway over the local Wi‑Fi LAN.  The Gateway stores data in
SQLite, generates PDF sleep reports, and serves a secure web dashboard over a
Tailscale VPN overlay.

---

## 2. Team Work Assignment

Work is divided into four equal modules. Each person owns end‑to‑end
responsibility for their files: design, implementation, unit tests, and
code review.

| Person | Module | Files |
|--------|--------|-------|
| **Kailash** | Sensor Hardware Drivers | `somniguard_pico/drivers/__init__.py`, `drivers/max30102.py`, `drivers/adxl345.py`, `drivers/gsr.py` |
| **Krishita** | Pico Configuration & Utilities | `somniguard_pico/config.py`, `somniguard_pico/utils.py`, `somniguard_pico/sampler.py` |
| **Rob** | Pico Transport & Gateway Web App | `somniguard_pico/transport.py`, `somniguard_pico/main.py`, `somniguard_gateway/app.py`, `somniguard_gateway/run.py` |
| **Ronit** | Gateway Data & Network Layer | `somniguard_gateway/config.py`, `somniguard_gateway/database.py`, `somniguard_gateway/reports.py`, `somniguard_gateway/tailscale.py` |

### Responsibility boundaries

```
Kailash          Krishita          Rob                   Ronit
──────────       ─────────         ─────────────────     ──────────────────────
Sensor drivers   Pico config       Pico main + transport  Gateway config
MAX30102         Pico utils        Flask routes           SQLite database
ADXL345          SensorSampler     run.py entry point     PDF reports
GSRSensor                                                 Tailscale helpers
```

### Integration points (shared interfaces)

| Interface | Producer | Consumer |
|-----------|----------|----------|
| `MAX30102.read_spo2_hr()` → dict | Kailash | Krishita (SensorSampler) |
| `ADXL345.read_xyz()` → dict | Kailash | Krishita (SensorSampler) |
| `GSRSensor.read_conductance()` → dict | Kailash | Krishita (SensorSampler) |
| `SensorSampler` callback dict | Krishita | Rob (main.py `_on_sensor_data`) |
| `transport.send_api()` HTTP POST | Rob | Ronit (Flask `/api/ingest`) |
| `db.*()` helpers | Ronit | Rob (app.py routes) |
| `reports.compute_summary()` + `generate_pdf()` | Ronit | Rob (app.py `generate_report`) |
| `tailscale.check_network_policy()` | Ronit | Rob (app.py `before_request`) |

---

## 3. Pico Firmware — `somniguard_pico/`

### 3.1 `config.py`

**Owner: Krishita**

Module with no functions — it is a flat collection of constants imported by
every other Pico module.  Changing a value here changes it everywhere.

#### Module‑level variables

| Variable | Type | Default value | Purpose |
|----------|------|---------------|---------|
| `I2C_ID` | `int` | `0` | `machine.I2C` bus index (GP4/GP5) |
| `I2C_SDA` | `int` | `4` | SDA GPIO pin number |
| `I2C_SCL` | `int` | `5` | SCL GPIO pin number |
| `I2C_FREQ` | `int` | `400_000` | I2C clock frequency in Hz |
| `GSR_ADC_PIN` | `int` | `26` | ADC GPIO pin for GSR sensor |
| `LED_PIN` | `str` | `"LED"` | MicroPython LED identifier on RP2350 |
| `MAX30102_ADDR` | `int` | `0x57` | I2C address of MAX30102 |
| `ADXL345_ADDR` | `int` | `0x53` | I2C address of ADXL345 (SDO‑low) |
| `ACCEL_RATE_HZ` | `int` | `10` | Target accelerometer sample rate |
| `SPO2_RATE_HZ` | `int` | `1` | Target SpO₂/HR sample rate |
| `GSR_RATE_HZ` | `int` | `1` | Target GSR sample rate |
| `ACCEL_INTERVAL_MS` | `int` | `100` | Milliseconds per accelerometer tick |
| `SPO2_INTERVAL_MS` | `int` | `1000` | Milliseconds per SpO₂ tick |
| `GSR_INTERVAL_MS` | `int` | `1000` | Milliseconds per GSR tick |
| `GSR_REF_RESISTOR_OHMS` | `int` | `10_000` | Reference resistor in voltage‑divider (Ω) |
| `GSR_SMOOTH_WINDOW` | `int` | `5` | Samples averaged in `read_smoothed()` |
| `ADC_VREF` | `float` | `3.3` | ADC reference voltage (V) |
| `ADC_FULL_SCALE` | `int` | `65535` | 16‑bit ADC full‑scale count |
| `SPO2_IR_MIN_VALID` | `int` | `50_000` | IR threshold below which no finger is assumed |
| `SPO2_LOW_WARN` | `float` | `90.0` | Lower SpO₂ plausibility bound (%) |
| `SPO2_HIGH_WARN` | `float` | `100.0` | Upper SpO₂ plausibility bound (%) |
| `HR_LOW_WARN` | `int` | `30` | Lower HR plausibility bound (bpm) |
| `HR_HIGH_WARN` | `int` | `200` | Upper HR plausibility bound (bpm) |
| `ADXL345_DATA_RATE_CODE` | `int` | `0x09` | ADXL345 BW_RATE register value (50 Hz ODR) |
| `ADXL345_SCALE_G` | `float` | `0.0039` | ADXL345 sensitivity (g per raw count) |
| `ACCEL_MOVEMENT_THRESHOLD_G` | `float` | `0.05` | Minimum Δ\|accel\| that counts as movement |
| `WIFI_SSID` | `str` | `"SomniGuard_Net"` | Wi‑Fi network name |
| `WIFI_PASSWORD` | `str` | `"change-me-wifi"` | Wi‑Fi password (must be changed in production) |
| `GATEWAY_HOST` | `str` | `"192.168.1.100"` | Pi 5 gateway LAN IP |
| `GATEWAY_PORT` | `int` | `5000` | Flask gateway TCP port |
| `GATEWAY_PATIENT_ID` | `int` | `1` | Patient ID in the gateway database |
| `DEVICE_ID` | `str` | `"pico-01"` | Unique identifier for this Pico device |
| `GATEWAY_HMAC_KEY` | `str` | `"dev-hmac-key-…"` | Shared HMAC secret (must be changed in production) |
| `TRANSPORT_BATCH_SIZE` | `int` | `5` | Readings buffered before each network send |
| `WIFI_CONNECT_TIMEOUT_S` | `int` | `30` | Wi‑Fi connection timeout in seconds |
| `TRANSPORT_ENABLED` | `bool` | `True` | Whether Wi‑Fi transport is active |

#### Interactions

`config` is imported (not called) by: `sampler.py`, `transport.py`, `main.py`,
`utils.py`, `drivers/gsr.py`.  It has no functions and makes no calls.

---

### 3.2 `utils.py`

**Owner: Krishita**

Shared utilities: a fixed‑size ring buffer, a timestamp helper, and a
human‑readable sensor‑data formatter.

#### Module‑level variables

| Variable | Purpose |
|----------|---------|
| `time.ticks_ms` (monkey‑patched on CPython) | Fallback shim providing millisecond uptime when running under standard Python |

---

#### Class `RingBuffer`

A fixed‑size circular (FIFO) buffer backed by a plain list.

**Constructor**

| Parameter | Type | Description |
|-----------|------|-------------|
| `size` | `int` | Maximum number of items the buffer can hold (must be ≥ 1) |

**Raises** `ValueError` if `size < 1`.

---

**`RingBuffer.push(item)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `item` | any | Value to store; oldest item is silently overwritten when full |

Returns: `None`

---

**`RingBuffer.get_all()`**

Returns all valid items in chronological order (oldest first).

| Input | — |
|-------|---|
| Returns | `list` — items from oldest to newest; empty list if no items yet |

---

**`RingBuffer.get_latest(n)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `n` | `int` | Number of most‑recent items to return (clamped to buffer length) |

Returns: `list` — up to `n` most‑recent items, oldest first within the slice.

---

**`RingBuffer.is_full()`**

Returns: `bool` — `True` when the buffer has reached its capacity.

---

**`RingBuffer.__len__()`**

Returns: `int` — current number of valid items.

---

#### Function `get_timestamp()`

| Input | — |
|-------|---|
| Returns | `int` — milliseconds since boot (wrapping, MicroPython `time.ticks_ms()`) |

Used by: `sampler.py` (in timer callback and `read_all()`).

---

#### Function `format_reading(sensor_data)`

| Parameter | Type | Description |
|-----------|------|-------------|
| `sensor_data` | `dict` | Output of `SensorSampler.read_all()` — keys: `timestamp_ms`, `spo2`, `accel`, `gsr` |

Returns: `str` — compact single‑line string, e.g.:

```
t=12345ms SpO2=98.2% HR=62.0bpm accel=(0.01,-0.02,1.00)g GSR=12.3uS
```

Used by: `main.py` (`_on_sensor_data` callback) to print the `[SOMNI][DATA]` line.

---

### 3.3 `drivers/__init__.py`

**Owner: Kailash**

Package init that re‑exports all three driver classes so callers can write:

```python
from drivers import MAX30102, ADXL345, GSRSensor
```

#### Module‑level variable

| Variable | Value |
|----------|-------|
| `__all__` | `["MAX30102", "ADXL345", "GSRSensor"]` |

No functions.  Imports from `max30102`, `adxl345`, and `gsr` sub‑modules.

---

### 3.4 `drivers/max30102.py`

**Owner: Kailash**

MicroPython driver for the MAX30102 SpO₂/HR sensor (I2C, 7‑bit address 0x57).

#### Module‑level constants (private)

| Constant | Value | Purpose |
|----------|-------|---------|
| `_REG_INT_STATUS1` | `0x00` | Interrupt status 1 register |
| `_REG_INT_STATUS2` | `0x01` | Interrupt status 2 register |
| `_REG_INT_ENABLE1` | `0x02` | Interrupt enable 1 register |
| `_REG_INT_ENABLE2` | `0x03` | Interrupt enable 2 register |
| `_REG_FIFO_WR_PTR` | `0x04` | FIFO write‑pointer register |
| `_REG_OVF_COUNTER` | `0x05` | FIFO overflow counter register |
| `_REG_FIFO_RD_PTR` | `0x06` | FIFO read‑pointer register |
| `_REG_FIFO_DATA` | `0x07` | FIFO data output register |
| `_REG_FIFO_CONFIG` | `0x08` | FIFO configuration register |
| `_REG_MODE_CONFIG` | `0x09` | Mode configuration register |
| `_REG_SPO2_CONFIG` | `0x0A` | SpO₂ configuration register |
| `_REG_LED1_PA` | `0x0C` | Red LED pulse amplitude register |
| `_REG_LED2_PA` | `0x0D` | IR LED pulse amplitude register |
| `_REG_PART_ID` | `0xFF` | Part ID register (expected value: 0x15) |
| `_PART_ID_EXPECTED` | `0x15` | Expected MAX30102 part ID |
| `_BYTES_PER_SAMPLE` | `6` | FIFO bytes per sample in SpO₂ mode |
| `_IR_NO_FINGER_THRESHOLD` | `50_000` | IR raw count below which no finger is assumed |

#### Class `MAX30102`

**Constructor `__init__(i2c, addr=0x57)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `i2c` | `machine.I2C` | Configured I2C bus (400 kHz, GP4/GP5) |
| `addr` | `int` | 7‑bit I2C address of the sensor; defaults to `0x57` |

Returns: `None`.  Calls `_configure()` internally.

Instance variables set by constructor:

| Variable | Type | Description |
|----------|------|-------------|
| `_i2c` | `machine.I2C` | I2C bus reference |
| `_addr` | `int` | Sensor I2C address |
| `_ir_buffer` | `list[int]` | Rolling IR raw samples for HR estimation |
| `_red_buffer` | `list[int]` | Rolling Red raw samples for SpO₂ estimation |
| `_buffer_len` | `int` | Maximum buffer length (100 samples) |
| `_configured` | `bool` | `True` if `_configure()` succeeded |

---

**`MAX30102._write_reg(reg, value)` (private)**

| Parameter | Type | Description |
|-----------|------|-------------|
| `reg` | `int` | Register address |
| `value` | `int` | Byte value to write |

Returns: `bool` — `True` on success, `False` on I2C error.

---

**`MAX30102._read_reg(reg, n=1)` (private)**

| Parameter | Type | Description |
|-----------|------|-------------|
| `reg` | `int` | Register address |
| `n` | `int` | Number of bytes to read (default 1) |

Returns: `bytes | None` — raw bytes on success, `None` on I2C error.

---

**`MAX30102._configure()` (private)**

Resets the device, then writes FIFO, mode, SpO₂ configuration, and LED‑current
registers to set up SpO₂ mode at 100 sps internal rate with ~7 mA LED current.

| Input | — |
|-------|---|
| Returns | `None` |

Side‑effect: sets `self._configured = True` on success.

---

**`MAX30102.check_sensor()`**

Reads the PART ID register and compares to `0x15`.

| Input | — |
|-------|---|
| Returns | `bool` — `True` if sensor is present and identified correctly |

Called by: `SensorSampler.check_all_sensors()`.

---

**`MAX30102.read_fifo()`**

Reads one sample from the FIFO (6 bytes: 3 Red + 3 IR, 18‑bit each).

| Input | — |
|-------|---|
| Returns | `tuple(int, int)` — `(ir_raw, red_raw)` on success; `(None, None)` on error or empty FIFO |

Called by: `MAX30102.read_spo2_hr()`.

---

**`MAX30102.read_spo2_hr()`**

Reads one FIFO sample, accumulates a rolling buffer, and computes a
simplified educational SpO₂ (R‑ratio method) and HR (zero‑crossing count).

| Input | — |
|-------|---|
| Returns | `dict` with keys: |

```python
{
    "spo2"    : float | None,   # estimated SpO₂ % [0.0–100.0], rounded to 1 dp
    "hr"      : float | None,   # estimated HR bpm [20–300], rounded to 1 dp
    "ir_raw"  : int   | None,   # raw 18-bit IR ADC count
    "red_raw" : int   | None,   # raw 18-bit Red ADC count
    "valid"   : bool            # True only when SpO₂/HR values are computed
}
```

Called by: `SensorSampler.read_all()` and `SensorSampler.start_sampling_loop()`
(via `_safe_read`).

---

### 3.5 `drivers/adxl345.py`

**Owner: Kailash**

MicroPython driver for the ADXL345 3‑axis MEMS accelerometer (I2C, address 0x53).

#### Module‑level constants (private)

| Constant | Value | Purpose |
|----------|-------|---------|
| `_REG_DEVID` | `0x00` | Device ID register (reads 0xE5) |
| `_REG_BW_RATE` | `0x2C` | Data‑rate and power mode control |
| `_REG_POWER_CTL` | `0x2D` | Power‑saving features control |
| `_REG_DATA_FORMAT` | `0x31` | Data format control (range, resolution) |
| `_REG_DATAX0` | `0x32` | X‑axis data LSB register |
| `_REG_DATAX1` | `0x33` | X‑axis data MSB register |
| `_REG_DATAY0` | `0x34` | Y‑axis data LSB register |
| `_REG_DATAY1` | `0x35` | Y‑axis data MSB register |
| `_REG_DATAZ0` | `0x36` | Z‑axis data LSB register |
| `_REG_DATAZ1` | `0x37` | Z‑axis data MSB register |
| `_DEVID_EXPECTED` | `0xE5` | Expected device ID per ADXL345 datasheet |
| `_SCALE_G` | `0.0039` | Sensitivity: 3.9 mg per raw count (±2g range) |

#### Class `ADXL345`

**Constructor `__init__(i2c, addr=0x53)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `i2c` | `machine.I2C` | Configured I2C bus |
| `addr` | `int` | Sensor I2C address (SDO‑low default: `0x53`) |

Returns: `None`.  Calls `_configure()` internally.

Instance variables:

| Variable | Type | Description |
|----------|------|-------------|
| `_i2c` | `machine.I2C` | I2C bus reference |
| `_addr` | `int` | Sensor address |
| `_configured` | `bool` | `True` if `_configure()` succeeded |

---

**`ADXL345._write_reg(reg, value)` (private)**

| Parameter | Type | Description |
|-----------|------|-------------|
| `reg` | `int` | Register address |
| `value` | `int` | Byte value to write |

Returns: `bool` — `True` on success, `False` on I2C error.

---

**`ADXL345._read_reg(reg, n=1)` (private)**

| Parameter | Type | Description |
|-----------|------|-------------|
| `reg` | `int` | Register address |
| `n` | `int` | Bytes to read (default 1) |

Returns: `bytes | None` — raw bytes on success, `None` on I2C error.

---

**`ADXL345._configure()` (private)**

Writes BW_RATE (50 Hz ODR), DATA_FORMAT (±2g range), and POWER_CTL
(measurement mode) registers.

| Input | — |
|-------|---|
| Returns | `None` |

Side‑effect: sets `self._configured = True` on full success.

---

**`ADXL345.check_sensor()`**

Reads DEVID register and compares to `0xE5`.

| Input | — |
|-------|---|
| Returns | `bool` — `True` if device is present and identified |

Called by: `SensorSampler.check_all_sensors()`.

---

**`ADXL345.read_raw()`**

Reads all six DATAX/Y/Z registers in one burst and returns signed 16‑bit counts.

| Input | — |
|-------|---|
| Returns | `tuple(int, int, int)` — `(x_raw, y_raw, z_raw)` on success; `(None, None, None)` on error |

Called by: `ADXL345.read_xyz()`.

---

**`ADXL345.read_xyz()`**

Calls `read_raw()` and converts raw counts to g‑units using `_SCALE_G = 0.0039 g/count`.

| Input | — |
|-------|---|
| Returns | `dict` with keys: |

```python
{
    "x"    : float | None,  # X-axis acceleration in g (4 dp)
    "y"    : float | None,  # Y-axis acceleration in g (4 dp)
    "z"    : float | None,  # Z-axis acceleration in g (4 dp)
    "valid": bool           # False if read_raw() returned None
}
```

Called by: `SensorSampler.read_all()` and `SensorSampler.start_sampling_loop()`
(via `_safe_read`).

---

### 3.6 `drivers/gsr.py`

**Owner: Kailash**

Driver for the resistive GSR (galvanic skin response) sensor connected to an ADC.

#### Circuit model

```
3.3 V → GSR_REF_RESISTOR → ADC pin → skin electrodes → GND

V_adc   = 3.3 × R_skin / (R_ref + R_skin)
R_skin  = R_ref × V_adc / (3.3 − V_adc)
Cond μS = 1 / R_skin × 10^6
```

#### Module‑level import

Imports `config` for `ADC_FULL_SCALE`, `ADC_VREF`, `GSR_REF_RESISTOR_OHMS`, and
`GSR_SMOOTH_WINDOW`.  Imports `machine.ADC` (falls back to `None` on CPython).

#### Class `GSRSensor`

**Constructor `__init__(adc_pin=26)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `adc_pin` | `int` | GPIO pin number for ADC input; defaults to 26 (ADC0) |

Returns: `None`.

Instance variables:

| Variable | Type | Description |
|----------|------|-------------|
| `_pin` | `int` | GPIO pin number |
| `_adc` | `machine.ADC | None` | ADC object; `None` if init failed |

---

**`GSRSensor.read_raw()`**

Reads the 16‑bit ADC count from the hardware.

| Input | — |
|-------|---|
| Returns | `int` — raw ADC count in `[0, 65535]`; `0` on error or if ADC is not available |

Called by: `GSRSensor.read_conductance()`.

---

**`GSRSensor.read_conductance()`**

Converts the ADC reading to skin conductance via the voltage‑divider formula.

| Input | — |
|-------|---|
| Returns | `dict` with keys: |

```python
{
    "raw"            : int,    # raw ADC count [0, 65535]
    "voltage"        : float,  # ADC pin voltage in V (4 dp)
    "conductance_us" : float,  # skin conductance in µS (3 dp)
    "valid"          : bool    # False only when self._adc is None
}
```

Called by: `GSRSensor.read_smoothed()` and `SensorSampler.read_all()` /
`start_sampling_loop()` (via `_safe_read`).

---

**`GSRSensor.read_smoothed(window=None)`**

Averages `window` calls to `read_conductance()` to reduce ADC noise.

| Parameter | Type | Description |
|-----------|------|-------------|
| `window` | `int | None` | Samples to average; defaults to `config.GSR_SMOOTH_WINDOW` (5) |

Returns: `dict` — same structure as `read_conductance()` with values averaged
over `window` samples.

Not currently called by the sampler (sampler uses `read_conductance()` directly),
but available for manual use.

---

### 3.7 `sampler.py`

**Owner: Krishita**

`SensorSampler` sits between the sensor drivers and `main.py`.  It owns the
timer loop and rate‑division logic.

#### Module‑level variables

| Variable | Purpose |
|----------|---------|
| `I2C` | `machine.I2C` class or `None` (CPython stub) |
| `Timer` | `machine.Timer` class or `None` (CPython stub) |

#### Class `SensorSampler`

**Constructor `__init__(i2c, cfg=None)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `i2c` | `machine.I2C` | Shared I2C bus (400 kHz, GP4/GP5) |
| `cfg` | `module | None` | Config module override; defaults to imported `config` |

Returns: `None`.  Instantiates `MAX30102`, `ADXL345`, and `GSRSensor`.

Instance variables:

| Variable | Type | Description |
|----------|------|-------------|
| `_cfg` | module | Config reference |
| `_i2c` | `machine.I2C` | I2C bus reference |
| `_max30102` | `MAX30102` | SpO₂/HR driver instance |
| `_adxl345` | `ADXL345` | Accelerometer driver instance |
| `_gsr` | `GSRSensor` | GSR driver instance |
| `_timer` | `machine.Timer | None` | Hardware timer (None until started) |
| `_tick_count` | `int` | Counter of 10 Hz ticks; resets at `_spo2_divisor` |
| `_spo2_divisor` | `int` | Ticks between 1 Hz SpO₂/GSR reads (= `ACCEL_RATE_HZ / SPO2_RATE_HZ = 10`) |
| `_callback` | `callable | None` | User callback registered by `start_sampling_loop()` |

---

**`SensorSampler.check_all_sensors()`**

Runs `check_sensor()` on MAX30102 and ADXL345, checks GSR ADC initialisation.

| Input | — |
|-------|---|
| Returns | `dict` — `{"max30102": bool, "adxl345": bool, "gsr": bool}` |

Called by: `main.py` during startup.
Calls: `MAX30102.check_sensor()`, `ADXL345.check_sensor()`.

---

**`SensorSampler.read_all()`**

Takes a synchronised snapshot from all three sensors.

| Input | — |
|-------|---|
| Returns | `dict` — |

```python
{
    "timestamp_ms": int,
    "spo2":  {"spo2": float|None, "hr": float|None,
              "ir_raw": int|None, "red_raw": int|None, "valid": bool},
    "accel": {"x": float|None, "y": float|None,
              "z": float|None, "valid": bool},
    "gsr":   {"raw": int, "voltage": float,
              "conductance_us": float, "valid": bool},
}
```

Called by: `main.py` `_blocking_loop()` (fallback mode).
Calls: `utils.get_timestamp()`, `MAX30102.read_spo2_hr()` (via `_safe_read`),
`ADXL345.read_xyz()` (via `_safe_read`), `GSRSensor.read_conductance()` (via
`_safe_read`).

---

**`SensorSampler.start_sampling_loop(callback)`**

Starts a periodic `machine.Timer` at `ACCEL_INTERVAL_MS` (100 ms).
Every tick reads the accelerometer; every 10th tick also reads SpO₂ and GSR.

| Parameter | Type | Description |
|-----------|------|-------------|
| `callback` | `callable(dict)` | Function to invoke with sensor data on every timer tick |

Returns: `None`.

The internal `_timer_cb(t)` closure (Timer ISR):
- **10 Hz ticks**: builds `{"timestamp_ms": int, "accel": dict}` and calls `callback`.
- **1 Hz ticks**: builds full dict (adds `"spo2"` and `"gsr"` keys) and calls `callback`.

Called by: `main.py`.

---

**`SensorSampler.stop()`**

Calls `self._timer.deinit()` to stop the hardware timer.

| Input | — |
|-------|---|
| Returns | `None` |

Called by: `main.py` on shutdown.

---

**`SensorSampler._safe_read(fn, fallback)` (private, static)**

| Parameter | Type | Description |
|-----------|------|-------------|
| `fn` | `callable()` | Zero‑argument function to call |
| `fallback` | any | Value returned if `fn` raises an exception |

Returns: result of `fn()` or `fallback`.

---

### 3.8 `transport.py`

**Owner: Rob**

Handles Wi‑Fi connection management and HMAC‑signed HTTP POST to the gateway.
Written in MicroPython‑compatible pure Python (no third‑party libraries).

#### Module‑level variables

| Variable | Type | Description |
|----------|------|-------------|
| `network` | module or `None` | MicroPython `network` module; `None` on CPython |
| `_socket` | module or `None` | MicroPython `socket` module; `None` on CPython |
| `_WIFI_AVAILABLE` | `bool` | `True` when running on MicroPython with networking |
| `_API_SESSION_START` | `str` | `"/api/session/start"` — gateway endpoint |
| `_API_SESSION_END` | `str` | `"/api/session/end"` — gateway endpoint |
| `_API_INGEST` | `str` | `"/api/ingest"` — gateway endpoint |

---

**`_hmac_sha256(key, message)` (private)**

Implements RFC 2104 HMAC‑SHA256 using only `hashlib` (no `hmac` module).

| Parameter | Type | Description |
|-----------|------|-------------|
| `key` | `str | bytes` | Shared secret key |
| `message` | `str | bytes` | Message to authenticate |

Returns: `str` — hex‑encoded HMAC‑SHA256 digest.

Called by: `send_api()` and `start_session()`.

---

**`connect_wifi(ssid, password, timeout_s=30)`**

Connects the Pico to a Wi‑Fi access point and blocks until connected or timeout.

| Parameter | Type | Description |
|-----------|------|-------------|
| `ssid` | `str` | Wi‑Fi network name |
| `password` | `str` | Wi‑Fi password |
| `timeout_s` | `int` | Maximum seconds to wait (default 30) |

Returns: `str | None` — IP address string on success; `None` on failure.

Called by: `main.py` during startup.

---

**`disconnect_wifi()`**

Disconnects from Wi‑Fi and deactivates the WLAN interface.

| Input | — |
|-------|---|
| Returns | `None` |

Called by: `main.py` on shutdown.

---

**`_http_post(host, port, path, body_bytes, extra_headers=None, timeout_s=10)` (private)**

Sends a raw HTTP/1.0 POST request over a socket and returns the status code.

| Parameter | Type | Description |
|-----------|------|-------------|
| `host` | `str` | Gateway hostname or IP |
| `port` | `int` | TCP port |
| `path` | `str` | URL path |
| `body_bytes` | `bytes` | Request body |
| `extra_headers` | `dict | None` | Additional HTTP headers |
| `timeout_s` | `int` | Socket timeout in seconds (default 10) |

Returns: `int` — HTTP status code (`200`, `201`, etc.); `0` on connection error.

Called by: `send_api()`.

---

**`send_api(host, port, path, payload, hmac_key)`**

Signs `payload` with HMAC‑SHA256, serialises to JSON, and POSTs to the gateway.

| Parameter | Type | Description |
|-----------|------|-------------|
| `host` | `str` | Gateway IP or hostname |
| `port` | `int` | Gateway TCP port |
| `path` | `str` | API path (e.g. `_API_INGEST`) |
| `payload` | `dict` | Data to send (must be JSON‑serialisable) |
| `hmac_key` | `str` | Shared HMAC secret |

Returns: `int` — HTTP status code from gateway (`200`/`201` = success, `0` = error).

Called by: `main.py` `_flush_batch()`.
Gateway receives at: `app.py` `api_ingest()`.

---

**`start_session(host, port, patient_id, device_id, hmac_key)`**

Signs and POSTs to `/api/session/start` and parses the `session_id` from the
JSON response body.

| Parameter | Type | Description |
|-----------|------|-------------|
| `host` | `str` | Gateway IP |
| `port` | `int` | Gateway TCP port |
| `patient_id` | `int` | Patient ID in gateway database |
| `device_id` | `str` | Identifier for this Pico device |
| `hmac_key` | `str` | Shared HMAC secret |

Returns: `int | None` — session ID assigned by the gateway; `None` on failure.

Called by: `main.py` during startup.
Gateway receives at: `app.py` `api_session_start()`.

---

**`end_session(host, port, session_id, hmac_key)`**

Sends an HMAC‑signed POST to `/api/session/end`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `host` | `str` | Gateway IP |
| `port` | `int` | Gateway TCP port |
| `session_id` | `int` | Session to close |
| `hmac_key` | `str` | Shared HMAC secret |

Returns: `bool` — `True` if gateway returned HTTP 200.

Called by: `main.py` on shutdown.
Gateway receives at: `app.py` `api_session_end()`.

---

### 3.9 `main.py`

**Owner: Rob**

Top‑level application entry point executed on boot by MicroPython.
Coordinates hardware setup, Wi‑Fi, sampling, and shutdown.

#### Module‑level variables

| Variable | Type | Description |
|----------|------|-------------|
| `_HARDWARE` | `bool` | `True` when running on real RP2350 hardware |
| `_led` | `machine.Pin | None` | Onboard LED pin; `None` until initialised |
| `_led_state` | `bool` | Current LED on/off state |
| `_session_id` | `int | None` | Session ID from gateway; `None` until connected |
| `_pending_batch` | `list` | Buffer of unsent 1 Hz sensor readings |

---

**`_toggle_led()` (private)**

Toggles `_led` and updates `_led_state`.

| Input | — |
|-------|---|
| Returns | `None` |

Called by: `_on_sensor_data()` (on every 1 Hz tick) and error‑handling loops.

---

**`_on_sensor_data(data)` (private)**

Callback registered with `SensorSampler.start_sampling_loop()`.

| Parameter | Type | Description |
|-----------|------|-------------|
| `data` | `dict` | Sensor reading from the sampler timer |

Returns: `None`.

Behaviour:
- Always calls `utils.format_reading(data)` and prints the `[SOMNI][DATA]` line.
- For 1 Hz full readings (dict contains `"spo2"` key): toggles LED, appends to `_pending_batch`.
- When `len(_pending_batch) >= config.TRANSPORT_BATCH_SIZE`: calls `_flush_batch()`.
- For 10 Hz accelerometer‑only ticks: only prints; does not transmit.

---

**`_flush_batch()` (private)**

Sends each reading in `_pending_batch` to the gateway via `transport.send_api()`,
then clears the buffer.

| Input | — |
|-------|---|
| Returns | `None` |

Calls: `transport.send_api()` for each item.  Failed sends are silently dropped.

---

**`main()`**

Full device startup sequence:

1. Initialise onboard LED.
2. Initialise I2C bus (GP4/GP5, 400 kHz).
3. Create `SensorSampler`, call `check_all_sensors()`.
4. Connect to Wi‑Fi, call `transport.start_session()`.
5. Call `sampler.start_sampling_loop(_on_sensor_data)`.
6. Idle loop (`while True: sleep_ms(1000)`) until Ctrl‑C or exception.
7. Shutdown: flush batch, call `transport.end_session()`, `sampler.stop()`,
   `transport.disconnect_wifi()`.

| Input | — |
|-------|---|
| Returns | `None` (runs indefinitely on device) |

---

**`_blocking_loop(sampler)` (private)**

Fallback polling loop used when `machine.Timer` is unavailable.
Calls `sampler.read_all()` then `_on_sensor_data()` in a `while True` loop
with `time.sleep_ms(config.SPO2_INTERVAL_MS)` delay (~1 Hz).

| Parameter | Type | Description |
|-----------|------|-------------|
| `sampler` | `SensorSampler` | Initialised sampler instance |

Returns: `None`.

---

## 4. Gateway — `somniguard_gateway/`

### 4.1 `config.py`

**Owner: Ronit**

Module of constants loaded from environment variables.  No functions.

#### Module‑level variables

| Variable | Type | Default | Environment variable | Purpose |
|----------|------|---------|----------------------|---------|
| `_BASE_DIR` | `str` | directory of config.py | — | Base path for relative paths |
| `DB_PATH` | `str` | `<BASE_DIR>/somniguard.db` | `SOMNI_DB_PATH` | SQLite database file path |
| `REPORT_DIR` | `str` | `<BASE_DIR>/reports` | `SOMNI_REPORT_DIR` | Directory for PDF reports |
| `SECRET_KEY` | `str` | `"dev-only-secret-key-…"` | `SOMNI_SECRET_KEY` | Flask session secret key |
| `WTF_CSRF_SECRET_KEY` | `str` | same as `SECRET_KEY` | `SOMNI_CSRF_KEY` | WTForms CSRF secret |
| `PICO_HMAC_KEY` | `str` | `"dev-hmac-key-…"` | `SOMNI_HMAC_KEY` | Shared HMAC key for Pico telemetry authentication |
| `FLASK_HOST` | `str` | `"0.0.0.0"` | `SOMNI_HOST` | Flask bind address |
| `FLASK_PORT` | `int` | `5000` | `SOMNI_PORT` | Flask bind port |
| `FLASK_DEBUG` | `bool` | `False` | `SOMNI_DEBUG` | Flask debug mode flag |
| `TAILSCALE_ONLY` | `bool` | `False` | `SOMNI_TAILSCALE_ONLY` | Restrict dashboard to Tailscale IPs only |
| `PICO_ALLOWED_CIDRS` | `list[str]` | RFC 1918 ranges | `SOMNI_PICO_CIDRS` | CIDRs from which the Pico may send telemetry |
| `DESATURATION_THRESHOLD_PCT` | `float` | `90.0` | — | SpO₂ threshold for a desaturation event (%) |
| `MOVEMENT_THRESHOLD_G` | `float` | `0.05` | — | Accel Δ‑magnitude threshold for a movement event (g) |

---

### 4.2 `database.py`

**Owner: Ronit**

SQLite access layer.  All queries use parameterised statements.
WAL journal mode and foreign‑key enforcement are applied on every connection.

#### Module‑level variable

| Variable | Purpose |
|----------|---------|
| `_SCHEMA` | `str` — DDL SQL string used by `init_db()` to create tables |

#### Schema (tables)

| Table | Key columns |
|-------|-------------|
| `users` | `id`, `username`, `email`, `password_hash`, `role` |
| `patients` | `id`, `name`, `dob`, `notes`, `created_by` |
| `sessions` | `id`, `patient_id`, `device_id`, `started_at`, `ended_at` |
| `telemetry` | `id`, `session_id`, `timestamp_ms`, `spo2`, `hr`, `accel_x/y/z`, `gsr_raw`, `gsr_voltage`, `gsr_conductance_us`, `valid_spo2`, `valid_accel`, `valid_gsr` |
| `reports` | `id`, `session_id`, `pdf_path`, `summary_json`, `hmac_sig` |

---

**`get_db()`**

Opens (or creates) the SQLite database and returns a connection.

| Input | — |
|-------|---|
| Returns | `sqlite3.Connection` — row_factory set to `sqlite3.Row`; WAL + FK enabled |

Called by: all other `database.py` helpers.

---

**`init_db()`**

Runs the `_SCHEMA` DDL with `executescript`.  Safe to call multiple times
(`CREATE TABLE IF NOT EXISTS`).

| Input | — |
|-------|---|
| Returns | `None` |

Called by: `run.py` `main()` on startup.

---

**`create_user(username, email, password_hash, role="clinician")`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `username` | `str` | Unique login name |
| `email` | `str` | Unique email address |
| `password_hash` | `str` | bcrypt hash of the plaintext password |
| `role` | `str` | `"admin"` or `"clinician"` (default) |

Returns: `int` — row ID of the new user.
Raises: `sqlite3.IntegrityError` if username or email already exists.

Called by: `app.py` `create_user()` route, `run.py` `_bootstrap_admin()`.

---

**`get_user_by_username(username)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `username` | `str` | Login name |

Returns: `sqlite3.Row | None` — user row, or `None` if not found.

Called by: `app.py` `login()` route.

---

**`get_user_by_id(user_id)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `user_id` | `int` | Primary key |

Returns: `sqlite3.Row | None`.

Called by: `app.py` `_load_user()` (Flask‑Login user loader).

---

**`list_users()`**

Returns all users ordered by username (excludes `password_hash` column).

| Input | — |
|-------|---|
| Returns | `list[sqlite3.Row]` |

Called by: `app.py` `manage_users()` route, `run.py` `_bootstrap_admin()`.

---

**`delete_user(user_id)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `user_id` | `int` | Primary key of user to delete |

Returns: `bool` — `True` if a row was deleted.

Called by: `app.py` `delete_user()` route.

---

**`create_patient(name, dob, notes, created_by)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Patient full name |
| `dob` | `str | None` | Date of birth in `"YYYY-MM-DD"` format, or `None` |
| `notes` | `str | None` | Free‑text clinical notes |
| `created_by` | `int` | User ID of the creator |

Returns: `int` — row ID of the new patient.

Called by: `app.py` `new_patient()` route.

---

**`list_patients()`**

| Input | — |
|-------|---|
| Returns | `list[sqlite3.Row]` — all patients (with `created_by_name` joined) ordered by name |

Called by: `app.py` `patients()` and `dashboard()` routes.

---

**`get_patient(patient_id)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `patient_id` | `int` | Primary key |

Returns: `sqlite3.Row | None`.

Called by: `app.py` `patient_detail()`, `api_session_start()`.

---

**`create_session(patient_id, device_id="pico-01")`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `patient_id` | `int` | Patient this session belongs to |
| `device_id` | `str` | Device identifier (default `"pico-01"`) |

Returns: `int` — row ID of the new session.

Called by: `app.py` `api_session_start()`.

---

**`end_session(session_id)`**

Sets `ended_at = CURRENT_TIMESTAMP` for the given session.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `int` | Session to close |

Returns: `None`.

Called by: `app.py` `api_session_end()`.

---

**`list_sessions(patient_id=None)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `patient_id` | `int | None` | If given, filters to sessions for this patient |

Returns: `list[sqlite3.Row]` — session rows with `patient_name` joined,
ordered by `started_at DESC`.

Called by: `app.py` `dashboard()` and `patient_detail()` routes.

---

**`get_session(session_id)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `int` | Primary key |

Returns: `sqlite3.Row | None` — session with `patient_name` and `patient_dob` joined.

Called by: `app.py` `session_detail()`, `generate_report()`, `download_report()`,
`api_session_end()`.

---

**`insert_telemetry(session_id, reading)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `int` | Session this reading belongs to |
| `reading` | `dict` | Telemetry payload from `api_ingest()` — keys: `timestamp_ms`, `spo2` (dict), `accel` (dict), `gsr` (dict) |

Returns: `int` — row ID of the inserted telemetry row.

Called by: `app.py` `api_ingest()`.

---

**`get_telemetry(session_id, limit=None)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `int` | Session to query |
| `limit` | `int | None` | Maximum rows to return; `None` = all |

Returns: `list[sqlite3.Row]` — telemetry rows in chronological order.

Called by: `reports.py` `compute_summary()`, `generate_pdf()`;
`app.py` `session_detail()`.

---

**`save_report(session_id, pdf_path, summary_json, hmac_sig)`**

Deletes any existing report for the session, then inserts the new record.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `int` | Session this report covers |
| `pdf_path` | `str` | Absolute path to the PDF file |
| `summary_json` | `str` | JSON string of the summary dict |
| `hmac_sig` | `str` | Hex HMAC‑SHA256 of `summary_json` |

Returns: `int` — row ID of the new report.

Called by: `app.py` `generate_report()` route.

---

**`get_report(session_id)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `int` | Session to look up |

Returns: `sqlite3.Row | None`.

Called by: `app.py` `session_detail()`, `download_report()`.

---

### 4.3 `reports.py`

**Owner: Ronit**

Feature extraction and PDF generation for sleep sessions.

#### Module‑level variables

| Variable | Purpose |
|----------|---------|
| `_REPORTLAB_AVAILABLE` | `bool` — `True` if `reportlab` package is installed |

---

**`compute_summary(session_id)`**

Queries all telemetry for a session and aggregates sleep metrics.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `int` | Session to summarise |

Returns: `dict` with keys:

```python
{
    "session_id"           : int,
    "total_telemetry_rows" : int,
    "duration_s"           : float,   # session duration in seconds
    "spo2"                 : {         # SpO₂ stats (valid readings only)
        "min": float|None, "max": float|None,
        "mean": float|None, "count": int
    },
    "hr"                   : {         # HR stats (valid readings only)
        "min": float|None, "max": float|None,
        "mean": float|None, "count": int
    },
    "gsr"                  : {         # GSR conductance stats (µS, valid readings only)
        "min": float|None, "max": float|None,
        "mean": float|None, "count": int
    },
    "desaturation_events"  : int,      # SpO₂ readings below cfg.DESATURATION_THRESHOLD_PCT
    "movement_events"      : int,      # accel vector-magnitude changes > cfg.MOVEMENT_THRESHOLD_G
    "generated_at"         : str,      # ISO 8601 UTC timestamp
    "non_clinical_note"    : str,      # disclaimer string
}
```

Called by: `app.py` `generate_report()` route.
Calls: `database.get_telemetry()`.

---

**`sign_summary(summary_json)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `summary_json` | `str` | JSON string of the summary dict |

Returns: `str` — hex‑encoded HMAC‑SHA256 of `summary_json` using `cfg.PICO_HMAC_KEY`.

Called by: `app.py` `generate_report()` route.

---

**`generate_pdf(session_row, summary)`**

Renders a ReportLab PDF report to `REPORT_DIR` and returns the file path.

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_row` | `sqlite3.Row` | Session record (joined with patient data) |
| `summary` | `dict` | Output of `compute_summary()` |

Returns: `str` — absolute path to the generated PDF file.
Raises: `RuntimeError` if `reportlab` is not installed.

Called by: `app.py` `generate_report()` route.
Calls: `database.get_telemetry()` (for the raw‑telemetry sample table in the PDF).

Private helpers used internally:

| Helper | Description |
|--------|-------------|
| `_fmt(value, decimals=2)` | Formats a float to given decimal places; returns `"—"` if `None` |
| `_summary_table_style(header_only_bold=False)` | Returns a `TableStyle` for summary tables |

---

### 4.4 `tailscale.py`

**Owner: Ronit**

Tailscale VPN integration: IP classification, daemon status queries, and
Flask network‑policy enforcement.

#### Module‑level variables

| Variable | Type | Value | Purpose |
|----------|------|-------|---------|
| `TAILSCALE_CIDR` | `ipaddress.IPv4Network` | `100.64.0.0/10` | Tailscale CGNAT range |
| `_PRIVATE_RANGES` | `list[IPv4Network]` | RFC 1918 + loopback | Private LAN ranges for Pico allowance |

---

**`is_tailscale_ip(ip_str)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `ip_str` | `str` | IPv4 or IPv6 address string |

Returns: `bool` — `True` if the address is in `100.64.0.0/10`.

Called by: `check_network_policy()`.

---

**`is_private_lan_ip(ip_str)`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `ip_str` | `str` | IPv4 or IPv6 address string |

Returns: `bool` — `True` if the address is in RFC 1918 or loopback ranges.

Not called directly by `check_network_policy()` (that function uses CIDR
matching against `pico_cidrs`); available for direct use.

---

**`get_tailscale_status()`**

Runs `tailscale status --json` as a subprocess (5‑second timeout).

| Input | — |
|-------|---|
| Returns | `dict | None` — parsed Tailscale status object; `None` on any failure |

Called by: `tailscale_running()`, `get_local_tailscale_ip()`,
`get_tailscale_hostname()`, `list_tailscale_peers()`.

---

**`tailscale_running()`**

| Input | — |
|-------|---|
| Returns | `bool` — `True` if `BackendState == "Running"` and `Self` is present |

Called by: `app.py` `api_tailscale_status()`.

---

**`get_local_tailscale_ip()`**

| Input | — |
|-------|---|
| Returns | `str | None` — first IPv4 address from `Self.TailscaleIPs`; `None` if unavailable |

Called by: `app.py` `api_tailscale_status()`.

---

**`get_tailscale_hostname()`**

| Input | — |
|-------|---|
| Returns | `str | None` — `DNSName` (or `HostName`) of this machine; `None` if unavailable |

Called by: `app.py` `api_tailscale_status()`.

---

**`list_tailscale_peers()`**

| Input | — |
|-------|---|
| Returns | `list[dict]` — each dict has keys `HostName`, `DNSName`, `TailscaleIPs`, `Online`, `OS` |

Called by: `app.py` `api_tailscale_status()`.

---

**`check_network_policy(remote_addr, tailscale_only, is_api_path=False, pico_cidrs=None)`**

Central policy engine evaluated by `app.py` on every request.

| Parameter | Type | Description |
|-----------|------|-------------|
| `remote_addr` | `str` | Client IP address |
| `tailscale_only` | `bool` | Whether `TAILSCALE_ONLY` mode is active |
| `is_api_path` | `bool` | `True` when path starts with `/api/` |
| `pico_cidrs` | `list[str] | None` | CIDRs from which Pico traffic may arrive |

Returns: `bool` — `True` if the request should proceed; `False` → caller returns
HTTP 403.

Policy rules (in order):
1. Loopback (`127.0.0.1` / `::1`) → always allow.
2. Tailscale IP (`100.64.0.0/10`) → always allow.
3. `tailscale_only = False` → allow all (development mode).
4. `tailscale_only = True` and `is_api_path = True` and IP in `pico_cidrs` → allow.
5. All other → deny.

Called by: `app.py` `_enforce_network_policy()` (`before_request` hook).

---

### 4.5 `app.py`

**Owner: Rob**

Flask web application.  Provides the web dashboard and the REST telemetry API.

#### Module‑level objects

| Object | Type | Description |
|--------|------|-------------|
| `app` | `Flask` | Application instance |
| `csrf` | `CSRFProtect` | Flask‑WTF CSRF protection (wraps `app`) |
| `login_mgr` | `LoginManager` | Flask‑Login manager |

#### WTForms form classes

| Form | Fields | Used by |
|------|--------|---------|
| `LoginForm` | `username` (string, required, 1–64 chars), `password` (password, required, 1–128 chars) | `login()` route |
| `NewUserForm` | `username` (3–64), `email` (email, 5–120), `password` (8–128), `role` (select: clinician/admin) | `create_user()` route |
| `NewPatientForm` | `name` (1–120), `dob` (date, optional), `notes` (text, optional, max 2000) | `new_patient()` route |

#### Inner class `_UserProxy(UserMixin)`

Wraps a `sqlite3.Row` to satisfy Flask‑Login.

| Property / Method | Returns | Source |
|-------------------|---------|--------|
| `get_id()` | `str` — string representation of `id` | `row["id"]` |
| `id` | `int` | `row["id"]` |
| `username` | `str` | `row["username"]` |
| `email` | `str` | `row["email"]` |
| `role` | `str` | `row["role"]` |

---

#### Routes and functions

**`_enforce_network_policy()` — `@app.before_request`**

Runs before every request.  Calls `tailscale.check_network_policy()`.

| Input | — |
|-------|---|
| Returns | `flask.Response` (HTTP 403 JSON) if denied; `None` to continue |

---

**`_load_user(user_id)` — `@login_mgr.user_loader`**

| Parameter | Type | Description |
|-----------|------|-------------|
| `user_id` | `str` | User primary key as string |

Returns: `_UserProxy | None`.
Calls: `database.get_user_by_id()`.

---

**`admin_required(f)` — decorator**

Redirects non‑admin users to `/dashboard` with a danger flash message.

| Parameter | Type | Description |
|-----------|------|-------------|
| `f` | `callable` | View function to protect |

Returns: decorated function.

---

**`index()` — `GET /`**

Redirects to `/dashboard` if logged in, otherwise to `/login`.

---

**`login()` — `GET /login`, `POST /login`**

Displays login form and authenticates users with bcrypt.

| Input | HTTP form: `username`, `password` |
|-------|-----------------------------------|
| Returns | Redirect to dashboard on success; re‑renders `login.html` with error on failure |

Calls: `database.get_user_by_username()`, `bcrypt.checkpw()`,
`flask_login.login_user()`, `_is_safe_url()`.

---

**`logout()` — `GET /logout`** _(login required)_

| Input | — |
|-------|---|
| Returns | Redirect to `/login` |

Calls: `flask_login.logout_user()`.

---

**`dashboard()` — `GET /dashboard`** _(login required)_

| Input | — |
|-------|---|
| Returns | Rendered `dashboard.html` with recent sessions and all patients |

Calls: `database.list_sessions()`, `database.list_patients()`.

---

**`patients()` — `GET /patients`** _(login required)_

| Input | — |
|-------|---|
| Returns | Rendered `patients.html` with all patients and a new‑patient form |

Calls: `database.list_patients()`.

---

**`new_patient()` — `POST /patients/new`** _(login required)_

| Input | HTTP form: `name`, `dob`, `notes` |
|-------|-----------------------------------|
| Returns | Redirect to `/patients` |

Calls: `database.create_patient()`.

---

**`patient_detail(patient_id)` — `GET /patients/<int:patient_id>`** _(login required)_

| Parameter | Type | Description |
|-----------|------|-------------|
| `patient_id` | `int` | Patient primary key (URL variable) |

Returns: Rendered `patient_detail.html` or redirect if not found.
Calls: `database.get_patient()`, `database.list_sessions()`.

---

**`session_detail(session_id)` — `GET /sessions/<int:session_id>`** _(login required)_

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `int` | Session primary key (URL variable) |

Returns: Rendered `session_detail.html` with telemetry and report (if any).
Calls: `database.get_session()`, `database.get_telemetry()`,
`database.get_report()`.

---

**`generate_report(session_id)` — `POST /sessions/<int:session_id>/report`** _(login required)_

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `int` | Session to generate report for |

Returns: Redirect to `session_detail`.
Calls: `reports.compute_summary()`, `reports.sign_summary()`,
`reports.generate_pdf()`, `database.save_report()`.

---

**`download_report(session_id)` — `GET /sessions/<int:session_id>/report/download`** _(login required)_

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `int` | Session whose PDF to serve |

Returns: PDF file download (`application/pdf`) or redirect on error.
Calls: `database.get_report()`, `flask.send_file()`.

---

**`manage_users()` — `GET /admin/users`** _(login required, admin only)_

| Input | — |
|-------|---|
| Returns | Rendered `manage_users.html` |

Calls: `database.list_users()`.

---

**`create_user()` — `POST /admin/users/new`** _(login required, admin only)_

| Input | HTTP form: `username`, `email`, `password`, `role` |
|-------|-----------------------------------------------------|
| Returns | Redirect to `/admin/users` |

Calls: `bcrypt.hashpw()`, `database.create_user()`.

---

**`delete_user(user_id)` — `POST /admin/users/<int:user_id>/delete`** _(login required, admin only)_

| Parameter | Type | Description |
|-----------|------|-------------|
| `user_id` | `int` | User to delete (cannot be the current user) |

Returns: Redirect to `/admin/users`.
Calls: `database.delete_user()`.

---

**`api_session_start()` — `POST /api/session/start`** _(CSRF‑exempt; HMAC auth)_

| Input | JSON body: `patient_id` (int), `device_id` (str), `hmac` (str) |
|-------|------------------------------------------------------------------|
| Returns | `{"session_id": int}` on success (HTTP 201); `{"error": str}` on failure |

Calls: `_verify_hmac()`, `database.get_patient()`, `database.create_session()`.
Called by: Pico `transport.start_session()`.

---

**`api_ingest()` — `POST /api/ingest`** _(CSRF‑exempt; HMAC auth)_

| Input | JSON body: `session_id`, `timestamp_ms`, `spo2` dict, `accel` dict, `gsr` dict, `hmac` |
|-------|------------------------------------------------------------------------------------------|
| Returns | `{"ok": true}` (HTTP 200) or `{"error": str}` |

Calls: `_verify_hmac()`, `database.insert_telemetry()`.
Called by: Pico `transport.send_api()`.

---

**`api_session_end()` — `POST /api/session/end`** _(CSRF‑exempt; HMAC auth)_

| Input | JSON body: `session_id` (int), `hmac` (str) |
|-------|----------------------------------------------|
| Returns | `{"ok": true}` (HTTP 200) or `{"error": str}` |

Calls: `_verify_hmac()`, `database.end_session()`.
Called by: Pico `transport.end_session()`.

---

**`api_tailscale_status()` — `GET /api/tailscale/status`** _(login required)_

Admin‑only JSON endpoint.

| Input | — |
|-------|---|
| Returns | `{"running": bool, "local_ip": str|null, "hostname": str|null, "peers": list, "tailscale_only_mode": bool}` |

Calls: `tailscale.tailscale_running()`, `tailscale.get_local_tailscale_ip()`,
`tailscale.get_tailscale_hostname()`, `tailscale.list_tailscale_peers()`.

---

**`_verify_hmac(body)` (private)**

| Parameter | Type | Description |
|-----------|------|-------------|
| `body` | `dict` | Parsed JSON body including the `"hmac"` field |

Returns: `bool` — `True` if the HMAC‑SHA256 tag is valid.

Algorithm: strips the `"hmac"` key, serialises remaining fields with sorted keys,
computes expected HMAC using `cfg.PICO_HMAC_KEY`, and compares with
`hmac.compare_digest()` to prevent timing attacks.

Called by: `api_session_start()`, `api_ingest()`, `api_session_end()`.

---

**`_is_safe_url(target)` (private)**

| Parameter | Type | Description |
|-----------|------|-------------|
| `target` | `str` | Redirect URL candidate |

Returns: `bool` — `True` only if the URL has the same host as the current request
(prevents open redirect).

Called by: `login()` route.

---

### 4.6 `run.py`

**Owner: Rob**

Gateway entry point.  Initialises the database, runs the first‑admin bootstrap,
and starts the Flask development server.

#### Functions

**`_bootstrap_admin()` (private)**

Creates the first admin account interactively if no users exist.

| Input | — (reads from stdin when `sys.stdin.isatty()`) |
|-------|------------------------------------------------|
| Returns | `None` |

Calls: `database.list_users()`, `database.create_user()`,
`bcrypt.hashpw()`, `getpass.getpass()`.

---

**`main()`**

Full gateway startup sequence:

1. `os.makedirs(cfg.REPORT_DIR)` — ensure report directory exists.
2. `database.init_db()` — create tables.
3. `_bootstrap_admin()` — prompt for admin credentials if no users exist.
4. `app.run(host, port, debug)` — start Flask server.

| Input | — |
|-------|---|
| Returns | `None` |

---

## 5. Cross‑File Interaction Map

This section maps every significant function call that crosses a module boundary.

### 5.1 Pico internal call graph

```
main.py
 ├─ imports config           (constants only)
 ├─ imports utils            → format_reading(), get_timestamp() (via sampler)
 ├─ from sampler import SensorSampler
 │     └─ SensorSampler.__init__()
 │           ├─ MAX30102(i2c)             [drivers/max30102.py]
 │           ├─ ADXL345(i2c)             [drivers/adxl345.py]
 │           └─ GSRSensor(adc_pin)       [drivers/gsr.py]
 ├─ import transport
 │     ├─ transport.connect_wifi()
 │     ├─ transport.start_session()  ──────────────── HTTP POST → /api/session/start
 │     ├─ transport.send_api()       ──────────────── HTTP POST → /api/ingest
 │     ├─ transport.end_session()    ──────────────── HTTP POST → /api/session/end
 │     └─ transport.disconnect_wifi()
 │
 ├─ sampler.check_all_sensors()
 │     ├─ MAX30102.check_sensor()
 │     └─ ADXL345.check_sensor()
 │
 ├─ sampler.start_sampling_loop(_on_sensor_data)
 │     [Timer ISR every 100 ms]
 │     ├─ utils.get_timestamp()
 │     ├─ ADXL345.read_xyz()            [every 100 ms]
 │     ├─ MAX30102.read_spo2_hr()       [every 1000 ms]
 │     └─ GSRSensor.read_conductance()  [every 1000 ms]
 │
 └─ _on_sensor_data(data)  ← called by Timer ISR
       ├─ utils.format_reading(data)
       └─ _flush_batch() → transport.send_api()
```

### 5.2 Gateway internal call graph

```
run.py
 ├─ database.init_db()
 ├─ _bootstrap_admin()
 │     ├─ database.list_users()
 │     └─ database.create_user()
 └─ app.run()

app.py  (before_request)
 └─ tailscale.check_network_policy()
       └─ tailscale.is_tailscale_ip()

app.py  (login route)
 └─ database.get_user_by_username()

app.py  (dashboard route)
 ├─ database.list_sessions()
 └─ database.list_patients()

app.py  (api_session_start)
 ├─ _verify_hmac()
 ├─ database.get_patient()
 └─ database.create_session()

app.py  (api_ingest)
 ├─ _verify_hmac()
 └─ database.insert_telemetry()

app.py  (api_session_end)
 ├─ _verify_hmac()
 └─ database.end_session()

app.py  (generate_report)
 ├─ database.get_session()
 ├─ reports.compute_summary()
 │     └─ database.get_telemetry()
 ├─ reports.sign_summary()
 ├─ reports.generate_pdf()
 │     └─ database.get_telemetry()
 └─ database.save_report()

app.py  (api_tailscale_status)
 ├─ tailscale.tailscale_running()
 ├─ tailscale.get_local_tailscale_ip()
 ├─ tailscale.get_tailscale_hostname()
 └─ tailscale.list_tailscale_peers()
       └─ (all above call) tailscale.get_tailscale_status()
```

### 5.3 Pico ↔ Gateway network calls

| Pico caller | Direction | Gateway receiver | Purpose |
|-------------|-----------|------------------|---------|
| `transport.start_session()` | → HTTP POST | `app.api_session_start()` | Begin a new sleep session; returns `session_id` |
| `transport.send_api(..., _API_INGEST, ...)` | → HTTP POST | `app.api_ingest()` | Stream one telemetry reading |
| `transport.end_session()` | → HTTP POST | `app.api_session_end()` | Mark session closed |

All three calls include an `"hmac"` field in the JSON body.
Gateway verifies with `_verify_hmac()` before processing.

### 5.4 Key shared data structures

#### Telemetry reading dict (Pico → Gateway)

Produced by `SensorSampler.read_all()` / `start_sampling_loop()`.
Sent by `transport.send_api()`.
Received and stored by `app.api_ingest()` → `database.insert_telemetry()`.

```python
{
    "session_id"   : int,
    "timestamp_ms" : int,
    "spo2"  : {
        "spo2"    : float | None,
        "hr"      : float | None,
        "ir_raw"  : int   | None,
        "red_raw" : int   | None,
        "valid"   : bool
    },
    "accel" : {
        "x"     : float | None,
        "y"     : float | None,
        "z"     : float | None,
        "valid" : bool
    },
    "gsr"   : {
        "raw"            : int,
        "voltage"        : float,
        "conductance_us" : float,
        "valid"          : bool
    },
    "hmac"  : str   # HMAC-SHA256 hex digest added by transport layer
}
```

#### Sleep summary dict (gateway‑internal)

Produced by `reports.compute_summary()`.
Passed to `reports.sign_summary()` and `reports.generate_pdf()`.
Stored as JSON in `database.save_report()`.

```python
{
    "session_id"           : int,
    "total_telemetry_rows" : int,
    "duration_s"           : float,
    "spo2"                 : {"min": float|None, "max": float|None,
                              "mean": float|None, "count": int},
    "hr"                   : {"min": float|None, "max": float|None,
                              "mean": float|None, "count": int},
    "gsr"                  : {"min": float|None, "max": float|None,
                              "mean": float|None, "count": int},
    "desaturation_events"  : int,
    "movement_events"      : int,
    "generated_at"         : str,   # ISO 8601 UTC
    "non_clinical_note"    : str
}
```

---

> **Disclaimer:** SOMNI‑Guard is an educational prototype.
> SpO₂ and HR values are approximations produced by simplified algorithms.
> This system must **not** be used for clinical diagnosis, treatment decisions,
> or any patient‑safety purpose.
