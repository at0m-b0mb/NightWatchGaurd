#!/usr/bin/env bash
# check_gateway_status.sh — Diagnostic script to verify SOMNI-Guard gateway is running correctly.
#
# Usage:
#   bash scripts/check_gateway_status.sh
#

set -u

# ── Helpers ──────────────────────────────────────────────────────────────────
info()  { echo "[CHECK] $*"; }
ok()    { echo "[CHECK] ✓ $*"; }
warn()  { echo "[CHECK] ⚠ $*"; }
fail()  { echo "[CHECK] ✗ $*"; }

# ── Detect paths ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
GATEWAY_DIR="$PROJECT_ROOT/somniguard_gateway"
ENV_DIR="/etc/somniguard"
ENV_FILE="$ENV_DIR/env"

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  SOMNI-Guard Gateway Status Diagnostic"
echo "════════════════════════════════════════════════════════════════"
echo ""

# ── 1. Check systemd service status ──────────────────────────────────────────
info "Checking systemd service status…"
if systemctl is-active --quiet somniguard.service; then
    ok "somniguard.service is RUNNING"
else
    fail "somniguard.service is NOT running"
    info "Attempting to start it…"
    sudo systemctl start somniguard.service
    sleep 3
    if systemctl is-active --quiet somniguard.service; then
        ok "Service started successfully"
    else
        fail "Failed to start service"
    fi
fi

# ── 2. Check if enabled on boot ──────────────────────────────────────────────
info "Checking if service is enabled on boot…"
if systemctl is-enabled --quiet somniguard.service; then
    ok "Service is enabled on boot"
else
    warn "Service is NOT enabled on boot"
    info "Enabling it now…"
    sudo systemctl enable somniguard.service
    ok "Service enabled"
fi

# ── 3. Check environment file ────────────────────────────────────────────────
echo ""
info "Checking environment file…"
if [[ -f "$ENV_FILE" ]]; then
    ok "Environment file exists: $ENV_FILE"
    if grep -q "SOMNI_SECRET_KEY=" "$ENV_FILE"; then
        ok "Environment variables are set"
    else
        fail "Environment variables missing in $ENV_FILE"
    fi
else
    fail "Environment file NOT found: $ENV_FILE"
fi

# ── 4. Check database ────────────────────────────────────────────────────────
echo ""
info "Checking database…"
DB_PATH="$GATEWAY_DIR/somniguard.db"
if [[ -f "$DB_PATH" ]]; then
    ok "Database exists: $DB_PATH"
    DB_SIZE=$(ls -lh "$DB_PATH" | awk '{print $5}')
    info "Database size: $DB_SIZE"
else
    fail "Database NOT found: $DB_PATH"
fi

# ── 5. Check TLS certificate ────────────────────────────────────────────────
echo ""
info "Checking TLS certificate…"
CERT_PATH="$GATEWAY_DIR/certs/server.crt"
if [[ -f "$CERT_PATH" ]]; then
    ok "TLS certificate exists"
    CERT_EXPIRY=$(openssl x509 -enddate -noout -in "$CERT_PATH" 2>/dev/null | cut -d= -f2)
    info "Certificate expires: $CERT_EXPIRY"
else
    warn "TLS certificate NOT found at $CERT_PATH"
    info "It will be generated on first service startup"
fi

# ── 6. Check service logs ────────────────────────────────────────────────────
echo ""
info "Recent service logs (last 15 lines):"
echo "────────────────────────────────────────────────────────────────"
journalctl -u somniguard -n 15 --no-pager || warn "No logs found"
echo "────────────────────────────────────────────────────────────────"

# ── 7. Check if gateway is accessible ────────────────────────────────────────
echo ""
info "Checking if gateway is accessible…"
if ss -ulnp 2>/dev/null | grep -q ':5443 '; then
    ok "Gateway is listening on port 5443"
else
    warn "Gateway not listening on port 5443 yet"
    info "It may still be starting up"
fi

# ── 8. Check hotspot status ──────────────────────────────────────────────────
echo ""
info "Checking Wi-Fi hotspot…"
HOTSPOT_IFACE="${SOMNI_HOTSPOT_IFACE:-wlan0}"
if ip -4 -o addr show "$HOTSPOT_IFACE" 2>/dev/null | grep -q '10\.42\.0\.1'; then
    ok "Hotspot is UP on $HOTSPOT_IFACE"
else
    warn "Hotspot not ready yet (will be created when service is running)"
fi

# ── 9. Provide next steps ────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Next Steps"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "  1. Wait 10-15 seconds for the service to fully start"
echo "  2. Access the dashboard:"
echo "     https://10.42.0.1:5443/"
echo ""
echo "  3. Default login (from install output):"
echo "     Username: admin"
echo "     Password: (check /tmp/somniguard_seed.log or install output)"
echo ""
echo "  4. If service is still not running, check logs:"
echo "     journalctl -u somniguard -f"
echo ""
echo "  5. To manually restart the service:"
echo "     sudo systemctl restart somniguard"
echo ""
echo "════════════════════════════════════════════════════════════════"
echo ""
