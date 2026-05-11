"""
secure_config.py — Encrypted configuration storage for SomniGuard on Raspberry Pi Pico 2W (RP2350).

Encrypts sensitive configuration data (HMAC keys, WiFi credentials) at rest on the Pico
filesystem using the XTEA cipher with a hardware-derived key from machine.unique_id().

Educational prototype — not a clinically approved device.

Design notes:
  - XTEA (eXtended Tiny Encryption Algorithm) is used as a MicroPython-compatible
    substitute for AES-256. It requires no external libraries and operates on 8-byte
    blocks with a 16-byte (128-bit) key.
  - The encryption key is derived from the Pico's hardware-unique ID via SHA-256,
    providing device-binding without storing the key on disk.
  - Encrypted config is stored as JSON with base64-encoded ciphertext.
  - PKCS7 padding is applied to align plaintext to 8-byte XTEA block boundaries.
  - CBC mode: each block is XOR-chained with the previous ciphertext block
    (Cipher Block Chaining).  A random 8-byte IV is prepended to the ciphertext
    so that identical plaintexts produce different ciphertexts on every call.
    File layout: [8-byte IV][PKCS7-padded XTEA-CBC ciphertext].

MicroPython compatibility:
  - Uses only ustruct, ujson, ubinascii, uhashlib, and machine (all MicroPython built-ins).
  - Falls back to CPython equivalents (struct, json, binascii, hashlib) for local testing.
  - Random IV generated via os.urandom(8) on both MicroPython and CPython.
"""

# ---------------------------------------------------------------------------
# Compatibility shim — MicroPython vs CPython
# ---------------------------------------------------------------------------
try:
    import machine            # MicroPython: hardware unique ID
    _MICROPYTHON = True
except ImportError:
    _MICROPYTHON = False      # Running on CPython for testing

try:
    import ustruct as struct
except ImportError:
    import struct

try:
    import ujson as json
except ImportError:
    import json

try:
    import ubinascii as binascii
except ImportError:
    import binascii

try:
    import uhashlib as hashlib
except ImportError:
    import hashlib

import os    # os.urandom() for random IV generation (available on both MicroPython and CPython)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_LOG_PREFIX = "[SOMNI][SECURE_CONFIG]"
_CONFIG_VERSION = 1
_XTEA_ROUNDS = 64
_XTEA_DELTA = 0x9E3779B9          # XTEA magic constant (fractional part of golden ratio)
_XTEA_SUM_INIT = 0xC6EF3720       # delta * 32 (used as initial sum for decryption)
_BLOCK_SIZE = 8                   # XTEA block size in bytes
_KEY_SIZE = 16                    # XTEA key size in bytes (four 32-bit words)
_MASK32 = 0xFFFFFFFF              # 32-bit overflow mask


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _derive_key(unique_id):
    """Derive a 16-byte XTEA key from the device's hardware unique ID using SHA-256.

    The full 32-byte SHA-256 digest is computed and the first 16 bytes are
    returned as the XTEA key.  This ties encryption to the specific hardware
    unit without storing any secret material on the filesystem.

    Args:
        unique_id (bytes): Raw bytes from machine.unique_id() (typically 8 bytes
                           on RP2350, but any length is accepted).

    Returns:
        bytearray: 16-byte derived key suitable for XTEA.
    """
    h = hashlib.sha256(unique_id)
    digest = h.digest()           # 32 bytes
    key = bytearray(digest[:_KEY_SIZE])
    print(_LOG_PREFIX, "Key derived from hardware unique ID (SHA-256, first 16 bytes).")
    return key


def _xtea_encrypt_block(block, key):
    """Encrypt a single 8-byte block with XTEA using 64 rounds.

    Implements the standard XTEA algorithm as described in the original
    Wheeler & Needham paper.  Operates on two 32-bit unsigned integers
    (v0, v1) derived from the 8-byte input block, using a 128-bit key
    split into four 32-bit words (k0–k3).

    Args:
        block (bytes | bytearray): Exactly 8 bytes of plaintext.
        key   (bytes | bytearray): Exactly 16 bytes (four 32-bit words).

    Returns:
        bytes: 8 bytes of ciphertext.

    Raises:
        ValueError: If block is not 8 bytes or key is not 16 bytes.
    """
    if len(block) != _BLOCK_SIZE:
        raise ValueError("XTEA block must be 8 bytes, got {}".format(len(block)))
    if len(key) != _KEY_SIZE:
        raise ValueError("XTEA key must be 16 bytes, got {}".format(len(key)))

    v0, v1 = struct.unpack(">II", block)
    k = struct.unpack(">IIII", key)

    s = 0
    for _ in range(_XTEA_ROUNDS):
        v0 = (v0 + (((v1 << 4 ^ v1 >> 5) + v1) ^ (s + k[s & 3]))) & _MASK32
        s = (s + _XTEA_DELTA) & _MASK32
        v1 = (v1 + (((v0 << 4 ^ v0 >> 5) + v0) ^ (s + k[(s >> 11) & 3]))) & _MASK32

    return struct.pack(">II", v0, v1)


def _xtea_decrypt_block(block, key):
    """Decrypt a single 8-byte block with XTEA using 64 rounds.

    Reverses the XTEA encryption process.  The initial sum value is
    _XTEA_DELTA * _XTEA_ROUNDS (i.e., 0x9E3779B9 * 64 mod 2^32 = 0xC6EF3720).

    Args:
        block (bytes | bytearray): Exactly 8 bytes of ciphertext.
        key   (bytes | bytearray): Exactly 16 bytes (four 32-bit words).

    Returns:
        bytes: 8 bytes of plaintext.

    Raises:
        ValueError: If block is not 8 bytes or key is not 16 bytes.
    """
    if len(block) != _BLOCK_SIZE:
        raise ValueError("XTEA block must be 8 bytes, got {}".format(len(block)))
    if len(key) != _KEY_SIZE:
        raise ValueError("XTEA key must be 16 bytes, got {}".format(len(key)))

    v0, v1 = struct.unpack(">II", block)
    k = struct.unpack(">IIII", key)

    s = _XTEA_SUM_INIT
    for _ in range(_XTEA_ROUNDS):
        v1 = (v1 - (((v0 << 4 ^ v0 >> 5) + v0) ^ (s + k[(s >> 11) & 3]))) & _MASK32
        s = (s - _XTEA_DELTA) & _MASK32
        v0 = (v0 - (((v1 << 4 ^ v1 >> 5) + v1) ^ (s + k[s & 3]))) & _MASK32

    return struct.pack(">II", v0, v1)


def _pad(data):
    """Apply PKCS7 padding to align data to an 8-byte boundary.

    PKCS7 padding appends N bytes each with value N, where N is the number
    of bytes required to reach the next block boundary.  N is always in the
    range [1, 8] so that padding can always be unambiguously removed.

    Args:
        data (bytes | bytearray): Input data of any length.

    Returns:
        bytes: Padded data whose length is a multiple of 8.
    """
    n = _BLOCK_SIZE - (len(data) % _BLOCK_SIZE)
    return bytes(data) + bytes([n] * n)


def _unpad(data):
    """Remove PKCS7 padding from data.

    Reads the value of the last byte to determine how many padding bytes
    to remove, then validates that all padding bytes have the correct value.

    Args:
        data (bytes | bytearray): Padded data whose length is a multiple of 8.

    Returns:
        bytes: Original data with padding removed.

    Raises:
        ValueError: If the padding is malformed or the data length is invalid.
    """
    if not data or len(data) % _BLOCK_SIZE != 0:
        raise ValueError("Invalid padded data length: {}".format(len(data)))
    n = data[-1]
    if n < 1 or n > _BLOCK_SIZE:
        raise ValueError("Invalid PKCS7 padding byte value: {}".format(n))
    for byte in data[-n:]:
        if byte != n:
            raise ValueError("PKCS7 padding validation failed.")
    return bytes(data[:-n])


# ---------------------------------------------------------------------------
# Public encryption / decryption API
# ---------------------------------------------------------------------------

def encrypt_config(config_dict):
    """Encrypt a configuration dictionary and return IV + CBC ciphertext as bytes.

    Uses XTEA-CBC mode: each plaintext block is XOR-chained with the previous
    ciphertext block before encryption (Cipher Block Chaining).  A random 8-byte
    IV is generated for each call and prepended to the ciphertext so that
    identical plaintexts produce different ciphertexts.

    File layout: ``[8-byte random IV][PKCS7-padded XTEA-CBC ciphertext]``

    Args:
        config_dict (dict): Arbitrary JSON-serialisable configuration mapping.

    Returns:
        bytes: IV (8 bytes) + XTEA-CBC ciphertext. Total length is 8 + N*8.
    """
    print(_LOG_PREFIX, "Encrypting configuration dictionary ({} keys, CBC mode).".format(len(config_dict)))
    key = get_hardware_key()
    try:
        plaintext = json.dumps(config_dict).encode("utf-8")
        padded = _pad(plaintext)
        iv = os.urandom(_BLOCK_SIZE)            # random 8-byte IV
        ciphertext = bytearray()
        prev_block = iv                         # CBC chaining starts with IV
        for i in range(0, len(padded), _BLOCK_SIZE):
            block = padded[i:i + _BLOCK_SIZE]
            # XOR plaintext block with previous ciphertext block (CBC)
            xored = bytes(a ^ b for a, b in zip(block, prev_block))
            enc_block = _xtea_encrypt_block(xored, key)
            ciphertext += enc_block
            prev_block = enc_block              # next block chains from this one
        result = bytes(iv) + bytes(ciphertext)
        print(_LOG_PREFIX, "Encryption complete. Total length: {} bytes (8 IV + {} cipher).".format(
            len(result), len(ciphertext)))
        return result
    finally:
        wipe_bytes(key)


def decrypt_config(encrypted_bytes):
    """Decrypt XTEA-CBC bytes (IV + ciphertext) back to a configuration dictionary.

    The first 8 bytes are the IV used for CBC unchaining.  Each ciphertext block
    is decrypted then XOR-unchained with the previous ciphertext block.

    Args:
        encrypted_bytes (bytes | bytearray): Payload produced by encrypt_config()
                                             (format: [8-byte IV][ciphertext]).

    Returns:
        dict: Decrypted configuration dictionary.

    Raises:
        ValueError: If payload is too short, not block-aligned, or padding is invalid.
        Exception:  If the decrypted data is not valid JSON.
    """
    print(_LOG_PREFIX, "Decrypting configuration ({} bytes, CBC mode).".format(len(encrypted_bytes)))
    if len(encrypted_bytes) < _BLOCK_SIZE * 2:
        raise ValueError(
            "Payload too short: expected at least {} bytes, got {}.".format(
                _BLOCK_SIZE * 2, len(encrypted_bytes))
        )
    iv = bytes(encrypted_bytes[:_BLOCK_SIZE])
    ciphertext = bytes(encrypted_bytes[_BLOCK_SIZE:])
    if len(ciphertext) % _BLOCK_SIZE != 0:
        raise ValueError(
            "Ciphertext length must be a multiple of {}, got {}.".format(
                _BLOCK_SIZE, len(ciphertext))
        )
    key = get_hardware_key()
    try:
        plaintext_padded = bytearray()
        prev_block = iv                         # CBC unchaining starts with IV
        for i in range(0, len(ciphertext), _BLOCK_SIZE):
            block = ciphertext[i:i + _BLOCK_SIZE]
            dec_block = _xtea_decrypt_block(block, key)
            # XOR decrypted block with previous ciphertext block (CBC unchain)
            plain_block = bytes(a ^ b for a, b in zip(dec_block, prev_block))
            plaintext_padded += plain_block
            prev_block = block                  # next block chains from this ciphertext block
        plaintext = _unpad(bytes(plaintext_padded))
        config_dict = json.loads(plaintext.decode("utf-8"))
        print(_LOG_PREFIX, "Decryption complete. Config keys: {}.".format(list(config_dict.keys())))
        return config_dict
    finally:
        wipe_bytes(key)


# ---------------------------------------------------------------------------
# Filesystem persistence
# ---------------------------------------------------------------------------

def save_secure_config(config_dict, filepath):
    """Encrypt a configuration dictionary and save it to a JSON file on disk.

    The encrypted bytes are base64-encoded and stored in a JSON envelope that
    also records a format version number for future migration support.

    File layout (JSON):
        {
            "version": 1,
            "data": "<base64-encoded XTEA ciphertext>"
        }

    Args:
        config_dict (dict): Configuration data to encrypt and persist.
        filepath    (str):  Destination file path on the Pico filesystem
                            (e.g. "/secure_config.json").

    Returns:
        None

    Raises:
        OSError: If the file cannot be written (e.g. filesystem is read-only).
    """
    print(_LOG_PREFIX, "Saving encrypted config to '{}'.".format(filepath))
    ciphertext = encrypt_config(config_dict)
    b64 = binascii.b2a_base64(ciphertext).decode("utf-8").strip()
    envelope = {
        "version": _CONFIG_VERSION,
        "data": b64,
    }
    with open(filepath, "w") as f:
        f.write(json.dumps(envelope))
    print(_LOG_PREFIX, "Secure config saved successfully.")


def load_secure_config(filepath):
    """Load and decrypt an encrypted configuration file from disk.

    Reads the JSON envelope written by save_secure_config(), base64-decodes
    the ciphertext, and decrypts it back to a plain Python dictionary.

    Args:
        filepath (str): Path to the encrypted config file on the Pico filesystem.

    Returns:
        dict: Decrypted configuration dictionary.

    Raises:
        OSError:    If the file does not exist or cannot be read.
        ValueError: If the file format version is unsupported or data is corrupt.
        Exception:  If decryption or JSON parsing fails.
    """
    print(_LOG_PREFIX, "Loading encrypted config from '{}'.".format(filepath))
    with open(filepath, "r") as f:
        raw = f.read()
    envelope = json.loads(raw)
    version = envelope.get("version", None)
    if version != _CONFIG_VERSION:
        raise ValueError(
            "Unsupported config file version: {}. Expected {}.".format(version, _CONFIG_VERSION)
        )
    b64 = envelope["data"]
    ciphertext = binascii.a2b_base64(b64)
    config_dict = decrypt_config(ciphertext)
    print(_LOG_PREFIX, "Secure config loaded successfully.")
    return config_dict


# ---------------------------------------------------------------------------
# Security utilities
# ---------------------------------------------------------------------------

def wipe_bytes(ba):
    """Securely zero out a bytearray in memory to limit secret exposure.

    Overwrites every byte with 0x00.  This reduces (but cannot guarantee
    elimination of) residual key material in RAM after use.  On MicroPython
    there is no guarantee that the GC has not already moved the object, but
    this is a best-effort precaution.

    Args:
        ba (bytearray): The buffer to zero.  Must be a mutable bytearray;
                        bytes objects are silently ignored.

    Returns:
        None
    """
    if not isinstance(ba, bytearray):
        return
    for i in range(len(ba)):
        ba[i] = 0


def get_hardware_key():
    """Retrieve the XTEA encryption key derived from the device's hardware unique ID.

    On a real Raspberry Pi Pico 2W this calls machine.unique_id() to obtain the
    factory-programmed 8-byte unique identifier and feeds it through SHA-256.

    On CPython (for unit testing) a fixed synthetic unique ID is used so that
    tests are deterministic.  Do NOT use the CPython fallback in production.

    Returns:
        bytearray: 16-byte derived XTEA key.
    """
    if _MICROPYTHON:
        uid = machine.unique_id()
        print(_LOG_PREFIX, "Hardware unique ID obtained from machine.unique_id().")
    else:
        # CPython fallback for local testing — fixed synthetic UID
        uid = b"\xDE\xAD\xBE\xEF\xCA\xFE\xBA\xBE"
        print(_LOG_PREFIX, "WARNING: Using synthetic unique ID for CPython testing. "
              "Do NOT use in production.")
    return _derive_key(uid)
