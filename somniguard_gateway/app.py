"""
app.py — SOMNI‑Guard gateway Flask application.

Provides:
- Web dashboard: login, patients, sessions, reports, user management.
- REST API: /api/session/start, /api/ingest, /api/session/end
  (used by the Pico transport layer, authenticated via HMAC‑SHA256).

All database operations use parameterised queries (see database.py).
Passwords are hashed with bcrypt.
CSRF protection is provided by Flask‑WTF on all state‑changing web forms.

Educational prototype — not a clinically approved device.
"""

import hashlib
import hmac as _hmac
import json
import os
import time
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

# ---------------------------------------------------------------------------
# Flask app + extensions
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"]         = cfg.SECRET_KEY
app.config["WTF_CSRF_SECRET_KEY"] = cfg.WTF_CSRF_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024   # 256 KB max request body

csrf    = CSRFProtect(app)
login_mgr = LoginManager(app)
login_mgr.login_view = "login"
login_mgr.login_message_category = "warning"


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
    to prevent username enumeration.
    """
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user_row = db.get_user_by_username(form.username.data.strip())
        if user_row and bcrypt.checkpw(
            form.password.data.encode("utf-8"),
            user_row["password_hash"].encode("utf-8"),
        ):
            login_user(_UserProxy(user_row))
            flash("Welcome, {}!".format(user_row["username"]), "success")
            next_page = request.args.get("next")
            if next_page and _is_safe_url(next_page):
                return redirect(next_page)
            return redirect(url_for("dashboard"))
        else:
            # Constant‑time delay to mitigate brute‑force timing attacks
            time.sleep(0.5)
            flash("Invalid username or password.", "danger")

    return render_template("login.html", form=form)


@app.route("/logout")
@login_required
def logout():
    """Log out the current user and redirect to login."""
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Web routes — dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    """Show recent sessions across all patients."""
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
            name=form.name.data.strip(),
            dob=dob_str,
            notes=form.notes.data.strip() if form.notes.data else None,
            created_by=current_user.id,
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
    if not os.path.isfile(pdf_path):
        flash("Report file missing on disk.", "danger")
        return redirect(url_for("session_detail", session_id=session_id))

    return send_file(
        pdf_path,
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
        pwd_hash = bcrypt.hashpw(
            form.password.data.encode("utf-8"),
            bcrypt.gensalt(rounds=12),
        ).decode("utf-8")
        try:
            db.create_user(
                username=form.username.data.strip(),
                email=form.email.data.strip().lower(),
                password_hash=pwd_hash,
                role=form.role.data,
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
          "hmac":       str   # hex HMAC-SHA256 of the JSON body (excl. hmac field)
        }

    Response (JSON):
        {"session_id": int}  on success
        {"error": str}       on failure
    """
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "invalid JSON"}), 400

    if not _verify_hmac(body):
        return jsonify({"error": "HMAC verification failed"}), 403

    patient_id = body.get("patient_id")
    device_id  = body.get("device_id", "pico-01")

    if not patient_id:
        return jsonify({"error": "patient_id required"}), 400

    # Verify patient exists
    if db.get_patient(patient_id) is None:
        return jsonify({"error": "patient not found"}), 404

    session_id = db.create_session(patient_id, device_id)
    return jsonify({"session_id": session_id}), 201


@csrf.exempt
@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """
    Accept a telemetry reading from the Pico.

    Request (JSON):
        {
          "session_id":   int,
          "timestamp_ms": int,
          "spo2":  {"spo2": float|null, "hr": float|null,
                    "ir_raw": int|null, "red_raw": int|null, "valid": bool},
          "accel": {"x": float|null, "y": float|null,
                    "z": float|null, "valid": bool},
          "gsr":   {"raw": int, "voltage": float,
                    "conductance_us": float, "valid": bool},
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

    try:
        db.insert_telemetry(session_id, body)
        return jsonify({"ok": True}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@csrf.exempt
@app.route("/api/session/end", methods=["POST"])
def api_session_end():
    """
    Mark a session as ended.

    Request (JSON):
        {"session_id": int, "hmac": str}

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
    return jsonify({"ok": True}), 200


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

    # Reconstruct the payload without the hmac field
    payload = {k: v for k, v in body.items() if k != "hmac"}
    payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")

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
