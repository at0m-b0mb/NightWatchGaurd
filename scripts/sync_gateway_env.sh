#!/usr/bin/env bash
################################################################################
# sync_gateway_env.sh — repair /etc/somniguard/env so the gateway HMAC key
# matches what the Pico is actually signing with, then make sure the gateway
# starts on boot.
#
# Idempotent — run it as many times as you like. It will:
#   1. Read GATEWAY_HMAC_KEY from somniguard_pico/config.py.
#   2. Create/repair /etc/somniguard/env (mode 0640, owned by root:somniguard
#      if that group exists, else root:root) so SOMNI_HMAC_KEY equals the
#      Pico's value byte-for-byte.
#   3. Preserve any existing SOMNI_SECRET_KEY; otherwise generate one.
#   4. (Re)install + enable the somniguard-gateway.service systemd unit so the
#      gateway starts automatically on every boot.
#   5. Restart the service and verify it's healthy.
#
# Run on the Pi 5:
#     sudo bash scripts/sync_gateway_env.sh
#
# Educational prototype — not a clinically approved device.
################################################################################

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY_DIR="${PROJECT_DIR}/somniguard_gateway"
PICO_CONFIG="${PROJECT_DIR}/somniguard_pico/config.py"
SCRIPTS_DIR="${PROJECT_DIR}/scripts"
VENV_DIR="${PROJECT_DIR}/.venv"

ETC_DIR="/etc/somniguard"
ENV_FILE="${ETC_DIR}/env"
DATA_DIR="/var/lib/somniguard"
LOG_DIR="/var/log/somniguard"
REPORT_DIR="${DATA_DIR}/reports"
CERT_DIR="${ETC_DIR}/certs"

SVC_USER="somniguard"
SVC_GROUP="somniguard"
SVC_NAME="somniguard-gateway"
SVC_UNIT="/etc/systemd/system/${SVC_NAME}.service"

echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE} SOMNI-Guard env-file + service repair${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"

if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}ERROR: must run as root.${NC}"
    echo "    sudo bash scripts/sync_gateway_env.sh"
    exit 1
fi

# ── 1. Pull GATEWAY_HMAC_KEY out of the Pico config ─────────────────────────
echo -e "${YELLOW}[1/5] Reading GATEWAY_HMAC_KEY from Pico config…${NC}"
if [[ ! -f "${PICO_CONFIG}" ]]; then
    echo -e "${RED}  ✗ Not found: ${PICO_CONFIG}${NC}"
    exit 1
fi

PICO_HMAC=$(python3 - "${PICO_CONFIG}" <<'PY'
import re, sys
src = open(sys.argv[1], "r", encoding="utf-8").read()
m = re.search(r'^GATEWAY_HMAC_KEY\s*=\s*["\']([^"\']+)["\']', src, re.M)
if not m:
    sys.exit("GATEWAY_HMAC_KEY not found in pico config")
print(m.group(1))
PY
)

if [[ -z "${PICO_HMAC}" ]]; then
    echo -e "${RED}  ✗ Could not extract GATEWAY_HMAC_KEY.${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ Pico HMAC key: ${PICO_HMAC:0:8}…${PICO_HMAC: -8}${NC}"

# ── 2. Service group (only if setup_gateway.sh hasn't been run yet) ─────────
if ! getent group "${SVC_GROUP}" >/dev/null 2>&1; then
    echo -e "${YELLOW}  • Creating service group/user '${SVC_USER}'…${NC}"
    useradd --system --no-create-home --shell /usr/sbin/nologin \
            --comment "SOMNI-Guard gateway service" "${SVC_USER}" 2>/dev/null || true
fi

# netdev membership lets the service user run `nmcli` without sudo
# (NetworkManager's default polkit rules grant netdev both
# network-control and system-connection-modify).  Idempotent; safe to
# re-run.  If netdev doesn't exist on a minimal image, create it.
if id -u "${SVC_USER}" >/dev/null 2>&1; then
    if ! getent group netdev >/dev/null 2>&1; then
        groupadd --system netdev 2>/dev/null || true
    fi
    usermod -aG netdev "${SVC_USER}" 2>/dev/null || true
fi

OWNER_GROUP="root"
if getent group "${SVC_GROUP}" >/dev/null 2>&1; then
    OWNER_GROUP="${SVC_GROUP}"
fi

# ── 3. Repair /etc/somniguard/env ────────────────────────────────────────────
echo -e "${YELLOW}[2/5] Repairing ${ENV_FILE}…${NC}"
install -d -m 0750 -o root -g "${OWNER_GROUP}" "${ETC_DIR}"
install -d -m 0750 -o root -g "${OWNER_GROUP}" "${DATA_DIR}" 2>/dev/null || true
install -d -m 0750 -o root -g "${OWNER_GROUP}" "${LOG_DIR}"  2>/dev/null || true
install -d -m 0750 -o root -g "${OWNER_GROUP}" "${REPORT_DIR}" 2>/dev/null || true
install -d -m 0700 -o root -g "${OWNER_GROUP}" "${CERT_DIR}" 2>/dev/null || true

EXISTING_SECRET=""
if [[ -f "${ENV_FILE}" ]]; then
    cp -a "${ENV_FILE}" "${ENV_FILE}.bak.$(date +%s)"
    EXISTING_SECRET=$(grep -E '^SOMNI_SECRET_KEY=' "${ENV_FILE}" | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
SECRET_KEY="${EXISTING_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"

cat > "${ENV_FILE}" <<EOF
# SOMNI-Guard gateway environment — single source of truth.
# Format: KEY=VALUE per line, no leading 'export'.
# Generated/repaired by scripts/sync_gateway_env.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ").

SOMNI_SECRET_KEY=${SECRET_KEY}
SOMNI_HMAC_KEY=${PICO_HMAC}

SOMNI_DB_PATH=${DATA_DIR}/somniguard.db
SOMNI_REPORT_DIR=${REPORT_DIR}
SOMNI_AUDIT_LOG_DIR=${LOG_DIR}

SOMNI_HTTPS=true
SOMNI_HOST=0.0.0.0
SOMNI_PORT=5443

# Hotspot credentials live under /var/lib/somniguard so the systemd
# unit's ReadWritePaths covers the location.  Putting it inside the
# project tree (e.g. /home/pi/NightWatchGaurd/) would fail under
# ProtectHome=read-only / ProtectSystem=full.
SOMNI_HOTSPOT_CREDS=${DATA_DIR}/hotspot_credentials.json

SOMNI_DEBUG=false
SOMNI_TAILSCALE_ONLY=false
SOMNI_PICO_CIDRS=10.42.0.0/24,127.0.0.1/32
EOF
chmod 0640 "${ENV_FILE}"
chown "root:${OWNER_GROUP}" "${ENV_FILE}"
echo -e "${GREEN}  ✓ Wrote ${ENV_FILE} (mode 0640, root:${OWNER_GROUP}).${NC}"

# ── 4. Validate: Python config.py must import without error ──────────────────
echo -e "${YELLOW}[3/5] Validating config.py loads cleanly…${NC}"
PY_BIN="${VENV_DIR}/bin/python"
[[ -x "${PY_BIN}" ]] || PY_BIN="$(command -v python3)"

if ! "${PY_BIN}" -c "
import sys; sys.path.insert(0, '${GATEWAY_DIR}')
import config as cfg
assert cfg.PICO_HMAC_KEY == '${PICO_HMAC}', 'HMAC mismatch after write'
print('  ✓ config.PICO_HMAC_KEY matches Pico ({}…{}).'.format(cfg.PICO_HMAC_KEY[:8], cfg.PICO_HMAC_KEY[-8:]))
print('  ✓ config.SECRET_KEY length =', len(cfg.SECRET_KEY))
"; then
    echo -e "${RED}  ✗ config.py refused to load. Inspect ${ENV_FILE}.${NC}"
    exit 1
fi

# ── 5. systemd unit: install if missing, then enable + restart ───────────────
echo -e "${YELLOW}[4/5] Ensuring systemd unit is installed and enabled…${NC}"
if [[ ! -f "${SVC_UNIT}" ]]; then
    if [[ -f "${PROJECT_DIR}/setup_gateway.sh" ]]; then
        echo -e "${YELLOW}  • No unit found. Run setup_gateway.sh once for the full install:${NC}"
        echo "      sudo bash ${PROJECT_DIR}/setup_gateway.sh"
        echo "    Then re-run this script to keep the env file in sync."
    else
        echo -e "${RED}  ✗ ${SVC_UNIT} missing and setup_gateway.sh not present.${NC}"
        exit 1
    fi
else
    systemctl daemon-reload
    systemctl enable "${SVC_NAME}" >/dev/null
    echo -e "${GREEN}  ✓ ${SVC_NAME} enabled (will start on every boot).${NC}"
fi

# ── 6. Restart + smoke-check ────────────────────────────────────────────────
echo -e "${YELLOW}[5/5] Restarting ${SVC_NAME}…${NC}"
if [[ -f "${SVC_UNIT}" ]]; then
    systemctl restart "${SVC_NAME}"
    sleep 2
    if systemctl is-active --quiet "${SVC_NAME}"; then
        echo -e "${GREEN}  ✓ ${SVC_NAME} is RUNNING.${NC}"
    else
        echo -e "${RED}  ✗ ${SVC_NAME} failed to start.${NC}"
        echo "    Check: sudo journalctl -u ${SVC_NAME} -n 80 --no-pager"
        exit 1
    fi
fi

echo ""
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN} ✓ Env file repaired and gateway restarted${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo ""
echo "Verify on the Pico — you should now see:"
echo "    [SOMNI][TRANSPORT] Session started: ID …"
echo ""
echo "Useful commands:"
echo "    sudo systemctl status   ${SVC_NAME}"
echo "    sudo journalctl -u ${SVC_NAME} -f"
echo "    sudo cat ${ENV_FILE}"
echo ""
