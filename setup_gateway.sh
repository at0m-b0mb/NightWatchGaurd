#!/usr/bin/env bash
################################################################################
# SOMNI-Guard Gateway Setup Script
#
# Installs system + Python dependencies, builds the PKI, creates the dedicated
# low-privileged 'somniguard' system user, installs the systemd unit, and
# enables + starts the gateway so it runs automatically (and on every boot)
# without any further manual `python run.py` step.
#
# Run ONCE on the Raspberry Pi 5:
#
#     sudo bash setup_gateway.sh
#
# Educational prototype — not a clinically approved device.
################################################################################

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="${PROJECT_DIR}/somniguard_gateway"
SCRIPTS_DIR="${PROJECT_DIR}/scripts"
VENV_DIR="${PROJECT_DIR}/.venv"

# Persistent state lives outside the project tree (survives a `git pull`).
ETC_DIR="/etc/somniguard"
DATA_DIR="/var/lib/somniguard"        # SQLite DB
LOG_DIR="/var/log/somniguard"         # JSON audit log
REPORT_DIR="${DATA_DIR}/reports"      # PDF reports
CERT_DIR="${ETC_DIR}/certs"           # CA + server + Pico client cert/key
ENV_FILE="${ETC_DIR}/env"

# Service identity
SVC_USER="somniguard"
SVC_GROUP="somniguard"
SVC_NAME="somniguard-gateway"
SVC_UNIT="/etc/systemd/system/${SVC_NAME}.service"

echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE} SOMNI-Guard Gateway Setup${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo "  Project dir : ${PROJECT_DIR}"
echo "  Service user: ${SVC_USER} (low-privileged, no shell)"
echo "  State dir   : ${DATA_DIR}"
echo "  Cert dir    : ${CERT_DIR}"
echo "  Env file    : ${ENV_FILE}"
echo ""

if [[ $EUID -ne 0 ]]; then
   echo -e "${RED}ERROR: This script must run as root.${NC}"
   echo "  sudo bash setup_gateway.sh"
   exit 1
fi

# ============================================================================
# 1. APT packages
# ============================================================================
echo -e "${YELLOW}[1/9] Installing system packages…${NC}"
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv python3-dev \
    libssl-dev libffi-dev build-essential \
    ufw git curl openssl sqlite3 acl >/dev/null
echo -e "${GREEN}  ✓ APT packages ready.${NC}"

# ============================================================================
# 2. Service user (low-privileged, no shell, no home dir)
# ============================================================================
echo -e "${YELLOW}[2/9] Creating service user '${SVC_USER}'…${NC}"
if ! id -u "${SVC_USER}" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
            --comment "SOMNI-Guard gateway service" "${SVC_USER}"
    echo -e "${GREEN}  ✓ Created system user '${SVC_USER}'.${NC}"
else
    echo "  ✓ System user '${SVC_USER}' already exists."
fi

# NetworkManager's default polkit rules grant the `netdev` group both
# `network-control` and `system-connection-modify`.  Putting the service
# user in netdev lets it run plain `nmcli` to inspect/bring up the
# hotspot without sudo (which would be blocked by NoNewPrivileges /
# RestrictSUIDSGID anyway).  Creating the group if it doesn't exist on
# minimal images.
if ! getent group netdev >/dev/null 2>&1; then
    groupadd --system netdev
fi
usermod -aG netdev "${SVC_USER}"
echo "  ✓ Added '${SVC_USER}' to the 'netdev' group (NetworkManager polkit)."

# ============================================================================
# 3. Persistent directories with strict ownership
# ============================================================================
echo -e "${YELLOW}[3/9] Creating persistent state directories…${NC}"
install -d -m 750 -o "${SVC_USER}" -g "${SVC_GROUP}" "${ETC_DIR}"
install -d -m 700 -o "${SVC_USER}" -g "${SVC_GROUP}" "${CERT_DIR}"
install -d -m 750 -o "${SVC_USER}" -g "${SVC_GROUP}" "${DATA_DIR}"
install -d -m 750 -o "${SVC_USER}" -g "${SVC_GROUP}" "${REPORT_DIR}"
install -d -m 750 -o "${SVC_USER}" -g "${SVC_GROUP}" "${LOG_DIR}"

# Project tree must be readable by the service user. We grant read+execute
# (no write) so the user can never modify the source it's running.
chgrp -R "${SVC_GROUP}" "${PROJECT_DIR}" 2>/dev/null || true
chmod -R g+rX "${PROJECT_DIR}" 2>/dev/null || true
echo "  ✓ Directories created and chowned to ${SVC_USER}:${SVC_GROUP}."

# ============================================================================
# 4. Python virtual environment (owned by service user)
# ============================================================================
echo -e "${YELLOW}[4/9] Setting up Python virtual environment…${NC}"
if [[ ! -d "${VENV_DIR}" ]]; then
    python3 -m venv "${VENV_DIR}"
    echo "  ✓ Virtualenv created at ${VENV_DIR}."
else
    echo "  ✓ Virtualenv already exists at ${VENV_DIR}."
fi
"${VENV_DIR}/bin/pip" install --upgrade -q pip setuptools wheel
"${VENV_DIR}/bin/pip" install --upgrade -q -r "${GATEWAY_DIR}/requirements.txt"
chown -R "${SVC_USER}:${SVC_GROUP}" "${VENV_DIR}"
echo -e "${GREEN}  ✓ Python deps installed.${NC}"

# ============================================================================
# 5. PKI bootstrap (CA + server cert + Pico client cert)
# ============================================================================
echo -e "${YELLOW}[5/9] Building PKI in ${CERT_DIR}…${NC}"
sudo -u "${SVC_USER}" "${VENV_DIR}/bin/python" \
    "${SCRIPTS_DIR}/setup_gateway_certs.py" \
    --cert-dir "${CERT_DIR}" >/dev/null
echo -e "${GREEN}  ✓ Root CA + server cert + Pico client cert generated.${NC}"
echo "  ✓ Run 'python3 scripts/embed_pico_cert.py --ca-cert ${CERT_DIR}/ca.crt"
echo "        --client-cert ${CERT_DIR}/pico_client.crt --client-key ${CERT_DIR}/pico_client.key'"
echo "    to push CA + client cert into the Pico's config.py."

# ============================================================================
# 6. Environment file (systemd-friendly: KEY=value, no `export`)
# ============================================================================
echo -e "${YELLOW}[6/9] Writing ${ENV_FILE}…${NC}"
if [[ -f "${ENV_FILE}" ]]; then
    cp -a "${ENV_FILE}" "${ENV_FILE}.bak.$(date +%s)"
    echo "  ✓ Backed up existing env file."
    SECRET_KEY=$(grep -E '^SOMNI_SECRET_KEY=' "${ENV_FILE}" | cut -d= -f2- | tr -d '"')
    HMAC_KEY=$(  grep -E '^SOMNI_HMAC_KEY='   "${ENV_FILE}" | cut -d= -f2- | tr -d '"')
fi
SECRET_KEY="${SECRET_KEY:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
HMAC_KEY="${HMAC_KEY:-$(python3   -c 'import secrets; print(secrets.token_hex(32))')}"

cat > "${ENV_FILE}" <<EOF
# SOMNI-Guard gateway environment — consumed by systemd EnvironmentFile.
# Format: KEY=VALUE  (NO leading 'export' — systemd does not parse shell syntax)
# Generated by setup_gateway.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ").

SOMNI_SECRET_KEY=${SECRET_KEY}
SOMNI_HMAC_KEY=${HMAC_KEY}

SOMNI_DB_PATH=${DATA_DIR}/somniguard.db
SOMNI_REPORT_DIR=${REPORT_DIR}
SOMNI_AUDIT_LOG_DIR=${LOG_DIR}

SOMNI_HTTPS=true
SOMNI_HOST=0.0.0.0
SOMNI_PORT=5443

SOMNI_HOTSPOT=true
SOMNI_WORKERS=2
SOMNI_THREADS=4

SOMNI_DEBUG=false
SOMNI_TAILSCALE_ONLY=false
SOMNI_PICO_CIDRS=10.42.0.0/24,127.0.0.1/32
EOF
chmod 640 "${ENV_FILE}"
chown root:"${SVC_GROUP}" "${ENV_FILE}"
echo -e "${GREEN}  ✓ Wrote ${ENV_FILE} (mode 640, root:${SVC_GROUP}).${NC}"
echo "    SOMNI_HMAC_KEY=${HMAC_KEY}"
echo "    → copy this into somniguard_pico/config.py as GATEWAY_HMAC_KEY"

# ============================================================================
# 7. Firewall (UFW)
# ============================================================================
echo -e "${YELLOW}[7/9] Configuring UFW firewall…${NC}"
ufw --force enable >/dev/null
ufw default deny incoming  >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp   >/dev/null    # SSH (don't lock yourself out)
ufw allow 5443/tcp >/dev/null    # gateway HTTPS (mTLS)
ufw allow 5000/tcp >/dev/null    # gateway plain-HTTP /api/time bootstrap
ufw allow 5353/udp >/dev/null    # mDNS for somniguard.local
echo -e "${GREEN}  ✓ Firewall: deny-by-default, allow {22,5443,5000,5353}.${NC}"

# ============================================================================
# 8. systemd unit (low-priv user, sandboxed, EnvironmentFile-driven)
# ============================================================================
echo -e "${YELLOW}[8/9] Installing systemd unit ${SVC_UNIT}…${NC}"
cat > "${SVC_UNIT}" <<EOF
[Unit]
Description=SOMNI-Guard Gateway — sleep-monitoring REST API + dashboard
Documentation=https://github.com/${USER}/NightWatchGaurd

# network-online.target is gated by NetworkManager-wait-online.service,
# which signals "at least one managed connection is active" — that
# includes the SomniGuard_Hotspot AP profile with autoconnect=yes, so
# by the time we start, nmcli already reports the AP as active.
After=network-online.target NetworkManager.service
Wants=network-online.target NetworkManager.service

# Boot-loop guard.  Default (5 starts in 10s) is too tight for a Pi 5
# cold boot where cert regeneration + nmcli probe can each take >5s.
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_GROUP}

# netdev → NetworkManager polkit rules grant network-control +
# system-connection-modify.  Lets the gateway run plain 'nmcli' to
# probe/bring up the hotspot without sudo (which would be blocked by
# any setuid restriction).
SupplementaryGroups=netdev

WorkingDirectory=${GATEWAY_DIR}

# All runtime configuration comes from this file (KEY=VALUE format).
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
# Force credentials into a path that survives ProtectHome=read-only.
Environment=SOMNI_HOTSPOT_CREDS=${DATA_DIR}/hotspot_credentials.json

# Refresh server cert SANs for current IPs — best-effort.  The leading '-'
# tells systemd to ignore a non-zero exit so a transient failure here
# (filesystem still mounting, cryptography lib slow on first import) does
# NOT block the main service from starting.  run.py also re-runs the same
# script at boot.
ExecStartPre=-${VENV_DIR}/bin/python ${SCRIPTS_DIR}/setup_gateway_certs.py --cert-dir ${CERT_DIR}

# Main process — gunicorn is launched from inside run.py.
ExecStart=${VENV_DIR}/bin/python ${GATEWAY_DIR}/run.py

Restart=always
RestartSec=5
TimeoutStartSec=120
TimeoutStopSec=20
KillSignal=SIGTERM
SuccessExitStatus=0
RestartPreventExitStatus=0

# ── Sandboxing — relaxed enough to actually run on a Pi ─────────────────────
# Intentionally NOT setting:
#   NoNewPrivileges / RestrictSUIDSGID: kept off so any setuid helper still
#     works if needed; nmcli today goes via the netdev group + polkit and
#     does NOT need either.
#   MemoryDenyWriteExecute: documented as incompatible with Python
#     extensions that use cffi callbacks / aarch64 BTI mprotect.

PrivateTmp=true
PrivateDevices=true

# 'full' = /usr + /boot + /etc read-only; ReadWritePaths punches holes.
ProtectSystem=full

# 'read-only' lets the service READ source from /home/<user>/NightWatchGaurd
# on developer Pis while still blocking writes into home directories.
# Production installs under /opt/ are unaffected.
ProtectHome=read-only

ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictNamespaces=true
RestrictRealtime=true
LockPersonality=true
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK

# Only these paths are writable to the service:
ReadWritePaths=${DATA_DIR} ${LOG_DIR} ${CERT_DIR} ${REPORT_DIR}

# Logs go to the journal (journalctl -u ${SVC_NAME} -f).
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SVC_NAME}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SVC_NAME}" >/dev/null
echo -e "${GREEN}  ✓ Unit installed and enabled (will start on every boot).${NC}"

# ============================================================================
# 9. Bootstrap admin (interactive, idempotent), then start the service
# ============================================================================
echo -e "${YELLOW}[9/9] Bootstrapping admin account + starting service…${NC}"

# Run the admin bootstrap interactively as the service user (it will skip if
# any user already exists — idempotent on re-runs).
set -a; . "${ENV_FILE}"; set +a
if [[ -t 0 ]]; then
    sudo -u "${SVC_USER}" --preserve-env=SOMNI_SECRET_KEY,SOMNI_HMAC_KEY,SOMNI_DB_PATH,SOMNI_REPORT_DIR,SOMNI_AUDIT_LOG_DIR,SOMNI_HTTPS,SOMNI_HOST,SOMNI_PORT,SOMNI_HOTSPOT,SOMNI_WORKERS,SOMNI_THREADS,SOMNI_DEBUG,SOMNI_TAILSCALE_ONLY,SOMNI_PICO_CIDRS \
        "${VENV_DIR}/bin/python" -c "
import sys, os
sys.path.insert(0, '${GATEWAY_DIR}')
import database as db
db.init_db()
if not db.list_users():
    print('[SOMNI] No users yet — launching interactive admin bootstrap…')
    from run import _bootstrap_admin
    _bootstrap_admin()
else:
    print('[SOMNI] Admin user(s) already exist — skipping bootstrap.')
"
else
    echo "  ⚠ Non-interactive shell — skipping admin bootstrap."
    echo "    Run later:  sudo -u ${SVC_USER} ${VENV_DIR}/bin/python ${GATEWAY_DIR}/run.py"
fi

# Start the service (or restart if it was already running)
systemctl restart "${SVC_NAME}"
sleep 2
if systemctl is-active --quiet "${SVC_NAME}"; then
    echo -e "${GREEN}  ✓ Service '${SVC_NAME}' is RUNNING.${NC}"
else
    echo -e "${RED}  ✗ Service failed to start. Check: journalctl -u ${SVC_NAME} -n 50${NC}"
    systemctl status "${SVC_NAME}" --no-pager || true
    exit 1
fi

# ============================================================================
# Summary
# ============================================================================
echo ""
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN} ✓ SOMNI-Guard gateway installed and running${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo ""
echo "Dashboard:        https://$(hostname -I | awk '{print $1}'):5443"
echo "                  https://somniguard.local:5443  (mDNS)"
echo ""
echo "Service control:"
echo "  sudo systemctl status   ${SVC_NAME}"
echo "  sudo systemctl restart  ${SVC_NAME}"
echo "  sudo journalctl -u ${SVC_NAME} -f"
echo ""
echo "Pico provisioning (run from your dev machine):"
echo "  python3 scripts/embed_pico_cert.py \\"
echo "      --ca-cert     ${CERT_DIR}/ca.crt \\"
echo "      --client-cert ${CERT_DIR}/pico_client.crt \\"
echo "      --client-key  ${CERT_DIR}/pico_client.key"
echo "  python3 scripts/encrypt_pico_files.py"
echo "  mpremote connect /dev/cu.usbmodem* fs cp -r somniguard_pico/. :"
echo ""
