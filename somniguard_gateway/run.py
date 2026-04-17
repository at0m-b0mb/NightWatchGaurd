"""
run.py — SOMNI‑Guard gateway entry point.

Usage on Raspberry Pi 5:

    cd somniguard_gateway
    pip install -r requirements.txt
    python run.py

On first run an admin account is created if no users exist.
Set environment variables to override defaults (see config.py):

    export SOMNI_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    export SOMNI_HMAC_KEY="your-shared-pico-hmac-key"
    export SOMNI_DB_PATH="/var/lib/somniguard/somni.db"

To enable HTTPS:

    export SOMNI_HTTPS=true

Educational prototype — not a clinically approved device.
"""

import getpass
import os
import sys

import bcrypt
from security import validate_password_complexity

# Ensure the gateway package directory is on sys.path when run directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config as cfg
import database as db
from app import app


def _bootstrap_admin():
    """
    Create an initial admin account on first run.

    Prompts interactively for username, email, and password if running
    in a terminal.  Skips if any users already exist in the database.

    Args:
        None

    Returns:
        None
    """
    users = db.list_users()
    if users:
        return  # users already exist; skip

    print("\n[SOMNI] No users found. Creating initial admin account.")
    print("[SOMNI] Leave blank to use the default (shown in brackets).\n")

    username = input("Admin username [admin]: ").strip() or "admin"
    email    = input("Admin email [admin@localhost]: ").strip() or "admin@localhost"

    while True:
        pwd = getpass.getpass("Admin password: ")
        valid, errors = validate_password_complexity(pwd)
        if valid:
            break
        for err in errors:
            print("  " + err)

    pwd_hash = bcrypt.hashpw(pwd.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")
    db.create_user(username, email, pwd_hash, role="admin")
    print("\n[SOMNI] Admin user '{}' created.\n".format(username))


def main():
    """
    Initialise the database, bootstrap admin if needed, and start Flask.

    Supports optional HTTPS via self-signed TLS certificates when
    SOMNI_HTTPS=true is set in the environment.

    Args:
        None

    Returns:
        None
    """
    # Start Wi-Fi hotspot (Raspberry Pi only — silently skipped on other platforms)
    if os.environ.get("SOMNI_HOTSPOT", "true").lower() != "false":
        try:
            from hotspot import start_hotspot
            start_hotspot()
        except Exception as exc:
            print("[SOMNI][HOTSPOT][WARN] Could not start hotspot: {}".format(exc))

    # Ensure the report output directory exists
    os.makedirs(cfg.REPORT_DIR, exist_ok=True)

    # Initialise database schema
    db.init_db()

    # Create initial admin on first run (interactive, terminal only)
    if sys.stdin.isatty():
        _bootstrap_admin()
    else:
        print("[SOMNI] Non‑interactive mode — skipping admin bootstrap.")
        print("[SOMNI] Use the /admin/users route to manage accounts.")

    # TLS/HTTPS setup
    ssl_context = None
    use_https = os.environ.get("SOMNI_HTTPS", "false").lower() == "true"
    if use_https:
        try:
            from tls_setup import configure_flask_ssl
            cert_dir = os.path.join(_HERE, "certs")
            ssl_context = configure_flask_ssl(app, cert_dir)
            print("[SOMNI] HTTPS enabled with TLS certificates.")
        except ImportError:
            print("[SOMNI][WARN] tls_setup.py not found; "
                  "HTTPS requested but TLS module unavailable.")
            print("[SOMNI][WARN] Install 'cryptography' package: "
                  "pip install cryptography")
        except Exception as exc:
            print("[SOMNI][WARN] TLS setup failed: {}".format(exc))
            print("[SOMNI][WARN] Falling back to HTTP.")

    protocol = "https" if ssl_context else "http"
    print("[SOMNI] Starting gateway on {}:{}".format(cfg.FLASK_HOST, cfg.FLASK_PORT))
    print("[SOMNI] Dashboard: {}://{}:{}/".format(
        protocol,
        "localhost" if cfg.FLASK_HOST == "0.0.0.0" else cfg.FLASK_HOST,
        cfg.FLASK_PORT,
    ))
    print("[SOMNI] NOT a clinically approved device.\n")

    app.run(
        host=cfg.FLASK_HOST,
        port=cfg.FLASK_PORT,
        debug=cfg.FLASK_DEBUG,
        ssl_context=ssl_context,
    )


if __name__ == "__main__":
    main()
