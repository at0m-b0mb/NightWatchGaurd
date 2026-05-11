"""
config.py — SOMNI-Guard gateway configuration.

All configuration values are sourced from /etc/somniguard/env (a
systemd-style EnvironmentFile, KEY=VALUE per line). The file is parsed
directly at import time so the gateway behaves identically whether it is
launched via systemd, via run.py, or by hand under sudo.

Required keys (the gateway refuses to start if any are missing or empty):
  SOMNI_SECRET_KEY   — Flask session / CSRF secret (32-byte hex)
  SOMNI_HMAC_KEY     — Shared HMAC key with the Pico; must match
                       GATEWAY_HMAC_KEY in somniguard_pico/config.py

Optional keys fall back to safe built-in defaults; see the constants below.

Educational prototype — not a clinically approved device.
"""

import os
import stat

# ---------------------------------------------------------------------------
# /etc/somniguard/env loader
# ---------------------------------------------------------------------------

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_FILE = "/etc/somniguard/env"


def _parse_env_file(path):
    """Parse a systemd-style EnvironmentFile.

    Format: one KEY=VALUE per line. Blank lines and lines starting with '#'
    are ignored. A leading 'export ' is tolerated. Values may be wrapped in
    single or double quotes; the wrapping pair is stripped.
    """
    if not os.path.isfile(path):
        raise RuntimeError(
            "[SOMNI][CONFIG] Required environment file not found: {0}.\n"
            "Create it (mode 0600, owned by the gateway user) with at least:\n"
            "  SOMNI_SECRET_KEY=<32-byte hex>\n"
            "  SOMNI_HMAC_KEY=<must match GATEWAY_HMAC_KEY on the Pico>\n"
            .format(path)
        )

    try:
        st = os.stat(path)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            print(
                "[SOMNI][SECURITY] WARNING: {0} is group/world-accessible "
                "(mode {1:o}). Run: sudo chmod 600 {0}".format(path, st.st_mode & 0o777)
            )
    except OSError:
        pass

    values = {}
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                print(
                    "[SOMNI][CONFIG] Ignoring malformed line {0} in {1}".format(
                        lineno, path
                    )
                )
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            values[key] = val
    return values


_ENV = _parse_env_file(_ENV_FILE)


def _required(key):
    val = _ENV.get(key, "").strip()
    if not val:
        raise RuntimeError(
            "[SOMNI][CONFIG] Required key '{0}' is missing or empty in {1}. "
            "The gateway will not start without it.".format(key, _ENV_FILE)
        )
    return val


def _opt(key, default):
    val = _ENV.get(key)
    if val is None or val == "":
        return default
    return val


# ---------------------------------------------------------------------------
# Filesystem paths
# ---------------------------------------------------------------------------

DB_PATH = _opt("SOMNI_DB_PATH", os.path.join(_BASE_DIR, "somniguard.db"))
REPORT_DIR = _opt("SOMNI_REPORT_DIR", os.path.join(_BASE_DIR, "reports"))

# ---------------------------------------------------------------------------
# Flask / CSRF secrets (required)
# ---------------------------------------------------------------------------

SECRET_KEY = _required("SOMNI_SECRET_KEY")
WTF_CSRF_SECRET_KEY = _opt("SOMNI_CSRF_KEY", SECRET_KEY)

# ---------------------------------------------------------------------------
# Pico ↔ Gateway shared HMAC key (required)
# ---------------------------------------------------------------------------
# Must match GATEWAY_HMAC_KEY in somniguard_pico/config.py.
# Generate with: python3 -c "import secrets; print(secrets.token_hex(32))"

PICO_HMAC_KEY = _required("SOMNI_HMAC_KEY")


def _key_fingerprint(key):
    """Return SHA-256(key)[:8] as hex — safe to log; reveals nothing useful."""
    import hashlib as _h
    return _h.sha256(key.encode("utf-8")).hexdigest()[:8]


print(
    "[SOMNI][CONFIG] Loaded {0}: SOMNI_HMAC_KEY (sha256[:8]={1}, len={2})  "
    "— must match GATEWAY_HMAC_KEY on the Pico.".format(
        _ENV_FILE, _key_fingerprint(PICO_HMAC_KEY), len(PICO_HMAC_KEY),
    )
)

# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

HTTPS_ENABLED = _opt("SOMNI_HTTPS", "true").lower() == "true"
_DEFAULT_PORT = "5443" if HTTPS_ENABLED else "5000"
FLASK_HOST = _opt("SOMNI_HOST", "0.0.0.0")
FLASK_PORT = int(_opt("SOMNI_PORT", _DEFAULT_PORT))
FLASK_DEBUG = _opt("SOMNI_DEBUG", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Tailscale VPN overlay
# ---------------------------------------------------------------------------

TAILSCALE_ONLY = _opt("SOMNI_TAILSCALE_ONLY", "false").lower() == "true"

PICO_ALLOWED_CIDRS = [
    c.strip()
    for c in _opt("SOMNI_PICO_CIDRS", "10.42.0.0/24,127.0.0.0/8").split(",")
    if c.strip()
]

# ---------------------------------------------------------------------------
# Feature-extraction thresholds (non-clinical heuristics)
# ---------------------------------------------------------------------------

DESATURATION_THRESHOLD_PCT = 90.0
MOVEMENT_THRESHOLD_G = 0.05
