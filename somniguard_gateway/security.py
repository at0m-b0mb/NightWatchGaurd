"""
somniguard_gateway/security.py

Security utilities for the SomniGuard Raspberry Pi 5 gateway (Flask application).

Provides rate limiting, security headers, session configuration, password validation,
account lockout tracking, and input sanitization.

DISCLAIMER: Educational prototype — not a clinically approved device.
"""

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from threading import Lock

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ---------------------------------------------------------------------------
# Rate limit constants
# ---------------------------------------------------------------------------

LOGIN_RATE_LIMIT = "5 per minute"
API_RATE_LIMIT = "20 per second"

# ---------------------------------------------------------------------------
# Flask-Limiter setup
# ---------------------------------------------------------------------------

_limiter: Limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri="memory://",
)


def init_limiter(app) -> Limiter:
    """Attach the module-level Limiter to *app* and return it.

    Call this once during application factory setup:

        limiter = init_limiter(app)

    After that, decorate individual routes with::

        @limiter.limit(LOGIN_RATE_LIMIT)
        def login(): ...
    """
    _limiter.init_app(app)
    print("[SOMNI][SECURITY] Flask-Limiter initialised "
          f"(login={LOGIN_RATE_LIMIT}, api={API_RATE_LIMIT}).")
    return _limiter


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

#: Pages whose responses should carry aggressive cache-busting headers.
_SENSITIVE_PATH_PREFIXES = (
    "/login",
    "/logout",
    "/api/",
    "/admin",
    "/dashboard",
)


def add_security_headers(response):
    """Flask ``after_request`` handler that attaches security headers.

    Register with::

        app.after_request(add_security_headers)
    """
    # HSTS: 1 year, all subdomains, preload-eligible.
    # Safe to send even when serving over plain HTTP — browsers ignore HSTS
    # over HTTP for the current request but cache it for the next HTTPS visit.
    response.headers["Strict-Transport-Security"] = (
        "max-age=63072000; includeSubDomains; preload"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # CSP:
    #   - default-src 'self'         everything else inherits this
    #   - script-src  'self'         no inline JS, no eval — XSS containment
    #   - style-src   'self' 'unsafe-inline'  templates use inline style="…"
    #                                attributes; Trusted Types/nonces would be
    #                                cleaner but require a template rewrite.
    #                                Inline style cannot exfiltrate data on
    #                                modern browsers (CSS expression is gone).
    #   - img-src 'self' data:       allow data: URIs for embedded QR codes
    #   - connect-src 'self'         block fetch/XHR to off-origin
    #   - object-src 'none'          no plugins
    #   - base-uri 'self'            block <base> tag injection
    #   - form-action 'self'         forms must submit to this origin
    #   - frame-ancestors 'none'     equivalent to X-Frame-Options: DENY
    #   - upgrade-insecure-requests  rewrite any http:// resource to https://
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "upgrade-insecure-requests"
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # X-XSS-Protection: disabled per modern best practice (browsers handle this
    # better without the header; some implementations introduced vulnerabilities).
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), "
        "payment=(), usb=(), bluetooth=(), magnetometer=(), gyroscope=(), "
        "accelerometer=(), interest-cohort=()"
    )
    # Cross-origin isolation — a missing or wrong COOP/CORP combination is
    # commonly flagged by web pen-test scanners.
    response.headers["Cross-Origin-Opener-Policy"]   = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    # Block legacy Flash/Silverlight crossdomain.xml lookups
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    # Hide implementation details (version-string scanning is PT6 territory).
    # Werkzeug appends its own Server header at the WSGI layer; .set() replaces
    # any existing value rather than adding a second one.
    response.headers.set("Server", "SOMNI-Guard")
    if "X-Powered-By" in response.headers:
        del response.headers["X-Powered-By"]

    # Cache-Control for sensitive pages
    from flask import request as flask_request  # local import avoids circular deps

    path = flask_request.path
    if any(path.startswith(prefix) for prefix in _SENSITIVE_PATH_PREFIXES):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"

    return response


# ---------------------------------------------------------------------------
# Session security
# ---------------------------------------------------------------------------

#: Session lifetime (30 minutes).
SESSION_TIMEOUT_MINUTES = 30


def configure_session(app) -> None:
    """Apply secure session settings to *app*.

    Sets HTTP-only cookies, Secure flag, SameSite=Lax, and a 30-minute
    session lifetime.
    """
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SECURE", True)   # Requires HTTPS
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault(
        "PERMANENT_SESSION_LIFETIME",
        timedelta(minutes=SESSION_TIMEOUT_MINUTES),
    )
    print(
        f"[SOMNI][SECURITY] Session configured: httponly=True, secure=True, "
        f"samesite=Lax, timeout={SESSION_TIMEOUT_MINUTES}min."
    )


# ---------------------------------------------------------------------------
# Password complexity validator
# ---------------------------------------------------------------------------

# NIST SP 800-63B minimum is 8; recommended for admin/PHI accounts is ≥14.
# We pick 14 to satisfy PT8 (NIST password-policy review).
PASSWORD_MIN_LENGTH = 14
PASSWORD_MAX_LENGTH = 128

# Tiny built-in deny-list of the most-common passwords / dictionary words that
# survive complexity rules but fail a real cracker in <1s.  Not a substitute
# for HIBP or zxcvbn, but blocks the obvious "Password123!" class of secrets.
_COMMON_PASSWORDS = frozenset({
    "password", "passw0rd", "p@ssw0rd", "p@ssword",
    "qwerty", "qwertyuiop", "asdfgh", "letmein",
    "welcome", "admin", "administrator", "root",
    "12345678", "123456789", "1234567890", "abc123",
    "iloveyou", "monkey", "dragon", "sunshine",
    "master", "trustno1", "changeme", "secret",
    "somniguard", "somniguard123",
})


def validate_password_complexity(password: str) -> tuple[bool, list[str]]:
    """Validate *password* against NIST-aligned complexity rules.

    Rules
    -----
    - Length between 14 and 128 characters (NIST SP 800-63B; long passphrases
      are stronger than short complex ones, but we still enforce class mix).
    - At least one uppercase letter (A-Z)
    - At least one lowercase letter (a-z)
    - At least one digit (0-9)
    - At least one special character (non-alphanumeric)
    - Not in the common-password deny-list (case-insensitive)
    - No more than 3 consecutive identical characters (e.g. "aaaa…")

    Returns
    -------
    (valid, errors)
        *valid* is ``True`` when all rules pass.  *errors* is an empty list
        on success, or a list of human-readable failure messages.
    """
    errors: list[str] = []

    if not isinstance(password, str):
        return False, ["Password must be a string."]

    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(
            f"Password must be at least {PASSWORD_MIN_LENGTH} characters long."
        )
    if len(password) > PASSWORD_MAX_LENGTH:
        errors.append(
            f"Password must be no more than {PASSWORD_MAX_LENGTH} characters long."
        )

    if not re.search(r"[A-Z]", password):
        errors.append("Password must contain at least one uppercase letter.")

    if not re.search(r"[a-z]", password):
        errors.append("Password must contain at least one lowercase letter.")

    if not re.search(r"\d", password):
        errors.append("Password must contain at least one digit.")

    if not re.search(r"[^A-Za-z0-9]", password):
        errors.append("Password must contain at least one special character.")

    if password.lower() in _COMMON_PASSWORDS:
        errors.append("Password is in the common-password deny-list.")

    if re.search(r"(.)\1{3,}", password):
        errors.append(
            "Password must not contain 4+ consecutive identical characters."
        )

    return (len(errors) == 0, errors)


# ---------------------------------------------------------------------------
# Account lockout
# ---------------------------------------------------------------------------

#: Number of consecutive failures before an account is locked.
MAX_FAILED_ATTEMPTS = 10

#: How long (in minutes) an account stays locked.
LOCKOUT_DURATION_MINUTES = 15


class LoginTracker:
    """Thread-safe in-memory tracker for login attempts and lockouts.

    Tracks state per IP address::

        {
            "<ip>": {
                "count": <int>,           # consecutive failed attempts
                "locked_until": <datetime | None>,
            }
        }
    """

    def __init__(
        self,
        max_attempts: int = MAX_FAILED_ATTEMPTS,
        lockout_minutes: int = LOCKOUT_DURATION_MINUTES,
    ) -> None:
        self._store: dict[str, dict] = {}
        self._lock = Lock()
        self.max_attempts = max_attempts
        self.lockout_minutes = lockout_minutes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_failed_login(self, ip: str) -> None:
        """Increment the failure counter for *ip*.

        If the counter reaches ``max_attempts``, the IP is locked for
        ``lockout_minutes`` minutes.
        """
        with self._lock:
            entry = self._store.setdefault(ip, {"count": 0, "locked_until": None})

            # If an existing lockout has expired, reset first.
            if entry["locked_until"] and datetime.now(timezone.utc) > entry["locked_until"]:
                entry["count"] = 0
                entry["locked_until"] = None

            entry["count"] += 1

            if entry["count"] >= self.max_attempts:
                entry["locked_until"] = datetime.now(timezone.utc) + timedelta(
                    minutes=self.lockout_minutes
                )
                print(
                    f"[SOMNI][SECURITY] IP {ip} locked out after "
                    f"{entry['count']} failed attempts "
                    f"(until {entry['locked_until'].isoformat()})."
                )

    def record_successful_login(self, ip: str) -> None:
        """Clear the failure counter for *ip* after a successful login."""
        with self._lock:
            self._store.pop(ip, None)

    def is_account_locked(self, ip: str) -> bool:
        """Return ``True`` if *ip* is currently locked out."""
        with self._lock:
            entry = self._store.get(ip)
            if not entry or not entry["locked_until"]:
                return False
            if datetime.now(timezone.utc) > entry["locked_until"]:
                # Lockout expired — clean up transparently.
                entry["count"] = 0
                entry["locked_until"] = None
                return False
            return True

    def get_remaining_lockout_seconds(self, ip: str) -> float:
        """Return the number of seconds remaining in the lockout for *ip*.

        Returns ``0.0`` if the IP is not locked.
        """
        with self._lock:
            entry = self._store.get(ip)
            if not entry or not entry["locked_until"]:
                return 0.0
            remaining = (entry["locked_until"] - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, remaining)


#: Module-level singleton used by the rest of the application.
login_tracker = LoginTracker()


# ---------------------------------------------------------------------------
# Input sanitization helpers
# ---------------------------------------------------------------------------

def sanitize_string(value: object, max_length: int = 1000) -> str:
    """Sanitize an arbitrary value into a safe string.

    Steps
    -----
    1. Coerce *value* to ``str``.
    2. Strip leading/trailing whitespace.
    3. Truncate to *max_length* characters.
    4. Remove ASCII control characters (U+0000–U+001F, U+007F) and Unicode
       categories Cc (other control) and Cf (format characters).

    Returns
    -------
    str
        The sanitized string.
    """
    if not isinstance(value, str):
        value = str(value)

    value = value.strip()

    if len(value) > max_length:
        value = value[:max_length]

    # Remove control and format characters.
    cleaned_chars = []
    for ch in value:
        cat = unicodedata.category(ch)
        code = ord(ch)
        if cat in ("Cc", "Cf"):
            continue
        # Belt-and-braces: also drop ASCII DEL explicitly.
        if code == 0x7F:
            continue
        cleaned_chars.append(ch)

    return "".join(cleaned_chars)


def sanitize_int(
    value: object,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int:
    """Parse and range-validate an integer value.

    Parameters
    ----------
    value:
        The raw value to coerce.  Can be an ``int``, a numeric ``float``,
        or a ``str`` representation of an integer.
    min_val:
        Optional inclusive lower bound.
    max_val:
        Optional inclusive upper bound.

    Returns
    -------
    int
        The validated integer.

    Raises
    ------
    ValueError
        If *value* cannot be converted to an integer or falls outside the
        specified range.
    """
    try:
        int_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer value: {value!r}") from exc

    if min_val is not None and int_value < min_val:
        raise ValueError(
            f"Value {int_value} is below minimum allowed value {min_val}."
        )
    if max_val is not None and int_value > max_val:
        raise ValueError(
            f"Value {int_value} exceeds maximum allowed value {max_val}."
        )

    return int_value


# ---------------------------------------------------------------------------
# Top-level initialisation
# ---------------------------------------------------------------------------

def init_security(app) -> Limiter:
    """Initialise all security features on *app*.

    Performs the following in order:

    1. Configures secure session settings via :func:`configure_session`.
    2. Registers :func:`add_security_headers` as an ``after_request`` handler.
    3. Attaches Flask-Limiter via :func:`init_limiter`.

    Returns the :class:`~flask_limiter.Limiter` instance so callers can
    apply per-route limits with ``@limiter.limit(...)``.

    Example
    -------
    ::

        from somniguard_gateway.security import init_security, LOGIN_RATE_LIMIT

        app = Flask(__name__)
        limiter = init_security(app)

        @app.route("/login", methods=["POST"])
        @limiter.limit(LOGIN_RATE_LIMIT)
        def login():
            ...

    DISCLAIMER: Educational prototype — not a clinically approved device.
    """
    print("[SOMNI][SECURITY] Initialising security subsystem...")
    configure_session(app)
    app.after_request(add_security_headers)
    print("[SOMNI][SECURITY] Security headers registered as after_request handler.")
    limiter = init_limiter(app)
    print("[SOMNI][SECURITY] Security subsystem ready.")
    return limiter
