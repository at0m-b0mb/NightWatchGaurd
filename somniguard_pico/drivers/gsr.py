"""
drivers/gsr.py — SOMNI‑Guard driver for the GSR (galvanic skin response) sensor.

The GSR sensor is a simple resistive element placed across a voltage divider
with a known reference resistor.  The Pico's built‑in ADC reads the voltage
at the midpoint and we compute skin conductance in microsiemens (µS).

GSR reflects sympathetic nervous system arousal and is used in sleep
research as a proxy for arousal events.

Hardware assumption
-------------------
Grove GSR v1.2 sensor module connected directly to the Pico 2W's ADC:

  - VCC (Red)    → 3.3 V
  - GND (Black)  → GND
  - SIG (Yellow) → GP26 / ADC0

The module contains an internal 10 kΩ reference resistor and outputs an
analogue voltage (0–3.3 V) on SIG that varies with skin resistance.

    V_adc = 3.3 V × R_skin / (R_ref + R_skin)
    R_skin = R_ref × V_adc / (3.3 − V_adc)
    Conductance (µS) = 1 / R_skin × 1_000_000

⚠️  EDUCATIONAL USE ONLY — conductance values depend heavily on electrode
    placement, skin hydration, and temperature.  This driver does not
    calibrate for those factors.

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
    Driver for the Grove GSR v1.2 resistive skin‑conductance sensor.

    Reads the Pico's built‑in ADC pin connected to the Grove GSR module's
    SIG output.  Converts the ADC reading to skin conductance in
    microsiemens (µS).

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

    def check_sensor(self):
        """
        Report whether the ADC initialised successfully.

        Provides a public interface so callers (e.g. SensorSampler) can check
        GSR availability without accessing the private ``_adc`` attribute.

        Note: True means the ADC *hardware* is ready.  It does NOT guarantee
        that the sensor module is physically wired or that the electrodes are
        on skin.  Use classify_contact() / read_conductance()["contact"] for
        electrode-state detection.

        Args:
            None

        Returns:
            bool: True if the ADC initialised without error, False otherwise.
        """
        return self._adc is not None

    def classify_contact(self, conductance_us):
        """
        Classify the electrode-contact state from a single conductance value.

        The Grove GSR v1.2 wired to the Pico ADC (GP26) sits in one of three
        distinct conductance ranges depending on whether the module is plugged
        in and whether the electrodes are resting on skin:

          "disconnected"
            conductance_us > config.GSR_DISCONNECTED_THRESHOLD_US  (default 250 µS)
            GP26 is floating because the sensor module is not plugged in, or
            the SIG wire is loose.  The voltage-divider formula interprets the
            floating ~0.7 V pin voltage as an anomalously high conductance.
            Readings in this state are meaningless.

          "no_contact"
            conductance_us < config.GSR_CONTACT_THRESHOLD_US  (default 80 µS)
            The module is powered (VCC/GND present) and SIG is connected, but
            the electrodes are in the air.  The open circuit creates very high
            effective R_skin, so V_adc approaches VCC and the formula returns
            a near-zero conductance.  Readings in this state are meaningless.

          "contact"
            config.GSR_CONTACT_THRESHOLD_US ≤ conductance_us
                ≤ config.GSR_DISCONNECTED_THRESHOLD_US
            The electrodes are on skin.  This is the only state in which
            conductance_us is a physiologically meaningful reading.

        Args:
            conductance_us (float): Conductance value from read_conductance().

        Returns:
            str: One of "disconnected", "no_contact", or "contact".
        """
        if conductance_us > config.GSR_DISCONNECTED_THRESHOLD_US:
            return "disconnected"
        if conductance_us < config.GSR_CONTACT_THRESHOLD_US:
            return "no_contact"
        return "contact"

    def read_raw(self):
        """
        Read the raw 16‑bit ADC value (0–65535).

        The Pico 2W's ADC is 12‑bit internally but ``read_u16()`` returns
        a value scaled to the 0–65535 range.

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
          conductance result will be extreme or near-zero.  The "contact"
          field in the returned dict classifies the reading so the caller
          knows whether conductance_us is a physiologically meaningful value.

        Args:
            None

        Returns:
            dict: {
                "raw"            : int,    # raw ADC count [0, 65535]
                "voltage"        : float,  # ADC pin voltage in volts
                "conductance_us" : float,  # skin conductance in µS
                "contact"        : str,    # "contact" | "no_contact" | "disconnected"
                "valid"          : bool    # False only on ADC hardware failure
            }

            "contact" values:
              "contact"      — electrodes on skin; conductance_us is valid
              "no_contact"   — sensor wired but electrodes not on skin
              "disconnected" — sensor not wired; ADC pin is floating
        """
        raw = self.read_raw()

        # Convert raw count to voltage
        voltage = (raw / config.ADC_FULL_SCALE) * config.ADC_VREF

        # Compute skin resistance via voltage‑divider formula.
        # Guard against division by zero if voltage equals rail voltage.
        vref    = config.ADC_VREF
        r_ref   = config.GSR_REF_RESISTOR_OHMS
        epsilon = 1e-6   # small guard to prevent exact zero denominator

        denom = max(vref - voltage, epsilon)
        r_skin = r_ref * voltage / denom

        # Conductance in µS = 1 / R_skin × 10^6
        conductance_us = (1.0 / max(r_skin, epsilon)) * 1_000_000

        contact = self.classify_contact(conductance_us)

        return {
            "raw":            raw,
            "voltage":        round(voltage, 4),
            "conductance_us": round(conductance_us, 3),
            "contact":        contact,
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
                  over 'window' samples.  The "contact" field reflects the
                  state of the averaged conductance value.
                  valid=False if ADC is unavailable.
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

        avg_cond = round(total_cond / window, 3)
        contact  = self.classify_contact(avg_cond)

        return {
            "raw":            total_raw  // window,
            "voltage":        round(total_volt / window, 4),
            "conductance_us": avg_cond,
            "contact":        contact,
            "valid":          valid,
        }
