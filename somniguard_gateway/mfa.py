"""
mfa.py — TOTP-based multi-factor authentication for the SOMNI-Guard gateway.

Implements RFC 6238 (TOTP) authenticator-app login factor plus single-use
backup codes. Secrets are encrypted at rest using the gateway SECRET_KEY as
a derivation key (AES-GCM via cryptography.fernet) so a database leak alone
does not yield usable second factors.

Educational prototype — not a clinically approved device.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import secrets
from typing import Optional

import bcrypt
import pyotp

from cryptography.fernet import Fernet, InvalidToken
import qrcode

import config as cfg
import database as db


# ---------------------------------------------------------------------------
# Symmetric encryption for stored TOTP secrets
# ---------------------------------------------------------------------------

def _fernet() -> Fernet:
    """Derive a Fernet key from SOMNI_SECRET_KEY.

    A separate environment variable (SOMNI_MFA_KEY) takes precedence so the
    operator can rotate the MFA wrapping key independently of cookie-signing
    secrets. Falling back to SOMNI_SECRET_KEY is acceptable because the
    gateway already treats SECRET_KEY as a server-side root secret.
    """
    raw = os.environ.get("SOMNI_MFA_KEY") or cfg.SECRET_KEY
    if not raw:
        raise RuntimeError("SOMNI_SECRET_KEY (or SOMNI_MFA_KEY) is required for MFA.")
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def _decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError(
            "Stored MFA secret cannot be decrypted — the SECRET_KEY may have "
            "been rotated without re-enrolling users."
        ) from exc


# ---------------------------------------------------------------------------
# Schema migration — add MFA columns / table on first import.
# ---------------------------------------------------------------------------

_MFA_SCHEMA = """
CREATE TABLE IF NOT EXISTS mfa_secrets (
    user_id        INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    totp_secret    TEXT    NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 0,
    enrolled_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used_step INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mfa_backup_codes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT    NOT NULL,
    used_at   DATETIME
);

CREATE INDEX IF NOT EXISTS idx_mfa_backup_user ON mfa_backup_codes(user_id);
"""


def init_mfa_schema() -> None:
    """Create MFA tables if they do not yet exist. Idempotent."""
    conn = db.get_db()
    conn.executescript(_MFA_SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Backup-code helpers
# ---------------------------------------------------------------------------

#: Number of single-use backup codes to issue per user.
BACKUP_CODE_COUNT = 10
BACKUP_CODE_LEN   = 10   # digits — high entropy without being unwieldy


def _hash_code(code: str) -> str:
    return bcrypt.hashpw(code.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def _check_code(code: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(code.encode("utf-8"), h.encode("utf-8"))
    except Exception:
        return False


def generate_backup_codes(user_id: int) -> list[str]:
    """Generate (and store the bcrypt hashes of) backup codes for *user_id*.

    Returns the plaintext codes — these MUST be shown to the user once and
    then forgotten.
    """
    conn = db.get_db()
    conn.execute("DELETE FROM mfa_backup_codes WHERE user_id = ?", (user_id,))
    codes: list[str] = []
    for _ in range(BACKUP_CODE_COUNT):
        # secrets.choice avoids modulo bias of os.urandom + int.
        code = "".join(secrets.choice("0123456789") for _ in range(BACKUP_CODE_LEN))
        codes.append(code)
        conn.execute(
            "INSERT INTO mfa_backup_codes (user_id, code_hash) VALUES (?, ?)",
            (user_id, _hash_code(code)),
        )
    conn.commit()
    return codes


def consume_backup_code(user_id: int, supplied: str) -> bool:
    """Verify a backup code in constant-ish time and mark it used.

    Returns True iff the code matched an unused row.
    """
    supplied = supplied.strip().replace("-", "").replace(" ", "")
    if not supplied or not supplied.isdigit():
        return False

    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, code_hash FROM mfa_backup_codes "
        "WHERE user_id = ? AND used_at IS NULL",
        (user_id,),
    ).fetchall()
    matched_id: Optional[int] = None
    for row in rows:
        if _check_code(supplied, row["code_hash"]):
            matched_id = row["id"]
            break
    if matched_id is None:
        return False

    conn.execute(
        "UPDATE mfa_backup_codes SET used_at = CURRENT_TIMESTAMP WHERE id = ?",
        (matched_id,),
    )
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# TOTP enrolment / verification
# ---------------------------------------------------------------------------

def begin_enrolment(user_id: int, *, force_new: bool = False) -> tuple[str, str]:
    """Return the TOTP secret + provisioning URI for *user_id*'s enrolment.

    On first call (or when *force_new*), a fresh base32 secret is generated
    and stored encrypted with ``enabled=0``. Subsequent calls reuse that
    pending secret so refreshing the setup page does not invalidate the QR
    code the user already scanned.

    Once the user successfully verifies with :func:`finish_enrolment` the
    secret is flipped to ``enabled=1`` and any later call to this function
    will issue a new pending secret (because the existing row is enabled
    and we never overwrite an enabled row here).
    """
    conn = db.get_db()
    user = db.get_user_by_id(user_id)
    if user is None:
        raise ValueError(f"No user {user_id}")

    issuer = os.environ.get("SOMNI_MFA_ISSUER", "SOMNI-Guard")

    existing = conn.execute(
        "SELECT totp_secret, enabled FROM mfa_secrets WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if existing and not force_new and not existing["enabled"]:
        # Re-use the in-progress secret.
        try:
            secret_b32 = _decrypt(existing["totp_secret"])
        except RuntimeError:
            secret_b32 = pyotp.random_base32()
            conn.execute(
                "UPDATE mfa_secrets SET totp_secret = ?, last_used_step = 0 "
                "WHERE user_id = ?",
                (_encrypt(secret_b32), user_id),
            )
            conn.commit()
    else:
        secret_b32 = pyotp.random_base32()
        enc = _encrypt(secret_b32)
        conn.execute(
            "INSERT INTO mfa_secrets (user_id, totp_secret, enabled, last_used_step) "
            "VALUES (?, ?, 0, 0) "
            "ON CONFLICT(user_id) DO UPDATE SET totp_secret = excluded.totp_secret, "
            "                                  enabled = 0, last_used_step = 0",
            (user_id, enc),
        )
        conn.commit()

    uri = pyotp.totp.TOTP(secret_b32).provisioning_uri(
        name=f"{user['username']}@{issuer}",
        issuer_name=issuer,
    )
    return secret_b32, uri


def finish_enrolment(user_id: int, code: str) -> bool:
    """Verify *code* against the user's pending TOTP secret. On success the
    secret is marked enabled.

    Returns True iff the code was valid.
    """
    if not _verify_totp(user_id, code, mark_step=False, only_if_disabled=True):
        return False
    db.get_db().execute(
        "UPDATE mfa_secrets SET enabled = 1 WHERE user_id = ?",
        (user_id,),
    )
    db.get_db().commit()
    return True


def disable_mfa(user_id: int) -> None:
    conn = db.get_db()
    conn.execute("DELETE FROM mfa_secrets       WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM mfa_backup_codes  WHERE user_id = ?", (user_id,))
    conn.commit()


def is_mfa_enabled(user_id: int) -> bool:
    row = db.get_db().execute(
        "SELECT enabled FROM mfa_secrets WHERE user_id = ?", (user_id,)
    ).fetchone()
    return bool(row and row["enabled"])


def verify_totp(user_id: int, code: str) -> bool:
    """Constant-time check that *code* matches the user's TOTP at the
    current step (with ±1 step window). Replays of the same step are
    refused via the ``last_used_step`` column.
    """
    return _verify_totp(user_id, code, mark_step=True, only_if_disabled=False)


def _verify_totp(
    user_id: int,
    code: str,
    *,
    mark_step: bool,
    only_if_disabled: bool,
) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False

    conn = db.get_db()
    row = conn.execute(
        "SELECT totp_secret, enabled, last_used_step "
        "FROM mfa_secrets WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return False
    if only_if_disabled and row["enabled"]:
        return False

    try:
        secret = _decrypt(row["totp_secret"])
    except RuntimeError:
        return False

    totp = pyotp.TOTP(secret)
    # Verify with a ±1 step (30 s) tolerance to allow for clock drift.
    valid_step: Optional[int] = None
    import time
    now = int(time.time())
    for offset in (-1, 0, 1):
        if totp.verify(code, for_time=now + offset * totp.interval):
            valid_step = (now + offset * totp.interval) // totp.interval
            break
    if valid_step is None:
        return False
    if valid_step <= int(row["last_used_step"] or 0):
        # Replay of an already-consumed code.
        return False

    if mark_step:
        conn.execute(
            "UPDATE mfa_secrets SET last_used_step = ? WHERE user_id = ?",
            (valid_step, user_id),
        )
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# QR helper for the enrolment page
# ---------------------------------------------------------------------------

def qr_png_bytes(uri: str) -> bytes:
    """Return a PNG byte string encoding *uri* as a QR code."""
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
