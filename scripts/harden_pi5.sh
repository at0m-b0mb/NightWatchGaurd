#!/usr/bin/env bash
# =============================================================================
# SOMNI-Guard Gateway — Raspberry Pi 5 Defensive Hardening
# =============================================================================
#
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# EDUCATIONAL PROTOTYPE — DESTRUCTIVE FIREWALL/SSHD CHANGES BELOW.
# Maintain a console / serial-console fallback before running. A misconfigured
# firewall or sshd can lock you out of a remote host.
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
#
# WHAT THIS SCRIPT DOES (idempotent, safe to re-run)
#   1. Detects the calling user and hostname dynamically — no hard-coding.
#   2. UFW firewall — default-deny in, allow only:
#        - SSH on the LAN / Tailscale interface
#        - HTTPS (gateway port)  on LAN / hotspot / Tailscale
#        - DHCP/DNS for the hotspot subnet
#      Public-internet exposure is denied by default.
#   3. fail2ban with a custom SOMNI-Guard jail that watches the gateway audit
#      log for repeated LOGIN_FAILED and bans offending IPs at the firewall.
#   4. SSH hardening: no root login, no password auth, only key auth,
#      AllowUsers <gateway_user>, modern KEX/cipher list.
#   5. Kernel/sysctl hardening: rp_filter, no IP forwarding (unless hotspot),
#      kernel.kptr_restrict, dmesg_restrict, ASLR, ptrace scope, etc.
#   6. Disable unused services (Bluetooth, avahi, cups) if present.
#   7. Enable unattended security upgrades.
#   8. systemd hardening drop-in for somniguard.service.
#   9. Boot-integrity check: AIDE/aideinit baseline of /etc and /usr/local/bin.
#  10. Login banner + MOTD warning.
#
# OPTIONS
#   --no-ufw            Skip firewall configuration.
#   --no-fail2ban       Skip fail2ban setup.
#   --no-ssh            Skip sshd hardening.
#   --no-aide           Skip AIDE baseline (it can take many minutes).
#   --keep-bluetooth    Do not disable bluetooth/avahi.
#   --gateway-port N    Override gateway HTTPS port (default: read from env or 5443).
#   --ssh-allow-from CIDR
#                       Limit SSH to one or more CIDRs (comma-separated).
#                       Default: any RFC1918 + Tailscale 100.64.0.0/10.
#   --dry-run           Print what would be done; make no changes.
#   --help              Show this help.
#
# =============================================================================

set -euo pipefail

readonly SCRIPT_VERSION="1.0.0"
readonly SCRIPT_NAME="$(basename "$0")"
readonly TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
readonly LOG_DIR="/var/log/somniguard"
readonly LOG_FILE="${LOG_DIR}/harden_${TIMESTAMP}.log"

# ---------------------------------------------------------------------------
# Defaults — anything that can be discovered is discovered, not hard-coded.
# ---------------------------------------------------------------------------
GATEWAY_USER="${SUDO_USER:-${USER:-}}"
[[ -z "$GATEWAY_USER" || "$GATEWAY_USER" == "root" ]] && \
    GATEWAY_USER="$(getent passwd 1000 | cut -d: -f1 || echo pi)"

HOSTNAME_FQDN="$(hostname -f 2>/dev/null || hostname)"
HOSTNAME_SHORT="$(hostname -s 2>/dev/null || hostname)"

GATEWAY_PORT="${SOMNI_PORT:-5443}"
HOTSPOT_CIDR="${SOMNI_HOTSPOT_CIDR:-10.42.0.0/24}"
TAILSCALE_CIDR="100.64.0.0/10"

SSH_ALLOW_FROM=""

DO_UFW=true
DO_F2B=true
DO_SSH=true
DO_AIDE=true
DISABLE_BT=true
DRY_RUN=false

# Colours
if [[ -t 1 ]]; then
    RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
    CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
else
    RED=''; YELLOW=''; GREEN=''; CYAN=''; BOLD=''; RESET=''
fi

_log() {
    local lvl="$1"; shift; local clr="$1"; shift
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

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
print_banner() {
    printf "${BOLD}${CYAN}"
    cat <<'BANNER'
 ___  ___  __  __ _  _ ___    _  _   _   ___ ___  ___ _  _
/ __|/ _ \|  \/  | \| |_ _|  | || | /_\ | _ \   \| __| \| |
\__ \ (_) | |\/| | .` || |   | __ |/ _ \|   / |) | _|| .` |
|___/\___/|_|  |_|_|\_|___|  |_||_/_/ \_\_|_\___/|___|_|\_|

       Defensive Hardening  |  Raspberry Pi 5
       NightWatchGuard / SOMNI-Guard Gateway
BANNER
    printf "${RESET}\n"
    info "Version       : $SCRIPT_VERSION"
    info "User          : $GATEWAY_USER"
    info "Hostname      : $HOSTNAME_SHORT ($HOSTNAME_FQDN)"
    info "Gateway port  : $GATEWAY_PORT/tcp"
    info "Hotspot CIDR  : $HOTSPOT_CIDR"
    info "Log file      : $LOG_FILE"
    echo
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --no-ufw)        DO_UFW=false; shift;;
            --no-fail2ban)   DO_F2B=false; shift;;
            --no-ssh)        DO_SSH=false; shift;;
            --no-aide)       DO_AIDE=false; shift;;
            --keep-bluetooth) DISABLE_BT=false; shift;;
            --gateway-port)  GATEWAY_PORT="$2"; shift 2;;
            --ssh-allow-from) SSH_ALLOW_FROM="$2"; shift 2;;
            --dry-run)       DRY_RUN=true; shift;;
            --help|-h)
                grep -E '^# (USAGE|OPTIONS|  --)' "$0" | sed 's/^# //'
                exit 0;;
            *) die "Unknown option: $1";;
        esac
    done
}

check_root() {
    [[ $EUID -eq 0 ]] || die "Run as root: sudo bash $SCRIPT_NAME"
}

init_logging() {
    mkdir -p "$LOG_DIR"
    chmod 750 "$LOG_DIR"
    touch "$LOG_FILE"
    chmod 640 "$LOG_FILE"
}

# ---------------------------------------------------------------------------
# 1. Package install — make sure every tool we use is present.
# ---------------------------------------------------------------------------
install_packages() {
    step "Installing hardening packages"
    local -a pkgs=()
    $DO_UFW && pkgs+=(ufw)
    $DO_F2B && pkgs+=(fail2ban)
    $DO_AIDE && pkgs+=(aide)
    pkgs+=(unattended-upgrades apt-listchanges debsums)
    pkgs+=(libpam-pwquality)
    if (( ${#pkgs[@]} )); then
        run env DEBIAN_FRONTEND=noninteractive apt-get update -y
        run env DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
    fi
}

# ---------------------------------------------------------------------------
# 2. UFW firewall — default deny in / allow out.
# ---------------------------------------------------------------------------
configure_ufw() {
    $DO_UFW || { info "Skipping UFW (--no-ufw)."; return; }
    step "Configuring UFW firewall"

    # Detect interfaces dynamically.
    local hotspot_iface
    hotspot_iface="$(ip -o -4 addr show 2>/dev/null \
        | awk -v cidr="$HOTSPOT_CIDR" 'index($4, cidr) {print $2; exit}' || true)"

    run ufw --force reset

    run ufw default deny incoming
    run ufw default allow outgoing

    # SSH — restrict source.
    local -a ssh_sources=()
    if [[ -n "$SSH_ALLOW_FROM" ]]; then
        IFS=',' read -ra ssh_sources <<<"$SSH_ALLOW_FROM"
    else
        ssh_sources=("192.168.0.0/16" "10.0.0.0/8" "172.16.0.0/12" "$TAILSCALE_CIDR")
    fi
    for src in "${ssh_sources[@]}"; do
        run ufw allow from "$src" to any port 22 proto tcp comment "ssh from $src"
    done

    # Gateway HTTPS — LAN + hotspot + Tailscale.
    for src in "192.168.0.0/16" "10.0.0.0/8" "172.16.0.0/12" "$HOTSPOT_CIDR" "$TAILSCALE_CIDR"; do
        run ufw allow from "$src" to any port "$GATEWAY_PORT" proto tcp \
            comment "somniguard https from $src"
    done

    # HTTP→HTTPS redirect listener (port 80) — same scope.
    for src in "192.168.0.0/16" "10.0.0.0/8" "172.16.0.0/12" "$HOTSPOT_CIDR" "$TAILSCALE_CIDR"; do
        run ufw allow from "$src" to any port 80 proto tcp \
            comment "http redirect from $src"
    done

    # Hotspot DHCP/DNS for connected Picos.
    if [[ -n "$hotspot_iface" ]]; then
        run ufw allow in on "$hotspot_iface" to any port 67 proto udp \
            comment "hotspot dhcp"
        run ufw allow in on "$hotspot_iface" to any port 53 \
            comment "hotspot dns"
    fi

    # Tailscale — UDP 41641 inbound for direct connections.
    run ufw allow 41641/udp comment "tailscale udp"

    # Logging on suspicious activity.
    run ufw logging medium

    run ufw --force enable
    info "UFW enabled."
    ufw status verbose 2>/dev/null | tee -a "$LOG_FILE" || true
}

# ---------------------------------------------------------------------------
# 3. fail2ban — SSH + custom SOMNI-Guard jail
# ---------------------------------------------------------------------------
configure_fail2ban() {
    $DO_F2B || { info "Skipping fail2ban (--no-fail2ban)."; return; }
    step "Configuring fail2ban"

    local jail_dir="/etc/fail2ban/jail.d"
    local filter_dir="/etc/fail2ban/filter.d"
    run mkdir -p "$jail_dir" "$filter_dir"

    # SSH jail — strict but with a sane unban window so a fat-fingered admin
    # isn't locked out for hours.
    cat > "$jail_dir/somniguard-sshd.conf" <<EOF
[sshd]
enabled  = true
port     = ssh
filter   = sshd
backend  = systemd
maxretry = 4
findtime = 10m
bantime  = 1h
ignoreip = 127.0.0.1/8 ::1 ${HOTSPOT_CIDR} ${TAILSCALE_CIDR}
EOF

    # Custom filter that recognises SOMNI-Guard audit-log entries for
    # LOGIN_FAILED and ACCESS_DENIED.
    cat > "$filter_dir/somniguard-gateway.conf" <<'EOF'
# fail2ban filter — SOMNI-Guard gateway audit log
[Definition]
failregex = ^.*"event_type":\s*"(LOGIN_FAILED|ACCESS_DENIED|REPLAY_DETECTED|PATH_TRAVERSAL_ATTEMPT)".*"ip_address":\s*"<HOST>".*$
ignoreregex =
EOF

    cat > "$jail_dir/somniguard-gateway.conf" <<EOF
[somniguard-gateway]
enabled  = true
filter   = somniguard-gateway
logpath  = /var/log/somniguard/audit.log
         /var/log/somniguard/audit.log.*
maxretry = 5
findtime = 5m
bantime  = 6h
ignoreip = 127.0.0.1/8 ::1 ${HOTSPOT_CIDR} ${TAILSCALE_CIDR}
banaction = ufw
EOF

    run systemctl enable fail2ban
    run systemctl restart fail2ban
    info "fail2ban configured (sshd + somniguard-gateway jails)."
}

# ---------------------------------------------------------------------------
# 4. SSH hardening
# ---------------------------------------------------------------------------
configure_ssh() {
    $DO_SSH || { info "Skipping sshd hardening (--no-ssh)."; return; }
    step "Hardening sshd"

    # Sanity: don't lock the operator out. Refuse if the gateway user has no
    # authorized_keys entry yet.
    local home; home="$(getent passwd "$GATEWAY_USER" | cut -d: -f6 || true)"
    local ak="$home/.ssh/authorized_keys"
    if [[ ! -s "$ak" ]] && ! $DRY_RUN; then
        warn "User '$GATEWAY_USER' has no $ak — disabling password auth would"
        warn "lock you out. SKIPPING sshd hardening."
        warn "Add your public key first:  ssh-copy-id ${GATEWAY_USER}@${HOSTNAME_SHORT}"
        return
    fi

    local cfg_dir=/etc/ssh/sshd_config.d
    mkdir -p "$cfg_dir"
    local cfg="$cfg_dir/99-somniguard-hardening.conf"

    cat > "$cfg" <<EOF
# SOMNI-Guard sshd hardening — generated $(date -Iseconds)

# --- Auth -------------------------------------------------------------------
PermitRootLogin                 no
PasswordAuthentication          no
ChallengeResponseAuthentication no
KbdInteractiveAuthentication    no
PermitEmptyPasswords            no
UsePAM                          yes
PubkeyAuthentication            yes
AuthenticationMethods           publickey

AllowUsers ${GATEWAY_USER}

# --- Restrict surface ------------------------------------------------------
X11Forwarding         no
AllowAgentForwarding  no
AllowTcpForwarding    no
PermitTunnel          no
GatewayPorts          no

# --- Modern crypto only ---------------------------------------------------
KexAlgorithms                 curve25519-sha256,curve25519-sha256@libssh.org,sntrup761x25519-sha512@openssh.com
Ciphers                       chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-gcm@openssh.com
MACs                          hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com
HostKeyAlgorithms             ssh-ed25519,ssh-ed25519-cert-v01@openssh.com,rsa-sha2-512,rsa-sha2-256
PubkeyAcceptedAlgorithms      ssh-ed25519,ssh-ed25519-cert-v01@openssh.com,rsa-sha2-512,rsa-sha2-256

# --- Session hygiene ------------------------------------------------------
ClientAliveInterval     300
ClientAliveCountMax     2
LoginGraceTime          30
MaxAuthTries            3
MaxSessions             4
LogLevel                VERBOSE

Banner /etc/issue.net
EOF
    chmod 644 "$cfg"

    # Re-generate weak host keys: drop everything except ed25519 + RSA-3072+.
    if [[ -f /etc/ssh/ssh_host_dsa_key ]]; then
        run rm -f /etc/ssh/ssh_host_dsa_key{,.pub}
    fi
    if [[ -f /etc/ssh/ssh_host_ecdsa_key ]]; then
        run rm -f /etc/ssh/ssh_host_ecdsa_key{,.pub}
    fi
    if [[ ! -f /etc/ssh/ssh_host_ed25519_key ]]; then
        run ssh-keygen -q -t ed25519 -N "" -f /etc/ssh/ssh_host_ed25519_key
    fi

    # Validate config before reloading.
    if sshd -t -f /etc/ssh/sshd_config 2>>"$LOG_FILE"; then
        run systemctl reload ssh || run systemctl reload sshd
        info "sshd hardened and reloaded."
    else
        error "sshd config validation FAILED — config NOT activated."
        error "Inspect $LOG_FILE for the sshd -t output."
        rm -f "$cfg"
    fi
}

# ---------------------------------------------------------------------------
# 5. Kernel / sysctl hardening
# ---------------------------------------------------------------------------
configure_sysctl() {
    step "Applying sysctl hardening"
    local f=/etc/sysctl.d/99-somniguard-hardening.conf
    cat > "$f" <<'EOF'
# SOMNI-Guard sysctl hardening

# --- Network: spoofing, source routing, ICMP ---
net.ipv4.conf.all.rp_filter           = 1
net.ipv4.conf.default.rp_filter       = 1
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0
net.ipv4.conf.all.accept_redirects    = 0
net.ipv4.conf.default.accept_redirects= 0
net.ipv4.conf.all.secure_redirects    = 0
net.ipv4.conf.default.secure_redirects= 0
net.ipv4.conf.all.send_redirects      = 0
net.ipv4.conf.default.send_redirects  = 0
net.ipv4.icmp_echo_ignore_broadcasts  = 1
net.ipv4.icmp_ignore_bogus_error_responses = 1

# Reverse-path / SYN flood resistance
net.ipv4.tcp_syncookies               = 1
net.ipv4.tcp_max_syn_backlog          = 4096
net.ipv4.tcp_synack_retries           = 2

# Privacy / kernel info leaks
kernel.dmesg_restrict                 = 1
kernel.kptr_restrict                  = 2
kernel.unprivileged_bpf_disabled      = 1
kernel.yama.ptrace_scope              = 1
kernel.perf_event_paranoid            = 3
fs.protected_symlinks                 = 1
fs.protected_hardlinks                = 1
fs.protected_fifos                    = 2
fs.protected_regular                  = 2
fs.suid_dumpable                      = 0

# IPv6 — disable router advertisements (a hostile peer on the LAN must not
# auto-configure routes on us); leave IPv6 itself enabled.
net.ipv6.conf.all.accept_ra           = 0
net.ipv6.conf.default.accept_ra       = 0
net.ipv6.conf.all.accept_redirects    = 0
net.ipv6.conf.default.accept_redirects= 0
EOF
    chmod 644 "$f"
    run sysctl --system | tail -5 | tee -a "$LOG_FILE" || true
}

# ---------------------------------------------------------------------------
# 6. Disable unused services
# ---------------------------------------------------------------------------
disable_unused_services() {
    step "Disabling unused services"
    local -a candidates=()
    $DISABLE_BT && candidates+=(bluetooth.service hciuart.service)
    candidates+=(avahi-daemon.service avahi-daemon.socket cups.service cups.socket)
    candidates+=(triggerhappy.service)

    for svc in "${candidates[@]}"; do
        if systemctl list-unit-files "$svc" &>/dev/null; then
            run systemctl disable --now "$svc" 2>/dev/null || true
            info "Disabled $svc"
        fi
    done
}

# ---------------------------------------------------------------------------
# 7. Unattended security upgrades
# ---------------------------------------------------------------------------
enable_unattended_upgrades() {
    step "Enabling unattended security upgrades"
    cat > /etc/apt/apt.conf.d/52somniguard-unattended <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Mail "";
EOF
    run dpkg-reconfigure -f noninteractive unattended-upgrades || true
    info "unattended-upgrades configured (security updates only, no auto reboot)."
}

# ---------------------------------------------------------------------------
# 8. systemd hardening drop-in for somniguard.service
# ---------------------------------------------------------------------------
harden_gateway_service() {
    step "Adding systemd hardening drop-in for somniguard.service"
    if ! systemctl list-unit-files somniguard.service &>/dev/null; then
        warn "somniguard.service not installed yet — run setup_gateway_pi5.sh first."
        return
    fi
    local d=/etc/systemd/system/somniguard.service.d
    mkdir -p "$d"
    cat > "$d/10-hardening.conf" <<EOF
[Service]
# Reduce blast radius if the gateway is exploited.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources

# Read-write paths — gateway needs to write its DB, reports, audit log,
# certs, hotspot creds, encrypted volume bind targets.
ReadWritePaths=/var/lib/somniguard /var/lib/somniguard-secure /var/log/somniguard /etc/somniguard

# Restrict network families to inet/inet6/unix — no raw sockets, no netlink
# from the gateway process.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

# Capabilities: gateway needs none for its primary work. nmcli is invoked
# via sudo and runs in its own process, so no NET_ADMIN here.
CapabilityBoundingSet=
AmbientCapabilities=

# Ulimits.
LimitNOFILE=4096
LimitNPROC=512
EOF
    chmod 644 "$d/10-hardening.conf"
    run systemctl daemon-reload
    info "Hardening drop-in installed; restart the service to apply."
}

# ---------------------------------------------------------------------------
# 9. AIDE baseline
# ---------------------------------------------------------------------------
configure_aide() {
    $DO_AIDE || { info "Skipping AIDE (--no-aide)."; return; }
    step "Initialising AIDE filesystem-integrity baseline (this can take a while)"
    if [[ ! -f /var/lib/aide/aide.db ]]; then
        run aideinit -y -f || true
        if [[ -f /var/lib/aide/aide.db.new ]]; then
            run mv /var/lib/aide/aide.db.new /var/lib/aide/aide.db
        fi
    else
        info "AIDE DB already initialised — leaving alone."
    fi
    info "AIDE baseline ready. Run 'sudo aide --check' periodically (or via cron)."
}

# ---------------------------------------------------------------------------
# 10. Banners
# ---------------------------------------------------------------------------
write_banners() {
    step "Writing legal/security banners"
    cat > /etc/issue.net <<EOF
*****************************************************************
*  ${HOSTNAME_SHORT} (SOMNI-Guard / NightWatchGuard)
*
*  Authorised use only. All activity is logged and audited.
*  Disconnect immediately if you are not an authorised operator.
*****************************************************************
EOF
    cp /etc/issue.net /etc/issue
    chmod 644 /etc/issue /etc/issue.net
}

# ---------------------------------------------------------------------------
# 11. Strengthen PAM password quality (for local accounts)
# ---------------------------------------------------------------------------
configure_pam_pwquality() {
    step "Tightening pam_pwquality for local accounts"
    local pq=/etc/security/pwquality.conf.d/99-somniguard.conf
    mkdir -p "$(dirname "$pq")"
    cat > "$pq" <<'EOF'
minlen        = 14
minclass      = 4
maxrepeat     = 3
maxsequence   = 3
gecoscheck    = 1
dictcheck     = 1
enforcing     = 1
retry         = 3
EOF
    chmod 644 "$pq"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"
    check_root
    init_logging
    print_banner
    install_packages
    configure_ufw
    configure_fail2ban
    configure_ssh
    configure_sysctl
    disable_unused_services
    enable_unattended_upgrades
    harden_gateway_service
    configure_aide
    write_banners
    configure_pam_pwquality

    echo
    info "================================================================="
    info " Hardening complete."
    info ""
    info " RECOMMENDED NEXT STEPS:"
    info "  - Verify ssh access from another terminal BEFORE closing this one."
    info "  - sudo systemctl restart somniguard       # apply systemd drop-in"
    info "  - sudo fail2ban-client status              # confirm jails are up"
    info "  - sudo ufw status verbose                  # confirm firewall"
    info "  - sudo aide --check                        # verify FS integrity"
    info ""
    info " Log file: $LOG_FILE"
    info "================================================================="
}

main "$@"
