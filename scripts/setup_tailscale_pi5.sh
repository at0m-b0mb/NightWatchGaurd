#!/usr/bin/env bash
# setup_tailscale_pi5.sh — Install and configure Tailscale on the SOMNI‑Guard Pi 5 gateway.
#
# Usage:
#   chmod +x scripts/setup_tailscale_pi5.sh
#   sudo ./scripts/setup_tailscale_pi5.sh
#
# What this script does:
#   1. Installs Tailscale via the official install script.
#   2. Starts and enables the Tailscale systemd service.
#   3. Authenticates the Pi 5 with your tailnet (opens a browser link).
#   4. Enables Tailscale SSH for convenient remote access.
#   5. Prints the assigned Tailscale IP and MagicDNS hostname.
#   6. Configures the SOMNI‑Guard gateway to accept connections only from
#      Tailscale peers (sets SOMNI_TAILSCALE_ONLY=true in the service env file).
#
# Prerequisites:
#   • Raspberry Pi OS (Bookworm/Bullseye) with internet access for the install.
#   • A Tailscale account — sign up free at https://tailscale.com
#   • Run as root (sudo).
#
# Educational prototype — not a clinically approved device.

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[SOMNI]${NC} $*"; }
warn()    { echo -e "${YELLOW}[SOMNI][WARN]${NC} $*"; }
fatal()   { echo -e "${RED}[SOMNI][FATAL]${NC} $*"; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || fatal "This script must be run as root (sudo)."
}

require_cmd() {
    command -v "$1" &>/dev/null || fatal "Required command '$1' not found. Install it and re-run."
}

# ---------------------------------------------------------------------------
# Step 0 — sanity checks
# ---------------------------------------------------------------------------

require_root
info "SOMNI‑Guard Tailscale setup starting…"

# Detect OS (Raspberry Pi OS identifies as debian / raspbian in /etc/os-release)
if ! grep -qiE '^(ID|ID_LIKE)=.*(raspbian|debian)' /etc/os-release 2>/dev/null; then
    warn "This script was written for Raspberry Pi OS (Bookworm).  Proceed with caution."
fi

# python3 and curl are used below.  Fail loudly if absent instead of half-way.
require_cmd curl
require_cmd python3

# ---------------------------------------------------------------------------
# Step 1 — Install Tailscale
# ---------------------------------------------------------------------------

if command -v tailscale &>/dev/null; then
    TAILSCALE_VER=$(tailscale version | head -1)
    info "Tailscale already installed: ${TAILSCALE_VER}"
else
    info "Installing Tailscale…"
    curl -fsSL https://tailscale.com/install.sh | sh
    info "Tailscale installed."
fi

# ---------------------------------------------------------------------------
# Step 2 — Enable and start the Tailscale daemon
# ---------------------------------------------------------------------------

info "Enabling and starting Tailscale daemon…"
systemctl enable --now tailscaled

# ---------------------------------------------------------------------------
# Step 3 — Authenticate with Tailscale
# ---------------------------------------------------------------------------

TAILSCALE_STATE=$(tailscale status --json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('BackendState',''))" 2>/dev/null || true)

if [[ "$TAILSCALE_STATE" == "Running" ]]; then
    info "Already authenticated with Tailscale."
else
    info "Authenticating with Tailscale…"
    info "A URL will appear below — open it in a browser to approve this device."
    echo ""
    # --ssh enables Tailscale SSH (secure shell without opening port 22 to the world)
    # --accept-routes accepts subnet routes advertised by other nodes
    tailscale up --ssh --accept-routes --hostname="somni-pi5" || true
    echo ""
fi

# ---------------------------------------------------------------------------
# Step 4 — Print Tailscale IP and hostname
# ---------------------------------------------------------------------------

TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")
TAILSCALE_HOST=$(tailscale status --json 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Self',{}).get('DNSName',''))" 2>/dev/null \
    || echo "unknown")

echo ""
info "Tailscale IP:       ${TAILSCALE_IP}"
info "Tailscale hostname: ${TAILSCALE_HOST}"
echo ""

# ---------------------------------------------------------------------------
# Step 5 — Configure SOMNI‑Guard to use Tailscale-only mode
# ---------------------------------------------------------------------------

ENV_FILE="/etc/somniguard/env"
info "Writing SOMNI‑Guard environment file: ${ENV_FILE}"
mkdir -p /etc/somniguard
chmod 700 /etc/somniguard

# Generate a random secret key if not already set
if [[ ! -f "${ENV_FILE}" ]]; then
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    HMAC_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    cat > "${ENV_FILE}" <<EOF
# SOMNI‑Guard gateway environment — auto-generated by setup_tailscale_pi5.sh
# WARNING: Keep this file secret.  It contains cryptographic keys.
SOMNI_SECRET_KEY=${SECRET_KEY}
SOMNI_HMAC_KEY=${HMAC_KEY}
SOMNI_DB_PATH=/var/lib/somniguard/somni.db
SOMNI_REPORT_DIR=/var/lib/somniguard/reports
SOMNI_TAILSCALE_ONLY=true
SOMNI_PORT=5000
SOMNI_DEBUG=false
EOF
    chmod 600 "${ENV_FILE}"
    info "Environment file created with fresh secret keys."
    echo ""
    warn "Copy the HMAC key below into GATEWAY_HMAC_KEY in somniguard_pico/config.py."
    warn "It must match exactly, or every Pico packet will be rejected with HTTP 403."
    echo "  SOMNI_HMAC_KEY=${HMAC_KEY}"
    echo ""
else
    # File exists — only set/update TAILSCALE_ONLY
    if grep -q "SOMNI_TAILSCALE_ONLY" "${ENV_FILE}"; then
        sed -i 's/^SOMNI_TAILSCALE_ONLY=.*/SOMNI_TAILSCALE_ONLY=true/' "${ENV_FILE}"
    else
        echo "SOMNI_TAILSCALE_ONLY=true" >> "${ENV_FILE}"
    fi
    info "Updated ${ENV_FILE}: SOMNI_TAILSCALE_ONLY=true"
fi

# Create data directories
mkdir -p /var/lib/somniguard/reports
chmod 750 /var/lib/somniguard

# ---------------------------------------------------------------------------
# Step 6 — Optional: create a systemd service unit for the gateway
# ---------------------------------------------------------------------------

SERVICE_FILE="/etc/systemd/system/somniguard-gateway.service"
INSTALL_DIR="/opt/somniguard/somniguard_gateway"
VENV_DIR="/opt/somniguard/venv"
SVC_USER="somniguard"

if [[ -d "${INSTALL_DIR}" ]]; then
    # 6a — create the dedicated service account (no shell, no login, no home).
    if ! id -u "${SVC_USER}" &>/dev/null; then
        info "Creating system user '${SVC_USER}' (no login, no shell)"
        useradd --system --home-dir /nonexistent --no-create-home \
                --shell /usr/sbin/nologin "${SVC_USER}"
    else
        info "System user '${SVC_USER}' already exists — reusing."
    fi

    # 6b — ensure data dirs and the install dir itself are owned by the service user.
    chown -R "${SVC_USER}:${SVC_USER}" /var/lib/somniguard
    chown    "${SVC_USER}:${SVC_USER}" "${ENV_FILE}"
    chown -R "${SVC_USER}:${SVC_USER}" "${INSTALL_DIR}" || true

    # 6c — build the venv if the referenced interpreter is missing.
    #       Requires python3-venv (pre-installed on Raspberry Pi OS Bookworm).
    if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
        if ! python3 -c 'import venv' &>/dev/null; then
            warn "python3 'venv' module missing.  Install with:"
            warn "  sudo apt-get install -y python3-venv"
            warn "Skipping venv creation; create it manually before enabling the service."
        else
            info "Creating Python venv at ${VENV_DIR}"
            mkdir -p "$(dirname "${VENV_DIR}")"
            python3 -m venv "${VENV_DIR}"
            # Install gateway deps if the requirements file is present.
            REQ_FILE="${INSTALL_DIR}/requirements.txt"
            if [[ -f "${REQ_FILE}" ]]; then
                info "Installing gateway Python dependencies (may take a minute)…"
                "${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null
                "${VENV_DIR}/bin/pip" install -r "${REQ_FILE}"
            else
                warn "No requirements.txt at ${REQ_FILE}; venv created but no deps installed."
            fi
            chown -R "${SVC_USER}:${SVC_USER}" "${VENV_DIR}"
        fi
    else
        info "Venv already present at ${VENV_DIR} — reusing."
    fi

    # 6d — write the unit.
    info "Creating systemd service: somniguard-gateway"
    cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=SOMNI‑Guard Gateway (Flask + ingestion API)
After=network-online.target tailscaled.service
Wants=network-online.target tailscaled.service

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python run.py
Restart=on-failure
RestartSec=10s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=somniguard

# Hardening — the gateway only needs its own data dirs.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/somniguard

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    info "Service unit written to ${SERVICE_FILE}"
    info "Enable with: sudo systemctl enable --now somniguard-gateway"
else
    warn "Gateway install dir not found (${INSTALL_DIR}); skipping systemd service creation."
    warn "Copy somniguard_gateway/ to ${INSTALL_DIR} and re-run, or create the service manually."
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
info "=========================================="
info " SOMNI‑Guard Tailscale setup complete"
info "=========================================="
echo ""
echo "  Tailscale IP      : ${TAILSCALE_IP}"
echo "  MagicDNS hostname : ${TAILSCALE_HOST}"
echo "  Environment file  : ${ENV_FILE}"
echo ""
echo "  Dashboard URL (from a device on the same tailnet):"
echo "    http://${TAILSCALE_IP}:5000/"
if [[ "${TAILSCALE_HOST}" != "unknown" && -n "${TAILSCALE_HOST}" ]]; then
    CLEAN_HOST="${TAILSCALE_HOST%.}"
    echo "    http://${CLEAN_HOST}:5000/"
fi
echo ""
echo "  Next steps:"
echo "  1. Install Tailscale on your developer laptop and sign in to the same account."
echo "  2. Open the URL above in your browser — no firewall rules needed."
echo "  3. Set GATEWAY_HMAC_KEY in somniguard_pico/config.py to match SOMNI_HMAC_KEY"
echo "     in ${ENV_FILE}."
echo ""
warn "EDUCATIONAL PROTOTYPE — not a regulated medical device."
