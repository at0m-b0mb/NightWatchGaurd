// SOMNI-Guard custom MicroPython board config for Raspberry Pi Pico 2 W.
// Based on the official RPI_PICO2_W config with USB completely disabled so
// that no tool (mpremote, Thonny, etc.) can access the REPL or filesystem.
//
// Recovery: BOOTSEL mode is in the RP2350 ROM and is NOT affected by this
// build — hold BOOTSEL while plugging in USB to re-flash at any time.
//
// Copy this file to:
//   micropython/ports/rp2/boards/SOMNI_GUARD_PICO2W/mpconfigboard.h

// ── Board identity ──────────────────────────────────────────────────────────
#define MICROPY_HW_BOARD_NAME                   "SOMNI-Guard Pico 2W"
#define MICROPY_HW_FLASH_STORAGE_BYTES          (PICO_FLASH_SIZE_BYTES - 1536 * 1024)

// ── Networking (required for Wi-Fi / CYW43439) ──────────────────────────────
#define MICROPY_PY_NETWORK                      1
#define MICROPY_PY_NETWORK_HOSTNAME_DEFAULT     "SOMNI-Guard"

// ── CYW43 Wi-Fi / BT driver (Pico 2 W specific) ────────────────────────────
#define CYW43_USE_SPI                           (1)
#define CYW43_LWIP                              (1)
#define CYW43_GPIO                              (1)
#define CYW43_SPI_PIO                           (1)

#define MICROPY_HW_PIN_EXT_COUNT                CYW43_WL_GPIO_COUNT
int mp_hal_is_pin_reserved(int n);
#define MICROPY_HW_PIN_RESERVED(i)              mp_hal_is_pin_reserved(i)

// ── USB — COMPLETELY DISABLED ───────────────────────────────────────────────
// Setting MICROPY_HW_ENABLE_USBDEV to 0 removes the TinyUSB stack entirely:
//   • No USB-CDC serial  → mpremote / Thonny cannot connect
//   • No USB-MSC drive   → filesystem is not visible over USB
//   • No machine.USBDevice runtime API
//
// The RP2350 ROM bootloader (BOOTSEL mode) is independent of this build and
// still presents a USB mass-storage interface for flashing — so you can
// always recover the device by holding BOOTSEL on power-on.
#define MICROPY_HW_ENABLE_USBDEV                (0)
