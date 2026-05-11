#!/usr/bin/env bash
################################################################################
# fix_autostart.sh — repair SOMNI-Guard gateway autostart on the Pi 5.
#
# Run on the Raspberry Pi 5 when:
#   - `sudo reboot` leaves the gateway unstarted
#   - `systemctl is-enabled somniguard-gateway` says `disabled` or `static`
#   - You have to manually `systemctl start somniguard-gateway` after every boot
#
# This script is idempotent — safe to run any number of times.  It does NOT
# regenerate certs, the database, or the env file (those belong to
# setup_gateway.sh / sync_gateway_env.sh).  It only fixes the systemd plumbing.
#
#     sudo bash scripts/fix_autostart.sh
#
# Educational prototype — not a clinically approved device.
################################################################################

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY_DIR="${PROJECT_DIR}/somniguard_gateway"
VENV_DIR="${PROJECT_DIR}/.venv"
SCRIPTS_DIR="${PROJECT_DIR}/scripts"

SVC_NAME="somniguard-gateway"
SVC_USER="somniguard"
SVC_UNIT="/etc/systemd/system/${SVC_NAME}.service"

ETC_DIR="/etc/somniguard"
ENV_FILE="${ETC_DIR}/env"
DATA_DIR="/var/lib/somniguard"
LOG_DIR="/var/log/somniguard"
REPORT_DIR="${DATA_DIR}/reports"
CERT_DIR="${ETC_DIR}/certs"

# ── Banner ──────────────────────────────────────────────────────────────────
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE} SOMNI-Guard autostart repair${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo "  Project dir : ${PROJECT_DIR}"
echo "  Unit path   : ${SVC_UNIT}"
echo ""

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}ERROR: must run as root.  sudo bash scripts/fix_autostart.sh${NC}"
    exit 1
fi

# ── 1. Verify the venv exists and is runnable as the service user ──────────
echo -e "${YELLOW}[1/8] Checking venv at ${VENV_DIR}…${NC}"
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo -e "${RED}  ✗ ${VENV_DIR}/bin/python is missing or not executable.${NC}"
    echo "    Run: sudo bash setup_gateway.sh first."
    exit 1
fi
echo -e "${GREEN}  ✓ venv python found.${NC}"

# ── 2. Verify the env file ──────────────────────────────────────────────────
echo -e "${YELLOW}[2/8] Checking env file at ${ENV_FILE}…${NC}"
if [[ ! -f "${ENV_FILE}" ]]; then
    echo -e "${RED}  ✗ ${ENV_FILE} is missing.${NC}"
    echo "    Run: sudo bash scripts/sync_gateway_env.sh"
    exit 1
fi
for key in SOMNI_SECRET_KEY SOMNI_HMAC_KEY; do
    if ! grep -qE "^${key}=." "${ENV_FILE}"; then
        echo -e "${RED}  ✗ ${key} missing or empty in ${ENV_FILE}.${NC}"
        echo "    Run: sudo bash scripts/sync_gateway_env.sh"
        exit 1
    fi
done
echo -e "${GREEN}  ✓ env file has SOMNI_SECRET_KEY and SOMNI_HMAC_KEY.${NC}"

# ── 3. Verify the service user exists and has netdev membership ────────────
echo -e "${YELLOW}[3/8] Checking service user '${SVC_USER}'…${NC}"
if ! id -u "${SVC_USER}" >/dev/null 2>&1; then
    echo -e "${RED}  ✗ User '${SVC_USER}' does not exist.${NC}"
    echo "    Run: sudo bash setup_gateway.sh first."
    exit 1
fi
if ! getent group netdev >/dev/null 2>&1; then
    echo -e "${YELLOW}  • netdev group missing — creating…${NC}"
    groupadd --system netdev
fi
if ! id -nG "${SVC_USER}" | tr ' ' '\n' | grep -qx netdev; then
    echo -e "${YELLOW}  • Adding ${SVC_USER} to netdev (for nmcli polkit)…${NC}"
    usermod -aG netdev "${SVC_USER}"
fi
echo -e "${GREEN}  ✓ ${SVC_USER} is in: $(id -nG "${SVC_USER}").${NC}"

# ── 4. Verify writable dirs ─────────────────────────────────────────────────
echo -e "${YELLOW}[4/8] Checking writable state directories…${NC}"
for d in "${DATA_DIR}" "${LOG_DIR}" "${CERT_DIR}" "${REPORT_DIR}"; do
    if [[ ! -d "${d}" ]]; then
        install -d -m 0750 -o "${SVC_USER}" -g "${SVC_USER}" "${d}"
        echo "    • created ${d}"
    fi
done
chown -R "${SVC_USER}:${SVC_USER}" "${DATA_DIR}" "${LOG_DIR}" 2>/dev/null || true
echo -e "${GREEN}  ✓ State dirs owned by ${SVC_USER}.${NC}"

# ── 5. Re-write the unit file with the right paths for THIS install ────────
echo -e "${YELLOW}[5/8] Rewriting ${SVC_UNIT}…${NC}"

EXTRA_UNIT_LINES=""

cat > "${SVC_UNIT}" <<EOF
[Unit]
Description=SOMNI-Guard Gateway — sleep-monitoring REST API + dashboard
Documentation=file://${PROJECT_DIR}/GUIDE.md

After=network-online.target NetworkManager.service
Wants=network-online.target NetworkManager.service
${EXTRA_UNIT_LINES}

# Don't give up after a few quick crashes — Pi 5 cold boot timing is
# noisy (NetworkManager probe, slow first import of cryptography).
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
SupplementaryGroups=netdev

WorkingDirectory=${GATEWAY_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=SOMNI_HOTSPOT_CREDS=${DATA_DIR}/hotspot_credentials.json

ExecStartPre=-${VENV_DIR}/bin/python ${SCRIPTS_DIR}/setup_gateway_certs.py --cert-dir ${CERT_DIR}
ExecStart=${VENV_DIR}/bin/python ${GATEWAY_DIR}/run.py

Restart=always
RestartSec=5
TimeoutStartSec=120
TimeoutStopSec=20
KillSignal=SIGTERM
SuccessExitStatus=0
RestartPreventExitStatus=0

PrivateTmp=true
PrivateDevices=true
ProtectSystem=full
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

ReadWritePaths=${DATA_DIR} ${LOG_DIR} ${CERT_DIR} ${REPORT_DIR}

StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SVC_NAME}

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "${SVC_UNIT}"
echo -e "${GREEN}  ✓ Unit written.${NC}"

# ── 6. Reload, enable, restart ─────────────────────────────────────────────
echo -e "${YELLOW}[6/8] Reloading systemd and (re)enabling the unit…${NC}"
systemctl daemon-reload
# Defensive triple play:
#   * unmask in case the unit was masked (symlink -> /dev/null) — the
#     `is-enabled` output for that state is `masked`, and `enable` then
#     fails silently in older systemd.
#   * reset-failed clears the StartLimitBurst counter so a previously
#     boot-looped unit can start again immediately.
#   * reenable is `disable` + `enable` — guaranteed fresh symlink in
#     /etc/systemd/system/multi-user.target.wants/.
systemctl unmask    "${SVC_NAME}" 2>/dev/null || true
systemctl reset-failed "${SVC_NAME}" 2>/dev/null || true
systemctl reenable  "${SVC_NAME}"

echo -e "${YELLOW}[7/8] Restarting ${SVC_NAME}…${NC}"
systemctl restart "${SVC_NAME}"
sleep 3

# ── 7. Verify ───────────────────────────────────────────────────────────────
echo -e "${YELLOW}[8/8] Verifying…${NC}"

is_enabled=$(systemctl is-enabled "${SVC_NAME}" 2>&1 || true)
is_active=$( systemctl is-active  "${SVC_NAME}" 2>&1 || true)

echo "  is-enabled : ${is_enabled}"
echo "  is-active  : ${is_active}"

if [[ "${is_enabled}" != "enabled" ]]; then
    echo -e "${RED}  ✗ Unit is not persistently enabled.${NC}"
    echo "    journalctl -u ${SVC_NAME} -n 80 --no-pager"
    exit 1
fi

if [[ "${is_active}" != "active" ]]; then
    echo -e "${RED}  ✗ Unit is not running right now.  Last 30 log lines:${NC}"
    journalctl -u "${SVC_NAME}" -n 30 --no-pager
    exit 1
fi

# Verify the symlink that makes autostart actually happen on reboot.
symlink="/etc/systemd/system/multi-user.target.wants/${SVC_NAME}.service"
if [[ -L "${symlink}" ]]; then
    target=$(readlink -f "${symlink}" 2>/dev/null || echo "?")
    if [[ "${target}" == "${SVC_UNIT}" ]]; then
        echo -e "${GREEN}  ✓ Autostart symlink present and points at ${SVC_UNIT}${NC}"
    else
        echo -e "${RED}  ✗ Autostart symlink points at the WRONG file: ${target}${NC}"
        echo "    Expected: ${SVC_UNIT}"
        echo "    Forcing re-link…"
        systemctl reenable "${SVC_NAME}"
    fi
else
    echo -e "${RED}  ✗ Autostart symlink MISSING: ${symlink}${NC}"
    echo "    This is the real reason the gateway doesn't come up on reboot."
    echo "    Re-running reenable to force-create it…"
    systemctl reenable "${SVC_NAME}"
    if [[ ! -L "${symlink}" ]]; then
        echo -e "${RED}  ✗ Symlink STILL missing after reenable.  Most likely the${NC}"
        echo -e "${RED}    [Install] section is broken or the unit has no WantedBy=.${NC}"
        echo "    Inspect: systemctl cat ${SVC_NAME}"
        exit 1
    fi
fi

# Encryption-gating check — if the unit Requires=Mounts then surface
# whether the mount is actually present right now.  A common boot-loop
# trap is "service is enabled, mount isn't ready, service times out".
gate_mount=$(systemctl show -p RequiresMountsFor --value "${SVC_NAME}" 2>/dev/null || true)
if [[ -n "${gate_mount}" ]]; then
    echo -e "${BLUE}  • Service is gated on mount: ${gate_mount}${NC}"
    if mountpoint -q "${gate_mount}" 2>/dev/null; then
        echo -e "${GREEN}    ✓ ${gate_mount} is currently mounted.${NC}"
    else
        echo -e "${YELLOW}    ! ${gate_mount} is NOT mounted right now.${NC}"
        echo "      The gateway will stay STOPPED on every boot until that mount is up."
        echo "      If you used setup_file_encryption_pi5.sh, run: sudo somniguard-start"
    fi
fi

# Best-effort sanity check that the dashboard actually answered.
if command -v curl >/dev/null 2>&1; then
    if curl -sk --max-time 5 https://127.0.0.1:5443/api/time | grep -q '"t"'; then
        echo -e "${GREEN}  ✓ /api/time responded over TLS.${NC}"
    else
        echo -e "${YELLOW}  ! /api/time did not respond — check journal logs.${NC}"
    fi
fi

echo ""
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN} ✓ Autostart configured.  Test it now:${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo ""
echo "    sudo reboot"
echo ""
echo "Then from another machine, wait ~30 seconds and:"
echo "    curl -k https://10.42.0.1:5443/api/time"
echo ""
echo "If that answers, autostart is working end-to-end."
echo ""
