"""
audit.py — SOMNI‑Guard gateway structured audit logging.

Provides an AuditLogger class and a module-level singleton (audit_log) that
records security-relevant events — login attempts, data access, API calls,
and administrative actions — in a structured JSON log file with automatic
rotation.

Log files are written to the same directory as the SQLite database by default,
or to a caller-supplied directory.  A rotating file handler caps each log file
at 10 MB and keeps up to 5 backup files.  Console output uses a human-readable
format for operator visibility.

Educational prototype — not a clinically approved device.
"""

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import config as cfg


# ---------------------------------------------------------------------------
# JSON log formatter
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object."""

    def format(self, record):
        # The record's message is already a dict serialised to JSON by
        # AuditLogger._emit(); just pass it through as the raw line.
        return record.getMessage()


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------

class AuditLogger:
    """
    Structured audit logger for the SOMNI‑Guard gateway.

    Each audit event is written as a JSON object to a rotating log file and
    as a human-readable line to the console.

    Args:
        log_dir (str|None): Directory in which to create ``audit.log``.
                            Defaults to the directory that contains the
                            SQLite database (``config.DB_PATH``).
    """

    #: Maximum size of a single log file before rotation (10 MB).
    MAX_BYTES = 10 * 1024 * 1024

    #: Number of rotated backup files to keep.
    BACKUP_COUNT = 5

    def __init__(self, log_dir=None):
        if log_dir is None:
            log_dir = os.path.dirname(os.path.abspath(cfg.DB_PATH))

        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "audit.log")

        self._logger = logging.getLogger("somniguard.audit")
        self._logger.setLevel(logging.INFO)

        # Avoid adding duplicate handlers when re-initialised in the same
        # process (e.g. during tests or hot-reload).
        if not self._logger.handlers:
            # --- Rotating JSON file handler ---
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=self.MAX_BYTES,
                backupCount=self.BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(_JsonFormatter())

            # --- Human-readable console handler ---
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(
                logging.Formatter(
                    "[SOMNI][AUDIT] %(asctime)s %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S",
                )
            )

            self._logger.addHandler(file_handler)
            self._logger.addHandler(console_handler)

        print(
            "[SOMNI][AUDIT] Audit logger initialised. "
            "Log file: {}".format(log_path)
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        """Return the current UTC time as an ISO 8601 string."""
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _emit(self, event_type: str, ip_address: str,
              username: str | None = None,
              details: dict | None = None) -> None:
        """
        Build a structured audit record and write it to the log.

        Args:
            event_type  (str):       Short uppercase identifier for the event.
            ip_address  (str):       Source IP address of the request.
            username    (str|None):  Authenticated (or attempted) username.
            details     (dict|None): Event-specific supplementary data.
        """
        record = {
            "timestamp": self._now_iso(),
            "event_type": event_type,
            "ip_address": ip_address,
            "details": details or {},
        }
        if username is not None:
            record["username"] = username

        # Serialise to compact JSON; the _JsonFormatter will pass it through
        # verbatim to the file handler.
        json_line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)

        # LogRecord message used by the file handler (raw JSON).
        # The console handler prefixes its own timestamp + label, so we use a
        # compact summary for that channel.
        self._logger.info(json_line)

    # ------------------------------------------------------------------
    # Authentication events
    # ------------------------------------------------------------------

    def log_login_attempt(self, username: str, ip_address: str,
                          success: bool, reason: str | None = None) -> None:
        """
        Record a login attempt (success or failure).

        Args:
            username   (str):       Username supplied in the login form.
            ip_address (str):       Client IP address.
            success    (bool):      True if authentication succeeded.
            reason     (str|None):  Human-readable explanation for failures.
        """
        event_type = "LOGIN_SUCCESS" if success else "LOGIN_FAILURE"
        details = {"success": success}
        if reason:
            details["reason"] = reason
        self._emit(event_type, ip_address, username=username, details=details)

    def log_login_lockout(self, ip_address: str, duration_seconds: int) -> None:
        """
        Record that an IP address has been temporarily locked out.

        Args:
            ip_address       (str): Locked-out client IP address.
            duration_seconds (int): Lockout duration in seconds.
        """
        self._emit(
            "LOGIN_LOCKOUT",
            ip_address,
            details={"duration_seconds": duration_seconds},
        )

    def log_logout(self, username: str, ip_address: str) -> None:
        """
        Record a user logout.

        Args:
            username   (str): Username of the session being ended.
            ip_address (str): Client IP address.
        """
        self._emit("LOGOUT", ip_address, username=username)

    # ------------------------------------------------------------------
    # Data access events
    # ------------------------------------------------------------------

    def log_data_access(self, username: str, ip_address: str,
                        resource_type: str, resource_id, action: str) -> None:
        """
        Record access to a data resource (patient records, sessions, etc.).

        Args:
            username      (str):       Authenticated username.
            ip_address    (str):       Client IP address.
            resource_type (str):       Type of resource (e.g. "patient", "session").
            resource_id   (int|str):   Primary key or identifier of the resource.
            action        (str):       Action performed (e.g. "read", "create", "delete").
        """
        self._emit(
            "DATA_ACCESS",
            ip_address,
            username=username,
            details={
                "resource_type": resource_type,
                "resource_id": resource_id,
                "action": action,
            },
        )

    # ------------------------------------------------------------------
    # API access events (Pico telemetry ingestion)
    # ------------------------------------------------------------------

    def log_api_access(self, ip_address: str, endpoint: str, method: str,
                       status_code: int, device_id: str | None = None) -> None:
        """
        Record an API request (typically from the Pico 2W telemetry uplink).

        Args:
            ip_address  (str):       Source IP address.
            endpoint    (str):       URL path of the API endpoint.
            method      (str):       HTTP method (GET, POST, …).
            status_code (int):       HTTP response status code.
            device_id   (str|None):  Pico device identifier, if available.
        """
        details = {
            "endpoint": endpoint,
            "method": method.upper(),
            "status_code": status_code,
        }
        if device_id is not None:
            details["device_id"] = device_id
        self._emit("API_ACCESS", ip_address, details=details)

    # ------------------------------------------------------------------
    # Report events
    # ------------------------------------------------------------------

    def log_report_generated(self, username: str, ip_address: str,
                             session_id: int) -> None:
        """
        Record that a sleep-session report was generated.

        Args:
            username   (str): User who triggered report generation.
            ip_address (str): Client IP address.
            session_id (int): Sleep session the report covers.
        """
        self._emit(
            "REPORT_GENERATED",
            ip_address,
            username=username,
            details={"session_id": session_id},
        )

    def log_report_downloaded(self, username: str, ip_address: str,
                              session_id: int) -> None:
        """
        Record that a sleep-session PDF report was downloaded.

        Args:
            username   (str): User who downloaded the report.
            ip_address (str): Client IP address.
            session_id (int): Sleep session the report covers.
        """
        self._emit(
            "REPORT_DOWNLOADED",
            ip_address,
            username=username,
            details={"session_id": session_id},
        )

    # ------------------------------------------------------------------
    # Administrative / user-management events
    # ------------------------------------------------------------------

    def log_user_created(self, admin_username: str, ip_address: str,
                         new_username: str, role: str) -> None:
        """
        Record that an administrator created a new gateway user account.

        Args:
            admin_username (str): Username of the administrator.
            ip_address     (str): Client IP address.
            new_username   (str): Username of the newly created account.
            role           (str): Role assigned to the new account.
        """
        self._emit(
            "USER_CREATED",
            ip_address,
            username=admin_username,
            details={"new_username": new_username, "role": role},
        )

    def log_user_deleted(self, admin_username: str, ip_address: str,
                         deleted_user_id: int) -> None:
        """
        Record that an administrator deleted a gateway user account.

        Args:
            admin_username  (str): Username of the administrator.
            ip_address      (str): Client IP address.
            deleted_user_id (int): Primary key of the deleted user record.
        """
        self._emit(
            "USER_DELETED",
            ip_address,
            username=admin_username,
            details={"deleted_user_id": deleted_user_id},
        )

    # ------------------------------------------------------------------
    # Generic security events
    # ------------------------------------------------------------------

    def log_security_event(self, event_type: str, ip_address: str,
                           details: dict | None = None) -> None:
        """
        Record a generic security event not covered by the specific methods.

        Args:
            event_type (str):       Short uppercase identifier (e.g. "CSRF_VIOLATION").
            ip_address (str):       Source IP address.
            details    (dict|None): Arbitrary supplementary data.
        """
        self._emit(event_type, ip_address, details=details)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

#: Lazily initialised module-level AuditLogger instance.
#: Call ``init_audit_log()`` before using this object, or import and call
#: ``audit_log`` methods directly after the app has started (which triggers
#: auto-initialisation via ``init_audit_log()`` with defaults).
audit_log: AuditLogger | None = None


def init_audit_log(log_dir: str | None = None) -> AuditLogger:
    """
    Initialise (or re-initialise) the module-level audit logger singleton.

    This function is idempotent when called with the same ``log_dir``.  It
    should be called once at application startup, before the first audit event
    is emitted.

    Args:
        log_dir (str|None): Directory for the ``audit.log`` file.  Defaults to
                            the directory that contains the SQLite database.

    Returns:
        AuditLogger: The initialised (or existing) singleton instance.
    """
    global audit_log  # noqa: PLW0603

    print("[SOMNI][AUDIT] Initialising audit logging subsystem.")
    audit_log = AuditLogger(log_dir=log_dir)
    return audit_log
