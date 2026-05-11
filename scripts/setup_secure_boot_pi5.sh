#!/usr/bin/env bash
# =============================================================================
# SOMNI-Guard Gateway — Raspberry Pi 5 UEFI Secure Boot Setup
# =============================================================================
#
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# EDUCATIONAL PROTOTYPE DISCLAIMER
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# This script is an EDUCATIONAL PROTOTYPE provided for learning and
# demonstration purposes only. It is NOT intended for use in production
# environments without thorough review, testing, and adaptation to your
# specific security requirements and threat model.
#
# Secure Boot setup involves low-level firmware manipulation. An incorrect
# configuration can render your device unbootable. Always maintain a recovery
# path (e.g., a second SD card with a working image) before proceeding.
#
# The authors and SOMNI-Guard project accept NO liability for damage, data
# loss, or bricked hardware resulting from use of this script.
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
#
# PURPOSE
# -------
# This script automates the configuration of UEFI Secure Boot on a
# Raspberry Pi 5 running the worproject/rpi5-uefi firmware. Secure Boot ensures
# that only cryptographically signed bootloaders and kernels are executed,
# providing a hardware-rooted chain of trust from power-on to the OS.
#
# SECURE BOOT KEY HIERARCHY (UEFI spec §32)
# -----------------------------------------
#   PK  (Platform Key)        — Ultimate root of trust. Signed by the owner.
#   KEK (Key Exchange Key)    — Authorises updates to the Signature Database.
#   db  (Signature Database)  — Contains keys/hashes of allowed boot images.
#   dbx (Forbidden Signature) — Revocation list (not configured here).
#
# USAGE
#   sudo bash setup_secure_boot_pi5.sh [OPTIONS]
#
# OPTIONS
#   --dry-run       Print what would be done without making any changes.
#   --verify-only   Check the current Secure Boot status and exit.
#   --help          Show this help and exit.
#
# REQUIREMENTS
#   - Raspberry Pi 5 running Raspberry Pi OS (Bookworm or later)
#   - worproject/rpi5-uefi firmware installed on the boot partition
#   - Packages: openssl efitools sbsigntool grub-efi-arm64-signed (or equivalent)
#   - Internet access (to download UEFI firmware if not already installed)
#
# NOTE ON PERMISSIONS
#   After downloading, make this script executable with:
#     chmod +x setup_secure_boot_pi5.sh
#
# LOGGING
#   All output is tee'd to /var/log/somniguard/secure_boot_setup.log
#
# ROLLBACK
#   See the "ROLLBACK INSTRUCTIONS" section near the bottom of this script.
#
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Global constants
# ---------------------------------------------------------------------------

readonly SCRIPT_VERSION="1.0.0"
readonly SCRIPT_NAME="$(basename "$0")"
readonly TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

# Directories
readonly KEY_DIR="/etc/somniguard/secure-boot/keys"
readonly LOG_DIR="/var/log/somniguard"
readonly LOG_FILE="${LOG_DIR}/secure_boot_setup.log"
readonly BACKUP_DIR="/etc/somniguard/secure-boot/backup_${TIMESTAMP}"
# EFI mount point.
# Raspberry Pi OS (Bookworm) mounts the FAT32 boot/EFI partition at
# /boot/firmware — NOT /boot/efi as on a typical Debian PC.  The
# worproject/rpi5-uefi firmware (RPI_EFI.fd) and its EFI/BOOT/BOOTAA64.EFI
# payload live on that same partition.  Auto-detect, with an override
# hook for custom layouts.
_efi_mount_default="/boot/firmware"
if [[ ! -d "$_efi_mount_default" && -d "/boot/efi" ]]; then
    _efi_mount_default="/boot/efi"
fi
readonly EFI_MOUNT="${SOMNI_EFI_MOUNT:-$_efi_mount_default}"

# UEFI firmware source — worproject/rpi5-uefi.
#
# History: an earlier revision of this script pointed at "pftf/RPi5" by
# analogy with pftf/RPi4 (the actively maintained Pi 4 UEFI project).
# That repo does NOT exist — the Pi 5 UEFI port lives at
# https://github.com/worproject/rpi5-uefi.  As of 2025 the project is
# archived and v0.3 is the terminal release; the asset filename pattern
# is "RPi5_UEFI_Release_v<ver>.zip" (note: "Release", not "Firmware").
#
# Override via SOMNI_UEFI_VERSION if a fork resumes maintenance.
readonly UEFI_FIRMWARE_VERSION="${SOMNI_UEFI_VERSION:-v0.3}"
readonly UEFI_FIRMWARE_URL="https://github.com/worproject/rpi5-uefi/releases/download/${UEFI_FIRMWARE_VERSION}/RPi5_UEFI_Release_${UEFI_FIRMWARE_VERSION}.zip"
readonly UEFI_TMP_DIR="/tmp/somniguard_uefi_$$"

# Key parameters
# RSA-2048 is the UEFI Secure Boot minimum; RSA-4096 is preferred for new
# deployments. We use 2048 here for broad firmware compatibility.
readonly KEY_BITS=2048
readonly KEY_DAYS=3650  # ~10 years

# Colours (disabled if not a terminal)
if [[ -t 1 ]]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
else
    RED=''; YELLOW=''; GREEN=''; CYAN=''; BOLD=''; RESET=''
fi

# Runtime flags (overridden by CLI options)
DRY_RUN=false
VERIFY_ONLY=false

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

_log() {
    local level="$1"; shift
    local colour="$1"; shift
    local msg="$*"
    local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"
    printf "${colour}[%s] [%-7s] %s${RESET}\n" "$ts" "$level" "$msg" | tee -a "$LOG_FILE"
}

info()    { _log "INFO"    "$GREEN"  "$@"; }
warn()    { _log "WARN"    "$YELLOW" "$@"; }
error()   { _log "ERROR"   "$RED"    "$@"; }
step()    { _log "STEP"    "$CYAN"   "$@"; }
verbose() { _log "VERBOSE" "$RESET"  "$@"; }

die() {
    error "$*"
    error "Setup FAILED. Review $LOG_FILE for details."
    error "See the ROLLBACK INSTRUCTIONS at the bottom of this script."
    exit 1
}

# Run a command, or just echo it in dry-run mode.
run() {
    if $DRY_RUN; then
        info "[DRY-RUN] Would run: $*"
    else
        verbose "Running: $*"
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

print_banner() {
    printf "${BOLD}${CYAN}"
    cat <<'BANNER'
 ___  ___  __  __ _  _ ___     ___                     _
/ __|/ _ \|  \/  | \| |_ _|   / __|_  _ __ _ _ __ _ __| |
\__ \ (_) | |\/| | .` || |   | (_ | || / _` | '__| / _` |
|___/\___/|_|  |_|_|\_|___|   \___|\_,_\__,_|_|  |_\__,_|

       N i g h t W a t c h G u a r d  —  S e c u r e  B o o t
       Raspberry Pi 5 UEFI Setup  |  SOMNI-Guard Gateway
BANNER
    printf "${RESET}\n"
    info "Script version : $SCRIPT_VERSION"
    info "Timestamp      : $TIMESTAMP"
    info "Log file       : $LOG_FILE"
    echo
}

# ---------------------------------------------------------------------------
# Usage / help
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
Usage: sudo bash $SCRIPT_NAME [OPTIONS]

Options:
  --dry-run       Show what would be done; make no changes.
  --verify-only   Check current Secure Boot status and exit.
  --help          Show this help message and exit.

Environment variables:
  SOMNIGUARD_ORG_NAME   Override the organisation name in generated certificates
                        (default: "SOMNI-Guard")
  SOMNIGUARD_ORG_UNIT   Override the organisational unit (default: "NightWatchGuard")
  SOMNI_UEFI_VERSION    worproject/rpi5-uefi release tag to download
                        (default: "v0.3").  Check
                        https://github.com/worproject/rpi5-uefi/releases
                        for forks if the upstream project resumes
                        maintenance after its early-2025 archive.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run)      DRY_RUN=true ;;
            --verify-only)  VERIFY_ONLY=true ;;
            --help|-h)      usage; exit 0 ;;
            *)              die "Unknown option: $1  (use --help for usage)" ;;
        esac
        shift
    done
}

# ---------------------------------------------------------------------------
# STEP 0 — Pre-flight checks
# ---------------------------------------------------------------------------

# 0a. Root / sudo check
check_root() {
    step "0a. Checking for root privileges"
    # Secure Boot setup requires writing to firmware partitions and system
    # directories — root is mandatory.
    if [[ $EUID -ne 0 ]]; then
        die "This script must be run as root.  Try: sudo bash $SCRIPT_NAME"
    fi
    info "Running as root — OK"
}

# 0b. Ensure the log directory exists before we start writing to it.
init_logging() {
    mkdir -p "$LOG_DIR"
    touch "$LOG_FILE"
    chmod 640 "$LOG_FILE"
}

# 0c. Verify we are on a Raspberry Pi 5.
# The Pi 5 introduces the BCM2712 SoC. We check /proc/cpuinfo for the model
# string and the device-tree compatible string, both of which are reliable
# identifiers set by the firmware at boot.
check_pi5() {
    step "0c. Verifying hardware: Raspberry Pi 5"

    local model_line
    model_line="$(grep -m1 'Model' /proc/cpuinfo 2>/dev/null || true)"

    if [[ "$model_line" != *"Raspberry Pi 5"* ]]; then
        # Also accept if running inside a container/VM for dry-run testing.
        if $DRY_RUN; then
            warn "Not running on Pi 5 (detected: ${model_line:-unknown}). Continuing because --dry-run is active."
        else
            die "This script is designed for Raspberry Pi 5 only.\nDetected: ${model_line:-unknown}\nUse --dry-run to simulate execution on other hardware."
        fi
    else
        info "Hardware check passed: $model_line"
    fi

    # Also check the UEFI firmware is already installed (we need the EFI partition).
    if [[ ! -d "$EFI_MOUNT" ]]; then
        warn "EFI partition not mounted at $EFI_MOUNT."
        warn "If you have not yet installed the worproject/rpi5-uefi firmware, this"
        warn "script will attempt to install it in STEP 5."
    else
        info "EFI partition found at $EFI_MOUNT — OK"
    fi
}

# 0d. Check all required tools are installed.
# We use a whitelist approach: list every binary we will call and fail early
# rather than encountering a missing command mid-setup.
check_required_tools() {
    step "0d. Checking required tools"

    local -a required_tools=(
        # Key generation
        openssl          # RSA key and certificate generation (OpenSSL)

        # UEFI Secure Boot tooling
        cert-to-efi-sig-list   # efitools: convert X.509 cert → EFI sig list
        sign-efi-sig-list      # efitools: sign an EFI sig list with a key
        efi-updatevar          # efitools: write EFI variables to NVRAM
        efi-readvar            # efitools: read EFI variables from NVRAM

        # Binary signing
        sbsign                 # sbsigntool: sign PE/COFF EFI binaries
        sbverify               # sbsigntool: verify a signed EFI binary

        # Boot-status queries
        mokutil                # MokManager: query/manage Secure Boot keys

        # Pi 5 EEPROM management (install with: sudo apt-get install rpi-eeprom)
        rpi-eeprom-config

        # Firmware download & extraction
        wget
        unzip

        # Miscellaneous
        uuidgen
    )

    local missing=()
    for tool in "${required_tools[@]}"; do
        if ! command -v "$tool" &>/dev/null; then
            missing+=("$tool")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        error "The following required tools are missing:"
        for t in "${missing[@]}"; do
            error "  - $t"
        done
        error ""
        error "Install them with:"
        error "  sudo apt-get update && sudo apt-get install -y \\"
        error "    openssl efitools sbsigntool mokutil wget unzip rpi-eeprom uuid-runtime"
        die "Missing required tools — cannot continue."
    fi

    info "All required tools found — OK"
}

# ---------------------------------------------------------------------------
# STEP 1 — Generate Secure Boot keys
# ---------------------------------------------------------------------------
#
# WHY THREE KEY PAIRS?
# UEFI Secure Boot uses a three-layer key hierarchy defined in the UEFI spec:
#
#  PK (Platform Key)
#    The root certificate for a platform. Only one PK is allowed. Its private
#    key must be kept off the device after enrolment — ideally on an HSM or
#    an air-gapped machine. Enrolling a PK puts the firmware into "User Mode"
#    (Secure Boot enforcement active).
#
#  KEK (Key Exchange Key)
#    Authorises updates to the db and dbx without requiring the PK private
#    key. Microsoft and OEM keys normally appear here on PC hardware.
#
#  db (Signature Database)
#    The "allowed" list. A bootloader or kernel is permitted to run if its
#    hash or its signing certificate appears here.
#
# For an embedded gateway we own the full key hierarchy, so we generate
# all three pairs ourselves and keep them on the device only during setup.
# After enrolment, you should move the PK private key off the device.

generate_keys() {
    step "1. Generating Secure Boot key pairs"

    local org="${SOMNIGUARD_ORG_NAME:-SOMNI-Guard}"
    local ou="${SOMNIGUARD_ORG_UNIT:-NightWatchGuard}"

    run mkdir -p "$KEY_DIR"
    run chmod 700 "$KEY_DIR"

    # We need a GUID to identify the key owner in EFI variable payloads.
    # Generate a random UUID if one doesn't already exist.
    local guid_file="${KEY_DIR}/owner-guid.txt"
    if [[ ! -f "$guid_file" ]] || $DRY_RUN; then
        local guid
        guid="$(uuidgen --random 2>/dev/null || cat /proc/sys/kernel/random/uuid)"
        if ! $DRY_RUN; then
            echo "$guid" > "$guid_file"
            chmod 600 "$guid_file"
        fi
        info "Generated owner GUID: $guid"
    else
        local guid; guid="$(cat "$guid_file")"
        info "Reusing existing owner GUID: $guid"
    fi

    # Helper: generate one RSA key + self-signed certificate.
    # Arguments: <name> <subject CN>
    _generate_key_pair() {
        local name="$1"
        local cn="$2"
        local key="${KEY_DIR}/${name}.key"
        local crt="${KEY_DIR}/${name}.crt"

        if [[ -f "$key" && -f "$crt" ]]; then
            warn "Key pair for '$name' already exists — skipping generation."
            warn "Delete $KEY_DIR/${name}.{key,crt} to regenerate."
            return
        fi

        info "  Generating $name key (RSA-${KEY_BITS}, valid ${KEY_DAYS} days) ..."

        run openssl req -newkey "rsa:${KEY_BITS}" \
            -nodes -keyout "$key" \
            -new -x509 -sha256 -days "$KEY_DAYS" \
            -subj "/O=${org}/OU=${ou}/CN=${cn}/" \
            -out "$crt"

        run chmod 600 "$key"
        run chmod 644 "$crt"

        # Also export the certificate in DER format (needed by efitools).
        run openssl x509 -outform DER -in "$crt" -out "${KEY_DIR}/${name}.der"
    }

    _generate_key_pair "PK"  "${org} Platform Key"
    _generate_key_pair "KEK" "${org} Key Exchange Key"
    _generate_key_pair "db"  "${org} Signature Database"

    # Convert certificates to EFI Signature List (ESL) format.
    # ESL is the binary format that UEFI firmware understands for key storage.
    step "1b. Converting certificates to EFI Signature List (ESL) format"

    _cert_to_esl() {
        local name="$1"
        local crt="${KEY_DIR}/${name}.crt"
        local esl="${KEY_DIR}/${name}.esl"
        info "  Converting $name.crt → $name.esl ..."
        run cert-to-efi-sig-list -g "$guid" "$crt" "$esl"
    }

    _cert_to_esl "PK"
    _cert_to_esl "KEK"
    _cert_to_esl "db"

    # Sign each ESL with the appropriate parent key to produce an
    # EFI Signature List with an AuthHeader (signed update payload).
    # This is what `efi-updatevar` (or the firmware's own key manager) needs.
    step "1c. Signing ESL payloads for authenticated variable update"

    # PK self-signs (there is no parent key above PK).
    run sign-efi-sig-list -k "${KEY_DIR}/PK.key" -c "${KEY_DIR}/PK.crt" \
        PK "${KEY_DIR}/PK.esl" "${KEY_DIR}/PK.auth"

    # KEK is signed by PK.
    run sign-efi-sig-list -k "${KEY_DIR}/PK.key" -c "${KEY_DIR}/PK.crt" \
        KEK "${KEY_DIR}/KEK.esl" "${KEY_DIR}/KEK.auth"

    # db is signed by KEK.
    run sign-efi-sig-list -k "${KEY_DIR}/KEK.key" -c "${KEY_DIR}/KEK.crt" \
        db "${KEY_DIR}/db.esl" "${KEY_DIR}/db.auth"

    info "Key generation and signing complete."
    info "Keys stored in: $KEY_DIR"
    warn "SECURITY: Move ${KEY_DIR}/PK.key off this device after enrolment!"
}

# ---------------------------------------------------------------------------
# STEP 2 — Sign the boot chain
# ---------------------------------------------------------------------------
#
# The boot chain on Pi 5 with UEFI firmware is:
#   UEFI firmware (RPI_EFI.fd) → GRUB (or systemd-boot) → Linux kernel
#
# All PE/COFF binaries in this chain must carry a valid signature that chains
# to a certificate in the UEFI db variable. We sign them with our db key.

sign_boot_chain() {
    step "2. Signing the boot chain"

    # Locate the kernel image.
    # Only PE/COFF EFI stub kernels (CONFIG_EFI_STUB=y) can be signed with sbsign.
    # /boot/Image and kernel8.img are raw AArch64 binaries — NOT PE/COFF — so
    # sbsign will fail on them with "not a valid EFI image".  They are excluded.
    # On Raspberry Pi OS Bookworm (arm64), /boot/vmlinuz is the EFI stub kernel.
    local kernel_candidates=(
        /boot/vmlinuz
    )

    local kernel=""
    for candidate in "${kernel_candidates[@]}"; do
        if [[ -f "$candidate" ]]; then
            kernel="$candidate"
            break
        fi
    done

    if [[ -z "$kernel" ]]; then
        if $DRY_RUN; then
            warn "[DRY-RUN] No kernel image found; skipping kernel signing simulation."
        else
            die "Cannot locate kernel image. Looked in: ${kernel_candidates[*]}"
        fi
    else
        info "Found kernel at: $kernel"
        _sign_efi_binary "$kernel" "${kernel}.signed"
        info "Kernel signed → ${kernel}.signed"
    fi

    # Locate the bootloader (GRUB or systemd-boot).
    local bootloader_candidates=(
        "${EFI_MOUNT}/EFI/debian/grubaa64.efi"
        "${EFI_MOUNT}/EFI/BOOT/BOOTAA64.EFI"
        "${EFI_MOUNT}/EFI/systemd/systemd-bootaa64.efi"
    )

    for bl in "${bootloader_candidates[@]}"; do
        if [[ -f "$bl" ]]; then
            # Keep an unsigned copy so the system can be recovered if signing
            # succeeds but the signed binary turns out to be rejected by UEFI.
            info "Backing up original bootloader: $bl → ${bl}.unsigned"
            run cp "$bl" "${bl}.unsigned"
            info "Signing bootloader: $bl"
            _sign_efi_binary "$bl" "${bl}.signed"
            # Replace the original with the signed version.
            run cp "${bl}.signed" "$bl"
            info "Bootloader signed and replaced in-place (original at ${bl}.unsigned)."
        fi
    done

    # Sign the UEFI Shell if present (useful for diagnostics).
    # Must sign to a temp path first: sbsign opens the output file before
    # reading the input, so passing the same path for both would truncate
    # the input before it is read, producing a corrupt output.
    local shell_path="${EFI_MOUNT}/EFI/BOOT/UEFI_Shell.efi"
    if [[ -f "$shell_path" ]]; then
        info "Signing UEFI Shell: $shell_path"
        _sign_efi_binary "$shell_path" "${shell_path}.signed"
        run mv "${shell_path}.signed" "$shell_path"
        info "UEFI Shell signed in-place."
    fi
}

# Helper: sign a single PE/COFF EFI binary with the db key.
_sign_efi_binary() {
    local input="$1"
    local output="$2"
    run sbsign \
        --key  "${KEY_DIR}/db.key" \
        --cert "${KEY_DIR}/db.crt" \
        --output "$output" \
        "$input"
}

# ---------------------------------------------------------------------------
# STEP 3 — Configure Pi 5 boot EEPROM
# ---------------------------------------------------------------------------
#
# The Raspberry Pi 5 EEPROM contains firmware that runs before the UEFI layer.
# To boot through UEFI, BOOT_ORDER must include SD/NVMe entries that then
# hand off to the UEFI firmware image (RPI_EFI.fd) placed on the FAT32
# boot partition.
#
# Reference: https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#raspberry-pi-bootloader-configuration

configure_eeprom() {
    step "3. Configuring Pi 5 boot EEPROM"

    info "Reading current EEPROM configuration ..."
    if $DRY_RUN; then
        info "[DRY-RUN] Would run: rpi-eeprom-config"
        info "[DRY-RUN] Would set BOOT_ORDER=0xf16 (NVMe→SD→restart)"
    else
        local current_config
        current_config="$(rpi-eeprom-config 2>/dev/null || true)"
        info "Current EEPROM config:"
        echo "$current_config" | tee -a "$LOG_FILE"

        # Write a modified config with UEFI-compatible boot order.
        # BOOT_ORDER digits are read right-to-left.  Codes:
        #   1 = SD card, 4 = USB-MSD, 6 = NVMe, f = restart sequence.
        # 0xf16 therefore means: try NVMe (6), then SD (1), then restart (f).
        # Adjust to match your actual boot device priority (e.g. 0xf41 to try
        # SD first, then USB-MSD).
        local boot_order="${SOMNI_BOOT_ORDER:-0xf16}"
        local tmp_cfg; tmp_cfg="$(mktemp)"
        echo "$current_config" > "$tmp_cfg"

        # Ensure BOOT_ORDER is set correctly.
        if grep -q "^BOOT_ORDER" "$tmp_cfg"; then
            sed -i "s/^BOOT_ORDER=.*/BOOT_ORDER=${boot_order}/" "$tmp_cfg"
        else
            echo "BOOT_ORDER=${boot_order}" >> "$tmp_cfg"
        fi

        rpi-eeprom-config --apply "$tmp_cfg"
        rm -f "$tmp_cfg"
        info "EEPROM config updated.  A reboot is required for changes to take effect."
    fi

    warn "After reboot, enter UEFI setup (press ESC during boot) and:"
    warn "  1. Navigate to: Security → Secure Boot"
    warn "  2. Set 'Secure Boot Mode' to 'Custom'"
    warn "  3. This script will enrol keys in STEP 6 — do NOT enable enforcement yet."
}

# ---------------------------------------------------------------------------
# STEP 4 — (Informational) UEFI firmware settings notes
# ---------------------------------------------------------------------------

print_uefi_settings_notes() {
    step "4. Notes on UEFI firmware settings"
    cat <<'NOTES' | tee -a "$LOG_FILE"

  UEFI Secure Boot on Pi 5 is controlled through the UEFI menu accessed by
  pressing ESC during the early boot splash screen. Key settings:

  Security → Secure Boot:
    • Secure Boot Mode → "Custom" (allows key management)
    • After enrolling keys (Step 6), change to "Standard" or enable
      "Secure Boot Enforcement".

  Device Manager → Raspberry Pi Configuration → Advanced Settings:
    • System Table Selection: ACPI + DeviceTree  (recommended for Linux)
    • Limit RAM to 3 GB: Disabled  (set to Disabled for full RAM access)

  Boot Manager:
    • Verify the signed GRUB / systemd-boot entry appears at the top.

NOTES
}

# ---------------------------------------------------------------------------
# STEP 5 — Install / update UEFI firmware
# ---------------------------------------------------------------------------
#
# The worproject/rpi5-uefi project provides a UEFI firmware image
# (RPI_EFI.fd) and associated files that replace the standard
# Raspberry Pi firmware on the FAT32 boot partition.  This unlocks
# UEFI features including Secure Boot.
#
# Project: https://github.com/worproject/rpi5-uefi
# Status (May 2026): archived, v0.3 is the terminal release.

install_uefi_firmware() {
    step "5. Installing worproject/rpi5-uefi UEFI firmware"

    # Check whether the UEFI firmware is already present.
    local rpi_efi_fd="/boot/firmware/RPI_EFI.fd"
    if [[ -f "$rpi_efi_fd" ]]; then
        info "UEFI firmware already present at $rpi_efi_fd"
        info "Skipping download. To reinstall, delete $rpi_efi_fd and re-run."
        return
    fi

    info "UEFI firmware not found. Downloading from GitHub ..."
    info "URL: $UEFI_FIRMWARE_URL"

    run mkdir -p "$UEFI_TMP_DIR"

    local zip_file="${UEFI_TMP_DIR}/RPi5_UEFI.zip"
    run wget -q --show-progress -O "$zip_file" "$UEFI_FIRMWARE_URL"

    # Verify the download completed (basic size check).
    if ! $DRY_RUN; then
        local size; size="$(stat -c%s "$zip_file" 2>/dev/null || echo 0)"
        if [[ "$size" -lt 100000 ]]; then
            die "Downloaded file appears too small ($size bytes). Check the URL: $UEFI_FIRMWARE_URL"
        fi
    fi

    info "Extracting UEFI firmware ..."
    run unzip -o "$zip_file" -d "$UEFI_TMP_DIR"

    # The zip contains files to be placed at the root of the FAT32 boot
    # partition (typically /boot/firmware on Raspberry Pi OS).
    local boot_partition="/boot/firmware"
    if [[ ! -d "$boot_partition" ]]; then
        boot_partition="/boot"
        warn "Using $boot_partition as boot partition (expected /boot/firmware)."
    fi

    info "Backing up existing boot partition files ..."
    run mkdir -p "$BACKUP_DIR/boot"
    # Pi 5 ships without fixup*.dat / start*.elf (those were for Pi 0–4).
    # Back up anything the worproject/rpi5-uefi zip is about to replace or overwrite.
    for f in config.txt cmdline.txt bcm2712-rpi-5-b.dtb \
             RPI_EFI.fd Readme.md firmware \
             bootcode.bin; do
        if [[ -e "${boot_partition}/${f}" ]]; then
            run cp -a "${boot_partition}/${f}" "$BACKUP_DIR/boot/" || true
        fi
    done
    # Also preserve the overlays directory wholesale, because the UEFI zip
    # brings its own overlays/ and may shadow Pi-specific DT overlays.
    if [[ -d "${boot_partition}/overlays" ]]; then
        run cp -a "${boot_partition}/overlays" "$BACKUP_DIR/boot/" || true
    fi

    info "Installing UEFI firmware files to $boot_partition ..."
    run cp -r "${UEFI_TMP_DIR}/"* "$boot_partition/"

    info "Cleaning up temporary files ..."
    run rm -rf "$UEFI_TMP_DIR"

    info "UEFI firmware installed. Reboot to enter UEFI setup."

    # Ensure the EFI partition directory structure exists.
    run mkdir -p "${EFI_MOUNT}/EFI/BOOT"
    run mkdir -p "${EFI_MOUNT}/EFI/somniguard"
    info "EFI directory structure created at $EFI_MOUNT"
}

# ---------------------------------------------------------------------------
# STEP 6 — Enrol keys in UEFI NVRAM
# ---------------------------------------------------------------------------
#
# Keys are enrolled by writing to EFI NVRAM variables. UEFI defines three
# variables we need to populate:
#
#   PK  (Platform Key)        — must be written last (enrolling PK activates
#                                Secure Boot enforcement in User Mode).
#   KEK (Key Exchange Key)    — must be written before PK.
#   db  (Signature Database)  — must be written before PK.
#
# We use efi-updatevar from the efitools package. The firmware must be in
# "Setup Mode" (no PK enrolled yet) for direct NVRAM writes; otherwise the
# update payload must be signed (our .auth files handle this).

enrol_keys() {
    step "6. Enrolling Secure Boot keys in UEFI NVRAM"

    # Writing EFI NVRAM variables from Linux requires /sys/firmware/efi/efivars,
    # which is only present when the kernel itself was booted via UEFI.  A Pi 5
    # running its stock bootloader (even with RPI_EFI.fd copied onto the boot
    # partition) will not expose efivars until after a reboot through the UEFI
    # firmware.  Bail out with clear instructions instead of producing cryptic
    # "Cannot open EFI variable" errors from efi-updatevar.
    if [[ ! -d /sys/firmware/efi/efivars ]]; then
        if $DRY_RUN; then
            warn "[DRY-RUN] /sys/firmware/efi/efivars missing — would skip enrolment here."
            warn "[DRY-RUN] On real hardware you must reboot into UEFI first, then re-run."
            return
        fi
        warn "======================================================================"
        warn " /sys/firmware/efi/efivars is not present — this kernel was not booted"
        warn " via UEFI.  Key enrolment from Linux is impossible until you reboot"
        warn " through the worproject/rpi5-uefi firmware."
        warn ""
        warn " NEXT STEPS:"
        warn "   1. sudo reboot"
        warn "   2. At the rainbow splash, press ESC to enter UEFI setup and"
        warn "      confirm the firmware comes up (Device Manager → Raspberry Pi"
        warn "      Configuration).  Exit saving changes to boot Linux via UEFI."
        warn "   3. Once booted, re-run:  sudo bash $SCRIPT_NAME"
        warn "      Steps already completed (keys, signing, EEPROM) will be"
        warn "      skipped and only enrolment + verification will run."
        warn ""
        warn " Alternative: enrol the .auth files manually from the UEFI menu"
        warn " (Device Manager → Secure Boot Configuration → Enroll from file)."
        warn " The payloads live in: $KEY_DIR/{db,KEK,PK}.auth"
        warn "======================================================================"
        return
    fi

    # Check whether we are in Setup Mode (no PK enrolled yet).
    # Use a sentinel pattern that survives grep returning 1 under `set -e`:
    # no pipeline, no command substitution chaining.
    local pk_enrolled=0
    if efi-readvar -v PK 2>/dev/null | grep -q 'List 0'; then
        pk_enrolled=1
    fi

    if [[ "$pk_enrolled" -gt 0 ]]; then
        warn "PK is already enrolled — firmware is in User Mode."
        warn "The .auth signed payloads will be used for authenticated updates."
        warn "If enrolment fails, clear keys in the UEFI menu first:"
        warn "  Security → Secure Boot → Reset Secure Boot Keys"
    else
        info "Firmware is in Setup Mode (no PK enrolled) — direct enrolment."
    fi

    # Write authenticated update payloads with efi-updatevar.
    # Do NOT use the -e flag here: -e means 'treat file as plain ESL', which
    # would ignore the auth signature.  We pass .auth files, so no -e flag.
    # Order: db first, then KEK, then PK last (enrolling PK activates enforcement).

    info "Enrolling db (Signature Database) ..."
    run efi-updatevar -f "${KEY_DIR}/db.auth" db

    info "Enrolling KEK (Key Exchange Key) ..."
    run efi-updatevar -f "${KEY_DIR}/KEK.auth" KEK

    info "Enrolling PK (Platform Key) — activates Secure Boot enforcement ..."
    run efi-updatevar -f "${KEY_DIR}/PK.auth" PK

    info "Key enrolment complete."
    warn "SECURITY: The PK private key (${KEY_DIR}/PK.key) should now be moved"
    warn "          off this device to a secure, offline location."
}

# ---------------------------------------------------------------------------
# STEP 7 — Verification
# ---------------------------------------------------------------------------

verify_secure_boot() {
    step "7. Verifying Secure Boot configuration"

    local all_ok=true

    # 7a. Check Secure Boot status via mokutil.
    info "7a. Checking Secure Boot status (mokutil) ..."
    local sb_status
    sb_status="$(mokutil --sb-state 2>/dev/null || echo "unknown")"
    info "  mokutil --sb-state: $sb_status"
    if [[ "$sb_status" == *"SecureBoot enabled"* ]]; then
        info "  Secure Boot is ENABLED — OK"
    else
        warn "  Secure Boot does not appear to be enabled yet."
        warn "  This is expected before the first reboot with the new keys."
        all_ok=false
    fi

    # 7b. Verify signed kernel signature.
    local kernel_signed
    for candidate in /boot/vmlinuz.signed /boot/Image.signed; do
        if [[ -f "$candidate" ]]; then
            info "7b. Verifying signature on $candidate ..."
            if sbverify --cert "${KEY_DIR}/db.crt" "$candidate" 2>&1 | tee -a "$LOG_FILE"; then
                info "  Signature on $candidate is valid — OK"
            else
                warn "  Signature verification FAILED for $candidate"
                all_ok=false
            fi
        fi
    done

    # 7c. Check EFI variables are populated.
    info "7c. Checking EFI variables ..."
    for var in PK KEK db; do
        local var_info
        var_info="$(efi-readvar -v "$var" 2>/dev/null | head -3 || echo "(not found)")"
        info "  $var: $var_info"
    done

    # 7d. List enrolled certificates via mokutil.
    info "7d. Listing enrolled Machine Owner Keys (MOK) / db certificates ..."
    mokutil --list-enrolled 2>/dev/null | tee -a "$LOG_FILE" || true

    if $all_ok; then
        info "All verification checks passed."
    else
        warn "Some checks did not pass — this may be normal before the first reboot."
        warn "Reboot the system, then re-run with --verify-only to confirm status."
    fi
}

# ---------------------------------------------------------------------------
# STEP 8 — Create systemd boot-verification service
# ---------------------------------------------------------------------------
#
# This service runs at every boot and logs the Secure Boot status to syslog
# and the SOMNI-Guard log file. It acts as a canary: if Secure Boot is
# unexpectedly disabled, the service will emit a prominent warning that
# monitoring infrastructure can alert on.

create_verification_service() {
    step "8. Installing systemd boot-verification service"

    local service_dir="/etc/systemd/system"
    local service_file="${service_dir}/somniguard-secure-boot-verify.service"
    local check_script="/usr/local/bin/somniguard-sb-verify.sh"

    # Write the verification helper script.
    info "Writing verification helper to $check_script ..."
    run bash -c "cat > '$check_script' <<'INNER_SCRIPT'
#!/usr/bin/env bash
# SOMNI-Guard — Secure Boot status check (runs at each boot)
set -euo pipefail

LOG=/var/log/somniguard/secure_boot_status.log
mkdir -p /var/log/somniguard
echo \"=== Secure Boot Status Check \$(date) ===\" >> \"\$LOG\"

SB_STATE=\"\$(mokutil --sb-state 2>/dev/null || echo 'mokutil unavailable')\"
echo \"mokutil: \$SB_STATE\" >> \"\$LOG\"

if echo \"\$SB_STATE\" | grep -q 'SecureBoot enabled'; then
    logger -t somniguard-sb 'Secure Boot ENABLED — boot chain integrity verified'
    echo 'STATUS: SECURE' >> \"\$LOG\"
    exit 0
else
    logger -p user.warning -t somniguard-sb 'WARNING: Secure Boot DISABLED or status unknown'
    echo 'STATUS: NOT SECURE' >> \"\$LOG\"
    echo 'ACTION REQUIRED: Investigate Secure Boot configuration immediately.' >> \"\$LOG\"
    exit 1
fi
INNER_SCRIPT
"
    run chmod 750 "$check_script"

    # Write the systemd unit file.
    info "Writing systemd unit to $service_file ..."
    run bash -c "cat > '$service_file' <<'UNIT'
[Unit]
Description=SOMNI-Guard Secure Boot Status Verification
Documentation=https://github.com/at0m-b0mb/NightWatchGaurd
After=local-fs.target
DefaultDependencies=no

[Service]
Type=oneshot
ExecStart=$check_script
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal
SyslogIdentifier=somniguard-sb

[Install]
WantedBy=multi-user.target
UNIT
"

    run systemctl daemon-reload
    run systemctl enable somniguard-secure-boot-verify.service
    info "Systemd service installed and enabled: somniguard-secure-boot-verify.service"
    info "It will run at next boot. To run now: systemctl start somniguard-secure-boot-verify.service"
}

# ---------------------------------------------------------------------------
# ROLLBACK INSTRUCTIONS
# ---------------------------------------------------------------------------
# If something goes wrong and the system does not boot:
#
# 1. IMMEDIATE RECOVERY (boot from a working SD card):
#    a. Flash a known-good Raspberry Pi OS image to a second SD card.
#    b. Boot from that card (hold shift, or change SD card).
#    c. Mount the failing SD card and restore backed-up firmware files from:
#         /etc/somniguard/secure-boot/backup_<timestamp>/boot/
#
# 2. CLEAR SECURE BOOT KEYS via UEFI menu:
#    a. Enter UEFI setup (press ESC at boot splash).
#    b. Navigate to: Security → Secure Boot → Reset Secure Boot Keys.
#    c. This clears PK, KEK, db and returns firmware to Setup Mode.
#    d. Re-flash firmware if needed.
#
# 3. RE-FLASH UEFI FIRMWARE:
#    a. Download a fresh copy of the worproject/rpi5-uefi firmware zip.
#    b. Extract to the FAT32 boot partition (mount on another machine if needed).
#
# 4. REMOVE GENERATED KEYS:
#    rm -rf /etc/somniguard/secure-boot/keys/
#    Then re-run this script to start fresh.
#
# 5. DISABLE THE SYSTEMD SERVICE:
#    systemctl disable somniguard-secure-boot-verify.service
#    rm /etc/systemd/system/somniguard-secure-boot-verify.service
#    systemctl daemon-reload
# ---------------------------------------------------------------------------

print_rollback_reminder() {
    warn "======================================================================="
    warn "ROLLBACK REMINDER"
    warn "If this device fails to boot after the next reboot, see the ROLLBACK"
    warn "INSTRUCTIONS section inside $SCRIPT_NAME."
    warn "Backup of pre-installation files stored in: $BACKUP_DIR"
    warn "======================================================================="
}

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

main() {
    parse_args "$@"

    # Root check must come before init_logging: the log directory
    # (/var/log/somniguard) requires root to create.  Checking first gives a
    # clear error message instead of a confusing "mkdir: Permission denied".
    check_root

    init_logging
    print_banner

    if $DRY_RUN; then
        warn "========================================================"
        warn " DRY-RUN MODE — no changes will be made to this system "
        warn "========================================================"
        echo
    fi

    # --verify-only: just check status and exit.
    if $VERIFY_ONLY; then
        info "Running in --verify-only mode."
        verify_secure_boot
        info "Verification complete. Exiting."
        exit 0
    fi

    # Full setup flow.
    check_pi5
    check_required_tools

    generate_keys
    sign_boot_chain
    configure_eeprom
    print_uefi_settings_notes
    install_uefi_firmware
    enrol_keys
    verify_secure_boot
    create_verification_service
    print_rollback_reminder

    echo
    info "================================================================="
    info " SOMNI-Guard Secure Boot setup complete."
    info ""
    info " NEXT STEPS:"
    info "  1. Review the output above for any warnings."
    info "  2. Move the PK private key off this device:"
    info "       ${KEY_DIR}/PK.key"
    info "  3. Reboot the device:"
    info "       sudo reboot"
    info "  4. After reboot, verify Secure Boot is active:"
    info "       sudo bash $SCRIPT_NAME --verify-only"
    info ""
    info " Log file: $LOG_FILE"
    info "================================================================="
}

main "$@"
