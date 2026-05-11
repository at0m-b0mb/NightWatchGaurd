"""
crypto_loader.py — SOMNI‑Guard encrypted firmware loader for Pico 2W (RP2350).

Runtime decryption engine that loads AES-256-CBC encrypted Python modules from
flash at boot.  This module must remain as **plaintext** on the Pico filesystem
so it can be imported directly by main.py before any encrypted modules are
available.

Encryption scheme
-----------------
- **Key derivation**: SHA-256(machine.unique_id() + salt) → 32 bytes (AES-256).
- **Salt**: Stored in ``_salt.bin`` on the Pico filesystem.  Without both the
  salt file AND the correct hardware chip, decryption fails.
- **File format**: Each ``.enc`` file is ``[16-byte IV][PKCS7-padded ciphertext]``.
- **Cipher**: AES-256-CBC via MicroPython's built-in ``ucryptolib`` module.

The hardware unique ID is factory-programmed into the RP2350 SoC and is
different for every chip.  This means encrypted ``.enc`` files extracted from
one Pico **cannot be decrypted on a different Pico** — the derived key will
not match.

Security model
--------------
- Protects all application source code and embedded credentials at rest.
- ``main.py`` and ``crypto_loader.py`` remain plaintext (contain no secrets).
- ``_salt.bin`` is useless without the specific chip's ``unique_id()``.
- For production, RP2350 secure boot (via the C SDK) would add a further
  hardware-rooted trust layer.

MicroPython compatibility
-------------------------
- ``ucryptolib`` (or ``cryptolib``) is included in standard MicroPython builds
  for the RP2350 (Pico 2W).  AES modes: 1 = ECB, 2 = CBC, 6 = CTR.
- Falls back to plaintext ``.py`` imports when ``.enc`` files are absent
  (development mode).

Educational prototype — not a clinically approved device.
"""

import hashlib
import sys

# ---------------------------------------------------------------------------
# Fake module class used to register decrypted modules in sys.modules
# ---------------------------------------------------------------------------

class _FakeModule:
    """Lightweight namespace object that acts as a MicroPython module.

    Used so that standard ``import X`` statements inside encrypted modules
    (executed via exec()) can find previously-loaded modules in sys.modules
    without requiring real ``.py`` files on the filesystem.
    """
    pass

# ---------------------------------------------------------------------------
# Compatibility shim — MicroPython vs CPython
# ---------------------------------------------------------------------------

try:
    import machine
    _MICROPYTHON = True
except ImportError:
    _MICROPYTHON = False

# ucryptolib / cryptolib — MicroPython built-in AES
_aes_cls = None
try:
    from ucryptolib import aes as _aes_cls  # type: ignore[import]
except ImportError:
    try:
        from cryptolib import aes as _aes_cls  # type: ignore[import]
    except ImportError:
        # No hardware AES available — will fall back to plaintext imports
        _aes_cls = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOG_PREFIX = "[SOMNI][CRYPTO]"
_AES_BLOCK_SIZE = 16        # AES block size in bytes
_AES_MODE_CBC = 2           # MicroPython ucryptolib mode constant for CBC
_KEY_SIZE = 32              # AES-256 = 32-byte key
_SALT_FILE = "_salt.bin"
_DEFAULT_SALT = b"SOMNI-Guard-Default-Salt-2026"

# Cache for the loaded modules so repeated imports don't re-decrypt
_module_cache = {}


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

def _derive_key():
    """Derive a 32-byte AES-256 key from the Pico's hardware unique ID and salt.

    Key = SHA-256(machine.unique_id() || salt)

    The salt is read from ``_salt.bin`` on the filesystem.  If the file is
    missing, a hard-coded default salt is used (suitable for development only).

    On CPython (testing), a synthetic unique ID is used.

    Returns:
        bytearray: 32-byte AES-256 key.
    """
    if _MICROPYTHON:
        uid = machine.unique_id()
    else:
        uid = b"\xDE\xAD\xBE\xEF\xCA\xFE\xBA\xBE"
        print(_LOG_PREFIX, "WARNING: Using synthetic UID for CPython testing.")

    try:
        with open(_SALT_FILE, "rb") as f:
            salt = f.read()
    except OSError:
        print(_LOG_PREFIX, "WARNING: _salt.bin not found — using default salt.")
        salt = _DEFAULT_SALT

    h = hashlib.sha256(uid + salt)
    key = bytearray(h.digest())  # 32 bytes
    return key


def _wipe_bytes(ba):
    """Zero out a bytearray to limit residual key material in RAM.

    Args:
        ba (bytearray): Buffer to wipe.
    """
    if isinstance(ba, bytearray):
        for i in range(len(ba)):
            ba[i] = 0


def _register_in_sys_modules(name, mod_dict):
    """Register a decrypted module in sys.modules under its dotted name.

    This is the critical step that allows standard ``import X`` statements
    inside encrypted modules (executed via exec()) to find already-loaded
    modules without needing ``.py`` files on the filesystem.

    For example, after ``config.enc`` is decrypted and registered, a later
    encrypted ``sampler.enc`` that contains ``import config`` at its top
    level will find ``config`` in sys.modules rather than trying (and
    failing) to open ``config.py`` from flash.

    For sub-packages (e.g. ``drivers/max30102``), the parent package
    (``drivers``) is created in sys.modules if it does not already exist,
    and the sub-module is set as an attribute on it so that
    ``from drivers import MAX30102`` also works.

    Args:
        name     (str):  Module name as used by import_encrypted()
                         (e.g. ``"config"``, ``"drivers/max30102"``).
        mod_dict (dict): Namespace dict produced by exec() of the decrypted
                         source.
    """
    dotted_name = name.replace("/", ".")

    # Build a lightweight module-like object from the namespace dict.
    mod_obj = _FakeModule()
    for k, v in mod_dict.items():
        setattr(mod_obj, k, v)

    # Register under the fully-qualified dotted name.
    sys.modules[dotted_name] = mod_obj

    # Also register under the simple leaf name so that
    # ``import config`` works even from inside a sub-package.
    leaf = dotted_name.split(".")[-1]
    if leaf != dotted_name and leaf not in sys.modules:
        sys.modules[leaf] = mod_obj

    # For sub-packages (e.g. ``drivers.max30102``), ensure the parent
    # package entry exists and has the sub-module as an attribute.
    parts = dotted_name.split(".")
    if len(parts) > 1:
        parent_name = ".".join(parts[:-1])
        if parent_name not in sys.modules:
            parent_obj = _FakeModule()
            sys.modules[parent_name] = parent_obj
        setattr(sys.modules[parent_name], parts[-1], mod_obj)

    print(_LOG_PREFIX, "Registered '{}' in sys.modules.".format(dotted_name))


# ---------------------------------------------------------------------------
# PKCS7 padding
# ---------------------------------------------------------------------------

def _pkcs7_unpad(data):
    """Remove PKCS7 padding from decrypted data.

    Args:
        data (bytes | bytearray): Decrypted data with PKCS7 padding.

    Returns:
        bytes: Original plaintext with padding removed.

    Raises:
        ValueError: If padding is invalid.
    """
    if not data or len(data) % _AES_BLOCK_SIZE != 0:
        raise ValueError("Invalid padded data length: {}".format(len(data)))
    pad_len = data[-1]
    if pad_len < 1 or pad_len > _AES_BLOCK_SIZE:
        raise ValueError("Invalid PKCS7 padding value: {}".format(pad_len))
    for i in range(pad_len):
        if data[-(i + 1)] != pad_len:
            raise ValueError("PKCS7 padding validation failed.")
    return bytes(data[:-pad_len])


# ---------------------------------------------------------------------------
# Decryption
# ---------------------------------------------------------------------------

def decrypt_module(name):
    """Decrypt a ``.enc`` file and return the Python source code as a string.

    File format: ``[16-byte IV][AES-256-CBC ciphertext with PKCS7 padding]``

    The decryption key is derived from the hardware unique ID + salt via
    ``_derive_key()``.  The key is wiped from memory after use.

    Args:
        name (str): Module name (without extension).  For sub-packages, use
                    ``/`` as the separator (e.g. ``drivers/max30102``).

    Returns:
        str: Decrypted Python source code (UTF-8).

    Raises:
        OSError:    If the ``.enc`` file cannot be read.
        ValueError: If decryption or padding validation fails.
        RuntimeError: If ucryptolib is not available.
    """
    if _aes_cls is None:
        raise RuntimeError("ucryptolib/cryptolib not available — "
                           "cannot decrypt .enc files.")

    path = name + ".enc"
    print(_LOG_PREFIX, "Decrypting '{}'...".format(path))

    with open(path, "rb") as f:
        iv = f.read(_AES_BLOCK_SIZE)
        ciphertext = f.read()

    if len(iv) != _AES_BLOCK_SIZE:
        raise ValueError("Invalid IV length in '{}': expected {}, got {}".format(
            path, _AES_BLOCK_SIZE, len(iv)))

    if len(ciphertext) == 0 or len(ciphertext) % _AES_BLOCK_SIZE != 0:
        raise ValueError("Invalid ciphertext length in '{}': {}".format(
            path, len(ciphertext)))

    key = _derive_key()
    try:
        cipher = _aes_cls(key, _AES_MODE_CBC, iv)
        plaintext_padded = cipher.decrypt(ciphertext)
        plaintext = _pkcs7_unpad(plaintext_padded)
        source = plaintext.decode("utf-8")
        print(_LOG_PREFIX, "Decrypted '{}' OK ({} bytes source).".format(
            path, len(source)))
        return source
    finally:
        _wipe_bytes(key)


# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

def import_encrypted(name, target_globals=None):
    """Import an encrypted module by name, decrypting it at runtime.

    If the ``.enc`` file exists, it is decrypted, compiled, and executed to
    produce a module namespace dict.  If the ``.enc`` file is missing, the
    function falls back to importing the plaintext ``.py`` file (development
    mode).

    Loaded modules are cached so that repeated calls for the same module name
    return the same dict without re-decryption.

    Args:
        name (str): Module name without extension.  For sub-packages use ``/``
                    as separator (e.g. ``drivers/max30102``).  The ``.enc``
                    file is looked up at ``<name>.enc`` relative to the
                    current working directory.
        target_globals (dict | None): If provided, the decrypted module's
                    namespace is merged into this dict (useful for
                    ``from X import *`` style usage).

    Returns:
        dict: The module's namespace dictionary.  Contains all top-level
              names defined in the module source.
    """
    # Check cache first
    if name in _module_cache:
        mod_dict = _module_cache[name]
        if target_globals is not None:
            target_globals.update(mod_dict)
        return mod_dict

    enc_path = name + ".enc"
    py_path = name + ".py"

    # Try encrypted file first
    source = None
    try:
        # Check if .enc file exists
        import os
        os.stat(enc_path)
        source = decrypt_module(name)
    except OSError:
        # .enc file not found — fall back to plaintext .py
        print(_LOG_PREFIX, "No '{}' found — falling back to '{}'.".format(
            enc_path, py_path))
    except Exception as exc:
        print(_LOG_PREFIX, "Decryption error for '{}': {}".format(
            enc_path, exc))
        print(_LOG_PREFIX, "Falling back to plaintext '{}'.".format(py_path))

    if source is not None:
        # Compile and execute the decrypted source.
        # Pre-populate the module namespace with already-cached modules so
        # that top-level ``import X`` statements inside the decrypted source
        # can resolve without needing plaintext .py files on the filesystem.
        display_name = name.replace("/", ".") + ".py"
        code = compile(source, display_name, "exec")
        mod_dict = {"__name__": name.replace("/", ".")}
        exec(code, mod_dict)
        _module_cache[name] = mod_dict
        # Register in sys.modules so subsequent encrypted modules can find
        # this module via standard 'import X' without needing .py files.
        _register_in_sys_modules(name, mod_dict)
        if target_globals is not None:
            target_globals.update(mod_dict)
        print(_LOG_PREFIX, "Loaded encrypted module '{}'.".format(name))
        return mod_dict

    # Fallback: standard Python import of the plaintext .py file
    # Convert path-style name (e.g. "drivers/max30102") to dotted module name
    dotted_name = name.replace("/", ".")
    try:
        mod = __import__(dotted_name)
        # For sub-module imports, navigate to the actual sub-module
        parts = dotted_name.split(".")
        for part in parts[1:]:
            mod = getattr(mod, part)
        mod_dict = {k: getattr(mod, k) for k in dir(mod) if not k.startswith("__")}
        mod_dict["__name__"] = dotted_name
        _module_cache[name] = mod_dict
        # Register in sys.modules for consistency with encrypted path.
        _register_in_sys_modules(name, mod_dict)
        if target_globals is not None:
            target_globals.update(mod_dict)
        print(_LOG_PREFIX, "Loaded plaintext module '{}' (dev mode).".format(
            dotted_name))
        return mod_dict
    except ImportError as exc:
        print(_LOG_PREFIX, "FATAL: Cannot import '{}': {}".format(
            dotted_name, exc))
        raise


def load_module_as_object(name):
    """Import an encrypted module and return it as a module-like object.

    This is a convenience wrapper around ``import_encrypted()`` that returns
    a SimpleNamespace-like object so callers can use attribute access
    (``mod.SOME_CONSTANT``) instead of dict access (``mod["SOME_CONSTANT"]``).

    Args:
        name (str): Module name (same format as ``import_encrypted``).

    Returns:
        object: A namespace object with all top-level module names as
                attributes.
    """
    mod_dict = import_encrypted(name)

    class _Module:
        pass

    obj = _Module()
    for k, v in mod_dict.items():
        if not k.startswith("__"):
            setattr(obj, k, v)
    # Preserve the module name for debugging
    obj.__name__ = name.replace("/", ".")
    return obj


# ---------------------------------------------------------------------------
# Bulk loader
# ---------------------------------------------------------------------------

# Modules that should be loaded as encrypted (in dependency order).
# main.py and crypto_loader.py are excluded — they must remain plaintext.
_ENCRYPTED_MODULES = [
    "config",
    "utils",
    "drivers/max30102",
    "drivers/adxl345",
    "drivers/gsr",
    "drivers/__init__",
    "transport",
    "sampler",
    "integrity",
    "secure_config",
]


def load_all():
    """Decrypt and load all application modules at boot.

    Iterates through ``_ENCRYPTED_MODULES`` in dependency order.  Each module
    is decrypted (if a ``.enc`` file is present) or loaded from plaintext
    (development fallback).

    Errors in individual modules are logged but do not halt the boot process
    (fail-soft).  The ``config`` module is loaded first because other modules
    depend on it.

    Returns:
        dict: Mapping of module name → namespace dict for all successfully
              loaded modules.
    """
    print(_LOG_PREFIX, "========================================")
    print(_LOG_PREFIX, "Loading encrypted firmware modules...")
    print(_LOG_PREFIX, "AES engine: {}".format(
        "ucryptolib" if _aes_cls is not None else "NOT AVAILABLE"))
    print(_LOG_PREFIX, "========================================")

    loaded = {}
    for name in _ENCRYPTED_MODULES:
        try:
            mod_dict = import_encrypted(name)
            loaded[name] = mod_dict
        except Exception as exc:
            print(_LOG_PREFIX, "WARN: Failed to load '{}': {}".format(name, exc))

    ok_count = len(loaded)
    total = len(_ENCRYPTED_MODULES)
    print(_LOG_PREFIX, "Loaded {}/{} modules.".format(ok_count, total))
    print(_LOG_PREFIX, "========================================")
    return loaded


def is_encryption_available():
    """Check whether the AES decryption engine is available.

    Returns:
        bool: True if ucryptolib/cryptolib is importable and .enc files
              can be decrypted.  False means the system will fall back to
              plaintext imports.
    """
    return _aes_cls is not None
