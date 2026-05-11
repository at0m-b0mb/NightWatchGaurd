#!/bin/bash
################################################################################
# SOMNI-Guard Pico Setup Script
#
# Handles Pico firmware encryption, certificate embedding, and deployment
# Run this on your development machine (not on the Pico itself)
#
# Prerequisites:
#   - Python 3.8+
#   - mpremote installed (for Pico communication)
#   - Gateway running with TLS enabled (to get the cert)
#
# Usage: bash setup_pico.sh <gateway-ip>
#
# Example: bash setup_pico.sh 10.42.0.1
#
# Educational prototype — not a clinically approved device.
################################################################################

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PICO_DIR="${PROJECT_DIR}/somniguard_pico"
GATEWAY_DIR="${PROJECT_DIR}/somniguard_gateway"
SCRIPTS_DIR="${PROJECT_DIR}/scripts"

echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}SOMNI-Guard Pico Setup${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}\n"

# ============================================================================
# Check Arguments
# ============================================================================
if [[ $# -eq 0 ]]; then
    echo -e "${RED}ERROR: Gateway IP address required${NC}"
    echo ""
    echo "Usage: bash setup_pico.sh <gateway-ip>"
    echo ""
    echo "Example: bash setup_pico.sh 10.42.0.1"
    echo ""
    echo "The gateway IP should be the Raspberry Pi 5 running somniguard_gateway"
    exit 1
fi

GATEWAY_IP="$1"

echo -e "${YELLOW}Configuration:${NC}"
echo "  Project:     $PROJECT_DIR"
echo "  Pico dir:    $PICO_DIR"
echo "  Gateway IP:  $GATEWAY_IP"
echo "  Gateway Port: 5443 (HTTPS)"
echo ""

# ============================================================================
# 1. Check Python Dependencies
# ============================================================================
echo -e "${YELLOW}[1/5] Checking Python dependencies...${NC}"

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "  Python: $PYTHON_VERSION"

# Check for required modules
REQUIRED_MODULES=("cryptography" "requests")
for module in "${REQUIRED_MODULES[@]}"; do
    if python3 -c "import $module" 2>/dev/null; then
        echo "  ✓ $module installed"
    else
        echo -e "${YELLOW}  Installing $module...${NC}"
        pip3 install "$module" -q
        echo "  ✓ $module installed"
    fi
done

# Check for mpremote
if ! command -v mpremote &> /dev/null; then
    echo -e "${YELLOW}  Installing mpremote (Pico communication tool)...${NC}"
    pip3 install mpremote -q
    echo "  ✓ mpremote installed"
else
    echo "  ✓ mpremote already installed"
fi

echo -e "${GREEN}✓ Python dependencies ready${NC}\n"

# ============================================================================
# 2. Verify Gateway Connectivity
# ============================================================================
echo -e "${YELLOW}[2/5] Verifying gateway connectivity...${NC}"

if timeout 5 python3 -c "import socket; s=socket.socket(); s.connect(('$GATEWAY_IP', 5443)); s.close()" 2>/dev/null; then
    echo "  ✓ Gateway reachable at $GATEWAY_IP:5443"
else
    echo -e "${YELLOW}  WARNING: Cannot reach gateway at $GATEWAY_IP:5443${NC}"
    echo "  Make sure:"
    echo "    1. The gateway is running: python run.py"
    echo "    2. Firewall allows port 5443: ufw allow 5443/tcp"
    echo "    3. IP address is correct"
    read -p "  Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo -e "${GREEN}✓ Gateway connectivity verified${NC}\n"

# ============================================================================
# 3. Verify gateway PKI is present locally
# ============================================================================
# The Pico no longer reads any cert from its filesystem — the CA + Pico
# client cert/key live inside config.py (encrypted at rest as config.enc).
# We just need the three PEMs locally so embed_pico_cert.py can splice them
# into config.py.  Generate them on the Pi 5 with:
#     python3 scripts/setup_gateway_certs.py
# then rsync somniguard_gateway/certs/ back to this machine if running here.
echo -e "${YELLOW}[3/5] Verifying gateway PKI is present locally...${NC}"

CA_CERT="$GATEWAY_DIR/certs/ca.crt"
CLIENT_CERT="$GATEWAY_DIR/certs/pico_client.crt"
CLIENT_KEY="$GATEWAY_DIR/certs/pico_client.key"

MISSING=()
[[ -f "$CA_CERT"     ]] || MISSING+=("$CA_CERT")
[[ -f "$CLIENT_CERT" ]] || MISSING+=("$CLIENT_CERT")
[[ -f "$CLIENT_KEY"  ]] || MISSING+=("$CLIENT_KEY")

if (( ${#MISSING[@]} > 0 )); then
    echo -e "${RED}ERROR: missing PKI files:${NC}"
    for f in "${MISSING[@]}"; do echo "  - $f"; done
    echo ""
    echo "Generate them on the Pi 5 gateway:"
    echo "  python3 scripts/setup_gateway_certs.py"
    echo ""
    echo "Then copy the certs directory back to this machine:"
    echo "  rsync -av pi@${GATEWAY_IP}:NightWatchGaurd/somniguard_gateway/certs/ \\"
    echo "      $GATEWAY_DIR/certs/"
    exit 1
fi

echo "  ✓ CA cert:        $CA_CERT"
echo "  ✓ Pico client:    $CLIENT_CERT"
echo "  ✓ Pico key:       $CLIENT_KEY"
echo ""
echo -e "${BLUE}  CA Certificate Details:${NC}"
openssl x509 -in "$CA_CERT" -noout -subject -issuer -startdate -enddate | sed 's/^/    /'
echo ""
echo -e "${GREEN}✓ Gateway PKI ready${NC}\n"

# ============================================================================
# 4. Embed CA + Pico client cert/key into Pico config.py
# ============================================================================
# embed_pico_cert.py rewrites GATEWAY_CA_CERT_PEM, PICO_CLIENT_CERT_PEM,
# and PICO_CLIENT_KEY_PEM in somniguard_pico/config.py.  Nothing is
# written to the Pico filesystem — the firmware loads everything from
# config.py (encrypted as config.enc once you run encrypt_pico_files.py).
echo -e "${YELLOW}[4/5] Embedding PKI into Pico config.py...${NC}"

if [[ ! -f "$SCRIPTS_DIR/embed_pico_cert.py" ]]; then
    echo -e "${RED}ERROR: $SCRIPTS_DIR/embed_pico_cert.py not found${NC}"
    exit 1
fi

python3 "$SCRIPTS_DIR/embed_pico_cert.py"
echo -e "${GREEN}✓ CA + client cert + client key embedded in config.py${NC}\n"

# ============================================================================
# 5. Encrypt Pico Firmware
# ============================================================================
echo -e "${YELLOW}[5/5] Encrypting Pico firmware...${NC}"

if [[ -f "$SCRIPTS_DIR/encrypt_pico_files.py" ]]; then
    echo "  Running firmware encryption..."
    python3 "$SCRIPTS_DIR/encrypt_pico_files.py"
    echo "  ✓ Firmware encrypted"
else
    echo -e "${YELLOW}  WARNING: encrypt_pico_files.py not found${NC}"
    echo "  Firmware will run in plaintext dev mode"
fi

echo -e "${GREEN}✓ Firmware ready${NC}\n"

# ============================================================================
# Summary & Next Steps
# ============================================================================
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}✓ Pico Setup Complete!${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}\n"

echo -e "${YELLOW}Configuration Summary:${NC}"
echo "  Gateway:    $GATEWAY_IP:5443"
echo "  Certificate: $CERT_FILE"
echo "  Config:     $PICO_DIR/config.py"
echo ""

echo -e "${YELLOW}Next Steps:${NC}"
echo ""
echo "1. Verify HMAC key matches:"
echo "   - On Pico config: GATEWAY_HMAC_KEY"
echo "   - On Gateway env: SOMNI_HMAC_KEY"
echo "   (Both should be identical for authentication)"
echo ""
echo "2. Copy files to Pico:"
echo "   - Connect Pico via USB"
echo "   - Run: mpremote cp -r somniguard_pico/* :"
echo ""
echo "3. Test the connection:"
echo "   mpremote connect /dev/ttyUSB0 run picosync_test.py"
echo ""
echo "4. Monitor the Pico:"
echo "   mpremote mount ."
echo "   (Then use repl or serial monitor)"
echo ""

echo -e "${YELLOW}Firewall Check:${NC}"
echo "  Make sure gateway firewall allows:"
echo "    ufw allow 5443/tcp  # HTTPS"
echo "    ufw allow 5000/tcp  # Time sync"
echo ""

echo -e "${YELLOW}Troubleshooting:${NC}"
echo "  If TLS still fails, check:"
echo "    1. Gateway running: ps aux | grep 'python.*run.py'"
echo "    2. Certificate valid: openssl x509 -in $CERT_FILE -text"
echo "    3. Network path: ping $GATEWAY_IP"
echo "    4. Port open: netstat -tlnp | grep 5443"
echo ""
