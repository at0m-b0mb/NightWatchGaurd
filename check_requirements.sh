#!/bin/bash
################################################################################
# SOMNI-Guard Requirements Checker
#
# Verifies all prerequisites before running setup scripts
# Run this first to catch missing dependencies early
#
# Usage: bash check_requirements.sh [gateway|pico]
#
# Examples:
#   bash check_requirements.sh gateway     # Check gateway requirements
#   bash check_requirements.sh pico        # Check Pico requirements
#   bash check_requirements.sh              # Check both
#
# Educational prototype — not a clinically approved device.
################################################################################

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Track results
PASS=0
WARN=0
FAIL=0

check_status() {
    if [[ $1 -eq 0 ]]; then
        echo -e "${GREEN}✓${NC} $2"
        ((PASS++))
    elif [[ $1 -eq 1 ]]; then
        echo -e "${YELLOW}⚠${NC} $2"
        ((WARN++))
    else
        echo -e "${RED}✗${NC} $2"
        ((FAIL++))
    fi
}

print_header() {
    echo ""
    echo -e "${BLUE}═══════════════════════════════════════════${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════${NC}\n"
}

# Determine what to check
CHECK_GATEWAY=false
CHECK_PICO=false

if [[ $# -eq 0 ]]; then
    CHECK_GATEWAY=true
    CHECK_PICO=true
elif [[ "$1" == "gateway" ]]; then
    CHECK_GATEWAY=true
elif [[ "$1" == "pico" ]]; then
    CHECK_PICO=true
else
    echo "Usage: $0 [gateway|pico]"
    exit 1
fi

echo -e "${BLUE}╔════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  SOMNI-Guard Requirements Checker         ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════╝${NC}"

# ============================================================================
# GATEWAY CHECKS
# ============================================================================

if [[ "$CHECK_GATEWAY" == true ]]; then
    print_header "GATEWAY REQUIREMENTS (Raspberry Pi 5)"

    # 1. Operating System
    if grep -q "Raspberry Pi 5" /proc/device-tree/model 2>/dev/null; then
        check_status 0 "Raspberry Pi 5 detected"
    else
        if grep -q "Raspberry Pi" /proc/device-tree/model 2>/dev/null; then
            check_status 1 "Raspberry Pi detected (not Pi 5)"
        else
            check_status 1 "Not detected as Raspberry Pi (might still work)"
        fi
    fi

    # 2. Operating System Version
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        if [[ "$VERSION_ID" == "12" ]] || [[ "$VERSION_ID" == "13" ]]; then
            check_status 0 "Debian/Raspberry Pi OS $VERSION_ID"
        else
            check_status 1 "Linux version: $PRETTY_NAME"
        fi
    else
        check_status 1 "Cannot determine OS version"
    fi

    # 3. Free Disk Space
    SPACE=$(df -B1 / | tail -1 | awk '{print $4}')
    if [[ $SPACE -gt 5368709120 ]]; then  # > 5 GB
        check_status 0 "Disk space: $(df -h / | tail -1 | awk '{print $4}') available"
    else
        check_status 2 "Low disk space: $(df -h / | tail -1 | awk '{print $4}') (5 GB recommended)"
    fi

    # 4. Python 3
    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
        check_status 0 "Python 3: $PYTHON_VERSION"
    else
        check_status 2 "Python 3 not found (required)"
    fi

    # 5. Python pip
    if command -v pip3 &> /dev/null; then
        PIP_VERSION=$(pip3 --version 2>&1 | awk '{print $2}')
        check_status 0 "pip3: version $PIP_VERSION"
    else
        check_status 2 "pip3 not found (required for package installation)"
    fi

    # 6. Git
    if command -v git &> /dev/null; then
        GIT_VERSION=$(git --version 2>&1 | awk '{print $3}')
        check_status 0 "git: version $GIT_VERSION"
    else
        check_status 1 "git not found (optional, only needed for updates)"
    fi

    # 7. curl
    if command -v curl &> /dev/null; then
        check_status 0 "curl: installed"
    else
        check_status 1 "curl not found (optional)"
    fi

    # 8. OpenSSL
    if command -v openssl &> /dev/null; then
        OPENSSL_VERSION=$(openssl version 2>&1 | awk '{print $2}')
        check_status 0 "OpenSSL: version $OPENSSL_VERSION"
    else
        check_status 2 "OpenSSL not found (required for TLS)"
    fi

    # 9. SQLite3
    if command -v sqlite3 &> /dev/null; then
        check_status 0 "SQLite3: installed"
    else
        check_status 1 "SQLite3 not found (optional for database backups)"
    fi

    # 10. Network connectivity
    if timeout 2 ping -c 1 8.8.8.8 > /dev/null 2>&1; then
        check_status 0 "Internet connection: available"
    else
        check_status 1 "Internet connection: not available (needed for package downloads)"
    fi

    # 11. Root access
    if [[ $EUID -eq 0 ]]; then
        check_status 0 "Running as root (required for setup)"
    else
        check_status 1 "Not running as root (run with: sudo bash check_requirements.sh)"
    fi

    # 12. UFW
    if command -v ufw &> /dev/null; then
        check_status 0 "UFW (firewall): installed"
    else
        check_status 1 "UFW not found (will be installed)"
    fi

    # 13. Network Manager
    if systemctl is-active --quiet NetworkManager 2>/dev/null; then
        check_status 0 "NetworkManager: running (for hotspot)"
    else
        check_status 1 "NetworkManager: not running (needed for Wi-Fi hotspot)"
    fi

fi

# ============================================================================
# PICO CHECKS
# ============================================================================

if [[ "$CHECK_PICO" == true ]]; then
    print_header "PICO REQUIREMENTS (Development Machine)"

    # 1. Python 3
    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
        MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
        MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

        if [[ $MAJOR -ge 3 ]] && [[ $MINOR -ge 8 ]]; then
            check_status 0 "Python 3: $PYTHON_VERSION"
        else
            check_status 2 "Python 3.8+ required (found: $PYTHON_VERSION)"
        fi
    else
        check_status 2 "Python 3 not found (required)"
    fi

    # 2. pip
    if command -v pip3 &> /dev/null; then
        check_status 0 "pip3: installed"
    else
        check_status 1 "pip3 not found (will be installed)"
    fi

    # 3. Cryptography package
    if python3 -c "import cryptography" 2>/dev/null; then
        check_status 0 "cryptography package: installed"
    else
        check_status 1 "cryptography package: not installed (will be installed)"
    fi

    # 4. Requests package
    if python3 -c "import requests" 2>/dev/null; then
        check_status 0 "requests package: installed"
    else
        check_status 1 "requests package: not installed (will be installed)"
    fi

    # 5. mpremote
    if command -v mpremote &> /dev/null; then
        MPREMOTE_VERSION=$(mpremote --version 2>&1 | awk '{print $NF}')
        check_status 0 "mpremote: version $MPREMOTE_VERSION"
    else
        check_status 1 "mpremote not found (will be installed)"
    fi

    # 6. USB device detection
    if command -v lsusb &> /dev/null; then
        if lsusb 2>/dev/null | grep -q "Pico"; then
            check_status 0 "Pico 2 W: connected via USB"
        else
            check_status 1 "Pico 2 W: not detected (check USB connection)"
        fi
    else
        check_status 1 "lsusb not found (cannot detect Pico)"
    fi

    # 7. OpenSSL
    if command -v openssl &> /dev/null; then
        check_status 0 "OpenSSL: installed (for cert verification)"
    else
        check_status 1 "OpenSSL not found (needed for cert verification)"
    fi

    # 8. Git
    if command -v git &> /dev/null; then
        check_status 0 "git: installed"
    else
        check_status 1 "git not found (optional)"
    fi

fi

# ============================================================================
# Summary
# ============================================================================

echo ""
print_header "SUMMARY"

echo -e "${GREEN}✓ Passed:${NC}  $PASS"
echo -e "${YELLOW}⚠ Warnings:${NC} $WARN"
echo -e "${RED}✗ Failed:${NC}  $FAIL"
echo ""

if [[ $FAIL -eq 0 ]]; then
    echo -e "${GREEN}All critical requirements met!${NC}"
    if [[ "$CHECK_GATEWAY" == true ]]; then
        echo ""
        echo "Ready to run:"
        echo -e "  ${BLUE}sudo bash setup_gateway.sh${NC}"
    fi
    if [[ "$CHECK_PICO" == true ]]; then
        echo ""
        echo "Ready to run:"
        echo -e "  ${BLUE}bash setup_pico.sh <gateway-ip>${NC}"
    fi
    echo ""
    exit 0
else
    echo -e "${RED}Please fix the failed items above before proceeding.${NC}"
    echo ""
    if [[ "$CHECK_GATEWAY" == true ]]; then
        echo "To install missing packages on gateway:"
        echo -e "  ${BLUE}sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv git curl openssl${NC}"
    fi
    if [[ "$CHECK_PICO" == true ]]; then
        echo "To install missing packages on development machine:"
        echo -e "  ${BLUE}pip3 install cryptography requests mpremote${NC}"
    fi
    echo ""
    exit 1
fi
