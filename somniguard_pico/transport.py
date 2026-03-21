"""
transport.py — SOMNI‑Guard Pico 2 W Wi‑Fi transport layer.

Handles:
1. Connecting the Pico 2 W to a Wi‑Fi access point.
2. Sending HMAC‑SHA256‑signed JSON telemetry packets to the Pi 5 gateway
   over HTTP (plain TCP socket, MicroPython‑compatible).

HMAC security model
-------------------
Each request body is signed with a shared secret key.  The key must match
``PICO_HMAC_KEY`` in the gateway's config.py.  The HMAC prevents replay
attacks (when combined with timestamp checking on the gateway) and
authenticates the Pico as the data source without requiring TLS.

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
# Internal HMAC‑SHA256 (pure Python, MicroPython‑compatible)
# ---------------------------------------------------------------------------

# API endpoint paths — defined once here so callers share the same strings.
_API_SESSION_START = "/api/session/start"
_API_SESSION_END   = "/api/session/end"
_API_INGEST        = "/api/ingest"


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
    key = key + b"\x00" * (block_size - len(key))

    o_key = bytes(b ^ 0x5C for b in key)
    i_key = bytes(b ^ 0x36 for b in key)

    inner = hashlib.sha256(i_key + message).digest()
    outer = hashlib.sha256(o_key + inner).digest()

    # Convert to hex string (no binascii needed)
    return "".join("{:02x}".format(b) for b in outer)


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

    headers  = "POST {} HTTP/1.0\r\n".format(path)
    headers += "Host: {}\r\n".format(host)
    headers += "Content-Type: application/json\r\n"
    headers += "Content-Length: {}\r\n".format(len(body_bytes))
    if extra_headers:
        for k, v in extra_headers.items():
            headers += "{}: {}\r\n".format(k, v)
    headers += "\r\n"

    try:
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
        sock.close()

        # Parse "HTTP/1.x NNN …"
        parts = response_line.decode("utf-8", "ignore").split()
        if len(parts) >= 2:
            return int(parts[1])
        return 0

    except Exception as exc:
        print("[SOMNI][TRANSPORT] HTTP POST error: {}".format(exc))
        return 0


# ---------------------------------------------------------------------------
# Public transport API
# ---------------------------------------------------------------------------

def send_api(host, port, path, payload, hmac_key):
    """
    Sign and POST a JSON payload to the gateway API.

    Adds an "hmac" field to the payload before serialising so the gateway
    can verify authenticity.  The HMAC is computed over the JSON of the
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
    # Canonical payload without hmac field, sorted keys
    canonical = json.dumps(payload, sort_keys=True)
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

    Args:
        host       (str): Gateway IP or hostname.
        port       (int): Gateway TCP port.
        patient_id (int): ID of the patient in the gateway database.
        device_id  (str): Identifier for this Pico device.
        hmac_key   (str): Shared HMAC secret.

    Returns:
        int | None: Session ID assigned by the gateway, or None on failure.
    """
    payload = {"patient_id": patient_id, "device_id": device_id}
    canonical = json.dumps(payload, sort_keys=True)
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
        sock.close()

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
