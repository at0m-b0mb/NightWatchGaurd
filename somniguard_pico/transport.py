"""
transport.py — SOMNI-Guard Pico 2 W secure transport layer.

Wire-level protocol
-------------------
The Pico talks to the Pi 5 gateway over **mutual TLS (mTLS)** on top of
HTTP/1.0 sockets. Every request body is also authenticated with HMAC-SHA256.

This is defence in depth, not duplication:

  ┌────────────────────────────────────────────────────────────────────┐
  │  Layer         │  Provides                       │  Defeats        │
  ├────────────────┼─────────────────────────────────┼─────────────────┤
  │  TLS 1.2/1.3   │  Confidentiality, integrity     │  Passive sniff  │
  │  Server cert   │  Gateway authenticity           │  Rogue AP/MITM  │
  │  Client cert   │  Pico authenticity              │  Rogue device   │
  │  HMAC-SHA256   │  App-layer integrity + replay   │  Stolen TLS key │
  └────────────────────────────────────────────────────────────────────┘

PKI
---
Pico trusts a single SOMNI-Guard Root CA (config.GATEWAY_CA_CERT_PEM).
Server certs may be re-issued at any time — as long as they're signed by
that CA, the Pico keeps validating them. The Pico itself presents a
CA-signed client certificate (config.PICO_CLIENT_CERT_PEM, KEY_PEM)
during the handshake; the gateway's ssl context uses CERT_OPTIONAL — it
validates any client cert that is presented, but does not require one
(browsers reach the dashboard without a cert and use session + MFA auth
instead).  HMAC-SHA256 over the body is the primary API auth layer and
is mandatory regardless of client-cert presence.

MicroPython compatibility
-------------------------
- Uses only ``network``, ``socket``, ``hashlib``, ``ssl``, ``ujson``/json.
- ssl path supports both modern ``SSLContext`` and legacy ``wrap_socket``.
- gc.collect() before each handshake — mbedTLS handshake on RP2350 wants ~30 KB.

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

# TLS client — MicroPython on rp2 (Pico 2W) ships ssl with mbedTLS support.
try:
    import ssl as _ssl
    _TLS_AVAILABLE = True
except ImportError:
    _ssl = None
    _TLS_AVAILABLE = False

# gc is helpful before TLS handshakes on memory-constrained MicroPython
try:
    import gc as _gc
except ImportError:
    _gc = None


# ---------------------------------------------------------------------------
# Epoch offset — MicroPython vs Unix time
# ---------------------------------------------------------------------------
# What time.time() returns depends on the MicroPython port AND the build:
#
#   - Stock rp2 / rp2-w pre-1.22: seconds since 2000-01-01 (the
#     "MicroPython epoch").  We add _EPOCH_OFFSET to convert to Unix.
#   - RP2350 / Pico 2 W / recent rp2 builds with the new ntptime: after
#     ntptime.settime() the RTC is set such that time.time() returns
#     Unix epoch *directly*.  Adding _EPOCH_OFFSET on top sends the
#     timestamp ~30 years into the future and the gateway rejects every
#     packet with "stale timestamp" (age ≈ 946684800).
#
# _get_timestamp_s() auto-detects which convention this build uses by
# checking whether time.time() is already a plausible Unix value
# (> 2014).  No need to know the build at compile time.
_EPOCH_OFFSET = 946684800   # seconds between 1970-01-01 and 2000-01-01

# Anti-replay sequence counter (resets each session)
_sequence_number = 0

# API endpoint paths
_API_SESSION_START = "/api/session/start"
_API_SESSION_END   = "/api/session/end"
_API_INGEST        = "/api/ingest"


def _next_sequence():
    global _sequence_number
    _sequence_number += 1
    return _sequence_number


def _get_timestamp_s():
    """Return Unix epoch seconds, robust to either MicroPython convention.

    On builds where ntptime.settime() leaves time.time() in Unix epoch
    we pass it straight through.  On builds where time.time() counts
    from 2000-01-01 we add _EPOCH_OFFSET (or whatever offset the
    sync_time_from_gateway fallback computed) to convert.
    """
    t = int(time.time())
    # 1_400_000_000 = 2014-05-13 — well above any 2000-epoch value we
    # would ever see in practice (max ~6.3e8 even in 2040), and well
    # below current Unix time (~1.77e9 in 2026).  Used purely as the
    # discriminator between the two conventions.
    if t > 1_400_000_000:
        return t
    return t + _EPOCH_OFFSET


# ---------------------------------------------------------------------------
# HMAC-SHA256 (pure Python, MicroPython-compatible)
# ---------------------------------------------------------------------------

def _json_sorted(obj):
    """JSON serialise with dict keys in sorted order (MicroPython-safe)."""
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        return json.dumps(obj)
    if isinstance(obj, str):
        return json.dumps(obj)
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(_json_sorted(item) for item in obj) + "]"
    if isinstance(obj, dict):
        parts = []
        for k in sorted(obj.keys()):
            parts.append(json.dumps(str(k)) + ":" + _json_sorted(obj[k]))
        return "{" + ",".join(parts) + "}"
    return json.dumps(obj)


def _wipe_bytes(ba):
    if isinstance(ba, bytearray):
        for i in range(len(ba)):
            ba[i] = 0


def _hmac_sha256(key, message):
    """RFC 2104 HMAC-SHA256, returns hex string."""
    if isinstance(key, str):
        key = key.encode("utf-8")
    if isinstance(message, str):
        message = message.encode("utf-8")

    block_size = 64
    if len(key) > block_size:
        key = hashlib.sha256(key).digest()
    key_padded = key + b"\x00" * (block_size - len(key))

    o_key = bytes(b ^ 0x5C for b in key_padded)
    i_key = bytes(b ^ 0x36 for b in key_padded)
    inner = hashlib.sha256(i_key + message).digest()
    outer = hashlib.sha256(o_key + inner).digest()

    _wipe_bytes(bytearray(key_padded))
    _wipe_bytes(bytearray(o_key))
    _wipe_bytes(bytearray(i_key))

    return "".join("{:02x}".format(b) for b in outer)


# ---------------------------------------------------------------------------
# Wi-Fi connection
# ---------------------------------------------------------------------------

def connect_wifi(ssid, password, timeout_s=30, feed_wdt=None):
    """Connect Pico to a Wi-Fi AP. Returns IP string or None."""
    if not _WIFI_AVAILABLE:
        print("[SOMNI][WIFI] Wi-Fi not available (CPython?); skipping.")
        return None

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if wlan.isconnected():
        ip = wlan.ifconfig()[0]
        print("[SOMNI][WIFI] Already connected, IP: {}".format(ip))
        return ip

    try:
        networks = wlan.scan()
        visible = [n[0].decode("utf-8") if isinstance(n[0], bytes) else n[0]
                   for n in networks]
        print("[SOMNI][WIFI] Visible networks: {}".format(visible))
        if ssid not in visible:
            print("[SOMNI][WIFI] '{}' not found in scan — "
                  "is the Pi 5 hotspot running?".format(ssid))
            return None
    except Exception as exc:
        print("[SOMNI][WIFI] Scan warning: {}".format(exc))

    print("[SOMNI][WIFI] Connecting to '{}'…".format(ssid))
    wlan.connect(ssid, password)

    _CYW43_STAT_IDLE = 0
    _CYW43_STAT_CONNECTING = 1
    _CYW43_STAT_WRONG_PASSWORD = 2
    _CYW43_STAT_NO_AP_FOUND = 3
    _CYW43_STAT_CONNECT_FAIL = 4
    _CYW43_STAT_GOT_IP = 1010
    _STATUS_LABELS = {
        _CYW43_STAT_IDLE: "idle (CYW43 STAT_IDLE)",
        _CYW43_STAT_CONNECTING: "connecting (CYW43 STAT_CONNECTING)",
        _CYW43_STAT_WRONG_PASSWORD: "wrong password (CYW43 STAT_WRONG_PASSWORD)",
        _CYW43_STAT_NO_AP_FOUND: "no AP found (CYW43 STAT_NO_AP_FOUND)",
        _CYW43_STAT_CONNECT_FAIL: "connect failed (CYW43 STAT_CONNECT_FAIL)",
        _CYW43_STAT_GOT_IP: "got IP (CYW43 STAT_GOT_IP)",
        -1: "connection failed (legacy)",
        -2: "no AP found (legacy)",
        -3: "wrong password (legacy)",
    }
    _FAIL_FAST = {
        _CYW43_STAT_WRONG_PASSWORD,
        _CYW43_STAT_NO_AP_FOUND,
        _CYW43_STAT_CONNECT_FAIL,
        -1,
        -2,
        -3,
    }
    deadline = time.time() + timeout_s
    last_status = None
    while not wlan.isconnected():
        if feed_wdt is not None:
            feed_wdt()
        status = wlan.status()
        if status in _FAIL_FAST:
            label = _STATUS_LABELS.get(status, str(status))
            print("[SOMNI][WIFI] Connection failed — status={} ({})".format(status, label))
            return None
        if status != last_status:
            print("[SOMNI][WIFI] status → {} ({})".format(
                status, _STATUS_LABELS.get(status, "unknown")))
            last_status = status
        if time.time() > deadline:
            print("[SOMNI][WIFI] Connection timeout after {}s "
                  "(status={}).".format(timeout_s, status))
            return None
        time.sleep(1)

    status = wlan.status()
    if status != last_status:
        print("[SOMNI][WIFI] status → {} ({})".format(
            status, _STATUS_LABELS.get(status, "unknown")))

    ip = wlan.ifconfig()[0]
    print("[SOMNI][WIFI] Connected. IP: {}".format(ip))
    try:
        print("[SOMNI][WIFI] Signal: {} dBm".format(wlan.status("rssi")))
    except Exception:
        pass
    return ip


def connect_wifi_with_retries(ssid, password, max_retries=5, timeout_s=30,
                              feed_wdt=None):
    """Try connect_wifi() up to max_retries times before giving up.

    Each attempt includes a fresh WLAN deactivation/activation cycle and
    scan to work around CYW43 driver quirks.  Returns the IP string on
    success or None after all attempts are exhausted (local-only mode).
    """
    for attempt in range(1, max_retries + 1):
        print("[SOMNI][WIFI] Connection attempt {}/{}…".format(attempt, max_retries))
        ip = connect_wifi(ssid, password, timeout_s=timeout_s, feed_wdt=feed_wdt)
        if ip:
            return ip
        if attempt < max_retries:
            print("[SOMNI][WIFI] Attempt {} failed — resetting WLAN for retry…".format(attempt))
            # Reset WLAN interface between attempts
            if _WIFI_AVAILABLE:
                try:
                    wlan = network.WLAN(network.STA_IF)
                    wlan.disconnect()
                    wlan.active(False)
                    time.sleep(2)
                    wlan.active(True)
                    time.sleep(1)
                except Exception as exc:
                    print("[SOMNI][WIFI] WLAN reset warning: {}".format(exc))
            if feed_wdt is not None:
                feed_wdt()
    print("[SOMNI][WIFI] All {} attempts failed — "
          "proceeding in local-only mode.".format(max_retries))
    return None


def disconnect_wifi():
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
# TLS configuration resolution
# ---------------------------------------------------------------------------

def _as_pem_bytes(x):
    """Normalise a PEM blob to bytes, ending with \\n + NUL.

    MicroPython's mbedtls binding requires the PEM buffer to be a bytes
    object whose length includes a terminating NUL byte; otherwise
    mbedtls_pem_read_buffer() returns MBEDTLS_ERR_X509_INVALID_FORMAT,
    which surfaces as a generic "invalid cert" exception.  This helper
    is defensive so the same blob works on every MicroPython build.
    """
    if x is None:
        return None
    if isinstance(x, str):
        x = x.encode("utf-8")
    elif isinstance(x, bytearray):
        x = bytes(x)
    if not isinstance(x, bytes):
        return None
    if not x.endswith(b"\n"):
        x = x + b"\n"
    if not x.endswith(b"\x00"):
        x = x + b"\x00"
    return x


def _pem_to_der(pem):
    """Convert a single-block PEM (cert or key) to raw DER bytes.

    mbedTLS on some MicroPython builds parses DER more reliably than PEM
    (no NUL-terminator quirks, no base64 step inside the C code).  This
    is the fallback path used when load_cert_chain/load_verify_locations
    rejects PEM input.
    """
    if pem is None:
        return None
    if isinstance(pem, (bytes, bytearray)):
        try:
            text = bytes(pem).decode("utf-8")
        except Exception:
            return None
    else:
        text = pem
    try:
        import ubinascii as _b64
    except ImportError:
        try:
            import binascii as _b64
        except ImportError:
            return None
    body_lines = []
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("-----BEGIN"):
            in_block = True
            continue
        if stripped.startswith("-----END"):
            break
        if in_block:
            body_lines.append(stripped)
    if not body_lines:
        return None
    try:
        return _b64.a2b_base64("".join(body_lines))
    except Exception:
        return None


def _resolve_tls_config():
    """Read TLS material from config.py and return a dict.

    Returns:
        dict with keys: enabled, ca_pem, client_cert_pem, client_key_pem,
        ca_der, client_cert_der, client_key_der, sni.
        enabled=False if TLS is disabled in config.

    All *_pem fields are bytes, NUL-terminated, ready to hand to mbedtls.
    All *_der fields are raw DER bytes for fallback paths.
    """
    cfg = {
        "enabled":         False,
        "ca_pem":          None,
        "client_cert_pem": None,
        "client_key_pem":  None,
        "ca_der":          None,
        "client_cert_der": None,
        "client_key_der":  None,
        "sni":             None,
    }
    try:
        import sys
        config = sys.modules.get("config")
        if config is None:
            import config

        cfg["enabled"] = bool(getattr(config, "GATEWAY_USE_TLS", False))
        cfg["sni"]     = (getattr(config, "GATEWAY_TLS_SNI", None)
                          or getattr(config, "GATEWAY_HOST", None))

        if not cfg["enabled"]:
            return cfg

        ca_raw  = getattr(config, "GATEWAY_CA_CERT_PEM",  None)
        cli_raw = getattr(config, "PICO_CLIENT_CERT_PEM", None)
        key_raw = getattr(config, "PICO_CLIENT_KEY_PEM",  None)

        missing = [name for name, val in
                   (("GATEWAY_CA_CERT_PEM", ca_raw),
                    ("PICO_CLIENT_CERT_PEM", cli_raw),
                    ("PICO_CLIENT_KEY_PEM", key_raw))
                   if not val or "BEGIN" not in (val if isinstance(val, str)
                                                 else val.decode("utf-8", "ignore"))]
        if missing:
            print("[SOMNI][TRANSPORT][TLS] Missing PKI material in config: {}".format(missing))
            print("[SOMNI][TRANSPORT][TLS] Run scripts/setup_gateway_certs.py "
                  "then scripts/embed_pico_cert.py.")
            cfg["enabled"] = False
            return cfg

        cfg["ca_pem"]          = _as_pem_bytes(ca_raw)
        cfg["client_cert_pem"] = _as_pem_bytes(cli_raw)
        cfg["client_key_pem"]  = _as_pem_bytes(key_raw)

        cfg["ca_der"]          = _pem_to_der(ca_raw)
        cfg["client_cert_der"] = _pem_to_der(cli_raw)
        cfg["client_key_der"]  = _pem_to_der(key_raw)

        return cfg
    except Exception as exc:
        print("[SOMNI][TRANSPORT][TLS] config lookup failed: {}".format(exc))
    return cfg


# ---------------------------------------------------------------------------
# TLS socket — mTLS handshake using SSLContext (with legacy fallback)
# ---------------------------------------------------------------------------

def _log_tls_session(sock, label, approach):
    """Print the negotiated TLS version + cipher suite, if the ssl backend
    exposes that information.

    MicroPython's mbedtls binding sometimes exposes ``cipher()`` (returning
    ``(cipher_name, tls_version, secret_bits)`` like CPython); on older
    builds it does not.  We try a few fallbacks before giving up so the
    user always sees *something* identifying which suite is in use.
    """
    info = None
    try:
        if hasattr(sock, "cipher"):
            c = sock.cipher()
            if c:
                if isinstance(c, tuple) and len(c) >= 2:
                    info = "{} / {}".format(c[1], c[0])
                else:
                    info = str(c)
    except Exception:
        info = None
    if info is None:
        try:
            if hasattr(sock, "version"):
                info = sock.version()
        except Exception:
            info = None
    if info is None:
        info = "<cipher info unavailable on this MicroPython build>"
    print("[SOMNI][TRANSPORT][TLS] {} via {} → {}".format(label, approach, info))


def _open_tls_socket(host, port, timeout_s, tls_cfg):
    """Open a TCP socket and complete an mTLS handshake.

    The Pico:
      - presents tls_cfg['client_cert_pem'] + client_key_pem as its identity
      - validates the server cert against tls_cfg['ca_pem']

    Tries three approaches in order (MicroPython ssl API varies by version):
      1. SSLContext with load_cert_chain (modern, MicroPython 1.22+)
      2. SSLContext with cert/key in wrap_socket (hybrid)
      3. ssl.wrap_socket() with cert/key kwargs (legacy)

    Returns the wrapped socket on success, None on any error.
    """
    if not _WIFI_AVAILABLE or not _TLS_AVAILABLE:
        return None

    # Free RAM before TLS — mbedTLS handshake on RP2350 needs ~30 KB.
    if _gc is not None:
        try:
            _gc.collect()
        except Exception:
            pass

    # SNI must be a DNS hostname per RFC 6066 — not an IP address.
    # config.GATEWAY_TLS_SNI should be "somniguard" (a DNS SAN in the
    # server cert).  Fall back to host only if no SNI is configured.
    sni = tls_cfg["sni"] or host

    # Build an ordered list of (label, ca, cert, key) credential tuples to
    # try.  DER first — on the RP2350 MicroPython 1.22+ mbedtls build the
    # PEM parser is brittle around trailing-NUL handling (micropython#14371)
    # and consistently rejects PEM blobs with "invalid cert" / "invalid key"
    # even when the same data parses fine as DER.  Putting DER first means
    # the handshake usually succeeds on the first attempt and we don't spam
    # the log with three failed PEM attempts.  PEM is kept as a fallback
    # for builds where DER is unavailable for some reason.
    cred_attempts = []
    if (tls_cfg["ca_der"] and tls_cfg["client_cert_der"]
            and tls_cfg["client_key_der"]):
        cred_attempts.append(
            ("DER",
             tls_cfg["ca_der"],
             tls_cfg["client_cert_der"],
             tls_cfg["client_key_der"]))
    if (tls_cfg["ca_pem"] and tls_cfg["client_cert_pem"]
            and tls_cfg["client_key_pem"]):
        cred_attempts.append(
            ("PEM-bytes",
             tls_cfg["ca_pem"],
             tls_cfg["client_cert_pem"],
             tls_cfg["client_key_pem"]))
    if not cred_attempts:
        print("[SOMNI][TRANSPORT][TLS] No usable PKI material to attempt.")
        return None

    addr = _socket.getaddrinfo(host, port)[0][-1]

    def _fresh_tcp():
        s = _socket.socket()
        s.settimeout(timeout_s)
        s.connect(addr)
        time.sleep_ms(100)  # let TCP settle on slow hotspot links
        return s

    raw = None
    try:
        for label, ca_buf, cli_buf, key_buf in cred_attempts:
            # --- Approach A: SSLContext + load_cert_chain (modern API) ---
            try:
                if _gc is not None:
                    _gc.collect()
                raw = _fresh_tcp()
                ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
                try:
                    ctx.minimum_version = _ssl.TLSVersion.TLSv1_2
                except Exception:
                    pass
                ctx.verify_mode = _ssl.CERT_REQUIRED
                # check_hostname=False because we authenticate via the CA
                # chain; hostname checking on MicroPython mbedtls is patchy
                # and the SAN list ("somniguard", IPs) is already trusted.
                ctx.check_hostname = False
                ctx.load_verify_locations(cadata=ca_buf)
                ctx.load_cert_chain(cli_buf, key_buf)
                sock = ctx.wrap_socket(raw, server_hostname=sni)
                print("[SOMNI][TRANSPORT][TLS] Connected ({}, SSLContext + load_cert_chain).".format(label))
                _log_tls_session(sock, label, "SSLContext+load_cert_chain")
                return sock
            except (AttributeError, TypeError) as _api_err:
                print("[SOMNI][TRANSPORT][TLS] [{}] SSLContext path unavailable: {}".format(label, _api_err))
                try: raw.close()
                except Exception: pass
                raw = None
            except Exception as _ctx_err:
                print("[SOMNI][TRANSPORT][TLS] [{}] SSLContext handshake failed: {}".format(label, _ctx_err))
                try: raw.close()
                except Exception: pass
                raw = None

            # --- Approach B: SSLContext + wrap_socket(cert=, key=) (hybrid) ---
            try:
                if _gc is not None:
                    _gc.collect()
                raw = _fresh_tcp()
                ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
                ctx.verify_mode = _ssl.CERT_REQUIRED
                ctx.check_hostname = False
                ctx.load_verify_locations(cadata=ca_buf)
                sock = ctx.wrap_socket(
                    raw, server_hostname=sni,
                    cert=cli_buf, key=key_buf,
                )
                print("[SOMNI][TRANSPORT][TLS] Connected ({}, SSLContext + wrap_socket cert/key).".format(label))
                _log_tls_session(sock, label, "SSLContext+wrap_socket cert/key")
                return sock
            except (AttributeError, TypeError) as _api_err:
                print("[SOMNI][TRANSPORT][TLS] [{}] SSLContext hybrid path unavailable: {}".format(label, _api_err))
                try: raw.close()
                except Exception: pass
                raw = None
            except Exception as _ctx_err:
                print("[SOMNI][TRANSPORT][TLS] [{}] SSLContext hybrid handshake failed: {}".format(label, _ctx_err))
                try: raw.close()
                except Exception: pass
                raw = None

            # --- Approach C: Legacy ssl.wrap_socket() with bytes ---
            # MUST be bytes — passing str makes legacy wrap_socket treat
            # the value as a filename and fail with [Errno 2] ENOENT.
            try:
                if _gc is not None:
                    _gc.collect()
                raw = _fresh_tcp()
                sock = _ssl.wrap_socket(
                    raw,
                    server_hostname=sni,
                    cert_reqs=_ssl.CERT_REQUIRED,
                    cadata=ca_buf,
                    cert=cli_buf,
                    key=key_buf,
                )
                print("[SOMNI][TRANSPORT][TLS] Connected ({}, legacy wrap_socket).".format(label))
                _log_tls_session(sock, label, "legacy wrap_socket")
                return sock
            except Exception as _legacy_err:
                print("[SOMNI][TRANSPORT][TLS] [{}] Legacy wrap_socket failed: {}".format(label, _legacy_err))
                try: raw.close()
                except Exception: pass
                raw = None

        print("[SOMNI][TRANSPORT][TLS] All TLS handshake approaches failed.")
        return None

    except Exception as exc:
        print("[SOMNI][TRANSPORT][TLS] handshake failed: {}".format(exc))
        if raw is not None:
            try:
                raw.close()
            except Exception:
                pass
        return None


def _open_tls_socket_with_retry(host, port, timeout_s, tls_cfg, retries=3, delay_s=2):
    """Try _open_tls_socket() up to `retries` times with delays between attempts.

    gc.collect() is called between attempts to maximise free heap for the
    mbedTLS handshake buffer (~30 KB on RP2350).
    """
    for attempt in range(1, retries + 1):
        sock = _open_tls_socket(host, port, timeout_s, tls_cfg)
        if sock is not None:
            return sock
        if attempt < retries:
            print("[SOMNI][TRANSPORT][TLS] Retry {}/{} in {}s…".format(
                attempt, retries, delay_s))
            if _gc is not None:
                _gc.collect()
            time.sleep(delay_s)
    return None


# ---------------------------------------------------------------------------
# HTTP helpers (raw socket + optional TLS)
# ---------------------------------------------------------------------------

def _http_post(host, port, path, body_bytes, extra_headers=None, timeout_s=10):
    """Send an HTTP/1.0 POST and return the status code (0 on error)."""
    if not _WIFI_AVAILABLE:
        print("[SOMNI][TRANSPORT] Socket not available (CPython?); skipping POST.")
        return 0

    tls_cfg = _resolve_tls_config()

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

        if tls_cfg["enabled"]:
            # Use retry wrapper — mbedTLS sometimes needs a second attempt
            # after gc.collect() frees heap for the handshake buffer.
            _retries = 3
            _delay = 2
            try:
                import sys
                _cfg = sys.modules.get("config")
                if _cfg:
                    _retries = getattr(_cfg, "TLS_HANDSHAKE_RETRIES", 3)
                    _delay = getattr(_cfg, "TLS_RETRY_DELAY_S", 2)
            except Exception:
                pass
            sock = _open_tls_socket_with_retry(
                host, port, timeout_s, tls_cfg,
                retries=_retries, delay_s=_delay,
            )
            if sock is None:
                print("[SOMNI][TRANSPORT] TLS handshake failed — refusing plaintext fallback.")
                return 0
        else:
            addr = _socket.getaddrinfo(host, port)[0][-1]
            sock = _socket.socket()
            sock.settimeout(timeout_s)
            sock.connect(addr)

        sock.send(headers.encode("utf-8") + body_bytes)

        response_line = b""
        while b"\n" not in response_line:
            chunk = sock.recv(64)
            if not chunk:
                break
            response_line += chunk

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
# Gateway clock sync over HTTPS (server cert not_before = 2000-01-01 so
# TLS validates even before the Pico's RTC is corrected)
# ---------------------------------------------------------------------------

def sync_time_from_gateway(host, port):
    """Sync the Pico's clock from the gateway's /api/time endpoint over HTTPS.

    The gateway server cert has not_before = 2000-01-01 so the TLS handshake
    succeeds even when the Pico's RTC has just reset to year 2000 on cold
    boot.  After this call, _EPOCH_OFFSET corrects all subsequent timestamps
    so HMAC freshness checks pass.

    All communication is over TLS — no plain HTTP is used.
    """
    global _EPOCH_OFFSET
    if not _WIFI_AVAILABLE:
        return False

    tls_cfg = _resolve_tls_config()

    sock = None
    try:
        req = "GET /api/time HTTP/1.0\r\nHost: {}\r\n\r\n".format(host)

        if tls_cfg["enabled"]:
            sock = _open_tls_socket_with_retry(host, port, 10, tls_cfg)
            if sock is None:
                print("[SOMNI][TRANSPORT] HTTPS clock sync failed — TLS handshake error.")
                return False
        else:
            # TLS disabled (dev/debug mode only — not for production)
            addr = _socket.getaddrinfo(host, port)[0][-1]
            sock = _socket.socket()
            sock.settimeout(10)
            sock.connect(addr)

        sock.send(req.encode("utf-8"))

        resp = b""
        while True:
            chunk = sock.recv(512)
            if not chunk:
                break
            resp += chunk

        body_bytes = resp.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in resp else resp
        data = json.loads(body_bytes.decode("utf-8"))
        server_unix_time = data.get("t")
        if server_unix_time is None:
            return False

        local_raw = int(time.time())
        _EPOCH_OFFSET = server_unix_time - local_raw
        print("[SOMNI][TRANSPORT] Clock synced via HTTPS: Unix={}".format(server_unix_time))
        return True

    except Exception as exc:
        print("[SOMNI][TRANSPORT] Gateway clock sync failed: {}".format(exc))
        return False

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
    """Sign + POST a JSON payload to the gateway. Returns HTTP status."""
    signed = dict(payload)
    signed["nonce"]     = _next_sequence()
    signed["timestamp"] = _get_timestamp_s()

    canonical = _json_sorted(signed)
    signed["hmac"] = _hmac_sha256(hmac_key, canonical)

    body = json.dumps(signed).encode("utf-8")
    status = _http_post(host, port, path, body)

    if status not in (200, 201):
        print("[SOMNI][TRANSPORT] {} {} → HTTP {}".format(host, path, status))
    return status


def start_session(host, port, patient_id, device_id, hmac_key):
    """Open a new sleep session on the gateway. Returns session_id or None."""
    global _sequence_number
    _sequence_number = 0

    payload = {
        "patient_id": patient_id,
        "device_id":  device_id,
        "nonce":      _next_sequence(),
        "timestamp":  _get_timestamp_s(),
    }
    canonical = _json_sorted(payload)
    signed = dict(payload)
    signed["hmac"] = _hmac_sha256(hmac_key, canonical)

    body = json.dumps(signed).encode("utf-8")

    if not _WIFI_AVAILABLE:
        print("[SOMNI][TRANSPORT] start_session: Wi-Fi not available.")
        return None

    tls_cfg = _resolve_tls_config()

    headers  = "POST {} HTTP/1.0\r\n".format(_API_SESSION_START)
    headers += "Host: {}\r\n".format(host)
    headers += "Content-Type: application/json\r\n"
    headers += "Content-Length: {}\r\n\r\n".format(len(body))

    sock = None
    try:
        if tls_cfg["enabled"]:
            sock = _open_tls_socket_with_retry(host, port, 15, tls_cfg)
            if sock is None:
                print("[SOMNI][TRANSPORT] start_session: TLS handshake failed.")
                return None
        else:
            addr = _socket.getaddrinfo(host, port)[0][-1]
            sock = _socket.socket()
            sock.settimeout(15)
            sock.connect(addr)
        sock.send(headers.encode("utf-8") + body)

        resp = b""
        while True:
            chunk = sock.recv(512)
            if not chunk:
                break
            resp += chunk

        resp_body = resp.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in resp else resp
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
    """Close a session on the gateway. Returns True on success."""
    status = send_api(
        host, port, _API_SESSION_END,
        {"session_id": session_id},
        hmac_key,
    )
    return status == 200
