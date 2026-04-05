"""
main.py — SOMNI‑Guard Pico 2 W application entry point.

This is the top‑level script that runs on the Raspberry Pi Pico 2 W when
the device boots.  It is one of only TWO plaintext files on the Pico
filesystem (along with ``crypto_loader.py``).  All other application modules
are stored as AES-256-CBC encrypted ``.enc`` files and are decrypted at
runtime by the crypto loader.

Boot sequence:

1. Imports ``crypto_loader`` (plaintext) to bootstrap the decryption engine.
2. Uses ``crypto_loader`` to decrypt and load all application modules
   (config, utils, transport, sampler, drivers, etc.).
3. Runs firmware integrity checks against the signed manifest (if present).
4. Sets up the hardware watchdog timer for crash recovery.
5. Sets up two dedicated I2C buses for sensors.
6. Instantiates SensorSampler and checks all sensors; logs missing sensors
   but does NOT abort — fail‑soft behaviour.
7. Connects to Wi‑Fi and opens a session on the Pi 5 gateway.
8. Starts the timer‑driven sampling loop.
9. Wraps the entire main flow in a top‑level try/except so that unexpected
   errors are caught, logged with ``[SOMNI][FATAL]``, and the device
   attempts to restart rather than halting.

Educational prototype — not a clinically approved device.
"""

import time

# ---------------------------------------------------------------------------
# Encrypted module loader — must be imported FIRST
# ---------------------------------------------------------------------------
# crypto_loader.py is one of only two plaintext files on the Pico.
# It decrypts .enc files at runtime using AES-256-CBC with a key derived
# from the device's hardware unique ID + salt.
try:
    import crypto_loader
    _CRYPTO_AVAILABLE = True
    print("[SOMNI] Encrypted firmware loader available.")
except ImportError:
    _CRYPTO_AVAILABLE = False
    print("[SOMNI] crypto_loader not found — using plaintext imports.")

# ---------------------------------------------------------------------------
# Load application modules (encrypted or plaintext fallback)
# ---------------------------------------------------------------------------
if _CRYPTO_AVAILABLE and crypto_loader.is_encryption_available():
    # Load all modules via the crypto loader (decrypts .enc files)
    config = crypto_loader.load_module_as_object("config")
    utils = crypto_loader.load_module_as_object("utils")
    transport = crypto_loader.load_module_as_object("transport")
    _sampler_mod = crypto_loader.import_encrypted("sampler")
    SensorSampler = _sampler_mod.get("SensorSampler")
    if SensorSampler is None:
        raise ImportError("SensorSampler not found in decrypted sampler module")
else:
    # Fallback: standard plaintext imports (dev mode / no .enc files)
    import config
    import utils
    from sampler import SensorSampler
    import transport

# Import machine peripherals — must run on RP2350 MicroPython
try:
    from machine import I2C, Pin, WDT
    _HARDWARE = True
except ImportError:
    # Allow syntax checking on CPython
    I2C = None
    Pin = None
    WDT = None
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

# ---------------------------------------------------------------------------
# Hardware watchdog
# ---------------------------------------------------------------------------
_wdt = None            # machine.WDT instance (if available)

# Watchdog timeout in milliseconds — must be fed within this interval
# or the device will reset.  8 seconds provides ample headroom for
# the 1 Hz sampling loop.
_WDT_TIMEOUT_MS = 8000


def _feed_watchdog():
    """
    Feed the hardware watchdog timer to prevent a reset.

    Must be called at least once every _WDT_TIMEOUT_MS milliseconds.
    Called from the main idle loop and from the sensor data callback.

    Args:
        None

    Returns:
        None
    """
    if _wdt is not None:
        _wdt.feed()


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

    For full 1 Hz readings (containing "spo2"):
    - Formats and prints the reading with ``[SOMNI][DATA]``.
    - Adds the reading to the pending batch buffer.
    - When the batch is full, POSTs all buffered readings to the gateway.
    - The "gsr" key is present only when config.GSR_ENABLED is True.

    For 10 Hz accelerometer‑only ticks the dict will not contain "spo2";
    only prints, no network send (to avoid overwhelming Wi‑Fi).

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
            _feed_watchdog()

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
# Firmware integrity check
# ---------------------------------------------------------------------------

def _run_integrity_check():
    """
    Run firmware integrity verification if the manifest file exists.

    Loads the integrity module and checks all firmware files against
    the signed manifest.  If integrity verification fails, prints a
    warning but does NOT abort — fail‑soft behaviour preserves device
    availability.  In a production system this should halt execution.

    Args:
        None

    Returns:
        bool: True if check passed or was skipped, False if files are tampered.
    """
    try:
        import os
        manifest_path = "manifest.json"
        # Check if manifest exists
        try:
            os.stat(manifest_path)
        except OSError:
            print("[SOMNI][INTEGRITY] No manifest.json found — "
                  "skipping integrity check.")
            return True

        import integrity
        passed, results = integrity.run_integrity_check(
            manifest_path=manifest_path,
            hmac_key=config.GATEWAY_HMAC_KEY,
            base_path="",
        )

        if passed:
            print("[SOMNI][INTEGRITY] All firmware files verified OK.")
        else:
            print("[SOMNI][INTEGRITY] *** WARNING: Integrity check FAILED ***")
            print("[SOMNI][INTEGRITY] Tampered files detected. "
                  "Continuing in fail-soft mode.")
            # In a production medical device, this should halt execution:
            # raise RuntimeError("Firmware integrity check failed")

        return passed

    except ImportError:
        print("[SOMNI][INTEGRITY] integrity module not available — skipping.")
        return True
    except Exception as exc:
        print("[SOMNI][INTEGRITY] Integrity check error: {}".format(exc))
        return True


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
    global _led, _wdt

    print("[SOMNI] ================================================")
    print("[SOMNI] SOMNI-Guard v0.3 — Educational Sleep Monitor")
    print("[SOMNI] NOT a clinically approved device.")
    print("[SOMNI] ================================================")

    # ---------------------------------------------------------------
    # 0. Firmware integrity check
    # ---------------------------------------------------------------
    _run_integrity_check()

    # ---------------------------------------------------------------
    # 1. Hardware watchdog timer setup
    # ---------------------------------------------------------------
    if _HARDWARE and WDT is not None:
        try:
            _wdt = WDT(timeout=_WDT_TIMEOUT_MS)
            print("[SOMNI] Hardware watchdog enabled "
                  "(timeout={}ms).".format(_WDT_TIMEOUT_MS))
        except Exception as exc:
            print("[SOMNI] Watchdog init warning: {}".format(exc))
            _wdt = None
    else:
        print("[SOMNI] Hardware watchdog not available.")

    # ---------------------------------------------------------------
    # 2. Onboard LED setup
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

    _feed_watchdog()

    # ---------------------------------------------------------------
    # 3. I2C bus setup — one bus per sensor so all pins are distinct
    # ---------------------------------------------------------------
    i2c_max30102 = None   # I2C0: SDA=GP4, SCL=GP5  (MAX30102 SpO₂/HR)
    i2c_adxl345  = None   # I2C1: SDA=GP2, SCL=GP3  (ADXL345 accelerometer)
    if _HARDWARE:
        try:
            i2c_max30102 = I2C(
                config.MAX30102_I2C_ID,
                sda=Pin(config.MAX30102_I2C_SDA),
                scl=Pin(config.MAX30102_I2C_SCL),
                freq=config.I2C_FREQ,
            )
            print("[SOMNI] MAX30102 I2C bus initialised "
                  "(SDA=GP{}, SCL=GP{}, {}Hz).".format(
                      config.MAX30102_I2C_SDA, config.MAX30102_I2C_SCL,
                      config.I2C_FREQ))
        except Exception as exc:
            print("[SOMNI][FATAL] MAX30102 I2C init failed: {}".format(exc))

        try:
            i2c_adxl345 = I2C(
                config.ADXL345_I2C_ID,
                sda=Pin(config.ADXL345_I2C_SDA),
                scl=Pin(config.ADXL345_I2C_SCL),
                freq=config.I2C_FREQ,
            )
            print("[SOMNI] ADXL345 I2C bus initialised "
                  "(SDA=GP{}, SCL=GP{}, {}Hz).".format(
                      config.ADXL345_I2C_SDA, config.ADXL345_I2C_SCL,
                      config.I2C_FREQ))
        except Exception as exc:
            print("[SOMNI][FATAL] ADXL345 I2C init failed: {}".format(exc))

    _feed_watchdog()

    # ---------------------------------------------------------------
    # 4. SensorSampler setup & sensor check
    # ---------------------------------------------------------------
    sampler = None
    try:
        sampler = SensorSampler(
            i2c_max30102=i2c_max30102,
            i2c_adxl345=i2c_adxl345,
            cfg=config,
        )
        sensor_status = sampler.check_all_sensors()
        # Log missing sensors but do not abort.
        # ok is True (present), False (failed), or None (intentionally disabled).
        # Only warn for False — None means the sensor is disabled in config.
        for name, ok in sensor_status.items():
            if ok is False:
                print("[SOMNI][WARN] Sensor '{}' not responding — "
                      "readings will be marked invalid.".format(name))
            elif ok is None:
                print("[SOMNI][INFO] Sensor '{}' disabled in config — "
                      "skipped.".format(name))
    except Exception as exc:
        print("[SOMNI][FATAL] SensorSampler init failed: {}".format(exc))
        # If sampler cannot be created we cannot sample; loop forever
        while True:
            _toggle_led()
            _feed_watchdog()
            time.sleep_ms(500)

    _feed_watchdog()

    # ---------------------------------------------------------------
    # 5. Wi‑Fi and gateway session setup
    # ---------------------------------------------------------------
    global _session_id
    if config.TRANSPORT_ENABLED:
        ip = transport.connect_wifi(
            config.WIFI_SSID,
            config.WIFI_PASSWORD,
            timeout_s=config.WIFI_CONNECT_TIMEOUT_S,
        )
        _feed_watchdog()
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

    _feed_watchdog()

    # ---------------------------------------------------------------
    # 6. Start sampling loop
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
    # 7. Idle loop — keep main thread alive; timer callback does work
    # ---------------------------------------------------------------
    print("[SOMNI] Sampling active. Press Ctrl‑C to stop.")
    try:
        while True:
            _feed_watchdog()
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
            _feed_watchdog()
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
                if _wdt is not None:
                    _wdt.feed()
        except Exception:
            pass  # Nothing more we can do
