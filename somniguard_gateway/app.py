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

import hashlib
import hmac as _hmac
import json
import os
import time
from datetime import timedelta
from functools import wraps

import bcrypt
from flask import (
    Flask, flash, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)
from flask_login import (
    LoginManager, UserMixin, current_user, login_required,
    login_user, logout_user,
)
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect
from wtforms import DateField, PasswordField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Optional

import config as cfg
import database as db
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


# ---------------------------------------------------------------------------
# Forms (WTForms + CSRF)
# ---------------------------------------------------------------------------

class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(1, 64)])
    password = PasswordField("Password", validators=[DataRequired(), Length(1, 128)])


class NewUserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(3, 64)])
    email    = StringField("Email",    validators=[DataRequired(), Email(), Length(5, 120)])
    password = PasswordField("Password", validators=[DataRequired(), Length(8, 128)])
    role     = SelectField("Role", choices=[("clinician", "Clinician"), ("admin", "Admin")])


class NewPatientForm(FlaskForm):
    name  = StringField("Patient Name", validators=[DataRequired(), Length(1, 120)])
    dob   = DateField("Date of Birth", format="%Y-%m-%d", validators=[Optional()])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=2000)])


# ---------------------------------------------------------------------------
# Web routes — authentication
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Redirect root to dashboard or login."""
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    Display the login form and authenticate the user.

    Uses bcrypt to verify the password against the stored hash.
    A generic error message is shown regardless of which credential is wrong
    to prevent username enumeration.  Account lockout is enforced after 10
    consecutive failed attempts from the same IP.
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
        if user_row and bcrypt.checkpw(
            form.password.data.encode("utf-8"),
            user_row["password_hash"].encode("utf-8"),
        ):
            login_user(_UserProxy(user_row))
            login_tracker.record_successful_login(client_ip)
            if _AUDIT_AVAILABLE:
                audit_log.log_login_attempt(username, client_ip, True)
            flash("Welcome, {}!".format(user_row["username"]), "success")
            next_page = request.args.get("next")
            if next_page and _is_safe_url(next_page):
                return redirect(next_page)
            return redirect(url_for("dashboard"))
        else:
            # Constant‑time delay to mitigate brute‑force timing attacks
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


@app.route("/logout")
@login_required
def logout():
    """Log out the current user and redirect to login."""
    username = current_user.username
    client_ip = request.remote_addr or "unknown"
    logout_user()
    if _AUDIT_AVAILABLE:
        audit_log.log_logout(username, client_ip)
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


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
    return render_template("dashboard.html",
                           sessions=sessions, patients=patients)


# ---------------------------------------------------------------------------
# Web routes — patients
# ---------------------------------------------------------------------------

@app.route("/patients")
@login_required
def patients():
    """List all patients."""
    if _AUDIT_AVAILABLE:
        audit_log.log_data_access(
            current_user.username, request.remote_addr,
            "patients", None, "list",
        )
    all_patients = db.list_patients()
    form = NewPatientForm()
    return render_template("patients.html", patients=all_patients, form=form)


@app.route("/patients/new", methods=["POST"])
@login_required
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


# ---------------------------------------------------------------------------
# Web routes — sessions
# ---------------------------------------------------------------------------

@app.route("/sessions/<int:session_id>")
@login_required
def session_detail(session_id):
    """Show telemetry and the existing report (if any) for a session."""
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
    return render_template("session_detail.html",
                           sess=sess, telemetry=telemetry,
                           report=report, summary=summary)


@app.route("/sessions/<int:session_id>/report", methods=["POST"])
@login_required
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
        flash("Report generation failed: {}".format(exc), "danger")

    return redirect(url_for("session_detail", session_id=session_id))


@app.route("/sessions/<int:session_id>/report/download")
@login_required
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
            flash("Could not create user: {}".format(exc), "danger")
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

    patient_id = body.get("patient_id")
    device_id  = body.get("device_id", "pico-01")

    if not patient_id:
        return jsonify({"error": "patient_id required"}), 400

    # Verify patient exists
    if db.get_patient(patient_id) is None:
        return jsonify({"error": "patient not found"}), 404

    session_id = db.create_session(patient_id, device_id)

    # Initialize nonce high-water mark for this session
    _nonce_hwm_set(session_id, 0)

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
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON"}), 400

    if not _verify_hmac(body):
        return jsonify({"error": "HMAC verification failed"}), 403

    session_id = body.get("session_id")
    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    # Anti-replay: check nonce is strictly increasing
    nonce = body.get("nonce")
    if nonce is not None and session_id in _nonce_hwm:
        if nonce <= _nonce_hwm[session_id]:
            if _AUDIT_AVAILABLE:
                audit_log.log_security_event(
                    "REPLAY_DETECTED", request.remote_addr,
                    {"session_id": session_id, "nonce": nonce},
                )
            return jsonify({"error": "replay detected: stale nonce"}), 403
        _nonce_hwm_set(session_id, nonce)
    elif nonce is not None:
        _nonce_hwm_set(session_id, nonce)

    # Timestamp freshness check
    pkt_timestamp = body.get("timestamp")
    if pkt_timestamp is not None:
        now = int(time.time())
        age = abs(now - pkt_timestamp)
        if age > _TIMESTAMP_WINDOW_S:
            return jsonify({"error": "stale timestamp"}), 403

    try:
        db.insert_telemetry(session_id, body)
        return jsonify({"ok": True}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON"}), 400

    if not _verify_hmac(body):
        return jsonify({"error": "HMAC verification failed"}), 403

    session_id = body.get("session_id")
    if not session_id:
        return jsonify({"error": "session_id required"}), 400

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
