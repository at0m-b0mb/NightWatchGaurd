"""
drivers/max30102.py — SOMNI‑Guard driver for the MAX30102 SpO₂/HR sensor.

The MAX30102 is a pulse‑oximetry and heart‑rate module from Maxim Integrated.
This driver communicates over I2C, configures the sensor for a modest
sampling rate suitable for sleep monitoring, and returns raw FIFO values
together with a simple educational SpO₂ / HR approximation.

⚠️  EDUCATIONAL APPROXIMATION ONLY — the SpO₂ and HR values produced here
    are derived from a simplified R‑ratio method and are NOT suitable for
    clinical diagnosis, treatment decisions, or any safety‑critical use.

References
----------
- MAX30102 datasheet (Rev 3), Maxim Integrated.
- Jubran, A. (1999). Pulse oximetry. Critical Care, 3(2), R11–R17.

Educational prototype — not a clinically approved device.
"""

import time

# ---------------------------------------------------------------------------
# MAX30102 register map (subset used by this driver)
# ---------------------------------------------------------------------------
_REG_INT_STATUS1  = 0x00
_REG_INT_STATUS2  = 0x01
_REG_INT_ENABLE1  = 0x02
_REG_INT_ENABLE2  = 0x03
_REG_FIFO_WR_PTR  = 0x04
_REG_OVF_COUNTER  = 0x05
_REG_FIFO_RD_PTR  = 0x06
_REG_FIFO_DATA    = 0x07
_REG_FIFO_CONFIG  = 0x08
_REG_MODE_CONFIG  = 0x09
_REG_SPO2_CONFIG  = 0x0A
_REG_LED1_PA      = 0x0C   # Red LED pulse amplitude
_REG_LED2_PA      = 0x0D   # IR LED pulse amplitude
_REG_PART_ID      = 0xFF

# Expected part‑ID for MAX30102
_PART_ID_EXPECTED = 0x15

# Number of bytes per FIFO sample in SpO₂ mode (3 bytes Red + 3 bytes IR)
_BYTES_PER_SAMPLE = 6

# Minimum IR count below which the sensor is treated as "no finger" present
_IR_NO_FINGER_THRESHOLD = 50_000


class MAX30102:
    """
    Driver for the MAX30102 SpO₂ and heart‑rate sensor module.

    Initialises the sensor for SpO₂ mode with sensible defaults for sleep
    monitoring (low LED current, 100 Hz internal sample rate, 4096 ADC
    range).  All I2C operations are wrapped in try/except; errors are logged
    with the ``[SOMNI][MAX30102]`` prefix and a safe sentinel is returned.

    Args:
        i2c  (machine.I2C): Configured I2C bus object.
        addr (int):         7‑bit I2C address of the MAX30102.
                            Defaults to 0x57.
    """

    def __init__(self, i2c, addr=0x57):
        """
        Initialise the MAX30102 and configure it for SpO₂ mode.

        Args:
            i2c  (machine.I2C): Configured I2C bus object.
            addr (int):         Sensor I2C address.  Defaults to 0x57.

        Returns:
            None
        """
        self._i2c  = i2c
        self._addr = addr
        self._ir_buffer  = []   # rolling IR samples for HR estimation
        self._red_buffer = []   # rolling Red samples for SpO₂ estimation
        self._buffer_len = 100  # samples to accumulate before estimating
        self._configured = False
        self._configure()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _write_reg(self, reg, value):
        """
        Write one byte to a MAX30102 register.

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
            print("[SOMNI][MAX30102] write_reg error reg=0x{:02X}: {}".format(reg, exc))
            return False

    def _read_reg(self, reg, n=1):
        """
        Read n bytes from a MAX30102 register.

        Args:
            reg (int): Register address.
            n   (int): Number of bytes to read.

        Returns:
            bytes | None: Raw bytes on success, None on I2C error.
        """
        try:
            return self._i2c.readfrom_mem(self._addr, reg, n)
        except Exception as exc:
            print("[SOMNI][MAX30102] read_reg error reg=0x{:02X}: {}".format(reg, exc))
            return None

    def _configure(self):
        """
        Write configuration registers to put the MAX30102 into SpO₂ mode.

        Settings chosen for sleep monitoring:
        - SpO₂ mode (mode = 0x03).
        - ADC range 4096 nA, sample rate 100 sps (internal), pulse width 411 µs.
        - LED current ~7 mA (amplitude 0x24) — sufficient for skin contact.
        - FIFO: 32‑sample average disabled (average = 1), FIFO rollover enabled.

        Returns:
            None
        """
        # Reset the device first
        if not self._write_reg(_REG_MODE_CONFIG, 0x40):
            print("[SOMNI][MAX30102] Reset failed; sensor may be absent.")
            return
        time.sleep_ms(10)

        # FIFO configuration: SMP_AVE=1 (no averaging), FIFO_ROLLOVER_EN=1
        self._write_reg(_REG_FIFO_CONFIG, 0x10)

        # SpO₂ mode = 0x03
        self._write_reg(_REG_MODE_CONFIG, 0x03)

        # SpO₂ config: ADC range=4096 (bits[6:5]=11), SR=100 sps (bits[4:2]=001),
        # LED pulse width=411 µs (bits[1:0]=11) → 0b_11_001_11 = 0x67
        self._write_reg(_REG_SPO2_CONFIG, 0x67)

        # LED amplitudes (~7 mA each; 0x24 = 36 * 200 µA = 7.2 mA)
        self._write_reg(_REG_LED1_PA, 0x24)  # Red LED
        self._write_reg(_REG_LED2_PA, 0x24)  # IR LED

        # Reset FIFO pointers
        self._write_reg(_REG_FIFO_WR_PTR, 0x00)
        self._write_reg(_REG_OVF_COUNTER, 0x00)
        self._write_reg(_REG_FIFO_RD_PTR, 0x00)

        self._configured = True
        print("[SOMNI][MAX30102] Sensor configured (SpO₂ mode).")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_sensor(self):
        """
        Verify that the connected device is a MAX30102 by reading its part ID.

        Args:
            None

        Returns:
            bool: True if part ID matches 0x15, False otherwise.
        """
        data = self._read_reg(_REG_PART_ID)
        if data is None:
            print("[SOMNI][MAX30102] check_sensor: I2C read failed.")
            return False
        part_id = data[0]
        if part_id != _PART_ID_EXPECTED:
            print("[SOMNI][MAX30102] check_sensor: unexpected part ID 0x{:02X} "
                  "(expected 0x{:02X}).".format(part_id, _PART_ID_EXPECTED))
            return False
        print("[SOMNI][MAX30102] check_sensor: OK (part ID 0x{:02X}).".format(part_id))
        return True

    def read_fifo(self):
        """
        Read one sample from the MAX30102 FIFO.

        Returns the most recent IR and Red raw 18‑bit ADC counts.
        Does not raise exceptions; returns (None, None) on any error.

        Args:
            None

        Returns:
            tuple: (ir_raw: int, red_raw: int) on success,
                   (None, None) on error or empty FIFO.
        """
        try:
            # Check how many unread samples are in the FIFO
            wr_ptr_data = self._read_reg(_REG_FIFO_WR_PTR)
            rd_ptr_data = self._read_reg(_REG_FIFO_RD_PTR)
            if wr_ptr_data is None or rd_ptr_data is None:
                return (None, None)

            wr_ptr = wr_ptr_data[0] & 0x1F
            rd_ptr = rd_ptr_data[0] & 0x1F
            num_samples = (wr_ptr - rd_ptr) & 0x1F

            if num_samples == 0:
                return (None, None)

            # Read one sample (6 bytes: 3 Red + 3 IR)
            raw = self._read_reg(_REG_FIFO_DATA, _BYTES_PER_SAMPLE)
            if raw is None or len(raw) < _BYTES_PER_SAMPLE:
                return (None, None)

            # Each channel is 18‑bit, MSB first, packed into 3 bytes
            red_raw = ((raw[0] & 0x03) << 16) | (raw[1] << 8) | raw[2]
            ir_raw  = ((raw[3] & 0x03) << 16) | (raw[4] << 8) | raw[5]
            return (ir_raw, red_raw)

        except Exception as exc:
            print("[SOMNI][MAX30102] read_fifo error: {}".format(exc))
            return (None, None)

    def read_spo2_hr(self):
        """
        Read one FIFO sample and compute educational SpO₂ and HR estimates.

        ⚠️  EDUCATIONAL APPROXIMATION — the R‑ratio method used here is a
            simplified illustration.  The constants (a, b) are empirical
            approximations and are NOT derived from a clinical calibration.
            Do NOT use these values for any medical purpose.

        The R‑ratio is defined as:
            R = (AC_red / DC_red) / (AC_ir / DC_ir)

        For simplicity, this driver approximates AC and DC from a rolling
        buffer of raw counts.  When fewer than two samples are available
        it returns valid=False.

        Args:
            None

        Returns:
            dict: {
                "spo2"    : float | None,  # estimated SpO₂ % (non‑clinical)
                "hr"      : float | None,  # estimated HR bpm (non‑clinical)
                "ir_raw"  : int   | None,  # raw IR ADC count
                "red_raw" : int   | None,  # raw Red ADC count
                "valid"   : bool           # False if data is absent/suspect
            }
        """
        result = {"spo2": None, "hr": None, "ir_raw": None, "red_raw": None, "valid": False}

        ir_raw, red_raw = self.read_fifo()
        if ir_raw is None or red_raw is None:
            return result

        result["ir_raw"]  = ir_raw
        result["red_raw"] = red_raw

        # No‑finger detection: IR below threshold means sensor is uncovered
        if ir_raw < _IR_NO_FINGER_THRESHOLD:
            print("[SOMNI][MAX30102] No finger detected (IR={}).".format(ir_raw))
            return result

        # Accumulate rolling buffer for AC/DC estimation
        self._ir_buffer.append(ir_raw)
        self._red_buffer.append(red_raw)
        if len(self._ir_buffer) > self._buffer_len:
            self._ir_buffer  = self._ir_buffer[-self._buffer_len:]
            self._red_buffer = self._red_buffer[-self._buffer_len:]

        if len(self._ir_buffer) < 2:
            return result

        # DC component = mean of the buffer
        dc_ir  = sum(self._ir_buffer)  / len(self._ir_buffer)
        dc_red = sum(self._red_buffer) / len(self._red_buffer)

        if dc_ir == 0 or dc_red == 0:
            return result

        # AC component ≈ peak‑to‑peak of buffer (simplified; not true AC)
        ac_ir  = max(self._ir_buffer)  - min(self._ir_buffer)
        ac_red = max(self._red_buffer) - min(self._red_buffer)

        # R‑ratio (avoid division by zero)
        if ac_ir == 0 or dc_ir == 0:
            return result
        R = (ac_red / dc_red) / (ac_ir / dc_ir)

        # ⚠️  Empirical linear approximation — EDUCATIONAL ONLY.
        # A real calibration requires lab measurements against a reference
        # co‑oximeter across many subjects.
        # Typical textbook approximation: SpO₂ ≈ 110 − 25 × R
        spo2 = 110.0 - 25.0 * R
        spo2 = max(0.0, min(100.0, spo2))   # clamp to [0, 100]

        # HR estimation: count zero‑crossings of mean‑subtracted IR signal
        # over the buffer window (very approximate; sample rate matters).
        # With only 100 samples at 100 sps → ~1 s window → one beat estimate.
        # This is a placeholder — a proper implementation needs a longer
        # buffer and proper peak detection.
        crossings = 0
        mean_ir = dc_ir
        for i in range(1, len(self._ir_buffer)):
            prev = self._ir_buffer[i - 1] - mean_ir
            curr = self._ir_buffer[i]     - mean_ir
            if prev < 0 and curr >= 0:
                crossings += 1

        # Each positive zero‑crossing ≈ one heartbeat (very rough)
        window_s = len(self._ir_buffer) / 100.0   # assuming 100 sps internal
        hr = (crossings / window_s) * 60.0 if window_s > 0 else None

        # Plausibility clamp
        if hr is not None and (hr < 20 or hr > 300):
            hr = None

        result["spo2"]  = round(spo2, 1)
        result["hr"]    = round(hr, 1) if hr is not None else None
        result["valid"] = True
        return result
