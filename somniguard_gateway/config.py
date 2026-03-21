"""
config.py — SOMNI‑Guard gateway configuration.

All values can be overridden via environment variables so secrets are never
hard‑coded in source.  In production, set these variables in
/etc/somniguard/env or via a systemd EnvironmentFile.

Educational prototype — not a clinically approved device.
"""

import os

# ---------------------------------------------------------------------------
# Filesystem paths
# ---------------------------------------------------------------------------

# Directory that holds the gateway package; used to resolve relative paths.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# SQLite database file.  Use an absolute path in production.
DB_PATH = os.environ.get(
    "SOMNI_DB_PATH",
    os.path.join(_BASE_DIR, "somniguard.db"),
)

# Directory where generated PDF reports are stored.
REPORT_DIR = os.environ.get(
    "SOMNI_REPORT_DIR",
    os.path.join(_BASE_DIR, "reports"),
)

# ---------------------------------------------------------------------------
# Flask security
# ---------------------------------------------------------------------------

# IMPORTANT: set SOMNI_SECRET_KEY to a random 32-byte hex string in
# production.  A fixed default is provided so the app starts without
# configuration, but it MUST be changed before deployment.
SECRET_KEY = os.environ.get(
    "SOMNI_SECRET_KEY",
    "dev-only-secret-key-change-before-production-123",
)

# WTForms CSRF secret (can be the same as SECRET_KEY or a separate value).
WTF_CSRF_SECRET_KEY = os.environ.get("SOMNI_CSRF_KEY", SECRET_KEY)

# ---------------------------------------------------------------------------
# Pico ↔ Gateway shared HMAC key
# ---------------------------------------------------------------------------

# This key must match GATEWAY_HMAC_KEY in somniguard_pico/config.py.
# In production: generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`
# and set SOMNI_HMAC_KEY as an environment variable on the Pi 5.
PICO_HMAC_KEY = os.environ.get(
    "SOMNI_HMAC_KEY",
    "dev-hmac-key-change-this-in-production-32chrs!",
)

# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

FLASK_HOST = os.environ.get("SOMNI_HOST", "0.0.0.0")
FLASK_PORT  = int(os.environ.get("SOMNI_PORT", "5000"))

# Set to False in production (use a proper WSGI server like gunicorn).
FLASK_DEBUG = os.environ.get("SOMNI_DEBUG", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Tailscale VPN overlay
# ---------------------------------------------------------------------------

# Set SOMNI_TAILSCALE_ONLY=true in production to restrict web dashboard access
# to Tailscale peers (100.64.0.0/10) and loopback only.
# When false (default), all IPs can reach the dashboard — use for development.
TAILSCALE_ONLY = os.environ.get("SOMNI_TAILSCALE_ONLY", "false").lower() == "true"

# Comma-separated LAN CIDRs from which the Pico 2W may send telemetry.
# These IPs are allowed through /api/* endpoints even in TAILSCALE_ONLY mode
# because the Pico cannot run Tailscale and communicates over the local LAN.
PICO_ALLOWED_CIDRS = [
    c.strip()
    for c in os.environ.get(
        "SOMNI_PICO_CIDRS",
        "192.168.0.0/16,10.0.0.0/8,172.16.0.0/12",
    ).split(",")
    if c.strip()
]

# ---------------------------------------------------------------------------
# Feature‑extraction thresholds (non‑clinical heuristics)
# ---------------------------------------------------------------------------

# SpO₂ threshold below which a sample is counted as a "desaturation event"
DESATURATION_THRESHOLD_PCT = 90.0

# Acceleration magnitude change (g) that counts as an "arousal / movement"
MOVEMENT_THRESHOLD_G = 0.05
