#!/usr/bin/env python3
"""
somni_uf2_tool.py — SOMNI-Guard Complete Firmware Packaging Tool

Combines three steps into one command:
  1. AES-256-CBC encrypt all Python source files (keyed to the Pico's hardware UID)
  2. Pack encrypted files into a LittleFS2 filesystem image
  3. Merge the image with the custom MicroPython UF2 → single flashable .uf2

Usage (run from the project root):
    python scripts/somni_uf2_tool.py \\
        --uid 2effff680e87ca96 \\
        --src scripts/somniguard_pico/ \\
        --firmware somni_guard_firmware.uf2 \\
        --out somni_guard_complete.uf2

Requirements:
    pip install littlefs-python cryptography

Flash the output:
    1. Hold BOOTSEL on the Pico 2W while plugging in USB.
    2. A drive called 'RP2350' appears on your Mac.
    3. Drag somni_guard_complete.uf2 onto that drive.
    4. Pico reboots — USB is gone, SOMNI-Guard firmware is running.

Recovery (always available):
    Hold BOOTSEL on power-on → ROM bootloader → drag any new .uf2 to reflash.

Educational prototype — not a clinically approved medical device.
"""

import argparse
import hashlib
import os
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

_MISSING = []

try:
    from littlefs import LittleFS as _LittleFS
except ImportError:
    _MISSING.append("littlefs-python")

_USE_CRYPTOGRAPHY = False
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as _crypto_padding
    _USE_CRYPTOGRAPHY = True
except ImportError:
    try:
        from Crypto.Cipher import AES as _PyCryptoAES  # noqa: F401
    except ImportError:
        _MISSING.append("cryptography  (pip install cryptography)")

if _MISSING:
    print("ERROR: Missing Python packages. Install them with:")
    for pkg in _MISSING:
        print("  pip install {}".format(pkg.split()[0]))
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOG = "[SOMNI][UF2]"

# UF2 block magic numbers (from the UF2 specification)
_UF2_MAGIC0      = 0x0A324655   # "UF2\n"
_UF2_MAGIC1      = 0x9E5D5157
_UF2_MAGIC_END   = 0x0AB16F30
_UF2_FLAG_FAMILY = 0x00002000   # familyID present flag
_RP2350_FAMILY   = 0xe48bff59   # RP2350 absolute-address family ID

# Pico 2W flash layout  (RP2350, 4 MB flash)
# These MUST match mpconfigboard.h in the custom build:
#   MICROPY_HW_FLASH_STORAGE_BYTES = PICO_FLASH_SIZE_BYTES - 1536 * 1024
_FLASH_BASE       = 0x10000000          # XIP base address (all Pico boards)
_FLASH_TOTAL      = 4 * 1024 * 1024     # 4 MB total flash on Pico 2W
_FW_RESERVED      = 1536 * 1024         # 1.5 MB reserved for MicroPython firmware
_FS_OFFSET        = _FW_RESERVED        # filesystem starts right after firmware
_FS_BASE_ADDR     = _FLASH_BASE + _FS_OFFSET   # 0x10180000
_FS_SIZE_BYTES    = _FLASH_TOTAL - _FW_RESERVED # 2,621,440 bytes (~2.5 MB)

# LittleFS2 parameters — must exactly match how MicroPython mounts the FS.
# Source: ports/rp2/rp2_flash.c + extmod/vfs_lfs.c (MicroPython master)
#   block_size  = FLASH_SECTOR_SIZE = 4096
#   read_size   = 32  (vfs_lfs default)
#   prog_size   = 32  (vfs_lfs default)
#   lookahead   = 32  (vfs_lfs default)
_LFS_BLOCK_SIZE   = 4096
_LFS_BLOCK_COUNT  = _FS_SIZE_BYTES // _LFS_BLOCK_SIZE   # 640
_LFS_READ_SIZE    = 32
_LFS_PROG_SIZE    = 32
_LFS_LOOKAHEAD    = 32

# Files that must stay as plaintext on the device (crypto_loader reads them
# before decryption is set up).
_PLAINTEXT = {"main.py", "crypto_loader.py", "boot.py", "_boot.py"}

# AES block size
_AES_BLOCK = 16

# ---------------------------------------------------------------------------
# Encryption  (identical algorithm to encrypt_pico_files.py)
# ---------------------------------------------------------------------------

def _parse_uid(uid_str: str) -> bytes:
    cleaned = uid_str.strip().replace("0x", "").replace(":", "").replace(" ", "")
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise ValueError("Bad UID hex string: '{}'".format(uid_str))


def _derive_key(uid_bytes: bytes, salt: bytes) -> bytes:
    return hashlib.sha256(uid_bytes + salt).digest()   # 32 bytes → AES-256


def _pkcs7_pad(data: bytes) -> bytes:
    pad = _AES_BLOCK - (len(data) % _AES_BLOCK)
    return data + bytes([pad] * pad)


def _aes_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-256-CBC encrypt; returns IV + ciphertext."""
    iv = os.urandom(_AES_BLOCK)
    if _USE_CRYPTOGRAPHY:
        padder = _crypto_padding.PKCS7(_AES_BLOCK * 8).padder()
        padded = padder.update(plaintext) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        enc    = cipher.encryptor()
        return iv + enc.update(padded) + enc.finalize()
    else:
        from Crypto.Cipher import AES as _AES
        padded = _pkcs7_pad(plaintext)
        return iv + _AES.new(key, _AES.MODE_CBC, iv).encrypt(padded)


def encrypt_sources(src_dir: Path, uid_bytes: bytes,
                    salt: bytes) -> dict:
    """
    Encrypt every .py file in src_dir (except bootstrap files).

    Returns dict:  { 'relative/path.enc': bytes, 'main.py': bytes, ... }
    Encrypted files use .enc extension; bootstrap files keep .py.
    """
    key = _derive_key(uid_bytes, salt)
    result = {}

    for py_file in sorted(src_dir.rglob("*.py")):
        rel  = py_file.relative_to(src_dir)
        name = rel.name

        if "__pycache__" in rel.parts:
            continue
        if name.startswith("test") or name == "tests.py":
            continue

        source = py_file.read_bytes()

        if name in _PLAINTEXT:
            result[str(rel)] = source
            print("{} PLAIN  {}".format(_LOG, rel))
        else:
            enc_rel = str(rel.with_suffix(".enc"))
            result[enc_rel] = _aes_encrypt(source, key)
            print("{} ENC    {} → {} ({} B → {} B)".format(
                _LOG, rel, enc_rel, len(source), len(result[enc_rel])))

    return result


# ---------------------------------------------------------------------------
# LittleFS2 image builder
# ---------------------------------------------------------------------------

def build_lfs_image(files: dict, salt: bytes) -> bytes:
    """
    Create a LittleFS2 binary image containing all files and _salt.bin.

    Parameters chosen to exactly match MicroPython's vfs_lfs mount call.
    """
    fs = _LittleFS(
        block_size    = _LFS_BLOCK_SIZE,
        block_count   = _LFS_BLOCK_COUNT,
        read_size     = _LFS_READ_SIZE,
        prog_size     = _LFS_PROG_SIZE,
        lookahead_size= _LFS_LOOKAHEAD,
    )

    # Create subdirectories (must exist before writing files into them)
    dirs_seen = set()
    for path_str in sorted(files):
        for parent in Path(path_str).parents:
            s = str(parent)
            if s not in (".", "") and s not in dirs_seen:
                try:
                    fs.mkdir("/" + s)
                except Exception:
                    pass   # already exists
                dirs_seen.add(s)

    # Write encrypted / plaintext files
    for path_str, data in sorted(files.items()):
        dest = "/" + path_str
        with fs.open(dest, "wb") as fh:
            fh.write(data)

    # Always embed the salt so the Pico can derive the decryption key
    with fs.open("/_salt.bin", "wb") as fh:
        fh.write(salt)
    print("{} _salt.bin written ({} B)".format(_LOG, len(salt)))

    image = bytes(fs.context.buffer)
    print("{} LittleFS2 image: {} bytes  ({} × {} B blocks)".format(
        _LOG, len(image), _LFS_BLOCK_COUNT, _LFS_BLOCK_SIZE))
    return image


# ---------------------------------------------------------------------------
# UF2 helpers
# ---------------------------------------------------------------------------

def _uf2_block(payload256: bytes, addr: int,
               blk_num: int, total: int, family: int) -> bytes:
    """Pack one 512-byte UF2 block."""
    payload256 = payload256.ljust(256, b'\xff')
    header = struct.pack("<IIIIIIII",
        _UF2_MAGIC0,
        _UF2_MAGIC1,
        _UF2_FLAG_FAMILY,
        addr,
        256,        # payload size always 256
        blk_num,
        total,
        family,
    )
    padding = b'\x00' * (512 - 32 - 256 - 4)
    return header + payload256 + padding + struct.pack("<I", _UF2_MAGIC_END)


def parse_firmware_uf2(uf2_path: Path):
    """
    Parse a UF2 file → list of (addr, 256-byte data) tuples.
    Also returns the family ID found in the first block (or RP2350 default).
    """
    raw      = uf2_path.read_bytes()
    blocks   = []
    family   = None

    for off in range(0, len(raw), 512):
        blk = raw[off: off + 512]
        if len(blk) < 512:
            break
        m0, m1, flags, addr, psize, bnum, total, fam_or_size = \
            struct.unpack_from("<IIIIIIII", blk)
        end = struct.unpack_from("<I", blk, 508)[0]
        if m0 != _UF2_MAGIC0 or m1 != _UF2_MAGIC1 or end != _UF2_MAGIC_END:
            continue
        if flags & _UF2_FLAG_FAMILY and family is None:
            family = fam_or_size
        blocks.append((addr, blk[32: 32 + psize]))

    if family is None:
        family = _RP2350_FAMILY
        print("{} No familyID in firmware UF2 — using RP2350 default "
              "(0x{:08X}).".format(_LOG, family))
    else:
        print("{} Firmware familyID: 0x{:08X}".format(_LOG, family))

    return blocks, family


def binary_to_uf2_pairs(data: bytes, base_addr: int):
    """Split binary data into (addr, 256-byte chunk) pairs."""
    # Pad to multiple of 256 with 0xFF (erased flash value)
    pad  = (-len(data)) % 256
    data = data + b'\xff' * pad
    return [(base_addr + i, data[i: i + 256])
            for i in range(0, len(data), 256)]


def write_combined_uf2(out_path: Path,
                       fw_pairs: list, fs_pairs: list, family: int):
    """Merge firmware + filesystem block pairs and write as a UF2 file."""
    all_pairs = fw_pairs + fs_pairs
    total     = len(all_pairs)

    with open(out_path, "wb") as fh:
        for idx, (addr, data) in enumerate(all_pairs):
            fh.write(_uf2_block(data, addr, idx, total, family))

    kb = out_path.stat().st_size / 1024
    print("{} {} blocks written → {} ({:.1f} KB)".format(
        _LOG, total, out_path, kb))


# ---------------------------------------------------------------------------
# Address-overlap safety check
# ---------------------------------------------------------------------------

def _check_overlap(fw_pairs: list):
    if not fw_pairs:
        return
    fw_end = max(addr + 256 for addr, _ in fw_pairs)
    if fw_end > _FS_BASE_ADDR:
        print()
        print("{} FATAL: Firmware ends at 0x{:08X} but filesystem starts at "
              "0x{:08X}.".format(_LOG, fw_end, _FS_BASE_ADDR))
        print("{} The MicroPython binary is too large for the 1.5 MB slot.".format(_LOG))
        print("{} Check MICROPY_HW_FLASH_STORAGE_BYTES in mpconfigboard.h.".format(_LOG))
        sys.exit(1)
    print("{} No overlap: firmware ends 0x{:08X}, FS starts 0x{:08X} ✓".format(
        _LOG, fw_end, _FS_BASE_ADDR))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build(uid_str: str, src_dir: Path, firmware_uf2: Path,
          out_uf2: Path, salt_path: Path | None):

    uid_bytes = _parse_uid(uid_str)

    print()
    print("{} =====================================================".format(_LOG))
    print("{} SOMNI-Guard UF2 Packaging Tool".format(_LOG))
    print("{} =====================================================".format(_LOG))
    print("{} Pico 2W UID  : {}".format(_LOG, uid_bytes.hex().upper()))
    print("{} Source dir   : {}".format(_LOG, src_dir))
    print("{} Firmware UF2 : {}".format(_LOG, firmware_uf2))
    print("{} Output UF2   : {}".format(_LOG, out_uf2))
    print("{} Flash layout : firmware 0x{:08X}–0x{:08X} ({} KB)".format(
        _LOG, _FLASH_BASE, _FS_BASE_ADDR, _FW_RESERVED // 1024))
    print("{} FS layout    : 0x{:08X}  ({} blocks × {} B = {} KB)".format(
        _LOG, _FS_BASE_ADDR, _LFS_BLOCK_COUNT, _LFS_BLOCK_SIZE,
        _FS_SIZE_BYTES // 1024))
    print("{} =====================================================".format(_LOG))

    # ── Salt ──────────────────────────────────────────────────────────────
    if salt_path and salt_path.exists():
        salt = salt_path.read_bytes()
        print("{} [salt] Loaded from {}".format(_LOG, salt_path))
    else:
        salt = os.urandom(16)
        # Save salt alongside the output UF2 for future re-runs
        salt_out = out_uf2.parent / "_salt.bin"
        salt_out.write_bytes(salt)
        print("{} [salt] Generated new random salt → {}".format(_LOG, salt_out))
    print("{} [salt] {} B: {}".format(_LOG, len(salt), salt.hex()))

    # ── Step 1: Encrypt sources ───────────────────────────────────────────
    print()
    print("{} [1/4] Encrypting source files...".format(_LOG))
    files = encrypt_sources(src_dir, uid_bytes, salt)
    print("{} Total: {} files".format(_LOG, len(files)))

    # ── Step 2: Build LittleFS2 image ─────────────────────────────────────
    print()
    print("{} [2/4] Building LittleFS2 image...".format(_LOG))
    lfs_image = build_lfs_image(files, salt)

    # ── Step 3: Parse firmware UF2 ────────────────────────────────────────
    print()
    print("{} [3/4] Parsing firmware UF2...".format(_LOG))
    fw_pairs, family = parse_firmware_uf2(firmware_uf2)
    print("{} Firmware: {} blocks".format(_LOG, len(fw_pairs)))
    _check_overlap(fw_pairs)

    fs_pairs = binary_to_uf2_pairs(lfs_image, _FS_BASE_ADDR)
    print("{} Filesystem: {} UF2 blocks  ({}–0x{:08X})".format(
        _LOG, len(fs_pairs), hex(_FS_BASE_ADDR),
        _FS_BASE_ADDR + len(fs_pairs) * 256))

    # ── Step 4: Write combined UF2 ────────────────────────────────────────
    print()
    print("{} [4/4] Writing combined UF2...".format(_LOG))
    write_combined_uf2(out_uf2, fw_pairs, fs_pairs, family)

    print()
    print("{} =====================================================".format(_LOG))
    print("{} SUCCESS:  {}".format(_LOG, out_uf2))
    print("{} =====================================================".format(_LOG))
    print("{} FLASH INSTRUCTIONS:".format(_LOG))
    print("{}   1. Hold BOOTSEL while plugging the Pico 2W into USB.".format(_LOG))
    print("{}   2. A drive named 'RP2350' appears on your Mac.".format(_LOG))
    print("{}   3. Copy {} to that drive.".format(_LOG, out_uf2.name))
    print("{}   4. Pico reboots — USB is disabled, firmware is running.".format(_LOG))
    print("{} RECOVERY:".format(_LOG))
    print("{}   Hold BOOTSEL on power-on → ROM bootloader → drag any UF2.".format(_LOG))
    print("{} =====================================================".format(_LOG))


def main():
    parser = argparse.ArgumentParser(
        description="SOMNI-Guard: Encrypt + package as a single flashable UF2.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # First build the custom firmware (if not done yet):
  cd scripts && ./custom_micropython_build/build.sh

  # Then package everything into one UF2:
  python scripts/somni_uf2_tool.py \\
      --uid 2effff680e87ca96 \\
      --src scripts/somniguard_pico/ \\
      --firmware somni_guard_firmware.uf2 \\
      --out somni_guard_complete.uf2

  # Re-run with the same salt (so the existing _salt.bin works):
  python scripts/somni_uf2_tool.py \\
      --uid 2effff680e87ca96 \\
      --src scripts/somniguard_pico/ \\
      --firmware somni_guard_firmware.uf2 \\
      --out somni_guard_complete.uf2 \\
      --salt _salt.bin

Educational prototype — not for clinical use.
""")

    parser.add_argument("--uid", required=True,
        metavar="HEX",
        help="Pico 2W hardware UID. Read it with: "
             "mpremote exec \"import machine; print(machine.unique_id().hex())\"")

    parser.add_argument("--src",
        default="scripts/somniguard_pico/",
        metavar="DIR",
        help="Source directory containing .py files "
             "(default: scripts/somniguard_pico/)")

    parser.add_argument("--firmware",
        default="somni_guard_firmware.uf2",
        metavar="UF2",
        help="Custom MicroPython UF2 built by build.sh "
             "(default: somni_guard_firmware.uf2)")

    parser.add_argument("--out",
        default="somni_guard_complete.uf2",
        metavar="UF2",
        help="Output combined UF2 path (default: somni_guard_complete.uf2)")

    parser.add_argument("--salt",
        default=None,
        metavar="BIN",
        help="Path to an existing _salt.bin. Omit to generate a new salt. "
             "IMPORTANT: if you re-flash the same Pico, use the same salt "
             "that was used when the key was first derived.")

    args = parser.parse_args()

    src_dir  = Path(args.src)
    fw_uf2   = Path(args.firmware)
    out_uf2  = Path(args.out)
    salt_p   = Path(args.salt) if args.salt else None

    errors = []
    if not src_dir.is_dir():
        errors.append("Source directory not found: {}".format(src_dir))
    if not fw_uf2.exists():
        errors.append(
            "Firmware UF2 not found: {}\n"
            "  Build it first:  cd scripts && ./custom_micropython_build/build.sh"
            .format(fw_uf2))
    if errors:
        for e in errors:
            print("ERROR: " + e)
        sys.exit(1)

    build(args.uid, src_dir, fw_uf2, out_uf2, salt_p)


if __name__ == "__main__":
    main()
