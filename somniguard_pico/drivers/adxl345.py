"""
drivers/adxl345.py — SOMNI‑Guard driver for the ADXL345 accelerometer.

The ADXL345 is a ±16g, 3‑axis MEMS accelerometer from Analog Devices.
This driver configures the device for ±2g range at a low output data rate
and converts raw counts to g‑units using the datasheet sensitivity value.

All I2C operations are wrapped in try/except.  Errors are logged with the
``[SOMNI][ADXL345]`` prefix and safe sentinel values are returned so the
rest of the firmware can continue operating with degraded data.

References
----------
- ADXL345 datasheet (Rev F), Analog Devices.

Educational prototype — not a clinically approved device.
"""

# ---------------------------------------------------------------------------
# ADXL345 register map (subset used by this driver)
# ---------------------------------------------------------------------------
_REG_DEVID       = 0x00   # Device ID — should read 0xE5
_REG_BW_RATE     = 0x2C   # Data rate and power mode control
_REG_POWER_CTL   = 0x2D   # Power‑saving features control
_REG_DATA_FORMAT = 0x31   # Data format control (range, resolution)
_REG_DATAX0      = 0x32   # X‑axis data 0 (LSB)
_REG_DATAX1      = 0x33   # X‑axis data 1 (MSB)
_REG_DATAY0      = 0x34
_REG_DATAY1      = 0x35
_REG_DATAZ0      = 0x36
_REG_DATAZ1      = 0x37

_DEVID_EXPECTED  = 0xE5   # Expected device ID per ADXL345 datasheet

# Sensitivity for ±2g range (full resolution disabled): 3.9 mg/LSB
# Source: ADXL345 datasheet Table 1.
_SCALE_G = 0.0039         # g per raw count


class ADXL345:
    """
    Driver for the ADXL345 3‑axis digital accelerometer.

    Configures the ADXL345 for:
    - ±2g measurement range.
    - ~50 Hz hardware output data rate (BW_RATE = 0x09), which gives
      comfortable anti‑aliasing headroom when subsampled to 10 Hz in
      the sampler layer.
    - Measurement mode (POWER_CTL bit 3 = 1).

    Args:
        i2c  (machine.I2C): Configured I2C bus object.
        addr (int):         7‑bit I2C address of the ADXL345.
                            Defaults to 0x53 (SDO tied low).
    """

    def __init__(self, i2c, addr=0x53):
        """
        Initialise the ADXL345 and place it in measurement mode.

        Args:
            i2c  (machine.I2C): Configured I2C bus object.
            addr (int):         Sensor I2C address.  Defaults to 0x53.

        Returns:
            None
        """
        self._i2c  = i2c
        self._addr = addr
        self._configured = False
        self._configure()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _write_reg(self, reg, value):
        """
        Write one byte to an ADXL345 register.

        Args:
            reg   (int): Register address.
            value (int): Byte value to write.

        Returns:
            bool: True on success, False on I2C error.
        """
        try:
            self._i2c.writeto_mem(self._addr, reg, bytes([value]))
            return True
        except Exception as exc:
            print("[SOMNI][ADXL345] write_reg error reg=0x{:02X}: {}".format(reg, exc))
            return False

    def _read_reg(self, reg, n=1):
        """
        Read n bytes from an ADXL345 register.

        Args:
            reg (int): Register address.
            n   (int): Number of bytes to read.

        Returns:
            bytes | None: Raw bytes on success, None on I2C error.
        """
        try:
            return self._i2c.readfrom_mem(self._addr, reg, n)
        except Exception as exc:
            print("[SOMNI][ADXL345] read_reg error reg=0x{:02X}: {}".format(reg, exc))
            return None

    def _configure(self):
        """
        Configure the ADXL345 for sleep‑monitoring operation.

        Sets:
        - BW_RATE  = 0x09 (50 Hz output data rate, normal power mode).
        - DATA_FORMAT = 0x00 (±2g range, 10‑bit, right‑justified).
        - POWER_CTL = 0x08 (measurement mode, no sleep/auto‑sleep).

        Returns:
            None
        """
        ok = True
        # Output data rate: 50 Hz (code 0x09 per datasheet Table 7)
        ok &= self._write_reg(_REG_BW_RATE, 0x09)
        # ±2g range, 10‑bit resolution (DATA_FORMAT = 0x00)
        ok &= self._write_reg(_REG_DATA_FORMAT, 0x00)
        # Enter measurement mode
        ok &= self._write_reg(_REG_POWER_CTL, 0x08)

        if ok:
            self._configured = True
            print("[SOMNI][ADXL345] Sensor configured (±2g, 50 Hz ODR, measurement mode).")
        else:
            print("[SOMNI][ADXL345] Configuration incomplete; sensor may be absent.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_sensor(self):
        """
        Verify device identity by reading the DEVID register (0x00).

        Args:
            None

        Returns:
            bool: True if DEVID == 0xE5, False otherwise.
        """
        data = self._read_reg(_REG_DEVID)
        if data is None:
            print("[SOMNI][ADXL345] check_sensor: I2C read failed.")
            return False
        devid = data[0]
        if devid != _DEVID_EXPECTED:
            print("[SOMNI][ADXL345] check_sensor: unexpected DEVID 0x{:02X} "
                  "(expected 0x{:02X}).".format(devid, _DEVID_EXPECTED))
            return False
        print("[SOMNI][ADXL345] check_sensor: OK (DEVID 0x{:02X}).".format(devid))
        return True

    def read_raw(self):
        """
        Read the six DATAX/Y/Z registers and return 16‑bit signed counts.

        The ADXL345 stores each axis in two consecutive registers
        (LSB first).  This method reads all six bytes in one burst
        and assembles them into signed 16‑bit integers.

        Args:
            None

        Returns:
            tuple: (x_raw: int, y_raw: int, z_raw: int) on success,
                   (None, None, None) on I2C error.
        """
        try:
            data = self._read_reg(_REG_DATAX0, 6)
            if data is None or len(data) < 6:
                return (None, None, None)

            # Assemble little‑endian 16‑bit signed values
            x = (data[1] << 8) | data[0]
            y = (data[3] << 8) | data[2]
            z = (data[5] << 8) | data[4]

            # Convert unsigned 16‑bit to signed (two's complement)
            if x > 32767:
                x -= 65536
            if y > 32767:
                y -= 65536
            if z > 32767:
                z -= 65536

            return (x, y, z)

        except Exception as exc:
            print("[SOMNI][ADXL345] read_raw error: {}".format(exc))
            return (None, None, None)

    def read_xyz(self):
        """
        Read acceleration and convert to g‑units.

        Uses the fixed sensitivity of 3.9 mg/LSB for the ±2g range
        (ADXL345 datasheet Table 1).

        Args:
            None

        Returns:
            dict: {
                "x"     : float | None,  # X‑axis acceleration in g
                "y"     : float | None,  # Y‑axis acceleration in g
                "z"     : float | None,  # Z‑axis acceleration in g
                "valid" : bool           # False if any read failed
            }
        """
        x_raw, y_raw, z_raw = self.read_raw()
        if x_raw is None:
            return {"x": None, "y": None, "z": None, "valid": False}

        return {
            "x":     round(x_raw * _SCALE_G, 4),
            "y":     round(y_raw * _SCALE_G, 4),
            "z":     round(z_raw * _SCALE_G, 4),
            "valid": True,
        }
