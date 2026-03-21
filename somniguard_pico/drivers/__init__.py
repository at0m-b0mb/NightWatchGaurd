"""
drivers/__init__.py — SOMNI‑Guard sensor driver package.

Importing this package gives access to all three sensor driver classes:
  - MAX30102  (SpO₂ / heart‑rate)
  - ADXL345   (3‑axis accelerometer)
  - GSRSensor (galvanic skin response)

Educational prototype — not a clinically approved device.
"""

from .max30102 import MAX30102
from .adxl345  import ADXL345
from .gsr      import GSRSensor

__all__ = ["MAX30102", "ADXL345", "GSRSensor"]
