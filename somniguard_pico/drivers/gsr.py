"""
drivers/gsr.py — SOMNI‑Guard driver for the GSR (galvanic skin response) sensor.

The GSR sensor is a simple resistive element placed across a voltage divider
with a known reference resistor.  The ADC reads the voltage at the midpoint
and we compute skin conductance in microsiemens (µS).

GSR reflects sympathetic nervous system arousal and is used in sleep
research as a proxy for arousal events.

⚠️  EDUCATIONAL USE ONLY — conductance values depend heavily on electrode
    placement, skin hydration, and temperature.  This driver does not
    calibrate for those factors.

Hardware assumption
-------------------
Circuit: 3.3 V → GSR_REF_RESISTOR → ADC pin → skin electrodes → GND

    V_adc = 3.3 V × R_skin / (R_ref + R_skin)
    R_skin = R_ref × V_adc / (3.3 − V_adc)
    Conductance (µS) = 1 / R_skin × 1_000_000

Educational prototype — not a clinically approved device.
"""

import config

# Import machine.ADC only if running on real hardware.  The try/except
# allows this module to be syntax‑checked on CPython during development.
try:
    from machine import ADC
except ImportError:
    ADC = None   # running on CPython for testing — ADC will not be used


class GSRSensor:
    """
    Driver for the GSR (galvanic skin response) resistive sensor.

    Reads an ADC pin connected to a voltage‑divider with a known
    reference resistor.  Converts the ADC reading to skin conductance
    in microsiemens (µS).

    Args:
        adc_pin (int): GPIO pin number for the ADC input.
                       Defaults to 26 (ADC0 on RP2350).
    """

    def __init__(self, adc_pin=26):
        """
        Initialise the GSR sensor on the specified ADC pin.

        Args:
            adc_pin (int): GPIO pin number.  Defaults to 26.

        Returns:
            None
        """
        self._pin = adc_pin
        try:
            self._adc = ADC(adc_pin)
            print("[SOMNI][GSR] ADC initialised on pin {}.".format(adc_pin))
        except Exception as exc:
            self._adc = None
            print("[SOMNI][GSR] ADC init error on pin {}: {}".format(adc_pin, exc))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_raw(self):
        """
        Read the raw 16‑bit ADC value (0–65535).

        Args:
            None

        Returns:
            int: Raw ADC count in range [0, 65535], or 0 on error.
        """
        if self._adc is None:
            print("[SOMNI][GSR] read_raw: ADC not initialised.")
            return 0
        try:
            return self._adc.read_u16()
        except Exception as exc:
            print("[SOMNI][GSR] read_raw error: {}".format(exc))
            return 0

    def read_conductance(self):
        """
        Read the ADC and compute skin conductance in microsiemens (µS).

        Converts the raw ADC count to a voltage using the 3.3 V reference,
        then applies the voltage‑divider formula to derive skin resistance,
        and finally inverts to get conductance.

        Edge cases:
        - If V_adc ≈ 0 V (short to GND) or ≈ 3.3 V (open circuit), the
          conductance result will be extreme or infinite.  The reading is
          still returned with valid=True, but the comment below flags this.
          The caller (sampler) can apply range‑checks if desired.

        Args:
            None

        Returns:
            dict: {
                "raw"            : int,    # raw ADC count [0, 65535]
                "voltage"        : float,  # ADC pin voltage in volts
                "conductance_us" : float,  # skin conductance in µS
                "valid"          : bool    # False only on ADC failure
            }
        """
        raw = self.read_raw()

        # Convert raw count to voltage
        voltage = (raw / config.ADC_FULL_SCALE) * config.ADC_VREF

        # Compute skin resistance via voltage‑divider formula.
        # Guard against division by zero if voltage equals rail voltage.
        # Note: voltage ≈ 3.3 V means R_skin → ∞ (open circuit / no contact).
        #       voltage ≈ 0 V means R_skin ≈ 0 (short circuit / saturated).
        # Both edge cases are valid hardware states; we clamp to avoid inf/NaN.
        vref    = config.ADC_VREF
        r_ref   = config.GSR_REF_RESISTOR_OHMS
        epsilon = 1e-6   # small guard to prevent exact zero denominator

        denom = max(vref - voltage, epsilon)
        r_skin = r_ref * voltage / denom

        # Conductance in µS = 1 / R_skin × 10^6
        conductance_us = (1.0 / max(r_skin, epsilon)) * 1_000_000

        return {
            "raw":            raw,
            "voltage":        round(voltage, 4),
            "conductance_us": round(conductance_us, 3),
            "valid":          self._adc is not None,
        }

    def read_smoothed(self, window=None):
        """
        Read multiple ADC samples and return the averaged conductance reading.

        Averaging reduces high‑frequency noise from ADC quantisation and
        minor electrode movement artefacts.

        Args:
            window (int | None): Number of samples to average.
                                 If None, uses config.GSR_SMOOTH_WINDOW.

        Returns:
            dict: Same structure as read_conductance(), with values averaged
                  over 'window' samples.  valid=False if ADC is unavailable.
        """
        if window is None:
            window = config.GSR_SMOOTH_WINDOW

        window = max(1, int(window))

        total_raw  = 0
        total_volt = 0.0
        total_cond = 0.0
        valid      = True

        for _ in range(window):
            reading = self.read_conductance()
            if not reading["valid"]:
                valid = False
            total_raw  += reading["raw"]
            total_volt += reading["voltage"]
            total_cond += reading["conductance_us"]

        return {
            "raw":            total_raw  // window,
            "voltage":        round(total_volt / window, 4),
            "conductance_us": round(total_cond / window, 3),
            "valid":          valid,
        }
