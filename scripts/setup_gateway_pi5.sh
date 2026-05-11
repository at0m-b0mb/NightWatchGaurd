#!/usr/bin/env bash
# setup_gateway_pi5.sh — One-shot Pi 5 gateway setup for SOMNI-Guard.
#
# Run once as root (sudo bash scripts/setup_gateway_pi5.sh).
# Safe to re-run — all steps are idempotent.
#
# What this script does:
#   1. Installs system dependencies (Python, pip, venv, build tools)
#   2. Detects the user who called sudo
#   3. Creates Python venv and installs requirements.txt
#   4. Seeds database with admin user and demo patient
#   5. Installs systemd service (starts on boot)
#   6. Security hardening (SSH, firewall, Bluetooth disabled)
#   7. Generates TLS certificates
#
# After running: sudo systemctl status somniguard

set -euo pipefail

# ── Helpers ──────────────────────────────────────────────────────────────────
info()  { echo "[SETUP] $*"; }
ok()    { echo "[SETUP] ✓ $*"; }
warn()  { echo "[SETUP] ⚠ $*"; }
die()   { echo "[SETUP] ✗ $*" >&2; exit 1; }

# ── Must run as root ──────────────────────────────────────────────────────────
[[ "$EUID" -eq 0 ]] || die "Run as root: sudo bash scripts/setup_gateway_pi5.sh"

# ── Detect the calling user (the one who typed sudo) ─────────────────────────
GATEWAY_USER="${SUDO_USER:-pi}"
info "Gateway will run as user: $GATEWAY_USER"

# Resolve the user's home directory
GATEWAY_HOME=$(getent passwd "$GATEWAY_USER" | cut -d: -f6)
[[ -d "$GATEWAY_HOME" ]] || die "Home directory not found for user $GATEWAY_USER"

# ── Locate the project root ───────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
GATEWAY_DIR="$PROJECT_ROOT/somniguard_gateway"
VENV_PYTHON="$GATEWAY_DIR/.venv/bin/python"

[[ -f "$GATEWAY_DIR/run.py" ]] || \
    die "run.py not found in $GATEWAY_DIR — run this script from the project root."

echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "  SOMNI-Guard Gateway Setup for Raspberry Pi 5"
echo "════════════════════════════════════════════════════════════════════"
echo ""

# ── 1. Update apt cache and install system dependencies ──────────────────────
info "Updating system package cache…"
apt-get update -qq 2>/dev/null || true

info "Installing system dependencies (Python, build tools, fonts, DHCP)…"

# Core Python and build dependencies
CRITICAL_DEPS="python3-dev python3-pip python3-venv"

# Cryptography and SSL/TLS
CRYPTO_DEPS="libssl-dev libffi-dev build-essential"

# PDF and fonts
FONT_DEPS="fonts-dejavu-core"

# Network/Hotspot
NETWORK_DEPS="dnsmasq-base"

# mDNS (so users can access https://somniguard.local:5443/)
MDNS_DEPS="avahi-daemon avahi-utils libnss-mdns"

ALL_DEPS="$CRITICAL_DEPS $CRYPTO_DEPS $FONT_DEPS $NETWORK_DEPS $MDNS_DEPS"

for pkg in $ALL_DEPS; do
    if dpkg -l 2>/dev/null | grep -q "^ii  $pkg "; then
        # Already installed, skip
        true
    else
        info "  Installing: $pkg"
        if ! apt-get install -y -qq "$pkg" 2>/dev/null; then
            warn "  Failed to install $pkg (may already exist or not available)"
        fi
    fi
done
ok "System dependencies ready."
echo ""

# ── 2. Python venv and requirements.txt ────────────────────────────────────────
info "Setting up Python virtual environment…"

if [[ ! -d "$GATEWAY_DIR/.venv" ]]; then
    info "  Creating venv at $GATEWAY_DIR/.venv"
    sudo -u "$GATEWAY_USER" python3 -m venv "$GATEWAY_DIR/.venv" || \
        die "Failed to create venv"
    ok "Virtual environment created."
else
    ok "Virtual environment already exists."
fi

# Upgrade pip first
info "Upgrading pip, setuptools, wheel…"
sudo -u "$GATEWAY_USER" "$GATEWAY_DIR/.venv/bin/pip" install -q --upgrade pip setuptools wheel 2>/dev/null || \
    warn "pip upgrade had issues (may be offline)"

# Install requirements from requirements.txt
if [[ ! -f "$GATEWAY_DIR/requirements.txt" ]]; then
    die "requirements.txt not found at $GATEWAY_DIR/requirements.txt"
fi

info "Installing Python requirements from $GATEWAY_DIR/requirements.txt"
info "  This may take 2-3 minutes (building cryptography, gunicorn, etc.)…"

if ! sudo -u "$GATEWAY_USER" "$GATEWAY_DIR/.venv/bin/pip" install -q \
    -r "$GATEWAY_DIR/requirements.txt"; then
    die "Failed to install requirements. Check your internet connection and try again."
fi

ok "Python requirements installed successfully."
ok "Packages installed:"
sudo -u "$GATEWAY_USER" "$GATEWAY_DIR/.venv/bin/pip" list | grep -E "Flask|gunicorn|cryptography|bcrypt" | sed 's/^/    /'
echo ""

# ── 3. Verify critical packages ────────────────────────────────────────────────
info "Verifying critical dependencies…"

for pkg in flask gunicorn cryptography bcrypt requests; do
    if sudo -u "$GATEWAY_USER" "$GATEWAY_DIR/.venv/bin/python" -c "import $pkg" 2>/dev/null; then
        ok "  $pkg ✓"
    else
        warn "  $pkg NOT FOUND - installation may have failed"
    fi
done
echo ""

# ── 4. Sudoers rule for nmcli ─────────────────────────────────────────────────
SUDOERS_FILE="/etc/sudoers.d/somniguard-nmcli"
info "Installing sudoers rule for hotspot management…"

cat > "$SUDOERS_FILE" <<EOF
# SOMNI-Guard: allow $GATEWAY_USER to manage NetworkManager
$GATEWAY_USER ALL=(ALL) NOPASSWD: /usr/bin/nmcli
EOF
chmod 440 "$SUDOERS_FILE"

if visudo -c -f "$SUDOERS_FILE" &>/dev/null; then
    ok "Sudoers rule valid and installed."
else
    rm -f "$SUDOERS_FILE"
    die "Sudoers rule failed validation"
fi
echo ""

# ── 5. Environment file ───────────────────────────────────────────────────────
ENV_DIR="/etc/somniguard"
ENV_FILE="$ENV_DIR/env"
info "Setting up environment file at $ENV_FILE"

mkdir -p "$ENV_DIR"
chmod 750 "$ENV_DIR"
chown root:"$GATEWAY_USER" "$ENV_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
    info "  Generating SECRET_KEY and HMAC_KEY…"
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    HMAC_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    
    cat > "$ENV_FILE" <<EOF
# SOMNI-Guard gateway environment — auto-generated by setup_gateway_pi5.sh
# Edit to customize; restart service after changes: sudo systemctl restart somniguard

SOMNI_SECRET_KEY=${SECRET_KEY}
SOMNI_HMAC_KEY=${HMAC_KEY}
SOMNI_HTTPS=true
SOMNI_TAILSCALE_ONLY=false
SOMNI_DB_PATH=${GATEWAY_DIR}/somniguard.db
SOMNI_REPORT_DIR=${GATEWAY_DIR}/reports
EOF
    chmod 640 "$ENV_FILE"
    chown root:"$GATEWAY_USER" "$ENV_FILE"
    ok "Environment file created."
    echo ""
    echo "  ⚠ IMPORTANT: The HMAC key will be auto-synced to Pico config"
    echo "    SOMNI_HMAC_KEY = $HMAC_KEY"
    echo ""

    # Auto-sync HMAC key into Pico config if embed_pico_config.py exists
    EMBED_CONFIG="$SCRIPT_DIR/embed_pico_config.py"
    if [[ -f "$EMBED_CONFIG" ]]; then
        info "  Auto-embedding HMAC key into Pico config…"
        sudo -u "$GATEWAY_USER" "$VENV_PYTHON" "$EMBED_CONFIG" \
            --hmac-key "$HMAC_KEY" 2>/dev/null || \
            warn "  Could not auto-embed HMAC key — update GATEWAY_HMAC_KEY in Pico config manually."
    else
        warn "  embed_pico_config.py not found — update GATEWAY_HMAC_KEY in Pico config manually."
    fi
else
    ok "Environment file already exists (not overwritten)."
fi
echo ""

# ── 6. Hotspot DHCP prerequisites ──────────────────────────────────────────────
info "Checking hotspot DHCP prerequisites…"

HOTSPOT_CON="${SOMNI_HOTSPOT_CON_NAME:-SomniGuard_Hotspot}"
if nmcli -t con show "$HOTSPOT_CON" &>/dev/null; then
    info "  Removing stale hotspot profile…"
    nmcli con down   "$HOTSPOT_CON" 2>/dev/null || true
    nmcli con delete "$HOTSPOT_CON" 2>/dev/null || true
    ok "Stale profile removed (fresh one will be created on start)."
fi

# Disable conflicting standalone dnsmasq
if systemctl is-active --quiet dnsmasq.service 2>/dev/null; then
    info "  Disabling standalone dnsmasq.service (conflicts with NetworkManager)…"
    systemctl disable --now dnsmasq.service 2>/dev/null || true
    ok "Standalone dnsmasq disabled."
fi
echo ""

# ── 7. Seed database with admin user and demo patient ────────────────────────
info "Seeding database with admin user and demo patient…"
info "  This will create admin credentials — SAVE THEM!"
echo ""

sudo -u "$GATEWAY_USER" \
    SOMNI_DB_PATH="$GATEWAY_DIR/somniguard.db" \
    "$VENV_PYTHON" "$SCRIPT_DIR/seed_db.py"

echo ""
ok "Database seeding complete."
echo ""

# ── 8. TLS certificates — force-regenerate to ensure consistency ──────────────
info "Regenerating TLS certificates (force-regenerate for clean PKI)…"
info "  All certs (CA, server, client) will be regenerated and re-signed."

# Force-regenerate ALL certs to ensure the CA → server → client chain
# is consistent. This avoids subtle mismatch bugs where the Pico's
# embedded CA cert does not match the CA that signed the server cert.
sudo -u "$GATEWAY_USER" "$VENV_PYTHON" "$SCRIPT_DIR/setup_gateway_certs.py" \
    --cert-dir "$GATEWAY_DIR/certs" \
    --force-regenerate || \
    warn "TLS certificate setup had issues"

ok "TLS certificates generated."
ok "CA + server + Pico client certs written under $GATEWAY_DIR/certs/"
ok "Pico trust anchor (CA) is embedded into config.py by embed_pico_cert.py — no filesystem cert upload needed."

# ── 8a. Auto-embed certificates into Pico config ────────────────────────────
info "Embedding certificates into Pico firmware config…"
EMBED_SCRIPT="$SCRIPT_DIR/embed_pico_cert.py"
if [[ -f "$EMBED_SCRIPT" ]]; then
    sudo -u "$GATEWAY_USER" "$VENV_PYTHON" "$EMBED_SCRIPT" \
        --ca-cert "$GATEWAY_DIR/certs/ca.crt" \
        --client-cert "$GATEWAY_DIR/certs/pico_client.crt" \
        --client-key "$GATEWAY_DIR/certs/pico_client.key" \
        --config "$PROJECT_ROOT/somniguard_pico/config.py" || \
        warn "Certificate embedding had issues — run embed_pico_cert.py manually."
    ok "Certificates embedded into somniguard_pico/config.py."
else
    warn "embed_pico_cert.py not found at $EMBED_SCRIPT — embed certs manually."
fi
echo ""

# ── 8b. dnsmasq DNS override — somniguard.local for ALL hotspot clients ────
# NetworkManager's shared-mode dnsmasq picks up custom records from
# /etc/NetworkManager/dnsmasq-shared.d/.  Writing an address record here
# means Windows, Android, and any device that doesn't support mDNS can
# still resolve somniguard.local using the hotspot's DNS server.
info "Writing dnsmasq DNS override: somniguard.local → 10.42.0.1"
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/somniguard.conf <<'EOF'
# SOMNI-Guard: resolve somniguard.local → gateway for ALL hotspot clients.
# This makes the .local name work on Windows/Android (no mDNS needed).
address=/somniguard.local/10.42.0.1
EOF
chmod 644 /etc/NetworkManager/dnsmasq-shared.d/somniguard.conf
ok "dnsmasq config written — somniguard.local will resolve on all hotspot clients."
echo ""

# ── 8c. Secure mDNS (Avahi) for somniguard.local ──────────────────────────
info "Configuring SECURE mDNS for https://somniguard.local:5443/…"

# Set hostname to 'somniguard' so mDNS advertises somniguard.local
hostnamectl set-hostname somniguard 2>/dev/null || true

# ── HARDENED Avahi configuration ───────────────────────────────────────────
# Security restrictions applied:
#   - Bind ONLY to the hotspot interface (no leakage to LAN/WAN)
#   - Disable IPv6 advertising (smaller attack surface)
#   - Disable wide-area DNS (no public mDNS responses)
#   - Disable publishing of system info (CPU, OS version, etc.)
#   - Disable publishing of workstation/domain info
#   - Disable reflection across interfaces
#   - Reject queries from non-local clients
HOTSPOT_IFACE="${SOMNI_HOTSPOT_IFACE:-wlan0}"

# Backup existing config
[[ -f /etc/avahi/avahi-daemon.conf ]] && \
    cp -n /etc/avahi/avahi-daemon.conf /etc/avahi/avahi-daemon.conf.somniguard.bak

cat > /etc/avahi/avahi-daemon.conf <<EOF
# SOMNI-Guard hardened Avahi config — generated by setup_gateway_pi5.sh
# Restricts mDNS to local hotspot only; no WAN/LAN leakage.

[server]
host-name=somniguard
domain-name=local
# Bind only to the hotspot — Picos and authorized devices on SomniGuard_Net
allow-interfaces=${HOTSPOT_IFACE}
# Reject anything not on these interfaces
deny-interfaces=eth0
# IPv4 only — fewer attack vectors
use-ipv4=yes
use-ipv6=no
# Don't reflect mDNS across interfaces (prevents cross-network leaks)
enable-reflector=no
# Reject queries that don't originate from a local link
ratelimit-interval-usec=1000000
ratelimit-burst=1000
# Enable DBus for service registration
enable-dbus=yes

[wide-area]
# CRITICAL: disable wide-area DNS — keeps mDNS strictly on local link
enable-wide-area=no

[publish]
# Don't broadcast system fingerprinting info
publish-addresses=yes
publish-hinfo=no
publish-workstation=no
publish-domain=no
publish-resolv-conf-dns-servers=no
publish-aaaa-on-ipv4=no
publish-a-on-ipv6=no
# Don't publish from /etc/hosts entries
disable-publishing=no
disable-user-service-publishing=no

[reflector]
# Disabled — no cross-interface reflection
reflect-ipv=no

[rlimits]
rlimit-as=
rlimit-core=0
rlimit-data=8388608
rlimit-fsize=0
rlimit-nofile=300
rlimit-stack=8388608
rlimit-nproc=3
EOF

# Create the service file (advertises HTTPS dashboard)
AVAHI_SERVICE=/etc/avahi/services/somniguard.service
mkdir -p /etc/avahi/services
cat > "$AVAHI_SERVICE" <<'EOF'
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>SOMNI-Guard Dashboard</name>
  <service protocol="ipv4">
    <type>_https._tcp</type>
    <port>5443</port>
    <txt-record>path=/</txt-record>
  </service>
</service-group>
EOF

# Remove any default Avahi service files (CUPS, SFTP, SSH advertisement)
# These would otherwise leak service info onto the hotspot
for default_svc in /etc/avahi/services/ssh.service \
                   /etc/avahi/services/sftp-ssh.service \
                   /etc/avahi/services/udisks.service; do
    [[ -f "$default_svc" ]] && rm -f "$default_svc"
done

systemctl enable avahi-daemon.service 2>/dev/null || true
systemctl restart avahi-daemon.service 2>/dev/null || true

ok "Hardened mDNS configured:"
ok "  - Bound to ${HOTSPOT_IFACE} only"
ok "  - Wide-area DNS disabled"
ok "  - IPv6 disabled"
ok "  - System info publishing disabled"
ok "  - Dashboard at https://somniguard.local:5443/"
echo ""

# ── 9. systemd service ─────────────────────────────────────────────────────────
SERVICE_FILE="/etc/systemd/system/somniguard.service"
info "Installing systemd service…"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=SOMNI-Guard Gateway (Flask + Wi-Fi hotspot)
After=network-online.target NetworkManager.service
Wants=network-online.target

[Service]
Type=simple
User=${GATEWAY_USER}
Group=${GATEWAY_USER}
WorkingDirectory=${GATEWAY_DIR}
EnvironmentFile=${ENV_FILE}
Environment="PYTHONUNBUFFERED=1"

ExecStart=${VENV_PYTHON} ${GATEWAY_DIR}/run.py

TimeoutStartSec=30
Restart=on-failure
RestartSec=10
StartLimitInterval=300
StartLimitBurst=5

StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${GATEWAY_DIR} ${ENV_DIR} /var/lib/somniguard
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=
UMask=0027

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable somniguard.service
ok "Service installed and enabled (starts on boot)."
echo ""

# ── 10. Disable attack surface ─────────────────────────────────────────────────
info "Hardening security: disabling Bluetooth and unused services…"

# Note: avahi-daemon is INTENTIONALLY kept enabled for mDNS (somniguard.local)
for unit in bluetooth.service hciuart.service cups.service; do
    if systemctl list-unit-files "$unit" &>/dev/null; then
        systemctl disable --now "$unit" 2>/dev/null || true
    fi
done

BT_BLACKLIST=/etc/modprobe.d/somniguard-no-bluetooth.conf
if [[ ! -f "$BT_BLACKLIST" ]]; then
    cat > "$BT_BLACKLIST" <<'EOF'
blacklist btusb
blacklist btintel
blacklist btbcm
blacklist bluetooth
EOF
    ok "Bluetooth disabled and blacklisted."
fi
echo ""

# ── 11. SSH hardening ──────────────────────────────────────────────────────────
SSHD_DROPIN=/etc/ssh/sshd_config.d/somniguard-hardening.conf
if [[ -d /etc/ssh/sshd_config.d ]] && [[ ! -f "$SSHD_DROPIN" ]]; then
    info "Hardening SSH (key-based auth only)…"
    cat > "$SSHD_DROPIN" <<'EOF'
PermitRootLogin no
PasswordAuthentication no
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
UsePAM yes
X11Forwarding no
ClientAliveInterval 300
ClientAliveCountMax 2
LoginGraceTime 30
MaxAuthTries 3
AllowAgentForwarding no
AllowTcpForwarding no
PermitUserEnvironment no
EOF
    chmod 644 "$SSHD_DROPIN"
    ok "SSH hardened (verify authorized_keys before rebooting)."
    warn "Check: ls ${GATEWAY_HOME}/.ssh/authorized_keys"
fi
echo ""

# ── 12. Firewall (UFW) ─────────────────────────────────────────────────────────
HOTSPOT_IFACE="${SOMNI_HOTSPOT_IFACE:-wlan0}"
if command -v ufw &>/dev/null; then
    info "Configuring UFW firewall…"
    ufw --force reset >/dev/null 2>&1 || true
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow 22/tcp        comment 'SSH'
    ufw allow 5443/tcp      comment 'SOMNI-Guard HTTPS'
    # Note: port 5000 (plain HTTP) is NOT opened — all traffic is HTTPS only.
    ufw allow in on "$HOTSPOT_IFACE" comment 'SomniGuard hotspot'
    ufw allow 67/udp        comment 'DHCP'
    ufw allow 53            comment 'DNS'
    ufw allow 5353/udp      comment 'mDNS (somniguard.local)'
    ufw --force enable >/dev/null 2>&1 || true
    ok "Firewall configured (22, 5443 HTTPS only — no plain HTTP)."
else
    warn "ufw not available (install with: apt install ufw)"
fi
echo ""

# ── 13. Start the service ──────────────────────────────────────────────────────
info "Starting somniguard service…"
systemctl restart NetworkManager 2>/dev/null || true
sleep 2
systemctl restart somniguard.service 2>/dev/null || \
    systemctl start  somniguard.service 2>/dev/null || true
sleep 5

# Health check
if systemctl is-active --quiet somniguard.service; then
    ok "somniguard.service is RUNNING ✓"
else
    warn "somniguard.service is NOT running"
    warn "View logs: journalctl -u somniguard -n 20"
fi
echo ""

# ── 14. Certificate file output ────────────────────────────────────────────────
CERT_PATH="$GATEWAY_DIR/certs/server.crt"
CA_CERT_PATH="$GATEWAY_DIR/certs/ca.crt"

if [[ -f "$CA_CERT_PATH" ]]; then
    echo "════════════════════════════════════════════════════════════════════"
    echo "  📜 PICO TRUST ANCHOR (Root CA) READY"
    echo "════════════════════════════════════════════════════════════════════"
    echo ""
    echo "  CA cert location:"
    echo "    $CA_CERT_PATH"
    echo ""
    echo "  SHA-256 fingerprint:"
    sha256sum "$CA_CERT_PATH" 2>/dev/null | awk '{print "    " $1}' || \
        openssl dgst -sha256 "$CA_CERT_PATH" 2>/dev/null | awk '{print "    " $NF}'
    echo ""
    echo "  The CA + Pico client cert/key are embedded in config.py by"
    echo "  embed_pico_cert.py — no filesystem cert upload to the Pico."
    echo ""
    echo "════════════════════════════════════════════════════════════════════"
    echo ""
fi

# ── 15. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════════"
echo "  ✓ SOMNI-Guard Gateway Setup Complete"
echo "════════════════════════════════════════════════════════════════════"
echo ""
echo "  Service:      somniguard.service"
echo "  Status:       sudo systemctl status somniguard"
echo "  Logs:         journalctl -u somniguard -f"
echo ""
echo "  📡 DASHBOARD (HTTPS only — no plain HTTP):"
echo "    https://10.42.0.1:5443/          ← direct IP (always works)"
echo "    https://somniguard.local:5443/   ← hostname (macOS + hotspot clients)"
echo ""
echo "  ⚠  FIRST-TIME BROWSER SETUP (one-off — removes the cert warning):"
echo "    1. Connect your device to: SomniGuard_Net"
echo "    2. Visit https://10.42.0.1:5443/ — click Advanced → Proceed"
echo "    3. Download the CA cert: https://10.42.0.1:5443/ca.crt"
echo "       macOS:   Keychain Access → System → import → Always Trust"
echo "       Windows: double-click → Install Certificate → Trusted Root CAs"
echo "       Firefox: Settings → Privacy → Certificates → Import → Trust"
echo "    4. Revisit — no more warning."
echo ""
echo "  📋 NEXT STEPS:"
echo "  1. Browser: install CA cert (see above ↑)"
echo "  2. Login to dashboard with admin password (above ↑)"
echo ""
echo "  3. Embed PKI into Pico config (CA + client cert/key, no filesystem upload):"
echo "       python3 scripts/embed_pico_cert.py"
echo ""
echo "  4. Configure Pico WiFi & HMAC:"
echo "       python3 scripts/embed_pico_config.py \\"
echo "           --ssid SomniGuard_Net --password <wifi-pw> \\"
echo "           --gateway-host 10.42.0.1 --hmac-key <hmac-key>"
echo ""
echo "  5. Encrypt & upload firmware:"
echo "       python3 scripts/encrypt_pico_files.py --uid <pico-uid>"
echo "       mpremote connect /dev/cu.usbmodem* fs cp -r encrypted_deploy/. :"
echo ""
echo "  6. Restart Pico:"
echo "       mpremote connect /dev/cu.usbmodem* reset"
echo ""
echo "════════════════════════════════════════════════════════════════════"
echo ""
