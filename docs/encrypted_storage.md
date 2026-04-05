# SOMNI-Guard Encrypted Storage Guide — Pico 2W

> **Educational prototype — not a clinically approved device.**
> The encrypted storage mechanism described here is designed to demonstrate
> the *principles* of hardware-bound secret storage on a microcontroller,
> using algorithms and libraries available in MicroPython. It provides
> meaningful protection against offline extraction of filesystem images but
> should not be treated as equivalent to certified hardware security module
> (HSM) storage or FIPS 140-2 validated cryptography. Do not use this device
> for clinical diagnosis, treatment, or any patient-safety purpose.

---

## Table of Contents

1. [Overview of Encrypted Configuration Storage](#1-overview-of-encrypted-configuration-storage)
2. [How It Works](#2-how-it-works)
3. [What Is Encrypted](#3-what-is-encrypted)
4. [Setup Instructions](#4-setup-instructions)
5. [Using the secure_config.py API](#5-using-the-secure_configpy-api)
6. [Key Derivation from Hardware Unique ID](#6-key-derivation-from-hardware-unique-id)
7. [Security Considerations and Limitations](#7-security-considerations-and-limitations)
8. [Migrating from Plaintext Configuration](#8-migrating-from-plaintext-configuration)

---

## 1. Overview of Encrypted Configuration Storage

### The problem: secrets stored as plaintext

The SOMNI-Guard Pico 2W sensor node must store several sensitive values to
operate:

- **HMAC key** (`GATEWAY_HMAC_KEY`): The shared secret used to authenticate
  telemetry packets sent to the Pi 5 gateway.
- **Wi-Fi SSID and password** (`WIFI_SSID`, `WIFI_PASSWORD`): Credentials
  needed to connect to the local network.

Without encryption, these values reside as plaintext in
`somniguard_pico/config.py` on the Pico's flash filesystem. An attacker with
physical access to the device (e.g., a family member, a curious visitor, or a
thief) can extract the filesystem over USB using MicroPython tools and read all
secrets in seconds.

### The solution: hardware-bound encryption

SOMNI-Guard implements the **L1-C7** security control (see
`docs/security_controls.md`): sensitive values are encrypted at rest using a
key that is **derived from the device's hardware unique ID**. The key never
appears on disk — it is re-derived at runtime from the hardware each time it
is needed, then wiped from memory.

This means:
- The encrypted file copied from one Pico **cannot be decrypted on a different
  Pico** (different hardware ID → different key).
- An attacker who extracts the `secure_config.json` file via USB cannot
  decrypt it without also obtaining the specific Pico hardware it was
  encrypted on.

### Security control classification

| Control | Threats mitigated | Implementation |
|---------|------------------|----------------|
| L1-C7 Secure configuration storage | H-04 (credential theft), H-08 (key material exposure) | `somniguard_pico/secure_config.py` |

---

## 2. How It Works

### Architecture overview

```
                      ┌─────────────────────────────────────────┐
                      │          Pico 2W Flash Filesystem        │
                      │                                          │
                      │  config.py          ← plaintext (no     │
                      │  (non-sensitive      sensitive values)   │
                      │   settings only)                         │
                      │                                          │
                      │  secure_config.json ← XTEA-encrypted     │
                      │  {                   JSON envelope       │
                      │    "version": 1,                         │
                      │    "data": "<base64>"                    │
                      │  }                                       │
                      └─────────────────────────────────────────┘
                                           ▲
                                           │ encrypt/decrypt
                                           │
                      ┌─────────────────────────────────────────┐
                      │         secure_config.py (runtime)      │
                      │                                          │
                      │  1. Call machine.unique_id()             │
                      │     → 8-byte hardware UID               │
                      │  2. SHA-256(UID) → 32 bytes             │
                      │  3. First 16 bytes = XTEA key           │
                      │  4. XTEA encrypt/decrypt plaintext       │
                      │  5. wipe_bytes(key) after use            │
                      └─────────────────────────────────────────┘
                                           │
                      ┌─────────────────────────────────────────┐
                      │       RP2350 SoC (read-only hardware)   │
                      │                                          │
                      │  machine.unique_id()                     │
                      │  → 8-byte factory-programmed UID        │
                      │  (unique per chip, cannot be changed)   │
                      └─────────────────────────────────────────┘
```

### Encryption algorithm: XTEA

SOMNI-Guard uses the **XTEA** (eXtended Tiny Encryption Algorithm) cipher.
XTEA was chosen over AES because:

- It is implementable in pure Python with no external libraries.
- It requires no hardware acceleration (runs on the RP2350's ARM Cortex-M33).
- MicroPython does not include an AES implementation in its standard build for
  the Pico 2W.
- It is a well-analysed algorithm with no known practical attacks against the
  64-round variant.

| Property | Value |
|----------|-------|
| Algorithm | XTEA (eXtended Tiny Encryption Algorithm) |
| Key size | 128 bits (16 bytes) |
| Block size | 64 bits (8 bytes) |
| Number of rounds | 64 |
| Block mode | ECB (independent per block) |
| Padding | PKCS7 (1–8 bytes to reach 8-byte boundary) |

**ECB mode note:** The current implementation encrypts each 8-byte block
independently (ECB mode). This means identical 8-byte plaintext blocks
produce identical ciphertext blocks. For JSON-formatted configuration data —
which is relatively short and not highly repetitive — this is an acceptable
trade-off. A production implementation would add an initialisation vector (IV)
and use CBC mode to prevent this pattern leakage.

### File format on disk

The encrypted configuration is stored in a JSON envelope at
`/secure_config.json` on the Pico filesystem:

```json
{
    "version": 1,
    "data": "<base64-encoded XTEA ciphertext>"
}
```

The `version` field supports future migration to a different encryption scheme
without breaking existing deployments.

### Data flow: writing secrets

```
config_dict (Python dict)
    │
    ▼
json.dumps(config_dict)  → UTF-8 bytes
    │
    ▼
_pad(plaintext)          → PKCS7-padded bytes (multiple of 8)
    │
    ▼
for each 8-byte block:
    _xtea_encrypt_block(block, key)  → 8-byte ciphertext block
    │
    ▼
binascii.b2a_base64(ciphertext)  → base64 string
    │
    ▼
json.dumps({"version": 1, "data": "<base64>"})
    │
    ▼
write to /secure_config.json
```

### Data flow: reading secrets

```
read /secure_config.json
    │
    ▼
json.loads()  → envelope dict
    │
    ▼
binascii.a2b_base64(envelope["data"])  → ciphertext bytes
    │
    ▼
for each 8-byte block:
    _xtea_decrypt_block(block, key)  → 8-byte plaintext block
    │
    ▼
_unpad(plaintext_padded)  → plaintext bytes (PKCS7 stripped)
    │
    ▼
json.loads(plaintext.decode("utf-8"))  → config_dict (Python dict)
```

---

## 3. What Is Encrypted

The following sensitive values should be stored in the encrypted configuration
rather than in plaintext `config.py`:

| Key | Description | Example value |
|-----|-------------|---------------|
| `GATEWAY_HMAC_KEY` | Shared secret for HMAC-SHA256 telemetry authentication | `"a3f9c2...e7b1"` (64 hex chars) |
| `WIFI_SSID` | Wi-Fi network name | `"HomeNetwork"` |
| `WIFI_PASSWORD` | Wi-Fi network password | `"correct-horse-battery-staple"` |

Values that do **not** need to be encrypted (and remain in `config.py`):

| Key | Description |
|-----|-------------|
| `GATEWAY_HOST` | Pi 5 LAN IP address — not a secret |
| `GATEWAY_PORT` | TCP port number — not a secret |
| `SPO2_IR_MIN_VALID` | Sensor threshold — not a secret |
| `HR_LOW_WARN`, `HR_HIGH_WARN` | Clinical alert thresholds — not a secret |

The separation keeps `config.py` as the "non-sensitive settings" file and
`secure_config.json` as the encrypted secrets store.

---

## 4. Setup Instructions

### Prerequisites

- Raspberry Pi Pico 2W with MicroPython firmware installed.
- The SOMNI-Guard Pico firmware deployed (`somniguard_pico/` directory).
- `mpremote` or Thonny IDE for file transfer.
- The `secure_config.py` module already on the Pico (part of the standard
  SOMNI-Guard firmware deployment).

### Step 1: Prepare your secrets

On your development machine, create a Python script to generate the encrypted
config file. Note that this script must run **on the Pico itself** (not your
PC) because key derivation uses `machine.unique_id()`.

Option A — using the MicroPython REPL (recommended for first-time setup):

```python
# Connect to the Pico REPL via USB (Thonny or: mpremote connect /dev/ttyACM0)
# Then paste the following into the REPL:

import secure_config

# Replace these with your actual values
secrets = {
    "GATEWAY_HMAC_KEY": "your-64-character-hex-hmac-key-here",
    "WIFI_SSID":        "YourNetworkName",
    "WIFI_PASSWORD":    "YourNetworkPassword"
}

secure_config.save_secure_config(secrets, "/secure_config.json")
print("Encrypted config saved.")
```

Option B — using mpremote exec:

```bash
# From your development machine
mpremote connect /dev/ttyACM0 exec "
import secure_config
secrets = {
    'GATEWAY_HMAC_KEY': 'your-key-here',
    'WIFI_SSID': 'YourSSID',
    'WIFI_PASSWORD': 'YourPassword'
}
secure_config.save_secure_config(secrets, '/secure_config.json')
print('Done')
"
```

### Step 2: Verify the file was created

```python
# On the Pico REPL:
import os
os.listdir("/")          # should include 'secure_config.json'

# Check file size (should be > 0)
os.stat("/secure_config.json")
```

### Step 3: Update config.py to load from secure storage

Modify `somniguard_pico/config.py` to load sensitive values from the encrypted
store at startup instead of hardcoding them:

```python
# In config.py — replace hardcoded secrets with:
import secure_config as _sc

try:
    _secrets = _sc.load_secure_config("/secure_config.json")
    GATEWAY_HMAC_KEY = _secrets.get("GATEWAY_HMAC_KEY", "")
    WIFI_SSID        = _secrets.get("WIFI_SSID", "")
    WIFI_PASSWORD    = _secrets.get("WIFI_PASSWORD", "")
    del _secrets  # remove from namespace after loading
except Exception as e:
    print("[SOMNI][CONFIG] WARNING: Could not load secure config:", e)
    print("[SOMNI][CONFIG] Falling back to plaintext defaults.")
    GATEWAY_HMAC_KEY = "CHANGE-ME-INSECURE-DEFAULT"
    WIFI_SSID        = ""
    WIFI_PASSWORD    = ""
```

### Step 4: Remove plaintext secrets from config.py

Once the encrypted config is working, delete or replace the plaintext secret
values in `config.py` with safe fallback strings (not real credentials):

```python
# DO NOT store real secrets here — they live in /secure_config.json
GATEWAY_HMAC_KEY = "LOADED-FROM-SECURE-CONFIG"  # overwritten at runtime
WIFI_SSID        = "LOADED-FROM-SECURE-CONFIG"
WIFI_PASSWORD    = "LOADED-FROM-SECURE-CONFIG"
```

### Step 5: Test the full boot cycle

1. Reset the Pico (press the reset button or power-cycle).
2. Open the serial console (Thonny or `mpremote`).
3. Confirm you see the log lines:
   ```
   [SOMNI][SECURE_CONFIG] Key derived from hardware unique ID (SHA-256, first 16 bytes).
   [SOMNI][SECURE_CONFIG] Loading encrypted config from '/secure_config.json'.
   [SOMNI][SECURE_CONFIG] Secure config loaded successfully.
   ```
4. Confirm the device connects to Wi-Fi and authenticates telemetry successfully.

---

## 5. Using the secure_config.py API

The `secure_config` module provides a simple, four-function public API.

### `save_secure_config(config_dict, filepath)`

Encrypts a Python dictionary and saves it to the specified file path.

```python
import secure_config

secrets = {
    "GATEWAY_HMAC_KEY": "abc123...",
    "WIFI_SSID": "MyNetwork",
    "WIFI_PASSWORD": "hunter2"
}

secure_config.save_secure_config(secrets, "/secure_config.json")
```

- `config_dict`: Any JSON-serialisable Python dictionary.
- `filepath`: Destination path on the Pico filesystem (e.g., `"/secure_config.json"`).
- Raises `OSError` if the file cannot be written.

### `load_secure_config(filepath)`

Reads and decrypts an encrypted configuration file. Returns the original dictionary.

```python
import secure_config

config = secure_config.load_secure_config("/secure_config.json")
hmac_key = config["GATEWAY_HMAC_KEY"]
```

- `filepath`: Path to an existing encrypted config file.
- Returns a Python `dict`.
- Raises `OSError` if the file does not exist.
- Raises `ValueError` if the file format version is unsupported.

### `encrypt_config(config_dict)` → `bytes`

Low-level function: encrypts a dictionary and returns raw ciphertext bytes
(without base64 encoding or the JSON envelope). Use `save_secure_config` for
filesystem storage.

```python
ciphertext = secure_config.encrypt_config({"key": "value"})
```

### `decrypt_config(encrypted_bytes)` → `dict`

Low-level function: decrypts raw ciphertext bytes back to a dictionary. Use
`load_secure_config` for filesystem storage.

```python
config = secure_config.decrypt_config(ciphertext)
```

### `get_hardware_key()` → `bytearray`

Returns the 16-byte XTEA key derived from the hardware unique ID. The returned
bytearray should be wiped with `wipe_bytes()` after use.

```python
key = secure_config.get_hardware_key()
# ... use key ...
secure_config.wipe_bytes(key)
```

### `wipe_bytes(ba)`

Overwrites all bytes in a `bytearray` with zeros. Called automatically inside
`encrypt_config` and `decrypt_config` to limit key exposure in RAM.

```python
key = bytearray(b"secret")
secure_config.wipe_bytes(key)
# key is now b"\x00\x00\x00\x00\x00\x00"
```

### Compatibility: MicroPython vs CPython

`secure_config.py` uses a compatibility shim to run on both MicroPython (on
the Pico) and CPython (on a development machine for unit tests):

| Module | MicroPython | CPython |
|--------|-------------|---------|
| `machine` | `machine.unique_id()` | Synthetic UID `b"\xDE\xAD\xBE\xEF\xCA\xFE\xBA\xBE"` |
| `struct` | `ustruct` | `struct` |
| `json` | `ujson` | `json` |
| `binascii` | `ubinascii` | `binascii` |
| `hashlib` | `uhashlib` | `hashlib` |

**Important:** The CPython fallback uses a **fixed synthetic unique ID** so
that unit tests are deterministic. Do NOT use CPython-generated encrypted
files on a real Pico — the key will not match.

---

## 6. Key Derivation from Hardware Unique ID

### What is the hardware unique ID?

The Raspberry Pi Pico 2W is built on the RP2350 microcontroller. During
manufacturing, each RP2350 chip is programmed with an **8-byte unique
identifier** accessible via `machine.unique_id()` in MicroPython. This value:

- Is factory-programmed and cannot be changed by software.
- Is unique to each individual chip.
- Is not stored anywhere on the flash filesystem.

### Derivation function

```
UID (8 bytes from machine.unique_id())
    │
    ▼
SHA-256(UID)  →  32-byte digest
    │
    ▼
digest[0:16]  →  16-byte XTEA key
```

The SHA-256 hash serves two purposes:
1. It expands the 8-byte UID to a full 16-byte key (XTEA requires exactly
   16 bytes).
2. It provides a one-way transformation — even if the XTEA key were somehow
   extracted from RAM, recovering the raw UID would require reversing SHA-256,
   which is computationally infeasible.

### Why hardware binding matters

Without hardware binding, an attacker who steals a Pico device could:
1. Extract the `secure_config.json` file over USB (takes under 30 seconds).
2. Copy it to another machine and brute-force the encryption key.

With hardware binding:
- The encryption key is derived from a value that **cannot leave the hardware**.
- Even with the ciphertext in hand, decryption requires the specific Pico chip.
- Brute-forcing 128-bit XTEA keys is computationally infeasible
  (2^128 ≈ 3.4 × 10^38 possible keys).

### Key lifecycle

```
Boot
  │
  ▼
machine.unique_id()  ──►  _derive_key()  ──►  key (bytearray, in RAM)
                                                  │
                                                  ▼
                                          encrypt or decrypt
                                                  │
                                                  ▼
                                          wipe_bytes(key)  ──►  key zeroed
```

The key exists in RAM only for the duration of a single encrypt or decrypt
operation. `wipe_bytes()` is called in a `finally` block in `encrypt_config`
and `decrypt_config` to ensure the key is zeroed even if an exception occurs.

---

## 7. Security Considerations and Limitations

### Strengths

- **Hardware binding**: The encrypted file cannot be decrypted without the
  specific Pico chip. Physical theft of the SD card or filesystem image is
  not sufficient.
- **No stored key material**: The XTEA key never appears in any file on disk.
- **Defence in depth**: Even if the filesystem is extracted, an attacker must
  also physically compromise the hardware to decrypt secrets.
- **MicroPython native**: No external C extensions or compiled binaries
  required; runs on any standard MicroPython build.

### Known Limitations

**ECB block mode**

The current implementation uses ECB mode (each 8-byte block is independently
encrypted). For long, repetitive plaintexts, ECB mode can reveal patterns in
the ciphertext. For SOMNI-Guard's typical payload (a short JSON object with
varying string values), this is not a meaningful practical weakness, but it
is documented here for transparency.

*Mitigation*: For a production device, add an IV stored alongside the
ciphertext and implement CBC mode.

**No authenticated encryption**

XTEA-ECB is a cipher; it does not provide authentication. An attacker who can
modify the `secure_config.json` file could corrupt the ciphertext, causing
decryption to produce garbled data (which would then fail JSON parsing). This
is a denial-of-service risk, not a confidentiality risk.

*Mitigation*: Wrap the ciphertext in an HMAC-SHA256 tag computed with the
hardware-derived key before saving, and verify the tag before decrypting.

**RAM wiping is best-effort**

`wipe_bytes()` zeroes the `bytearray` holding the key in Python, but
MicroPython's garbage collector may have already moved or copied the buffer
before wiping. There is no guarantee of complete key erasure from RAM.

*Mitigation*: In practice, an attacker with the ability to dump MicroPython
RAM has already compromised the device at a deeper level. This is an accepted
limitation of software-only key management.

**USB access bypasses encryption**

An attacker with physical access and knowledge of MicroPython can:
1. Connect to the Pico's REPL over USB.
2. Run `import secure_config; print(secure_config.load_secure_config('/secure_config.json'))`
3. The decrypted secrets are printed to the console.

*Mitigation*: Disable the USB serial interface in production (requires a
custom MicroPython build or BOOTSEL pin configuration). For SOMNI-Guard's
threat model (unattended bedside device with limited attacker dwell time),
this is an accepted limitation.

**SHA-256 UID derivation is deterministic**

If an attacker learns the hardware UID (e.g., by reading it from the REPL),
they can derive the XTEA key and decrypt the config on a PC.

*Mitigation*: Restrict REPL access (see USB access point above). The UID is
protected by the same physical access requirement as the REPL.

### Comparison with alternative approaches

| Approach | SOMNI-Guard | Production alternative |
|----------|-------------|----------------------|
| Cipher | XTEA (MicroPython-compatible) | AES-256-GCM (hardware accelerated) |
| Key storage | Derived from hardware UID | TPM, secure element, or eFuse OTP |
| Block mode | ECB | CBC or GCM (authenticated) |
| Key wiping | Best-effort (Python) | Guaranteed (C/hardware) |
| REPL access | Available (USB) | Disabled in production firmware |

---

## 8. Migrating from Plaintext Configuration

If you have an existing SOMNI-Guard deployment with plaintext secrets in
`config.py`, follow these steps to migrate to encrypted storage.

### Step 1: Note your current secrets

Before making any changes, record your current values from `config.py`:

```python
# Note these values:
# GATEWAY_HMAC_KEY = "..."
# WIFI_SSID = "..."
# WIFI_PASSWORD = "..."
```

### Step 2: Deploy the new firmware

Ensure `secure_config.py` is present on the Pico:

```bash
# From your development machine
mpremote connect /dev/ttyACM0 cp somniguard_pico/secure_config.py :secure_config.py
```

Verify it is present:

```bash
mpremote connect /dev/ttyACM0 ls
```

### Step 3: Write the encrypted config on the Pico

Connect to the REPL and run:

```python
import secure_config

secrets = {
    "GATEWAY_HMAC_KEY": "YOUR_EXISTING_HMAC_KEY",
    "WIFI_SSID":        "YOUR_EXISTING_SSID",
    "WIFI_PASSWORD":    "YOUR_EXISTING_WIFI_PASSWORD"
}

secure_config.save_secure_config(secrets, "/secure_config.json")
print("Migration complete.")
```

### Step 4: Test decryption

Without leaving the REPL, verify the file can be read back:

```python
import secure_config
loaded = secure_config.load_secure_config("/secure_config.json")
print("Keys found:", list(loaded.keys()))
# Expected: Keys found: ['GATEWAY_HMAC_KEY', 'WIFI_SSID', 'WIFI_PASSWORD']
```

Do not print the actual values to the console during testing in a
non-private environment.

### Step 5: Update config.py

Replace the plaintext values in `config.py` with the secure-load pattern
shown in Section 4, Step 3.

### Step 6: Deploy updated config.py

```bash
mpremote connect /dev/ttyACM0 cp somniguard_pico/config.py :config.py
```

### Step 7: Reboot and verify

Reset the Pico and confirm it boots, connects to Wi-Fi, and sends
authenticated telemetry to the gateway without errors.

### Step 8: Remove plaintext secrets from config.py (committed version)

Ensure that the version of `config.py` committed to version control does
**not** contain real HMAC keys or Wi-Fi passwords. Use the placeholder
strings described in Section 4, Step 4.

### Rollback

If anything goes wrong, the encrypted config file can be deleted and the
plaintext fallback values in `config.py` will be used:

```python
# On the Pico REPL
import os
os.remove("/secure_config.json")
```

Then update `config.py` to restore the plaintext secrets temporarily while
you diagnose the issue.

---

## Related Documents

- [Security Controls](security_controls.md) — L1-C7 (Encrypted Configuration Storage)
- [Security Hardening](security_hardening.md) — Full hardening checklist
- [Developer Guide](developer_guide.md) — Module reference for secure_config.py
- [Architecture](architecture.md) — Pico security layer overview
