#!/usr/bin/env python3
"""
SOMNI-Guard Pico Firmware Encryption Tool v1.0

Encrypts all Python source files destined for the Pico 2W using AES-256-CBC
with a key derived from the target device's hardware unique ID and a random
salt.  The encrypted ``.enc`` files can only be decrypted on the specific Pico
board whose unique ID was provided.

EDUCATIONAL PROTOTYPE DISCLAIMER:
This script is part of an educational prototype and is provided for
demonstration and learning purposes only.  It is not intended for use in
production or safety-critical environments.

Usage:
    python scripts/encrypt_pico_files.py \\
        --uid E660C0D1C7921E28 \\
        --src somniguard_pico/ \\
        --out encrypted_deploy/

    python scripts/encrypt_pico_files.py \\
        --uid E660C0D1C7921E28 \\
        --src somniguard_pico/ \\
        --out encrypted_deploy/ \\
        --dev-mode

Key derivation (must match crypto_loader.py on the Pico):
    key = SHA-256(unique_id_bytes + salt_bytes)   →  32 bytes (AES-256)

File format of .enc files:
    [16-byte random IV][AES-256-CBC ciphertext with PKCS7 padding]
"""

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional: use the 'cryptography' library for AES if available, otherwise
# fall back to a pure-Python implementation.
# ---------------------------------------------------------------------------
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as crypto_padding
    _USE_CRYPTOGRAPHY_LIB = True
except ImportError:
    _USE_CRYPTOGRAPHY_LIB = False

_LOG_PREFIX = "[SOMNI][ENCRYPT]"

# Files that must remain as plaintext on the Pico (bootstrap files).
# boot.py and main.py run before encrypted modules are available, so they
# cannot be stored as .enc files.  crypto_loader.py decrypts all others.
_PLAINTEXT_FILES = {"main.py", "crypto_loader.py", "boot.py", "_boot.py"}

# AES constants
_AES_BLOCK_SIZE = 16
_KEY_SIZE = 32
_SALT_SIZE = 16


# ---------------------------------------------------------------------------
# Key derivation (must match crypto_loader.py)
# ---------------------------------------------------------------------------

def derive_key(uid_bytes, salt_bytes):
    """Derive the AES-256 key from a Pico's unique ID and salt.

    This MUST produce the same key as ``crypto_loader._derive_key()`` on the
    Pico.  The formula is: SHA-256(uid_bytes || salt_bytes) → 32 bytes.

    Args:
        uid_bytes (bytes): Raw unique ID bytes from the Pico (typically 8 bytes).
        salt_bytes (bytes): Random salt bytes from ``_salt.bin``.

    Returns:
        bytes: 32-byte AES-256 key.
    """
    h = hashlib.sha256(uid_bytes + salt_bytes)
    return h.digest()


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------

def pkcs7_pad(data, block_size=_AES_BLOCK_SIZE):
    """Apply PKCS7 padding to align data to the AES block size.

    If the data is already aligned, a full block of padding is appended.

    Args:
        data (bytes): Plaintext data.
        block_size (int): Block size (default 16 for AES).

    Returns:
        bytes: Padded data whose length is a multiple of block_size.
    """
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def encrypt_file_content(plaintext_bytes, key):
    """Encrypt plaintext bytes with AES-256-CBC and a random IV.

    Returns the encrypted payload in the format expected by crypto_loader.py:
    ``[16-byte IV][PKCS7-padded ciphertext]``

    Args:
        plaintext_bytes (bytes): Raw plaintext to encrypt.
        key (bytes): 32-byte AES-256 key.

    Returns:
        bytes: IV + ciphertext.
    """
    iv = os.urandom(_AES_BLOCK_SIZE)

    if _USE_CRYPTOGRAPHY_LIB:
        # Use the cryptography library (preferred)
        padder = crypto_padding.PKCS7(_AES_BLOCK_SIZE * 8).padder()
        padded = padder.update(plaintext_bytes) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
    else:
        # Pure-Python fallback using PyCryptodome or manual PKCS7
        try:
            from Crypto.Cipher import AES as PyCryptoAES
            padded = pkcs7_pad(plaintext_bytes)
            cipher = PyCryptoAES.new(key, PyCryptoAES.MODE_CBC, iv)
            ciphertext = cipher.encrypt(padded)
        except ImportError:
            print("{} ERROR: Neither 'cryptography' nor 'pycryptodome' "
                  "is installed.".format(_LOG_PREFIX))
            print("{} Install one of them:".format(_LOG_PREFIX))
            print("{}   pip install cryptography".format(_LOG_PREFIX))
            print("{}   pip install pycryptodome".format(_LOG_PREFIX))
            sys.exit(1)

    return iv + ciphertext


def decrypt_file_content(encrypted_bytes, key):
    """Decrypt a .enc payload (IV + ciphertext) to verify round-trip correctness.

    Args:
        encrypted_bytes (bytes): IV + ciphertext.
        key (bytes): 32-byte AES-256 key.

    Returns:
        bytes: Decrypted plaintext.
    """
    iv = encrypted_bytes[:_AES_BLOCK_SIZE]
    ciphertext = encrypted_bytes[_AES_BLOCK_SIZE:]

    if _USE_CRYPTOGRAPHY_LIB:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = crypto_padding.PKCS7(_AES_BLOCK_SIZE * 8).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
    else:
        from Crypto.Cipher import AES as PyCryptoAES
        cipher = PyCryptoAES.new(key, PyCryptoAES.MODE_CBC, iv)
        padded = cipher.decrypt(ciphertext)
        pad_len = padded[-1]
        plaintext = padded[:-pad_len]

    return plaintext


# ---------------------------------------------------------------------------
# UID parsing
# ---------------------------------------------------------------------------

def parse_uid(uid_str):
    """Parse a Pico unique ID from a hex string.

    Accepts hex strings with or without ``0x`` prefix, spaces, or colons.
    The Pico 2W's ``machine.unique_id()`` returns 8 bytes.

    Args:
        uid_str (str): Hex-encoded unique ID (e.g. ``E660C0D1C7921E28``).

    Returns:
        bytes: Raw UID bytes.

    Raises:
        ValueError: If the hex string is invalid.
    """
    cleaned = uid_str.strip().replace("0x", "").replace(":", "").replace(" ", "")
    try:
        uid_bytes = bytes.fromhex(cleaned)
    except ValueError:
        raise ValueError(
            "Invalid UID hex string: '{}'. Expected a hex string like "
            "'E660C0D1C7921E28'.".format(uid_str)
        )
    return uid_bytes


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_py_files(src_dir):
    """Find all .py files in the source directory that should be encrypted.

    Excludes files in _PLAINTEXT_FILES (main.py, crypto_loader.py, _boot.py)
    and test files.

    Args:
        src_dir (Path): Source directory (e.g. somniguard_pico/).

    Returns:
        list[Path]: Sorted list of .py file paths relative to src_dir.
    """
    src_dir = Path(src_dir)
    py_files = []

    for py_file in sorted(src_dir.rglob("*.py")):
        rel_path = py_file.relative_to(src_dir)
        name = rel_path.name

        # Skip plaintext bootstrap files
        if name in _PLAINTEXT_FILES:
            continue

        # Skip test files
        if name.startswith("test") or name == "tests.py":
            continue

        # Skip __pycache__
        if "__pycache__" in str(rel_path):
            continue

        py_files.append(rel_path)

    return py_files


# ---------------------------------------------------------------------------
# Main encryption workflow
# ---------------------------------------------------------------------------

def encrypt_pico_files(uid_str, src_dir, out_dir, dev_mode=False,
                       salt_file=None):
    """Encrypt all Pico firmware files for deployment.

    Args:
        uid_str (str): Pico unique ID as a hex string.
        src_dir (str | Path): Source directory containing .py files.
        out_dir (str | Path): Output directory for encrypted files.
        dev_mode (bool): If True, copy .py files directly (no encryption).
        salt_file (str | Path | None): Path to existing _salt.bin.  If None
            or non-existent, a new salt is generated.

    Returns:
        dict: Summary with counts of encrypted, copied, and skipped files.
    """
    src_dir = Path(src_dir)
    out_dir = Path(out_dir)
    uid_bytes = parse_uid(uid_str)

    print("{} ========================================".format(_LOG_PREFIX))
    print("{} SOMNI-Guard Firmware Encryption Tool".format(_LOG_PREFIX))
    print("{} Educational prototype — not for clinical use.".format(_LOG_PREFIX))
    print("{} ========================================".format(_LOG_PREFIX))
    print("{} Source dir : {}".format(_LOG_PREFIX, src_dir))
    print("{} Output dir : {}".format(_LOG_PREFIX, out_dir))
    print("{} UID        : {} ({} bytes)".format(
        _LOG_PREFIX, uid_bytes.hex().upper(), len(uid_bytes)))
    print("{} Dev mode   : {}".format(_LOG_PREFIX, dev_mode))
    print("{} AES lib    : {}".format(
        _LOG_PREFIX,
        "cryptography" if _USE_CRYPTOGRAPHY_LIB else "pycryptodome/fallback"))

    if not src_dir.is_dir():
        print("{} ERROR: Source directory '{}' not found.".format(
            _LOG_PREFIX, src_dir))
        sys.exit(1)

    # Create output directory
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------
    # Step 1: Generate or load salt
    # ---------------------------------------------------------------
    salt_out_path = out_dir / "_salt.bin"
    if salt_file and Path(salt_file).exists():
        salt_bytes = Path(salt_file).read_bytes()
        print("{} Using existing salt from '{}'.".format(_LOG_PREFIX, salt_file))
    elif salt_out_path.exists():
        salt_bytes = salt_out_path.read_bytes()
        print("{} Using existing salt from '{}'.".format(
            _LOG_PREFIX, salt_out_path))
    else:
        salt_bytes = os.urandom(_SALT_SIZE)
        print("{} Generated new random salt ({} bytes).".format(
            _LOG_PREFIX, _SALT_SIZE))

    # Always write the salt to the output directory
    salt_out_path.write_bytes(salt_bytes)
    print("{} Salt written to '{}'.".format(_LOG_PREFIX, salt_out_path))

    # ---------------------------------------------------------------
    # Step 2: Derive key
    # ---------------------------------------------------------------
    key = derive_key(uid_bytes, salt_bytes)
    print("{} AES-256 key derived (SHA-256 of UID + salt).".format(_LOG_PREFIX))

    # ---------------------------------------------------------------
    # Step 3: Discover files
    # ---------------------------------------------------------------
    py_files = discover_py_files(src_dir)
    print("{} Found {} Python files to process.".format(
        _LOG_PREFIX, len(py_files)))

    stats = {"encrypted": 0, "copied_plaintext": 0, "skipped": 0, "errors": 0}

    # ---------------------------------------------------------------
    # Step 4: Encrypt (or copy) each file
    # ---------------------------------------------------------------
    for rel_path in py_files:
        src_file = src_dir / rel_path

        # Create subdirectories in output (e.g. drivers/)
        enc_rel = rel_path.with_suffix(".enc")
        enc_out = out_dir / enc_rel
        enc_out.parent.mkdir(parents=True, exist_ok=True)

        try:
            source_bytes = src_file.read_bytes()

            if dev_mode:
                # Dev mode: copy .py files directly
                py_out = out_dir / rel_path
                py_out.parent.mkdir(parents=True, exist_ok=True)
                py_out.write_bytes(source_bytes)
                print("{} COPY (dev) {} → {}".format(
                    _LOG_PREFIX, rel_path, py_out.name))
                stats["copied_plaintext"] += 1
            else:
                # Production: encrypt to .enc
                encrypted = encrypt_file_content(source_bytes, key)

                # Verify round-trip before writing
                decrypted = decrypt_file_content(encrypted, key)
                if decrypted != source_bytes:
                    print("{} ERROR: Round-trip verification failed for '{}'!".format(
                        _LOG_PREFIX, rel_path))
                    stats["errors"] += 1
                    continue

                enc_out.write_bytes(encrypted)
                print("{} ENC  {} → {} ({} → {} bytes)".format(
                    _LOG_PREFIX, rel_path, enc_rel,
                    len(source_bytes), len(encrypted)))
                stats["encrypted"] += 1

        except Exception as exc:
            print("{} ERROR processing '{}': {}".format(
                _LOG_PREFIX, rel_path, exc))
            stats["errors"] += 1

    # ---------------------------------------------------------------
    # Step 5: Copy plaintext bootstrap files
    # ---------------------------------------------------------------
    print("{} ----------------------------------------".format(_LOG_PREFIX))
    print("{} Copying plaintext bootstrap files...".format(_LOG_PREFIX))
    for name in sorted(_PLAINTEXT_FILES):
        src_file = src_dir / name
        if src_file.exists():
            dst_file = out_dir / name
            shutil.copy2(src_file, dst_file)
            print("{} COPY {} (plaintext bootstrap)".format(_LOG_PREFIX, name))
            stats["copied_plaintext"] += 1
        else:
            print("{} SKIP {} (not found in source)".format(_LOG_PREFIX, name))

    # Also copy manifest.json if it exists
    manifest_src = src_dir / "manifest.json"
    if manifest_src.exists():
        shutil.copy2(manifest_src, out_dir / "manifest.json")
        print("{} COPY manifest.json".format(_LOG_PREFIX))

    # ---------------------------------------------------------------
    # Step 6: Summary
    # ---------------------------------------------------------------
    print("{} ========================================".format(_LOG_PREFIX))
    print("{} Encryption complete.".format(_LOG_PREFIX))
    print("{} Encrypted   : {} files".format(_LOG_PREFIX, stats["encrypted"]))
    print("{} Plaintext   : {} files".format(
        _LOG_PREFIX, stats["copied_plaintext"]))
    print("{} Errors      : {} files".format(_LOG_PREFIX, stats["errors"]))
    print("{} ========================================".format(_LOG_PREFIX))

    if not dev_mode:
        print("")
        print("{} DEPLOYMENT INSTRUCTIONS:".format(_LOG_PREFIX))
        print("{} ----------------------------------------".format(_LOG_PREFIX))
        print("{} 1. Connect the target Pico 2W via USB.".format(_LOG_PREFIX))
        print("{} 2. Copy ALL files from '{}' to the Pico filesystem:".format(
            _LOG_PREFIX, out_dir))
        print("{}    mpremote cp -r {}/* :".format(_LOG_PREFIX, out_dir))
        print("{} 3. The Pico will boot and decrypt modules automatically.".format(
            _LOG_PREFIX))
        print("{} 4. Verify boot by monitoring the serial console:".format(
            _LOG_PREFIX))
        print("{}    mpremote connect /dev/ttyACM0 repl".format(_LOG_PREFIX))
        print("{} ----------------------------------------".format(_LOG_PREFIX))
        print("{} The encrypted files ONLY work on the Pico with UID: {}".format(
            _LOG_PREFIX, uid_bytes.hex().upper()))
        print("{} Copying them to a different Pico will cause decryption "
              "to fail.".format(_LOG_PREFIX))

    return stats


# ---------------------------------------------------------------------------
# UID reader helper
# ---------------------------------------------------------------------------

def print_uid_instructions():
    """Print instructions for reading a Pico's unique ID."""
    print("")
    print("How to read your Pico's unique ID:")
    print("-----------------------------------")
    print("1. Connect the Pico 2W via USB.")
    print("2. Open a MicroPython REPL (e.g. via Thonny or mpremote):")
    print("     mpremote connect /dev/ttyACM0 repl")
    print("3. Run this command in the REPL:")
    print("     import machine; print(machine.unique_id().hex())")
    print("4. Copy the hex string (e.g. 'e660c0d1c7921e28') and pass it")
    print("   as --uid to this script.")
    print("")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """CLI entry point for the Pico firmware encryption tool."""
    parser = argparse.ArgumentParser(
        description="SOMNI-Guard: Encrypt Pico 2W firmware files for deployment.",
        epilog="Educational prototype — not for clinical use.",
    )
    parser.add_argument(
        "--uid",
        required=True,
        help="Pico hardware unique ID as a hex string (e.g. E660C0D1C7921E28). "
             "Read it from the Pico REPL: "
             "import machine; print(machine.unique_id().hex())",
    )
    parser.add_argument(
        "--src",
        default="somniguard_pico/",
        help="Source directory containing .py files (default: somniguard_pico/)",
    )
    parser.add_argument(
        "--out",
        default="encrypted_deploy/",
        help="Output directory for encrypted files (default: encrypted_deploy/)",
    )
    parser.add_argument(
        "--salt",
        default=None,
        help="Path to an existing _salt.bin file. If not specified, a new "
             "salt is generated (or reused from the output directory).",
    )
    parser.add_argument(
        "--dev-mode",
        action="store_true",
        help="Development mode: copy .py files directly without encryption.",
    )
    parser.add_argument(
        "--show-uid-help",
        action="store_true",
        help="Print instructions for reading the Pico's unique ID and exit.",
    )

    args = parser.parse_args()

    if args.show_uid_help:
        print_uid_instructions()
        sys.exit(0)

    stats = encrypt_pico_files(
        uid_str=args.uid,
        src_dir=args.src,
        out_dir=args.out,
        dev_mode=args.dev_mode,
        salt_file=args.salt,
    )

    sys.exit(1 if stats["errors"] > 0 else 0)


if __name__ == "__main__":
    main()
