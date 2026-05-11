"""
run.py -- SOMNI-Guard gateway entry point.

Usage on Raspberry Pi 5:

    cd somniguard_gateway
    pip install -r requirements.txt
    python run.py

On first run an admin account is created if no users exist.
Set environment variables to override defaults (see config.py):

    export SOMNI_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    export SOMNI_HMAC_KEY="your-shared-pico-hmac-key"
    export SOMNI_DB_PATH="/var/lib/somniguard/somni.db"

To enable HTTPS (served via gunicorn for production performance):

    export SOMNI_HTTPS=true

WSGI server selection
---------------------
When gunicorn is installed (it is in requirements.txt), it is used as the
WSGI server automatically.  gunicorn handles concurrent requests properly
which eliminates the slowness of Flask's single-threaded development server,
especially noticeable under HTTPS where each connection involves a TLS
handshake.

    Workers  : 2  (configurable via SOMNI_WORKERS env var)
    Threads  : 4  (configurable via SOMNI_THREADS env var)
    Worker class: gthread (sync threads, no eventlet/gevent needed)

If gunicorn is not importable the code falls back to Flask's built-in server
with threading enabled.

Educational prototype -- not a clinically approved device.
"""

import getpass
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import bcrypt
from security import validate_password_complexity
import config as cfg
import database as db
import mfa as mfa_mod
from app import app


# ---------------------------------------------------------------------------
# Admin bootstrap
# ---------------------------------------------------------------------------

def _bootstrap_admin():
    """Create an initial admin account on first run (interactive terminal only).

    The bootstrapped admin has no MFA configured — the gateway will redirect
    them to /mfa/setup the first time they log in.  An authenticator app
    (Google Authenticator, Aegis, 1Password, Bitwarden) is required to scan
    the QR code that the setup page renders.
    """
    users = db.list_users()
    if users:
        return

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
    print("\n[SOMNI] Admin user '{}' created.".format(username))
    print("[SOMNI] On first login the dashboard will require you to scan a")
    print("[SOMNI] QR code with an authenticator app to enrol two-factor")
    print("[SOMNI] authentication. MFA is mandatory for every user.\n")


# ---------------------------------------------------------------------------
# WSGI server helpers
# ---------------------------------------------------------------------------

def _gunicorn_available() -> bool:
    try:
        import gunicorn  # noqa: F401
        return True
    except ImportError:
        return False


def _run_with_gunicorn(cert_path=None, key_path=None, ca_path=None):
    """Run Flask via gunicorn for production-grade concurrency.

    TLS hardening:
      - Ciphers: ECDHE+AEAD allowlist (TLS 1.2+1.3 only), including CCM
        for MicroPython mbedTLS compatibility on RP2350.
      - cert_reqs = CERT_OPTIONAL: Pico presents its client cert; browsers
        don't need one and use session auth instead.
      - HMAC-SHA256 guards all /api/* endpoints regardless of client cert.

    No plain-HTTP server is started. Pico clock sync uses HTTPS /api/time
    (the server cert not_before is 2000-01-01 so TLS works before clock sync).
    """
    import ssl as _ssl

    from gunicorn.app.base import BaseApplication
    from tls_setup import STRONG_CIPHERS_TLS12

    workers = int(os.environ.get("SOMNI_WORKERS", "2"))
    threads = int(os.environ.get("SOMNI_THREADS", "4"))

    options = {
        "bind":            "{}:{}".format(cfg.FLASK_HOST, cfg.FLASK_PORT),
        "workers":         workers,
        "threads":         threads,
        "worker_class":    "gthread",
        "timeout":         120,
        "server_software": "SOMNI-Guard",
        "accesslog":       "-",
        "errorlog":        "-",
        "loglevel":        "info",
    }

    if cert_path and key_path:
        options["certfile"]   = cert_path
        options["keyfile"]    = key_path
        options["ciphers"]    = STRONG_CIPHERS_TLS12
        options["do_handshake_on_connect"] = True
        # TLS 1.2 minimum — no SSLv3/TLS 1.0/1.1
        options["ssl_version"] = _ssl.PROTOCOL_TLS_SERVER
        if ca_path:
            options["ca_certs"] = ca_path
            options["cert_reqs"] = _ssl.CERT_OPTIONAL
        else:
            options["cert_reqs"] = _ssl.CERT_NONE

    protocol = "https" if cert_path else "http"
    print("[SOMNI] Starting gunicorn ({} workers x {} threads) on {}://{}:{}/".format(
        workers, threads, protocol,
        "localhost" if cfg.FLASK_HOST == "0.0.0.0" else cfg.FLASK_HOST,
        cfg.FLASK_PORT,
    ))
    if cert_path:
        print("[SOMNI] TLS 1.2+1.3, ECDHE+AEAD — HTTPS only, no plain HTTP.")

    class _App(BaseApplication):
        def __init__(self, application, options=None):
            self.options     = options or {}
            self.application = application
            super().__init__()

        def load_config(self):
            for key, value in self.options.items():
                if key in self.cfg.settings and value is not None:
                    self.cfg.set(key.lower(), value)

        def load(self):
            return self.application

    _App(app, options).run()


def _run_dev_server(ssl_context=None):
    """Fall back to Flask's built-in server with threading enabled."""
    protocol = "https" if ssl_context else "http"
    print("[SOMNI] Starting Flask dev server (threaded) on {}://{}:{}/".format(
        protocol,
        "localhost" if cfg.FLASK_HOST == "0.0.0.0" else cfg.FLASK_HOST,
        cfg.FLASK_PORT,
    ))
    print("[SOMNI] NOTE: install gunicorn for production performance: "
          "pip install gunicorn")
    app.run(
        host=cfg.FLASK_HOST,
        port=cfg.FLASK_PORT,
        debug=cfg.FLASK_DEBUG,
        threaded=True,
        ssl_context=ssl_context,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Start Wi-Fi hotspot (Raspberry Pi only -- silently skipped elsewhere)
    if os.environ.get("SOMNI_HOTSPOT", "true").lower() != "false":
        try:
            from hotspot import start_hotspot
            start_hotspot()
        except Exception as exc:
            print("[SOMNI][HOTSPOT][WARN] Could not start hotspot: {}".format(exc))

    os.makedirs(cfg.REPORT_DIR, exist_ok=True)
    db.init_db()
    # MFA tables live in their own schema migration (they reference users.id).
    try:
        with app.app_context():
            mfa_mod.init_mfa_schema()
    except Exception as exc:
        print("[SOMNI][MFA][FATAL] init_mfa_schema failed: {}".format(exc))
        sys.exit(1)

    if sys.stdin.isatty():
        _bootstrap_admin()
    else:
        print("[SOMNI] Non-interactive mode -- skipping admin bootstrap.")
        print("[SOMNI] Use the /admin/users route to manage accounts.")

    # TLS/HTTPS setup — HTTPS is the default; HTTP is opt-in (SOMNI_HTTPS=false).
    cert_path = key_path = ca_path = None
    ssl_context = None
    use_https = cfg.HTTPS_ENABLED

    if use_https:
        try:
            # Auto-generate certificates matching current gateway IP on every boot
            from tls_setup import (
                configure_flask_ssl, get_cert_sha256_fingerprint,
                CA_CERT_NAME, SERVER_CERT_NAME, SERVER_KEY_NAME,
            )
            cert_dir = os.path.join(_HERE, "certs")

            # Run setup script to ensure certs match current IP/hostname
            try:
                setup_script = os.path.join(
                    os.path.dirname(_HERE), "scripts", "setup_gateway_certs.py"
                )
                if os.path.isfile(setup_script):
                    import subprocess
                    result = subprocess.run(
                        [sys.executable, setup_script, "--cert-dir", cert_dir],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if result.stdout:
                        for line in result.stdout.strip().split("\n"):
                            print(line)
                    if result.returncode != 0 and result.stderr:
                        print("[SOMNI][TLS][WARN] Certificate setup warning:")
                        for line in result.stderr.strip().split("\n"):
                            print("[SOMNI][TLS][WARN] " + line)
            except Exception as setup_exc:
                print(f"[SOMNI][TLS][WARN] Could not run cert setup script: {setup_exc}")

            ssl_context = configure_flask_ssl(app, cert_dir)
            cert_path = os.path.join(cert_dir, SERVER_CERT_NAME)
            key_path  = os.path.join(cert_dir, SERVER_KEY_NAME)
            ca_path   = os.path.join(cert_dir, CA_CERT_NAME)
            print("[SOMNI] HTTPS ENABLED — TLS 1.2/1.3, ECDHE+AEAD. No plain HTTP.")
            print("[SOMNI] CA cert (install once in browser): https://10.42.0.1:5443/ca.crt")
            print("[SOMNI] Dashboard: https://10.42.0.1:5443/  or  https://somniguard.local:5443/")
            try:
                print("[SOMNI] CA SHA-256:     {}".format(get_cert_sha256_fingerprint(ca_path)))
                print("[SOMNI] Server SHA-256: {}".format(get_cert_sha256_fingerprint(cert_path)))
                print("[SOMNI] Run scripts/embed_pico_cert.py to push CA + Pico client cert "
                      "into the Pico firmware.")
            except Exception as _fp_exc:
                print("[SOMNI][WARN] Could not compute cert fingerprint: {}".format(_fp_exc))
        except ImportError:
            print("[SOMNI][FATAL] tls_setup.py not found; install 'cryptography': "
                  "pip install cryptography")
            sys.exit(1)
        except Exception as exc:
            print("[SOMNI][FATAL] TLS setup failed: {}".format(exc))
            print("[SOMNI][FATAL] Refusing to start in plaintext HTTP mode.")
            print("[SOMNI][FATAL] Fix the TLS config or set SOMNI_HTTPS=false explicitly")
            print("[SOMNI][FATAL] (debug only — Pico clients require TLS).")
            sys.exit(1)
    else:
        print("[SOMNI][WARN] ============================================")
        print("[SOMNI][WARN] HTTPS is DISABLED (SOMNI_HTTPS=false).")
        print("[SOMNI][WARN] Telemetry and dashboard traffic are PLAINTEXT.")
        print("[SOMNI][WARN] Do not use this mode on any real network.")
        print("[SOMNI][WARN] ============================================")

    print("[SOMNI] NOT a clinically approved device.\n")

    # Choose server: gunicorn (production) or Flask dev server (fallback)
    if _gunicorn_available():
        # gunicorn handles TLS directly via certfile/keyfile/ca_certs options --
        # do NOT pass ssl_context (that's Werkzeug-specific).
        _run_with_gunicorn(
            cert_path=cert_path if use_https else None,
            key_path=key_path  if use_https else None,
            ca_path=ca_path    if use_https else None,
        )
    else:
        _run_dev_server(ssl_context=ssl_context)


if __name__ == "__main__":
    main()
