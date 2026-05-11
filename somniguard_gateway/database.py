"""
database.py — SOMNI‑Guard gateway SQLite schema and helper functions.

Uses the standard library ``sqlite3`` module with WAL journal mode for
better concurrency.  All queries use parameterised statements to prevent
SQL injection.

Schema
------
users       — gateway web‑app accounts (clinicians / admin).
patients    — one record per monitored patient.
sessions    — one record per sleep session (Pico powered on → off).
telemetry   — raw sensor readings streamed from the Pico.
reports     — generated sleep reports (metadata + PDF path).
audit_log   — structured audit trail for all security-relevant events.

Educational prototype — not a clinically approved device.
"""

import sqlite3
import os
import threading

import config as cfg


# ---------------------------------------------------------------------------
# Connection pool — thread-safe SQLite connection reuse
# ---------------------------------------------------------------------------

_local = threading.local()

# Query timeout in seconds — prevents long-running queries from blocking
_QUERY_TIMEOUT_S = 30


# ---------------------------------------------------------------------------
# DDL — table creation statements
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS users (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    username                 TEXT    UNIQUE NOT NULL,
    email                    TEXT    UNIQUE NOT NULL,
    password_hash            TEXT    NOT NULL,
    role                     TEXT    NOT NULL DEFAULT 'clinician',
    created_at               DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_login_at            DATETIME,
    last_login_ip            TEXT,
    password_changed_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    must_change_password     INTEGER NOT NULL DEFAULT 0,
    is_active                INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS patients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mrn           TEXT,
    name          TEXT NOT NULL,
    dob           DATE,
    sex           TEXT,
    contact_phone TEXT,
    contact_email TEXT,
    allergies     TEXT,
    height_cm     REAL,
    weight_kg     REAL,
    notes         TEXT,
    archived      INTEGER NOT NULL DEFAULT 0,
    archived_at   DATETIME,
    created_by    INTEGER REFERENCES users(id),
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      INTEGER NOT NULL REFERENCES patients(id),
    device_id       TEXT NOT NULL DEFAULT 'pico-01',
    started_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    ended_at        DATETIME,
    discharged_at   DATETIME,
    discharged_by   INTEGER REFERENCES users(id),
    discharge_notes TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS telemetry (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES sessions(id),
    timestamp_ms        INTEGER NOT NULL,
    spo2                REAL,
    hr                  REAL,
    ir_raw              INTEGER,
    red_raw             INTEGER,
    accel_x             REAL,
    accel_y             REAL,
    accel_z             REAL,
    gsr_raw             INTEGER,
    gsr_voltage         REAL,
    gsr_conductance_us  REAL,
    valid_spo2          INTEGER DEFAULT 0,
    valid_accel         INTEGER DEFAULT 0,
    valid_gsr           INTEGER DEFAULT 0,
    received_at         DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id),
    generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    pdf_path     TEXT,
    summary_json TEXT,
    hmac_sig     TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    username   TEXT,
    ip_address TEXT,
    details    TEXT
);

CREATE TABLE IF NOT EXISTS clinical_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    author_id   INTEGER REFERENCES users(id),
    body        TEXT NOT NULL,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    triggered_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    alert_type      TEXT NOT NULL,
    severity        TEXT NOT NULL DEFAULT 'warning',
    metric          TEXT,
    measured_value  REAL,
    threshold_value REAL,
    message         TEXT,
    acknowledged_at DATETIME,
    acknowledged_by INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS devices (
    device_id        TEXT PRIMARY KEY,
    last_seen_at     DATETIME,
    last_session_id  INTEGER REFERENCES sessions(id),
    last_battery_pct REAL,
    last_rssi_dbm    INTEGER,
    last_ip          TEXT,
    firmware_version TEXT,
    notes            TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp  ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_telemetry_session    ON telemetry(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_patient     ON sessions(patient_id);
CREATE INDEX IF NOT EXISTS idx_alerts_session       ON alerts(session_id);
CREATE INDEX IF NOT EXISTS idx_alerts_unack         ON alerts(acknowledged_at) WHERE acknowledged_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_clinical_notes_sess  ON clinical_notes(session_id);
"""


# In-place column additions for older SOMNI databases that pre-date the
# medical-device feature additions.  Safe to run on every boot — each ALTER
# silently no-ops when the column already exists.
_MIGRATIONS = [
    ("users",    "last_login_at",         "DATETIME"),
    ("users",    "last_login_ip",         "TEXT"),
    ("users",    "password_changed_at",   "DATETIME"),
    ("users",    "must_change_password",  "INTEGER NOT NULL DEFAULT 0"),
    ("users",    "is_active",             "INTEGER NOT NULL DEFAULT 1"),
    ("patients", "mrn",                   "TEXT"),
    ("patients", "sex",                   "TEXT"),
    ("patients", "contact_phone",         "TEXT"),
    ("patients", "contact_email",         "TEXT"),
    ("patients", "allergies",             "TEXT"),
    ("patients", "height_cm",             "REAL"),
    ("patients", "weight_kg",             "REAL"),
    ("patients", "archived",              "INTEGER NOT NULL DEFAULT 0"),
    ("patients", "archived_at",           "DATETIME"),
    ("sessions", "discharged_at",         "DATETIME"),
    ("sessions", "discharged_by",         "INTEGER"),
    ("sessions", "discharge_notes",       "TEXT"),
]


def _apply_migrations(conn):
    """Add any newer columns missing from older deployments. Idempotent."""
    for table, column, ddl in _MIGRATIONS:
        try:
            cols = {row["name"] for row in conn.execute(
                "PRAGMA table_info({})".format(table)
            ).fetchall()}
            if column not in cols:
                conn.execute(
                    "ALTER TABLE {} ADD COLUMN {} {}".format(table, column, ddl)
                )
        except Exception as exc:
            print("[SOMNI][DB][WARN] Migration {}.{} failed: {}".format(
                table, column, exc))


def get_db():
    """
    Open (or reuse) a thread-local SQLite database connection.

    Uses thread-local storage for connection pooling — each thread gets
    its own persistent connection that is reused across calls.  Applies
    WAL mode, foreign‑key enforcement, and a busy timeout.

    Args:
        None

    Returns:
        sqlite3.Connection: Open database connection with row_factory set to
                            sqlite3.Row for dict‑like row access.
    """
    conn = getattr(_local, 'connection', None)
    if conn is not None:
        try:
            # Verify connection is still alive
            conn.execute("SELECT 1")
            return conn
        except (sqlite3.Error, sqlite3.ProgrammingError):
            _local.connection = None

    os.makedirs(os.path.dirname(cfg.DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(cfg.DB_PATH, timeout=_QUERY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    _local.connection = conn
    return conn


def close_db():
    """
    Close the thread-local database connection if open.

    Args:
        None

    Returns:
        None
    """
    conn = getattr(_local, 'connection', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.connection = None


def init_db():
    """
    Create all tables if they do not already exist.

    Safe to call multiple times (uses CREATE TABLE IF NOT EXISTS).

    Args:
        None

    Returns:
        None
    """
    os.makedirs(os.path.dirname(cfg.DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(cfg.DB_PATH, timeout=_QUERY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _apply_migrations(conn)
    conn.commit()
    conn.close()
    print("[SOMNI][DB] Database initialised at {}.".format(cfg.DB_PATH))


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

def create_user(username, email, password_hash, role="clinician"):
    """
    Insert a new user record.

    Args:
        username      (str): Unique login name.
        email         (str): Unique email address.
        password_hash (str): bcrypt hash of the plaintext password.
        role          (str): 'admin' or 'clinician'.  Defaults to 'clinician'.

    Returns:
        int: Row ID of the newly created user.

    Raises:
        sqlite3.IntegrityError: If username or email already exists.
    """
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO users (username, email, password_hash, role) "
        "VALUES (?, ?, ?, ?)",
        (username, email, password_hash, role),
    )
    conn.commit()
    return cur.lastrowid


def get_user_by_username(username):
    """
    Fetch a user row by username.

    Args:
        username (str): Login name to look up.

    Returns:
        sqlite3.Row | None: User row, or None if not found.
    """
    conn = get_db()
    return conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def get_user_by_id(user_id):
    """
    Fetch a user row by primary key.

    Args:
        user_id (int): Primary key.

    Returns:
        sqlite3.Row | None: User row, or None if not found.
    """
    conn = get_db()
    return conn.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def list_users():
    """
    Return all users ordered by username.

    Args:
        None

    Returns:
        list[sqlite3.Row]: All user rows.
    """
    conn = get_db()
    return conn.execute(
        "SELECT id, username, email, role, created_at, last_login_at, "
        "last_login_ip, must_change_password, is_active "
        "FROM users ORDER BY username"
    ).fetchall()


def update_user_password(user_id, password_hash, must_change=0):
    """Update a user's password hash and clear the must-change flag."""
    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ?, "
        "password_changed_at = CURRENT_TIMESTAMP, must_change_password = ? "
        "WHERE id = ?",
        (password_hash, 1 if must_change else 0, user_id),
    )
    conn.commit()


def record_user_login(user_id, ip_address):
    """Record a successful login (timestamp + IP)."""
    conn = get_db()
    conn.execute(
        "UPDATE users SET last_login_at = CURRENT_TIMESTAMP, last_login_ip = ? "
        "WHERE id = ?",
        (ip_address, user_id),
    )
    conn.commit()


def set_must_change_password(user_id, flag=1):
    """Force the user to change their password on next login."""
    conn = get_db()
    conn.execute(
        "UPDATE users SET must_change_password = ? WHERE id = ?",
        (1 if flag else 0, user_id),
    )
    conn.commit()


def set_user_active(user_id, active):
    """Activate or deactivate a user account."""
    conn = get_db()
    conn.execute(
        "UPDATE users SET is_active = ? WHERE id = ?",
        (1 if active else 0, user_id),
    )
    conn.commit()


def delete_user(user_id):
    """
    Delete a user by primary key.

    Args:
        user_id (int): Primary key of the user to delete.

    Returns:
        bool: True if a row was deleted, False otherwise.
    """
    conn = get_db()
    cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Patient helpers
# ---------------------------------------------------------------------------

def create_patient(name, dob, notes, created_by, mrn=None, sex=None,
                   contact_phone=None, contact_email=None, allergies=None,
                   height_cm=None, weight_kg=None):
    """
    Insert a new patient record.

    Args:
        name       (str):      Patient full name.
        dob        (str|None): Date of birth in 'YYYY-MM-DD' format, or None.
        notes      (str|None): Free‑text clinical notes.
        created_by (int):      ID of the user creating the record.

    Returns:
        int: Row ID of the newly created patient.
    """
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO patients (name, dob, notes, created_by, mrn, sex, "
        "contact_phone, contact_email, allergies, height_cm, weight_kg) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, dob or None, notes or None, created_by,
         mrn or None, sex or None, contact_phone or None, contact_email or None,
         allergies or None, height_cm, weight_kg),
    )
    conn.commit()
    return cur.lastrowid


def list_patients(include_archived=False):
    """
    Return patients ordered by name. By default the archived list is hidden.
    """
    conn = get_db()
    if include_archived:
        return conn.execute(
            "SELECT p.*, u.username AS created_by_name "
            "FROM patients p LEFT JOIN users u ON p.created_by = u.id "
            "ORDER BY p.archived ASC, p.name"
        ).fetchall()
    return conn.execute(
        "SELECT p.*, u.username AS created_by_name "
        "FROM patients p LEFT JOIN users u ON p.created_by = u.id "
        "WHERE p.archived = 0 "
        "ORDER BY p.name"
    ).fetchall()


def archive_patient(patient_id, archived=True):
    """Archive (soft-delete) or restore a patient."""
    conn = get_db()
    if archived:
        conn.execute(
            "UPDATE patients SET archived = 1, archived_at = CURRENT_TIMESTAMP "
            "WHERE id = ?", (patient_id,))
    else:
        conn.execute(
            "UPDATE patients SET archived = 0, archived_at = NULL WHERE id = ?",
            (patient_id,))
    conn.commit()


def get_patient(patient_id):
    """
    Fetch a single patient by ID.

    Args:
        patient_id (int): Primary key.

    Returns:
        sqlite3.Row | None
    """
    conn = get_db()
    return conn.execute(
        "SELECT * FROM patients WHERE id = ?", (patient_id,)
    ).fetchone()


def update_patient(patient_id, name, dob, notes, mrn=None, sex=None,
                   contact_phone=None, contact_email=None, allergies=None,
                   height_cm=None, weight_kg=None):
    """Update an existing patient record (full demographic set)."""
    conn = get_db()
    cur = conn.execute(
        "UPDATE patients SET name = ?, dob = ?, notes = ?, mrn = ?, sex = ?, "
        "contact_phone = ?, contact_email = ?, allergies = ?, "
        "height_cm = ?, weight_kg = ? WHERE id = ?",
        (name, dob or None, notes or None,
         mrn or None, sex or None,
         contact_phone or None, contact_email or None, allergies or None,
         height_cm, weight_kg, patient_id),
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def create_session(patient_id, device_id="pico-01"):
    """
    Start a new sleep‑monitoring session.

    Args:
        patient_id (int): Patient this session belongs to.
        device_id  (str): Device identifier.  Defaults to 'pico-01'.

    Returns:
        int: Row ID of the new session.
    """
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO sessions (patient_id, device_id) VALUES (?, ?)",
        (patient_id, device_id),
    )
    conn.commit()
    return cur.lastrowid


def end_session(session_id):
    """
    Mark a session as ended at the current UTC time.

    Args:
        session_id (int): Session to close.

    Returns:
        None
    """
    conn = get_db()
    conn.execute(
        "UPDATE sessions SET ended_at = CURRENT_TIMESTAMP WHERE id = ?",
        (session_id,),
    )
    conn.commit()


def list_sessions(patient_id=None):
    """
    Return sessions, optionally filtered by patient.

    Args:
        patient_id (int|None): If given, returns only sessions for this patient.

    Returns:
        list[sqlite3.Row]: Session rows with patient name joined.
    """
    conn = get_db()
    if patient_id is not None:
        return conn.execute(
            "SELECT s.*, p.name AS patient_name FROM sessions s "
            "JOIN patients p ON s.patient_id = p.id "
            "WHERE s.patient_id = ? ORDER BY s.started_at DESC",
            (patient_id,),
        ).fetchall()
    else:
        return conn.execute(
            "SELECT s.*, p.name AS patient_name FROM sessions s "
            "JOIN patients p ON s.patient_id = p.id "
            "ORDER BY s.started_at DESC"
        ).fetchall()


def get_session(session_id):
    """
    Fetch a single session with patient name.

    Args:
        session_id (int): Primary key.

    Returns:
        sqlite3.Row | None
    """
    conn = get_db()
    return conn.execute(
        "SELECT s.*, p.name AS patient_name, p.dob AS patient_dob "
        "FROM sessions s JOIN patients p ON s.patient_id = p.id "
        "WHERE s.id = ?",
        (session_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Telemetry helpers
# ---------------------------------------------------------------------------

def insert_telemetry(session_id, reading):
    """
    Insert one telemetry row.

    Args:
        session_id (int):  Session this reading belongs to.
        reading    (dict): Dict with keys: timestamp_ms, spo2 (dict),
                           accel (dict), gsr (dict).

    Returns:
        int: Row ID of the inserted telemetry row.
    """
    spo2  = reading.get("spo2",  {})
    accel = reading.get("accel", {})
    gsr   = reading.get("gsr",   {})

    conn = get_db()
    cur = conn.execute(
        """INSERT INTO telemetry (
            session_id, timestamp_ms,
            spo2, hr, ir_raw, red_raw,
            accel_x, accel_y, accel_z,
            gsr_raw, gsr_voltage, gsr_conductance_us,
            valid_spo2, valid_accel, valid_gsr
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            session_id,
            reading.get("timestamp_ms"),
            spo2.get("spo2"),
            spo2.get("hr"),
            spo2.get("ir_raw"),
            spo2.get("red_raw"),
            accel.get("x"),
            accel.get("y"),
            accel.get("z"),
            gsr.get("raw"),
            gsr.get("voltage"),
            gsr.get("conductance_us"),
            1 if spo2.get("valid") else 0,
            1 if accel.get("valid") else 0,
            1 if gsr.get("valid") else 0,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_telemetry(session_id, limit=None):
    """
    Return telemetry rows for a session in chronological order.

    Args:
        session_id (int):      Session to query.
        limit      (int|None): Maximum number of rows to return.

    Returns:
        list[sqlite3.Row]: Telemetry rows.
    """
    conn = get_db()
    if limit is not None:
        return conn.execute(
            "SELECT * FROM telemetry WHERE session_id = ? "
            "ORDER BY timestamp_ms LIMIT ?",
            (session_id, limit),
        ).fetchall()
    else:
        return conn.execute(
            "SELECT * FROM telemetry WHERE session_id = ? ORDER BY timestamp_ms",
            (session_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def save_report(session_id, pdf_path, summary_json, hmac_sig):
    """
    Insert or replace the report record for a session.

    Args:
        session_id   (int): Session this report covers.
        pdf_path     (str): Absolute path to the generated PDF file.
        summary_json (str): JSON string of the sleep‑summary dict.
        hmac_sig     (str): Hex HMAC‑SHA256 of summary_json for integrity.

    Returns:
        int: Row ID.
    """
    conn = get_db()
    # Delete any existing report for this session before inserting
    conn.execute("DELETE FROM reports WHERE session_id = ?", (session_id,))
    cur = conn.execute(
        "INSERT INTO reports (session_id, pdf_path, summary_json, hmac_sig) "
        "VALUES (?, ?, ?, ?)",
        (session_id, pdf_path, summary_json, hmac_sig),
    )
    conn.commit()
    return cur.lastrowid


def get_report(session_id):
    """
    Fetch the report for a session.

    Args:
        session_id (int): Session to look up.

    Returns:
        sqlite3.Row | None
    """
    conn = get_db()
    return conn.execute(
        "SELECT * FROM reports WHERE session_id = ?", (session_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Audit log helpers
# ---------------------------------------------------------------------------

def insert_audit_log(event_type, username=None, ip_address=None, details=None):
    """
    Insert an audit log entry into the database.

    Args:
        event_type (str):      Type of event (e.g. 'LOGIN_SUCCESS').
        username   (str|None): Username associated with the event.
        ip_address (str|None): IP address of the client.
        details    (str|None): JSON string with additional event details.

    Returns:
        int: Row ID of the inserted audit log entry.
    """
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO audit_log (event_type, username, ip_address, details) "
        "VALUES (?, ?, ?, ?)",
        (event_type, username, ip_address, details),
    )
    conn.commit()
    return cur.lastrowid


def get_audit_logs(limit=100, event_type=None, username=None, since=None):
    """Fetch audit-log entries with optional filters (event type, username,
    ISO-8601 ``since`` timestamp). Always ordered newest-first.
    """
    conn = get_db()
    sql = "SELECT * FROM audit_log WHERE 1=1"
    args = []
    if event_type:
        sql += " AND event_type = ?"
        args.append(event_type)
    if username:
        sql += " AND username = ?"
        args.append(username)
    if since:
        sql += " AND timestamp >= ?"
        args.append(since)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    args.append(int(limit))
    return conn.execute(sql, args).fetchall()


def list_distinct_audit_event_types():
    """Return the set of distinct event_type strings present in the audit log."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT event_type FROM audit_log ORDER BY event_type"
    ).fetchall()
    return [r["event_type"] for r in rows]


# ---------------------------------------------------------------------------
# Clinical notes — free-text observations attached to a sleep session.
# ---------------------------------------------------------------------------

def add_clinical_note(session_id, author_id, body):
    """Insert a clinical note. Returns the new row's ID."""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO clinical_notes (session_id, author_id, body) "
        "VALUES (?, ?, ?)",
        (session_id, author_id, body),
    )
    conn.commit()
    return cur.lastrowid


def list_clinical_notes(session_id):
    """Return clinical notes for a session, newest first, with author name."""
    conn = get_db()
    return conn.execute(
        "SELECT n.*, u.username AS author_name "
        "FROM clinical_notes n LEFT JOIN users u ON n.author_id = u.id "
        "WHERE n.session_id = ? ORDER BY n.created_at DESC",
        (session_id,),
    ).fetchall()


def delete_clinical_note(note_id, session_id):
    """Delete a single clinical note (scoped to its session for safety)."""
    conn = get_db()
    cur = conn.execute(
        "DELETE FROM clinical_notes WHERE id = ? AND session_id = ?",
        (note_id, session_id),
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Alerts — clinical threshold breaches captured during ingest.
# ---------------------------------------------------------------------------

def insert_alert(session_id, alert_type, severity, metric=None,
                 measured_value=None, threshold_value=None, message=None):
    """Persist an alert. Returns the row ID."""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO alerts (session_id, alert_type, severity, metric, "
        "measured_value, threshold_value, message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, alert_type, severity, metric,
         measured_value, threshold_value, message),
    )
    conn.commit()
    return cur.lastrowid


def list_alerts(session_id=None, only_unacknowledged=False, limit=200):
    """Return alerts with patient name joined for convenience."""
    conn = get_db()
    sql = (
        "SELECT a.*, s.patient_id, p.name AS patient_name, "
        "u.username AS acknowledged_by_name "
        "FROM alerts a "
        "JOIN sessions s ON a.session_id = s.id "
        "JOIN patients p ON s.patient_id = p.id "
        "LEFT JOIN users u ON a.acknowledged_by = u.id "
        "WHERE 1=1"
    )
    args = []
    if session_id is not None:
        sql += " AND a.session_id = ?"
        args.append(session_id)
    if only_unacknowledged:
        sql += " AND a.acknowledged_at IS NULL"
    sql += " ORDER BY a.triggered_at DESC LIMIT ?"
    args.append(int(limit))
    return conn.execute(sql, args).fetchall()


def acknowledge_alert(alert_id, user_id):
    """Mark a single alert as acknowledged. Returns True if a row changed."""
    conn = get_db()
    cur = conn.execute(
        "UPDATE alerts SET acknowledged_at = CURRENT_TIMESTAMP, "
        "acknowledged_by = ? WHERE id = ? AND acknowledged_at IS NULL",
        (user_id, alert_id),
    )
    conn.commit()
    return cur.rowcount > 0


def count_unacknowledged_alerts():
    """Return the count of un-acknowledged alerts (used in the nav badge)."""
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM alerts WHERE acknowledged_at IS NULL"
    ).fetchone()
    return int(row["c"]) if row else 0


# ---------------------------------------------------------------------------
# Device fleet — last-seen telemetry per Pico.
# ---------------------------------------------------------------------------

def upsert_device_seen(device_id, session_id=None, battery_pct=None,
                        rssi_dbm=None, ip_address=None, firmware_version=None):
    """Update last-seen / status for a device. Inserts on first contact."""
    conn = get_db()
    conn.execute(
        "INSERT INTO devices (device_id, last_seen_at, last_session_id, "
        "                     last_battery_pct, last_rssi_dbm, last_ip, "
        "                     firmware_version) "
        "VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?) "
        "ON CONFLICT(device_id) DO UPDATE SET "
        "  last_seen_at     = CURRENT_TIMESTAMP, "
        "  last_session_id  = COALESCE(excluded.last_session_id,  devices.last_session_id), "
        "  last_battery_pct = COALESCE(excluded.last_battery_pct, devices.last_battery_pct), "
        "  last_rssi_dbm    = COALESCE(excluded.last_rssi_dbm,    devices.last_rssi_dbm), "
        "  last_ip          = COALESCE(excluded.last_ip,          devices.last_ip), "
        "  firmware_version = COALESCE(excluded.firmware_version, devices.firmware_version)",
        (device_id, session_id, battery_pct, rssi_dbm,
         ip_address, firmware_version),
    )
    conn.commit()


def list_devices():
    """Return all known devices ordered by most-recent last-seen first."""
    conn = get_db()
    return conn.execute(
        "SELECT d.*, s.patient_id, p.name AS last_patient_name "
        "FROM devices d "
        "LEFT JOIN sessions s ON d.last_session_id = s.id "
        "LEFT JOIN patients p ON s.patient_id = p.id "
        "ORDER BY d.last_seen_at DESC NULLS LAST, d.device_id"
    ).fetchall()


# ---------------------------------------------------------------------------
# Session helpers — discharge and latest-telemetry convenience.
# ---------------------------------------------------------------------------

def discharge_session(session_id, user_id, discharge_notes=None):
    """Mark a session as clinically discharged."""
    conn = get_db()
    conn.execute(
        "UPDATE sessions SET discharged_at = CURRENT_TIMESTAMP, "
        "discharged_by = ?, discharge_notes = ?, "
        "ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP) WHERE id = ?",
        (user_id, discharge_notes, session_id),
    )
    conn.commit()


def get_latest_telemetry(session_id):
    """Return the most-recent telemetry row for a session (or None)."""
    conn = get_db()
    return conn.execute(
        "SELECT * FROM telemetry WHERE session_id = ? "
        "ORDER BY timestamp_ms DESC LIMIT 1",
        (session_id,),
    ).fetchone()


def get_active_sessions():
    """Sessions that have not yet ended (live monitor view)."""
    conn = get_db()
    return conn.execute(
        "SELECT s.*, p.name AS patient_name, p.mrn AS patient_mrn "
        "FROM sessions s JOIN patients p ON s.patient_id = p.id "
        "WHERE s.ended_at IS NULL ORDER BY s.started_at DESC"
    ).fetchall()
