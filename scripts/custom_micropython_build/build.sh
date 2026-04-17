#!/bin/bash
# =============================================================================
# SOMNI-Guard — Custom MicroPython Build Script (macOS)
# Builds a Pico 2W firmware with USB completely disabled.
#
# RESULT: build/SOMNI_GUARD_PICO2W/firmware.uf2
#   Flash it by holding BOOTSEL while plugging in USB, then copy the UF2
#   to the Pico drive that appears.
#
# RECOVERY: BOOTSEL mode (ROM) is unaffected — you can always reflash.
# =============================================================================

set -e   # exit on any error

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BOARD_FILES_DIR="$SCRIPT_DIR"
MP_DIR="$SCRIPT_DIR/micropython"
BOARD_NAME="SOMNI_GUARD_PICO2W"
BOARD_DEST="$MP_DIR/ports/rp2/boards/$BOARD_NAME"

echo "=============================================="
echo " SOMNI-Guard Custom MicroPython Build"
echo " Target board : $BOARD_NAME"
echo " MicroPython  : $MP_DIR"
echo "=============================================="

# ── Step 1: Check / install dependencies ──────────────────────────────────
echo ""
echo "[1/6] Checking dependencies..."

if ! command -v cmake &>/dev/null; then
    echo "  Installing cmake via Homebrew..."
    brew install cmake
fi

if ! command -v arm-none-eabi-gcc &>/dev/null; then
    echo "  ARM toolchain not found."
    echo "  Installing arm-none-eabi-gcc via Homebrew..."
    brew install arm-none-eabi-gcc
    if ! command -v arm-none-eabi-gcc &>/dev/null; then
        echo ""
        echo "  Homebrew install failed or PATH not updated."
        echo "  Download manually from:"
        echo "    https://developer.arm.com/downloads/-/arm-gnu-toolchain-downloads"
        echo "  Then re-run this script."
        exit 1
    fi
fi

echo "  cmake  : $(cmake --version | head -1)"
echo "  arm-gcc: $(arm-none-eabi-gcc --version | head -1)"
echo "  python : $(python3 --version)"

# ── Step 2: Clone MicroPython ─────────────────────────────────────────────
echo ""
echo "[2/6] Cloning MicroPython (latest stable)..."
if [ -d "$MP_DIR" ]; then
    echo "  Already exists at $MP_DIR — skipping clone."
else
    cd "$SCRIPT_DIR"
    git clone --depth 1 https://github.com/micropython/micropython.git
    echo "  Cloned OK."
fi

cd "$MP_DIR"

# ── Step 3: Initialize submodules ─────────────────────────────────────────
echo ""
echo "[3/6] Initialising submodules (Pico SDK, TinyUSB, mbedtls)..."
git submodule update --init lib/pico-sdk lib/tinyusb lib/mbedtls lib/btstack lib/cyw43-driver lib/lwip lib/micropython-lib
cd lib/pico-sdk && git submodule update --init && cd "$MP_DIR"
echo "  Submodules OK."

# ── Step 4: Build mpy-cross ───────────────────────────────────────────────
echo ""
echo "[4/6] Building mpy-cross (MicroPython cross-compiler)..."
make -C mpy-cross -j$(sysctl -n hw.logicalcpu)
echo "  mpy-cross OK."

# ── Step 5: Install custom board config ───────────────────────────────────
echo ""
echo "[5/6] Installing custom board config: $BOARD_NAME..."
mkdir -p "$BOARD_DEST"
cp "$BOARD_FILES_DIR/mpconfigboard.h"     "$BOARD_DEST/mpconfigboard.h"
cp "$BOARD_FILES_DIR/mpconfigboard.cmake" "$BOARD_DEST/mpconfigboard.cmake"

# Copy pin definitions from the official Pico 2 W board
cp "$MP_DIR/ports/rp2/boards/RPI_PICO2_W/pins.csv" "$BOARD_DEST/pins.csv" 2>/dev/null || true

echo "  Board config installed at $BOARD_DEST"
echo "  Files:"
ls "$BOARD_DEST"

# ── Step 6: Build firmware ────────────────────────────────────────────────
echo ""
echo "[6/6] Building firmware (this takes ~5 minutes)..."
cd "$MP_DIR/ports/rp2"

# Clean any previous build for this board
rm -rf "build-$BOARD_NAME"

make -j$(sysctl -n hw.logicalcpu) BOARD=$BOARD_NAME

UF2="$MP_DIR/ports/rp2/build-$BOARD_NAME/firmware.uf2"
if [ -f "$UF2" ]; then
    # Copy UF2 to project root for easy access
    cp "$UF2" "$PROJECT_ROOT/somni_guard_firmware.uf2"
    echo ""
    echo "=============================================="
    echo " BUILD COMPLETE"
    echo " UF2: $PROJECT_ROOT/somni_guard_firmware.uf2"
    echo "=============================================="
    echo ""
    echo "FLASH INSTRUCTIONS:"
    echo "  1. Hold BOOTSEL while plugging the Pico 2W into USB."
    echo "  2. A drive named 'RP2350' appears on your Mac."
    echo "  3. Copy somni_guard_firmware.uf2 to that drive."
    echo "  4. The Pico reboots automatically — no USB access after that."
    echo ""
    echo "THEN deploy your encrypted files:"
    echo "  (Pico is now running custom firmware but USB CDC is gone.)"
    echo "  You must deploy files BEFORE flashing this firmware, OR"
    echo "  flash standard MicroPython first, deploy files, then flash this."
    echo ""
    echo "RECOVERY:"
    echo "  Hold BOOTSEL on power-on → ROM bootloader → reflash any UF2."
else
    echo ""
    echo "ERROR: firmware.uf2 not found — build may have failed."
    echo "Check the output above for errors."
    exit 1
fi
