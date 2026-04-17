# SOMNI-Guard — Encrypted Firmware Storage (Pico 2W)

> **Educational prototype — not a clinically approved device.**

## Table of Contents

1. [Overview](#1-overview)
2. [How It Works](#2-how-it-works)
3. [Key Derivation](#3-key-derivation)
4. [Security Properties](#4-security-properties)
5. [Step-by-Step Deployment](#5-step-by-step-deployment)
6. [Development Mode](#6-development-mode)
7. [Threat Model](#7-threat-model)
8. [Limitations](#8-limitations)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Overview

The SOMNI-Guard Pico 2W firmware uses **AES-256-CBC encrypted at-rest
storage** for all application Python modules.  Every `.py` source file
(except `main.py` and `crypto_loader.py`) is encrypted before deployment
and stored on the Pico's flash filesystem as `.enc` files.

At boot, `main.py` imports `crypto_loader.py` (both stored as plaintext),
which derives a device-bound decryption key and decrypts all modules into
memory for execution.

### Why encrypt firmware?

Without encryption, anyone with physical access to the Pico can extract all
source code, Wi-Fi credentials, HMAC keys, and business logic in seconds
using tools like `picotool save` or the Thonny IDE.  Encrypted firmware
ensures these assets are protected at rest.

### What is protected

| Asset | Protection |
|-------|-----------|
| Wi-Fi credentials (in config.py) | Encrypted at rest |
| HMAC shared key | Encrypted at rest |
| Gateway host/port | Encrypted at rest |
| Sensor drivers and algorithms | Encrypted at rest |
| Transport protocol implementation | Encrypted at rest |
| All business logic | Encrypted at rest |

### What remains as plaintext

| File | Purpose | Contains secrets? |
|------|---------|-------------------|
| `main.py` | Bootstrap — imports crypto_loader and starts the app | No |
| `crypto_loader.py` | Decryption engine — derives key and decrypts modules | No |
| `_salt.bin` | Random salt for key derivation | No (useless without the chip's UID) |
| `manifest.json` | Firmware integrity manifest (if present) | No |

---

## 2. How It Works

### Boot sequence

```
Power on
   │
   ▼
boot.py (plaintext) — USB lockdown layers applied here if usb_locked.flag present
   │
   ▼
main.py (plaintext)
   │
   ├── import crypto_loader (plaintext)
   │
   ├── crypto_loader.load_module_as_object("config")
   │      ├── Read config.enc from flash
   │      ├── Derive key: SHA-256(machine.unique_id() + _salt.bin)
   │      ├── AES-256-CBC decrypt (IV from first 16 bytes)
   │      ├── Remove PKCS7 padding
   │      ├── compile() + exec() the Python source
   │      └── Return as module object
   │
   ├── crypto_loader.load_module_as_object("utils")
   │
   ├── crypto_loader.import_encrypted("drivers/max30102")  ← registered in sys.modules
   ├── crypto_loader.import_encrypted("drivers/adxl345")   ← registered in sys.modules
   ├── crypto_loader.import_encrypted("drivers/gsr")       ← registered in sys.modules
   ├── crypto_loader.import_encrypted("drivers/__init__")  ← depends on above three
   │
   ├── crypto_loader.load_module_as_object("transport")
   ├── crypto_loader.import_encrypted("sampler")           ← depends on drivers being loaded
   │      └── Extract SensorSampler class
   │
   └── Normal operation begins (sampling, Wi-Fi, gateway, etc.)
```

> **Driver load order is critical.** `sampler.enc` contains `from drivers.max30102 import MAX30102`
> at its top level. MicroPython's `exec()` runs those imports when the module is decrypted.
> If the driver modules are not already in `sys.modules` at that point, the import fails.
> The load order above ensures all drivers are registered before `sampler.enc` is exec'd.

### File format

Each `.enc` file has the following binary structure:

```
┌──────────────┬─────────────────────────────────────┐
│ 16 bytes IV  │  AES-256-CBC ciphertext (PKCS7 pad) │
└──────────────┴─────────────────────────────────────┘
```

- **IV**: 16 random bytes generated during encryption (unique per file).
- **Ciphertext**: The Python source file encrypted with AES-256-CBC and
  PKCS7 padding to align to the 16-byte AES block boundary.

### Module loading

`crypto_loader.py` provides two loading functions:

- **`load_module_as_object(name)`** — Returns a namespace object with
  attribute access (e.g. `config.WIFI_SSID`).  Used for modules like
  `config` where attribute-style access is expected.
- **`import_encrypted(name)`** — Returns a dict namespace.  Used when you
  need to extract specific names (e.g. `SensorSampler` class from `sampler`).

Both functions cache results, so repeated imports of the same module do not
trigger re-decryption.

---

## 3. Key Derivation

```
machine.unique_id()     (8 bytes, factory-programmed, unique per chip)
         │
         │   +   _salt.bin   (16 bytes, random, deployed with firmware)
         │
         ▼
     SHA-256( uid || salt )
         │
         ▼
     32 bytes = AES-256 key
```

### Why this is secure

1. **Hardware binding**: `machine.unique_id()` returns a factory-programmed
   serial number unique to each RP2350 chip.  It cannot be changed or read
   without physical access to the specific board.

2. **Salt adds entropy**: The random salt in `_salt.bin` prevents precomputation
   attacks.  Even if an attacker knows the key derivation formula, they need
   BOTH the salt file AND the specific chip to derive the key.

3. **Key never stored**: The AES key exists only transiently in RAM during
   decryption.  It is wiped from memory after each use.

### Key derivation must match

The key derivation in `crypto_loader.py` (Pico, MicroPython) and
`scripts/encrypt_pico_files.py` (developer machine, CPython) must produce
**identical** keys given the same UID and salt:

```python
# Both files compute:
key = hashlib.sha256(uid_bytes + salt_bytes).digest()
```

---

## 4. Security Properties

### What this protects against

| Threat | Protection |
|--------|-----------|
| Physical flash dump (`picotool save`) | Attacker gets encrypted `.enc` blobs — unusable |
| Copy files to another Pico via Thonny | Decryption fails — different `unique_id` = different key |
| Read source code via USB serial (REPL) | `.enc` files are binary; source only exists in RAM |
| Extract Wi-Fi/HMAC credentials | Credentials are in encrypted `config.enc` |
| Reverse-engineer sensor algorithms | Driver source is encrypted |

### What this does NOT protect against

| Threat | Why |
|--------|-----|
| `main.py` and `crypto_loader.py` visible | Required for bootstrap — contain no secrets |
| Attacker with the specific Pico board | Can power it on and the Pico decrypts its own firmware |
| Attacker reads unique_id from the chip | Could derive the key if they also have the salt |
| RAM dump while device is running | Decrypted modules exist in RAM during operation |
| JTAG/SWD debug access | Hardware debug interfaces can read RAM |

---

## 5. Step-by-Step Deployment

### Step 1: Read the Pico's unique ID

Connect the Pico 2W via USB and open a MicroPython REPL:

```bash
mpremote connect /dev/ttyACM0 repl
```

Run in the REPL:

```python
import machine
print(machine.unique_id().hex())
```

Output example: `e660c0d1c7921e28`

Copy this hex string — you will need it for the encryption step.

### Step 2: Encrypt the firmware

From the project root on your developer machine:

```bash
python scripts/encrypt_pico_files.py \
    --uid e660c0d1c7921e28 \
    --src scripts/somniguard_pico/ \
    --out encrypted_deploy/
```

This will:
1. Generate a random `_salt.bin` (or reuse an existing one).
2. Derive the AES-256 key from your Pico's UID + salt.
3. Encrypt all `.py` files to `.enc` files.
4. Copy `main.py` and `crypto_loader.py` as plaintext.
5. Verify each encryption with a round-trip decryption check.

### Step 3: Deploy to the Pico

**Recommended — single UF2 flash (no mpremote required):**

```bash
pip install cryptography littlefs-python

python scripts/somni_uf2_tool.py \
    --uid e660c0d1c7921e28 \
    --src scripts/somniguard_pico/ \
    --firmware somni_guard_firmware.uf2 \
    --out somni_guard_complete.uf2
```

Hold BOOTSEL, plug USB, drag `somni_guard_complete.uf2` to the `RPI-RP2` drive.
Firmware and encrypted files are written in a single operation.

**Alternative — copy via mpremote:**

```bash
mpremote connect /dev/ttyACM0 cp -r encrypted_deploy/. :
```

Or file by file:

```bash
mpremote cp encrypted_deploy/main.py :main.py
mpremote cp encrypted_deploy/crypto_loader.py :crypto_loader.py
mpremote cp encrypted_deploy/_salt.bin :_salt.bin
mpremote cp encrypted_deploy/config.enc :config.enc
mpremote cp encrypted_deploy/utils.enc :utils.enc
mpremote cp encrypted_deploy/transport.enc :transport.enc
mpremote cp encrypted_deploy/sampler.enc :sampler.enc
mpremote cp encrypted_deploy/integrity.enc :integrity.enc
mpremote cp encrypted_deploy/secure_config.enc :secure_config.enc
mpremote mkdir :drivers
mpremote cp encrypted_deploy/drivers/__init__.enc :drivers/__init__.enc
mpremote cp encrypted_deploy/drivers/max30102.enc :drivers/max30102.enc
mpremote cp encrypted_deploy/drivers/adxl345.enc :drivers/adxl345.enc
mpremote cp encrypted_deploy/drivers/gsr.enc :drivers/gsr.enc
```

> Ensure driver `.enc` files land in `drivers/` (not `drivers/drivers/`).
> Verify: `mpremote exec "import os; print(os.listdir('/drivers'))"`

### Step 4: Verify

Reset the Pico and monitor the serial console:

```bash
mpremote connect /dev/ttyACM0 repl
```

You should see:

```
[SOMNI] Encrypted firmware loader available.
[SOMNI][CRYPTO] ========================================
[SOMNI][CRYPTO] Loading encrypted firmware modules...
[SOMNI][CRYPTO] AES engine: ucryptolib
[SOMNI][CRYPTO] ========================================
[SOMNI][CRYPTO] Decrypting 'config.enc'...
[SOMNI][CRYPTO] Decrypted 'config.enc' OK (3421 bytes source).
[SOMNI][CRYPTO] Loaded encrypted module 'config'.
...
[SOMNI] SOMNI-Guard v0.3 — Educational Sleep Monitor
```

---

## 6. Development Mode

For development, you can skip encryption entirely.  The crypto loader
automatically falls back to importing plaintext `.py` files when `.enc`
files are not present.

### Option A: Deploy .py files directly (no encryption)

Simply copy the `.py` files to the Pico as normal.  `crypto_loader.py` will
detect that no `.enc` files exist and import the `.py` files instead.

### Option B: Use --dev-mode flag

The encryption script has a `--dev-mode` flag that copies `.py` files
directly (no encryption):

```bash
python scripts/encrypt_pico_files.py \
    --uid e660c0d1c7921e28 \
    --src scripts/somniguard_pico/ \
    --out dev_deploy/ \
    --dev-mode
```

---

## 7. Threat Model

### Assets protected

- A1: Telemetry payload (indirectly — HMAC key is encrypted)
- A7: Firmware integrity manifest
- All source code and embedded credentials

### Attack scenarios

| Scenario | Outcome |
|----------|---------|
| Attacker steals the Pico and dumps flash | Gets encrypted blobs; cannot recover source or credentials |
| Attacker copies `.enc` files to their own Pico | Decryption fails — different hardware UID |
| Attacker reads `_salt.bin` from flash dump | Useless without the specific chip's UID |
| Attacker reads `main.py` and `crypto_loader.py` | Learns the key derivation algorithm but not the key (UID is not stored on flash) |
| Attacker has both the Pico and the salt | Can derive the key; this is the limitation of software-only encryption |

### Defence-in-depth context

Encrypted firmware storage (L1-C10) works alongside:
- L1-C6: Firmware integrity verification (SHA-256 manifest)
- L1-C7: Encrypted configuration storage (XTEA, for sensitive config values)
- L1-C9: Hardware watchdog timer
- L0-C6: Pi 5 UEFI Secure Boot

---

## 8. Limitations

| Limitation | Impact | Production mitigation |
|-----------|--------|----------------------|
| `main.py` and `crypto_loader.py` are plaintext | Reveals boot logic and key derivation formula | Use RP2350 secure boot (C SDK) to protect all code |
| AES key derivable from UID + salt | Anyone with both the chip and salt can decrypt | Use hardware secure element (ATECC608A) for key storage |
| Decrypted modules exist in RAM | Memory dump reveals source code | Enable RP2350 memory protection (MPU) |
| No code signing on `.enc` files | Attacker could replace `.enc` files with crafted ones | Combine with integrity manifest (L1-C6) to detect tampering |
| ECB mode for key derivation (SHA-256 is fine for this) | N/A — CBC is used for the actual encryption | N/A |

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `RuntimeError: ucryptolib/cryptolib not available` | MicroPython build missing AES support | Reflash with the official Pico 2W MicroPython build |
| `ValueError: Invalid PKCS7 padding` | Wrong key (wrong UID or salt) | Verify UID matches the target Pico; re-encrypt with correct UID |
| `OSError` on `.enc` file | File not deployed | Copy the missing `.enc` file to the Pico |
| Falls back to `.py` imports | No `.enc` files present | Expected in dev mode; run the encryption script for production |
| `[SOMNI][CRYPTO] WARNING: _salt.bin not found` | Salt file not deployed | Copy `_salt.bin` to the Pico filesystem |

---

## Related Documents

- [Security Controls](security_controls.md) — L1-C10 (Encrypted Firmware Storage)
- [Encrypted Storage](encrypted_storage.md) — XTEA encrypted configuration (complementary)
- [Security Hardening](security_hardening.md) — Full hardening checklist
- [Developer Guide](developer_guide.md) — Module reference
- [Architecture](architecture.md) — System architecture
