# SOMNI-Guard custom board cmake — Raspberry Pi Pico 2 W, USB disabled.
#
# Copy this file to:
#   micropython/ports/rp2/boards/SOMNI_GUARD_PICO2W/mpconfigboard.cmake

set(PICO_BOARD "pico2_w")

# Wi-Fi / networking stack (needed for gateway transport)
set(MICROPY_PY_LWIP ON)
set(MICROPY_PY_NETWORK_CYW43 ON)

# Bluetooth (keep matching upstream Pico 2 W config)
set(MICROPY_PY_BLUETOOTH ON)
set(MICROPY_BLUETOOTH_BTSTACK ON)
set(MICROPY_PY_BLUETOOTH_CYW43 ON)

# No frozen manifest needed — all app code is in encrypted .enc files
# set(MICROPY_FROZEN_MANIFEST ${MICROPY_BOARD_DIR}/manifest.py)
