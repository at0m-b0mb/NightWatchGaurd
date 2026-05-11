#!/usr/bin/env bash
# =============================================================================
# SOMNI-Guard Gateway — Raspberry Pi 5 TRUE FULL-DISK ENCRYPTION (LUKS2)
# Trixie Edition  (also backward-compatible with Bookworm)
# =============================================================================
#
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# DESTRUCTIVE OPERATION — TAKE A FULL VERIFIED BACKUP FIRST.
# A power loss or mistake during Phase 2 can render the system unbootable
# with no recovery path.  Your data will be gone.
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
#
# WHAT THIS DOES (vs. setup_file_encryption_pi5.sh)
# --------------------------------------------------
# setup_file_encryption_pi5.sh encrypts only the gateway *secret files*
# (env, private keys) and decrypts them to RAM at runtime via somniguard-start.
# THIS script encrypts the **entire root partition** — every byte of the OS,
# /home, /etc, /var, the lot — so the Pi 5 prompts for a LUKS passphrase
# BEFORE the kernel hands control to systemd.  Without the passphrase, no
# userland code runs at
# all.  An attacker with the SD card or NVMe sees only random-looking bytes.
#
# REFERENCE GUIDES (Trixie-tested)
# ---------------------------------
#   https://forums.raspberrypi.com/viewtopic.php?t=395799
#     "Howto: Full Disk Encryption with LUKS and LVM, Trixie Edition"
#   https://github.com/raspberrypi/trixie-feedback/issues/5
#     16 KiB page kernel incompatibility with LUKS on Pi 5 (must use kernel8.img)
#   https://cryptsetup-team.pages.debian.net/cryptsetup/README.initramfs.html
#     Official Debian cryptsetup-initramfs integration docs
#
# ─────────────────────────────────────────────────────────────────────────────
# TRIXIE-SPECIFIC CHANGES VS. BOOKWORM
# ─────────────────────────────────────────────────────────────────────────────
#
#  1. PAGE-SIZE KERNEL
#     Pi 5 Trixie defaults to kernel_2712.img (16 KiB pages).  LUKS brings up
#     device-mapper, which uses 4 KiB page alignment.  The two are incompatible;
#     the device-mapper module refuses to load under a 16 KiB page kernel.
#     FIX: this script injects "kernel=kernel8.img" into config.txt to force
#     the 4 KiB page kernel.  The system works identically except for one
#     small performance difference that is irrelevant for this use case.
#
#  2. MODULES=most IN initramfs.conf
#     Trixie's initramfs-tools defaults to MODULES=dep, which only bundles
#     kernel modules that are currently loaded.  At Phase-1 time the crypto
#     modules (dm-crypt, aes_ce_blk, etc.) may not be loaded yet, so the
#     generated initramfs would be missing them.  MODULES=most includes every
#     module that could possibly be needed, guaranteeing the unlock pipeline
#     is present.
#
#  3. argon2id KEY-DERIVATION FUNCTION
#     cryptsetup 2.7+ (shipped with Trixie) changed its default PBKDF from
#     argon2i to argon2id.  argon2id is strictly better — it resists both
#     side-channel and time-memory trade-off attacks.  This script uses
#     argon2id for all luksFormat commands.
#
#  4. dropbear-initramfs AUTHORIZED-KEYS PATH
#     Bookworm: /etc/dropbear-initramfs/authorized_keys
#     Trixie:   /etc/dropbear/initramfs/authorized_keys
#     This script detects the OS codename and writes to the right place.
#
#  5. /boot/firmware LAYOUT UNCHANGED
#     Trixie keeps /boot/firmware as the FAT boot partition (same as Bookworm).
#     config.txt and cmdline.txt are still in /boot/firmware.
#
# ─────────────────────────────────────────────────────────────────────────────
# WHY THREE PHASES
# ─────────────────────────────────────────────────────────────────────────────
#
# Linux cannot LUKS-encrypt the partition it is currently running from.
# Think of trying to shred a sheet of paper you are reading.  The script
# therefore stages the work across reboots:
#
#   Phase 1  --prepare    [run from the normally booted Pi]
#     ┌─────────────────────────────────────────────────────────────────────┐
#     │ What it does and WHY each step is needed:                           │
#     │                                                                     │
#     │ a) Install packages: cryptsetup-initramfs, busybox, initramfs-tools │
#     │    These provide the tools that run INSIDE the early boot           │
#     │    environment (before the root filesystem is mounted).             │
#     │                                                                     │
#     │ b) Set MODULES=most in /etc/initramfs-tools/initramfs.conf          │
#     │    Ensures ALL kernel modules (especially crypto ones) get bundled  │
#     │    into the initramfs image, not just the ones currently loaded.    │
#     │                                                                     │
#     │ c) Set CRYPTSETUP=y in /etc/cryptsetup-initramfs/conf-hook          │
#     │    Tells cryptsetup-initramfs to include the unlock pipeline even   │
#     │    though /etc/crypttab does not yet list an encrypted device.      │
#     │                                                                     │
#     │ d) Add kernel module names to /etc/initramfs-tools/modules          │
#     │    Belt-and-suspenders: explicitly lists dm-crypt, aes_ce_blk, etc. │
#     │    so they are definitely present.                                  │
#     │                                                                     │
#     │ e) Install /etc/initramfs-tools/hooks/luks_hooks                    │
#     │    A small script that copies resize2fs and fdisk into the          │
#     │    initramfs so Phase 2 can run them from the initramfs shell.      │
#     │                                                                     │
#     │ f) Build /boot/firmware/initramfs.gz                                │
#     │    The actual compressed RAM disk that the Pi firmware will load    │
#     │    instead of going straight to the root filesystem.                │
#     │                                                                     │
#     │ g) Add "initramfs initramfs.gz followkernel" to config.txt          │
#     │    Tells the Pi firmware to load our RAM disk at boot.              │
#     │                                                                     │
#     │ h) Add "kernel=kernel8.img" to config.txt (Trixie only)            │
#     │    Forces the 4 KiB page kernel so LUKS device-mapper works.       │
#     │                                                                     │
#     │ i) Rewrite cmdline.txt                                              │
#     │    - root= now points at /dev/mapper/cryptroot (the unlocked name)  │
#     │    - cryptdevice=PARTITION:cryptroot tells initramfs WHICH          │
#     │      partition to unlock and what to call it after unlocking        │
#     │    - break=init drops you into the initramfs shell so you can run  │
#     │      the Phase-2 commands manually                                  │
#     │                                                                     │
#     │ j) Write /etc/crypttab and update /etc/fstab                        │
#     │    So that after Phase 2, systemd knows how to unlock/mount root.  │
#     │                                                                     │
#     │ k) Write the Phase-2 cheatsheet to /boot/firmware/PHASE2.txt       │
#     │    This is the exact set of commands you will type at the           │
#     │    initramfs prompt.  Copy it to your laptop before rebooting.     │
#     └─────────────────────────────────────────────────────────────────────┘
#
#   Phase 2  [run MANUALLY from the initramfs (initramfs) shell]
#     ┌─────────────────────────────────────────────────────────────────────┐
#     │ The initramfs shell is a minimal BusyBox environment that starts    │
#     │ BEFORE the root filesystem is mounted.  From here the root          │
#     │ partition IS unmounted, so we can encrypt it.  You will:            │
#     │                                                                     │
#     │ 1. Run e2fsck on the root partition to verify filesystem health.    │
#     │ 2. Shrink the filesystem to the minimum size (resize2fs -M).        │
#     │ 3. dd a copy of the shrunken data to a USB stick.                  │
#     │ 4. Run cryptsetup luksFormat to create the LUKS2 header on the     │
#     │    partition — this is where you choose your boot passphrase.       │
#     │ 5. Open the new LUKS volume (cryptsetup luksOpen).                  │
#     │ 6. dd the OS data back from USB into the encrypted volume.          │
#     │ 7. Expand the filesystem back to full size (resize2fs).             │
#     │ 8. Type "exit" to continue booting.                                 │
#     │                                                                     │
#     │ OR: use --in-place mode (no USB stick needed):                      │
#     │ 1. cryptsetup reencrypt --encrypt --reduce-device-size 32M         │
#     │    This encrypts the data in-place.  Takes longer.                  │
#     └─────────────────────────────────────────────────────────────────────┘
#
#   Phase 3  --finalize   [run from the now-encrypted, fully booted Pi]
#     ┌─────────────────────────────────────────────────────────────────────┐
#     │ 1. Remove "break=init" from cmdline.txt so subsequent boots go      │
#     │    straight from the passphrase prompt to the desktop.              │
#     │ 2. Rebuild initramfs (cleaner, without the maintenance hooks).      │
#     │ 3. Record the "finalized" state so --status shows the right info.   │
#     │ 4. Shred the Phase-2 cheatsheet from /boot/firmware.                │
#     └─────────────────────────────────────────────────────────────────────┘
#
# ─────────────────────────────────────────────────────────────────────────────
# PASSPHRASE — READ THIS BEFORE YOU START
# ─────────────────────────────────────────────────────────────────────────────
#
# The passphrase you type at the cryptsetup luksFormat prompt in Phase 2 is
# the SAME passphrase the bootloader will ask for on EVERY subsequent boot.
#
# Requirements:
#   - At least 16 characters.  Short passphrases fall to GPU brute-force in
#     hours.  This passphrase is the ONLY barrier between an attacker with
#     your SD card and all your patient data.
#   - Memorable.  There is NO recovery if you forget it.  Not even Anthropic
#     or the Pi Foundation can help you.  The data is gone.
#   - Different from your Linux login password.  The login password is checked
#     AFTER boot by systemd/PAM.  The LUKS passphrase is checked BEFORE boot
#     by cryptsetup.  They are completely separate systems.
#
# Write the passphrase down on paper.  Store the paper in a locked drawer or
# enter it in a password manager on a DIFFERENT device.
#
# ─────────────────────────────────────────────────────────────────────────────
# USAGE
#   sudo bash setup_full_disk_encryption_pi5.sh --status        # show state
#   sudo bash setup_full_disk_encryption_pi5.sh --check         # pre-flight
#   sudo bash setup_full_disk_encryption_pi5.sh --prepare       # phase 1
#   sudo bash setup_full_disk_encryption_pi5.sh --finalize      # phase 3
#   sudo bash setup_full_disk_encryption_pi5.sh --rollback      # undo phase 1
#   sudo bash setup_full_disk_encryption_pi5.sh --dry-run --prepare
#
# OPTIONS
#   --root-device <dev>  Override auto-detected root partition.
#   --boot-device <dev>  Override auto-detected boot/firmware partition.
#   --mapper-name <n>    Mapper name (default: cryptroot).
#   --no-resize          Skip resize2fs -M shrink step.  Only safe if you
#                        already know your used data fits on the USB target.
#   --in-place           Generate Phase-2 commands that use cryptsetup
#                        reencrypt (LUKS2 in-place; no USB stick needed).
#                        Slower.  Requires reliable mains power.
#   --headless           Install dropbear-initramfs so the LUKS passphrase
#                        can be typed over SSH at boot.  Requires your
#                        SSH public key in ~/.ssh/authorized_keys.
#   --yes                Skip interactive confirmations (CI / Ansible runs).
#   --dry-run            Print every action without executing it.
#   --help               Show this help and exit.
#
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
readonly SCRIPT_VERSION="2.0.0"       # 2.x = Trixie edition
readonly SCRIPT_NAME="$(basename "$0")"
readonly TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

readonly LOG_DIR="/var/log/somniguard"
readonly LOG_FILE="${LOG_DIR}/full_disk_encryption_${TIMESTAMP}.log"

# Phase tracking lives outside any path that gets reformatted in Phase 2.
readonly STATE_DIR="/var/lib/somniguard/fde-state"
readonly STATE_FILE="${STATE_DIR}/phase"

# Every file we mutate is backed up here so --rollback is real.
readonly BACKUP_DIR="${STATE_DIR}/backups.${TIMESTAMP}"

# The FAT boot partition is /boot/firmware on both Bookworm and Trixie.
readonly BOOT_FIRMWARE="/boot/firmware"
readonly CMDLINE_FILE="${BOOT_FIRMWARE}/cmdline.txt"
readonly CONFIG_FILE="${BOOT_FIRMWARE}/config.txt"
readonly INITRAMFS_FILE="${BOOT_FIRMWARE}/initramfs.gz"
readonly PHASE2_HINTS="${BOOT_FIRMWARE}/SOMNI_FDE_PHASE2.txt"

# initramfs-tools configuration files.
readonly INITRAMFS_CONF="/etc/initramfs-tools/initramfs.conf"
readonly INITRAMFS_MODULES_FILE="/etc/initramfs-tools/modules"

# cryptsetup-initramfs drops its own conf hook here.
readonly CRYPTSETUP_CONF_HOOK="/etc/cryptsetup-initramfs/conf-hook"

DEFAULT_MAPPER="cryptroot"

# Required apt packages for the initramfs unlock pipeline.
# cryptsetup-initramfs: provides the initramfs integration (unlock hooks).
# initramfs-tools: the tool that builds the initramfs image.
# busybox: the minimal shell environment inside the initramfs.
# e2fsprogs: provides resize2fs (shrink/grow) and e2fsck (check) for Phase 2.
# rsync: used to copy files robustly.
readonly -a REQUIRED_PKGS=(
    cryptsetup
    cryptsetup-initramfs
    cryptsetup-bin
    initramfs-tools
    busybox
    e2fsprogs
    rsync
)

# Kernel modules the LUKS unlock pipeline needs inside the initramfs.
# These are the ARM64/Pi-5 hardware-accelerated AES + dm-crypt paths.
# MODULES=most (set below) should pull these in automatically, but we
# also list them explicitly in /etc/initramfs-tools/modules as a safety net.
readonly -a INITRAMFS_MODULES=(
    algif_skcipher    # AF_ALG interface — lets userspace use kernel crypto
    aes_arm64         # AES-NI for ARMv8 (software fallback)
    aes_ce_blk        # AES via ARM Crypto Extensions (Cortex-A76 hardware AES)
    aes_ce_ccm        # AES-CCM mode via ARM Crypto Extensions
    aes_ce_cipher     # Core AES cipher via ARM Crypto Extensions
    sha256_arm64      # SHA-256 via ARM Crypto Extensions (used by LUKS header)
    cbc               # Cipher Block Chaining mode
    xts               # XEX-based tweaked-codebook mode (default for LUKS2)
    dm-mod            # Device Mapper core — the layer that cryptsetup sits on
    dm-crypt          # The specific DM target that does the per-sector encrypt/decrypt
)

# Colours (only when stdout is a real terminal)
if [[ -t 1 ]]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
else
    RED=''; YELLOW=''; GREEN=''; CYAN=''; BOLD=''; RESET=''
fi

# Runtime state
ACTION=""
DRY_RUN=false
ASSUME_YES=false
NO_RESIZE=false
IN_PLACE=false
HEADLESS=false
ROOT_DEV=""
BOOT_DEV=""
MAPPER_NAME="${DEFAULT_MAPPER}"
OS_CODENAME="unknown"
IS_TRIXIE=false

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
_log() {
    local lvl="$1"; shift
    local clr="$1"; shift
    local ts; ts="$(date '+%Y-%m-%d %H:%M:%S')"
    printf "${clr}[%s] [%-7s] %s${RESET}\n" "$ts" "$lvl" "$*" | tee -a "$LOG_FILE"
}
info()  { _log "INFO"  "$GREEN"  "$@"; }
warn()  { _log "WARN"  "$YELLOW" "$@"; }
error() { _log "ERROR" "$RED"    "$@"; }
step()  { _log "STEP"  "$CYAN"   "$@"; }
die()   { error "$*"; exit 1; }

run() {
    if $DRY_RUN; then
        info "[DRY-RUN] Would run: $*"
    else
        "$@"
    fi
}

# Three-action confirmation guard: the --yes flag, a tty check, and the user
# typing the expected phrase.  All three must align before anything destructive.
confirm() {
    local prompt="$1"
    local expected="$2"
    if $ASSUME_YES; then
        info "[--yes] auto-confirming: $prompt"
        return 0
    fi
    if [[ ! -t 0 ]]; then
        die "Refusing to run from a non-tty without --yes."
    fi
    echo
    printf "${BOLD}${YELLOW}%s${RESET}\n" "$prompt"
    printf "Type ${BOLD}%s${RESET} to continue (anything else aborts): " "$expected"
    local answer
    read -r answer
    if [[ "$answer" != "$expected" ]]; then
        die "Aborted (got '$answer', expected '$expected')."
    fi
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
print_banner() {
    printf "${BOLD}${CYAN}"
    cat <<'BANNER'
 ___  ___  __  __ _  _ ___    ___ ___  ___
/ __|/ _ \|  \/  | \| |_ _|  | __|   \| __|
\__ \ (_) | |\/| | .` || |   | _|| |) | _|
|___/\___/|_|  |_|_|\_|___|  |_| |___/|___|

   TRUE FULL-DISK ENCRYPTION  |  LUKS2 + initramfs unlock
   NightWatchGuard / SOMNI-Guard / Raspberry Pi 5 (Trixie / Bookworm)
BANNER
    printf "${RESET}\n"
    info "Version : $SCRIPT_VERSION  (Trixie Edition)"
    info "Log     : $LOG_FILE"
    echo
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
usage() {
    sed -n '/^# USAGE/,/^# ===/p' "$0" | sed -e 's/^# \{0,1\}//' -e '/^=*$/d'
    exit 0
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --status)        ACTION="status";   shift;;
            --check)         ACTION="check";    shift;;
            --prepare)       ACTION="prepare";  shift;;
            --finalize)      ACTION="finalize"; shift;;
            --rollback)      ACTION="rollback"; shift;;
            --root-device)   ROOT_DEV="$2";     shift 2;;
            --boot-device)   BOOT_DEV="$2";     shift 2;;
            --mapper-name)   MAPPER_NAME="$2";  shift 2;;
            --no-resize)     NO_RESIZE=true;    shift;;
            --in-place)      IN_PLACE=true;     shift;;
            --headless)      HEADLESS=true;     shift;;
            --yes|-y)        ASSUME_YES=true;   shift;;
            --dry-run)       DRY_RUN=true;      shift;;
            --help|-h)       usage;;
            *)               die "Unknown option: $1 (try --help)";;
        esac
    done
    [[ -z "$ACTION" ]] && die "No action given.  Use --status, --check, --prepare, --finalize, or --rollback.  See --help."
}

# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
# We need to know if we are on Trixie so we can apply the right workarounds.
# /etc/os-release is the authoritative source on all modern Debian systems.
detect_os_version() {
    if [[ -r /etc/os-release ]]; then
        OS_CODENAME="$(grep -oP '(?<=VERSION_CODENAME=)[^\s]+' /etc/os-release 2>/dev/null || echo unknown)"
    fi
    case "$OS_CODENAME" in
        trixie)  IS_TRIXIE=true;  info "OS detected : Raspberry Pi OS Trixie (Debian 13)";;
        bookworm) IS_TRIXIE=false; info "OS detected : Raspberry Pi OS Bookworm (Debian 12)";;
        *)
            IS_TRIXIE=true
            warn "OS codename '$OS_CODENAME' not specifically recognized."
            warn "Treating as Trixie-equivalent and applying Trixie workarounds."
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
check_root() {
    [[ $EUID -eq 0 ]] || die "Must run as root: sudo bash $SCRIPT_NAME ..."
}

init_logging() {
    mkdir -p "$LOG_DIR" "$STATE_DIR"
    chmod 750 "$LOG_DIR" "$STATE_DIR"
    touch "$LOG_FILE"
    chmod 640 "$LOG_FILE"
}

# Verify the expected boot layout.  On both Bookworm and Trixie the FAT
# firmware partition is mounted at /boot/firmware.  This function also
# reminds the operator that an upgrade from an older OS that used /boot
# needs a fstab migration before this script will work.
detect_boot_layout() {
    if [[ ! -d "$BOOT_FIRMWARE" ]]; then
        die "$BOOT_FIRMWARE not found.
  This script requires Pi-OS with the firmware partition at /boot/firmware
  (Bookworm and later).  If you upgraded from Bullseye or older and your
  firmware partition is still at /boot, update /etc/fstab to mount it at
  /boot/firmware, reboot, and re-run this script."
    fi
    [[ -f "$CMDLINE_FILE" ]] || die "$CMDLINE_FILE missing — cannot continue."
    [[ -f "$CONFIG_FILE"  ]] || die "$CONFIG_FILE missing — cannot continue."
    info "Boot layout : $BOOT_FIRMWARE (OK)"
}

# Check the hardware model and — critically — the kernel page size.
# On Trixie, the Pi 5 defaults to a 16 KiB page kernel (kernel_2712.img).
# The Linux device-mapper subsystem (which LUKS sits on top of) requires
# 4 KiB pages.  The two are incompatible: dm-crypt will refuse to open.
# We detect this at Phase 1 time and inject the fix rather than letting
# the operator discover it after a failed reboot.
check_pi5() {
    if [[ -r /proc/device-tree/model ]]; then
        local model
        model="$(tr -d '\0' < /proc/device-tree/model)"
        if [[ "$model" == *"Raspberry Pi 5"* ]]; then
            info "Hardware : $model (supported)"
        else
            warn "Hardware : $model — this script targets Pi 5.  Continuing anyway."
        fi
    fi

    local page_size
    page_size="$(getconf PAGE_SIZE 2>/dev/null || echo 0)"
    info "Kernel page size : ${page_size} bytes"

    if [[ "$page_size" == "16384" ]]; then
        error "─────────────────────────────────────────────────────────────────────"
        error " FATAL: Your Pi 5 is running the 16 KiB page kernel (kernel_2712.img)"
        error " LUKS uses the Linux device-mapper, which requires 4 KiB pages."
        error " The two are incompatible — LUKS will fail to unlock at boot."
        error ""
        error " FIX: Add the following line to $CONFIG_FILE :"
        error "   kernel=kernel8.img"
        error ""
        error " That tells the firmware to load the 4 KiB page kernel instead."
        error " After adding the line, reboot and re-run this script."
        error ""
        error " Reference: https://github.com/raspberrypi/trixie-feedback/issues/5"
        error "─────────────────────────────────────────────────────────────────────"
        die "Cannot proceed with a 16 KiB page kernel."
    fi

    info "Kernel page size : OK (4 KiB — compatible with LUKS device-mapper)"
}

check_packages() {
    step "Installing required packages"
    info ""
    info "These packages are needed to build and run the initramfs unlock pipeline:"
    info "  cryptsetup          — the actual LUKS encryption/decryption tool"
    info "  cryptsetup-initramfs — integrates cryptsetup into the boot initramfs"
    info "  cryptsetup-bin      — cryptsetup command-line utilities"
    info "  initramfs-tools     — builds the initramfs image that boots before root"
    info "  busybox             — the tiny shell that runs inside the initramfs"
    info "  e2fsprogs           — provides resize2fs (shrink FS) and e2fsck (check)"
    info "  rsync               — reliable file copying"
    info ""

    local -a missing=()
    for p in "${REQUIRED_PKGS[@]}"; do
        dpkg -s "$p" &>/dev/null || missing+=("$p")
    done

    if (( ${#missing[@]} )); then
        info "Missing packages: ${missing[*]}"
        info "Running: apt-get update && apt-get install ..."
        run apt-get update -qq
        run apt-get install -y -qq "${missing[@]}"
    else
        info "All required packages are already installed."
    fi

    if $HEADLESS && ! dpkg -s dropbear-initramfs &>/dev/null; then
        info "Headless mode: installing dropbear-initramfs"
        info "  (dropbear is a tiny SSH server that starts inside the initramfs"
        info "   so you can type the LUKS passphrase over SSH at boot time)"
        run apt-get install -y -qq dropbear-initramfs
    fi
}

# ---------------------------------------------------------------------------
# Device detection
# ---------------------------------------------------------------------------
detect_root_device() {
    if [[ -n "$ROOT_DEV" ]]; then
        info "Root device : $ROOT_DEV (user-supplied)"
        return
    fi
    local src
    src="$(findmnt -no SOURCE / 2>/dev/null || true)"
    [[ -n "$src" ]] || die "Could not resolve / via findmnt."
    if [[ "$src" == /dev/mapper/* ]]; then
        info "/ is already on $src — FDE appears to have already been applied."
    fi
    ROOT_DEV="$src"
    info "Root device : $ROOT_DEV (auto-detected)"
}

detect_boot_device() {
    if [[ -n "$BOOT_DEV" ]]; then
        info "Boot device : $BOOT_DEV (user-supplied)"
        return
    fi
    BOOT_DEV="$(findmnt -no SOURCE "$BOOT_FIRMWARE" 2>/dev/null || true)"
    [[ -n "$BOOT_DEV" ]] || die "Could not resolve $BOOT_FIRMWARE via findmnt."
    info "Boot device : $BOOT_DEV (auto-detected)"
}

# ---------------------------------------------------------------------------
# Phase / state helpers
# ---------------------------------------------------------------------------
write_phase() {
    local p="$1"
    $DRY_RUN || { printf '%s\n' "$p" > "$STATE_FILE"; chmod 640 "$STATE_FILE"; }
    info "Phase recorded: $p"
}

read_phase() {
    [[ -f "$STATE_FILE" ]] && cat "$STATE_FILE" || echo "none"
}

backup_file() {
    local f="$1"
    [[ -f "$f" ]] || return 0
    mkdir -p "$BACKUP_DIR"; chmod 700 "$BACKUP_DIR"
    local rel="${f#/}"
    local dst="${BACKUP_DIR}/${rel//\//__}"
    $DRY_RUN || { cp -a "$f" "$dst"; chmod 600 "$dst"; }
    info "Backed up $f → $dst"
}

# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------
report_status() {
    step "Full-disk encryption status"

    local phase; phase="$(read_phase)"
    info "Recorded phase           : $phase"

    detect_os_version
    detect_root_device
    detect_boot_device

    if cryptsetup isLuks "$ROOT_DEV" 2>/dev/null; then
        info "LUKS on $ROOT_DEV        : YES"
        cryptsetup luksDump "$ROOT_DEV" 2>/dev/null \
            | grep -E '^(Version|Cipher|Hash|MK iterations|Key Slot)' \
            | sed 's/^/    /' | tee -a "$LOG_FILE" || true
    else
        warn "LUKS on $ROOT_DEV        : no (not yet encrypted)"
    fi

    [[ -e "/dev/mapper/${MAPPER_NAME}" ]] \
        && info "/dev/mapper/${MAPPER_NAME}   : active" \
        || warn "/dev/mapper/${MAPPER_NAME}   : not active"

    info "cmdline.txt :"
    sed 's/^/    /' "$CMDLINE_FILE" | tee -a "$LOG_FILE"

    if grep -q '^initramfs ' "$CONFIG_FILE" 2>/dev/null; then
        info "config.txt initramfs line : present"
        grep '^initramfs ' "$CONFIG_FILE" | sed 's/^/    /' | tee -a "$LOG_FILE"
    else
        warn "config.txt initramfs line : MISSING — boot would skip unlock"
    fi

    if grep -q '^kernel=' "$CONFIG_FILE" 2>/dev/null; then
        info "config.txt kernel line    :"
        grep '^kernel=' "$CONFIG_FILE" | sed 's/^/    /' | tee -a "$LOG_FILE"
    else
        warn "config.txt kernel line    : absent (Pi 5 Trixie may use 16 KiB kernel by default)"
    fi

    if [[ -f /etc/crypttab ]]; then
        info "/etc/crypttab :"
        sed 's/^/    /' /etc/crypttab | tee -a "$LOG_FILE"
    else
        warn "/etc/crypttab : MISSING"
    fi

    if [[ -f /etc/fstab ]]; then
        info "/etc/fstab (root row) :"
        awk '$2=="/"{print "    " $0}' /etc/fstab | tee -a "$LOG_FILE"
    fi

    if [[ -f "$INITRAMFS_FILE" ]]; then
        info "initramfs : $INITRAMFS_FILE ($(du -h "$INITRAMFS_FILE" | awk '{print $1}'))"
        if lsinitramfs "$INITRAMFS_FILE" 2>/dev/null | grep -q sbin/cryptsetup; then
            info "  -> contains sbin/cryptsetup (unlock pipeline present)"
        else
            warn "  -> does NOT contain sbin/cryptsetup (unlock will fail at boot)"
        fi
    else
        warn "initramfs : $INITRAMFS_FILE missing — Phase 1 has not run."
    fi

    local page_size; page_size="$(getconf PAGE_SIZE 2>/dev/null || echo 0)"
    if [[ "$page_size" == "16384" ]]; then
        warn "Kernel page size : 16 KiB — LUKS will NOT work (add kernel=kernel8.img to config.txt)"
    else
        info "Kernel page size : ${page_size} bytes (OK)"
    fi

    case "$phase" in
        prepared)  info ""; info "Next: REBOOT then follow $PHASE2_HINTS";;
        encrypted) info ""; info "Next: sudo bash $SCRIPT_NAME --finalize";;
        finalized) info ""; info "Full-disk encryption is active and finalised.";;
        *)         info ""; info "Next: sudo bash $SCRIPT_NAME --check, then --prepare";;
    esac
}

# ---------------------------------------------------------------------------
# Pre-flight check  (--check)
# ---------------------------------------------------------------------------
do_check() {
    step "Pre-flight checks"
    detect_os_version
    detect_boot_layout
    check_pi5
    detect_root_device
    detect_boot_device

    local root_size_mib root_used_mib free_pct
    root_size_mib="$(df --output=size --block-size=1M / | tail -1 | tr -d ' ')"
    root_used_mib="$(df --output=used --block-size=1M / | tail -1 | tr -d ' ')"
    free_pct=$(( 100 - (100 * root_used_mib / root_size_mib) ))
    info "Root size : ${root_size_mib} MiB"
    info "Root used : ${root_used_mib} MiB"
    info "Free      : ${free_pct}%"

    if (( free_pct < 30 )) && ! $IN_PLACE; then
        warn "Less than 30% free on /.  The dd backup will work but the USB"
        warn "stick must be large enough for the used portion.  Consider"
        warn "--in-place if you do not have a big enough USB stick."
    fi

    if cryptsetup isLuks "$ROOT_DEV" 2>/dev/null; then
        warn "$ROOT_DEV is already a LUKS volume.  Looks like FDE has already run."
    fi

    info ""
    info "All pre-flight checks passed.  Safe to proceed to --prepare."
}

# ---------------------------------------------------------------------------
# Phase 1  --prepare
# ---------------------------------------------------------------------------
phase1_prepare() {
    step "Phase 1 — Preparing the Pi for encrypted-root boot"
    info ""
    info "This phase modifies several system files and builds a new initramfs."
    info "Every file modified is backed up to: $BACKUP_DIR"
    info ""

    detect_os_version
    detect_boot_layout
    check_pi5
    detect_root_device
    detect_boot_device
    check_packages

    confirm \
        "Phase 1 will modify $CMDLINE_FILE, $CONFIG_FILE, /etc/fstab, /etc/crypttab, $INITRAMFS_CONF, and rebuild $INITRAMFS_FILE.  All originals are backed up.  TAKE A FULL VERIFIED BACKUP OF YOUR SD CARD BEFORE PROCEEDING — Phase 2 will overwrite $ROOT_DEV." \
        "PROCEED"

    # Back up every file we will touch so --rollback can restore them exactly.
    backup_file "$CMDLINE_FILE"
    backup_file "$CONFIG_FILE"
    backup_file /etc/fstab
    backup_file /etc/crypttab 2>/dev/null || true
    backup_file "$INITRAMFS_MODULES_FILE"
    backup_file "$INITRAMFS_CONF"

    _set_modules_most
    _install_initramfs_modules
    _install_initramfs_hooks
    _install_kernel_postinst
    _build_initramfs
    _patch_config_txt
    _patch_cmdline_txt
    _patch_crypttab
    _patch_fstab
    $HEADLESS && _configure_headless_dropbear
    _write_phase2_cheatsheet

    write_phase prepared

    echo
    info "================================================================="
    info " Phase 1 complete."
    info ""
    info " BEFORE YOU REBOOT — do all of these:"
    info ""
    info " 1. Read the Phase-2 cheatsheet (the commands you will type at the"
    info "    initramfs shell):"
    info "      cat $PHASE2_HINTS"
    info ""
    info " 2. Copy the cheatsheet to your laptop so you can read it during"
    info "    the Phase-2 initramfs shell (the Pi will be in a minimal env):"
    info "      scp pi@<pi-ip>:$PHASE2_HINTS ~/PHASE2.txt"
    info "    OR take a clear photo of the terminal output."
    info ""
    info " 3. Write down your chosen LUKS passphrase on paper now."
    info "    You will be asked for it during Phase 2 (luksFormat)."
    info "    There is NO recovery if you forget it."
    info ""
    local _used_mib; _used_mib="$(df --output=used --block-size=1M / | tail -1 | tr -d ' ')"
    info " 4. Plug a USB stick into the Pi."
    info "    It must hold at least the USED portion of your root partition"
    info "    (${_used_mib} MiB).  Check with: lsblk"
    if $IN_PLACE; then
        info "    (--in-place mode: no USB stick required)"
    fi
    info ""
    info " 5. Reboot:  sudo reboot"
    info ""
    info " 6. The Pi will boot and then DROP INTO a BusyBox initramfs shell:"
    info "      BusyBox v1.xx.x (Debian) built-in shell (ash)"
    info "      (initramfs) _"
    info "    THIS IS EXPECTED.  'break=init' was added to cmdline.txt."
    info ""
    info " 7. At the (initramfs) prompt, type the commands from $PHASE2_HINTS."
    info "    You will format the partition, restore the OS, and reboot."
    info ""
    info " 8. After Phase 2 and a successful encrypted boot, run Phase 3:"
    info "      sudo bash $SCRIPT_NAME --finalize"
    info "================================================================="
}

# ── Step: Set MODULES=most ──────────────────────────────────────────────────
# WHY: initramfs-tools has three MODULES settings:
#   dep    — only includes modules currently loaded.  On Trixie, crypto modules
#            may not be loaded at Phase-1 time, so they would be MISSING from
#            the initramfs, causing boot to fail.
#   most   — includes all modules that are likely to be needed.  This is the
#            safe choice for full-disk encryption because it guarantees the
#            dm-crypt and AES modules are present.
#   all    — includes everything; creates a very large initramfs.
# We set MODULES=most for maximum compatibility.
_set_modules_most() {
    step "Setting MODULES=most in $INITRAMFS_CONF"
    info ""
    info "Why: MODULES=dep (the default) only bundles modules that are currently"
    info "loaded.  On Trixie, the crypto modules (dm-crypt, aes_ce_blk, etc.)"
    info "may not be loaded yet, so the initramfs would be missing them and"
    info "LUKS unlock would fail at boot.  MODULES=most includes all modules"
    info "that could reasonably be needed."
    info ""

    if ! $DRY_RUN; then
        if grep -qE '^MODULES=' "$INITRAMFS_CONF"; then
            sed -i 's/^MODULES=.*/MODULES=most/' "$INITRAMFS_CONF"
        else
            printf 'MODULES=most\n' >> "$INITRAMFS_CONF"
        fi
    fi
    info "MODULES=most set in $INITRAMFS_CONF"
}

# ── Step: Kernel modules list ───────────────────────────────────────────────
# WHY: Even with MODULES=most, explicitly listing required modules in
# /etc/initramfs-tools/modules acts as a belt-and-suspenders guarantee.
# mkinitramfs reads this file and ensures each module is included.
_install_initramfs_modules() {
    step "Adding crypto kernel modules to $INITRAMFS_MODULES_FILE"
    info ""
    info "Modules being added:"
    if ! $DRY_RUN; then
        for m in "${INITRAMFS_MODULES[@]}"; do
            grep -qE "^${m}($|\s)" "$INITRAMFS_MODULES_FILE" \
                || printf '%s\n' "$m" >> "$INITRAMFS_MODULES_FILE"
            info "  $m"
        done
    else
        for m in "${INITRAMFS_MODULES[@]}"; do
            info "  [DRY-RUN] would add: $m"
        done
    fi
    info ""
}

# ── Step: initramfs hooks ───────────────────────────────────────────────────
# WHY: The hook script runs during initramfs BUILD time (when mkinitramfs is
# called).  It uses copy_exec to copy binaries INTO the initramfs image.
# We need resize2fs and fdisk in the initramfs so Phase-2 can run them from
# the initramfs shell to shrink/grow the filesystem around the dd backup.
# CRYPTSETUP=y forces the cryptsetup-initramfs package to include the full
# unlock pipeline even though /etc/crypttab does not yet list a LUKS device.
_install_initramfs_hooks() {
    step "Installing initramfs hook script and enabling cryptsetup pipeline"

    local hook=/etc/initramfs-tools/hooks/luks_hooks
    backup_file "$hook" 2>/dev/null || true

    info "Writing $hook"
    info "  -> This hook runs at initramfs BUILD time."
    info "  -> It copies cryptsetup, resize2fs, fdisk, and e2fsck into the image"
    info "     so Phase 2 can use them from the initramfs (initramfs) shell."
    info ""

    cat > "${hook}.new" <<'HOOK'
#!/bin/sh
# SOMNI-Guard FDE hook — copies maintenance tools into the initramfs image.
# Runs at mkinitramfs time, not at boot time.
PREREQ=""
prereqs() { echo "$PREREQ"; }
case "$1" in
    prereqs) prereqs; exit 0 ;;
esac
. /usr/share/initramfs-tools/hook-functions
copy_exec /sbin/cryptsetup /sbin
copy_exec /sbin/resize2fs   /sbin
copy_exec /sbin/fdisk       /sbin
copy_exec /sbin/e2fsck      /sbin
HOOK

    if ! $DRY_RUN; then
        mv "${hook}.new" "$hook"
        chmod 755 "$hook"
    fi
    info "Hook written: $hook"

    # CRYPTSETUP=y in the conf-hook tells cryptsetup-initramfs to include
    # the full passphrase-prompt unlock pipeline even when crypttab is empty.
    # Without this, the initramfs has no unlock code and the boot stalls.
    if [[ -d /etc/cryptsetup-initramfs ]]; then
        info "Setting CRYPTSETUP=y in $CRYPTSETUP_CONF_HOOK"
        if ! $DRY_RUN; then
            if grep -qE '^CRYPTSETUP=' "$CRYPTSETUP_CONF_HOOK" 2>/dev/null; then
                sed -i 's/^CRYPTSETUP=.*/CRYPTSETUP=y/' "$CRYPTSETUP_CONF_HOOK"
            else
                printf 'CRYPTSETUP=y\n' >> "$CRYPTSETUP_CONF_HOOK"
            fi
        fi
        info "CRYPTSETUP=y set — unlock pipeline will be included in initramfs."
    else
        warn "/etc/cryptsetup-initramfs not found — cryptsetup-initramfs may not be installed."
    fi
}

# ── Step: Kernel postinst hook ──────────────────────────────────────────────
# WHY: Every time apt upgrades the kernel (kernel8.img / kernel_2712.img),
# the initramfs must be rebuilt with the NEW kernel's modules.  Without this
# hook, a kernel upgrade would produce a mismatched initramfs and the Pi
# would fail to unlock LUKS at the next boot.
_install_kernel_postinst() {
    local p=/etc/kernel/postinst.d/zz-somniguard-fde
    step "Installing kernel-upgrade postinst hook at $p"
    info "  -> This hook ensures the initramfs is rebuilt after every kernel"
    info "     upgrade so the LUKS crypto modules stay in sync with the kernel."
    info ""

    cat > "${p}.new" <<POSTINST
#!/bin/sh
# SOMNI-Guard FDE: rebuild the Pi firmware initramfs after a kernel upgrade.
# A kernel upgrade without an initramfs rebuild would produce a mismatched
# image and LUKS unlock would fail at boot.
set -e
KERNEL_VERSION="\${1:-\$(uname -r)}"
if command -v mkinitramfs >/dev/null 2>&1; then
    CRYPTSETUP=y mkinitramfs -o ${INITRAMFS_FILE} "\$KERNEL_VERSION" || \
    CRYPTSETUP=y mkinitramfs -o ${INITRAMFS_FILE}
fi
POSTINST

    if ! $DRY_RUN; then
        mv "${p}.new" "$p"
        chmod 755 "$p"
    fi
    info "Kernel postinst hook installed: $p"
}

# ── Step: Build the initramfs ───────────────────────────────────────────────
# WHY: The initramfs (initial RAM filesystem) is a compressed archive that
# the Pi firmware loads into RAM before touching the SD card's root partition.
# It contains a minimal Linux environment with cryptsetup and BusyBox.
# At boot time, the initramfs runs cryptsetup to unlock the LUKS partition,
# then hands the now-decrypted partition to the main kernel as the root FS.
#
# We build it now (while we have a running system with all modules available)
# and place it at /boot/firmware/initramfs.gz where config.txt will point.
_build_initramfs() {
    step "Building $INITRAMFS_FILE (this may take 30-60 seconds)"
    info ""
    info "mkinitramfs is bundling the kernel, crypto modules, and cryptsetup"
    info "into a single compressed image that boots before the root filesystem."
    info "CRYPTSETUP=y env var ensures the unlock pipeline is always included."
    info ""

    if ! $DRY_RUN; then
        CRYPTSETUP=y mkinitramfs -o "$INITRAMFS_FILE" "$(uname -r)"
    fi
    info "initramfs built."

    # Verify the critical binaries are present in the image.
    # If any are missing the next boot will fail — better to catch it now.
    local -a needed=(sbin/cryptsetup sbin/resize2fs sbin/fdisk)
    for n in "${needed[@]}"; do
        if ! $DRY_RUN; then
            if ! lsinitramfs "$INITRAMFS_FILE" | grep -q "$n"; then
                die "FATAL: initramfs is missing $n.
  This means the unlock pipeline is incomplete.  The next boot will fail.
  Check that cryptsetup-initramfs is installed and CRYPTSETUP=y is set in
  $CRYPTSETUP_CONF_HOOK, then re-run --prepare."
            fi
        fi
    done
    info "Verified: initramfs contains cryptsetup, resize2fs, and fdisk."
    info "Size: $(du -h "$INITRAMFS_FILE" 2>/dev/null | awk '{print $1}' || echo '?')"
}

# ── Step: config.txt — initramfs and kernel lines ───────────────────────────
# WHY: The Raspberry Pi firmware (not GRUB) reads config.txt at power-on.
# Two lines are needed:
#   initramfs initramfs.gz followkernel
#     -> Tells the firmware to load initramfs.gz immediately after the kernel.
#     -> "followkernel" means it is loaded right after kernel8.img in memory.
#   kernel=kernel8.img   (Trixie only)
#     -> Forces the 4 KiB page kernel instead of the default 16 KiB kernel.
#     -> Required because LUKS device-mapper needs 4 KiB pages.
_patch_config_txt() {
    step "Patching $CONFIG_FILE"

    # Add the initramfs line if missing.
    if grep -qE '^[[:space:]]*initramfs[[:space:]]+initramfs\.gz' "$CONFIG_FILE"; then
        info "initramfs line already present in config.txt."
    else
        if ! $DRY_RUN; then
            printf '\n# Added by SOMNI-Guard FDE (%s) — tells firmware to load the initramfs.\n' "$TIMESTAMP" >> "$CONFIG_FILE"
            printf 'initramfs initramfs.gz followkernel\n' >> "$CONFIG_FILE"
        fi
        info "Added: initramfs initramfs.gz followkernel"
    fi

    # On Trixie, force the 4 KiB page kernel.
    if $IS_TRIXIE; then
        if grep -qE '^kernel=kernel8\.img' "$CONFIG_FILE"; then
            info "kernel=kernel8.img already present in config.txt."
        else
            if ! $DRY_RUN; then
                printf '# SOMNI-Guard FDE (%s) — force 4 KiB page kernel (required for LUKS on Trixie).\n' "$TIMESTAMP" >> "$CONFIG_FILE"
                printf 'kernel=kernel8.img\n' >> "$CONFIG_FILE"
            fi
            info "Added: kernel=kernel8.img (forces 4 KiB page kernel for LUKS compatibility)"
            info "  Without this, the Pi 5 uses kernel_2712.img (16 KiB pages)"
            info "  which breaks LUKS device-mapper.  See: raspberrypi/trixie-feedback#5"
        fi
    fi
}

# ── Step: cmdline.txt ────────────────────────────────────────────────────────
# WHY: cmdline.txt is the kernel command line that the firmware passes to the
# kernel at boot.  Three changes are needed for LUKS:
#
#   root=/dev/mapper/cryptroot
#     -> Tells the kernel that the root filesystem lives on the decrypted
#        device-mapper volume, NOT on the raw partition.  The initramfs
#        does the LUKS unlock first, creating /dev/mapper/cryptroot, then
#        the kernel mounts it as /.
#
#   cryptdevice=/dev/mmcblk0p2:cryptroot
#     -> Tells initramfs WHICH raw partition to unlock and WHAT NAME to give
#        it in /dev/mapper/.  The format is "source:name".
#
#   break=init
#     -> Drops into the initramfs (initramfs) shell BEFORE init runs.
#        This is temporary — Phase 2 relies on this to give you the shell
#        where you run the encryption commands.  Phase 3 removes it so
#        subsequent boots go straight to the passphrase prompt.
_patch_cmdline_txt() {
    step "Patching $CMDLINE_FILE"
    info ""
    info "Three things are being changed in cmdline.txt:"
    info "  root=        -> /dev/mapper/${MAPPER_NAME} (the unlocked device name)"
    info "  cryptdevice= -> ${ROOT_DEV}:${MAPPER_NAME} (which partition to unlock)"
    info "  break=init   -> drops to initramfs shell for Phase 2 (removed in Phase 3)"
    info ""

    local old new
    old="$(tr -d '\n' < "$CMDLINE_FILE")"
    [[ -n "$old" ]] || die "cmdline.txt is empty — refusing to write."

    # Rewrite root= to point at the mapper device.
    new="$(printf '%s' "$old" | sed -E "s|root=[^ ]+|root=/dev/mapper/${MAPPER_NAME}|")"

    # Add or update cryptdevice=.
    if grep -q 'cryptdevice=' <<<"$new"; then
        new="$(sed -E "s|cryptdevice=[^ ]+|cryptdevice=${ROOT_DEV}:${MAPPER_NAME}|" <<<"$new")"
    else
        new="$new cryptdevice=${ROOT_DEV}:${MAPPER_NAME}"
    fi

    # Add break=init so we land in the initramfs shell for Phase 2.
    grep -q 'break=init' <<<"$new" || new="$new break=init"

    if [[ "$new" == "$old" ]]; then
        info "cmdline.txt already up to date."
    else
        if ! $DRY_RUN; then
            printf '%s\n' "$new" > "$CMDLINE_FILE"
            chmod 755 "$CMDLINE_FILE"
        fi
        info "cmdline.txt rewritten:"
        info "  $new"
    fi
}

# ── Step: /etc/crypttab ──────────────────────────────────────────────────────
# WHY: /etc/crypttab is the systemd/cryptsetup configuration file that tells
# the system WHICH LUKS devices to unlock at boot and with what parameters.
# Without an entry here, systemd would not know to unlock the root device and
# the encrypted system would not be able to mount / after Phase 2.
#
# Fields:  <name>  <source-device>  <key-file>  <options>
#   name:   cryptroot — the name of the /dev/mapper/ device that will appear
#   source: the raw partition (e.g., /dev/mmcblk0p2)
#   key:    none — means "prompt the operator for the passphrase at boot"
#   opts:   luks,initramfs,discard
#             luks      = it is a LUKS device (not plain dm-crypt)
#             initramfs = process this entry in the initramfs, not in systemd
#             discard   = pass TRIM operations through (good for flash storage)
_patch_crypttab() {
    step "Writing /etc/crypttab"
    local entry="${MAPPER_NAME} ${ROOT_DEV} none luks,initramfs,discard"
    if [[ -f /etc/crypttab ]] && grep -qE "^${MAPPER_NAME}\s" /etc/crypttab; then
        if ! $DRY_RUN; then
            sed -i -E "s|^${MAPPER_NAME}\s.*|${entry}|" /etc/crypttab
        fi
        info "Updated existing crypttab entry."
    else
        if ! $DRY_RUN; then
            mkdir -p /etc
            printf '%s\n' "$entry" >> /etc/crypttab
        fi
        info "Appended crypttab entry."
    fi
    info "  $entry"
}

# ── Step: /etc/fstab ─────────────────────────────────────────────────────────
# WHY: /etc/fstab tells the kernel where to mount filesystems at boot.
# The root filesystem (/) currently points at the raw partition (e.g.,
# PARTUUID=xxxx or /dev/mmcblk0p2).  After encryption, the root filesystem
# lives INSIDE the LUKS volume, so we must update the / entry to point at
# /dev/mapper/cryptroot instead.
_patch_fstab() {
    step "Updating /etc/fstab root entry to /dev/mapper/${MAPPER_NAME}"
    if grep -qE "^/dev/mapper/${MAPPER_NAME}\s+/\s" /etc/fstab; then
        info "fstab root row already points at mapper — no change needed."
        return
    fi
    if ! $DRY_RUN; then
        local tmp; tmp="$(mktemp)"
        awk -v map="/dev/mapper/${MAPPER_NAME}" '
            $2 == "/" && $0 !~ /^[[:space:]]*#/ {
                $1 = map
                print
                next
            }
            { print }
        ' /etc/fstab > "$tmp"
        cat "$tmp" > /etc/fstab
        rm -f "$tmp"
    fi
    info "fstab root row rewritten to /dev/mapper/${MAPPER_NAME}."
}

# ── Step: dropbear (headless SSH unlock) ─────────────────────────────────────
# WHY: If the Pi is headless (no monitor/keyboard), there is no way to type
# the LUKS passphrase at boot unless dropbear-initramfs is installed.
# dropbear is a tiny SSH server that runs INSIDE the initramfs.  You SSH in
# and type: cryptroot-unlock
# Then enter the passphrase, and the Pi continues booting.
#
# IMPORTANT: Trixie moved the authorized_keys file to a new path.
#   Bookworm: /etc/dropbear-initramfs/authorized_keys
#   Trixie:   /etc/dropbear/initramfs/authorized_keys
_configure_headless_dropbear() {
    step "Configuring dropbear-initramfs for headless SSH unlock"
    info ""
    info "Headless mode: the LUKS passphrase can be typed over SSH at boot."
    info "Usage after reboot: ssh -p 2222 root@<pi-ip>"
    info "Then type: cryptroot-unlock"
    info "Then enter your LUKS passphrase."
    info ""

    # Determine the correct authorized_keys path for this OS version.
    local auth_keys_dir
    if $IS_TRIXIE; then
        auth_keys_dir="/etc/dropbear/initramfs"
        info "Trixie authorized_keys path: $auth_keys_dir/authorized_keys"
    else
        auth_keys_dir="/etc/dropbear-initramfs"
        info "Bookworm authorized_keys path: $auth_keys_dir/authorized_keys"
    fi

    # Find the calling user's public key.
    local src_key=""
    local calling_user="${SUDO_USER:-${USER:-}}"
    if [[ -n "$calling_user" ]]; then
        local home; home="$(getent passwd "$calling_user" | cut -d: -f6)"
        [[ -f "$home/.ssh/authorized_keys" ]] && src_key="$home/.ssh/authorized_keys"
        [[ -z "$src_key" && -f "$home/.ssh/id_rsa.pub"     ]] && src_key="$home/.ssh/id_rsa.pub"
        [[ -z "$src_key" && -f "$home/.ssh/id_ed25519.pub" ]] && src_key="$home/.ssh/id_ed25519.pub"
    fi

    if ! $DRY_RUN; then
        mkdir -p "$auth_keys_dir"
        chmod 700 "$auth_keys_dir"
    fi

    if [[ -n "$src_key" ]]; then
        run cp "$src_key" "$auth_keys_dir/authorized_keys"
        run chmod 600 "$auth_keys_dir/authorized_keys"
        info "Copied $src_key → $auth_keys_dir/authorized_keys"
    else
        warn "Could not find an SSH public key for user '$calling_user'."
        warn "Copy your PUBLIC key manually:"
        warn "  sudo cp ~/.ssh/authorized_keys $auth_keys_dir/authorized_keys"
        warn "  sudo chmod 600 $auth_keys_dir/authorized_keys"
        warn "Then rebuild the initramfs: sudo mkinitramfs -o $INITRAMFS_FILE \$(uname -r)"
    fi

    # IMPORTANT: dropbear in Debian does NOT support ed25519 keys.
    # If the user's key is ed25519, warn them to generate an RSA key.
    if [[ -n "$src_key" ]] && grep -q 'ed25519' "$src_key" 2>/dev/null; then
        warn "WARNING: Your SSH key appears to be ed25519."
        warn "dropbear-initramfs does NOT support ed25519 keys."
        warn "Generate an RSA key for headless unlock:"
        warn "  ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa_luks -N ''"
        warn "Then add id_rsa_luks.pub to $auth_keys_dir/authorized_keys."
    fi

    # Set the SSH port in the dropbear initramfs config.
    local dropbear_conf
    if $IS_TRIXIE; then
        dropbear_conf="/etc/dropbear/initramfs/dropbear.conf"
    else
        dropbear_conf="/etc/dropbear-initramfs/config"
    fi

    if [[ -f "$dropbear_conf" ]]; then
        if ! grep -qE '^DROPBEAR_OPTIONS=' "$dropbear_conf"; then
            $DRY_RUN || printf 'DROPBEAR_OPTIONS="-p 2222 -s -j -k"\n' >> "$dropbear_conf"
            info "Set dropbear port 2222 in $dropbear_conf"
        fi
    fi
}

# ── Step: Phase-2 cheatsheet ─────────────────────────────────────────────────
# This is the most important output of Phase 1.  It is a text file that you
# read on your laptop while typing commands at the initramfs shell.  It explains
# every command you need to type, why you are typing it, and what to do if
# something goes wrong.
_write_phase2_cheatsheet() {
    step "Writing Phase-2 cheatsheet to $PHASE2_HINTS"

    local mode_commands
    if $IN_PLACE; then
        mode_commands="$(cat <<EOF

# ═══════════════════════════════════════════════════════════════════════════
# MODE: --in-place  (no USB stick — encrypts the partition in place)
# ═══════════════════════════════════════════════════════════════════════════
#
# How in-place encryption works:
#   cryptsetup reencrypt --encrypt reads plaintext data from the partition,
#   encrypts it, and writes it back to the SAME partition — sector by sector.
#   It reserves 32 MiB at the END of the partition for the LUKS2 header.
#   The data you see shrinks by 32 MiB.
#
# Risk: if power fails mid-reencrypt the partition is partially encrypted.
#   cryptsetup reencrypt has a recovery journal but you need working mains
#   power.  Do NOT use this on battery.
#
# ─── STEP 1: Check the root partition is healthy ───────────────────────────
#
# e2fsck checks the filesystem for errors before we touch it.
# If it finds errors, it will ask whether to fix them — answer 'y' to all.

e2fsck -fy ${ROOT_DEV}

# ─── STEP 2: Encrypt the partition in-place ────────────────────────────────
#
# cryptsetup reencrypt will ask for a passphrase TWICE (Enter + Verify).
# This is your BOOT PASSPHRASE.  Every time the Pi powers on from now on,
# you will see:  "Please unlock disk ${MAPPER_NAME}:"
# and must type this SAME passphrase.  Write it down NOW before continuing.
#
# Explanation of the options:
#   --encrypt            : we are encrypting a currently-plaintext partition
#   --reduce-device-size 32M : reserves 32 MiB for the LUKS2 metadata header
#   --type luks2         : LUKS version 2 (supports Argon2 key derivation)
#   --cipher aes-xts-plain64 : AES in XTS mode (standard for disk encryption)
#   --key-size 256       : 256-bit key (XTS uses two keys internally = 512 bits effective)
#   --hash sha256        : SHA-256 for the header checksum
#   --pbkdf argon2id     : Argon2id key derivation — memory-hard, resists GPU brute force
#   --iter-time 5000     : spend 5 seconds deriving the key (makes brute force harder)
#   --use-random         : use /dev/random for key material (more entropy)

cryptsetup reencrypt \\
    --encrypt \\
    --reduce-device-size 32M \\
    --type luks2 \\
    --cipher aes-xts-plain64 \\
    --key-size 256 \\
    --hash sha256 \\
    --pbkdf argon2id \\
    --iter-time 5000 \\
    --use-random \\
    ${ROOT_DEV}

# ─── STEP 3: Open the newly-encrypted volume ───────────────────────────────
#
# luksOpen reads the LUKS header, prompts for the passphrase, and creates
# /dev/mapper/${MAPPER_NAME} — the decrypted view of the partition.

cryptsetup luksOpen ${ROOT_DEV} ${MAPPER_NAME}

# ─── STEP 4: Check and expand the filesystem ───────────────────────────────
#
# After encryption the filesystem still thinks it is the old (smaller) size.
# resize2fs without a size argument expands it to fill the available space.
# e2fsck must run first (resize2fs refuses to resize an unchecked FS).

e2fsck -fy /dev/mapper/${MAPPER_NAME}
resize2fs /dev/mapper/${MAPPER_NAME}

# ─── STEP 5: Continue booting ──────────────────────────────────────────────
#
# 'exit' tells the initramfs to continue the boot sequence.
# The kernel will mount /dev/mapper/${MAPPER_NAME} as / and bring up systemd.

exit

EOF
)"
    else
        mode_commands="$(cat <<EOF

# ═══════════════════════════════════════════════════════════════════════════
# MODE: dd backup to USB stick (default — safer, more transparent)
# ═══════════════════════════════════════════════════════════════════════════
#
# How the dd method works:
#   1. Shrink the filesystem to the minimum size (to reduce backup time).
#   2. dd copies the shrunken data to the USB stick (byte for byte).
#   3. cryptsetup luksFormat overwrites the partition with a LUKS2 header.
#   4. cryptsetup luksOpen creates /dev/mapper/${MAPPER_NAME} (the decrypted view).
#   5. dd copies the data back from USB into the encrypted partition.
#   6. resize2fs expands the filesystem to fill the full partition again.
#
# This method is safer than in-place because you have a complete backup on
# the USB stick.  If anything goes wrong at step 3-5, the data is still safe.
#
# ─── STEP 1: Identify your USB stick ───────────────────────────────────────
#
# Run lsblk to see all attached drives.  Your USB stick will appear as
# /dev/sda (or /dev/sdb if you have another USB device).  It must be LARGER
# than the used portion of your root partition.
# Your root partition is: ${ROOT_DEV}

lsblk

# ─── STEP 2: Check the root filesystem for errors ──────────────────────────
#
# e2fsck -fy checks the ext4 filesystem on the raw partition.
# -f = force check even if FS is marked clean
# -y = automatically answer 'yes' to all repair questions
# This MUST succeed before we can shrink.  If it fails, the filesystem has
# corruption and you should restore from your pre-FDE backup instead.

e2fsck -fy ${ROOT_DEV}

# ─── STEP 3: Shrink the filesystem to the minimum size ─────────────────────
#
# resize2fs -fM shrinks the ext4 filesystem to the absolute minimum size.
# -f = force the resize
# -M = minimum size
# -p = show progress
#
# IMPORTANT: Read the output.  It will print something like:
#   The filesystem on /dev/mmcblk0p2 is now 1234567 (4k) blocks long.
# Write down the NUMBER (1234567 in this example).  You need it in Step 4.

resize2fs -fM -p ${ROOT_DEV}

# ─── STEP 4: Copy the shrunken filesystem to USB ───────────────────────────
#
# Replace:
#   XXXXX   with the block COUNT from the resize2fs output above
#   /dev/sda with the actual USB device you saw in lsblk
#
# dd copies the raw partition data block by block.
# bs=4k  = 4 KiB block size (matches the ext4 block size from resize2fs)
# count= = how many blocks to copy (= the number from resize2fs output)
# status=progress = show copy speed and bytes transferred
#
# This will take several minutes.  Do not interrupt it.

USB_DEV=/dev/sda      # <-- change this if your USB stick is at a different path
dd bs=4k count=XXXXX if=${ROOT_DEV} of=\$USB_DEV status=progress

# ─── STEP 5: LUKS-format the root partition ────────────────────────────────
#
# cryptsetup luksFormat DESTROYS all data on ${ROOT_DEV} and writes a
# new LUKS2 header.  Your data is safe on the USB stick from Step 4.
#
# cryptsetup will ask:
#   WARNING: Device ${ROOT_DEV} already contains a 'ext4' signature.
#   Are you sure? (Type uppercase yes): YES
# Type: YES  (all caps)
#
# Then it will ask:
#   Enter passphrase for ${ROOT_DEV}:
# Then:
#   Verify passphrase:
#
# THIS IS YOUR BOOT PASSPHRASE.  Every time the Pi powers on from now on,
# the boot loader will show:  "Please unlock disk ${MAPPER_NAME}:"
# and you must type this SAME passphrase.
#
# Requirements:
#   * At least 16 characters
#   * Something you can remember but not easily guess
#   * NOT the same as your Linux login password
#   * WRITE IT DOWN.  There is NO recovery if you forget it.
#
# Explanation of the options:
#   --type luks2         : LUKS version 2 (modern, supports Argon2)
#   --cipher aes-xts-plain64 : AES in XTS mode (industry standard for disks)
#   --hash sha256        : hash for LUKS header integrity
#   --iter-time 5000     : spend 5 seconds deriving the encryption key from
#                          your passphrase (makes brute-force much harder)
#   --key-size 256       : 256-bit key (XTS uses this as two 128-bit keys)
#   --pbkdf argon2id     : Argon2id — memory-hard key derivation that resists
#                          GPU brute force attacks (better than PBKDF2 or bcrypt)
#   --use-random         : use /dev/random (more entropy) for key material

cryptsetup luksFormat \\
    --type luks2 \\
    --cipher aes-xts-plain64 \\
    --hash sha256 \\
    --iter-time 5000 \\
    --key-size 256 \\
    --pbkdf argon2id \\
    --use-random \\
    ${ROOT_DEV}

# ─── STEP 6: Open the new LUKS volume ──────────────────────────────────────
#
# luksOpen reads the LUKS header you just created, asks for the passphrase,
# and creates /dev/mapper/${MAPPER_NAME} — the decrypted view of the partition.
# All reads and writes to /dev/mapper/${MAPPER_NAME} go through AES-XTS
# transparently.

cryptsetup luksOpen ${ROOT_DEV} ${MAPPER_NAME}

# ─── STEP 7: Restore the OS data from USB ──────────────────────────────────
#
# Replace:
#   XXXXX   with the SAME block count you used in Step 4
#   /dev/sda with your USB device
#
# This copies the OS data back from USB into the encrypted partition.
# The data passes through AES-XTS encryption on every write, so it is
# encrypted on the partition but appears as normal ext4 through the mapper.
#
# This will take the same amount of time as Step 4.

dd bs=4k count=XXXXX if=\$USB_DEV of=/dev/mapper/${MAPPER_NAME} status=progress

# ─── STEP 8: Check and expand the filesystem ───────────────────────────────
#
# After restoring, the filesystem is still the shrunken size (from Step 3).
# We need to:
#   a) e2fsck: verify the restored filesystem is intact
#   b) resize2fs: expand it to fill the full LUKS volume
#      (the LUKS volume is slightly smaller than the raw partition because
#       the LUKS header itself takes a few MiB)

e2fsck -fy /dev/mapper/${MAPPER_NAME}
resize2fs /dev/mapper/${MAPPER_NAME}

# ─── STEP 9: Continue booting ──────────────────────────────────────────────
#
# 'exit' tells the initramfs to continue the boot sequence.
# The kernel will:
#   1. See root=/dev/mapper/${MAPPER_NAME} in cmdline.txt
#   2. The crypttab entry ensures the LUKS volume is already open
#   3. Mount /dev/mapper/${MAPPER_NAME} as /
#   4. Hand control to systemd
#
# You will be prompted for the passphrase AGAIN — this is crypttab's
# second unlock attempt (belt-and-suspenders).  It is expected.

exit

EOF
)"
    fi

    if ! $DRY_RUN; then
        cat > "$PHASE2_HINTS" <<CHEATSHEET
═══════════════════════════════════════════════════════════════════════════════
 SOMNI-Guard FULL-DISK ENCRYPTION — Phase 2 Cheatsheet
 Generated: ${TIMESTAMP}
 OS: ${OS_CODENAME}
═══════════════════════════════════════════════════════════════════════════════

WHAT YOU ARE ABOUT TO DO
─────────────────────────────────────────────────────────────────────────────
You are reading this because Phase 1 (--prepare) has already run.
It added "break=init" to ${CMDLINE_FILE} which dropped you into this
BusyBox initramfs shell instead of booting normally.

THIS IS EXPECTED.  You are in the right place.

The BusyBox prompt looks like this:
    BusyBox v1.xx.x (Debian) built-in shell (ash)
    Enter 'help' for a list of built-in commands.

    (initramfs) _

From here, the root partition (${ROOT_DEV}) is NOT mounted.  That means
you can encrypt it.  The commands below will:
  1. Back up the OS data to a USB stick
  2. LUKS-format the partition (overwrite with encryption header)
  3. Restore the OS data into the encrypted partition
  4. Expand the filesystem back to full size
  5. 'exit' to continue booting

YOUR DEVICES
─────────────────────────────────────────────────────────────────────────────
  Root partition : ${ROOT_DEV}
  Boot partition : ${BOOT_DEV}
  Mapper name    : ${MAPPER_NAME}    (will appear as /dev/mapper/${MAPPER_NAME})
  Mode           : $($IN_PLACE && echo "in-place (no USB needed)" || echo "dd backup to USB (USB required)")

PASSPHRASE — WRITE THIS DOWN BEFORE CONTINUING
─────────────────────────────────────────────────────────────────────────────
The luksFormat command will ask:
    Enter passphrase for ${ROOT_DEV}:
    Verify passphrase:

THE PASSPHRASE YOU TYPE IS YOUR BOOT PASSPHRASE.
Every time the Pi 5 powers on, you will see:
    Please unlock disk ${MAPPER_NAME}:
and you must type this same passphrase.

Requirements:
  * At least 16 characters
  * Memorable (no recovery if you forget — the data is gone)
  * NOT the same as your Linux login password
  * Store it in a password manager on another device OR on paper in a safe

═══════════════════════════════════════════════════════════════════════════════
 COMMANDS TO TYPE AT THE (initramfs) PROMPT
═══════════════════════════════════════════════════════════════════════════════
${mode_commands}
═══════════════════════════════════════════════════════════════════════════════
 AFTER 'exit' — WHAT HAPPENS NEXT
═══════════════════════════════════════════════════════════════════════════════

The kernel continues booting.  It will:
  1. Mount /dev/mapper/${MAPPER_NAME} as /  (your encrypted root)
  2. Start systemd
  3. Bring up all services normally

You may see a second passphrase prompt from /etc/crypttab.  Enter the SAME
passphrase.  This is the systemd-level unlock (belt-and-suspenders).

ONCE FULLY BOOTED — RUN PHASE 3
─────────────────────────────────────────────────────────────────────────────
From the fully-booted encrypted Pi, run:
    sudo bash scripts/setup_full_disk_encryption_pi5.sh --finalize

Phase 3:
  * Removes 'break=init' from cmdline.txt so future boots go straight from
    the passphrase prompt to the desktop (no more initramfs shell drop).
  * Rebuilds the initramfs cleanly.
  * Marks the FDE setup as finalised.

IF SOMETHING WENT WRONG BEFORE luksFormat
─────────────────────────────────────────────────────────────────────────────
If you have NOT yet typed 'cryptsetup luksFormat' above, you can still
abort and roll back to the plaintext system:
  1. Type: exit      (the kernel boots the still-plaintext root partition)
  2. Once booted, run:
       sudo bash scripts/setup_full_disk_encryption_pi5.sh --rollback
  Rollback restores the original cmdline.txt, config.txt, fstab, crypttab
  from the Phase-1 backup.

═══════════════════════════════════════════════════════════════════════════════
CHEATSHEET
        chmod 644 "$PHASE2_HINTS"
    fi
    info "Cheatsheet written to $PHASE2_HINTS"
    info "  -> Read it:  cat $PHASE2_HINTS"
    info "  -> Copy it:  scp pi@<ip>:$PHASE2_HINTS ~/PHASE2.txt"
}

# ---------------------------------------------------------------------------
# Phase 3  --finalize
# ---------------------------------------------------------------------------
phase3_finalize() {
    step "Phase 3 — Finalising encrypted-root boot configuration"
    info ""
    info "Phase 3 does two things:"
    info "  1. Removes 'break=init' from cmdline.txt so future boots go"
    info "     straight from the passphrase prompt to the desktop."
    info "  2. Rebuilds the initramfs cleanly (without Phase-2 maintenance hooks)."
    info ""

    detect_os_version
    detect_boot_layout
    detect_root_device
    detect_boot_device

    if [[ "$ROOT_DEV" != /dev/mapper/* ]]; then
        die "Root is on $ROOT_DEV (not /dev/mapper/*).
  This means the system did not boot from the encrypted partition.
  Possible causes:
    - Phase 2 (the initramfs shell commands) did not complete successfully.
    - You are still booting the plaintext partition.
  Check with: sudo bash $SCRIPT_NAME --status
  Then redo Phase 2 if needed, or --rollback to return to plaintext."
    fi

    confirm \
        "Phase 3 will strip 'break=init' from $CMDLINE_FILE and rebuild the initramfs.  After this, every boot prompts for the LUKS passphrase and goes straight to the desktop." \
        "FINALIZE"

    backup_file "$CMDLINE_FILE"

    local old new
    old="$(tr -d '\n' < "$CMDLINE_FILE")"
    new="$(sed -E 's| *break=init||g; s|  +| |g' <<<"$old")"

    if [[ "$new" != "$old" ]]; then
        if ! $DRY_RUN; then
            printf '%s\n' "$new" > "$CMDLINE_FILE"
        fi
        info "Removed 'break=init' from cmdline.txt."
        info "New cmdline.txt: $new"
    else
        info "cmdline.txt already had no 'break=init' — nothing to remove."
    fi

    step "Rebuilding initramfs (final, clean build)"
    info "This rebuild uses the same CRYPTSETUP=y + mkinitramfs approach as Phase 1,"
    info "but without the Phase-2 maintenance hooks.  The result is the initramfs"
    info "that will run on every subsequent boot."
    if ! $DRY_RUN; then
        CRYPTSETUP=y mkinitramfs -o "$INITRAMFS_FILE" "$(uname -r)"
    fi
    info "initramfs rebuilt."

    write_phase finalized

    # Remove the Phase-2 cheatsheet.  It does not contain the passphrase,
    # but it does contain the device layout.  Shred it so it cannot be
    # recovered from the boot partition with a card reader.
    if [[ -f "$PHASE2_HINTS" ]]; then
        $DRY_RUN || shred -u "$PHASE2_HINTS" 2>/dev/null || rm -f "$PHASE2_HINTS"
        info "Shredded $PHASE2_HINTS (no longer needed on the boot partition)."
    fi

    echo
    info "================================================================="
    info " Phase 3 complete.  Encrypted boot is fully active."
    info ""
    info " On every boot from now on the kernel will pause and display:"
    info ""
    info "     Please unlock disk ${MAPPER_NAME}:"
    info ""
    info " Type the passphrase you chose in Phase 2.  The Pi will boot"
    info " normally.  No passphrase = no boot."
    info ""
    info " Verify by rebooting:  sudo reboot"
    info ""
    info " Check status any time:  sudo bash $SCRIPT_NAME --status"
    info "================================================================="
}

# ---------------------------------------------------------------------------
# Rollback  (only safe BEFORE Phase 2's luksFormat)
# ---------------------------------------------------------------------------
do_rollback() {
    step "Rolling back Phase-1 changes"
    info ""
    info "Rollback restores the original cmdline.txt, config.txt, fstab, and"
    info "crypttab from the Phase-1 backup directory."
    info ""
    info "SAFE ONLY if you have NOT yet typed 'cryptsetup luksFormat' in Phase 2."
    info "Once the partition is LUKS-formatted, rollback would make it unbootable."
    info ""

    detect_os_version
    detect_boot_layout
    detect_root_device

    if [[ "$ROOT_DEV" == /dev/mapper/* ]]; then
        die "Root is already encrypted ($ROOT_DEV).  Rollback would make the system
  unbootable.  Refusing.  Use --status to see the current state."
    fi

    confirm \
        "Rollback will restore cmdline.txt, config.txt, fstab, crypttab from the most recent Phase-1 backup.  Only safe BEFORE you ran luksFormat." \
        "ROLLBACK"

    local latest
    latest="$(ls -1dt "${STATE_DIR}"/backups.* 2>/dev/null | head -1 || true)"
    [[ -n "$latest" ]] || die "No backups found under ${STATE_DIR}/backups.*"

    info "Restoring from: $latest"
    for src in "$latest"/*; do
        [[ -f "$src" ]] || continue
        local rel="${src##*/}"
        local dst="/${rel//__/\/}"
        $DRY_RUN || cp -a "$src" "$dst"
        info "Restored: $dst"
    done

    # Belt-and-suspenders: also strip the LUKS-specific params from cmdline.txt
    # in case the backup does not exactly match (e.g. partial prepare run).
    local old new
    old="$(tr -d '\n' < "$CMDLINE_FILE")"
    new="$(sed -E 's| *cryptdevice=[^ ]+||g; s| *break=init||g; s|  +| |g' <<<"$old")"
    if [[ "$new" != "$old" ]]; then
        warn "Stripped cryptdevice= and break=init from cmdline.txt as safety net."
        warn "Verify that root= still points at your actual root partition before rebooting."
        $DRY_RUN || printf '%s\n' "$new" > "$CMDLINE_FILE"
    fi

    $DRY_RUN || rm -f "$PHASE2_HINTS" 2>/dev/null || true

    write_phase none
    info ""
    info "Rollback complete."
    info "Reboot and confirm the system comes up normally (unencrypted):"
    info "  sudo reboot"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"
    check_root
    init_logging
    print_banner

    case "$ACTION" in
        status)    report_status ;;
        check)     do_check ;;
        prepare)   phase1_prepare ;;
        finalize)  phase3_finalize ;;
        rollback)  do_rollback ;;
        *)         die "Unhandled action: $ACTION" ;;
    esac
}

main "$@"
