#!/usr/bin/env bash
# =============================================================================
# SOMNI-Guard — File-level Secrets Encryption for Raspberry Pi 5
# =============================================================================
#
# Encrypts the gateway's sensitive files into a password-protected vault.
# After running this script, the gateway only starts when you run:
#
#   sudo somniguard-start
#
# That command asks for the passphrase, decrypts secrets into RAM (never
# writes plaintext back to the SD card), starts the gateway, and wipes
# secrets from memory automatically when the gateway stops.
#
# WHAT GETS ENCRYPTED
#   /etc/somniguard/env                Flask secret, HMAC key, hotspot password
#   /etc/somniguard/certs/ca.key       CA private key
#   /etc/somniguard/certs/server.key   TLS server private key
#   /etc/somniguard/certs/pico_client.key  Pico mTLS client private key
#
# WHAT STAYS ON DISK (not secret — public material only)
#   /etc/somniguard/certs/*.crt / *.pem   Public certificates
#   /var/lib/somniguard/somni.db           Database (use FDE for full protection)
#   /var/log/somniguard/                   Audit log
#
# Encryption: AES-256-CBC, PBKDF2-SHA256, 600 000 iterations, random salt.
# Vault:      /var/lib/somniguard-vault/   (mode 700, root-only)
# Runtime:    /run/somniguard-secrets/     (tmpfs — RAM only, cleared on exit)
#
# USAGE
#   sudo bash scripts/setup_file_encryption_pi5.sh [OPTIONS]
#
# OPTIONS
#   (no args)      Full setup — encrypt secrets, install somniguard-start
#   --status       Show vault status and whether each secret is encrypted
#   --rotate-key   Re-encrypt the vault with a new passphrase
#   --remove       Decrypt vault back to plaintext and undo all changes
#   --dry-run      Print what would happen; make no changes
#   --help, -h     Show this help and exit
#
# AFTER RUNNING
#   Start gateway:    sudo somniguard-start
#   Check status:     sudo bash scripts/setup_file_encryption_pi5.sh --status
#   Change passphrase: sudo bash scripts/setup_file_encryption_pi5.sh --rotate-key
#   Undo everything:  sudo bash scripts/setup_file_encryption_pi5.sh --remove
#
# =============================================================================
set -euo pipefail

readonly SCRIPT_VERSION="1.0.0"
readonly SCRIPT_NAME="$(basename "$0")"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
readonly VAULT_DIR="/var/lib/somniguard-vault"
readonly SECRETS_TMPFS="/run/somniguard-secrets"
readonly START_CMD="/usr/local/bin/somniguard-start"
readonly DROPIN_DIR="/etc/systemd/system/somniguard-gateway.service.d"
readonly DROPIN_FILE="${DROPIN_DIR}/somni-vault.conf"
readonly ENV_FILE="/etc/somniguard/env"
readonly CERTS_DIR="/etc/somniguard/certs"
readonly GATEWAY_UNIT="somniguard-gateway.service"

# Files to encrypt: "source-path:vault-relative-name"
# server.key is regenerated each start but vaulted for a clean baseline.
readonly -a VAULT_ENTRIES=(
    "${ENV_FILE}:env"
    "${CERTS_DIR}/ca.key:certs/ca.key"
    "${CERTS_DIR}/server.key:certs/server.key"
    "${CERTS_DIR}/pico_client.key:certs/pico_client.key"
)

readonly CIPHER="aes-256-cbc"
readonly KDF_ITER=600000

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    RED='\033[0;31m' YELLOW='\033[1;33m' GREEN='\033[0;32m'
    CYAN='\033[0;36m' BOLD='\033[1m' RESET='\033[0m'
else
    RED='' YELLOW='' GREEN='' CYAN='' BOLD='' RESET=''
fi

# ---------------------------------------------------------------------------
# Runtime flags
# ---------------------------------------------------------------------------
DRY_RUN=false
STATUS_ONLY=false
REMOVE=false
ROTATE_KEY=false

# PASSPHRASE is set by prompt functions and read by encrypt_file / decrypt_file.
PASSPHRASE=""

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
info()  { printf "${GREEN}[INFO   ]${RESET} %s\n" "$*"; }
warn()  { printf "${YELLOW}[WARN   ]${RESET} %s\n" "$*"; }
error() { printf "${RED}[ERROR  ]${RESET} %s\n" "$*" >&2; }
step()  { printf "\n${CYAN}[STEP   ]${RESET} %s\n" "$*"; }
die()   { error "$*"; exit 1; }

run() {
    if $DRY_RUN; then
        info "[DRY-RUN] $*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Banner — printed before any checks so the user always sees output
# ---------------------------------------------------------------------------
print_banner() {
    printf "${BOLD}${CYAN}"
    cat <<'BANNER'
 ___  ___  __  __ _  _ ___   __   ___   _   _   _ _  _____
/ __|/ _ \|  \/  | \| |_ _| / _| / __| /_\ | | | | ||_   _|
\__ \ (_) | |\/| | .` || | \_ \| (__ / _ \| |_| | |__| |
|___/\___/|_|  |_|_|\_|___| |__/ \___/_/ \_\___/|____|_|

      File-level Secrets Encryption  |  Raspberry Pi 5
      NightWatchGuard / SOMNI-Guard Gateway
BANNER
    printf "${RESET}\n"
    info "Version : $SCRIPT_VERSION"
    echo
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
    sed -n '/^# USAGE/,/^# ===/p' "$0" | sed -e 's/^# \{0,1\}//' -e '/^=*$/d'
    exit 0
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --status)     STATUS_ONLY=true; shift;;
            --remove)     REMOVE=true; shift;;
            --rotate-key) ROTATE_KEY=true; shift;;
            --dry-run)    DRY_RUN=true; shift;;
            --help|-h)    usage;;
            *)            die "Unknown option: $1  (use --help)";;
        esac
    done
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
check_root() {
    [[ $EUID -eq 0 ]] || die "Run as root:  sudo bash $SCRIPT_NAME"
}

check_openssl() {
    command -v openssl &>/dev/null \
        || die "openssl not found — install:  sudo apt-get install -y openssl"
    # Verify PBKDF2 support (OpenSSL 1.1.1+)
    openssl enc -"$CIPHER" -pbkdf2 -iter 1 -pass pass:test \
        -in /dev/null -out /dev/null 2>/dev/null \
        || die "openssl too old — need OpenSSL 1.1.1+ for PBKDF2 (-pbkdf2) support."
}

check_gateway_installed() {
    [[ -f "$ENV_FILE" ]] \
        || die "$ENV_FILE not found — run setup_gateway_pi5.sh first."
    [[ -d "$CERTS_DIR" ]] \
        || die "$CERTS_DIR not found — run setup_gateway_pi5.sh first."
}

# ---------------------------------------------------------------------------
# Passphrase prompts
# ---------------------------------------------------------------------------
prompt_passphrase_new() {
    local pass1 pass2
    echo
    printf "${BOLD}${YELLOW}"
    cat <<'MSG'
─────────────────────────────────────────────────────────────────────
 Choose a passphrase for the SOMNI-Guard secret vault.
 ─────────────────────────────────────────────────────────────────────
  • NOT your Linux login, dashboard password, or Pico HMAC key.
  • Required every time you run  sudo somniguard-start.
  • 16+ characters strongly recommended.
  • No recovery if forgotten — write it down and store it safely.
 ─────────────────────────────────────────────────────────────────────
MSG
    printf "${RESET}\n"
    read -rs -p "  Enter new passphrase:    " pass1; echo
    read -rs -p "  Confirm passphrase:      " pass2; echo
    echo
    [[ "$pass1" == "$pass2" ]] || die "Passphrases do not match."
    [[ ${#pass1} -ge 8 ]]      || die "Passphrase must be at least 8 characters."
    PASSPHRASE="$pass1"
}

prompt_passphrase_existing() {
    echo
    read -rs -p "Enter SOMNI-Guard vault passphrase: " PASSPHRASE; echo
    echo
    [[ -n "$PASSPHRASE" ]] || die "Passphrase cannot be empty."
}

# ---------------------------------------------------------------------------
# Crypto helpers — passphrase passed via fd:3, never via argv
# ---------------------------------------------------------------------------
encrypt_file() {
    local src="$1" dst="$2"
    openssl enc -"$CIPHER" -pbkdf2 -iter "$KDF_ITER" -salt \
        -in "$src" -out "$dst" -pass fd:3 3<<<"$PASSPHRASE"
}

decrypt_file() {
    local src="$1" dst="$2"
    openssl enc -d -"$CIPHER" -pbkdf2 -iter "$KDF_ITER" \
        -in "$src" -out "$dst" -pass fd:3 3<<<"$PASSPHRASE"
}

verify_passphrase() {
    local first_enc="${VAULT_DIR}/env.enc"
    [[ -f "$first_enc" ]] || die "Vault not set up (env.enc missing). Run without flags first."
    local tmp; tmp="$(mktemp)"
    if ! decrypt_file "$first_enc" "$tmp" 2>/dev/null; then
        rm -f "$tmp"
        die "Wrong passphrase."
    fi
    rm -f "$tmp"
}

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
report_status() {
    step "Vault"
    if [[ ! -d "$VAULT_DIR" ]]; then
        warn "Vault not found at $VAULT_DIR — encryption not set up yet."
        warn "Run:  sudo bash $SCRIPT_NAME"
        return
    fi
    info "Location : $VAULT_DIR"
    for entry in "${VAULT_ENTRIES[@]}"; do
        local src="${entry%%:*}"
        local rel="${entry##*:}"
        local enc="${VAULT_DIR}/${rel}.enc"
        if [[ -f "$enc" ]]; then
            local sz; sz="$(du -h "$enc" | awk '{print $1}')"
            info "  ENCRYPTED  ${src}  →  ${enc}  (${sz})"
        else
            warn "  MISSING    ${src}  →  ${enc}"
        fi
    done

    step "Systemd drop-in"
    if [[ -f "$DROPIN_FILE" ]]; then
        info "  Present: $DROPIN_FILE"
        grep -E 'EnvironmentFile' "$DROPIN_FILE" 2>/dev/null | sed 's/^/    /' || true
    else
        warn "  Missing: $DROPIN_FILE"
        warn "  Gateway will look for plaintext env — re-run setup."
    fi

    step "Startup command"
    if [[ -x "$START_CMD" ]]; then
        info "  Installed: $START_CMD"
    else
        warn "  Not installed: $START_CMD"
    fi

    step "Gateway autostart"
    if systemctl is-enabled "$GATEWAY_UNIT" &>/dev/null; then
        warn "  $GATEWAY_UNIT is ENABLED (autostart ON)"
        warn "  It will fail at boot without the vault passphrase."
        warn "  Disable:  sudo systemctl disable $GATEWAY_UNIT"
    else
        info "  $GATEWAY_UNIT autostart: disabled  (correct — use somniguard-start)"
    fi

    step "Runtime secrets (this boot)"
    if mountpoint -q "$SECRETS_TMPFS" 2>/dev/null; then
        info "  $SECRETS_TMPFS : MOUNTED — gateway running with secrets in RAM"
    else
        info "  $SECRETS_TMPFS : not mounted — gateway stopped or not yet started"
    fi
}

# ---------------------------------------------------------------------------
# Encryption setup
# ---------------------------------------------------------------------------
setup_vault() {
    step "Creating vault directory"
    run mkdir -p "${VAULT_DIR}/certs"
    if ! $DRY_RUN; then
        chmod 700 "$VAULT_DIR"
        chmod 700 "${VAULT_DIR}/certs"
    fi
    info "Vault: $VAULT_DIR  (mode 700, root-only)"

    step "Encrypting sensitive files"
    local any_done=false
    for entry in "${VAULT_ENTRIES[@]}"; do
        local src="${entry%%:*}"
        local rel="${entry##*:}"
        local enc="${VAULT_DIR}/${rel}.enc"

        if [[ ! -f "$src" ]]; then
            warn "  $src not found — skipping"
            continue
        fi
        # Skip files that are already placeholders (setup already ran)
        if [[ "$src" == "$ENV_FILE" ]] && grep -q "SOMNI-Guard secret vault" "$src" 2>/dev/null; then
            if [[ -f "$enc" ]]; then
                info "  Already encrypted: $src"
                continue
            fi
        fi

        info "  Encrypting $src ..."
        if ! $DRY_RUN; then
            encrypt_file "$src" "$enc"
            chmod 600 "$enc"

            # Verify round-trip before shredding
            local tmp; tmp="$(mktemp)"
            if ! decrypt_file "$enc" "$tmp" 2>/dev/null || ! cmp -s "$src" "$tmp"; then
                rm -f "$tmp" "$enc"
                die "Round-trip verify failed for $src — vault aborted. Originals untouched."
            fi
            rm -f "$tmp"
            info "    → verified  ($enc)"
        else
            info "  [DRY-RUN] Would encrypt $src → $enc"
        fi
        any_done=true
    done
    $any_done || warn "No new files encrypted (all already in vault or missing)."
}

shred_originals() {
    step "Shredding plaintext originals"
    for entry in "${VAULT_ENTRIES[@]}"; do
        local src="${entry%%:*}"
        local rel="${entry##*:}"
        local enc="${VAULT_DIR}/${rel}.enc"

        [[ -f "$enc" ]] || continue   # only shred if vault copy confirmed
        [[ -f "$src" ]] || continue

        # Skip if it's already a placeholder
        if [[ "$src" == "$ENV_FILE" ]] && grep -q "SOMNI-Guard secret vault" "$src" 2>/dev/null; then
            info "  Already placeholder: $src"
            continue
        fi

        if ! $DRY_RUN; then
            shred -u "$src" 2>/dev/null || rm -f "$src"
            # For env, leave a placeholder so systemd doesn't log "file not found"
            if [[ "$src" == "$ENV_FILE" ]]; then
                printf '# SOMNI-Guard secret vault is enabled.\n# Run: sudo somniguard-start\n' \
                    > "$src"
                chmod 640 "$src"
                chown root:somniguard "$src" 2>/dev/null || true
            fi
        else
            info "  [DRY-RUN] Would shred $src"
            continue
        fi
        info "  Shredded: $src"
    done
}

# ---------------------------------------------------------------------------
# Systemd drop-in: redirect EnvironmentFile to the tmpfs path
# ---------------------------------------------------------------------------
install_dropin() {
    step "Installing systemd drop-in"
    run mkdir -p "$DROPIN_DIR"
    if ! $DRY_RUN; then
        cat > "$DROPIN_FILE" <<EOF
# Written by setup_file_encryption_pi5.sh — do not edit by hand.
# Points the gateway at the RAM-resident env file that somniguard-start
# creates.  The gateway cannot start without it (enforces passphrase gate).
[Service]
EnvironmentFile=
EnvironmentFile=${SECRETS_TMPFS}/env
EOF
        chmod 644 "$DROPIN_FILE"
        systemctl daemon-reload
    fi
    info "Drop-in: $DROPIN_FILE"
    info "Gateway EnvironmentFile → ${SECRETS_TMPFS}/env"
}

disable_autostart() {
    step "Disabling gateway autostart"
    if systemctl is-enabled "$GATEWAY_UNIT" &>/dev/null; then
        run systemctl stop  "$GATEWAY_UNIT" 2>/dev/null || true
        run systemctl disable "$GATEWAY_UNIT"
        info "Disabled $GATEWAY_UNIT autostart."
    else
        info "$GATEWAY_UNIT autostart already disabled."
    fi
    info "Start the gateway with:  sudo somniguard-start"
}

# ---------------------------------------------------------------------------
# Install /usr/local/bin/somniguard-start
# ---------------------------------------------------------------------------
install_start_cmd() {
    step "Installing ${START_CMD}"
    if $DRY_RUN; then
        info "[DRY-RUN] Would install $START_CMD"
        return
    fi

    cat > "$START_CMD" <<'STARTSCRIPT'
#!/usr/bin/env bash
# somniguard-start — decrypt secrets from vault and start the SOMNI-Guard gateway.
# Installed by setup_file_encryption_pi5.sh.
set -euo pipefail

VAULT_DIR="/var/lib/somniguard-vault"
SECRETS_TMPFS="/run/somniguard-secrets"
CERTS_DIR="/etc/somniguard/certs"
GATEWAY_UNIT="somniguard-gateway.service"
CIPHER="aes-256-cbc"
KDF_ITER=600000

if [[ -t 1 ]]; then
    RED='\033[0;31m' YELLOW='\033[1;33m' GREEN='\033[0;32m'
    CYAN='\033[0;36m' BOLD='\033[1m' RESET='\033[0m'
else
    RED='' YELLOW='' GREEN='' CYAN='' BOLD='' RESET=''
fi
info()  { printf "${GREEN}[INFO   ]${RESET} %s\n" "$*"; }
warn()  { printf "${YELLOW}[WARN   ]${RESET} %s\n" "$*"; }
die()   { printf "${RED}[ERROR  ]${RESET} %s\n" "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root:  sudo somniguard-start"
[[ -d "$VAULT_DIR" ]] || die "Vault not found at $VAULT_DIR — run setup first:\n  sudo bash scripts/setup_file_encryption_pi5.sh"

# ── Banner ─────────────────────────────────────────────────────────────────
printf "${BOLD}${CYAN}"
cat <<'BANNER'
  ___  ___  __  __ _  _ ___    ___ _____  _   ___ _____
 / __|/ _ \|  \/  | \| |_ _|  / __|_   _|/_\ | _ \_   _|
 \__ \ (_) | |\/| | .` || |   \__ \ | | / _ \|   / | |
 |___/\___/|_|  |_|_|\_|___|  |___/ |_|/_/ \_\_|_\ |_|

       SOMNI-Guard Gateway — Encrypted Mode
BANNER
printf "${RESET}\n"

# ── Already running? ───────────────────────────────────────────────────────
if mountpoint -q "$SECRETS_TMPFS" 2>/dev/null; then
    warn "Secrets tmpfs already mounted at $SECRETS_TMPFS."
    warn "The gateway may already be running:"
    warn "  sudo systemctl status $GATEWAY_UNIT"
    exit 1
fi

# ── Passphrase ─────────────────────────────────────────────────────────────
read -rs -p "Enter SOMNI-Guard passphrase: " PASSPHRASE
echo
[[ -n "$PASSPHRASE" ]] || die "Passphrase cannot be empty."

# ── Create RAM-only tmpfs ──────────────────────────────────────────────────
mkdir -p "$SECRETS_TMPFS"
mount -t tmpfs -o "size=32m,mode=700,uid=0,gid=0" tmpfs "$SECRETS_TMPFS"
mkdir -p "${SECRETS_TMPFS}/certs"
chmod 700 "${SECRETS_TMPFS}/certs"

# ── Cleanup on any exit ────────────────────────────────────────────────────
_cleanup() {
    echo
    info "Stopping gateway and wiping secrets from memory..."
    systemctl stop "$GATEWAY_UNIT" 2>/dev/null || true

    if mountpoint -q "$CERTS_DIR" 2>/dev/null; then
        umount "$CERTS_DIR" 2>/dev/null || true
    fi

    if mountpoint -q "$SECRETS_TMPFS" 2>/dev/null; then
        find "$SECRETS_TMPFS" -type f -exec shred -u {} \; 2>/dev/null || true
        umount "$SECRETS_TMPFS" 2>/dev/null || true
    fi
    info "Secrets cleared from memory."
}
trap _cleanup EXIT INT TERM

# ── Verify passphrase (decrypt env) ───────────────────────────────────────
info "Verifying passphrase..."
if ! openssl enc -d -"$CIPHER" -pbkdf2 -iter "$KDF_ITER" \
        -in "${VAULT_DIR}/env.enc" \
        -out "${SECRETS_TMPFS}/env" \
        -pass fd:3 3<<<"$PASSPHRASE" 2>/dev/null; then
    die "Wrong passphrase or corrupted vault."
fi
chmod 640 "${SECRETS_TMPFS}/env"
chown "root:somniguard" "${SECRETS_TMPFS}/env" 2>/dev/null || true
info "Passphrase accepted."

# ── Decrypt private keys ───────────────────────────────────────────────────
info "Decrypting secrets to RAM..."
for enc_file in "${VAULT_DIR}/certs/"*.enc; do
    [[ -f "$enc_file" ]] || continue
    key_name="$(basename "$enc_file" .enc)"
    out="${SECRETS_TMPFS}/certs/${key_name}"
    if ! openssl enc -d -"$CIPHER" -pbkdf2 -iter "$KDF_ITER" \
            -in "$enc_file" -out "$out" \
            -pass fd:3 3<<<"$PASSPHRASE" 2>/dev/null; then
        die "Failed to decrypt ${key_name} — vault may be corrupted."
    fi
    chmod 640 "$out"
    chown "root:somniguard" "$out" 2>/dev/null || true
    info "  Decrypted: ${key_name}"
done

# ── Clear passphrase (best-effort) ────────────────────────────────────────
PASSPHRASE=""
unset PASSPHRASE

# ── Copy public certs to tmpfs ────────────────────────────────────────────
for cert in "${CERTS_DIR}"/*.crt "${CERTS_DIR}"/*.pem; do
    [[ -f "$cert" ]] || continue
    cp "$cert" "${SECRETS_TMPFS}/certs/"
done

# ── Bind-mount tmpfs/certs over the real certs directory ──────────────────
# Gateway (and ExecStartPre cert-regen) reads/writes certs from RAM only.
if ! mountpoint -q "$CERTS_DIR" 2>/dev/null; then
    mount --bind "${SECRETS_TMPFS}/certs" "$CERTS_DIR"
fi
info "Certs bound from RAM at $CERTS_DIR"

# ── Start the gateway service ──────────────────────────────────────────────
info "Starting $GATEWAY_UNIT ..."
systemctl start "$GATEWAY_UNIT"
info "Gateway started."
echo
printf "${BOLD}${GREEN}  SOMNI-Guard is running.${RESET}\n"
printf "${CYAN}  Dashboard : https://10.42.0.1:5443/${RESET}\n"
printf "${CYAN}  Logs      : sudo journalctl -u ${GATEWAY_UNIT} -f${RESET}\n"
printf "${CYAN}  Stop      : Ctrl-C  (secrets wiped from memory automatically)${RESET}\n"
echo

# ── Wait for gateway to stop or user presses Ctrl-C ───────────────────────
while systemctl is-active --quiet "$GATEWAY_UNIT"; do
    sleep 5
done
info "Gateway stopped."
STARTSCRIPT

    chmod 755 "$START_CMD"
    info "Installed: $START_CMD"
}

# ---------------------------------------------------------------------------
# Remove / rollback — decrypt vault back to original paths
# ---------------------------------------------------------------------------
do_remove() {
    step "Removing vault and restoring plaintext files"
    [[ -d "$VAULT_DIR" ]] || die "Vault not found at $VAULT_DIR — nothing to remove."

    prompt_passphrase_existing
    verify_passphrase

    for entry in "${VAULT_ENTRIES[@]}"; do
        local src="${entry%%:*}"
        local rel="${entry##*:}"
        local enc="${VAULT_DIR}/${rel}.enc"
        [[ -f "$enc" ]] || continue

        info "  Restoring: $src"
        run decrypt_file "$enc" "$src"
        if ! $DRY_RUN; then
            chmod 640 "$src"
            chown root:somniguard "$src" 2>/dev/null || true
        fi
    done

    step "Wiping vault"
    if ! $DRY_RUN; then
        find "$VAULT_DIR" -name "*.enc" -exec shred -u {} \; 2>/dev/null || true
        rm -rf "$VAULT_DIR"
    else
        info "[DRY-RUN] Would wipe $VAULT_DIR"
    fi

    step "Removing systemd drop-in"
    run rm -f "$DROPIN_FILE"
    run rmdir "$DROPIN_DIR" 2>/dev/null || true
    if ! $DRY_RUN; then systemctl daemon-reload; fi

    step "Removing startup command"
    run rm -f "$START_CMD"

    step "Re-enabling gateway autostart"
    run systemctl enable "$GATEWAY_UNIT"

    info ""
    info "Rollback complete. Plaintext secrets restored."
    warn "Re-run without flags to re-encrypt at any time."
}

# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------
do_rotate_key() {
    step "Rotating vault passphrase"
    [[ -d "$VAULT_DIR" ]] || die "Vault not found — run without flags first."

    info "Step 1 of 2 — current passphrase"
    prompt_passphrase_existing
    verify_passphrase
    local old_pass="$PASSPHRASE"

    info "Step 2 of 2 — new passphrase"
    prompt_passphrase_new
    local new_pass="$PASSPHRASE"

    local tmp_dir; tmp_dir="$(mktemp -d)"
    chmod 700 "$tmp_dir"

    for entry in "${VAULT_ENTRIES[@]}"; do
        local rel="${entry##*:}"
        local enc="${VAULT_DIR}/${rel}.enc"
        [[ -f "$enc" ]] || continue

        local tmp="${tmp_dir}/$(basename "$enc").plain"
        PASSPHRASE="$old_pass"
        decrypt_file "$enc" "$tmp"

        PASSPHRASE="$new_pass"
        encrypt_file "$tmp" "${enc}.new"
        run mv "${enc}.new" "$enc"
        shred -u "$tmp" 2>/dev/null || rm -f "$tmp"
        info "  Re-encrypted: $enc"
    done

    rm -rf "$tmp_dir"
    PASSPHRASE=""; unset PASSPHRASE
    info "Key rotation complete."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    # Banner always first — visible even if we abort early
    print_banner

    parse_args "$@"
    check_root

    if $STATUS_ONLY; then report_status; exit 0; fi
    if $REMOVE;      then do_remove;     exit 0; fi
    if $ROTATE_KEY;  then do_rotate_key; exit 0; fi

    # Full setup path
    check_openssl
    check_gateway_installed

    if [[ -d "$VAULT_DIR" ]]; then
        warn "Vault already exists at $VAULT_DIR."
        warn "Re-running will fill in any missing vault entries."
        warn "To change the passphrase use:  --rotate-key"
        echo
    fi

    prompt_passphrase_new
    setup_vault
    shred_originals
    install_dropin
    disable_autostart
    install_start_cmd

    echo
    info "================================================================="
    info " Setup complete."
    info ""
    info " ENCRYPTED:"
    for entry in "${VAULT_ENTRIES[@]}"; do
        local src="${entry%%:*}"
        local rel="${entry##*:}"
        info "   ${src}"
        info "   → ${VAULT_DIR}/${rel}.enc"
    done
    info ""
    info " TO START THE GATEWAY:"
    info "   sudo somniguard-start"
    info ""
    info " Secrets live in RAM only while the gateway runs."
    info " Ctrl-C or service stop wipes them from memory."
    info ""
    info " BACKUP THE VAULT (do this now):"
    info "   sudo tar czf somniguard-vault-backup.tar.gz ${VAULT_DIR}"
    info "   # Copy the .tar.gz to a USB stick or offline storage"
    info "================================================================="
}

main "$@"
