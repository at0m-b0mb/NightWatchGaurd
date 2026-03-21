"""
sampler.py — SOMNI‑Guard sensor orchestration layer.

The SensorSampler class sits between the low‑level sensor drivers and the
application (main.py).  It:

1. Instantiates all three sensor drivers on a shared I2C bus.
2. Provides check_all_sensors() to verify hardware presence at start‑up.
3. Provides read_all() to take a synchronised snapshot across all sensors.
4. Drives a hardware timer loop (via machine.Timer) that calls a user
   callback at the appropriate rates:
   - Accelerometer: 10 Hz (every 100 ms).
   - SpO₂ and GSR:  1 Hz (subsampled from the 10 Hz base tick).

Design notes
------------
- The timer fires at ACCEL_RATE_HZ (10 Hz).  A counter divides this down
  to 1 Hz for SpO₂/GSR reads.
- All sensor reads inside the callback are wrapped in try/except so a
  failing sensor cannot crash the timer ISR.
- If a sensor's check_sensor() returns False at start‑up, sampling
  continues with that sensor returning valid=False on every call (fail‑soft).

Educational prototype — not a clinically approved device.
"""

import config
import utils
from drivers import MAX30102, ADXL345, GSRSensor

# Import machine.Timer only when running on real hardware
try:
    from machine import I2C, Timer
except ImportError:
    I2C    = None   # CPython stub
    Timer  = None   # CPython stub


class SensorSampler:
    """
    Orchestrates timing and data collection across all SOMNI‑Guard sensors.

    Instantiates MAX30102, ADXL345, and GSRSensor on a shared I2C bus (for
    MAX30102 and ADXL345) and an ADC pin (for GSR).  Provides a timer‑driven
    sampling loop that delivers data dictionaries to an application callback.

    Args:
        i2c    (machine.I2C): Configured I2C bus shared by MAX30102 & ADXL345.
        config (module):      The config module with pin/rate constants.
    """

    def __init__(self, i2c, cfg=None):
        """
        Instantiate all sensor drivers.

        Args:
            i2c (machine.I2C): I2C bus object (400 kHz, GP4/GP5).
            cfg (module):      Optional config module override; defaults to
                               the imported ``config`` module.

        Returns:
            None
        """
        self._cfg   = cfg if cfg is not None else config
        self._i2c   = i2c

        # Instantiate drivers — errors are caught inside each __init__
        self._max30102 = MAX30102(i2c, addr=self._cfg.MAX30102_ADDR)
        self._adxl345  = ADXL345(i2c,  addr=self._cfg.ADXL345_ADDR)
        self._gsr      = GSRSensor(adc_pin=self._cfg.GSR_ADC_PIN)

        # Timer state
        self._timer         = None
        self._tick_count    = 0   # counts 10 Hz ticks; resets at 10 → 1 Hz
        self._spo2_divisor  = self._cfg.ACCEL_RATE_HZ // self._cfg.SPO2_RATE_HZ
        self._callback      = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_all_sensors(self):
        """
        Run built‑in self‑tests on each sensor and log results.

        Sensors that fail the check are flagged but sampling continues
        (fail‑soft behaviour).

        Args:
            None

        Returns:
            dict: {
                "max30102": bool,   # True = sensor present & responding
                "adxl345":  bool,   # True = sensor present & responding
                "gsr":      bool,   # True = ADC initialised (no self‑test
                                    #        available for passive GSR)
            }
        """
        results = {
            "max30102": self._max30102.check_sensor(),
            "adxl345":  self._adxl345.check_sensor(),
            "gsr":      self._gsr._adc is not None,
        }
        print(
            "[SOMNI][SAMPLER] Sensor check — "
            "MAX30102:{max30102} ADXL345:{adxl345} GSR:{gsr}".format(**results)
        )
        return results

    def read_all(self):
        """
        Take a synchronised reading from all sensors and return a data dict.

        Accelerometer data is always read.  SpO₂ and GSR are read at the
        same moment for 1 Hz samples; this method always reads all three
        (the timer‑driven loop handles sub‑sampling).

        Args:
            None

        Returns:
            dict: {
                "timestamp_ms": int,
                "spo2":  { "spo2":float|None, "hr":float|None,
                           "ir_raw":int|None, "red_raw":int|None,
                           "valid":bool },
                "accel": { "x":float|None, "y":float|None,
                           "z":float|None, "valid":bool },
                "gsr":   { "raw":int, "voltage":float,
                           "conductance_us":float, "valid":bool },
            }
        """
        ts    = utils.get_timestamp()
        spo2  = self._safe_read(self._max30102.read_spo2_hr,
                                {"spo2": None, "hr": None,
                                 "ir_raw": None, "red_raw": None,
                                 "valid": False})
        accel = self._safe_read(self._adxl345.read_xyz,
                                {"x": None, "y": None,
                                 "z": None, "valid": False})
        gsr   = self._safe_read(self._gsr.read_conductance,
                                {"raw": 0, "voltage": 0.0,
                                 "conductance_us": 0.0, "valid": False})
        return {
            "timestamp_ms": ts,
            "spo2":         spo2,
            "accel":        accel,
            "gsr":          gsr,
        }

    def start_sampling_loop(self, callback):
        """
        Start the hardware timer loop that calls *callback* with sensor data.

        The timer fires every ``ACCEL_INTERVAL_MS`` milliseconds (100 ms at
        10 Hz).  On every tick the accelerometer is read.  Every tenth tick
        (1 Hz) SpO₂ and GSR are also read, and a full data dict is passed
        to *callback*.  On accelerometer‑only ticks a reduced dict with
        only "timestamp_ms" and "accel" is passed.

        Args:
            callback (callable): Function accepting one dict argument.
                                 Called from a Timer ISR — must be brief.

        Returns:
            None
        """
        if Timer is None:
            print("[SOMNI][SAMPLER] Timer not available (CPython?); "
                  "start_sampling_loop is a no‑op.")
            return

        self._callback  = callback
        self._tick_count = 0

        def _timer_cb(t):
            """Internal timer callback (ISR context)."""
            try:
                self._tick_count += 1
                ts    = utils.get_timestamp()
                accel = self._safe_read(
                    self._adxl345.read_xyz,
                    {"x": None, "y": None, "z": None, "valid": False},
                )

                if self._tick_count >= self._spo2_divisor:
                    # 1 Hz tick — read SpO₂ and GSR as well
                    self._tick_count = 0
                    spo2 = self._safe_read(
                        self._max30102.read_spo2_hr,
                        {"spo2": None, "hr": None,
                         "ir_raw": None, "red_raw": None, "valid": False},
                    )
                    gsr = self._safe_read(
                        self._gsr.read_conductance,
                        {"raw": 0, "voltage": 0.0,
                         "conductance_us": 0.0, "valid": False},
                    )
                    data = {
                        "timestamp_ms": ts,
                        "spo2":         spo2,
                        "accel":        accel,
                        "gsr":          gsr,
                    }
                else:
                    # 10 Hz accelerometer‑only tick
                    data = {
                        "timestamp_ms": ts,
                        "accel":        accel,
                    }

                if self._callback is not None:
                    self._callback(data)

            except Exception as exc:
                print("[SOMNI][SAMPLER] Timer callback error: {}".format(exc))

        # Initialise a periodic hardware timer (timer id=-1 for virtual timer
        # on RP2350, or 0 for a hardware timer — both work in MicroPython).
        self._timer = Timer(-1)
        self._timer.init(
            period=self._cfg.ACCEL_INTERVAL_MS,
            mode=Timer.PERIODIC,
            callback=_timer_cb,
        )
        print("[SOMNI][SAMPLER] Sampling loop started "
              "(accel@{}Hz, SpO2/GSR@{}Hz).".format(
                  self._cfg.ACCEL_RATE_HZ, self._cfg.SPO2_RATE_HZ))

    def stop(self):
        """
        Stop the hardware timer and release resources.

        Safe to call even if the timer was never started or already stopped.

        Args:
            None

        Returns:
            None
        """
        if self._timer is not None:
            try:
                self._timer.deinit()
                print("[SOMNI][SAMPLER] Sampling loop stopped.")
            except Exception as exc:
                print("[SOMNI][SAMPLER] stop() error: {}".format(exc))
            finally:
                self._timer = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_read(fn, fallback):
        """
        Call *fn* and return its result, or *fallback* on any exception.

        Args:
            fn       (callable): Zero‑argument function to call.
            fallback (any):      Value returned if fn raises an exception.

        Returns:
            any: Result of fn() or fallback.
        """
        try:
            return fn()
        except Exception as exc:
            print("[SOMNI][SAMPLER] _safe_read error: {}".format(exc))
            return fallback
