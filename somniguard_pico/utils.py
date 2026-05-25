"""
utils.py — SOMNI‑Guard shared utility functions and data structures.

Provides:
- RingBuffer  — fixed‑size circular buffer for rolling sensor data.
- get_timestamp()  — millisecond timestamp wrapper.
- format_reading() — compact human‑readable string for one sensor sample.

All utilities are MicroPython‑compatible: no CPython‑only imports are used.

Educational prototype — not a clinically approved device.
"""

import time

# ticks_ms and sleep_ms are MicroPython‑specific; provide CPython fallbacks for
# syntax‑checking and unit‑testing purposes only.
if not hasattr(time, "ticks_ms"):
    def _ticks_ms_fallback():
        """CPython shim: milliseconds since epoch (wraps at 2^30 like MicroPython)."""
        return int(time.monotonic() * 1000) % (2 ** 30)
    time.ticks_ms = _ticks_ms_fallback  # type: ignore[attr-defined]

if not hasattr(time, "sleep_ms"):
    def _sleep_ms_fallback(ms):
        """CPython shim: sleep for the given number of milliseconds."""
        time.sleep(ms / 1000.0)
    time.sleep_ms = _sleep_ms_fallback  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# RingBuffer
# ---------------------------------------------------------------------------

class RingBuffer:
    """
    Fixed‑size circular (ring) buffer.

    New items pushed when the buffer is full overwrite the oldest item.
    The implementation uses a plain list and an integer write pointer, which
    is efficient on MicroPython (no deque or collections dependency).

    Args:
        size (int): Maximum number of items the buffer can hold.
    """

    def __init__(self, size):
        """
        Initialise an empty ring buffer.

        Args:
            size (int): Buffer capacity (must be ≥ 1).

        Returns:
            None
        """
        if size < 1:
            raise ValueError("RingBuffer size must be >= 1.")
        self._size  = size
        self._buf   = [None] * size
        self._ptr   = 0      # index of the *next* write position
        self._count = 0      # number of valid items (capped at _size)

    def push(self, item):
        """
        Push one item into the buffer.

        If the buffer is full the oldest item is silently overwritten.

        Args:
            item: Any value to store.

        Returns:
            None
        """
        self._buf[self._ptr] = item
        self._ptr = (self._ptr + 1) % self._size
        if self._count < self._size:
            self._count += 1

    def get_all(self):
        """
        Return all valid items in chronological order (oldest first).

        Args:
            None

        Returns:
            list: Items from oldest to newest.  Empty list if no items.
        """
        if self._count == 0:
            return []
        if self._count < self._size:
            # Buffer not yet full — items start at index 0
            return list(self._buf[:self._count])
        # Buffer is full — oldest item is at self._ptr
        tail = self._buf[self._ptr:]
        head = self._buf[:self._ptr]
        return tail + head

    def get_latest(self, n):
        """
        Return the n most recent items (newest last).

        Args:
            n (int): Number of items to return.  Clamped to buffer length.

        Returns:
            list: Up to n most‑recent items, oldest first within the slice.
        """
        all_items = self.get_all()
        return all_items[-n:] if n < len(all_items) else all_items

    def is_full(self):
        """
        Return True if the buffer has reached its capacity.

        Args:
            None

        Returns:
            bool: True when the buffer is full.
        """
        return self._count == self._size

    def __len__(self):
        """Return the current number of valid items in the buffer."""
        return self._count


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------

def get_timestamp():
    """
    Return the current monotonic millisecond counter.

    Wraps ``time.ticks_ms()`` which is the MicroPython equivalent of a
    millisecond‑resolution uptime counter.  It wraps around at
    ``time.ticks_period()`` (typically 2^30 on RP2350).

    Args:
        None

    Returns:
        int: Milliseconds since boot (wrapping).
    """
    return time.ticks_ms()


# ---------------------------------------------------------------------------
# Sensor reading formatter
# ---------------------------------------------------------------------------

def format_reading(sensor_data):
    """
    Convert a sampler data dictionary into a compact, human‑readable string.

    The output is suitable for a single ``print()`` call prefixed with
    ``[SOMNI][DATA]`` in main.py.

    Expected input structure (from SensorSampler.read_all())::

        {
            "timestamp_ms": int,
            "spo2":  {"spo2": float|None, "hr": float|None,
                      "ir_raw": int|None, "red_raw": int|None, "valid": bool},
            "accel": {"x": float|None, "y": float|None,
                      "z": float|None, "valid": bool},
            # "gsr" key is present only when config.GSR_ENABLED is True:
            "gsr":   {"raw": int, "voltage": float,
                      "conductance_us": float, "valid": bool},
        }

    Args:
        sensor_data (dict): Dictionary returned by SensorSampler.read_all().

    Returns:
        str: Single‑line formatted string, e.g.:
             "t=12345ms SpO2=98.2% HR=62.0bpm accel=(0.01,-0.02,1.00)g"
             or with GSR when enabled:
             "t=12345ms SpO2=98.2% HR=62.0bpm accel=(0.01,-0.02,1.00)g GSR=12.3µS"
    """
    ts = sensor_data.get("timestamp_ms", 0)

    # SpO₂ / HR
    spo2_d = sensor_data.get("spo2", {})
    spo2_v = spo2_d.get("spo2")
    hr_v   = spo2_d.get("hr")
    spo2_str = "{:.1f}%".format(spo2_v) if spo2_v is not None else "---"
    hr_str   = "{:.1f}bpm".format(hr_v) if hr_v   is not None else "---"

    # Accelerometer
    acc_d = sensor_data.get("accel", {})
    x = acc_d.get("x")
    y = acc_d.get("y")
    z = acc_d.get("z")
    if x is not None:
        accel_str = "({:.3f},{:.3f},{:.3f})g".format(x, y, z)
    else:
        accel_str = "(---,---,---)g"

    line = "t={ms}ms SpO2={spo2} HR={hr} accel={accel}".format(
        ms=ts,
        spo2=spo2_str,
        hr=hr_str,
        accel=accel_str,
    )

    # GSR — only appended when the key is present (GSR_ENABLED=True)
    if "gsr" in sensor_data:
        gsr_d  = sensor_data["gsr"]
        gsr_us = gsr_d.get("conductance_us")
        gsr_str = "{:.2f}\u00b5S".format(gsr_us) if gsr_us is not None else "---"
        line += " GSR={}".format(gsr_str)

    return line
