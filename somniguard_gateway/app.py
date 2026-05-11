"""
app.py — SOMNI‑Guard gateway Flask application.

Provides:
- Web dashboard: login, patients, sessions, reports, user management.
- REST API: /api/session/start, /api/ingest, /api/session/end
  (used by the Pico transport layer, authenticated via HMAC‑SHA256).

Security features:
- All database operations use parameterised queries (see database.py).
- Passwords are hashed with bcrypt.
- CSRF protection is provided by Flask‑WTF on all state‑changing web forms.
- Rate limiting on login and API endpoints (Flask-Limiter).
- Security headers on all responses (HSTS, CSP, X-Frame-Options, etc.).
- Audit logging for all access (login, data, API).
- Session timeout (30 minutes) with secure cookie configuration.
- Account lockout after 10 failed login attempts (15-minute lockout).
- Anti-replay protection via nonce/timestamp validation on API packets.

Educational prototype — not a clinically approved device.
"""

import csv
import hashlib
import hmac as _hmac
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
from flask import (
    Flask, Response, flash, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)
from flask_login import (
    LoginManager, UserMixin, current_user, login_required,
    login_user, logout_user,
)
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect
from wtforms import (
    DateField, FloatField, PasswordField, SelectField, StringField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, Length, NumberRange, Optional

import config as cfg
import database as db
import mfa as mfa_mod
import reports as rpt
import tailscale as ts

# ---------------------------------------------------------------------------
# Flask app + extensions
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"]         = cfg.SECRET_KEY
app.config["WTF_CSRF_SECRET_KEY"] = cfg.WTF_CSRF_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024   # 256 KB max request body

# Session security configuration
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
    "SOMNI_HTTPS", "false").lower() == "true"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
app.config["SESSION_COOKIE_NAME"] = "somni_session"

csrf    = CSRFProtect(app)
login_mgr = LoginManager(app)
login_mgr.login_view = "login"
login_mgr.login_message_category = "warning"
login_mgr.session_protection = "strong"

# ---------------------------------------------------------------------------
# Rate limiting (Flask-Limiter)
# ---------------------------------------------------------------------------

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    limiter = Limiter(
        get_remote_address,
        app=app,
        default_limits=["200 per day", "50 per hour"],
        storage_uri="memory://",
    )
    _LIMITER_AVAILABLE = True
    print("[SOMNI][SECURITY] Flask-Limiter initialised.")
except Exception as _limiter_exc:
    limiter = None
    _LIMITER_AVAILABLE = False
    print("[SOMNI][SECURITY] Flask-Limiter disabled: {}".format(_limiter_exc))

# ---------------------------------------------------------------------------
# Security middleware — import helpers
# ---------------------------------------------------------------------------

try:
    from security import (
        add_security_headers, login_tracker, validate_password_complexity,
        sanitize_string, sanitize_int,
    )
    _SECURITY_MODULE = True
    print("[SOMNI][SECURITY] Security module loaded.")
except Exception as _sec_exc:
    _SECURITY_MODULE = False
    print("[SOMNI][SECURITY] Security module disabled: {}".format(_sec_exc))

    # Fallback stubs
    def add_security_headers(response):
        """Fallback security headers."""
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    class _StubTracker:
        def is_account_locked(self, ip):
            return False
        def record_failed_login(self, ip):
            pass
        def record_successful_login(self, ip):
            pass
        def get_remaining_lockout_seconds(self, ip):
            return 0

    login_tracker = _StubTracker()

    def validate_password_complexity(pw):
        return (len(pw) >= 8, ["Password must be at least 8 characters"] if len(pw) < 8 else [])

    def sanitize_string(v, max_length=1000):
        if not isinstance(v, str):
            return ""
        return v.strip()[:max_length]

    def sanitize_int(v, min_val=None, max_val=None):
        try:
            v = int(v)
            if min_val is not None:
                v = max(v, min_val)
            if max_val is not None:
                v = min(v, max_val)
            return v
        except (TypeError, ValueError):
            return None

# ---------------------------------------------------------------------------
# Audit logging — import helpers
# ---------------------------------------------------------------------------

class _StubAudit:
    """No-op audit logger used when audit.py fails to load."""
    def log_login_attempt(self, *a, **kw): pass
    def log_login_lockout(self, *a, **kw): pass
    def log_logout(self, *a, **kw): pass
    def log_data_access(self, *a, **kw): pass
    def log_api_access(self, *a, **kw): pass
    def log_report_generated(self, *a, **kw): pass
    def log_report_downloaded(self, *a, **kw): pass
    def log_user_created(self, *a, **kw): pass
    def log_user_deleted(self, *a, **kw): pass
    def log_security_event(self, *a, **kw): pass

audit_log = _StubAudit()  # safe default — overwritten below on success

try:
    from audit import init_audit_log as _init_audit
    _result = _init_audit()
    if _result is not None:
        audit_log = _result
    _AUDIT_AVAILABLE = True
    print("[SOMNI][AUDIT] Audit logging initialised.")
except Exception as _audit_exc:
    _AUDIT_AVAILABLE = False
    print("[SOMNI][AUDIT] Audit logging disabled: {}".format(_audit_exc))


# ---------------------------------------------------------------------------
# Anti-replay state for API packets
# ---------------------------------------------------------------------------

# High-water mark for sequence numbers per device session.
# Keyed by session_id (int).  Entries are removed when a session ends.
_nonce_hwm = {}   # {session_id: highest_seen_nonce}

# Maximum number of concurrent sessions tracked in _nonce_hwm.
# If this limit is exceeded the oldest entry is evicted to prevent
# unbounded memory growth on a long-running gateway.
_NONCE_HWM_MAX_SESSIONS = 1000

# Timestamp staleness window (seconds) — reject packets older than this
_TIMESTAMP_WINDOW_S = 300   # 5 minutes

# ---------------------------------------------------------------------------
# Hard caps for /api/* request bodies (PT2/PT5/PT9 — input fuzzing)
# ---------------------------------------------------------------------------

# An ingest packet is ~500 bytes of JSON.  8 KB is generous and catches the
# memory-pressure / DoS-by-large-body class of attack long before the
# 256 KB application-wide MAX_CONTENT_LENGTH would.
_API_MAX_BODY_BYTES = 8 * 1024

# Numeric bounds for telemetry fields.  Anything outside these is treated as
# a fuzzing/malformed input and the row is rejected (not silently coerced).
_TELEMETRY_BOUNDS = {
    "timestamp_ms": (0, 2**63 - 1),
    "spo2.spo2":    (0.0, 100.0),
    "spo2.hr":      (0.0, 300.0),
    "spo2.ir_raw":  (0, 2**24),
    "spo2.red_raw": (0, 2**24),
    "accel.x":      (-16.0, 16.0),
    "accel.y":      (-16.0, 16.0),
    "accel.z":      (-16.0, 16.0),
    "gsr.raw":      (0, 65535),
    "gsr.voltage":  (-1.0, 5.0),
    "gsr.conductance_us": (0.0, 1_000_000.0),
}

# Max allowed nonce — int64 ceiling.  Stops attackers from "jumping" the
# nonce HWM up to a value that legit packets can never exceed.
_NONCE_MAX = 2**63 - 1

# ---------------------------------------------------------------------------
# Clinical alert thresholds (non-clinical defaults; tune per deployment)
# ---------------------------------------------------------------------------

# Triggered alerts are stored once per session+metric+severity to avoid
# storms.  These thresholds are educational defaults and are not validated
# against any clinical guideline.
ALERT_THRESHOLDS = {
    "spo2_critical_low":  85.0,    # %
    "spo2_warning_low":   90.0,    # %
    "hr_critical_low":    40.0,    # bpm
    "hr_warning_low":     50.0,    # bpm
    "hr_warning_high":   120.0,    # bpm
    "hr_critical_high":  140.0,    # bpm
}

# Suppress duplicate alert insertions within this window per (session, key).
_ALERT_DEDUP_WINDOW_S = 60
_recent_alerts = {}   # {(session_id, key): epoch_seconds}
_RECENT_ALERTS_MAX = 4000


def _maybe_record_alert(session_id, key, severity, metric, value,
                        threshold, message):
    """Insert an alert unless the same key fired recently for this session."""
    now = time.time()
    cache_key = (session_id, key)
    last = _recent_alerts.get(cache_key)
    if last is not None and (now - last) < _ALERT_DEDUP_WINDOW_S:
        return
    if len(_recent_alerts) >= _RECENT_ALERTS_MAX:
        # Best-effort eviction — drop the oldest tracked entry.
        try:
            del _recent_alerts[next(iter(_recent_alerts))]
        except StopIteration:
            pass
    _recent_alerts[cache_key] = now
    try:
        db.insert_alert(session_id, key, severity, metric=metric,
                        measured_value=value, threshold_value=threshold,
                        message=message)
    except Exception as exc:
        print("[SOMNI][ALERT] Could not record alert: {}".format(exc))


def _evaluate_alerts(session_id, body):
    """Inspect a telemetry body and persist any threshold breaches."""
    spo2_block = body.get("spo2") or {}
    spo2_val = spo2_block.get("spo2") if spo2_block.get("valid") else None
    hr_val   = spo2_block.get("hr")   if spo2_block.get("valid") else None

    if spo2_val is not None:
        if spo2_val < ALERT_THRESHOLDS["spo2_critical_low"]:
            _maybe_record_alert(
                session_id, "spo2_critical_low", "critical", "spo2",
                spo2_val, ALERT_THRESHOLDS["spo2_critical_low"],
                "Critical desaturation: SpO2 below {:.0f}%".format(
                    ALERT_THRESHOLDS["spo2_critical_low"]),
            )
        elif spo2_val < ALERT_THRESHOLDS["spo2_warning_low"]:
            _maybe_record_alert(
                session_id, "spo2_warning_low", "warning", "spo2",
                spo2_val, ALERT_THRESHOLDS["spo2_warning_low"],
                "Low SpO2: below {:.0f}%".format(
                    ALERT_THRESHOLDS["spo2_warning_low"]),
            )

    if hr_val is not None:
        if hr_val < ALERT_THRESHOLDS["hr_critical_low"]:
            _maybe_record_alert(
                session_id, "hr_critical_low", "critical", "hr",
                hr_val, ALERT_THRESHOLDS["hr_critical_low"],
                "Severe bradycardia: HR below {:.0f} bpm".format(
                    ALERT_THRESHOLDS["hr_critical_low"]),
            )
        elif hr_val < ALERT_THRESHOLDS["hr_warning_low"]:
            _maybe_record_alert(
                session_id, "hr_warning_low", "warning", "hr",
                hr_val, ALERT_THRESHOLDS["hr_warning_low"],
                "Bradycardia: HR below {:.0f} bpm".format(
                    ALERT_THRESHOLDS["hr_warning_low"]),
            )
        elif hr_val > ALERT_THRESHOLDS["hr_critical_high"]:
            _maybe_record_alert(
                session_id, "hr_critical_high", "critical", "hr",
                hr_val, ALERT_THRESHOLDS["hr_critical_high"],
                "Severe tachycardia: HR above {:.0f} bpm".format(
                    ALERT_THRESHOLDS["hr_critical_high"]),
            )
        elif hr_val > ALERT_THRESHOLDS["hr_warning_high"]:
            _maybe_record_alert(
                session_id, "hr_warning_high", "warning", "hr",
                hr_val, ALERT_THRESHOLDS["hr_warning_high"],
                "Tachycardia: HR above {:.0f} bpm".format(
                    ALERT_THRESHOLDS["hr_warning_high"]),
            )


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_telemetry_payload(body):
    """Bounds-check a telemetry body before it reaches the database.

    Returns (ok, error_string).  Used to defend against PT2 (random data
    injection from cloud) and PT9 (fuzzing) — the gateway must not crash,
    leak, or insert garbage when the payload is malformed.
    """
    if not isinstance(body, dict):
        return False, "body must be an object"

    sid = body.get("session_id")
    if not isinstance(sid, int) or sid <= 0 or sid > _NONCE_MAX:
        return False, "invalid session_id"

    nonce = body.get("nonce")
    if not isinstance(nonce, int) or nonce <= 0 or nonce > _NONCE_MAX:
        return False, "invalid nonce"

    ts = body.get("timestamp")
    if not isinstance(ts, int) or ts < 0 or ts > _NONCE_MAX:
        return False, "invalid timestamp"

    # All sub-dicts are optional — but if present they must be the right type
    for sub in ("spo2", "accel", "gsr"):
        if sub in body and not isinstance(body[sub], dict):
            return False, f"{sub} must be an object"

    # Bound every numeric field we know about
    for path, (lo, hi) in _TELEMETRY_BOUNDS.items():
        cur = body
        for part in path.split("."):
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(part)
        if cur is None:
            continue
        if not _is_number(cur):
            return False, f"{path} must be numeric"
        if cur < lo or cur > hi:
            return False, f"{path} out of range"

    return True, ""


def _nonce_hwm_set(session_id, nonce):
    """Update the nonce high-water mark for a session, evicting stale entries if needed."""
    if session_id not in _nonce_hwm and len(_nonce_hwm) >= _NONCE_HWM_MAX_SESSIONS:
        # Evict the oldest tracked session to prevent unbounded memory growth.
        # dict.keys() preserves insertion order in Python 3.7+.
        oldest = next(iter(_nonce_hwm))
        del _nonce_hwm[oldest]
    _nonce_hwm[session_id] = nonce


# ---------------------------------------------------------------------------
# Security headers — after_request hook
# ---------------------------------------------------------------------------

@app.after_request
def _apply_security_headers(response):
    """Apply security headers to every response."""
    return add_security_headers(response)


# ---------------------------------------------------------------------------
# Session management — make sessions permanent with timeout
# ---------------------------------------------------------------------------

@app.before_request
def _make_session_permanent():
    """Make all sessions permanent so the timeout applies."""
    session.permanent = True


@app.context_processor
def _inject_globals():
    """Expose globals every authenticated template needs (alert badge, etc.)."""
    unack = 0
    if current_user.is_authenticated:
        try:
            unack = db.count_unacknowledged_alerts()
        except Exception:
            unack = 0
    return {"unack_count": unack}


# ---------------------------------------------------------------------------
# Network-access policy (Tailscale enforcement)
# ---------------------------------------------------------------------------

@app.before_request
def _enforce_network_policy():
    """
    Enforce the Tailscale-only access policy when SOMNI_TAILSCALE_ONLY=true.

    Web-dashboard routes require a Tailscale peer IP (100.64.0.0/10) or
    loopback.  Pico telemetry API routes (/api/*) additionally allow private
    LAN IPs defined in PICO_ALLOWED_CIDRS, because the Pico 2 W cannot run
    Tailscale and communicates over the local Wi-Fi LAN segment.

    In development mode (TAILSCALE_ONLY=false, the default), all IPs are
    permitted so the gateway works without Tailscale installed.

    Args:
        None

    Returns:
        flask.Response | None: HTTP 403 JSON response if access is denied,
                               None to continue normal request processing.
    """
    is_api = request.path.startswith("/api/")
    allowed = ts.check_network_policy(
        remote_addr=request.remote_addr,
        tailscale_only=cfg.TAILSCALE_ONLY,
        is_api_path=is_api,
        pico_cidrs=cfg.PICO_ALLOWED_CIDRS,
    )
    if not allowed:
        if _AUDIT_AVAILABLE:
            audit_log.log_security_event(
                "ACCESS_DENIED",
                request.remote_addr,
                {"path": request.path, "reason": "network_policy"},
            )
        return (
            jsonify({
                "error": "Access denied: connect via Tailscale VPN to reach this service.",
                "tailscale_only": True,
            }),
            403,
        )


# ---------------------------------------------------------------------------
# Database connection teardown
# ---------------------------------------------------------------------------

@app.teardown_appcontext
def _close_db(exception):
    """Close the database connection at the end of each request."""
    db.close_db()


# ---------------------------------------------------------------------------
# Flask‑Login user proxy
# ---------------------------------------------------------------------------

class _UserProxy(UserMixin):
    """Thin wrapper around a sqlite3.Row to satisfy Flask‑Login."""

    def __init__(self, row):
        self._row = row

    def get_id(self):
        return str(self._row["id"])

    @property
    def id(self):
        return self._row["id"]

    @property
    def username(self):
        return self._row["username"]

    @property
    def email(self):
        return self._row["email"]

    @property
    def role(self):
        return self._row["role"]


@login_mgr.user_loader
def _load_user(user_id):
    row = db.get_user_by_id(int(user_id))
    return _UserProxy(row) if row else None


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def admin_required(f):
    """Redirect non‑admin users to the dashboard."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != "admin":
            flash("Administrator access required.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


def role_required(*allowed_roles):
    """Redirect users without one of the specified roles to the dashboard."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role not in allowed_roles:
                flash("You do not have permission to access this page.", "danger")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return decorated
    return decorator


# ---------------------------------------------------------------------------
# MFA / first-login enforcement (runs before every authenticated request)
# ---------------------------------------------------------------------------

# Routes that the MFA / password-change gates allow even when MFA or a
# password change is still pending.  Anything outside this set redirects
# to the appropriate setup page.
_MFA_EXEMPT_ENDPOINTS = {
    "login", "logout", "mfa_setup", "mfa_setup_qr", "mfa_backup_codes",
    "force_password_change", "download_ca_cert", "static",
    "api_session_start", "api_ingest", "api_session_end", "api_time",
}


@app.before_request
def _enforce_mfa_and_password_gates():
    """Funnel logged-in users through MFA enrolment / password reset first."""
    if not current_user.is_authenticated:
        return None
    endpoint = request.endpoint or ""
    if endpoint in _MFA_EXEMPT_ENDPOINTS:
        return None
    if endpoint.startswith("api_"):
        return None

    # Force password change before anything else
    user_row = db.get_user_by_id(current_user.id)
    if user_row and user_row["must_change_password"]:
        flash("You must change your password before continuing.", "warning")
        return redirect(url_for("force_password_change"))

    # MFA enrolment is mandatory for every account
    if not mfa_mod.is_mfa_enabled(current_user.id):
        flash("Two-factor authentication is required. Please enrol now.",
              "warning")
        return redirect(url_for("mfa_setup"))
    return None


# ---------------------------------------------------------------------------
# Forms (WTForms + CSRF)
# ---------------------------------------------------------------------------

class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(1, 64)])
    password = PasswordField("Password", validators=[DataRequired(), Length(1, 128)])


class NewUserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(3, 64)])
    email    = StringField("Email",    validators=[DataRequired(), Email(), Length(5, 120)])
    # Length matches PASSWORD_MIN_LENGTH / PASSWORD_MAX_LENGTH in security.py.
    # validate_password_complexity() runs the full rule set after this.
    password = PasswordField("Password", validators=[DataRequired(), Length(14, 128)])
    role     = SelectField("Role", choices=[
        ("admin", "Admin"),
        ("doctor", "Doctor"),
        ("clinician", "Clinician"),
        ("nurse", "Nurse"),
        ("viewer", "Viewer"),
    ])


class NewPatientForm(FlaskForm):
    name          = StringField("Patient Name", validators=[DataRequired(), Length(1, 120)])
    mrn           = StringField("Medical Record Number (MRN)",
                                validators=[Optional(), Length(max=64)])
    dob           = DateField("Date of Birth", format="%Y-%m-%d",
                              validators=[Optional()])
    sex           = SelectField("Sex", choices=[
        ("",         "—"),
        ("female",   "Female"),
        ("male",     "Male"),
        ("intersex", "Intersex"),
        ("other",    "Other / Prefer not to say"),
    ], validators=[Optional()])
    contact_phone = StringField("Contact Phone",
                                validators=[Optional(), Length(max=40)])
    contact_email = StringField("Contact Email",
                                validators=[Optional(), Length(max=120)])
    allergies     = TextAreaField("Allergies",
                                  validators=[Optional(), Length(max=1000)])
    height_cm     = FloatField("Height (cm)", validators=[
        Optional(), NumberRange(min=20, max=260)])
    weight_kg     = FloatField("Weight (kg)", validators=[
        Optional(), NumberRange(min=0.5, max=400)])
    notes         = TextAreaField("Clinical notes",
                                  validators=[Optional(), Length(max=2000)])


class MfaCodeForm(FlaskForm):
    code = StringField(
        "Authentication code",
        validators=[DataRequired(), Length(6, 32)],
    )


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField(
        "Current password",
        validators=[DataRequired(), Length(1, 128)],
    )
    new_password = PasswordField(
        "New password",
        validators=[DataRequired(), Length(14, 128)],
    )
    confirm_password = PasswordField(
        "Confirm new password",
        validators=[DataRequired(), Length(14, 128)],
    )


class ClinicalNoteForm(FlaskForm):
    body = TextAreaField(
        "Clinical observation",
        validators=[DataRequired(), Length(min=1, max=4000)],
    )


class DischargeForm(FlaskForm):
    discharge_notes = TextAreaField(
        "Discharge summary",
        validators=[Optional(), Length(max=2000)],
    )


# ---------------------------------------------------------------------------
# Web routes — authentication
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Redirect root to dashboard or login."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/ca.crt")
def download_ca_cert():
    """Serve the Root CA certificate for browser installation.

    No authentication required — the CA cert is public information (it only
    contains the public key, never the private key).  Users download this
    once and install it in their OS / browser trust store so the HTTPS
    dashboard works without a certificate warning.

    Users who haven't yet trusted the cert can click "Advanced → Proceed" on the
    browser warning to reach this page, or SCP the file from the Pi directly.
    """
    ca_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", "ca.crt")
    if not os.path.isfile(ca_path):
        return "CA certificate not found — run setup_gateway_certs.py first.", 404
    return send_file(
        ca_path,
        mimetype="application/x-pem-file",
        as_attachment=True,
        download_name="somniguard-ca.crt",
    )


# Session keys used to bridge the password -> TOTP step.  We never set
# Flask-Login's "logged_in" state until BOTH factors have been verified.
_MFA_PENDING_USER  = "mfa_pending_user_id"
_MFA_PENDING_NEXT  = "mfa_pending_next"
_MFA_PENDING_ISSUED = "mfa_pending_issued_at"
_MFA_PENDING_TTL_S  = 5 * 60   # 5 minutes


def _clear_mfa_pending():
    session.pop(_MFA_PENDING_USER,   None)
    session.pop(_MFA_PENDING_NEXT,   None)
    session.pop(_MFA_PENDING_ISSUED, None)


def _mfa_pending_user_row():
    """Return the user row for the in-progress login, or None if absent/expired."""
    pending = session.get(_MFA_PENDING_USER)
    issued  = session.get(_MFA_PENDING_ISSUED, 0)
    if not pending:
        return None
    if (time.time() - float(issued)) > _MFA_PENDING_TTL_S:
        _clear_mfa_pending()
        return None
    return db.get_user_by_id(int(pending))


@app.route("/login", methods=["GET", "POST"])
def login():
    """Phase 1: username + password.  On success we stash the user_id in the
    session and redirect to /mfa/verify (or /mfa/setup for first-login).
    Flask-Login is NOT engaged until the second factor passes.
    """
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    # Check for account lockout
    client_ip = request.remote_addr or "unknown"
    if login_tracker.is_account_locked(client_ip):
        remaining = login_tracker.get_remaining_lockout_seconds(client_ip)
        if _AUDIT_AVAILABLE:
            audit_log.log_login_lockout(client_ip, remaining)
        flash("Too many failed attempts. Try again in {} seconds.".format(
            remaining), "danger")
        return render_template("login.html", form=LoginForm())

    form = LoginForm()
    if form.validate_on_submit():
        username = sanitize_string(form.username.data, max_length=64)
        user_row = db.get_user_by_username(username)
        if (user_row
                and user_row["is_active"]
                and bcrypt.checkpw(
                    form.password.data.encode("utf-8"),
                    user_row["password_hash"].encode("utf-8"),
                )):
            login_tracker.record_successful_login(client_ip)
            if _AUDIT_AVAILABLE:
                audit_log.log_login_attempt(username, client_ip, True)

            # Stash a one-shot pre-auth ticket — full login happens after
            # MFA verifies (or after first-time MFA enrolment).
            _clear_mfa_pending()
            session[_MFA_PENDING_USER]   = user_row["id"]
            session[_MFA_PENDING_ISSUED] = time.time()
            next_page = request.args.get("next")
            if next_page and _is_safe_url(next_page):
                session[_MFA_PENDING_NEXT] = next_page

            if mfa_mod.is_mfa_enabled(user_row["id"]):
                return redirect(url_for("mfa_verify"))
            # First-time MFA enrolment: complete primary login first so the
            # enrolment page can read current_user, then immediately gate to
            # /mfa/setup via _enforce_mfa_and_password_gates.
            login_user(_UserProxy(user_row))
            db.record_user_login(user_row["id"], client_ip)
            _clear_mfa_pending()
            flash("Two-factor authentication is required for every account. "
                  "Please enrol now.", "warning")
            return redirect(url_for("mfa_setup"))

        # Failure path — constant-time delay
        time.sleep(0.5)
        login_tracker.record_failed_login(client_ip)
        if _AUDIT_AVAILABLE:
            audit_log.log_login_attempt(
                form.username.data, client_ip, False,
                reason="invalid_credentials",
            )
        flash("Invalid username or password.", "danger")

    return render_template("login.html", form=form)


# Apply rate limit to login if limiter available
if _LIMITER_AVAILABLE:
    login = limiter.limit("5 per minute")(login)


@app.route("/mfa/verify", methods=["GET", "POST"])
def mfa_verify():
    """Phase 2: TOTP / backup-code verification.  Engages Flask-Login on
    success so the rest of the dashboard becomes accessible."""
    user_row = _mfa_pending_user_row()
    if not user_row:
        flash("Login session expired. Please sign in again.", "info")
        return redirect(url_for("login"))

    form = MfaCodeForm()
    client_ip = request.remote_addr or "unknown"
    if form.validate_on_submit():
        code = sanitize_string(form.code.data, max_length=32)
        verified = False
        if len(code) == 6 and code.isdigit():
            verified = mfa_mod.verify_totp(user_row["id"], code)
        if not verified:
            verified = mfa_mod.consume_backup_code(user_row["id"], code)

        if verified:
            login_user(_UserProxy(user_row))
            db.record_user_login(user_row["id"], client_ip)
            next_page = session.pop(_MFA_PENDING_NEXT, None)
            _clear_mfa_pending()
            if _AUDIT_AVAILABLE:
                audit_log.log_security_event(
                    "MFA_SUCCESS", client_ip,
                    {"username": user_row["username"]},
                )
            flash("Welcome back, {}!".format(user_row["username"]), "success")
            if next_page and _is_safe_url(next_page):
                return redirect(next_page)
            return redirect(url_for("dashboard"))

        time.sleep(0.5)
        login_tracker.record_failed_login(client_ip)
        if _AUDIT_AVAILABLE:
            audit_log.log_security_event(
                "MFA_FAILURE", client_ip,
                {"username": user_row["username"]},
            )
        flash("Invalid authentication code. Please try again.", "danger")

    return render_template("mfa_verify.html", form=form)


if _LIMITER_AVAILABLE:
    mfa_verify = limiter.limit("10 per minute")(mfa_verify)


@app.route("/logout")
@login_required
def logout():
    """Log out the current user and redirect to login."""
    username = current_user.username
    client_ip = request.remote_addr or "unknown"
    logout_user()
    _clear_mfa_pending()
    if _AUDIT_AVAILABLE:
        audit_log.log_logout(username, client_ip)
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# MFA: enrolment, QR code, backup codes, disable.
# ---------------------------------------------------------------------------

@app.route("/mfa/setup", methods=["GET", "POST"])
@login_required
def mfa_setup():
    """First-time TOTP enrolment.  Allowed even when MFA is not yet enabled."""
    if mfa_mod.is_mfa_enabled(current_user.id):
        flash("Two-factor authentication is already enabled.", "info")
        return redirect(url_for("user_profile"))

    form = MfaCodeForm()
    secret, _uri = mfa_mod.begin_enrolment(current_user.id)
    if form.validate_on_submit():
        code = sanitize_string(form.code.data, max_length=32)
        if mfa_mod.finish_enrolment(current_user.id, code):
            codes = mfa_mod.generate_backup_codes(current_user.id)
            if _AUDIT_AVAILABLE:
                audit_log.log_security_event(
                    "MFA_ENROLLED", request.remote_addr or "unknown",
                    {"username": current_user.username},
                )
            return render_template("mfa_backup_codes.html", codes=codes)
        flash("That code did not match. Make sure your authenticator clock "
              "is in sync and try again.", "danger")

    return render_template("mfa_setup.html",
                           form=form, secret=secret,
                           username=current_user.username)


@app.route("/mfa/qr.png")
@login_required
def mfa_setup_qr():
    """Render the provisioning URI as a PNG QR code (CSP-friendly)."""
    if mfa_mod.is_mfa_enabled(current_user.id):
        return ("MFA already enabled.", 403)
    _secret, uri = mfa_mod.begin_enrolment(current_user.id)
    return Response(mfa_mod.qr_png_bytes(uri), mimetype="image/png")


@app.route("/mfa/backup-codes", methods=["POST"])
@login_required
def mfa_backup_codes():
    """Regenerate backup codes (invalidates any previously-issued set)."""
    if not mfa_mod.is_mfa_enabled(current_user.id):
        flash("Enable two-factor authentication first.", "warning")
        return redirect(url_for("mfa_setup"))
    codes = mfa_mod.generate_backup_codes(current_user.id)
    if _AUDIT_AVAILABLE:
        audit_log.log_security_event(
            "MFA_BACKUP_CODES_REGENERATED",
            request.remote_addr or "unknown",
            {"username": current_user.username},
        )
    return render_template("mfa_backup_codes.html", codes=codes)


@app.route("/mfa/disable", methods=["POST"])
@login_required
@admin_required
def mfa_disable():
    """Admin-only: disable MFA for a target user (forces them to re-enrol on
    next login).  Useful when a clinician loses their authenticator."""
    target_id = sanitize_int(request.form.get("user_id"), min_val=1)
    if target_id is None:
        flash("Invalid user id.", "danger")
        return redirect(url_for("manage_users"))
    target = db.get_user_by_id(target_id)
    if target is None:
        flash("User not found.", "danger")
        return redirect(url_for("manage_users"))
    mfa_mod.disable_mfa(target_id)
    if _AUDIT_AVAILABLE:
        audit_log.log_security_event(
            "MFA_RESET_BY_ADMIN", request.remote_addr or "unknown",
            {"admin": current_user.username, "target": target["username"]},
        )
    flash("MFA reset for {} — they will be asked to enrol again on next "
          "login.".format(target["username"]), "info")
    return redirect(url_for("manage_users"))


# ---------------------------------------------------------------------------
# Password management
# ---------------------------------------------------------------------------

@app.route("/account/password", methods=["GET", "POST"])
@login_required
def force_password_change():
    """Self-service password change. Required when must_change_password=1."""
    form = ChangePasswordForm()
    user_row = db.get_user_by_id(current_user.id)
    forced = bool(user_row and user_row["must_change_password"])

    if form.validate_on_submit():
        if not bcrypt.checkpw(
            form.current_password.data.encode("utf-8"),
            user_row["password_hash"].encode("utf-8"),
        ):
            time.sleep(0.5)
            flash("Current password is incorrect.", "danger")
            return render_template("change_password.html",
                                   form=form, forced=forced)
        if form.new_password.data != form.confirm_password.data:
            flash("New password and confirmation do not match.", "danger")
            return render_template("change_password.html",
                                   form=form, forced=forced)
        if form.new_password.data == form.current_password.data:
            flash("New password must differ from the current password.",
                  "danger")
            return render_template("change_password.html",
                                   form=form, forced=forced)

        valid, errors = validate_password_complexity(form.new_password.data)
        if not valid:
            for err in errors:
                flash(err, "danger")
            return render_template("change_password.html",
                                   form=form, forced=forced)

        new_hash = bcrypt.hashpw(
            form.new_password.data.encode("utf-8"),
            bcrypt.gensalt(rounds=12),
        ).decode("utf-8")
        db.update_user_password(current_user.id, new_hash, must_change=0)
        if _AUDIT_AVAILABLE:
            audit_log.log_security_event(
                "PASSWORD_CHANGED", request.remote_addr or "unknown",
                {"username": current_user.username},
            )
        flash("Password updated.", "success")
        return redirect(url_for("user_profile"))

    return render_template("change_password.html", form=form, forced=forced)


# ---------------------------------------------------------------------------
# User profile (self-service)
# ---------------------------------------------------------------------------

@app.route("/account")
@login_required
def user_profile():
    """User-facing settings: MFA status, recent sign-ins, password age."""
    user_row = db.get_user_by_id(current_user.id)
    mfa_on = mfa_mod.is_mfa_enabled(current_user.id)
    return render_template("user_profile.html", user=user_row, mfa_on=mfa_on)


# ---------------------------------------------------------------------------
# Web routes — dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    """Show recent sessions across all patients."""
    if _AUDIT_AVAILABLE:
        audit_log.log_data_access(
            current_user.username, request.remote_addr,
            "dashboard", None, "view",
        )
    sessions = db.list_sessions()
    patients = db.list_patients()
    recent_audit = db.get_audit_logs(limit=5) if _AUDIT_AVAILABLE else []
    return render_template("dashboard.html",
                           sessions=sessions, patients=patients, recent_audit=recent_audit)


# ---------------------------------------------------------------------------
# Web routes — patients
# ---------------------------------------------------------------------------

@app.route("/patients")
@login_required
def patients():
    """List all patients (active by default; ?show=archived to include them)."""
    if _AUDIT_AVAILABLE:
        audit_log.log_data_access(
            current_user.username, request.remote_addr,
            "patients", None, "list",
        )
    show_archived = request.args.get("show") == "archived"
    all_patients = db.list_patients(include_archived=show_archived)
    form = NewPatientForm()
    return render_template("patients.html",
                           patients=all_patients,
                           form=form,
                           show_archived=show_archived)


@app.route("/patients/new", methods=["POST"])
@login_required
@role_required("admin", "doctor", "clinician", "nurse")
def new_patient():
    """Create a new patient record."""
    form = NewPatientForm()
    if form.validate_on_submit():
        dob_str = form.dob.data.isoformat() if form.dob.data else None
        db.create_patient(
            name=sanitize_string(form.name.data, max_length=120),
            dob=dob_str,
            notes=sanitize_string(form.notes.data, max_length=2000) if form.notes.data else None,
            created_by=current_user.id,
            mrn=sanitize_string(form.mrn.data or "", max_length=64) or None,
            sex=sanitize_string(form.sex.data or "", max_length=20) or None,
            contact_phone=sanitize_string(form.contact_phone.data or "",
                                          max_length=40) or None,
            contact_email=sanitize_string(form.contact_email.data or "",
                                          max_length=120).lower() or None,
            allergies=sanitize_string(form.allergies.data or "",
                                      max_length=1000) or None,
            height_cm=form.height_cm.data,
            weight_kg=form.weight_kg.data,
        )
        if _AUDIT_AVAILABLE:
            audit_log.log_data_access(
                current_user.username, request.remote_addr,
                "patient", None, "create",
            )
        flash("Patient '{}' created.".format(form.name.data), "success")
    else:
        for field, errors in form.errors.items():
            for err in errors:
                flash("{}: {}".format(field, err), "danger")
    return redirect(url_for("patients"))


@app.route("/patients/<int:patient_id>/archive", methods=["POST"])
@login_required
@role_required("admin", "doctor", "clinician")
def archive_patient_route(patient_id):
    """Archive (or restore) a patient record."""
    restore = request.form.get("restore") == "1"
    db.archive_patient(patient_id, archived=not restore)
    if _AUDIT_AVAILABLE:
        audit_log.log_data_access(
            current_user.username, request.remote_addr,
            "patient", patient_id,
            "restore" if restore else "archive",
        )
    flash("Patient {}.".format("restored" if restore else "archived"),
          "success")
    return redirect(url_for("patients"))


@app.route("/patients/<int:patient_id>")
@login_required
def patient_detail(patient_id):
    """Show a patient's profile and their sleep sessions."""
    patient = db.get_patient(patient_id)
    if patient is None:
        flash("Patient not found.", "warning")
        return redirect(url_for("patients"))
    if _AUDIT_AVAILABLE:
        audit_log.log_data_access(
            current_user.username, request.remote_addr,
            "patient", patient_id, "view",
        )
    sessions = db.list_sessions(patient_id=patient_id)
    return render_template("patient_detail.html",
                           patient=patient, sessions=sessions)


@app.route("/patients/<int:patient_id>/edit", methods=["POST"])
@login_required
@role_required("admin", "doctor", "clinician", "nurse")
def edit_patient(patient_id):
    """Update an existing patient record (full demographic set)."""
    form = NewPatientForm()
    if form.validate_on_submit():
        dob_str = form.dob.data.isoformat() if form.dob.data else None
        db.update_patient(
            patient_id=patient_id,
            name=sanitize_string(form.name.data, max_length=120),
            dob=dob_str,
            notes=sanitize_string(form.notes.data, max_length=2000) if form.notes.data else None,
            mrn=sanitize_string(form.mrn.data or "", max_length=64) or None,
            sex=sanitize_string(form.sex.data or "", max_length=20) or None,
            contact_phone=sanitize_string(form.contact_phone.data or "",
                                          max_length=40) or None,
            contact_email=sanitize_string(form.contact_email.data or "",
                                          max_length=120).lower() or None,
            allergies=sanitize_string(form.allergies.data or "",
                                      max_length=1000) or None,
            height_cm=form.height_cm.data,
            weight_kg=form.weight_kg.data,
        )
        if _AUDIT_AVAILABLE:
            audit_log.log_data_access(
                current_user.username, request.remote_addr,
                "patient", patient_id, "update",
            )
        flash("Patient updated.", "success")
    else:
        for field, errors in form.errors.items():
            for err in errors:
                flash("{}: {}".format(field, err), "danger")
    return redirect(url_for("patient_detail", patient_id=patient_id))


# ---------------------------------------------------------------------------
# Web routes — sessions
# ---------------------------------------------------------------------------

@app.route("/sessions/<int:session_id>")
@login_required
def session_detail(session_id):
    """Show telemetry, alerts, clinical notes and the report for a session."""
    sess = db.get_session(session_id)
    if sess is None:
        flash("Session not found.", "warning")
        return redirect(url_for("dashboard"))
    if _AUDIT_AVAILABLE:
        audit_log.log_data_access(
            current_user.username, request.remote_addr,
            "session", session_id, "view",
        )
    telemetry = db.get_telemetry(session_id, limit=200)
    report    = db.get_report(session_id)
    summary   = json.loads(report["summary_json"]) if report else None
    alerts    = db.list_alerts(session_id=session_id, limit=200)
    notes     = db.list_clinical_notes(session_id)
    note_form     = ClinicalNoteForm()
    discharge_form = DischargeForm()
    return render_template(
        "session_detail.html",
        sess=sess, telemetry=telemetry, report=report,
        summary=summary, alerts=alerts, notes=notes,
        note_form=note_form, discharge_form=discharge_form,
    )


# ---------------------------------------------------------------------------
# Clinical notes — attach observations to a session
# ---------------------------------------------------------------------------

@app.route("/sessions/<int:session_id>/notes/add", methods=["POST"])
@login_required
@role_required("admin", "doctor", "clinician", "nurse")
def add_session_note(session_id):
    """Append a clinical observation to a session."""
    if db.get_session(session_id) is None:
        flash("Session not found.", "warning")
        return redirect(url_for("dashboard"))
    form = ClinicalNoteForm()
    if form.validate_on_submit():
        body = sanitize_string(form.body.data, max_length=4000)
        db.add_clinical_note(session_id, current_user.id, body)
        if _AUDIT_AVAILABLE:
            audit_log.log_data_access(
                current_user.username, request.remote_addr,
                "clinical_note", session_id, "create",
            )
        flash("Clinical note saved.", "success")
    else:
        for field, errors in form.errors.items():
            for err in errors:
                flash("{}: {}".format(field, err), "danger")
    return redirect(url_for("session_detail", session_id=session_id))


@app.route("/sessions/<int:session_id>/notes/<int:note_id>/delete",
           methods=["POST"])
@login_required
@role_required("admin", "doctor")
def delete_session_note(session_id, note_id):
    """Remove a clinical note (admin/doctor only)."""
    if db.delete_clinical_note(note_id, session_id):
        if _AUDIT_AVAILABLE:
            audit_log.log_data_access(
                current_user.username, request.remote_addr,
                "clinical_note", note_id, "delete",
            )
        flash("Clinical note deleted.", "info")
    else:
        flash("Clinical note not found.", "warning")
    return redirect(url_for("session_detail", session_id=session_id))


# ---------------------------------------------------------------------------
# Session discharge
# ---------------------------------------------------------------------------

@app.route("/sessions/<int:session_id>/discharge", methods=["POST"])
@login_required
@role_required("admin", "doctor", "clinician")
def discharge_session_route(session_id):
    """Mark a session as clinically discharged."""
    sess = db.get_session(session_id)
    if sess is None:
        flash("Session not found.", "warning")
        return redirect(url_for("dashboard"))
    form = DischargeForm()
    notes = sanitize_string(form.discharge_notes.data or "",
                            max_length=2000) if form.discharge_notes.data else None
    db.discharge_session(session_id, current_user.id, discharge_notes=notes)
    if _AUDIT_AVAILABLE:
        audit_log.log_data_access(
            current_user.username, request.remote_addr,
            "session", session_id, "discharge",
        )
    flash("Session discharged.", "success")
    return redirect(url_for("session_detail", session_id=session_id))


@app.route("/sessions/<int:session_id>/report", methods=["POST"])
@login_required
@role_required("admin", "doctor", "clinician")
def generate_report(session_id):
    """
    Generate (or regenerate) the PDF report for a session.

    POST to this URL to trigger generation.  After generation the user is
    redirected back to the session detail page.
    """
    sess = db.get_session(session_id)
    if sess is None:
        flash("Session not found.", "warning")
        return redirect(url_for("dashboard"))

    try:
        summary      = rpt.compute_summary(session_id)
        summary_json = json.dumps(summary, indent=2)
        sig          = rpt.sign_summary(summary_json)
        pdf_path     = rpt.generate_pdf(sess, summary)
        db.save_report(session_id, pdf_path, summary_json, sig)
        if _AUDIT_AVAILABLE:
            audit_log.log_report_generated(
                current_user.username, request.remote_addr, session_id,
            )
        flash("Report generated successfully.", "success")
    except Exception as exc:
        print("[SOMNI][ERROR] Report generation failed for session {}: {}".format(
            session_id, exc))
        flash("Report generation failed. Please try again.", "danger")

    return redirect(url_for("session_detail", session_id=session_id))


@app.route("/sessions/<int:session_id>/report/download")
@login_required
@role_required("admin", "doctor", "clinician")
def download_report(session_id):
    """Serve the generated PDF report as a file download."""
    report = db.get_report(session_id)
    if report is None or not report["pdf_path"]:
        flash("No report found for this session.", "warning")
        return redirect(url_for("session_detail", session_id=session_id))

    pdf_path = report["pdf_path"]
    # Validate path is within REPORT_DIR to prevent path traversal attacks.
    # An attacker who could write to the reports table might otherwise inject
    # an arbitrary path and read sensitive files from the Pi 5 filesystem.
    report_dir_real = os.path.realpath(cfg.REPORT_DIR)
    pdf_path_real   = os.path.realpath(pdf_path)
    if not pdf_path_real.startswith(report_dir_real + os.sep):
        if _AUDIT_AVAILABLE:
            audit_log.log_security_event(
                "PATH_TRAVERSAL_ATTEMPT",
                request.remote_addr,
                {"session_id": session_id, "path": pdf_path},
            )
        flash("Invalid report path.", "danger")
        return redirect(url_for("session_detail", session_id=session_id))
    if not os.path.isfile(pdf_path_real):
        flash("Report file missing on disk.", "danger")
        return redirect(url_for("session_detail", session_id=session_id))

    if _AUDIT_AVAILABLE:
        audit_log.log_report_downloaded(
            current_user.username, request.remote_addr, session_id,
        )

    return send_file(
        pdf_path_real,
        as_attachment=True,
        download_name="somni_report_session_{}.pdf".format(session_id),
        mimetype="application/pdf",
    )


# ---------------------------------------------------------------------------
# Web routes — user management (admin only)
# ---------------------------------------------------------------------------

@app.route("/admin/users")
@login_required
@admin_required
def manage_users():
    """List all users and show the create‑user form."""
    all_users = db.list_users()
    form = NewUserForm()
    return render_template("manage_users.html", users=all_users, form=form)


@app.route("/admin/users/new", methods=["POST"])
@login_required
@admin_required
def create_user():
    """Create a new gateway user (admin only)."""
    form = NewUserForm()
    if form.validate_on_submit():
        # Validate password complexity
        pw_valid, pw_errors = validate_password_complexity(form.password.data)
        if not pw_valid:
            for err in pw_errors:
                flash(err, "danger")
            return redirect(url_for("manage_users"))

        pwd_hash = bcrypt.hashpw(
            form.password.data.encode("utf-8"),
            bcrypt.gensalt(rounds=12),
        ).decode("utf-8")
        try:
            db.create_user(
                username=sanitize_string(form.username.data, max_length=64),
                email=sanitize_string(form.email.data, max_length=120).lower(),
                password_hash=pwd_hash,
                role=form.role.data,
            )
            if _AUDIT_AVAILABLE:
                audit_log.log_user_created(
                    current_user.username, request.remote_addr,
                    form.username.data, form.role.data,
                )
            flash("User '{}' created.".format(form.username.data), "success")
        except Exception as exc:
            print("[SOMNI][ERROR] User creation failed: {}".format(exc))
            flash("Could not create user. Please try again.", "danger")
    else:
        for field, errors in form.errors.items():
            for err in errors:
                flash("{}: {}".format(field, err), "danger")
    return redirect(url_for("manage_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_user(user_id):
    """Delete a user (cannot delete yourself)."""
    if user_id == current_user.id:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("manage_users"))
    if _AUDIT_AVAILABLE:
        audit_log.log_user_deleted(
            current_user.username, request.remote_addr, user_id,
        )
    db.delete_user(user_id)
    flash("User deleted.", "info")
    return redirect(url_for("manage_users"))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@admin_required
def admin_reset_password(user_id):
    """Force a user to set a new password on next login.

    Generates a strong temporary password, marks must_change_password=1,
    and shows the temp password to the admin (one time only).
    """
    target = db.get_user_by_id(user_id)
    if target is None:
        flash("User not found.", "danger")
        return redirect(url_for("manage_users"))

    import secrets, string
    alphabet = string.ascii_letters + string.digits + "!@#$%&*-_=+"
    while True:
        # 18 chars, all four classes, no 4-run repeats — passes
        # validate_password_complexity() most of the time.
        temp = "".join(secrets.choice(alphabet) for _ in range(18))
        ok, _ = validate_password_complexity(temp)
        if ok:
            break

    new_hash = bcrypt.hashpw(
        temp.encode("utf-8"), bcrypt.gensalt(rounds=12),
    ).decode("utf-8")
    db.update_user_password(user_id, new_hash, must_change=1)
    if _AUDIT_AVAILABLE:
        audit_log.log_security_event(
            "ADMIN_PASSWORD_RESET", request.remote_addr or "unknown",
            {"admin": current_user.username, "target": target["username"]},
        )
    flash("Temporary password for {}: {} — share securely. The user "
          "must change it on next login.".format(target["username"], temp),
          "warning")
    return redirect(url_for("manage_users"))


@app.route("/admin/users/<int:user_id>/toggle-active", methods=["POST"])
@login_required
@admin_required
def admin_toggle_active(user_id):
    """Activate or deactivate a user account (cannot deactivate yourself)."""
    if user_id == current_user.id:
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("manage_users"))
    target = db.get_user_by_id(user_id)
    if target is None:
        flash("User not found.", "danger")
        return redirect(url_for("manage_users"))
    new_state = 0 if target["is_active"] else 1
    db.set_user_active(user_id, new_state)
    if _AUDIT_AVAILABLE:
        audit_log.log_security_event(
            "USER_ACTIVATED" if new_state else "USER_DEACTIVATED",
            request.remote_addr or "unknown",
            {"admin": current_user.username, "target": target["username"]},
        )
    flash("User {} {}.".format(target["username"],
                                "activated" if new_state else "deactivated"),
          "success")
    return redirect(url_for("manage_users"))


# ---------------------------------------------------------------------------
# Audit log viewer (admin only)
# ---------------------------------------------------------------------------

@app.route("/admin/audit")
@login_required
@admin_required
def audit_log_view():
    """Filterable audit-log viewer."""
    event_type = sanitize_string(request.args.get("event_type", ""),
                                 max_length=64) or None
    username   = sanitize_string(request.args.get("username", ""),
                                 max_length=64) or None
    limit      = sanitize_int(request.args.get("limit", "200"),
                              min_val=10, max_val=2000) or 200
    rows = db.get_audit_logs(limit=limit, event_type=event_type,
                             username=username)
    types = db.list_distinct_audit_event_types()
    return render_template("audit_log.html",
                           rows=rows, types=types,
                           filter_event_type=event_type,
                           filter_username=username,
                           filter_limit=limit)


@app.route("/admin/audit.csv")
@login_required
@admin_required
def audit_log_csv():
    """Export the filtered audit log as CSV."""
    event_type = sanitize_string(request.args.get("event_type", ""),
                                 max_length=64) or None
    username   = sanitize_string(request.args.get("username", ""),
                                 max_length=64) or None
    limit      = sanitize_int(request.args.get("limit", "5000"),
                              min_val=10, max_val=10000) or 5000
    rows = db.get_audit_logs(limit=limit, event_type=event_type,
                             username=username)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "event_type", "username", "ip_address", "details"])
    for r in rows:
        w.writerow([r["timestamp"], r["event_type"], r["username"] or "",
                    r["ip_address"] or "", r["details"] or ""])

    if _AUDIT_AVAILABLE:
        audit_log.log_security_event(
            "AUDIT_LOG_EXPORTED", request.remote_addr or "unknown",
            {"by": current_user.username, "rows": len(rows)},
        )

    fname = "somniguard_audit_{}.csv".format(
        datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
    )
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=" + fname},
    )


# ---------------------------------------------------------------------------
# Alerts page (clinician + admin) and acknowledge endpoint.
# ---------------------------------------------------------------------------

@app.route("/alerts")
@login_required
def alerts_view():
    """Show all alerts (most recent first), default filter = unacknowledged."""
    only_unack = request.args.get("filter", "unack") == "unack"
    rows = db.list_alerts(only_unacknowledged=only_unack, limit=500)
    return render_template("alerts.html",
                           alerts=rows, only_unack=only_unack)


@app.route("/alerts/<int:alert_id>/ack", methods=["POST"])
@login_required
@role_required("admin", "doctor", "clinician", "nurse")
def acknowledge_alert_route(alert_id):
    """Acknowledge an alert."""
    if db.acknowledge_alert(alert_id, current_user.id):
        if _AUDIT_AVAILABLE:
            audit_log.log_security_event(
                "ALERT_ACKNOWLEDGED", request.remote_addr or "unknown",
                {"by": current_user.username, "alert_id": alert_id},
            )
        flash("Alert acknowledged.", "success")
    else:
        flash("Alert was already acknowledged or not found.", "info")
    return redirect(request.referrer or url_for("alerts_view"))


# ---------------------------------------------------------------------------
# Device fleet view (admin)
# ---------------------------------------------------------------------------

@app.route("/devices")
@login_required
def devices_view():
    """Show every Pico the gateway has heard from, with online/offline state."""
    rows = db.list_devices()
    # Devices seen within 90s are considered ONLINE.
    online_window_s = 90
    now_epoch = time.time()
    enriched = []
    for r in rows:
        last_seen = r["last_seen_at"]
        is_online = False
        if last_seen:
            try:
                # SQLite CURRENT_TIMESTAMP returns naive UTC.
                ts_dt = datetime.fromisoformat(str(last_seen)).replace(
                    tzinfo=timezone.utc)
                age = now_epoch - ts_dt.timestamp()
                is_online = age <= online_window_s
            except Exception:
                is_online = False
        enriched.append({
            "device_id":         r["device_id"],
            "last_seen_at":      r["last_seen_at"],
            "online":            is_online,
            "last_battery_pct":  r["last_battery_pct"],
            "last_rssi_dbm":     r["last_rssi_dbm"],
            "last_ip":           r["last_ip"],
            "firmware_version":  r["firmware_version"],
            "last_session_id":   r["last_session_id"],
            "last_patient_name": r["last_patient_name"],
            "patient_id":        r["patient_id"],
        })
    return render_template("devices.html",
                           devices=enriched,
                           online_window_s=online_window_s)


# ---------------------------------------------------------------------------
# Live monitor: real-time view of active sessions
# ---------------------------------------------------------------------------

@app.route("/live")
@login_required
def live_monitor():
    """Render the real-time multi-patient monitor page."""
    sessions = db.get_active_sessions()
    return render_template("live_monitor.html", sessions=sessions)


@app.route("/live/data")
@login_required
def live_monitor_data():
    """JSON snapshot used by the live monitor's polling loop (CSP-friendly)."""
    sessions = db.get_active_sessions()
    payload = []
    for s in sessions:
        latest = db.get_latest_telemetry(s["id"])
        unack = db.list_alerts(session_id=s["id"], only_unacknowledged=True,
                               limit=10)
        recent = db.get_telemetry(s["id"], limit=60)
        spo2_series = [r["spo2"] for r in recent
                       if r["spo2"] is not None and r["valid_spo2"]]
        hr_series   = [r["hr"]   for r in recent
                       if r["hr"]   is not None and r["valid_spo2"]]
        payload.append({
            "session_id":   s["id"],
            "patient_id":   s["patient_id"],
            "patient_name": s["patient_name"],
            "patient_mrn":  s["patient_mrn"],
            "device_id":    s["device_id"],
            "started_at":   s["started_at"],
            "latest": {
                "spo2":    latest["spo2"]            if latest else None,
                "hr":      latest["hr"]              if latest else None,
                "valid":   bool(latest["valid_spo2"]) if latest else False,
                "received_at": latest["received_at"] if latest else None,
            } if latest else None,
            "spo2_recent": spo2_series[-30:],
            "hr_recent":   hr_series[-30:],
            "unack_alerts": [
                {
                    "id":         a["id"],
                    "alert_type": a["alert_type"],
                    "severity":   a["severity"],
                    "message":    a["message"],
                    "triggered_at": a["triggered_at"],
                }
                for a in unack
            ],
        })
    return jsonify({"sessions": payload, "now": int(time.time())})


# ---------------------------------------------------------------------------
# REST API — Pico telemetry ingestion (CSRF‑exempt; uses HMAC auth)
# ---------------------------------------------------------------------------

@csrf.exempt
@app.route("/api/session/start", methods=["POST"])
def api_session_start():
    """
    Start a new sleep session.

    Request (JSON):
        {
          "patient_id": int,
          "device_id":  str,
          "nonce":      int,
          "timestamp":  int,
          "hmac":       str
        }

    Response (JSON):
        {"session_id": int}  on success
        {"error": str}       on failure
    """
    if request.content_length is not None and request.content_length > _API_MAX_BODY_BYTES:
        return jsonify({"error": "body too large"}), 413
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON"}), 400

    if not _verify_hmac(body):
        if _AUDIT_AVAILABLE:
            audit_log.log_api_access(
                request.remote_addr, "/api/session/start",
                "POST", 403, body.get("device_id"),
            )
        return jsonify({"error": "HMAC verification failed"}), 403

    # Timestamp freshness — same 5-minute window used by /api/ingest
    pkt_timestamp = body.get("timestamp")
    if pkt_timestamp is not None:
        age = abs(int(time.time()) - pkt_timestamp)
        if age > _TIMESTAMP_WINDOW_S:
            if _AUDIT_AVAILABLE:
                audit_log.log_security_event(
                    "REPLAY_DETECTED", request.remote_addr,
                    {"endpoint": "/api/session/start", "age_s": age},
                )
            return jsonify({"error": "stale timestamp"}), 403

    # Nonce must be a positive integer (Pico resets to 0 then increments to 1)
    nonce = body.get("nonce")
    if nonce is None or not isinstance(nonce, int) or nonce <= 0:
        return jsonify({"error": "invalid nonce"}), 400

    patient_id = body.get("patient_id")
    device_id  = sanitize_string(
        body.get("device_id", "pico-01"), max_length=64
    )

    if not patient_id:
        return jsonify({"error": "patient_id required"}), 400

    # Verify patient exists
    if db.get_patient(patient_id) is None:
        return jsonify({"error": "patient not found"}), 404

    session_id = db.create_session(patient_id, device_id)

    # Seed the nonce high-water mark with the received nonce so the first
    # ingest packet must use a strictly higher value.
    _nonce_hwm_set(session_id, nonce)

    # Register / refresh the device in the fleet table on session start.
    try:
        db.upsert_device_seen(
            device_id,
            session_id=session_id,
            ip_address=request.remote_addr,
            firmware_version=sanitize_string(
                body.get("fw_version", "") or "", max_length=32) or None,
        )
    except Exception as exc:
        print("[SOMNI][DEVICE][WARN] fleet upsert (start): {}".format(exc))

    if _AUDIT_AVAILABLE:
        audit_log.log_api_access(
            request.remote_addr, "/api/session/start",
            "POST", 201, device_id,
        )

    return jsonify({"session_id": session_id}), 201


# Apply rate limit to API endpoints
if _LIMITER_AVAILABLE:
    api_session_start = limiter.limit("20 per second")(api_session_start)


@csrf.exempt
@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """
    Accept a telemetry reading from the Pico.

    Order of checks (cheap → expensive — minimises DoS amplification):
      1. Body size cap        (free; rejects bandwidth flood)
      2. JSON parse           (free)
      3. Schema/bounds check  (cheap; rejects fuzz before HMAC)
      4. HMAC verification    (~50 µs SHA-256)
      5. Anti-replay (nonce)  (cheap)
      6. Timestamp freshness  (free)
      7. Database insert      (the expensive part)

    Validates HMAC, nonce (anti-replay), and timestamp freshness.

    Request (JSON):
        {
          "session_id":   int,
          "timestamp_ms": int,
          "nonce":        int,
          "timestamp":    int,
          "spo2":  {...},
          "accel": {...},
          "gsr":   {...},
          "hmac":  str
        }

    Response (JSON):
        {"ok": true}  on success
        {"error": str} on failure
    """
    # 1. Hard body size cap — rejects flood/DoS before parsing.
    if request.content_length is not None and request.content_length > _API_MAX_BODY_BYTES:
        return jsonify({"error": "body too large"}), 413

    # 2. JSON parse
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON"}), 400

    # 3. Schema + bounds check (PT2/PT5/PT9)
    ok, err = _validate_telemetry_payload(body)
    if not ok:
        if _AUDIT_AVAILABLE:
            audit_log.log_security_event(
                "INGEST_REJECTED", request.remote_addr,
                {"reason": err},
            )
        return jsonify({"error": err}), 400

    # 4. HMAC verification (only after body has been bounds-checked)
    if not _verify_hmac(body):
        return jsonify({"error": "HMAC verification failed"}), 403

    session_id = body["session_id"]
    nonce      = body["nonce"]

    # 5. Anti-replay: nonce must be strictly increasing within a session.
    if session_id in _nonce_hwm:
        if nonce <= _nonce_hwm[session_id]:
            if _AUDIT_AVAILABLE:
                audit_log.log_security_event(
                    "REPLAY_DETECTED", request.remote_addr,
                    {"session_id": session_id, "nonce": nonce},
                )
            return jsonify({"error": "replay detected: stale nonce"}), 403
    _nonce_hwm_set(session_id, nonce)

    # 6. Timestamp freshness
    age = abs(int(time.time()) - body["timestamp"])
    if age > _TIMESTAMP_WINDOW_S:
        return jsonify({"error": "stale timestamp"}), 403

    # 7. Database insert
    try:
        db.insert_telemetry(session_id, body)
    except Exception as exc:
        print("[SOMNI][ERROR] Telemetry insert failed for session {}: {}".format(
            session_id, exc))
        return jsonify({"error": "internal error"}), 500

    # 8. Threshold-based alert evaluation (best-effort; never fails the
    #    ingest call — telemetry is the primary clinical record).
    try:
        _evaluate_alerts(session_id, body)
    except Exception as exc:
        print("[SOMNI][ALERT][WARN] alert eval: {}".format(exc))

    # 9. Device fleet tracking — record last-seen / battery / rssi so the
    #    /devices page can show fleet health.
    try:
        device_id = sanitize_string(body.get("device_id", "") or "",
                                    max_length=64) or None
        battery_pct = body.get("battery_pct")
        rssi_dbm    = body.get("rssi_dbm")
        if device_id:
            db.upsert_device_seen(
                device_id,
                session_id=session_id,
                battery_pct=battery_pct if isinstance(battery_pct, (int, float))
                                       and 0 <= battery_pct <= 100 else None,
                rssi_dbm=int(rssi_dbm) if isinstance(rssi_dbm, (int, float))
                                      and -200 <= rssi_dbm <= 0 else None,
                ip_address=request.remote_addr,
                firmware_version=sanitize_string(
                    body.get("fw_version", "") or "", max_length=32) or None,
            )
    except Exception as exc:
        print("[SOMNI][DEVICE][WARN] fleet upsert: {}".format(exc))

    return jsonify({"ok": True}), 200


if _LIMITER_AVAILABLE:
    api_ingest = limiter.limit("20 per second")(api_ingest)


@csrf.exempt
@app.route("/api/session/end", methods=["POST"])
def api_session_end():
    """
    Mark a session as ended.

    Request (JSON):
        {"session_id": int, "nonce": int, "timestamp": int, "hmac": str}

    Response (JSON):
        {"ok": true}
    """
    if request.content_length is not None and request.content_length > _API_MAX_BODY_BYTES:
        return jsonify({"error": "body too large"}), 413
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON"}), 400

    if not _verify_hmac(body):
        return jsonify({"error": "HMAC verification failed"}), 403

    # Timestamp freshness check
    pkt_timestamp = body.get("timestamp")
    if pkt_timestamp is not None:
        age = abs(int(time.time()) - pkt_timestamp)
        if age > _TIMESTAMP_WINDOW_S:
            if _AUDIT_AVAILABLE:
                audit_log.log_security_event(
                    "REPLAY_DETECTED", request.remote_addr,
                    {"endpoint": "/api/session/end", "age_s": age},
                )
            return jsonify({"error": "stale timestamp"}), 403

    session_id = body.get("session_id")
    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    # Anti-replay: nonce must be strictly greater than the high-water mark
    nonce = body.get("nonce")
    if nonce is not None and session_id in _nonce_hwm:
        if nonce <= _nonce_hwm[session_id]:
            if _AUDIT_AVAILABLE:
                audit_log.log_security_event(
                    "REPLAY_DETECTED", request.remote_addr,
                    {"endpoint": "/api/session/end",
                     "session_id": session_id, "nonce": nonce},
                )
            return jsonify({"error": "replay detected: stale nonce"}), 403

    db.end_session(session_id)

    # Clean up nonce tracking for this session
    _nonce_hwm.pop(session_id, None)

    if _AUDIT_AVAILABLE:
        audit_log.log_api_access(
            request.remote_addr, "/api/session/end",
            "POST", 200, body.get("device_id"),
        )

    return jsonify({"ok": True}), 200


if _LIMITER_AVAILABLE:
    api_session_end = limiter.limit("20 per second")(api_session_end)


# ---------------------------------------------------------------------------
# REST API — clock sync (unauthenticated; returns only Unix time)
# ---------------------------------------------------------------------------

@csrf.exempt
@app.route("/api/time", methods=["GET"])
def api_time():
    """Return current Unix timestamp so the Pico can sync its clock.

    The Pico's MicroPython time.time() counts from 2000-01-01 (not Unix
    epoch). This endpoint lets the Pico correct its epoch offset without
    needing public internet access for NTP.

    No authentication required — the response contains only a timestamp.
    Access is still gated by the existing network-policy middleware.
    """
    return jsonify({"t": int(time.time())}), 200


# ---------------------------------------------------------------------------
# REST API — Tailscale status (authenticated, admin only)
# ---------------------------------------------------------------------------

@app.route("/api/tailscale/status")
@login_required
def api_tailscale_status():
    """
    Return the current Tailscale daemon status for this gateway node.

    Only admin users may call this endpoint.  Returns the local Tailscale IP,
    MagicDNS hostname, daemon state, and the list of known peers.

    Response (JSON):
        {
          "running":    bool,
          "local_ip":   str | null,
          "hostname":   str | null,
          "peers":      [{HostName, DNSName, TailscaleIPs, Online, OS}, ...],
          "tailscale_only_mode": bool
        }
    """
    if current_user.role != "admin":
        return jsonify({"error": "Admin access required."}), 403

    return jsonify({
        "running":             ts.tailscale_running(),
        "local_ip":            ts.get_local_tailscale_ip(),
        "hostname":            ts.get_tailscale_hostname(),
        "peers":               ts.list_tailscale_peers(),
        "tailscale_only_mode": cfg.TAILSCALE_ONLY,
    })


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _verify_hmac(body):
    """
    Verify the HMAC‑SHA256 tag on an incoming API request body.

    The client sends the JSON body with an "hmac" field whose value is
    HMAC‑SHA256(shared_key, canonical_payload) where canonical_payload is
    the JSON of the body **without** the "hmac" key, sorted by key.

    Args:
        body (dict): Parsed JSON body including the "hmac" field.

    Returns:
        bool: True if the HMAC is valid, False otherwise.
    """
    received_mac = body.get("hmac", "")
    if not received_mac:
        return False

    # Reconstruct the payload without the hmac field.
    # IMPORTANT: use separators=(',', ':') to match the Pico's _json_sorted()
    # which produces compact JSON with no spaces.  json.dumps default separators
    # include spaces (', ' and ': ') which would produce a different string and
    # cause HMAC verification to always fail.
    payload = {k: v for k, v in body.items() if k != "hmac"}
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode("utf-8")

    key = cfg.PICO_HMAC_KEY.encode("utf-8")
    expected_mac = _hmac.new(key, payload_bytes, hashlib.sha256).hexdigest()

    # Constant‑time comparison to prevent timing attacks
    return _hmac.compare_digest(expected_mac, received_mac)


def _is_safe_url(target):
    """Return True if target is a safe redirect URL (same host)."""
    from urllib.parse import urlparse, urljoin
    ref_url  = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target or ""))
    return test_url.scheme in ("http", "https") and ref_url.netloc == test_url.netloc
