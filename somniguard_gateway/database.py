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

Educational prototype — not a clinically approved device.
"""

import sqlite3
import os

import config as cfg


# ---------------------------------------------------------------------------
# DDL — table creation statements
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    UNIQUE NOT NULL,
    email         TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'clinician',
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS patients (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    dob        DATE,
    notes      TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id  INTEGER NOT NULL REFERENCES patients(id),
    device_id   TEXT NOT NULL DEFAULT 'pico-01',
    started_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    ended_at    DATETIME,
    notes       TEXT
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
"""


def get_db():
    """
    Open (or create) the SQLite database and return a connection.

    Applies WAL mode and foreign‑key enforcement.  The caller is responsible
    for closing the connection (or using it as a context manager).

    Args:
        None

    Returns:
        sqlite3.Connection: Open database connection with row_factory set to
                            sqlite3.Row for dict‑like row access.
    """
    os.makedirs(os.path.dirname(cfg.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(cfg.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    """
    Create all tables if they do not already exist.

    Safe to call multiple times (uses CREATE TABLE IF NOT EXISTS).

    Args:
        None

    Returns:
        None
    """
    os.makedirs(os.path.dirname(cfg.DB_PATH), exist_ok=True)
    conn = get_db()
    conn.executescript(_SCHEMA)
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
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, email, password_hash, role) "
            "VALUES (?, ?, ?, ?)",
            (username, email, password_hash, role),
        )
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
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    return row


def get_user_by_id(user_id):
    """
    Fetch a user row by primary key.

    Args:
        user_id (int): Primary key.

    Returns:
        sqlite3.Row | None: User row, or None if not found.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row


def list_users():
    """
    Return all users ordered by username.

    Args:
        None

    Returns:
        list[sqlite3.Row]: All user rows.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT id, username, email, role, created_at FROM users ORDER BY username"
    ).fetchall()
    conn.close()
    return rows


def delete_user(user_id):
    """
    Delete a user by primary key.

    Args:
        user_id (int): Primary key of the user to delete.

    Returns:
        bool: True if a row was deleted, False otherwise.
    """
    with get_db() as conn:
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Patient helpers
# ---------------------------------------------------------------------------

def create_patient(name, dob, notes, created_by):
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
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO patients (name, dob, notes, created_by) VALUES (?, ?, ?, ?)",
            (name, dob or None, notes or None, created_by),
        )
        return cur.lastrowid


def list_patients():
    """
    Return all patients ordered by name.

    Args:
        None

    Returns:
        list[sqlite3.Row]: All patient rows.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT p.*, u.username AS created_by_name "
        "FROM patients p LEFT JOIN users u ON p.created_by = u.id "
        "ORDER BY p.name"
    ).fetchall()
    conn.close()
    return rows


def get_patient(patient_id):
    """
    Fetch a single patient by ID.

    Args:
        patient_id (int): Primary key.

    Returns:
        sqlite3.Row | None
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    conn.close()
    return row


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
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (patient_id, device_id) VALUES (?, ?)",
            (patient_id, device_id),
        )
        return cur.lastrowid


def end_session(session_id):
    """
    Mark a session as ended at the current UTC time.

    Args:
        session_id (int): Session to close.

    Returns:
        None
    """
    with get_db() as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )


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
        rows = conn.execute(
            "SELECT s.*, p.name AS patient_name FROM sessions s "
            "JOIN patients p ON s.patient_id = p.id "
            "WHERE s.patient_id = ? ORDER BY s.started_at DESC",
            (patient_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT s.*, p.name AS patient_name FROM sessions s "
            "JOIN patients p ON s.patient_id = p.id "
            "ORDER BY s.started_at DESC"
        ).fetchall()
    conn.close()
    return rows


def get_session(session_id):
    """
    Fetch a single session with patient name.

    Args:
        session_id (int): Primary key.

    Returns:
        sqlite3.Row | None
    """
    conn = get_db()
    row = conn.execute(
        "SELECT s.*, p.name AS patient_name, p.dob AS patient_dob "
        "FROM sessions s JOIN patients p ON s.patient_id = p.id "
        "WHERE s.id = ?",
        (session_id,),
    ).fetchone()
    conn.close()
    return row


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

    with get_db() as conn:
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
        rows = conn.execute(
            "SELECT * FROM telemetry WHERE session_id = ? "
            "ORDER BY timestamp_ms LIMIT ?",
            (session_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM telemetry WHERE session_id = ? ORDER BY timestamp_ms",
            (session_id,),
        ).fetchall()
    conn.close()
    return rows


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
    # Delete any existing report for this session before inserting
    with get_db() as conn:
        conn.execute("DELETE FROM reports WHERE session_id = ?", (session_id,))
        cur = conn.execute(
            "INSERT INTO reports (session_id, pdf_path, summary_json, hmac_sig) "
            "VALUES (?, ?, ?, ?)",
            (session_id, pdf_path, summary_json, hmac_sig),
        )
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
    row = conn.execute(
        "SELECT * FROM reports WHERE session_id = ?", (session_id,)
    ).fetchone()
    conn.close()
    return row
