# SOMNI-Guard — Custom MicroPython Build Guide

> **Educational prototype — not a clinically approved device.**

This guide explains how to build the custom MicroPython firmware for the
SOMNI-Guard sensor node (Raspberry Pi Pico 2W / RP2350 chip). The custom
build removes the USB stack entirely at compile time, meaning no tool —
`mpremote`, Thonny, or any debugger — can connect to the device over USB
once this firmware is flashed.

---

## Table of Contents

1. [Why a Custom Build?](#1-why-a-custom-build)
2. [How It Differs from Stock MicroPython](#2-how-it-differs-from-stock-micropython)
3. [Directory Structure](#3-directory-structure)
4. [Prerequisites](#4-prerequisites)
5. [Build Steps](#5-build-steps)
6. [What the Build Script Does — Step by Step](#6-what-the-build-script-does--step-by-step)
7. [Board Configuration Files Explained](#7-board-configuration-files-explained)
8. [Flashing the Firmware](#8-flashing-the-firmware)
9. [Combining Firmware with Encrypted App Files](#9-combining-firmware-with-encrypted-app-files)
10. [Recovery](#10-recovery)
11. [Troubleshooting](#11-troubleshooting)
12. [Re-running After a Failed Build](#12-re-running-after-a-failed-build)

---

## 1. Why a Custom Build?

Stock MicroPython for the Pico 2W exposes three USB interfaces at runtime:

| USB interface | Risk |
|---------------|------|
| CDC serial (REPL) | `mpremote` / Thonny can read or overwrite any file |
| Mass-storage drive | Filesystem is visible as a USB drive |
| WebUSB | Remote filesystem access |

Even when `boot.py` blocks these interfaces in software (the three-layer
lockdown described in `docs/secure_boot.md`), the underlying TinyUSB stack
is still compiled into the firmware and could be re-enabled by an attacker
who reflashes the Pico with standard MicroPython.

The custom `SOMNI_GUARD_PICO2W` build sets `MICROPY_HW_ENABLE_USBDEV=0`
which removes TinyUSB at compile time. No USB device is enumerated — there
is no CDC port, no drive, and no WebUSB endpoint.

---

## 2. How It Differs from Stock MicroPython

| Feature | Stock MicroPython | SOMNI-Guard Build |
|---------|-------------------|-------------------|
| USB CDC serial (REPL) | Enabled | **Removed** |
| USB Mass-storage drive | Enabled | **Removed** |
| Wi-Fi (CYW43439) | Enabled | Enabled |
| Bluetooth (BTstack) | Enabled | Enabled |
| lwIP networking | Enabled | Enabled |
| Frozen modules | Optional | Not used (app runs from encrypted `.enc` files) |
| Board name | `RPI_PICO2_W` | `SOMNI_GUARD_PICO2W` |
| BOOTSEL recovery | Works | Works (ROM-level, unaffected by firmware) |

---

## 3. Directory Structure

```
scripts/custom_micropython_build/
├── build.sh                  ← Main build script (run this)
├── mpconfigboard.h           ← C header: board identity + USB disable flag
├── mpconfigboard.cmake       ← CMake config: networking stack options
└── micropython/              ← Created by build.sh (git clone)
    ├── mpy-cross/            ← Cross-compiler (built first)
    ├── ports/rp2/
    │   ├── boards/
    │   │   └── SOMNI_GUARD_PICO2W/   ← Installed by build.sh from above files
    │   │       ├── mpconfigboard.h
    │   │       ├── mpconfigboard.cmake
    │   │       └── pins.csv          ← Copied from RPI_PICO2_W
    │   └── build-SOMNI_GUARD_PICO2W/
    │       └── firmware.uf2          ← Final output
    └── lib/
        ├── pico-sdk/         ← Raspberry Pi Pico SDK (submodule)
        ├── tinyusb/          ← USB stack (compiled out by our config)
        ├── lwip/             ← TCP/IP stack
        ├── mbedtls/          ← TLS library
        ├── btstack/          ← Bluetooth stack
        ├── cyw43-driver/     ← Wi-Fi chip driver
        └── micropython-lib/  ← Standard library modules
```

The finished UF2 is also copied to the project root as
`somni_guard_firmware.uf2` for easy access.

---

## 4. Prerequisites

### macOS

All tools are installable via [Homebrew](https://brew.sh).

```bash
# Install cmake and the ARM cross-compiler in one command
brew install cmake arm-none-eabi-gcc

# Verify versions
cmake --version          # needs 3.20 or later
arm-none-eabi-gcc --version
python3 --version        # needs 3.10 or later
```

> **Apple Silicon (M1/M2/M3):** `arm-none-eabi-gcc` from Homebrew works
> correctly on Apple Silicon. No Rosetta 2 translation is needed.

### Linux (Raspberry Pi OS / Ubuntu)

```bash
sudo apt-get update
sudo apt-get install -y \
    cmake \
    gcc-arm-none-eabi \
    python3 \
    git \
    wget \
    unzip
```

### Windows

Use [WSL 2](https://learn.microsoft.com/en-us/windows/wsl/) with Ubuntu
and follow the Linux instructions above. Native Windows builds are not
supported by this script.

### Disk space

The full build (MicroPython source + all submodules + build artefacts)
requires approximately **3–4 GB** of free disk space.

### Time

| Stage | Approximate time |
|-------|-----------------|
| Clone MicroPython + submodules | 5–15 min (network speed) |
| Build mpy-cross | 1–2 min |
| Build firmware | 3–6 min |
| **Total (first run)** | **10–25 min** |

Subsequent runs that skip already-completed stages take 3–6 minutes.

---

## 5. Build Steps

Run the script from the `scripts/` directory:

```bash
cd /path/to/NightWatchGaurd-main/scripts
./custom_micropython_build/build.sh
```

The script is idempotent — it skips stages that are already complete
(e.g. if MicroPython was already cloned). To force a clean build:

```bash
# Remove the cloned MicroPython directory and re-run
rm -rf custom_micropython_build/micropython
./custom_micropython_build/build.sh
```

On success the script prints:

```
==============================================
 BUILD COMPLETE
 UF2: /path/to/NightWatchGaurd-main/somni_guard_firmware.uf2
==============================================
```

---

## 6. What the Build Script Does — Step by Step

### Step 1 — Dependency check

Verifies that `cmake`, `arm-none-eabi-gcc`, and `python3` are on `PATH`.
If `cmake` or `arm-none-eabi-gcc` are missing on macOS, the script
attempts to install them via Homebrew automatically.

### Step 2 — Clone MicroPython

```bash
git clone --depth 1 https://github.com/micropython/micropython.git
```

A shallow clone (`--depth 1`) of the latest stable commit is used to
minimise download size. If the `micropython/` directory already exists
this step is skipped entirely.

### Step 3 — Initialise submodules

```bash
git submodule update --init \
    lib/pico-sdk lib/tinyusb lib/mbedtls \
    lib/btstack lib/cyw43-driver lib/lwip \
    lib/micropython-lib
```

Then initialises the Pico SDK's own nested submodules:

```bash
cd lib/pico-sdk && git submodule update --init
```

`micropython-lib` is required by the rp2 port's CMake rules. Omitting it
causes a `micropython-lib not initialized` CMake error at Step 6.

### Step 4 — Build mpy-cross

`mpy-cross` is the MicroPython cross-compiler. It compiles `.py` source
files into `.mpy` bytecode on the host machine. It must be built for the
host architecture (x86-64 or arm64) before the firmware build can proceed.

```bash
make -C mpy-cross -j$(sysctl -n hw.logicalcpu)
```

### Step 5 — Install custom board config

Copies the three board configuration files into MicroPython's board
directory so CMake can find the `SOMNI_GUARD_PICO2W` target:

```
mpconfigboard.h      → micropython/ports/rp2/boards/SOMNI_GUARD_PICO2W/
mpconfigboard.cmake  → micropython/ports/rp2/boards/SOMNI_GUARD_PICO2W/
pins.csv             → copied from the official RPI_PICO2_W board
```

### Step 6 — Build firmware

```bash
cd micropython/ports/rp2
make -j$(nproc) BOARD=SOMNI_GUARD_PICO2W
```

CMake configures the build automatically. Key decisions CMake makes:

- Detects `PICO_BOARD=pico2_w` → targets the RP2350 chip
- Selects the ARM Cortex-M33 GCC toolchain
- Picks up `mpconfigboard.h` → sets `MICROPY_HW_ENABLE_USBDEV=0`
- Downloads `picotool` from source if not installed system-wide (harmless
  warning — the build succeeds regardless)

The final firmware binary is at:

```
micropython/ports/rp2/build-SOMNI_GUARD_PICO2W/firmware.uf2
```

It is also copied to the project root as `somni_guard_firmware.uf2`.

---

## 7. Board Configuration Files Explained

### `mpconfigboard.h`

This C header is included by the MicroPython build system for every
board-specific compilation unit. The critical lines for SOMNI-Guard are:

```c
// Removes TinyUSB entirely — no USB device is enumerated at runtime.
#define MICROPY_HW_ENABLE_USBDEV (0)

// Enables the CYW43439 Wi-Fi/BT chip (required for networking).
#define MICROPY_PY_NETWORK       1
#define CYW43_USE_SPI            (1)
#define CYW43_LWIP               (1)
```

`MICROPY_HW_ENABLE_USBDEV=0` is the single most important setting. It
cascades through the build system to exclude `lib/tinyusb` from compilation
entirely, not just disable it at runtime.

The flash storage size is set to leave 1.5 MB for MicroPython's LittleFS2
filesystem (where encrypted `.enc` files live):

```c
#define MICROPY_HW_FLASH_STORAGE_BYTES (PICO_FLASH_SIZE_BYTES - 1536 * 1024)
```

### `mpconfigboard.cmake`

Tells CMake which optional MicroPython modules to include:

```cmake
set(PICO_BOARD "pico2_w")          # Target the Pico 2W hardware
set(MICROPY_PY_LWIP ON)            # lwIP TCP/IP stack (needed for Wi-Fi)
set(MICROPY_PY_NETWORK_CYW43 ON)   # CYW43 Wi-Fi driver
set(MICROPY_PY_BLUETOOTH ON)       # Bluetooth support
```

Frozen modules are intentionally disabled. All application code runs from
AES-256-CBC encrypted `.enc` files on the filesystem, decrypted at runtime
by `crypto_loader.py`. Freezing modules into flash would expose them in
plaintext.

---

## 8. Flashing the Firmware

### Method A — BOOTSEL drag-and-drop (recommended)

1. Hold the **BOOTSEL** button on the Pico 2W.
2. While holding BOOTSEL, plug the USB cable into your computer.
3. Release BOOTSEL. A drive named **`RP2350`** appears on your computer.
4. Copy `somni_guard_firmware.uf2` to the `RP2350` drive.
5. The drive disappears and the Pico reboots automatically.

```bash
# macOS — copy via terminal
cp somni_guard_firmware.uf2 /Volumes/RP2350/
```

> After flashing this firmware the USB drive will **not** reappear on
> normal power-up. This is expected — USB has been removed from the
> firmware. The BOOTSEL recovery path (step 1 above) still works because
> it uses the RP2350 ROM bootloader, which is independent of the firmware.

### Method B — picotool (Linux / macOS)

```bash
# Install picotool
brew install picotool           # macOS
sudo apt-get install picotool   # Ubuntu/Debian

# Flash (Pico must be in BOOTSEL mode)
picotool load somni_guard_firmware.uf2
picotool reboot
```

---

## 9. Combining Firmware with Encrypted App Files

The bare `somni_guard_firmware.uf2` contains only the MicroPython
interpreter — no application code. Use `somni_uf2_tool.py` to produce a
single UF2 that embeds both the firmware and your encrypted `.enc` files.
Flashing this combined UF2 is the recommended one-step deployment method.

```bash
# Get your Pico's hardware UID first (while on standard MicroPython)
python3 -c "import machine; print(machine.unique_id().hex())"

# Build the combined UF2
python3 scripts/somni_uf2_tool.py \
    --uid  <your-pico-uid>         \
    --src  somniguard_pico/        \
    --firmware somni_guard_firmware.uf2 \
    --out  somni_guard_complete.uf2
```

Flash `somni_guard_complete.uf2` via BOOTSEL drag-and-drop as described
in [Section 8](#8-flashing-the-firmware).

For detailed encryption options see `docs/encrypted_firmware.md`.

---

## 10. Recovery

Because USB has been removed from the firmware, `mpremote` and Thonny
will not connect over USB after flashing. The only way to access the
device is via BOOTSEL mode.

**To recover or reflash at any time:**

1. Hold **BOOTSEL** while plugging in USB.
2. The `RP2350` drive appears — the ROM bootloader is running, not your
   firmware.
3. Drop any `.uf2` file (standard MicroPython, your custom build, etc.)
   onto the drive.

The RP2350 ROM is stored in on-chip read-only memory. It cannot be
overwritten by any firmware, including this custom build.

---

## 11. Troubleshooting

### `micropython-lib not initialized`

```
CMake Error: micropython-lib not initialized.
Run 'make BOARD=SOMNI_GUARD_PICO2W submodules'
```

The `lib/micropython-lib` submodule was not initialised. If MicroPython
is already cloned, run:

```bash
cd scripts/custom_micropython_build/micropython
git submodule update --init lib/micropython-lib
```

Then re-run the build script.

### `cp: No such file or directory` for `mpconfigboard.h`

The `BOARD_FILES_DIR` path in `build.sh` was wrong (doubled directory
name). This was fixed in the current version of the script. If you see
this error, ensure you are running the latest `build.sh`.

### `arm-none-eabi-gcc: command not found`

```bash
# macOS
brew install arm-none-eabi-gcc

# Ubuntu / Debian
sudo apt-get install gcc-arm-none-eabi
```

If Homebrew installs the compiler but the script still cannot find it,
add the Homebrew bin directory to your PATH:

```bash
echo 'export PATH="/opt/homebrew/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### `cmake: command not found`

```bash
brew install cmake          # macOS
sudo apt-get install cmake  # Ubuntu/Debian
```

Minimum required CMake version is **3.20**. Check with `cmake --version`.

### Build fails with `No space left on device`

The build directory requires ~3 GB. Run `df -h .` to check available
space and free up disk before retrying.

### `picotool` warning during CMake configure

```
CMake Warning: No installed picotool with version 2.1.1 found — building from source
```

This is a harmless warning. CMake downloads and builds `picotool` as part
of the SDK. The firmware build completes successfully. To silence it, install
`picotool` system-wide:

```bash
brew install picotool          # macOS
sudo apt-get install picotool  # Ubuntu/Debian
```

### Pico does not appear as `RP2350` drive

- Ensure you are using a **data** USB cable (not a charge-only cable).
- Hold BOOTSEL **before** plugging in USB, not after.
- Try a different USB port or cable.

### Wi-Fi does not connect after flashing

The custom firmware keeps Wi-Fi enabled. If the Pico cannot connect,
check `somniguard_pico/config.py`:

```python
WIFI_SSID     = "SomniGuard_Net"     # Must match the Pi 5 hotspot SSID
WIFI_PASSWORD = "..."                 # Must match hotspot_credentials.json
GATEWAY_HOST  = "10.42.0.1"          # Default Pi 5 hotspot IP
```

The hotspot credentials are generated and saved to
`somniguard_gateway/hotspot_credentials.json` the first time `run.py`
starts. Update `config.py` with those values, then rebuild and reflash.

---

## 12. Re-running After a Failed Build

The build script is designed to skip completed stages automatically:

| Situation | What to do |
|-----------|-----------|
| Clone already exists | Script skips clone — runs from existing directory |
| Submodules already initialised | `git submodule update --init` is idempotent |
| `mpy-cross` already built | Make detects no changes and skips |
| Firmware build failed partway | Re-run the script — CMake incremental build picks up where it stopped |
| Want a completely clean build | `rm -rf scripts/custom_micropython_build/micropython` then re-run |

To re-run only Step 6 (firmware build) manually:

```bash
cd scripts/custom_micropython_build/micropython/ports/rp2
make -j$(sysctl -n hw.logicalcpu) BOARD=SOMNI_GUARD_PICO2W
```

---

*For the full security rationale see `docs/security_controls.md` (control L0-C5).*
*For deploying encrypted application files see `docs/encrypted_firmware.md`.*
