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

# ---------------------------------------------------------------------------
# Internal sample rate (must match the SPO2_SR field written to register 0x0A
# in _configure() — currently 0x67 → 100 sps).
# ---------------------------------------------------------------------------
_SENSOR_SPS = 100

# Rolling buffer length used for SpO₂ AC/DC and HR peak detection.
# 6 seconds at 100 sps gives ~4–6 cardiac cycles in the buffer at typical
# sleep heart rates, which makes the median-of-intervals HR estimate
# substantially more stable than the 4-second window used in v0.4.
_BUFFER_LEN = _SENSOR_SPS * 6   # 600 samples

# Plausibility window for HR.  Anything outside this is treated as a noise
# artefact and the sample is dropped (returned as None) rather than reported.
_HR_PLAUSIBLE_MIN_BPM = 30
_HR_PLAUSIBLE_MAX_BPM = 200

# Refractory period between heartbeat peaks, expressed as an inter-peak
# distance in samples.  At 100 sps, an HR of 200 bpm gives ~3.3 beats/s,
# i.e. ~30 samples between peaks; we reject any peak detected closer than
# that to the previous one to avoid double-counting the systolic notch.
_MIN_PEAK_INTERVAL_SAMPLES = (60 * _SENSOR_SPS) // _HR_PLAUSIBLE_MAX_BPM

# Width of the moving-average smoother applied to the IR-PPG before
# peak detection.  5 samples = 50 ms at 100 sps, narrow enough to keep
# the systolic upstroke intact and wide enough to kill the single-sample
# quantisation noise that was making peak detection over-trigger.
_SMOOTH_WINDOW = 5

# EMA coefficient applied to the *output* HR value across reads (each
# read produces an HR estimate from the 6-second buffer; we then smooth
# those estimates over time).  alpha=0.3 means the newest estimate
# counts for 30 %, the previous smoothed value 70 % — visibly steadier
# numbers without lagging real changes by more than a few seconds.
_HR_EMA_ALPHA = 0.3


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
        # Rolling buffers truly sampled at 100 sps (filled by draining the
        # FIFO on every read).  Previously these were filled at 1 Hz which
        # caused the HR calculation to over-count crossings by 100×.
        self._ir_buffer  = []
        self._red_buffer = []
        self._buffer_len = _BUFFER_LEN
        # Output-side smoothers — see _HR_EMA_ALPHA above.  These persist
        # across calls so HR readings drift toward a stable value rather
        # than flicker between bandpass artefacts.
        self._hr_ema   = None
        self._spo2_ema = None
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

    def read_fifo_all(self):
        """
        Drain ALL unread samples from the MAX30102 FIFO.

        This is the method ``read_spo2_hr()`` should use whenever the sensor
        is being polled at less than the internal 100 sps rate (which is
        always — even our 1 Hz sleep-monitor cadence).  Pulling every sample
        on each call lets the rolling buffer be a true 100 sps time series,
        which is what the HR / SpO₂ math actually expects.

        FIFO behaviour
        --------------
        * Internal ADC pushes a new sample every 10 ms (100 sps).
        * FIFO depth is 32 slots.
        * If we are called less often than 320 ms apart, the FIFO will have
          overflowed at least once and the OVF_COUNTER will be > 0.  In that
          case the 32 slots all contain valid data — they are just the
          *most recent* 32 samples (older ones were dropped by the rollover
          logic we enabled in FIFO_CONFIG).  Read all 32.
        * If we are called more often than 320 ms apart, only
          ``(wr_ptr − rd_ptr) mod 32`` slots are unread.  Read just those.

        Returns:
            list[tuple[int, int]]: list of (ir_raw, red_raw) tuples in
                                   chronological order.  Empty list on
                                   error or genuinely empty FIFO.
        """
        samples = []
        try:
            wr_data  = self._read_reg(_REG_FIFO_WR_PTR)
            rd_data  = self._read_reg(_REG_FIFO_RD_PTR)
            ovf_data = self._read_reg(_REG_OVF_COUNTER)
            if wr_data is None or rd_data is None or ovf_data is None:
                return samples

            wr_ptr = wr_data[0]  & 0x1F
            rd_ptr = rd_data[0]  & 0x1F
            ovf    = ovf_data[0] & 0x1F

            if ovf > 0:
                # FIFO is full of the most-recent 32 samples.  Reset rd_ptr
                # to wr_ptr so the next read returns the OLDEST surviving
                # sample first, then read all 32.  Clear OVF afterwards.
                self._write_reg(_REG_FIFO_RD_PTR, wr_ptr)
                self._write_reg(_REG_OVF_COUNTER, 0x00)
                num_samples = 32
            else:
                num_samples = (wr_ptr - rd_ptr) & 0x1F

            if num_samples == 0:
                return samples

            # Bulk read: 6 bytes per sample.  Reading them in one I2C burst
            # is much faster than one sample at a time, especially over the
            # 400 kHz bus this driver uses.
            burst = self._read_reg(_REG_FIFO_DATA, num_samples * _BYTES_PER_SAMPLE)
            if burst is None or len(burst) < num_samples * _BYTES_PER_SAMPLE:
                return samples

            for i in range(num_samples):
                base = i * _BYTES_PER_SAMPLE
                red = ((burst[base + 0] & 0x03) << 16) | (burst[base + 1] << 8) | burst[base + 2]
                ir  = ((burst[base + 3] & 0x03) << 16) | (burst[base + 4] << 8) | burst[base + 5]
                samples.append((ir, red))

            return samples

        except Exception as exc:
            print("[SOMNI][MAX30102] read_fifo_all error: {}".format(exc))
            return []

    @staticmethod
    def _moving_average(buf, w):
        """Sliding-window mean over a list, pure-Python, O(n).

        Edge samples (the first and last ``w//2``) are returned unmodified
        so the buffer length is preserved.  This is a deliberately simple
        FIR low-pass — sufficient to kill single-sample quantisation noise
        ahead of peak detection without distorting the systolic upstroke.
        """
        n = len(buf)
        if w <= 1 or n < w:
            return list(buf)
        half = w // 2
        out  = [0.0] * n
        for i in range(half):
            out[i] = buf[i]
            out[n - 1 - i] = buf[n - 1 - i]
        window_sum = sum(buf[0:w])
        out[half] = window_sum / w
        for i in range(half + 1, n - half):
            window_sum += buf[i + half] - buf[i - half - 1]
            out[i] = window_sum / w
        return out

    @staticmethod
    def _median(values):
        """Median of an iterable (pure Python, no statistics import)."""
        s = sorted(values)
        n = len(s)
        if n == 0:
            return 0.0
        mid = n // 2
        if n % 2 == 1:
            return float(s[mid])
        return (s[mid - 1] + s[mid]) / 2.0

    def _detect_hr_bpm(self, ir_buf):
        """
        Estimate heart rate from a 100 sps IR-PPG buffer.

        v0.5 changes (this revision)
        ----------------------------
        * Pre-smooth the IR signal with a 5-sample moving average before
          peak detection.  Single-sample quantisation glitches were the
          dominant source of HR jitter at sleep heart rates (low AC
          amplitude relative to ADC LSB).
        * Use the *median* of inter-peak intervals instead of the mean.
          One spurious peak — common at low SNR — used to drag the
          mean-based HR by 5–10 bpm; the median ignores it entirely.
        * Output EMA smoothing happens one level up in ``read_spo2_hr``
          so the user-visible HR is a temporal average across reads,
          not just within a single 6-second window.

        Algorithm
        ---------
        1. Smooth ir_buf with a 50 ms moving average.
        2. Subtract the rolling mean.
        3. Find local maxima above 40 % of the buffer's max deviation
           that are also at least ``_MIN_PEAK_INTERVAL_SAMPLES`` from the
           previous peak (refractory period rejects the dicrotic notch).
        4. HR = 60 / median(inter-peak interval in seconds).
        5. Return None if HR is outside the plausible range or fewer
           than two peaks were found.
        """
        n = len(ir_buf)
        # Need at least 2 cardiac cycles at the slowest plausible HR.
        # 30 bpm = 2 s/beat → 4 s minimum window for two beats at the slowest end.
        if n < _SENSOR_SPS * 2:
            return None

        # Smooth ir_buf to suppress single-sample quantisation glitches
        # without distorting the systolic peak shape.
        ir = self._moving_average(ir_buf, _SMOOTH_WINDOW)

        mean_ir = sum(ir) / n
        max_dev = max(ir) - mean_ir
        if max_dev <= 0:
            return None

        # ── Minimum AC/DC ratio guard ─────────────────────────────────────
        # A finger-on PPG has AC ≈ 1-5 % of DC.  Anything below ~0.3 % is
        # almost certainly Gaussian sensor noise on a covered-but-still
        # finger, ambient-light intrusion, or motion glitch — not a real
        # cardiac waveform.  Treat these as "no plausible HR" and return
        # None, matching the way the SpO₂ path bails out earlier when the
        # IR raw count is below _IR_NO_FINGER_THRESHOLD.
        if mean_ir > 0 and (max_dev / mean_ir) < 0.003:
            return None

        # 40 % is conservative for a clean PPG; loosens slightly with motion.
        peak_threshold = 0.4 * max_dev

        # 50 ms (= 5 samples at 100 sps) is wider than any noise spike but
        # narrower than the ~100 ms systolic upstroke we want to preserve.
        _PEAK_NEIGHBOURHOOD = 5

        peaks = []
        last_peak = -10_000

        for i in range(_PEAK_NEIGHBOURHOOD, n - _PEAK_NEIGHBOURHOOD):
            v_curr = ir[i] - mean_ir
            if v_curr < peak_threshold:
                continue
            if (i - last_peak) < _MIN_PEAK_INTERVAL_SAMPLES:
                continue
            is_peak = True
            for dj in range(1, _PEAK_NEIGHBOURHOOD + 1):
                if ir[i] < ir[i - dj] or ir[i] < ir[i + dj]:
                    is_peak = False
                    break
            if is_peak:
                peaks.append(i)
                last_peak = i

        if len(peaks) < 2:
            return None

        # Median inter-peak interval, in samples — robust to a single
        # spurious peak that mean-of-intervals would over-weight.
        intervals = [peaks[k] - peaks[k - 1] for k in range(1, len(peaks))]
        median_interval_samples = self._median(intervals)
        if median_interval_samples <= 0:
            return None

        hr = 60.0 * _SENSOR_SPS / median_interval_samples
        if hr < _HR_PLAUSIBLE_MIN_BPM or hr > _HR_PLAUSIBLE_MAX_BPM:
            return None
        return hr

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

        # ── Drain ALL new FIFO samples — buffer becomes a true 100 sps series ──
        new_samples = self.read_fifo_all()
        if not new_samples:
            return result

        # The most-recent sample is reported as the "raw" reading for telemetry.
        latest_ir, latest_red = new_samples[-1]
        result["ir_raw"]  = latest_ir
        result["red_raw"] = latest_red

        # No-finger detection on the latest sample.  When uncovered, also
        # *clear* the rolling buffer so old waveform data does not poison
        # the next reading once the user re-applies their finger.
        if latest_ir < _IR_NO_FINGER_THRESHOLD:
            self._ir_buffer  = []
            self._red_buffer = []
            # Reset EMA state too — otherwise the first reading after a
            # re-applied finger would be averaged with stale numbers from
            # before the user removed it, which the user would see as
            # "weird first reading then it settles".
            self._hr_ema   = None
            self._spo2_ema = None
            print("[SOMNI][MAX30102] No finger detected "
                  "(IR={}, threshold={}).".format(latest_ir, _IR_NO_FINGER_THRESHOLD))
            return result

        # Append every new sample (true 100 sps), trim to fixed window length.
        for ir_s, red_s in new_samples:
            self._ir_buffer.append(ir_s)
            self._red_buffer.append(red_s)
        if len(self._ir_buffer) > self._buffer_len:
            self._ir_buffer  = self._ir_buffer[-self._buffer_len:]
            self._red_buffer = self._red_buffer[-self._buffer_len:]

        # Need at least 2 seconds of true 100 sps data before any estimate
        # is meaningful — that is two cardiac cycles even at 60 bpm.
        if len(self._ir_buffer) < _SENSOR_SPS * 2:
            return result

        # ── AC / DC over the most-recent 1-second window ───────────────────
        # Using only the last second (one cardiac cycle at typical adult
        # rates) makes AC reflect the actual pulsatile amplitude rather
        # than the swing of any low-frequency baseline drift across the
        # whole 6-second buffer.  Pre-smoothed with the same MA filter
        # used by the HR path so AC isn't inflated by sub-systolic noise.
        recent_n = min(_SENSOR_SPS, len(self._ir_buffer))
        ir_recent  = self._moving_average(self._ir_buffer[-recent_n:],
                                          _SMOOTH_WINDOW)
        red_recent = self._moving_average(self._red_buffer[-recent_n:],
                                          _SMOOTH_WINDOW)

        dc_ir  = sum(ir_recent)  / len(ir_recent)
        dc_red = sum(red_recent) / len(red_recent)
        if dc_ir == 0 or dc_red == 0:
            return result

        ac_ir  = max(ir_recent)  - min(ir_recent)
        ac_red = max(red_recent) - min(red_recent)
        if ac_ir == 0:
            return result

        # ── R-ratio + calibrated SpO₂ polynomial ──────────────────────────
        # Source: Maxim app note for the MAX30102 reference design
        #   SpO₂ ≈ -45.060·R² + 30.354·R + 94.845
        # This is a substantially better fit at the 90–100 % range than
        # the linear "110 − 25R" we used in v0.4, which over-predicted
        # SpO₂ by 2–4 percentage points across most users.  Educational
        # only — not a clinical calibration.
        R = (ac_red / dc_red) / (ac_ir / dc_ir)
        spo2 = -45.060 * R * R + 30.354 * R + 94.845
        # Physiologically plausible window (anything outside is almost
        # certainly a bad reading from motion or low SNR).
        spo2 = max(70.0, min(100.0, spo2))

        # ── HR via peak detection (median-based, smoothed signal) ─────────
        hr = self._detect_hr_bpm(self._ir_buffer)

        # ── Output EMA smoothing — kills second-to-second jitter ──────────
        if hr is not None:
            self._hr_ema = (hr if self._hr_ema is None
                            else _HR_EMA_ALPHA * hr
                                 + (1.0 - _HR_EMA_ALPHA) * self._hr_ema)
            hr_out = self._hr_ema
        else:
            hr_out = None

        self._spo2_ema = (spo2 if self._spo2_ema is None
                          else _HR_EMA_ALPHA * spo2
                               + (1.0 - _HR_EMA_ALPHA) * self._spo2_ema)
        spo2_out = self._spo2_ema

        result["spo2"]  = round(spo2_out, 1)
        result["hr"]    = round(hr_out, 1) if hr_out is not None else None
        result["valid"] = True
        return result
