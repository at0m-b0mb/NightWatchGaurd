"""
drivers/ads1115.py — OPTIONAL ADS1115 16-bit ADC driver.

This driver is NOT required for the default SOMNI-Guard configuration.
The Grove GSR v1.2 sensor connects directly to the Pico's built-in ADC
on GP26.

Use this driver only if upgrading to an ADS1115 external ADC for higher
resolution GSR readings (16-bit vs 12-bit).  To use it:

1. Wire the ADS1115 to I2C bus 0 (GP4/GP5, shared with MAX30102).
2. Wire the Grove GSR SIG pin to ADS1115 AIN0 (instead of Pico GP26).
3. Uncomment the ADS1115 settings in config.py.
4. Modify drivers/gsr.py to accept an ADS1115 instance instead of
   using machine.ADC directly.
5. Update sampler.py to create the ADS1115 and pass it to GSRSensor.

See docs/hardware_setup.md for ADS1115 wiring instructions.

Key specs:
- 16-bit resolution (signed: -32768 to +32767)
- Programmable gain amplifier (PGA): ±0.256 V to ±6.144 V full-scale
- Single-shot or continuous conversion modes
- 4 single-ended or 2 differential input channels
- I2C interface up to 3.4 MHz
- Default address 0x48 (ADDR pin → GND)

Educational prototype — not a clinically approved device.
"""

import struct
import time

# ---------------------------------------------------------------------------
# Register addresses
# ---------------------------------------------------------------------------
_REG_CONVERSION = 0x00   # 16-bit conversion result (read-only)
_REG_CONFIG     = 0x01   # 16-bit configuration register (read/write)

# ---------------------------------------------------------------------------
# Config register bit fields
# ---------------------------------------------------------------------------

# OS — Operational status / single-shot start (bit 15)
_OS_START = 0x8000       # write: start a single conversion
_OS_BUSY  = 0x0000       # read: conversion in progress
_OS_IDLE  = 0x8000       # read: device is idle / conversion complete

# MUX — Input multiplexer (bits 14:12), single-ended vs GND
_MUX = {
    0: 0x4000,  # AIN0 vs GND
    1: 0x5000,  # AIN1 vs GND
    2: 0x6000,  # AIN2 vs GND
    3: 0x7000,  # AIN3 vs GND
}

# PGA — Programmable gain (bits 11:9)
# gain_value → (config bits, full-scale voltage)
_PGA = {
    2/3: (0x0000, 6.144),
    1:   (0x0200, 4.096),
    2:   (0x0400, 2.048),
    4:   (0x0600, 1.024),
    8:   (0x0800, 0.512),
    16:  (0x0A00, 0.256),
}

# MODE — single-shot (bit 8)
_MODE_SINGLE = 0x0100

# DR — Data rate (bits 7:5), index → (config bits, samples per second)
_DR = {
    0: (0x0000,   8),
    1: (0x0020,  16),
    2: (0x0040,  32),
    3: (0x0060,  64),
    4: (0x0080, 128),
    5: (0x00A0, 250),
    6: (0x00C0, 475),
    7: (0x00E0, 860),
}

# Comparator disable (bits 4:0) — set COMP_QUE = 0b11 to disable
_COMP_DISABLE = 0x0003


class ADS1115:
    """
    Driver for the Texas Instruments ADS1115 16-bit I2C ADC.

    Uses single-shot conversion mode.  Shares an existing I2C bus
    object (e.g. the same bus as MAX30102).

    Args:
        i2c     (machine.I2C): Pre-configured I2C bus instance.
        address (int):         7-bit I2C address.  Default 0x48
                               (ADDR pin tied to GND).
        gain    (int|float):   PGA gain setting.  One of:
                               2/3, 1, 2, 4, 8, 16.
                               Default 1 (±4.096 V full-scale).
        data_rate (int):       Data rate index 0-7.  Default 4 (128 SPS).
    """

    def __init__(self, i2c, address=0x48, gain=1, data_rate=4):
        self._i2c = i2c
        self._addr = address

        if gain not in _PGA:
            raise ValueError("ADS1115: invalid gain {}; "
                             "use 2/3, 1, 2, 4, 8, or 16".format(gain))
        if data_rate not in _DR:
            raise ValueError("ADS1115: invalid data_rate {}; "
                             "use 0-7".format(data_rate))

        self._gain = gain
        self._pga_bits, self._fs_voltage = _PGA[gain]
        self._dr_bits, self._sps = _DR[data_rate]
        # Pre-compute conversion timeout (ms): 2× the conversion period + margin
        self._conv_timeout_ms = max(int(2000 / self._sps) + 5, 10)

        self._ready = False
        try:
            self._detect()
            self._ready = True
            print("[SOMNI][ADS1115] Initialised at 0x{:02X} "
                  "(gain={}, ±{}V, {}SPS).".format(
                      address, gain, self._fs_voltage, self._sps))
        except Exception as exc:
            print("[SOMNI][ADS1115] Init error at 0x{:02X}: {}".format(
                address, exc))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_sensor(self):
        """
        Check that the ADS1115 is present on the I2C bus.

        Returns:
            bool: True if the device responds, False otherwise.
        """
        try:
            self._detect()
            return True
        except Exception:
            return False

    def read_raw(self, channel=0):
        """
        Perform a single-shot conversion and return the raw 16-bit signed
        result.

        Args:
            channel (int): Input channel 0-3 (single-ended vs GND).

        Returns:
            int: Signed 16-bit ADC value (-32768 to +32767), or 0 on error.
        """
        if not self._ready:
            print("[SOMNI][ADS1115] read_raw: not initialised.")
            return 0
        if channel not in _MUX:
            print("[SOMNI][ADS1115] read_raw: invalid channel {}.".format(
                channel))
            return 0

        try:
            # Build 16-bit config word
            cfg = (_OS_START
                   | _MUX[channel]
                   | self._pga_bits
                   | _MODE_SINGLE
                   | self._dr_bits
                   | _COMP_DISABLE)

            # Write config to start conversion
            self._write_register(_REG_CONFIG, cfg)

            # Poll until conversion completes (OS bit reads back as 1)
            deadline = time.ticks_add(time.ticks_ms(), self._conv_timeout_ms)
            while True:
                status = self._read_register(_REG_CONFIG)
                if status & _OS_IDLE:
                    break
                if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                    print("[SOMNI][ADS1115] Conversion timeout on ch{}.".format(
                        channel))
                    return 0
                time.sleep_ms(1)

            # Read 16-bit signed result
            raw = self._read_register(_REG_CONVERSION)
            # Convert unsigned 16-bit to signed
            if raw >= 0x8000:
                raw -= 0x10000
            return raw

        except Exception as exc:
            print("[SOMNI][ADS1115] read_raw error: {}".format(exc))
            return 0

    def read_voltage(self, channel=0):
        """
        Perform a single-shot conversion and return the voltage in volts.

        Args:
            channel (int): Input channel 0-3.

        Returns:
            float: Voltage in volts, or 0.0 on error.
        """
        raw = self.read_raw(channel)
        return self.raw_to_voltage(raw)

    def raw_to_voltage(self, raw):
        """
        Convert a raw 16-bit signed ADC value to voltage.

        Args:
            raw (int): Signed 16-bit value from read_raw().

        Returns:
            float: Voltage in volts.
        """
        return raw * (self._fs_voltage / 32767.0)

    @property
    def ready(self):
        """True if the ADS1115 was successfully detected at init."""
        return self._ready

    @property
    def full_scale_voltage(self):
        """The full-scale voltage for the current PGA gain setting."""
        return self._fs_voltage

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _detect(self):
        """Verify the ADS1115 is present by reading the config register."""
        self._read_register(_REG_CONFIG)

    def _write_register(self, reg, value):
        """Write a 16-bit value to a register (big-endian)."""
        buf = struct.pack(">H", value)
        self._i2c.writeto(self._addr, bytes([reg]) + buf)

    def _read_register(self, reg):
        """Read a 16-bit unsigned value from a register (big-endian)."""
        self._i2c.writeto(self._addr, bytes([reg]))
        data = self._i2c.readfrom(self._addr, 2)
        return struct.unpack(">H", data)[0]
