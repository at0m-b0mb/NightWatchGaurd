# SOMNI-Guard Secure Boot Guide — Raspberry Pi 5

> **Educational prototype — not a clinically approved device.**
> This guide describes security controls appropriate for a student research
> project. The techniques and configurations described here are for educational
> purposes only. Secure Boot setup involves low-level firmware manipulation;
> an incorrect configuration can render your device unbootable. Always maintain
> a recovery path (a second SD card with a working image) before proceeding.
> The authors and SOMNI-Guard project accept NO liability for damage, data
> loss, or bricked hardware resulting from use of these instructions.

---

## Table of Contents

1. [Overview of UEFI Secure Boot on Raspberry Pi 5](#1-overview-of-uefi-secure-boot-on-raspberry-pi-5)
2. [Prerequisites](#2-prerequisites)
3. [Key Concepts](#3-key-concepts)
4. [Automated Setup using setup_secure_boot_pi5.sh](#4-automated-setup-using-setup_secure_boot_pi5sh)
5. [Manual Setup Instructions](#5-manual-setup-instructions)
6. [Verification Steps](#6-verification-steps)
7. [Troubleshooting](#7-troubleshooting)
8. [Rollback Instructions](#8-rollback-instructions)
9. [Security Considerations](#9-security-considerations)

---

## 1. Overview of UEFI Secure Boot on Raspberry Pi 5

### What is Secure Boot?

Secure Boot is a UEFI firmware feature that prevents unauthorised or tampered
boot software from loading during the power-on sequence. It establishes a
**chain of trust** that runs from the moment the firmware starts until the
operating system kernel is loaded.

In the context of SOMNI-Guard, Secure Boot is the **L0-C6** control (see
`docs/security_controls.md`). It mitigates threats H-08 and H-09 (boot chain
tampering and unauthorised firmware modification) from the attack tree.

### Why is it needed for a patient monitor gateway?

A gateway device that handles health sensor data is a high-value target. An
attacker with physical access to the Raspberry Pi 5 could:

- Replace the kernel with a backdoored version that exfiltrates data.
- Insert a bootkit that persists across OS reinstalls.
- Tamper with the GRUB configuration to disable security features at startup.

Secure Boot ensures that **only cryptographically signed binaries** execute
during the boot sequence. If any binary in the chain has been modified — even
by one byte — the firmware will refuse to load it.

### How the Pi 5 Secure Boot chain works

The Raspberry Pi 5 uses the **pftf/RPi5** open-source UEFI firmware to gain
standard UEFI capabilities, including Secure Boot. The boot flow is:

```
Power on
    └─► Pi 5 EEPROM (first-stage bootloader — not covered by Secure Boot)
            └─► UEFI firmware (RPI_EFI.fd — verifies Secure Boot keys)
                    └─► GRUB or systemd-boot (must be signed with db key)
                            └─► Linux kernel (must be signed with db key)
                                    └─► SOMNI-Guard gateway service
```

The UEFI firmware checks each PE/COFF binary against the key database before
executing it. Binaries not signed by a trusted key are rejected.

### How SOMNI-Guard uses Secure Boot

SOMNI-Guard generates its own key hierarchy (PK, KEK, db) using the provided
setup script. These keys are unique to each deployment — there are no shared
or default keys. The setup script:

1. Generates RSA key pairs for PK, KEK, and db.
2. Signs the bootloader (GRUB) and kernel with the db private key.
3. Enrols the public keys into UEFI NVRAM.
4. Installs a systemd service that verifies Secure Boot status at every boot.

---

## 2. Prerequisites

### Hardware Requirements

| Item | Requirement |
|------|-------------|
| Raspberry Pi 5 | 4 GB or 8 GB RAM (BCM2712 SoC) |
| MicroSD card | 32 GB or larger, Class 10 / A2 rated |
| USB-C power supply | Official Pi 5 supply (5 V / 5 A) recommended |
| Monitor + keyboard | Required for UEFI menu interaction during first setup |
| Second SD card | Strongly recommended as recovery media |

### Software Requirements

The following Debian/Ubuntu packages must be installed on the Pi 5 before
running the setup script:

```bash
sudo apt-get update && sudo apt-get install -y \
    openssl \
    efitools \
    sbsigntool \
    mokutil \
    efibootmgr \
    wget \
    unzip
```

| Package | Purpose |
|---------|---------|
| `openssl` | RSA key and X.509 certificate generation |
| `efitools` | `cert-to-efi-sig-list`, `sign-efi-sig-list`, `efi-updatevar`, `efi-readvar` |
| `sbsigntool` | `sbsign` (sign PE/COFF binaries), `sbverify` (verify signatures) |
| `mokutil` | Query and manage Secure Boot / MokManager keys |
| `efibootmgr` | Manage EFI boot entries |
| `wget` + `unzip` | Download and extract the pftf/RPi5 UEFI firmware |

### Operating System

- **Raspberry Pi OS Bookworm** (64-bit, arm64) is the recommended and tested
  base OS. The script may work on Ubuntu for Pi 5 but has not been validated.
- The OS must be installed and running before configuring Secure Boot.

### pftf/RPi5 UEFI Firmware

Standard Raspberry Pi OS does not ship with UEFI firmware. The setup script
automatically downloads and installs the
[pftf/RPi5 UEFI firmware](https://github.com/pftf/RPi5) onto the FAT32 boot
partition. If you prefer to install it manually first, download the latest
release ZIP from the pftf/RPi5 GitHub releases page and extract all files to
`/boot/firmware/`.

---

## 3. Key Concepts

### The UEFI Secure Boot Key Hierarchy

UEFI Secure Boot uses a three-layer key hierarchy defined in the UEFI
Specification §32. Understanding this hierarchy is essential for troubleshooting
and for understanding the security properties of the configuration.

#### PK — Platform Key

The PK is the **root of trust** for a UEFI platform. There can be only one PK.
Enrolling a PK transitions the firmware from **Setup Mode** (no enforcement)
to **User Mode** (Secure Boot active). The PK private key is used to sign
updates to the KEK. After enrolment, the PK private key should be moved **off
the device** to a secure offline location — it is not needed for day-to-day
operation.

In SOMNI-Guard, the PK is stored at `/etc/somniguard/secure-boot/keys/PK.key`
(private) and `PK.crt` (public certificate).

#### KEK — Key Exchange Key

The KEK authorises updates to the Signature Database (db) and the Forbidden
Signature Database (dbx) without requiring the PK private key. On commercial
PC hardware, Microsoft and OEM keys appear in the KEK. In SOMNI-Guard, the
project generates its own KEK, giving full control over the key hierarchy.

#### db — Signature Database

The db is the **allowlist** of trusted signing certificates and binary hashes.
A bootloader or kernel EFI binary is permitted to execute if:
- Its PE/COFF signature chains to a certificate in db, **or**
- Its SHA-256 hash appears directly in db.

SOMNI-Guard signs the bootloader and kernel with the db private key and enrols
the db public certificate into UEFI NVRAM.

#### dbx — Forbidden Signature Database

The dbx is the **revocation list** — binaries or certificates listed here are
explicitly denied even if they would otherwise be permitted by db. The
SOMNI-Guard setup script does not configure dbx (it is empty), which is
acceptable for an embedded gateway where all software is locally controlled.

### EFI Signature List (ESL) and .auth Files

UEFI stores keys in a binary format called an **EFI Signature List** (`.esl`
file). To write a key into UEFI NVRAM, the update payload must itself be
cryptographically signed, producing an **authenticated update** (`.auth` file).
This prevents an attacker from replacing the enrolled keys even if they have
brief physical access to a running system.

### Signed PE/COFF Binaries

UEFI Secure Boot uses the **Authenticode** standard (PE/COFF with a signature
embedded in the binary) to sign EFI executables. The `sbsign` tool creates
Authenticode signatures compatible with UEFI. `sbverify` can check them without
needing a running UEFI environment.

### Setup Mode vs User Mode

| Mode | PK status | Secure Boot enforcement |
|------|-----------|------------------------|
| Setup Mode | No PK enrolled | Off — any binary can load |
| User Mode | PK enrolled | On — only signed binaries load |

A firmware freshly flashed with the pftf/RPi5 image starts in Setup Mode.
Enrolling the PK (the final step in key enrolment) transitions to User Mode
and activates enforcement.

---

## 4. Automated Setup using setup_secure_boot_pi5.sh

The script `scripts/setup_secure_boot_pi5.sh` automates the entire Secure Boot
configuration process. It is the recommended method for SOMNI-Guard deployments.

### Running the script

```bash
# Clone the repository on the Pi 5
git clone https://github.com/at0m-b0mb/NightWatchGaurd.git
cd NightWatchGaurd

# Make the script executable
chmod +x scripts/setup_secure_boot_pi5.sh

# Run as root (required for firmware and NVRAM access)
sudo bash scripts/setup_secure_boot_pi5.sh
```

### Script options

| Option | Description |
|--------|-------------|
| `--dry-run` | Print every step without making any changes. Safe to run on any hardware. |
| `--verify-only` | Check current Secure Boot status and exit. No modifications. |
| `--help` | Show usage information. |

```bash
# Simulate setup without making changes (good for review)
sudo bash scripts/setup_secure_boot_pi5.sh --dry-run

# Check Secure Boot status after reboot
sudo bash scripts/setup_secure_boot_pi5.sh --verify-only
```

### Environment variable overrides

| Variable | Default | Description |
|----------|---------|-------------|
| `SOMNIGUARD_ORG_NAME` | `SOMNI-Guard` | Organisation name in generated certificates |
| `SOMNIGUARD_ORG_UNIT` | `NightWatchGuard` | Organisational unit in certificates |

### What the script does — step by step

The script executes eight steps in sequence:

**Step 0 — Pre-flight checks**
- Verifies the script is running as root.
- Confirms the hardware is a Raspberry Pi 5 (reads `/proc/cpuinfo`).
- Checks that all required tools (`openssl`, `efitools`, `sbsigntool`, etc.)
  are installed. Fails with a clear error listing missing packages.
- Initialises the log file at `/var/log/somniguard/secure_boot_setup.log`.

**Step 1 — Generate Secure Boot key pairs**
- Generates a random UUID to identify the key owner in EFI payloads.
- Creates RSA-2048 key pairs for PK, KEK, and db under
  `/etc/somniguard/secure-boot/keys/` (mode 700).
- Converts each X.509 certificate to EFI Signature List (`.esl`) format.
- Signs each ESL with the appropriate parent key to create authenticated
  update payloads (`.auth` files): PK self-signs, KEK is signed by PK,
  db is signed by KEK.

**Step 2 — Sign the boot chain**
- Locates the Linux kernel (`/boot/vmlinuz`, `/boot/Image`, or
  `/boot/firmware/kernel8.img`).
- Signs the kernel with `sbsign` using the db private key.
- Locates and signs the bootloader (GRUB at
  `/boot/efi/EFI/debian/grubaa64.efi` or systemd-boot).
- Replaces the unsigned bootloader with the signed version in-place.
- Signs the UEFI Shell if present.

**Step 3 — Configure Pi 5 boot EEPROM**
- Reads the current EEPROM configuration with `rpi-eeprom-config`.
- Sets `BOOT_ORDER=0xf16` (NVMe → SD → restart) to ensure the UEFI
  firmware image is found.

**Step 4 — UEFI firmware settings notes**
- Prints guidance on the UEFI menu settings to apply after reboot.

**Step 5 — Install pftf/RPi5 UEFI firmware**
- Downloads the UEFI firmware from GitHub if not already present.
- Backs up existing boot partition files to
  `/etc/somniguard/secure-boot/backup_<timestamp>/boot/`.
- Extracts firmware files to the FAT32 boot partition (`/boot/firmware/`).
- Creates the EFI directory structure (`/boot/efi/EFI/BOOT/`,
  `/boot/efi/EFI/somniguard/`).

**Step 6 — Enrol keys in UEFI NVRAM**
- Writes `db.auth`, `KEK.auth`, and `PK.auth` to UEFI NVRAM using
  `efi-updatevar`. PK is written last, activating Secure Boot enforcement.

**Step 7 — Verification**
- Runs `mokutil --sb-state` to check enforcement status.
- Runs `sbverify` to confirm the signed kernel signature.
- Reads EFI variables with `efi-readvar`.
- Lists enrolled certificates with `mokutil --list-enrolled`.

**Step 8 — Install systemd verification service**
- Installs `/usr/local/bin/somniguard-sb-verify.sh` and the systemd unit
  `somniguard-secure-boot-verify.service`.
- Enables the service so it runs at every boot, logging Secure Boot status
  to `/var/log/somniguard/secure_boot_status.log`.

### After running the script

1. Review the output and log file at
   `/var/log/somniguard/secure_boot_setup.log` for any warnings.
2. **Move the PK private key off the device:**
   ```bash
   # Copy to a secure offline location, then delete from the Pi
   scp /etc/somniguard/secure-boot/keys/PK.key user@secure-machine:~/somni-pk-backup/
   sudo shred -u /etc/somniguard/secure-boot/keys/PK.key
   ```
3. Reboot and enter the UEFI setup (press **ESC** at the boot splash):
   - Navigate to **Security → Secure Boot**.
   - Confirm keys are enrolled.
   - Enable **Secure Boot Enforcement**.
4. After reboot, verify Secure Boot is active:
   ```bash
   sudo bash scripts/setup_secure_boot_pi5.sh --verify-only
   ```

---

## 5. Manual Setup Instructions

If you prefer not to use the automated script, follow these steps manually.
The automated script is a thin wrapper around these same commands.

### Step 1: Install required packages

```bash
sudo apt-get update && sudo apt-get install -y \
    openssl efitools sbsigntool mokutil efibootmgr wget unzip
```

### Step 2: Install pftf/RPi5 UEFI firmware

```bash
# Download the UEFI firmware (check GitHub for the latest version)
UEFI_VERSION="v0.3"
wget -O /tmp/RPi5_UEFI.zip \
    "https://github.com/pftf/RPi5/releases/download/${UEFI_VERSION}/RPi5_UEFI_Firmware_${UEFI_VERSION}.zip"

# Back up boot partition files
sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.bak

# Extract to the boot partition
sudo unzip -o /tmp/RPi5_UEFI.zip -d /boot/firmware/
```

### Step 3: Generate key pairs

```bash
KEY_DIR="/etc/somniguard/secure-boot/keys"
sudo mkdir -p "$KEY_DIR"
sudo chmod 700 "$KEY_DIR"

# Generate a unique GUID for this deployment
GUID="$(uuidgen --random)"
echo "$GUID" | sudo tee "${KEY_DIR}/owner-guid.txt"

# Generate PK (Platform Key)
sudo openssl req -newkey rsa:2048 -nodes \
    -keyout "${KEY_DIR}/PK.key" \
    -new -x509 -sha256 -days 3650 \
    -subj "/O=SOMNI-Guard/OU=NightWatchGuard/CN=SOMNI-Guard Platform Key/" \
    -out "${KEY_DIR}/PK.crt"

# Generate KEK (Key Exchange Key)
sudo openssl req -newkey rsa:2048 -nodes \
    -keyout "${KEY_DIR}/KEK.key" \
    -new -x509 -sha256 -days 3650 \
    -subj "/O=SOMNI-Guard/OU=NightWatchGuard/CN=SOMNI-Guard Key Exchange Key/" \
    -out "${KEY_DIR}/KEK.crt"

# Generate db (Signature Database)
sudo openssl req -newkey rsa:2048 -nodes \
    -keyout "${KEY_DIR}/db.key" \
    -new -x509 -sha256 -days 3650 \
    -subj "/O=SOMNI-Guard/OU=NightWatchGuard/CN=SOMNI-Guard Signature Database/" \
    -out "${KEY_DIR}/db.crt"

# Set permissions
sudo chmod 600 "${KEY_DIR}/"*.key
sudo chmod 644 "${KEY_DIR}/"*.crt
```

### Step 4: Convert certificates to EFI Signature Lists

```bash
GUID="$(cat ${KEY_DIR}/owner-guid.txt)"

sudo cert-to-efi-sig-list -g "$GUID" "${KEY_DIR}/PK.crt"  "${KEY_DIR}/PK.esl"
sudo cert-to-efi-sig-list -g "$GUID" "${KEY_DIR}/KEK.crt" "${KEY_DIR}/KEK.esl"
sudo cert-to-efi-sig-list -g "$GUID" "${KEY_DIR}/db.crt"  "${KEY_DIR}/db.esl"
```

### Step 5: Sign EFI Signature Lists (create .auth files)

```bash
# PK self-signs
sudo sign-efi-sig-list \
    -k "${KEY_DIR}/PK.key" -c "${KEY_DIR}/PK.crt" \
    PK "${KEY_DIR}/PK.esl" "${KEY_DIR}/PK.auth"

# KEK signed by PK
sudo sign-efi-sig-list \
    -k "${KEY_DIR}/PK.key" -c "${KEY_DIR}/PK.crt" \
    KEK "${KEY_DIR}/KEK.esl" "${KEY_DIR}/KEK.auth"

# db signed by KEK
sudo sign-efi-sig-list \
    -k "${KEY_DIR}/KEK.key" -c "${KEY_DIR}/KEK.crt" \
    db "${KEY_DIR}/db.esl" "${KEY_DIR}/db.auth"
```

### Step 6: Sign the boot chain

```bash
# Sign the kernel
sudo sbsign \
    --key  "${KEY_DIR}/db.key" \
    --cert "${KEY_DIR}/db.crt" \
    --output /boot/vmlinuz.signed \
    /boot/vmlinuz

# Sign the GRUB bootloader (adjust path for your system)
GRUB_PATH="/boot/efi/EFI/debian/grubaa64.efi"
sudo sbsign \
    --key  "${KEY_DIR}/db.key" \
    --cert "${KEY_DIR}/db.crt" \
    --output "${GRUB_PATH}.signed" \
    "$GRUB_PATH"
sudo cp "${GRUB_PATH}.signed" "$GRUB_PATH"
```

### Step 7: Enrol keys in UEFI NVRAM

The firmware must be in Setup Mode (no PK enrolled) for this to work without
requiring signed payloads. If you see an error, try clearing existing keys in
the UEFI menu first (Security → Secure Boot → Reset Secure Boot Keys).

```bash
# Enrol in order: db first, then KEK, then PK last
sudo efi-updatevar -e -f "${KEY_DIR}/db.auth"  db
sudo efi-updatevar -e -f "${KEY_DIR}/KEK.auth" KEK
sudo efi-updatevar    -f "${KEY_DIR}/PK.auth"  PK
```

### Step 8: Install the boot-verification systemd service

Create `/etc/systemd/system/somniguard-secure-boot-verify.service` and
`/usr/local/bin/somniguard-sb-verify.sh` as described in the automated script,
or run the script with `--dry-run` to see the exact file contents.

---

## 6. Verification Steps

Run these checks after the initial setup and after every reboot.

### Check Secure Boot enforcement status

```bash
# Method 1: mokutil
mokutil --sb-state
# Expected output: "SecureBoot enabled"

# Method 2: kernel parameter
cat /sys/firmware/efi/efivars/SecureBoot-*/
# A non-zero value in byte 5 means Secure Boot is active

# Method 3: SOMNI-Guard verification script
sudo bash scripts/setup_secure_boot_pi5.sh --verify-only

# Method 4: Check the SOMNI-Guard systemd service
systemctl status somniguard-secure-boot-verify.service
```

### Verify a signed binary

```bash
# Verify the signed kernel
sbverify --cert /etc/somniguard/secure-boot/keys/db.crt /boot/vmlinuz.signed
# Expected output: "Signature verification OK"

# Verify the signed GRUB
sbverify --cert /etc/somniguard/secure-boot/keys/db.crt \
    /boot/efi/EFI/debian/grubaa64.efi
```

### Inspect enrolled UEFI keys

```bash
# List all enrolled keys in UEFI variables
efi-readvar

# List Machine Owner Keys (MOK) and db certificates
mokutil --list-enrolled

# Check individual variables
efi-readvar -v PK
efi-readvar -v KEK
efi-readvar -v db
```

### Check the boot verification log

```bash
# View the most recent Secure Boot status check
cat /var/log/somniguard/secure_boot_status.log

# View the full setup log
cat /var/log/somniguard/secure_boot_setup.log
```

---

## 7. Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `mokutil --sb-state` shows `SecureBoot disabled` | Secure Boot not enforced in UEFI menu | Enter UEFI setup (ESC at boot), enable enforcement under Security → Secure Boot |
| System will not boot after key enrolment | Bootloader or kernel not signed, or signed with wrong key | Boot from recovery SD card; see Section 8 (Rollback) |
| `sbverify` fails: "signature verification failed" | Binary was modified after signing or signed with a different key | Re-sign the binary: `sudo sbsign --key db.key --cert db.crt --output <output> <input>` |
| `efi-updatevar` returns "No space left on device" | UEFI NVRAM full | Clear existing keys in UEFI menu first |
| `efi-updatevar` returns "Permission denied" | Firmware in User Mode (PK already enrolled) | Use the `.auth` signed payloads; or clear keys in UEFI menu and re-enrol |
| GRUB prompts for key enrolment on first boot | Signed bootloader not replacing unsigned one correctly | Confirm `sbsign` output was copied over the original: `cp grubaa64.efi.signed grubaa64.efi` |
| `setup_secure_boot_pi5.sh` fails at Pi 5 hardware check | Script run on non-Pi5 hardware | Use `--dry-run` flag for testing on other hardware |
| UEFI firmware not found at `/boot/firmware/RPI_EFI.fd` | pftf/RPi5 firmware not installed | Let the script install it (Step 5), or manually download and extract from GitHub |
| `rpi-eeprom-config` command not found | EEPROM tools not installed | `sudo apt-get install -y rpi-eeprom` |
| Secure Boot status shows "unknown" | `mokutil` not available inside container/CI | Expected in virtualised environments; ignore for development |

### Checking if a binary is signed

```bash
# Show all signatures on a PE/COFF binary
objdump -p /boot/efi/EFI/debian/grubaa64.efi | grep -A5 "Certificate Table"

# Alternative: sbverify with --no-verify to just show cert info
sbverify --no-verify /boot/vmlinuz.signed
```

### Recovering from a bad boot after key enrolment

If the Pi 5 fails to boot after enrolling Secure Boot keys:

1. Power off the Pi 5.
2. Insert a recovery SD card with a fresh Raspberry Pi OS image.
3. Boot from the recovery card.
4. Mount the original SD card and restore backed-up firmware files from
   `/etc/somniguard/secure-boot/backup_<timestamp>/boot/`.
5. (Optional) Re-sign binaries correctly and re-enrol keys.

---

## 8. Rollback Instructions

### Scenario 1: Disable Secure Boot without clearing keys

If Secure Boot is causing boot failures but you want to keep the enrolled keys:

1. Enter UEFI setup: press **ESC** during the early boot splash screen.
2. Navigate to **Security → Secure Boot**.
3. Set **Secure Boot Mode** to **Disabled**.
4. Save and exit. The system will boot without enforcement.
5. Diagnose and fix the signing issue, then re-enable enforcement.

### Scenario 2: Clear all Secure Boot keys

To remove all enrolled keys and return the firmware to Setup Mode:

1. Enter UEFI setup (press **ESC** at boot splash).
2. Navigate to **Security → Secure Boot → Reset Secure Boot Keys** (or
   **Delete All Secure Boot Keys**).
3. Confirm. The firmware returns to Setup Mode.
4. Re-run `setup_secure_boot_pi5.sh` to enrol fresh keys.

### Scenario 3: Restore from recovery SD card

If the system is completely unbootable:

1. Flash a known-good Raspberry Pi OS image to a spare SD card using
   Raspberry Pi Imager.
2. Insert the recovery SD card into the Pi 5 and boot from it.
3. Mount the original (failing) SD card as a USB mass storage device or via
   a card reader.
4. Restore the backup from the setup script:
   ```bash
   # Assuming the original SD card is mounted at /mnt/sdcard
   sudo cp /etc/somniguard/secure-boot/backup_*/boot/* /mnt/sdcard/boot/firmware/
   ```
5. Remove the recovery SD card and reboot with the original.

### Scenario 4: Re-flash UEFI firmware

If the UEFI firmware image itself is corrupt:

1. Boot from the recovery SD card (Step 3 above).
2. Download a fresh pftf/RPi5 firmware ZIP from GitHub.
3. Mount the original boot partition.
4. Extract the firmware ZIP onto the FAT32 boot partition.

### Removing the SOMNI-Guard systemd service

```bash
sudo systemctl stop somniguard-secure-boot-verify.service
sudo systemctl disable somniguard-secure-boot-verify.service
sudo rm /etc/systemd/system/somniguard-secure-boot-verify.service
sudo rm /usr/local/bin/somniguard-sb-verify.sh
sudo systemctl daemon-reload
```

### Removing generated keys

```bash
# WARNING: this removes the ability to sign new binaries for this deployment
sudo rm -rf /etc/somniguard/secure-boot/keys/
```

---

## 9. Security Considerations

### PK private key management

The Platform Key private key (`PK.key`) is the root of trust for the entire
Secure Boot configuration. Once key enrolment is complete:

- **Remove `PK.key` from the device.** Store it on an encrypted USB drive in
  a physically secured location (not in the same place as the Pi 5).
- An attacker who obtains `PK.key` can sign arbitrary binaries and enrol
  them into the UEFI key database, completely bypassing Secure Boot.
- The `db.key` is needed to sign new binaries (e.g., after a kernel update).
  It can remain on the device with restricted permissions (mode 600, owned by
  root), but should ideally also be stored offline.

### Key rotation

Rotate Secure Boot keys annually or when any key material is suspected to be
compromised. Rotation requires:
1. Generating a new db key pair.
2. Signing new binaries with the new db key.
3. Updating the db EFI variable using a KEK-signed payload.
4. No reboot is required for key enrolment; a reboot is required for the
   new signed binaries to be the active versions.

### Kernel update procedure

Every time the Linux kernel is updated, the new kernel binary must be signed
before Secure Boot will allow it to load:

```bash
# After running: sudo apt-get upgrade
sudo sbsign \
    --key  /etc/somniguard/secure-boot/keys/db.key \
    --cert /etc/somniguard/secure-boot/keys/db.crt \
    --output /boot/vmlinuz.signed \
    /boot/vmlinuz
```

Consider automating this with a DPKG post-install hook or systemd-boot's
unified kernel image workflow.

### EEPROM is not covered by Secure Boot

The Raspberry Pi 5 boot EEPROM (the first-stage bootloader embedded in the
SoC) runs before the UEFI layer and is **not** protected by UEFI Secure Boot.
Physical access to the EEPROM flash chip could theoretically allow a
sophisticated attacker to bypass Secure Boot entirely. This is a hardware-level
limitation common to all ARM-based embedded systems that lack a hardware root
of trust (e.g., TPM 2.0, TrustZone OP-TEE). For SOMNI-Guard's threat model,
this risk is accepted as a known limitation.

### Secure Boot complements, does not replace, other controls

Secure Boot protects the boot chain. It does not:
- Protect data at rest (use LUKS2 and SQLCipher — see `docs/security_controls.md`).
- Protect network traffic (use Tailscale — see `docs/tailscale_setup.md`).
- Protect against a compromised running OS.

All security controls in SOMNI-Guard are designed to be **layered**. Secure
Boot is one layer in a defence-in-depth architecture.

### SOMNI-Guard is an educational prototype

The key sizes (RSA-2048) and algorithms used in this guide were chosen for
broad firmware compatibility. A production medical device would require:
- RSA-4096 or ECC P-384 keys.
- Hardware Security Module (HSM) key storage.
- Formal key ceremony documentation.
- Regulatory validation (IEC 62443, FDA cybersecurity guidance).

This configuration is appropriate for a university research project demonstrating
the *principles* of Secure Boot in an embedded health monitoring context.
