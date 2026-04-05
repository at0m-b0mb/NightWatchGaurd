#!/usr/bin/env python3
"""
SOMNI-Guard Integrity Manifest Generator v1.0

EDUCATIONAL PROTOTYPE DISCLAIMER:
This script is part of an educational prototype and is provided for
demonstration and learning purposes only. It is not intended for use
in production or safety-critical environments without thorough review,
testing, and hardening by qualified security professionals.

Scans all .py files in the Pico source directory, computes SHA-256
hashes, signs the manifest with HMAC-SHA256, and writes manifest.json
to the specified output path.

Usage:
    python scripts/generate_integrity_manifest.py \
        [--pico-dir somniguard_pico/] \
        [--output somniguard_pico/manifest.json] \
        [--hmac-key <key>]
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PREFIX = "[SOMNI][MANIFEST]"
GENERATOR_NAME = "SOMNI-Guard Integrity Manifest Generator v1.0"
MANIFEST_VERSION = 1


def log(message: str) -> None:
    """Print a prefixed log message."""
    print(f"{PREFIX} {message}")


def log_error(message: str) -> None:
    """Print a prefixed error message to stderr."""
    print(f"{PREFIX} ERROR: {message}", file=sys.stderr)


def find_py_files(pico_dir: Path) -> list[Path]:
    """Recursively find all .py files in the given directory."""
    if not pico_dir.exists():
        raise FileNotFoundError(f"Pico directory not found: {pico_dir}")
    if not pico_dir.is_dir():
        raise NotADirectoryError(f"Not a directory: {pico_dir}")

    py_files = sorted(pico_dir.rglob("*.py"))
    return py_files


def compute_sha256(file_path: Path) -> str:
    """Compute the SHA-256 hash of a file's contents and return the hex digest."""
    sha256 = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
    except PermissionError as exc:
        raise PermissionError(f"Permission denied reading file: {file_path}") from exc
    except OSError as exc:
        raise OSError(f"Failed to read file {file_path}: {exc}") from exc
    return sha256.hexdigest()


def compute_hmac_signature(files_dict: dict, hmac_key: str) -> str:
    """Compute HMAC-SHA256 signature over the canonical JSON of the files dict."""
    # Canonical JSON: sorted keys, no extra whitespace
    canonical_json = json.dumps(files_dict, sort_keys=True, separators=(",", ":"))
    key_bytes = hmac_key.encode("utf-8")
    message_bytes = canonical_json.encode("utf-8")
    signature = hmac.new(key_bytes, message_bytes, hashlib.sha256).hexdigest()
    return signature


def verify_hmac_signature(files_dict: dict, hmac_key: str, expected_signature: str) -> bool:
    """Verify the HMAC-SHA256 signature of the files dict."""
    computed = compute_hmac_signature(files_dict, hmac_key)
    return hmac.compare_digest(computed, expected_signature)


def build_manifest(pico_dir: Path, hmac_key: str) -> dict:
    """Scan pico_dir for .py files, compute hashes, and build the manifest dict."""
    log(f"Scanning for .py files in: {pico_dir.resolve()}")

    py_files = find_py_files(pico_dir)

    if not py_files:
        log("WARNING: No .py files found in the specified directory.")

    files_dict: dict[str, str] = {}
    for file_path in py_files:
        # Store relative path from pico_dir, using forward slashes for consistency
        relative_path = file_path.relative_to(pico_dir).as_posix()
        file_hash = compute_sha256(file_path)
        files_dict[relative_path] = file_hash
        log(f"  {relative_path}: {file_hash}")

    log(f"Hashed {len(files_dict)} file(s).")

    signature = compute_hmac_signature(files_dict, hmac_key)

    manifest = {
        "version": MANIFEST_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": GENERATOR_NAME,
        "files": files_dict,
        "signature": signature,
    }
    return manifest


def write_manifest(manifest: dict, output_path: Path) -> None:
    """Write the manifest dict to the output JSON file."""
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=False)
            f.write("\n")
    except PermissionError as exc:
        raise PermissionError(f"Permission denied writing manifest: {output_path}") from exc
    except OSError as exc:
        raise OSError(f"Failed to write manifest to {output_path}: {exc}") from exc


def verify_written_manifest(output_path: Path, hmac_key: str) -> bool:
    """Re-read the written manifest and verify its HMAC signature."""
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log_error(f"Failed to re-read manifest for verification: {exc}")
        return False

    files_dict = loaded.get("files", {})
    stored_signature = loaded.get("signature", "")

    return verify_hmac_signature(files_dict, hmac_key, stored_signature)


def get_hmac_key(args_key: str | None) -> str:
    """Resolve the HMAC key from CLI arg, environment variable, or interactive prompt."""
    if args_key:
        return args_key

    env_key = os.environ.get("SOMNI_HMAC_KEY")
    if env_key:
        log("Using HMAC key from environment variable SOMNI_HMAC_KEY.")
        return env_key

    # Fall back to interactive prompt
    try:
        import getpass
        key = getpass.getpass(f"{PREFIX} Enter HMAC key (input hidden): ")
        if not key:
            raise ValueError("HMAC key must not be empty.")
        return key
    except (EOFError, KeyboardInterrupt):
        raise ValueError(
            "No HMAC key provided. Set SOMNI_HMAC_KEY, pass --hmac-key, or enter interactively."
        )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            f"{GENERATOR_NAME}\n\n"
            "Scans .py files in the Pico source directory, computes SHA-256 hashes,\n"
            "signs the manifest with HMAC-SHA256, and writes manifest.json.\n\n"
            "EDUCATIONAL PROTOTYPE — not for production use."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--pico-dir",
        default="somniguard_pico/",
        help="Directory containing Pico .py source files (default: somniguard_pico/)",
    )
    parser.add_argument(
        "--output",
        default="somniguard_pico/manifest.json",
        help="Output path for manifest.json (default: somniguard_pico/manifest.json)",
    )
    parser.add_argument(
        "--hmac-key",
        default=None,
        help=(
            "HMAC-SHA256 signing key. If omitted, reads from SOMNI_HMAC_KEY "
            "environment variable, or prompts interactively."
        ),
    )
    return parser.parse_args()


def main() -> int:
    """Main entry point. Returns exit code."""
    log("SOMNI-Guard Integrity Manifest Generator v1.0")
    log("EDUCATIONAL PROTOTYPE — not for production use without security review.")
    log("-" * 60)

    args = parse_args()

    pico_dir = Path(args.pico_dir)
    output_path = Path(args.output)

    # Resolve HMAC key
    try:
        hmac_key = get_hmac_key(args.hmac_key)
    except ValueError as exc:
        log_error(str(exc))
        return 1

    # Build manifest
    try:
        manifest = build_manifest(pico_dir, hmac_key)
    except FileNotFoundError as exc:
        log_error(str(exc))
        return 1
    except NotADirectoryError as exc:
        log_error(str(exc))
        return 1
    except PermissionError as exc:
        log_error(str(exc))
        return 1
    except OSError as exc:
        log_error(str(exc))
        return 1

    # Write manifest
    log(f"Writing manifest to: {output_path.resolve()}")
    try:
        write_manifest(manifest, output_path)
    except (PermissionError, OSError) as exc:
        log_error(str(exc))
        return 1

    log(f"Manifest written successfully.")

    # Verification: re-read and verify
    log("Verifying written manifest...")
    if verify_written_manifest(output_path, hmac_key):
        log("Verification PASSED: HMAC signature is valid.")
    else:
        log_error("Verification FAILED: HMAC signature mismatch in written manifest.")
        return 1

    log("-" * 60)
    log(f"Summary:")
    log(f"  Files hashed : {len(manifest['files'])}")
    log(f"  Generated at : {manifest['generated_at']}")
    log(f"  Output path  : {output_path.resolve()}")
    log(f"  Signature    : {manifest['signature']}")
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
