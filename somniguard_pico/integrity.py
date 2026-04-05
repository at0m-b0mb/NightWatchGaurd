"""
integrity.py — SOMNI‑Guard Pico 2 W firmware integrity checker.

Computes SHA‑256 hashes of all firmware modules at boot, compares them
against a signed manifest file (manifest.json), and blocks execution if
any file has been tampered with.

Manifest format
---------------
The manifest is a JSON file with the following structure::

    {
        "version": 1,
        "generated_at": "2024-01-01T00:00:00Z",
        "files": {
            "main.py":   "sha256hexhash...",
            "config.py": "sha256hexhash...",
            ...
        },
        "signature": "hmac-sha256-hex-of-files-dict"
    }

The ``signature`` field is an HMAC‑SHA256 hex digest computed over the
canonical JSON serialisation of the ``files`` dict (keys sorted, no extra
whitespace).  This prevents an attacker from modifying the manifest itself
without knowing the shared HMAC key.

Security model
--------------
- File hashes detect tampering with individual firmware files.
- The manifest HMAC prevents an attacker from updating the manifest to
  match tampered files (requires knowledge of the shared key).
- The HMAC key must be provisioned securely (e.g. stored in a protected
  area of flash or derived from a hardware secret).
- On tamper detection the module raises SystemExit so the calling code
  can decide whether to halt or alert.

MicroPython compatibility
-------------------------
- Uses only ``hashlib`` and ``json`` (``ujson`` on MicroPython).
- No third‑party libraries required.
- All I/O is wrapped in try/except for robustness.

Educational prototype — not a clinically approved device.
"""

import hashlib
import json

# ---------------------------------------------------------------------------
# Internal HMAC‑SHA256 (pure Python, MicroPython‑compatible)
# Identical implementation style to transport.py so the two modules stay
# in sync and are easy to audit together.
# ---------------------------------------------------------------------------


def _json_sorted(obj):
    """Serialise *obj* to JSON with dict keys in sorted order.

    MicroPython's ujson.dumps() does not support sort_keys=True.
    """
    if obj is None:
        return "null"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        return json.dumps(obj)
    if isinstance(obj, str):
        return json.dumps(obj)
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(_json_sorted(item) for item in obj) + "]"
    if isinstance(obj, dict):
        parts = []
        for k in sorted(obj.keys()):
            parts.append(json.dumps(str(k)) + ":" + _json_sorted(obj[k]))
        return "{" + ",".join(parts) + "}"
    return json.dumps(obj)


def _hmac_sha256(key, message):
    """
    Compute HMAC‑SHA256 using only hashlib (no hmac module required).

    Implements RFC 2104 HMAC using the hashlib SHA‑256 primitive.  This
    avoids any dependency on the ``hmac`` module which may not be present
    in all MicroPython builds.

    Args:
        key     (str | bytes): Shared secret key.
        message (str | bytes): Message to authenticate.

    Returns:
        str: Hex‑encoded HMAC‑SHA256 digest.
    """
    if isinstance(key, str):
        key = key.encode("utf-8")
    if isinstance(message, str):
        message = message.encode("utf-8")

    block_size = 64  # SHA‑256 block size in bytes

    # Keys longer than the block size are hashed first
    if len(key) > block_size:
        key = hashlib.sha256(key).digest()

    # Pad key to block_size
    key = key + b"\x00" * (block_size - len(key))

    o_key = bytes(b ^ 0x5C for b in key)
    i_key = bytes(b ^ 0x36 for b in key)

    inner = hashlib.sha256(i_key + message).digest()
    outer = hashlib.sha256(o_key + inner).digest()

    # Convert to hex string (no binascii needed)
    return "".join("{:02x}".format(b) for b in outer)


# ---------------------------------------------------------------------------
# File hashing
# ---------------------------------------------------------------------------

def _sha256_file(filepath):
    """
    Compute the SHA‑256 hex digest of a file.

    Reads the file in chunks so that large files do not exhaust the limited
    RAM available on the Pico 2 W.

    Args:
        filepath (str): Path to the file to hash.

    Returns:
        str: Lowercase hex‑encoded SHA‑256 digest.

    Raises:
        OSError: If the file cannot be opened or read.
    """
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(512)
            if not chunk:
                break
            h.update(chunk)
    digest = h.digest()
    return "".join("{:02x}".format(b) for b in digest)


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------

def load_manifest(manifest_path):
    """
    Load and parse the manifest JSON file.

    The manifest must conform to the schema described in this module's
    docstring.  Missing top‑level keys are not filled in; the caller is
    expected to validate the returned dict via ``verify_manifest_signature``
    before trusting its contents.

    Args:
        manifest_path (str): Filesystem path to manifest.json.

    Returns:
        dict: Parsed manifest dictionary.

    Raises:
        OSError:    If the file cannot be opened.
        ValueError: If the file does not contain valid JSON.
    """
    print("[SOMNI][INTEGRITY] Loading manifest: {}".format(manifest_path))
    with open(manifest_path, "r") as f:
        manifest = json.loads(f.read())
    print("[SOMNI][INTEGRITY] Manifest loaded (version={}, generated_at={}).".format(
        manifest.get("version", "?"),
        manifest.get("generated_at", "?"),
    ))
    return manifest


# ---------------------------------------------------------------------------
# Manifest signature verification
# ---------------------------------------------------------------------------

def verify_manifest_signature(manifest, hmac_key):
    """
    Verify the HMAC‑SHA256 signature embedded in the manifest.

    The expected signature is recomputed over the canonical JSON of the
    ``files`` dict (sorted keys, no whitespace) and compared against the
    ``signature`` field stored in the manifest.

    Args:
        manifest (dict): Parsed manifest dictionary from ``load_manifest``.
        hmac_key (str):  Shared HMAC secret used to sign the manifest.

    Returns:
        bool: True if the signature is valid, False otherwise.
    """
    stored_sig = manifest.get("signature", "")
    files_dict = manifest.get("files", {})

    # Canonical representation: sorted keys, no extra whitespace
    # MicroPython ujson.dumps() does not support sort_keys=True
    canonical = _json_sorted(files_dict)
    expected_sig = _hmac_sha256(hmac_key, canonical)

    valid = (stored_sig == expected_sig)
    if valid:
        print("[SOMNI][INTEGRITY] Manifest signature: VALID")
    else:
        print("[SOMNI][INTEGRITY] Manifest signature: INVALID — manifest may have been tampered with!")
    return valid


# ---------------------------------------------------------------------------
# Single‑file integrity verification
# ---------------------------------------------------------------------------

def verify_file_integrity(filepath, expected_hash):
    """
    Verify that a single file's SHA‑256 hash matches the expected value.

    Args:
        filepath      (str): Path to the file to verify.
        expected_hash (str): Expected lowercase SHA‑256 hex digest.

    Returns:
        bool: True if the computed hash matches expected_hash, False otherwise.
              Also returns False if the file cannot be read (treated as a
              tamper event — a missing file is a failed integrity check).
    """
    try:
        actual_hash = _sha256_file(filepath)
    except OSError as exc:
        print("[SOMNI][INTEGRITY] FAIL — cannot read '{}': {}".format(filepath, exc))
        return False

    if actual_hash == expected_hash:
        print("[SOMNI][INTEGRITY] OK   — {}".format(filepath))
        return True

    print("[SOMNI][INTEGRITY] FAIL — {} (expected={} got={})".format(
        filepath, expected_hash, actual_hash))
    return False


# ---------------------------------------------------------------------------
# Bulk file verification
# ---------------------------------------------------------------------------

def verify_all_files(manifest, base_path):
    """
    Verify all files listed in the manifest against their expected hashes.

    Iterates over every entry in ``manifest["files"]`` and calls
    ``verify_file_integrity`` for each one.  All files are checked even if
    an earlier file fails, so the caller receives a complete picture of
    which files have been tampered with.

    Args:
        manifest  (dict): Parsed and signature‑verified manifest dictionary.
        base_path (str):  Directory that contains the firmware files.
                          Each filename in ``manifest["files"]`` is resolved
                          relative to this path.

    Returns:
        dict: Mapping of filename → bool (True = OK, False = tampered/missing).
    """
    files_dict = manifest.get("files", {})
    results = {}

    if not files_dict:
        print("[SOMNI][INTEGRITY] Warning: manifest contains no files to verify.")

    for filename, expected_hash in files_dict.items():
        # Build an absolute‑ish path; MicroPython has no os.path.join in
        # all builds so we construct it manually.
        if base_path.endswith("/"):
            filepath = base_path + filename
        else:
            filepath = base_path + "/" + filename

        results[filename] = verify_file_integrity(filepath, expected_hash)

    passed = sum(1 for ok in results.values() if ok)
    total  = len(results)
    print("[SOMNI][INTEGRITY] File verification: {}/{} files passed.".format(passed, total))
    return results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_integrity_check(manifest_path, hmac_key, base_path):
    """
    Run the full firmware integrity check at boot.

    Steps performed:
    1. Load ``manifest_path`` and parse it as JSON.
    2. Verify the manifest's HMAC signature using ``hmac_key``.
    3. If the signature is invalid, fail immediately without checking files.
    4. Verify every file listed in the manifest against its expected hash.
    5. Return a summary tuple.

    If any check fails the function prints a clear tamper‑detected message
    with the ``[SOMNI][INTEGRITY]`` prefix.  It does NOT call ``sys.exit``
    itself — the caller (typically ``main.py``) is responsible for deciding
    whether to halt the device, enter a safe mode, or alert the user.

    Args:
        manifest_path (str): Filesystem path to manifest.json.
        hmac_key      (str): Shared HMAC secret used to sign the manifest.
        base_path     (str): Directory containing the firmware files listed
                             in the manifest (e.g. "/" or "/somniguard_pico").

    Returns:
        tuple[bool, dict]:
            - passed (bool): True only if the manifest signature is valid AND
              every listed file hash matches.
            - results (dict): Detailed results dictionary with the following
              structure::

                  {
                      "manifest_signature_valid": bool,
                      "files": {
                          "filename.py": bool,
                          ...
                      },
                  }

              ``results["files"]`` is empty when the manifest signature check
              fails (files are not checked in that case).
    """
    print("[SOMNI][INTEGRITY] ============================================")
    print("[SOMNI][INTEGRITY] Starting firmware integrity check …")
    print("[SOMNI][INTEGRITY] Manifest : {}".format(manifest_path))
    print("[SOMNI][INTEGRITY] Base path: {}".format(base_path))
    print("[SOMNI][INTEGRITY] ============================================")

    results = {
        "manifest_signature_valid": False,
        "files": {},
    }

    # ---------------------------------------------------------------
    # Step 1: Load manifest
    # ---------------------------------------------------------------
    try:
        manifest = load_manifest(manifest_path)
    except OSError as exc:
        print("[SOMNI][INTEGRITY] FAIL — cannot open manifest '{}': {}".format(
            manifest_path, exc))
        print("[SOMNI][INTEGRITY] Integrity check FAILED (manifest unreadable).")
        return False, results
    except ValueError as exc:
        print("[SOMNI][INTEGRITY] FAIL — manifest JSON parse error: {}".format(exc))
        print("[SOMNI][INTEGRITY] Integrity check FAILED (manifest corrupt).")
        return False, results

    # ---------------------------------------------------------------
    # Step 2: Verify manifest signature
    # ---------------------------------------------------------------
    sig_valid = verify_manifest_signature(manifest, hmac_key)
    results["manifest_signature_valid"] = sig_valid

    if not sig_valid:
        print("[SOMNI][INTEGRITY] FAIL — manifest signature invalid.")
        print("[SOMNI][INTEGRITY] Integrity check FAILED (manifest tampered or wrong key).")
        print("[SOMNI][INTEGRITY] *** TAMPER DETECTED — halting is recommended ***")
        return False, results

    # ---------------------------------------------------------------
    # Step 3: Verify all listed files
    # ---------------------------------------------------------------
    file_results = verify_all_files(manifest, base_path)
    results["files"] = file_results

    all_files_ok = all(file_results.values()) if file_results else True

    # ---------------------------------------------------------------
    # Step 4: Report overall result
    # ---------------------------------------------------------------
    passed = sig_valid and all_files_ok

    print("[SOMNI][INTEGRITY] ============================================")
    if passed:
        print("[SOMNI][INTEGRITY] Integrity check PASSED. All files verified.")
    else:
        failed_files = [f for f, ok in file_results.items() if not ok]
        print("[SOMNI][INTEGRITY] Integrity check FAILED.")
        if failed_files:
            print("[SOMNI][INTEGRITY] Tampered or missing files:")
            for fname in failed_files:
                print("[SOMNI][INTEGRITY]   - {}".format(fname))
        print("[SOMNI][INTEGRITY] *** TAMPER DETECTED — halting is recommended ***")
    print("[SOMNI][INTEGRITY] ============================================")

    return passed, results
