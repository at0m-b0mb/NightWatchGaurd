"""
main.py — SOMNI‑Guard Pico 2 W application entry point.

This is the top‑level script that runs on the Raspberry Pi Pico 2 W when
the device boots.  It:

1. Sets up the shared I2C bus on GP4 (SDA) / GP5 (SCL) at 400 kHz.
2. Instantiates SensorSampler and checks all sensors; logs missing sensors
   but does NOT abort — fail‑soft behaviour means the device keeps running
   even if one sensor is absent or faulty.
3. Connects to Wi‑Fi and opens a session on the Pi 5 gateway (if
   TRANSPORT_ENABLED = True in config.py).
4. Configures the onboard LED to blink once per second as a heartbeat.
5. Registers a data callback that:
   a. Formats and prints each reading with a ``[SOMNI][DATA]`` prefix.
   b. Buffers full 1 Hz readings and periodically POSTs them to the gateway.
6. Starts the timer‑driven sampling loop.
7. Wraps the entire main flow in a top‑level try/except so that unexpected
   errors are caught, logged with ``[SOMNI][FATAL]``, and — where possible —
   the device attempts to restart rather than halting.

Educational prototype — not a clinically approved device.
"""

import time
import config
import utils
from sampler import SensorSampler
import transport

# Import machine peripherals — must run on RP2350 MicroPython
try:
    from machine import I2C, Pin
    _HARDWARE = True
except ImportError:
    # Allow syntax checking on CPython
    I2C = None
    Pin = None
    _HARDWARE = False

# ---------------------------------------------------------------------------
# LED heartbeat state
# ---------------------------------------------------------------------------
_led = None          # machine.Pin for onboard LED
_led_state = False   # current LED on/off state

# ---------------------------------------------------------------------------
# Transport / session state
# ---------------------------------------------------------------------------
_session_id   = None   # session ID assigned by the gateway
_pending_batch = []    # buffer of full 1 Hz readings waiting to be sent


def _toggle_led():
    """
    Toggle the onboard LED and update _led_state.

    Args:
        None

    Returns:
        None
    """
    global _led_state
    if _led is not None:
        _led_state = not _led_state
        _led.value(1 if _led_state else 0)


# ---------------------------------------------------------------------------
# Sampling callback
# ---------------------------------------------------------------------------

def _on_sensor_data(data):
    """
    Callback invoked by SensorSampler on every sampling event.

    For full 1 Hz readings (containing "spo2" and "gsr"):
    - Formats and prints the reading with ``[SOMNI][DATA]``.
    - Adds the reading to the pending batch buffer.
    - When the batch is full, POSTs all buffered readings to the gateway.

    For 10 Hz accelerometer‑only ticks the dict will not contain "spo2" or
    "gsr" keys; only prints, no network send (to avoid overwhelming Wi‑Fi).

    Args:
        data (dict): Sensor reading dictionary from SensorSampler.

    Returns:
        None
    """
    global _pending_batch, _session_id

    try:
        is_full_reading = "spo2" in data

        if is_full_reading:
            _toggle_led()

        line = utils.format_reading(data)
        print("[SOMNI][DATA] " + line)

        # Only buffer and transmit full 1 Hz readings
        if not is_full_reading or not config.TRANSPORT_ENABLED:
            return

        if _session_id is None:
            return   # transport not ready yet; drop reading

        _pending_batch.append(data)

        if len(_pending_batch) >= config.TRANSPORT_BATCH_SIZE:
            _flush_batch()

    except Exception as exc:
        print("[SOMNI][CB] Callback error: {}".format(exc))


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------

def _flush_batch():
    """
    Send all buffered readings to the gateway, then clear the batch.

    Sends each reading individually to the /api/ingest endpoint.
    If any send fails the reading is silently dropped (fail‑soft).

    Args:
        None

    Returns:
        None
    """
    global _pending_batch
    batch = _pending_batch
    _pending_batch = []

    for reading in batch:
        try:
            payload = dict(reading)
            payload["session_id"] = _session_id
            transport.send_api(
                config.GATEWAY_HOST,
                config.GATEWAY_PORT,
                transport._API_INGEST,
                payload,
                config.GATEWAY_HMAC_KEY,
            )
        except Exception as exc:
            print("[SOMNI][TRANSPORT] flush error: {}".format(exc))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    """
    Main application function for SOMNI‑Guard sensor layer.

    Sets up hardware, starts the sampling loop, and then enters an idle
    loop that keeps the device alive.

    Args:
        None

    Returns:
        None — this function runs indefinitely on the device.
    """
    global _led

    print("[SOMNI] ================================================")
    print("[SOMNI] SOMNI-Guard v0.1 — Educational Sleep Monitor")
    print("[SOMNI] NOT a clinically approved device.")
    print("[SOMNI] ================================================")

    # ---------------------------------------------------------------
    # 1. Onboard LED setup
    # ---------------------------------------------------------------
    if _HARDWARE:
        try:
            _led = Pin(config.LED_PIN, Pin.OUT)
            _led.value(0)
            print("[SOMNI] Onboard LED initialised.")
        except Exception as exc:
            print("[SOMNI] LED init warning: {}".format(exc))
            _led = None
    else:
        print("[SOMNI] Running on CPython — hardware stubs active.")

    # ---------------------------------------------------------------
    # 2. I2C bus setup
    # ---------------------------------------------------------------
    i2c = None
    if _HARDWARE:
        try:
            i2c = I2C(
                config.I2C_ID,
                sda=Pin(config.I2C_SDA),
                scl=Pin(config.I2C_SCL),
                freq=config.I2C_FREQ,
            )
            print("[SOMNI] I2C bus initialised (SDA=GP{}, SCL=GP{}, {}Hz).".format(
                config.I2C_SDA, config.I2C_SCL, config.I2C_FREQ))
        except Exception as exc:
            print("[SOMNI][FATAL] I2C init failed: {}".format(exc))
            # Without I2C we cannot talk to MAX30102 or ADXL345;
            # log and fall through — GSR will still work.

    # ---------------------------------------------------------------
    # 3. SensorSampler setup & sensor check
    # ---------------------------------------------------------------
    sampler = None
    try:
        sampler = SensorSampler(i2c, cfg=config)
        sensor_status = sampler.check_all_sensors()
        # Log missing sensors but do not abort
        for name, ok in sensor_status.items():
            if not ok:
                print("[SOMNI][WARN] Sensor '{}' not responding — "
                      "readings will be marked invalid.".format(name))
    except Exception as exc:
        print("[SOMNI][FATAL] SensorSampler init failed: {}".format(exc))
        # If sampler cannot be created we cannot sample; loop forever
        while True:
            _toggle_led()
            time.sleep_ms(500)

    # ---------------------------------------------------------------
    # 4. Wi‑Fi and gateway session setup
    # ---------------------------------------------------------------
    global _session_id
    if config.TRANSPORT_ENABLED:
        ip = transport.connect_wifi(
            config.WIFI_SSID,
            config.WIFI_PASSWORD,
            timeout_s=config.WIFI_CONNECT_TIMEOUT_S,
        )
        if ip:
            print("[SOMNI] Connected to Wi‑Fi as {}.".format(ip))
            _session_id = transport.start_session(
                config.GATEWAY_HOST,
                config.GATEWAY_PORT,
                config.GATEWAY_PATIENT_ID,
                config.DEVICE_ID,
                config.GATEWAY_HMAC_KEY,
            )
            if _session_id:
                print("[SOMNI] Gateway session started: ID {}.".format(_session_id))
            else:
                print("[SOMNI][WARN] Could not start gateway session; "
                      "data will be logged locally only.")
        else:
            print("[SOMNI][WARN] Wi‑Fi unavailable; "
                  "data will be logged locally only.")
    else:
        print("[SOMNI] Transport disabled (TRANSPORT_ENABLED=False); "
              "USB‑serial only.")

    # ---------------------------------------------------------------
    # 5. Start sampling loop
    # ---------------------------------------------------------------
    try:
        sampler.start_sampling_loop(_on_sensor_data)
    except Exception as exc:
        print("[SOMNI][FATAL] start_sampling_loop failed: {}".format(exc))
        # Attempt a manual blocking fallback loop
        print("[SOMNI] Falling back to blocking poll loop (no timer).")
        _blocking_loop(sampler)
        return

    # ---------------------------------------------------------------
    # 6. Idle loop — keep main thread alive; timer callback does work
    # ---------------------------------------------------------------
    print("[SOMNI] Sampling active. Press Ctrl‑C to stop.")
    try:
        while True:
            time.sleep_ms(1000)
    except KeyboardInterrupt:
        print("[SOMNI] KeyboardInterrupt received — stopping.")
    except Exception as exc:
        print("[SOMNI][FATAL] Idle loop error: {}".format(exc))
    finally:
        # Flush any remaining batch before shutdown
        if _pending_batch and _session_id:
            _flush_batch()
        # End the gateway session
        if _session_id and config.TRANSPORT_ENABLED:
            transport.end_session(
                config.GATEWAY_HOST,
                config.GATEWAY_PORT,
                _session_id,
                config.GATEWAY_HMAC_KEY,
            )
        if sampler is not None:
            sampler.stop()
        if _led is not None:
            _led.value(0)
        transport.disconnect_wifi()
        print("[SOMNI] Shutdown complete.")


def _blocking_loop(sampler):
    """
    Fallback blocking poll loop used when machine.Timer is unavailable.

    Polls all sensors at approximately 1 Hz using time.sleep_ms().
    This is less precise than the timer‑driven loop but ensures the
    device continues to produce data even if the timer subsystem fails.

    Args:
        sampler (SensorSampler): Initialised sampler instance.

    Returns:
        None
    """
    print("[SOMNI] Blocking poll loop active (~1 Hz).")
    while True:
        try:
            data = sampler.read_all()
            _on_sensor_data(data)
        except Exception as exc:
            print("[SOMNI][FATAL] Blocking loop error: {}".format(exc))
        time.sleep_ms(config.SPO2_INTERVAL_MS)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

try:
    main()
except Exception as exc:
    # Absolute last‑resort catch — should never reach here in normal operation
    print("[SOMNI][FATAL] Unhandled top‑level exception: {}".format(exc))
    # Blink LED rapidly to signal fault condition
    if _HARDWARE:
        try:
            _fault_led = Pin(config.LED_PIN, Pin.OUT)
            while True:
                _fault_led.value(1)
                time.sleep_ms(100)
                _fault_led.value(0)
                time.sleep_ms(100)
        except Exception:
            pass  # Nothing more we can do
