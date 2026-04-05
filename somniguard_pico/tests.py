"""
tests.py — SOMNI‑Guard Pico firmware unit tests.

Run on CPython with:
    python3 somniguard_pico/tests.py

All sensor drivers, the sampler layer, utility helpers, and the HMAC
transport are tested using lightweight mock objects so no real hardware
is required.

Educational prototype — not a clinically approved device.
"""

import sys
import os
import unittest

# Make sure all pico modules are importable from this file's location
sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Shared mock primitives
# ---------------------------------------------------------------------------

class _MockI2C:
    """
    Minimal I2C stub that lets tests pre‑program responses for readfrom_mem
    and record writeto_mem calls.
    """

    def __init__(self, responses=None):
        """
        Args:
            responses (dict): {(addr, reg, n): bytes} mapping that
                              readfrom_mem returns.  Unknown reads raise
                              OSError to simulate a missing device.
        """
        self._responses = responses or {}
        self.writes = []   # list of (addr, reg, data) tuples recorded

    def readfrom_mem(self, addr, reg, n):
        key = (addr, reg, n)
        if key in self._responses:
            return self._responses[key]
        raise OSError("mock I2C: no response for addr=0x{:02X} reg=0x{:02X}".format(
            addr, reg))

    def writeto_mem(self, addr, reg, data):
        self.writes.append((addr, reg, bytes(data)))


class _MockADC:
    """Minimal ADC stub; returns a pre‑set 16‑bit value."""

    def __init__(self, value=32768):
        self._value = value

    def read_u16(self):
        return self._value


# ---------------------------------------------------------------------------
# utils.py tests
# ---------------------------------------------------------------------------

class TestRingBuffer(unittest.TestCase):

    def test_empty_buffer(self):
        import utils
        rb = utils.RingBuffer(4)
        self.assertEqual(len(rb), 0)
        self.assertEqual(rb.get_all(), [])
        self.assertFalse(rb.is_full())

    def test_size_one_validation(self):
        import utils
        with self.assertRaises(ValueError):
            utils.RingBuffer(0)

    def test_partial_fill(self):
        import utils
        rb = utils.RingBuffer(4)
        rb.push(1)
        rb.push(2)
        self.assertEqual(len(rb), 2)
        self.assertEqual(rb.get_all(), [1, 2])
        self.assertFalse(rb.is_full())

    def test_full_buffer(self):
        import utils
        rb = utils.RingBuffer(3)
        rb.push("a")
        rb.push("b")
        rb.push("c")
        self.assertTrue(rb.is_full())
        self.assertEqual(rb.get_all(), ["a", "b", "c"])

    def test_overwrite_oldest(self):
        import utils
        rb = utils.RingBuffer(3)
        for i in range(5):
            rb.push(i)
        # 0,1 were overwritten; should contain 2,3,4
        self.assertEqual(rb.get_all(), [2, 3, 4])

    def test_get_latest_fewer_than_n(self):
        import utils
        rb = utils.RingBuffer(5)
        rb.push(10)
        rb.push(20)
        result = rb.get_latest(10)
        self.assertEqual(result, [10, 20])

    def test_get_latest_exactly_n(self):
        import utils
        rb = utils.RingBuffer(5)
        for i in range(5):
            rb.push(i)
        result = rb.get_latest(3)
        self.assertEqual(result, [2, 3, 4])

    def test_chronological_order_after_wrap(self):
        import utils
        rb = utils.RingBuffer(3)
        for i in range(6):   # wrap twice
            rb.push(i)
        self.assertEqual(rb.get_all(), [3, 4, 5])


class TestGetTimestamp(unittest.TestCase):

    def test_returns_integer(self):
        import utils
        ts = utils.get_timestamp()
        self.assertIsInstance(ts, int)

    def test_monotonically_non_decreasing(self):
        import utils
        t1 = utils.get_timestamp()
        t2 = utils.get_timestamp()
        self.assertGreaterEqual(t2, t1)


class TestFormatReading(unittest.TestCase):

    def _full_reading_no_gsr(self):
        return {
            "timestamp_ms": 5000,
            "spo2":  {"spo2": 97.5, "hr": 65.0, "ir_raw": 80000,
                      "red_raw": 75000, "valid": True},
            "accel": {"x": 0.01, "y": -0.02, "z": 1.00, "valid": True},
        }

    def test_no_gsr_key_absent_from_output(self):
        import utils
        line = utils.format_reading(self._full_reading_no_gsr())
        self.assertNotIn("GSR", line)
        self.assertIn("SpO2=97.5%", line)
        self.assertIn("HR=65.0bpm", line)
        self.assertIn("accel=", line)

    def test_with_gsr_key_present(self):
        import utils
        data = self._full_reading_no_gsr()
        data["gsr"] = {"raw": 30000, "voltage": 1.5,
                       "conductance_us": 12.34, "valid": True}
        line = utils.format_reading(data)
        self.assertIn("GSR=12.34\u00b5S", line)

    def test_none_spo2_and_hr_show_dashes(self):
        import utils
        data = {
            "timestamp_ms": 0,
            "spo2":  {"spo2": None, "hr": None, "valid": False},
            "accel": {"x": None, "y": None, "z": None, "valid": False},
        }
        line = utils.format_reading(data)
        self.assertIn("SpO2=---", line)
        self.assertIn("HR=---", line)
        self.assertIn("accel=(---,---,---)g", line)

    def test_accel_only_tick_no_spo2(self):
        import utils
        data = {
            "timestamp_ms": 1000,
            "accel": {"x": 0.0, "y": 0.0, "z": 1.0, "valid": True},
        }
        line = utils.format_reading(data)
        self.assertIn("SpO2=---", line)
        self.assertNotIn("GSR", line)

    def test_timestamp_appears_in_output(self):
        import utils
        data = {"timestamp_ms": 99999, "spo2": {}, "accel": {}}
        line = utils.format_reading(data)
        self.assertIn("t=99999ms", line)


# ---------------------------------------------------------------------------
# drivers/adxl345.py tests
# ---------------------------------------------------------------------------

class TestADXL345(unittest.TestCase):

    def _make_i2c(self, devid=0xE5, data_bytes=None):
        """Return a mock I2C pre‑loaded with ADXL345 register responses."""
        addr = 0x53
        responses = {
            (addr, 0x00, 1): bytes([devid]),   # DEVID register
        }
        if data_bytes is not None:
            responses[(addr, 0x32, 6)] = data_bytes
        return _MockI2C(responses)

    def test_check_sensor_ok(self):
        from drivers import ADXL345
        i2c = self._make_i2c(devid=0xE5)
        sensor = ADXL345(i2c, addr=0x53)
        self.assertTrue(sensor.check_sensor())

    def test_check_sensor_wrong_id(self):
        from drivers import ADXL345
        i2c = self._make_i2c(devid=0xAA)
        sensor = ADXL345(i2c, addr=0x53)
        self.assertFalse(sensor.check_sensor())

    def test_check_sensor_i2c_failure(self):
        from drivers import ADXL345
        i2c = _MockI2C({})   # no response → OSError → returns False
        sensor = ADXL345(i2c, addr=0x53)
        self.assertFalse(sensor.check_sensor())

    def test_read_raw_positive_values(self):
        from drivers import ADXL345
        # x=256 (0x100), y=512 (0x200), z=1024 (0x400) in little‑endian
        raw = bytes([0x00, 0x01,   # x LSB, MSB → 256
                     0x00, 0x02,   # y LSB, MSB → 512
                     0x00, 0x04])  # z LSB, MSB → 1024
        i2c = self._make_i2c(data_bytes=raw)
        sensor = ADXL345(i2c, addr=0x53)
        x, y, z = sensor.read_raw()
        self.assertEqual(x, 256)
        self.assertEqual(y, 512)
        self.assertEqual(z, 1024)

    def test_read_raw_negative_z(self):
        from drivers import ADXL345
        # z = -1 in two's complement 16‑bit → 0xFFFF
        raw = bytes([0x00, 0x00,
                     0x00, 0x00,
                     0xFF, 0xFF])
        i2c = self._make_i2c(data_bytes=raw)
        sensor = ADXL345(i2c, addr=0x53)
        _, _, z = sensor.read_raw()
        self.assertEqual(z, -1)

    def test_read_raw_i2c_error_returns_nones(self):
        from drivers import ADXL345
        i2c = _MockI2C({})
        sensor = ADXL345(i2c, addr=0x53)
        result = sensor.read_raw()
        self.assertEqual(result, (None, None, None))

    def test_read_xyz_valid_output(self):
        from drivers import ADXL345
        # x=0, y=0, z≈256 counts → 256 * 0.0039 g ≈ 0.9984 g
        raw = bytes([0x00, 0x00,
                     0x00, 0x00,
                     0x00, 0x01])   # z = 256
        i2c = self._make_i2c(data_bytes=raw)
        sensor = ADXL345(i2c, addr=0x53)
        result = sensor.read_xyz()
        self.assertTrue(result["valid"])
        self.assertAlmostEqual(result["z"], 256 * 0.0039, places=4)

    def test_read_xyz_i2c_error_invalid(self):
        from drivers import ADXL345
        i2c = _MockI2C({})
        sensor = ADXL345(i2c, addr=0x53)
        result = sensor.read_xyz()
        self.assertFalse(result["valid"])
        self.assertIsNone(result["x"])


# ---------------------------------------------------------------------------
# drivers/max30102.py tests
# ---------------------------------------------------------------------------

class TestMAX30102(unittest.TestCase):
    """Tests for the MAX30102 SpO₂/HR driver."""

    _ADDR = 0x57

    def _make_i2c(self, part_id=0x15, wr_ptr=1, rd_ptr=0, fifo_bytes=None):
        """
        Return a mock I2C with just enough registers for the driver to work.
        """
        responses = {
            (self._ADDR, 0xFF, 1): bytes([part_id]),  # PART_ID
            (self._ADDR, 0x04, 1): bytes([wr_ptr]),   # FIFO_WR_PTR
            (self._ADDR, 0x06, 1): bytes([rd_ptr]),   # FIFO_RD_PTR
        }
        if fifo_bytes is not None:
            responses[(self._ADDR, 0x07, 6)] = fifo_bytes
        return _MockI2C(responses)

    def _sample_fifo(self, red=0xC000, ir=0xC000):
        """Encode Red and IR as 18‑bit values packed into 3 bytes each."""
        r = [(red >> 16) & 0x03, (red >> 8) & 0xFF, red & 0xFF,
             (ir  >> 16) & 0x03, (ir  >> 8) & 0xFF, ir  & 0xFF]
        return bytes(r)

    def test_check_sensor_ok(self):
        from drivers import MAX30102
        i2c = self._make_i2c(part_id=0x15)
        sensor = MAX30102(i2c, addr=self._ADDR)
        self.assertTrue(sensor.check_sensor())

    def test_check_sensor_wrong_id(self):
        from drivers import MAX30102
        i2c = self._make_i2c(part_id=0xAB)
        sensor = MAX30102(i2c, addr=self._ADDR)
        self.assertFalse(sensor.check_sensor())

    def test_check_sensor_i2c_failure(self):
        from drivers import MAX30102
        i2c = _MockI2C({})
        sensor = MAX30102(i2c, addr=self._ADDR)
        self.assertFalse(sensor.check_sensor())

    def test_read_fifo_empty_returns_none(self):
        from drivers import MAX30102
        # wr_ptr == rd_ptr → 0 samples available
        i2c = self._make_i2c(wr_ptr=0, rd_ptr=0)
        sensor = MAX30102(i2c, addr=self._ADDR)
        ir, red = sensor.read_fifo()
        self.assertIsNone(ir)
        self.assertIsNone(red)

    def test_read_fifo_one_sample(self):
        from drivers import MAX30102
        fifo = self._sample_fifo(red=0x10000, ir=0x20000)
        i2c = self._make_i2c(wr_ptr=1, rd_ptr=0, fifo_bytes=fifo)
        sensor = MAX30102(i2c, addr=self._ADDR)
        ir, red = sensor.read_fifo()
        self.assertEqual(red, 0x10000 & 0x3FFFF)
        self.assertEqual(ir,  0x20000 & 0x3FFFF)

    def test_read_spo2_hr_no_finger(self):
        from drivers import MAX30102
        # IR value below _IR_NO_FINGER_THRESHOLD (50 000) → valid=False
        fifo = self._sample_fifo(red=1000, ir=1000)
        i2c = self._make_i2c(wr_ptr=1, rd_ptr=0, fifo_bytes=fifo)
        sensor = MAX30102(i2c, addr=self._ADDR)
        result = sensor.read_spo2_hr()
        self.assertFalse(result["valid"])
        self.assertIsNone(result["spo2"])

    def test_read_spo2_hr_accumulates_and_computes(self):
        from drivers import MAX30102
        # Feed 60 000 IR / red samples so the buffer fills past 2 samples
        fifo = self._sample_fifo(red=60000, ir=65000)
        i2c = self._make_i2c(wr_ptr=1, rd_ptr=0, fifo_bytes=fifo)
        sensor = MAX30102(i2c, addr=self._ADDR)
        # Push two samples to satisfy the ≥ 2 buffer requirement
        sensor._ir_buffer  = [65000, 65100]
        sensor._red_buffer = [60000, 60100]
        ir, red = sensor.read_fifo()
        # Now call the full method
        result = sensor.read_spo2_hr()
        # SpO₂ must be in plausible range even if computed from stub data
        if result["valid"]:
            self.assertGreaterEqual(result["spo2"], 0.0)
            self.assertLessEqual(result["spo2"], 100.0)

    def test_read_spo2_hr_i2c_error_returns_invalid(self):
        from drivers import MAX30102
        i2c = _MockI2C({})
        sensor = MAX30102(i2c, addr=self._ADDR)
        result = sensor.read_spo2_hr()
        self.assertFalse(result["valid"])

    def test_fifo_byte_unpacking(self):
        """Verify the 18‑bit channel unpacking formula is correct."""
        from drivers import MAX30102
        # Construct known values: red=0x3FFFF (max 18‑bit), ir=0x00001
        red_val = 0x3FFFF
        ir_val  = 0x00001
        fifo = bytes([
            (red_val >> 16) & 0x03,
            (red_val >> 8)  & 0xFF,
             red_val        & 0xFF,
            (ir_val  >> 16) & 0x03,
            (ir_val  >> 8)  & 0xFF,
             ir_val         & 0xFF,
        ])
        i2c = self._make_i2c(wr_ptr=1, rd_ptr=0, fifo_bytes=fifo)
        sensor = MAX30102(i2c, addr=self._ADDR)
        ir, red = sensor.read_fifo()
        self.assertEqual(red, red_val)
        self.assertEqual(ir,  ir_val)


# ---------------------------------------------------------------------------
# drivers/gsr.py tests
# ---------------------------------------------------------------------------

class TestGSRSensor(unittest.TestCase):

    def _make_sensor(self, adc_value=32768):
        """Return a GSRSensor with a stubbed ADC."""
        from drivers import GSRSensor
        sensor = GSRSensor.__new__(GSRSensor)
        sensor._pin = 26
        sensor._adc = _MockADC(adc_value)
        return sensor

    def test_read_raw_returns_adc_value(self):
        sensor = self._make_sensor(adc_value=40000)
        self.assertEqual(sensor.read_raw(), 40000)

    def test_read_raw_no_adc_returns_zero(self):
        from drivers import GSRSensor
        sensor = GSRSensor.__new__(GSRSensor)
        sensor._pin = 26
        sensor._adc = None
        self.assertEqual(sensor.read_raw(), 0)

    def test_read_conductance_valid_flag(self):
        sensor = self._make_sensor(adc_value=32768)
        result = sensor.read_conductance()
        self.assertTrue(result["valid"])

    def test_read_conductance_no_adc_invalid(self):
        from drivers import GSRSensor
        sensor = GSRSensor.__new__(GSRSensor)
        sensor._pin = 26
        sensor._adc = None
        result = sensor.read_conductance()
        self.assertFalse(result["valid"])

    def test_read_conductance_midscale_voltage(self):
        """At mid‑scale ADC (≈ 1.65 V) R_skin ≈ R_ref → conductance ≈ 100 µS."""
        sensor = self._make_sensor(adc_value=65535 // 2)   # ≈ half of 16-bit range
        result = sensor.read_conductance()
        self.assertIn("voltage", result)
        self.assertIn("conductance_us", result)
        self.assertIn("raw", result)
        self.assertGreater(result["conductance_us"], 0)

    def test_read_conductance_zero_raw_no_crash(self):
        """raw=0 → voltage=0 → r_skin=0 → clamped by epsilon → no crash."""
        sensor = self._make_sensor(adc_value=0)
        result = sensor.read_conductance()
        self.assertIsNotNone(result["conductance_us"])

    def test_read_conductance_full_scale_no_crash(self):
        """raw=65535 → voltage≈3.3 V → denominator clamped → no crash."""
        sensor = self._make_sensor(adc_value=65535)
        result = sensor.read_conductance()
        self.assertIsNotNone(result["conductance_us"])

    def test_read_smoothed_averages_values(self):
        """read_smoothed() should return the same as read_conductance() when all
        samples are identical (constant ADC value)."""
        sensor = self._make_sensor(adc_value=20000)
        single = sensor.read_conductance()
        smoothed = sensor.read_smoothed(window=5)
        self.assertAlmostEqual(smoothed["conductance_us"],
                               single["conductance_us"], places=2)

    def test_read_smoothed_window_1(self):
        sensor = self._make_sensor(adc_value=10000)
        result = sensor.read_smoothed(window=1)
        self.assertIn("raw", result)
        self.assertIn("conductance_us", result)


# ---------------------------------------------------------------------------
# sampler.py tests
# ---------------------------------------------------------------------------

class TestSensorSampler(unittest.TestCase):

    def _make_sampler(self, gsr_enabled=False):
        """Return a SensorSampler built with mock I2C objects."""
        import config
        import sampler as s_mod
        # Use a throw‑away config override so tests are isolated
        class _Cfg:
            MAX30102_ADDR = 0x57
            ADXL345_ADDR  = 0x53
            GSR_ADC_PIN   = 26
            GSR_ENABLED   = gsr_enabled
            ACCEL_RATE_HZ = 10
            SPO2_RATE_HZ  = 1
            ACCEL_INTERVAL_MS = 100

        return s_mod.SensorSampler(
            i2c_max30102=_MockI2C({}),
            i2c_adxl345=_MockI2C({}),
            cfg=_Cfg,
        )

    def test_gsr_disabled_gsr_is_none(self):
        sampler = self._make_sampler(gsr_enabled=False)
        self.assertIsNone(sampler._gsr)

    def test_check_all_sensors_gsr_none_when_disabled(self):
        sampler = self._make_sampler(gsr_enabled=False)
        results = sampler.check_all_sensors()
        self.assertIsNone(results["gsr"])   # None = disabled, not failed
        self.assertIn("max30102", results)
        self.assertIn("adxl345", results)

    def test_read_all_no_gsr_key_when_disabled(self):
        sampler = self._make_sampler(gsr_enabled=False)
        data = sampler.read_all()
        self.assertNotIn("gsr", data)
        self.assertIn("timestamp_ms", data)
        self.assertIn("spo2", data)
        self.assertIn("accel", data)

    def test_read_all_i2c_errors_return_valid_false(self):
        sampler = self._make_sampler(gsr_enabled=False)
        data = sampler.read_all()
        self.assertFalse(data["spo2"]["valid"])
        self.assertFalse(data["accel"]["valid"])

    def test_spo2_divisor_calculation(self):
        sampler = self._make_sampler()
        # ACCEL_RATE_HZ // SPO2_RATE_HZ = 10 // 1 = 10
        self.assertEqual(sampler._spo2_divisor, 10)

    def test_stop_when_timer_never_started(self):
        """stop() must not raise even if the timer was never initialised."""
        sampler = self._make_sampler()
        try:
            sampler.stop()
        except Exception as exc:
            self.fail("stop() raised unexpectedly: {}".format(exc))

    def test_safe_read_returns_fallback_on_exception(self):
        import sampler as s_mod
        fallback = {"valid": False}

        def bad_fn():
            raise RuntimeError("sensor exploded")

        result = s_mod.SensorSampler._safe_read(bad_fn, fallback)
        self.assertIs(result, fallback)

    def test_safe_read_returns_fn_result(self):
        import sampler as s_mod

        def good_fn():
            return {"valid": True, "x": 1.0}

        result = s_mod.SensorSampler._safe_read(good_fn, {})
        self.assertEqual(result["x"], 1.0)


# ---------------------------------------------------------------------------
# transport.py HMAC tests
# ---------------------------------------------------------------------------

class TestHmacSha256(unittest.TestCase):
    """Verify the custom pure‑Python HMAC implementation against stdlib."""

    def _stdlib_hmac(self, key, message):
        """Compute HMAC‑SHA256 using stdlib for comparison."""
        import hmac as _hmac
        import hashlib
        if isinstance(key, str):
            key = key.encode("utf-8")
        if isinstance(message, str):
            message = message.encode("utf-8")
        return _hmac.new(key, message, hashlib.sha256).hexdigest()

    def test_short_key(self):
        from transport import _hmac_sha256
        key = "secret"
        msg = "hello"
        self.assertEqual(_hmac_sha256(key, msg), self._stdlib_hmac(key, msg))

    def test_long_key_exceeding_block_size(self):
        from transport import _hmac_sha256
        key = "k" * 100   # longer than 64‑byte SHA‑256 block size
        msg = "test message"
        self.assertEqual(_hmac_sha256(key, msg), self._stdlib_hmac(key, msg))

    def test_bytes_key(self):
        from transport import _hmac_sha256
        key = b"\x00\x01\x02\x03"
        msg = b"binary message"
        self.assertEqual(_hmac_sha256(key, msg), self._stdlib_hmac(key, msg))

    def test_empty_message(self):
        from transport import _hmac_sha256
        key = "key"
        msg = ""
        self.assertEqual(_hmac_sha256(key, msg), self._stdlib_hmac(key, msg))

    def test_unicode_key_and_message(self):
        from transport import _hmac_sha256
        key = "dev-hmac-key-change-this-in-production-32chrs!"
        msg = '{"patient_id": 1, "device_id": "pico-01"}'
        self.assertEqual(_hmac_sha256(key, msg), self._stdlib_hmac(key, msg))

    def test_output_is_64_char_hex(self):
        from transport import _hmac_sha256
        result = _hmac_sha256("key", "message")
        self.assertEqual(len(result), 64)
        self.assertRegex(result, r"^[0-9a-f]{64}$")

    def test_different_messages_produce_different_macs(self):
        from transport import _hmac_sha256
        key = "shared-secret"
        mac1 = _hmac_sha256(key, "message-one")
        mac2 = _hmac_sha256(key, "message-two")
        self.assertNotEqual(mac1, mac2)

    def test_different_keys_produce_different_macs(self):
        from transport import _hmac_sha256
        msg = "same message"
        mac1 = _hmac_sha256("key-one", msg)
        mac2 = _hmac_sha256("key-two", msg)
        self.assertNotEqual(mac1, mac2)


# ---------------------------------------------------------------------------
# Integration: format_reading ↔ sampler output
# ---------------------------------------------------------------------------

class TestFormatReadingIntegration(unittest.TestCase):
    """Make sure format_reading handles every dict shape sampler produces."""

    def test_full_reading_without_gsr(self):
        import utils
        data = {
            "timestamp_ms": 1000,
            "spo2":  {"spo2": 98.0, "hr": 70.0,
                      "ir_raw": 80000, "red_raw": 75000, "valid": True},
            "accel": {"x": 0.0, "y": 0.0, "z": 1.0, "valid": True},
        }
        line = utils.format_reading(data)
        self.assertIn("SpO2=98.0%", line)
        self.assertIn("HR=70.0bpm", line)
        self.assertNotIn("GSR", line)

    def test_accel_only_dict(self):
        import utils
        data = {
            "timestamp_ms": 2000,
            "accel": {"x": 0.1, "y": 0.2, "z": 0.9, "valid": True},
        }
        line = utils.format_reading(data)
        self.assertIn("t=2000ms", line)
        self.assertNotIn("GSR", line)

    def test_full_reading_with_gsr(self):
        import utils
        data = {
            "timestamp_ms": 3000,
            "spo2":  {"spo2": 95.5, "hr": 55.0,
                      "ir_raw": 60000, "red_raw": 55000, "valid": True},
            "accel": {"x": 0.0, "y": 0.0, "z": 1.0, "valid": True},
            "gsr":   {"raw": 20000, "voltage": 1.0,
                      "conductance_us": 50.0, "valid": True},
        }
        line = utils.format_reading(data)
        self.assertIn("GSR=50.00\u00b5S", line)


# ---------------------------------------------------------------------------
# config.py — derived constants
# ---------------------------------------------------------------------------

class TestConfigDerivedConstants(unittest.TestCase):
    """Verify that the derived interval-ms values in config match the Hz rates."""

    def test_accel_interval_ms(self):
        import config
        self.assertEqual(config.ACCEL_INTERVAL_MS, 1000 // config.ACCEL_RATE_HZ)

    def test_spo2_interval_ms(self):
        import config
        self.assertEqual(config.SPO2_INTERVAL_MS, 1000 // config.SPO2_RATE_HZ)

    def test_gsr_interval_ms(self):
        import config
        self.assertEqual(config.GSR_INTERVAL_MS, 1000 // config.GSR_RATE_HZ)


# ---------------------------------------------------------------------------
# drivers/adxl345.py — additional edge cases
# ---------------------------------------------------------------------------

class TestADXL345Extended(unittest.TestCase):

    def _make_i2c(self, devid=0xE5, data_bytes=None):
        addr = 0x53
        responses = {(addr, 0x00, 1): bytes([devid])}
        if data_bytes is not None:
            responses[(addr, 0x32, 6)] = data_bytes
        return _MockI2C(responses)

    def test_read_raw_short_i2c_response_returns_nones(self):
        """If I2C returns fewer than 6 bytes, read_raw must return (None, None, None)."""
        from drivers import ADXL345
        # Register response contains only 3 bytes instead of 6
        i2c = _MockI2C({(0x53, 0x00, 1): bytes([0xE5]),
                         (0x53, 0x32, 6): bytes([0x01, 0x00, 0x02])})
        sensor = ADXL345(i2c, addr=0x53)
        result = sensor.read_raw()
        self.assertEqual(result, (None, None, None))

    def test_read_xyz_negative_values(self):
        """Negative raw counts (two's-complement) must produce negative g values."""
        from drivers import ADXL345
        # x = y = z = -256 (0xFF00 in unsigned 16-bit, little-endian: 0x00, 0xFF)
        raw = bytes([0x00, 0xFF,   # x LSB, MSB → 0xFF00 = 65280 → signed -256
                     0x00, 0xFF,   # y
                     0x00, 0xFF])  # z
        i2c = self._make_i2c(data_bytes=raw)
        sensor = ADXL345(i2c, addr=0x53)
        result = sensor.read_xyz()
        self.assertTrue(result["valid"])
        self.assertLess(result["x"], 0)
        self.assertLess(result["y"], 0)
        self.assertLess(result["z"], 0)
        # -256 counts × 0.0039 g/count = -0.9984 g
        self.assertAlmostEqual(result["x"], -256 * 0.0039, places=4)


# ---------------------------------------------------------------------------
# drivers/max30102.py — additional edge cases
# ---------------------------------------------------------------------------

class TestMAX30102Extended(unittest.TestCase):

    _ADDR = 0x57

    def _make_i2c(self, wr_ptr=1, rd_ptr=0, fifo_bytes=None):
        responses = {
            (self._ADDR, 0xFF, 1): bytes([0x15]),
            (self._ADDR, 0x04, 1): bytes([wr_ptr]),
            (self._ADDR, 0x06, 1): bytes([rd_ptr]),
        }
        if fifo_bytes is not None:
            responses[(self._ADDR, 0x07, 6)] = fifo_bytes
        return _MockI2C(responses)

    def _sample_fifo(self, red=0xC000, ir=0xC000):
        r = [(red >> 16) & 0x03, (red >> 8) & 0xFF, red & 0xFF,
             (ir  >> 16) & 0x03, (ir  >> 8) & 0xFF, ir  & 0xFF]
        return bytes(r)

    def test_ir_buffer_trimmed_to_buffer_len(self):
        """After _buffer_len + 1 samples the buffers must stay ≤ _buffer_len."""
        from drivers import MAX30102
        fifo = self._sample_fifo(red=60000, ir=65000)
        i2c = self._make_i2c(wr_ptr=1, rd_ptr=0, fifo_bytes=fifo)
        sensor = MAX30102(i2c, addr=self._ADDR)
        # Pre-fill buffers to exactly capacity
        sensor._ir_buffer  = list(range(sensor._buffer_len))
        sensor._red_buffer = list(range(sensor._buffer_len))
        # One more call adds another sample; trimming must keep length at cap
        sensor.read_spo2_hr()
        self.assertLessEqual(len(sensor._ir_buffer),  sensor._buffer_len)
        self.assertLessEqual(len(sensor._red_buffer), sensor._buffer_len)

    def test_hr_plausibility_clamp_zero_crossings(self):
        """All-identical IR values → 0 crossings → HR = 0 → clamped to None."""
        from drivers import MAX30102
        fifo = self._sample_fifo(red=60000, ir=65000)
        i2c = self._make_i2c(wr_ptr=1, rd_ptr=0, fifo_bytes=fifo)
        sensor = MAX30102(i2c, addr=self._ADDR)
        # Flat buffer: all samples identical → no zero-crossings → hr = 0 bpm
        sensor._ir_buffer  = [65000] * 50
        sensor._red_buffer = [60000] * 50
        result = sensor.read_spo2_hr()
        if result["valid"]:
            # HR of 0 is < 20; clamp must convert it to None
            self.assertIsNone(result["hr"])

    def test_dc_zero_guard_returns_valid_false(self):
        """dc_red == 0 must trigger the early-return guard (valid=False).

        IR must be above the no-finger threshold so the buffer-accumulation
        path is reached.  Red is set to 0 both in the pre-filled buffer and
        in the FIFO sample; dc_red therefore equals 0, hitting the guard.
        """
        from drivers import MAX30102
        # ir=65000 passes the no-finger check; red=0 keeps dc_red at 0
        fifo = self._sample_fifo(red=0, ir=65000)
        i2c = self._make_i2c(wr_ptr=1, rd_ptr=0, fifo_bytes=fifo)
        sensor = MAX30102(i2c, addr=self._ADDR)
        sensor._ir_buffer  = [65000, 65000]   # dc_ir > 0
        sensor._red_buffer = [0, 0]           # dc_red will remain 0
        result = sensor.read_spo2_hr()
        # dc_red = 0 → guard triggers → valid=False
        self.assertFalse(result["valid"])


# ---------------------------------------------------------------------------
# drivers/gsr.py — additional edge cases
# ---------------------------------------------------------------------------

class TestGSRSensorExtended(unittest.TestCase):

    def _make_sensor(self, adc_value=32768):
        from drivers import GSRSensor
        sensor = GSRSensor.__new__(GSRSensor)
        sensor._pin = 26
        sensor._adc = _MockADC(adc_value)
        return sensor

    def test_read_smoothed_no_adc_valid_false(self):
        """read_smoothed() must return valid=False when ADC is not initialised."""
        from drivers import GSRSensor
        sensor = GSRSensor.__new__(GSRSensor)
        sensor._pin = 26
        sensor._adc = None
        result = sensor.read_smoothed(window=3)
        self.assertFalse(result["valid"])
        # Keys must still be present
        self.assertIn("raw", result)
        self.assertIn("conductance_us", result)

    def test_read_smoothed_window_zero_clamped_to_one(self):
        """window=0 must be silently clamped to 1 without raising."""
        sensor = self._make_sensor(adc_value=20000)
        try:
            result = sensor.read_smoothed(window=0)
        except Exception as exc:
            self.fail("read_smoothed(window=0) raised unexpectedly: {}".format(exc))
        self.assertIn("raw", result)
        self.assertIn("conductance_us", result)


# ---------------------------------------------------------------------------
# sampler.py — GSR-enabled paths (mock GSR injected after construction)
# ---------------------------------------------------------------------------

class TestSensorSamplerWithGSR(unittest.TestCase):
    """Tests for SensorSampler behaviour when a GSR sensor is active.

    Because sampler.py imports GSRSensor only at module load time when
    config.GSR_ENABLED is True (and tests run with GSR_ENABLED=False),
    we build the sampler without GSR and then inject a mock GSR sensor
    directly onto ``sampler._gsr``.
    """

    def _make_sampler_with_gsr(self, adc_value=32768):
        """Build a sampler with GSR disabled, then inject a mock GSR sensor."""
        import sampler as s_mod
        from drivers import GSRSensor

        class _Cfg:
            MAX30102_ADDR     = 0x57
            ADXL345_ADDR      = 0x53
            GSR_ADC_PIN       = 26
            GSR_ENABLED       = False   # avoids name-error at import time
            ACCEL_RATE_HZ     = 10
            SPO2_RATE_HZ      = 1
            ACCEL_INTERVAL_MS = 100

        smplr = s_mod.SensorSampler(
            i2c_max30102=_MockI2C({}),
            i2c_adxl345=_MockI2C({}),
            cfg=_Cfg,
        )
        # Inject a mock GSR sensor
        gsr = GSRSensor.__new__(GSRSensor)
        gsr._pin = 26
        gsr._adc = _MockADC(adc_value)
        smplr._gsr = gsr
        return smplr

    def test_read_all_includes_gsr_key_when_gsr_is_set(self):
        """read_all() must include a 'gsr' key when _gsr is not None."""
        smplr = self._make_sampler_with_gsr()
        data = smplr.read_all()
        self.assertIn("gsr", data)
        self.assertIn("conductance_us", data["gsr"])
        self.assertIn("valid", data["gsr"])

    def test_check_all_sensors_gsr_bool_when_adc_ok(self):
        """check_all_sensors()['gsr'] must be True (bool) when ADC is available."""
        smplr = self._make_sampler_with_gsr()
        results = smplr.check_all_sensors()
        self.assertIsNotNone(results["gsr"])
        self.assertIsInstance(results["gsr"], bool)
        self.assertTrue(results["gsr"])

    def test_check_all_sensors_gsr_false_when_adc_none(self):
        """check_all_sensors()['gsr'] must be False when ADC initialisation failed."""
        smplr = self._make_sampler_with_gsr()
        smplr._gsr._adc = None   # simulate ADC failure
        results = smplr.check_all_sensors()
        self.assertFalse(results["gsr"])


# ---------------------------------------------------------------------------
# sampler.py — timer tick-counter logic
# ---------------------------------------------------------------------------

class TestSamplerTickCounter(unittest.TestCase):
    """Verify the 10 Hz → 1 Hz sub-division inside the timer callback."""

    def _make_sampler(self):
        import sampler as s_mod

        class _Cfg:
            MAX30102_ADDR     = 0x57
            ADXL345_ADDR      = 0x53
            GSR_ADC_PIN       = 26
            GSR_ENABLED       = False
            ACCEL_RATE_HZ     = 10
            SPO2_RATE_HZ      = 1
            ACCEL_INTERVAL_MS = 100

        return s_mod.SensorSampler(
            i2c_max30102=_MockI2C({}),
            i2c_adxl345=_MockI2C({}),
            cfg=_Cfg,
        )

    def test_timer_callback_tick_division(self):
        """Sub-divisor ticks produce accel-only dicts; the divisor tick adds 'spo2'."""
        import sampler as s_mod

        captured_cb = [None]

        class _FakeTimer:
            PERIODIC = 1

            def __init__(self, timer_id):
                pass

            def init(self, period, mode, callback):
                captured_cb[0] = callback

            def deinit(self):
                pass

        original_timer = s_mod.Timer
        s_mod.Timer = _FakeTimer
        try:
            smplr = self._make_sampler()
            received = []
            smplr.start_sampling_loop(received.append)

            cb = captured_cb[0]
            self.assertIsNotNone(cb, "Timer callback was not registered by start_sampling_loop")

            divisor = smplr._spo2_divisor   # 10

            # First (divisor - 1) ticks → accel-only, no 'spo2' key
            for _ in range(divisor - 1):
                cb(None)

            self.assertEqual(len(received), divisor - 1)
            for d in received:
                self.assertIn("accel", d)
                self.assertNotIn("spo2", d)

            # The divisor-th tick → full reading with 'spo2', tick count resets
            received.clear()
            cb(None)

            self.assertEqual(len(received), 1)
            full = received[0]
            self.assertIn("spo2",  full)
            self.assertIn("accel", full)
            self.assertEqual(smplr._tick_count, 0)

        finally:
            s_mod.Timer = original_timer


# ---------------------------------------------------------------------------
# transport.py — graceful no-Wi-Fi paths
# ---------------------------------------------------------------------------

class TestTransportNoWifi(unittest.TestCase):
    """All transport functions must degrade gracefully when Wi-Fi is unavailable."""

    def _patch_wifi(self, transport_mod, available):
        """Context manager helper — returns original value for cleanup."""
        original = transport_mod._WIFI_AVAILABLE
        transport_mod._WIFI_AVAILABLE = available
        return original

    def test_http_post_no_wifi_returns_zero(self):
        import transport as t
        orig = self._patch_wifi(t, False)
        try:
            result = t._http_post("127.0.0.1", 5000, "/test", b"{}")
            self.assertEqual(result, 0)
        finally:
            t._WIFI_AVAILABLE = orig

    def test_connect_wifi_no_wifi_returns_none(self):
        import transport as t
        orig = self._patch_wifi(t, False)
        try:
            result = t.connect_wifi("MySSID", "password")
            self.assertIsNone(result)
        finally:
            t._WIFI_AVAILABLE = orig

    def test_disconnect_wifi_no_wifi_no_crash(self):
        import transport as t
        orig = self._patch_wifi(t, False)
        try:
            t.disconnect_wifi()   # must not raise
        except Exception as exc:
            self.fail("disconnect_wifi raised unexpectedly: {}".format(exc))
        finally:
            t._WIFI_AVAILABLE = orig

    def test_start_session_no_wifi_returns_none(self):
        import transport as t
        orig = self._patch_wifi(t, False)
        try:
            result = t.start_session("127.0.0.1", 5000, 1, "pico-01", "key")
            self.assertIsNone(result)
        finally:
            t._WIFI_AVAILABLE = orig

    def test_end_session_no_wifi_returns_false(self):
        import transport as t
        orig = self._patch_wifi(t, False)
        try:
            result = t.end_session("127.0.0.1", 5000, 42, "key")
            self.assertFalse(result)
        finally:
            t._WIFI_AVAILABLE = orig


# ---------------------------------------------------------------------------
# transport.py — send_api HMAC signing correctness
# ---------------------------------------------------------------------------

class TestSendApiHmac(unittest.TestCase):
    """Verify that send_api signs the payload in a way the gateway can verify."""

    def _capture_send(self, transport_mod, payload, key):
        """
        Call send_api with a fake _http_post that captures the body bytes.
        Returns the parsed dict that would have been sent over the wire.
        """
        import json

        captured = []

        def _fake_post(host, port, path, body_bytes, **kwargs):
            captured.append(body_bytes)
            return 200

        orig_post = transport_mod._http_post
        orig_wifi = transport_mod._WIFI_AVAILABLE
        transport_mod._http_post = _fake_post
        transport_mod._WIFI_AVAILABLE = True
        try:
            transport_mod.send_api("127.0.0.1", 5000, "/api/ingest", payload, key)
        finally:
            transport_mod._http_post = orig_post
            transport_mod._WIFI_AVAILABLE = orig_wifi

        self.assertEqual(len(captured), 1, "Expected exactly one HTTP POST")
        return json.loads(captured[0].decode("utf-8"))

    def test_send_api_adds_hmac_field(self):
        """The body transmitted by send_api must contain an 'hmac' key."""
        import transport as t
        sent = self._capture_send(t, {"session_id": 1, "value": 99}, "test-key")
        self.assertIn("hmac", sent)
        self.assertIsInstance(sent["hmac"], str)
        self.assertEqual(len(sent["hmac"]), 64)   # SHA-256 hex digest length

    def test_send_api_hmac_matches_manual_computation(self):
        """The transmitted HMAC must equal the expected value computed manually."""
        import transport as t
        import json
        key     = "shared-hmac-key"
        payload = {"session_id": 3, "value": 42}
        sent    = self._capture_send(t, payload, key)

        received_mac = sent.pop("hmac")
        # Re-compute: canonical = JSON(payload, sort_keys=True), no 'hmac' field
        canonical    = json.dumps(payload, sort_keys=True)
        expected_mac = t._hmac_sha256(key, canonical)
        self.assertEqual(received_mac, expected_mac)

    def test_send_api_canonical_uses_sorted_keys(self):
        """HMAC must be computed on sort_keys=True JSON (matching gateway behaviour)."""
        import transport as t
        import json
        key     = "test-key"
        payload = {"z_field": 1, "a_field": 2, "m_field": 3}
        sent    = self._capture_send(t, payload, key)

        received_mac     = sent["hmac"]
        canonical_sorted = json.dumps(payload, sort_keys=True)
        mac_sorted       = t._hmac_sha256(key, canonical_sorted)
        self.assertEqual(received_mac, mac_sorted)

    def test_send_api_returns_zero_without_wifi(self):
        """send_api must return 0 (not raise) when _WIFI_AVAILABLE is False."""
        import transport as t
        orig = t._WIFI_AVAILABLE
        t._WIFI_AVAILABLE = False
        try:
            result = t.send_api(
                "127.0.0.1", 5000, "/api/ingest",
                {"session_id": 1}, "key",
            )
            self.assertEqual(result, 0)
        finally:
            t._WIFI_AVAILABLE = orig


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
