"""
drivers/max30102.py — SOMNI‑Guard driver for the MAX30102 SpO₂/HR sensor.

The MAX30102 is a pulse‑oximetry and heart‑rate module from Maxim Integrated.
This driver communicates over I2C, configures the sensor for a modest
sampling rate suitable for sleep monitoring, and returns raw FIFO values
together with a simple educational SpO₂ / HR approximation.

⚠️  EDUCATIONAL APPROXIMATION ONLY — the SpO₂ and HR values produced here
    are derived from a simplified R‑ratio method and are NOT suitable for
    clinical diagnosis, treatment decisions, or any safety‑critical use.

Bug fix (v0.4)
--------------
The FIFO on the MAX30102 runs at 100 sps internally.  The sampler reads it
at only 1 Hz, so by the time read_fifo() is called the FIFO has long since
overflowed its 32‑sample depth and FIFO_ROLLOVER has wrapped the write
pointer back to equal the read pointer.  The original code interpreted
(wr_ptr == rd_ptr) as "no data", which caused every 1 Hz read to return
(None, None) and the no‑finger message to appear.

The fix reads the OVF_COUNTER register.  When overflow has occurred,
OVF_COUNTER > 0 and we seek the read pointer to the latest unread
sample (wr_ptr − 1) before reading, then clear OVF_COUNTER.

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

# ---------------------------------------------------------------------------
# LED amplitude (pulse current) for both LEDs.
# Each register step = 200 µA.
#   0x24 = 7.2 mA  (original — too low for some modules / skin tones)
#   0x3F = 12.6 mA (conservative sleep‑monitoring value)
#   0x7F = 25.4 mA (recommended — reliable across a wide range of users)
#   0xFF = 51.0 mA (maximum — only for very short spot‑checks)
#
# Increase this value if you still see "No finger detected" after the FIFO
# fix, especially with darker skin tones or higher melanin concentration.
# ---------------------------------------------------------------------------
_LED_AMPLITUDE = 0x7F   # 25.4 mA — reliable for most users

# Minimum IR count below which the sensor is treated as "no finger" present.
# With the FIFO fix in place and 25 mA LED current, a covered sensor reads
# 50 000–250 000.  An open sensor (no finger) reads < 1 000.
# The threshold is deliberately conservative so darker skin tones are not
# misclassified as "no finger".
_IR_NO_FINGER_THRESHOLD = 5_000


class MAX30102:
    """
    Driver for the MAX30102 SpO₂ and heart‑rate sensor module.

    Initialises the sensor for SpO₂ mode with settings suitable for sleep
    monitoring (25 mA LED current, 100 Hz internal sample rate, 16384 nA
    ADC range, 18‑bit resolution, 411 µs pulse width).

    v0.4 changes
    ~~~~~~~~~~~~
    * Fixed FIFO overflow handling in read_fifo() — the sensor samples at
      100 sps but the sampler only reads at 1 Hz.  By the time read_fifo()
      is called the FIFO has overflowed and the write pointer has wrapped
      to equal the read pointer, making the old code return (None, None)
      on every call.  The fix checks OVF_COUNTER and seeks to the latest
      sample when overflow is detected.
    * Increased LED amplitude from 0x24 (7.2 mA) to 0x7F (25.4 mA) for
      more reliable readings across diverse skin tones.
    * Lowered no‑finger threshold from 50 000 to 5 000 to avoid classifying
      valid low‑signal readings as "no finger".
    * Extended post‑reset delay from 10 ms to 50 ms for module stability.

    All I2C operations are wrapped in try/except; errors are logged with the
    ``[SOMNI][MAX30102]`` prefix and a safe sentinel is returned.

    Args:
        i2c  (machine.I2C): Configured I2C bus object.
        addr (int):         7‑bit I2C address of the MAX30102.
                            Defaults to 0x57.
    """

    def __init__(self, i2c, addr=0x57):
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
        - ADC range 16384 nA, sample rate 100 sps (internal), pulse width
          411 µs (18‑bit ADC resolution).  Register value = 0x67.
        - LED current 25.4 mA (amplitude 0x7F) — reliable across diverse
          skin tones and finger pressures.
        - FIFO: 1 sample per slot (no averaging), FIFO rollover enabled.

        Returns:
            None
        """
        # Hard‑reset the device
        if not self._write_reg(_REG_MODE_CONFIG, 0x40):
            print("[SOMNI][MAX30102] Reset failed; sensor may be absent.")
            return
        # 50 ms gives the module time to complete its internal POR sequence.
        # Some cheap breakout boards need more than the datasheet minimum.
        time.sleep_ms(50)

        # Clear any stale interrupt status flags
        self._read_reg(_REG_INT_STATUS1)
        self._read_reg(_REG_INT_STATUS2)

        # FIFO configuration:
        #   SMP_AVE     = 000 (no averaging, 1 sample per slot)
        #   FIFO_ROLLOVER_EN = 1 (overwrite oldest sample on overflow)
        #   FIFO_A_FULL = 0000
        #   → 0b0001_0000 = 0x10
        self._write_reg(_REG_FIFO_CONFIG, 0x10)

        # SpO₂ mode = 0x03 (Red LED + IR LED, two channels in FIFO)
        self._write_reg(_REG_MODE_CONFIG, 0x03)

        # SpO₂ ADC / sample rate / pulse width:
        #   SPO2_ADC_RGE [6:5] = 11 → 16384 nA full‑scale
        #   SPO2_SR      [4:2] = 001 → 100 samples/second
        #   LED_PW       [1:0] = 11 → 411 µs pulse (18‑bit ADC)
        #   → 0b0_11_001_11 = 0x67
        self._write_reg(_REG_SPO2_CONFIG, 0x67)

        # LED amplitudes — 25.4 mA (0x7F) for both Red and IR.
        self._write_reg(_REG_LED1_PA, _LED_AMPLITUDE)   # Red LED
        self._write_reg(_REG_LED2_PA, _LED_AMPLITUDE)   # IR LED

        # Reset FIFO pointers and overflow counter so we start from a
        # known state.
        self._write_reg(_REG_FIFO_WR_PTR, 0x00)
        self._write_reg(_REG_OVF_COUNTER, 0x00)
        self._write_reg(_REG_FIFO_RD_PTR, 0x00)

        # Small settling delay after full configuration
        time.sleep_ms(10)

        self._configured = True
        print("[SOMNI][MAX30102] Sensor configured "
              "(SpO₂ mode, LED={:.1f}mA, 100sps, 18‑bit).".format(
                  _LED_AMPLITUDE * 0.2))

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
        Read the latest sample from the MAX30102 FIFO.

        The sensor runs its internal ADC at 100 sps continuously.  When the
        FIFO is full (32 samples) and FIFO_ROLLOVER_EN is set, the write
        pointer wraps back to the current read pointer position.  At that
        moment (wr_ptr == rd_ptr) the old logic incorrectly reported zero
        samples and returned (None, None).

        This version also reads OVF_COUNTER.  When overflow has occurred,
        OVF_COUNTER is non‑zero and we seek the read pointer to the slot
        immediately before the write pointer (the most recent sample), clear
        OVF_COUNTER, then perform the read.  This always returns the freshest
        available data regardless of how long the FIFO has been running.

        Args:
            None

        Returns:
            tuple: (ir_raw: int, red_raw: int) on success,
                   (None, None) on error or genuinely empty FIFO.
        """
        try:
            # ── 1. Read all three FIFO pointer / status registers ───────────
            wr_data  = self._read_reg(_REG_FIFO_WR_PTR)
            rd_data  = self._read_reg(_REG_FIFO_RD_PTR)
            ovf_data = self._read_reg(_REG_OVF_COUNTER)

            if wr_data is None or rd_data is None or ovf_data is None:
                return (None, None)

            wr_ptr = wr_data[0]  & 0x1F
            rd_ptr = rd_data[0]  & 0x1F
            ovf    = ovf_data[0] & 0x1F

            # Number of unread samples (5‑bit wrapping subtraction)
            num_samples = (wr_ptr - rd_ptr) & 0x1F

            # ── 2. Determine whether there is data to read ──────────────────
            # If the FIFO overflowed, OVF_COUNTER > 0 even though
            # (wr_ptr - rd_ptr) == 0 appears to say the FIFO is empty.
            has_data = (num_samples > 0) or (ovf > 0)
            if not has_data:
                return (None, None)

            # ── 3. On overflow, seek to the latest available sample ─────────
            if ovf > 0:
                # Move read pointer to the slot just before write pointer
                # so the very next FIFO read returns the freshest sample.
                latest_ptr = (wr_ptr - 1) & 0x1F
                self._write_reg(_REG_FIFO_RD_PTR, latest_ptr)
                self._write_reg(_REG_OVF_COUNTER, 0x00)

            # ── 4. Read one sample: 6 bytes (3 Red + 3 IR) ─────────────────
            raw = self._read_reg(_REG_FIFO_DATA, _BYTES_PER_SAMPLE)
            if raw is None or len(raw) < _BYTES_PER_SAMPLE:
                return (None, None)

            # Each channel is 18‑bit, MSB first.
            # Byte layout per channel: [D17:D16 | D15:D8 | D7:D0]
            # Upper 6 bits of byte 0 are always zero in 18‑bit mode.
            # Mask 0x03 extracts bits 17:16 from byte 0.
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

        # No‑finger detection: IR below threshold means sensor is uncovered.
        # Threshold lowered from 50 000 → 5 000 so that low‑signal but valid
        # readings (darker skin tones, lighter finger pressure) are not
        # incorrectly reported as "no finger".
        if ir_raw < _IR_NO_FINGER_THRESHOLD:
            print("[SOMNI][MAX30102] No finger detected "
                  "(IR={}, threshold={}).".format(ir_raw, _IR_NO_FINGER_THRESHOLD))
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
