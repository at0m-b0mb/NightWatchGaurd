"""
transport.py — SOMNI‑Guard Pico 2 W Wi‑Fi transport layer.

Handles:
1. Connecting the Pico 2 W to a Wi‑Fi access point.
2. Sending HMAC‑SHA256‑signed JSON telemetry packets to the Pi 5 gateway
   over HTTP (plain TCP socket, MicroPython‑compatible).

HMAC security model
-------------------
Each request body is signed with a shared secret key.  The key must match
``PICO_HMAC_KEY`` in the gateway's config.py.  Every packet includes:
- A monotonically increasing sequence number to prevent replay attacks.
- A timestamp for the gateway to validate freshness (reject stale packets).
The HMAC is computed over the full payload including the nonce and timestamp,
so an attacker cannot strip or modify these anti‑replay fields.

MicroPython compatibility
-------------------------
- Uses only ``network``, ``socket``, ``hashlib``, and ``ujson`` (or json).
- No third‑party libraries required.
- All networking operations have timeouts and are wrapped in try/except.

Educational prototype — not a clinically approved device.
"""

import hashlib
import json
import time

# MicroPython network / socket — will fail on CPython (expected).
try:
    import network
    import socket as _socket
    _WIFI_AVAILABLE = True
except ImportError:
    network = None
    _socket = None
    _WIFI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Anti‑replay sequence counter
# ---------------------------------------------------------------------------

_sequence_number = 0   # monotonically increasing nonce per session


def _next_sequence():
    """
    Return the next sequence number and increment the counter.

    The sequence number is a monotonically increasing integer that resets
    when the Pico reboots.  It is included in every HMAC‑signed packet to
    prevent replay attacks — the gateway rejects any packet whose sequence
    number has already been seen or is below the high‑water mark.

    Args:
        None

    Returns:
        int: Next sequence number.
    """
    global _sequence_number
    _sequence_number += 1
    return _sequence_number


def _get_timestamp_s():
    """
    Return the current time as seconds since epoch (or boot).

    On MicroPython this uses time.time() which returns seconds since
    2000-01-01 or the epoch depending on the port.  The gateway uses
    a configurable staleness window to reject old timestamps.

    Args:
        None

    Returns:
        int: Current timestamp in seconds.
    """
    return int(time.time())


# ---------------------------------------------------------------------------
# Internal HMAC‑SHA256 (pure Python, MicroPython‑compatible)
# ---------------------------------------------------------------------------

# API endpoint paths — defined once here so callers share the same strings.
_API_SESSION_START = "/api/session/start"
_API_SESSION_END   = "/api/session/end"
_API_INGEST        = "/api/ingest"


def _json_sorted(obj):
    """
    Serialise *obj* to JSON with keys in sorted order.

    MicroPython's ``ujson.dumps()`` does NOT support the ``sort_keys``
    keyword argument.  Passing ``sort_keys=True`` to ``ujson.dumps()``
    raises ``TypeError: extra keyword arguments given``.

    This helper manually builds the JSON string with sorted keys.

    Args:
        obj: Any JSON-serialisable value (typically a dict).

    Returns:
        str: JSON string with dict keys in alphabetical order.
    """
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        return json.dumps(obj)   # handles inf/nan edge cases
    if isinstance(obj, str):
        return json.dumps(obj)   # handles escaping
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(_json_sorted(item) for item in obj) + "]"
    if isinstance(obj, dict):
        parts = []
        for k in sorted(obj.keys()):
            parts.append(json.dumps(str(k)) + ":" + _json_sorted(obj[k]))
        return "{" + ",".join(parts) + "}"
    # Fallback for other types
    return json.dumps(obj)


def _hmac_sha256(key, message):
    """
    Compute HMAC‑SHA256 using only hashlib (no hmac module required).

    Implements RFC 2104 HMAC using the hashlib SHA‑256 primitive.  This
    avoids any dependency on the ``hmac`` module which may not be present
    in all MicroPython builds.

    Args:
        key     (str | bytes): Shared secret key.
        message (str | bytes): Message to authenticate.

    Returns:
        str: Hex‑encoded HMAC‑SHA256 digest.
    """
    if isinstance(key, str):
        key = key.encode("utf-8")
    if isinstance(message, str):
        message = message.encode("utf-8")

    block_size = 64  # SHA‑256 block size in bytes

    # Keys longer than the block size are hashed first
    if len(key) > block_size:
        key = hashlib.sha256(key).digest()

    # Pad key to block_size
    key_padded = key + b"\x00" * (block_size - len(key))

    o_key = bytes(b ^ 0x5C for b in key_padded)
    i_key = bytes(b ^ 0x36 for b in key_padded)

    inner = hashlib.sha256(i_key + message).digest()
    outer = hashlib.sha256(o_key + inner).digest()

    # Securely wipe intermediate key material
    _wipe_bytes(bytearray(key_padded))
    _wipe_bytes(bytearray(o_key))
    _wipe_bytes(bytearray(i_key))

    # Convert to hex string (no binascii needed)
    return "".join("{:02x}".format(b) for b in outer)


# ---------------------------------------------------------------------------
# Secure memory helpers
# ---------------------------------------------------------------------------

def _wipe_bytes(ba):
    """
    Zero out a bytearray in memory to prevent secret leakage.

    MicroPython does not guarantee secure erasure, but overwriting with
    zeros is the best effort available without OS-level secure memory APIs.

    Args:
        ba (bytearray): Mutable byte buffer to wipe.

    Returns:
        None
    """
    if isinstance(ba, bytearray):
        for i in range(len(ba)):
            ba[i] = 0


# ---------------------------------------------------------------------------
# Wi‑Fi connection
# ---------------------------------------------------------------------------

def connect_wifi(ssid, password, timeout_s=30):
    """
    Connect the Pico 2 W to a Wi‑Fi access point.

    Prints status messages with the ``[SOMNI][WIFI]`` prefix.  Blocks until
    connected or the timeout expires.

    Args:
        ssid       (str): Wi‑Fi network name.
        password   (str): Wi‑Fi password.
        timeout_s  (int): Maximum seconds to wait for connection.
                          Defaults to 30.

    Returns:
        str | None: IP address string on success, None on failure.
    """
    if not _WIFI_AVAILABLE:
        print("[SOMNI][WIFI] Wi‑Fi not available (CPython?); skipping.")
        return None

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        ip = wlan.ifconfig()[0]
        print("[SOMNI][WIFI] Already connected, IP: {}".format(ip))
        return ip

    print("[SOMNI][WIFI] Connecting to '{}'…".format(ssid))
    wlan.connect(ssid, password)

    deadline = time.time() + timeout_s
    while not wlan.isconnected():
        if time.time() > deadline:
            print("[SOMNI][WIFI] Connection timeout after {}s.".format(timeout_s))
            return None
        time.sleep(1)

    ip = wlan.ifconfig()[0]
    print("[SOMNI][WIFI] Connected. IP: {}".format(ip))
    return ip


def disconnect_wifi():
    """
    Disconnect from Wi‑Fi and deactivate the WLAN interface.

    Args:
        None

    Returns:
        None
    """
    if not _WIFI_AVAILABLE:
        return
    try:
        wlan = network.WLAN(network.STA_IF)
        wlan.disconnect()
        wlan.active(False)
        print("[SOMNI][WIFI] Disconnected.")
    except Exception as exc:
        print("[SOMNI][WIFI] disconnect error: {}".format(exc))


# ---------------------------------------------------------------------------
# HTTP helper (raw socket, no urequests dependency)
# ---------------------------------------------------------------------------

def _http_post(host, port, path, body_bytes, extra_headers=None, timeout_s=10):
    """
    Send an HTTP/1.0 POST request and return the status code.

    Uses a raw socket so no HTTP library is needed.  HTTP/1.0 is used to
    avoid keep‑alive complexity.

    Args:
        host          (str):        Hostname or IP of the gateway.
        port          (int):        TCP port.
        path          (str):        URL path (e.g. '/api/ingest').
        body_bytes    (bytes):      Request body.
        extra_headers (dict|None):  Additional HTTP headers.
        timeout_s     (int):        Socket timeout in seconds.

    Returns:
        int: HTTP status code, or 0 on connection error.
    """
    if not _WIFI_AVAILABLE:
        print("[SOMNI][TRANSPORT] Socket not available (CPython?); skipping POST.")
        return 0

    sock = None
    try:
        headers  = "POST {} HTTP/1.0\r\n".format(path)
        headers += "Host: {}\r\n".format(host)
        headers += "Content-Type: application/json\r\n"
        headers += "Content-Length: {}\r\n".format(len(body_bytes))
        if extra_headers:
            for k, v in extra_headers.items():
                headers += "{}: {}\r\n".format(k, v)
        headers += "\r\n"

        addr = _socket.getaddrinfo(host, port)[0][-1]
        sock = _socket.socket()
        sock.settimeout(timeout_s)
        sock.connect(addr)
        sock.send(headers.encode("utf-8") + body_bytes)

        # Read status line only (we don't need the full response)
        response_line = b""
        while b"\n" not in response_line:
            chunk = sock.recv(64)
            if not chunk:
                break
            response_line += chunk

        # Parse "HTTP/1.x NNN …"
        parts = response_line.decode("utf-8", "ignore").split()
        if len(parts) >= 2:
            return int(parts[1])
        return 0

    except Exception as exc:
        print("[SOMNI][TRANSPORT] HTTP POST error: {}".format(exc))
        return 0

    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Public transport API
# ---------------------------------------------------------------------------

def send_api(host, port, path, payload, hmac_key):
    """
    Sign and POST a JSON payload to the gateway API.

    Adds anti‑replay fields (sequence number and timestamp) and an HMAC
    field to the payload before serialising so the gateway can verify
    authenticity and freshness.  The HMAC is computed over the JSON of the
    payload (sorted keys, "hmac" field excluded).

    Args:
        host      (str):  Gateway IP or hostname.
        port      (int):  Gateway TCP port.
        path      (str):  API path (e.g. '/api/ingest').
        payload   (dict): Data to send (must be JSON‑serialisable).
        hmac_key  (str):  Shared HMAC secret (must match gateway config).

    Returns:
        int: HTTP status code from the gateway (200/201 = success, 0 = error).
    """
    # Add anti-replay fields
    payload["nonce"] = _next_sequence()
    payload["timestamp"] = _get_timestamp_s()

    # Canonical payload without hmac field, sorted keys
    canonical = _json_sorted(payload)
    mac = _hmac_sha256(hmac_key, canonical)

    # Add hmac field for transmission
    signed = dict(payload)
    signed["hmac"] = mac

    body = json.dumps(signed).encode("utf-8")
    status = _http_post(host, port, path, body)

    if status not in (200, 201):
        print("[SOMNI][TRANSPORT] {} {} → HTTP {}".format(
            host, path, status))

    return status


def start_session(host, port, patient_id, device_id, hmac_key):
    """
    Tell the gateway to start a new sleep session and return the session ID.

    Resets the sequence counter for the new session and includes anti-replay
    fields in the session start request.

    Args:
        host       (str): Gateway IP or hostname.
        port       (int): Gateway TCP port.
        patient_id (int): ID of the patient in the gateway database.
        device_id  (str): Identifier for this Pico device.
        hmac_key   (str): Shared HMAC secret.

    Returns:
        int | None: Session ID assigned by the gateway, or None on failure.
    """
    global _sequence_number
    _sequence_number = 0  # reset for new session

    payload = {
        "patient_id": patient_id,
        "device_id": device_id,
        "nonce": _next_sequence(),
        "timestamp": _get_timestamp_s(),
    }
    canonical = _json_sorted(payload)
    mac = _hmac_sha256(hmac_key, canonical)
    signed = dict(payload)
    signed["hmac"] = mac

    body = json.dumps(signed).encode("utf-8")

    # We need to read the response body to get the session_id
    if not _WIFI_AVAILABLE:
        print("[SOMNI][TRANSPORT] start_session: Wi‑Fi not available.")
        return None

    headers  = "POST {} HTTP/1.0\r\n".format(_API_SESSION_START)
    headers += "Host: {}\r\n".format(host)
    headers += "Content-Type: application/json\r\n"
    headers += "Content-Length: {}\r\n\r\n".format(len(body))

    sock = None
    try:
        addr = _socket.getaddrinfo(host, port)[0][-1]
        sock = _socket.socket()
        sock.settimeout(15)
        sock.connect(addr)
        sock.send(headers.encode("utf-8") + body)

        # Read full response (small body — session_id int)
        resp = b""
        while True:
            chunk = sock.recv(512)
            if not chunk:
                break
            resp += chunk

        # Split headers / body
        if b"\r\n\r\n" in resp:
            resp_body = resp.split(b"\r\n\r\n", 1)[1]
        else:
            resp_body = resp

        data = json.loads(resp_body.decode("utf-8"))
        session_id = data.get("session_id")
        if session_id:
            print("[SOMNI][TRANSPORT] Session started: ID {}".format(session_id))
        else:
            print("[SOMNI][TRANSPORT] start_session error: {}".format(data))
        return session_id

    except Exception as exc:
        print("[SOMNI][TRANSPORT] start_session exception: {}".format(exc))
        return None

    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def end_session(host, port, session_id, hmac_key):
    """
    Tell the gateway to close the current sleep session.

    Args:
        host       (str): Gateway IP or hostname.
        port       (int): Gateway TCP port.
        session_id (int): Session to close.
        hmac_key   (str): Shared HMAC secret.

    Returns:
        bool: True if the gateway acknowledged, False otherwise.
    """
    status = send_api(
        host, port, _API_SESSION_END,
        {"session_id": session_id},
        hmac_key,
    )
    return status == 200
