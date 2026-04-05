"""
drivers/__init__.py — SOMNI‑Guard sensor driver package.

Importing this package gives access to the core sensor driver classes:
  - MAX30102  (SpO₂ / heart‑rate)
  - ADXL345   (3‑axis accelerometer)
  - GSRSensor (galvanic skin response, via Pico built‑in ADC)

The optional ADS1115 driver (16‑bit external ADC) is available in
drivers/ads1115.py but is NOT imported by default.  Import it explicitly
if upgrading from the Pico's built‑in ADC:
  from drivers.ads1115 import ADS1115

Educational prototype — not a clinically approved device.
"""

from .max30102 import MAX30102
from .adxl345  import ADXL345
from .gsr      import GSRSensor

__all__ = ["MAX30102", "ADXL345", "GSRSensor"]
