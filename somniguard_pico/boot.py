"""
boot.py — SOMNI-Guard secure boot for Raspberry Pi Pico 2W (RP2350).

Runs before main.py on every power-on or reset.

SECURITY LAYERS (best-effort on stock MicroPython)
---------------------------------------------------
1. stdin blocked   — mpremote / Thonny rely on the USB-CDC REPL to send
                     commands.  Replacing sys.stdin with a null reader
                     makes the raw-REPL handshake hang so tools cannot
                     execute arbitrary code or read files.
2. Filesystem RO   — The LittleFS partition is remounted read-only so
                     any tool that does get past layer 1 cannot overwrite
                     or inject files.
3. storage API     — If a CircuitPython-compatible build is present,
                     storage.disable_usb_drive() hides the drive entirely.

NOTE ON STOCK MICROPYTHON
--------------------------
Standard MicroPython for RP2350 cannot remove the USB-CDC interface at
runtime — that requires a custom build (MICROPY_HW_USB_MSC=0) or the
RP2350 OTP fuses.  For this educational prototype the AES-256-CBC
encryption is the primary security layer; USB lockdown is a secondary
deterrent.

ESCAPE HATCHES
--------------
1. ``maintenance.flag`` — drop a file by that name into the device
   filesystem (over mpremote, BEFORE the device is locked) to skip
   lockdown on the next boot.  Remove and reset to re-arm lockdown.
2. **Physical BOOTSEL + power-cycle** — hold the BOOTSEL button while
   plugging in USB.  This enters the RP2350 *ROM* mass-storage bootloader
   (handled by hardware, before MicroPython runs at all) and is the
   ultimate recovery path: drag any MicroPython UF2 onto the drive to
   reflash the device.  Wipes the filesystem.

We deliberately do NOT use ``rp2.bootsel_button()`` for a soft escape
hatch — see micropython#16908: on the Pico 2 W (RP2350) the function
always returns 1, so any boot-time call would falsely report "BOOTSEL
held" on every cold boot and the lockdown would never apply.  The
physical BOOTSEL+power-cycle path is hardware and is unaffected.

ACTIVATING LOCKDOWN
-------------------
After deploying all encrypted files, run ONCE from the REPL:

    mpremote connect /dev/cu.usbmodem2101 exec \
        "from boot import lock_usb; lock_usb()"

Then reboot.  From that point on boot.py will apply the lockdown.

Educational prototype — not a clinically approved device.
"""

import os
import sys

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_FLAG             = "usb_locked.flag"
_MAINTENANCE_FLAG = "maintenance.flag"
_LOG              = "[SOMNI][BOOT]"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flag_exists():
    try:
        os.stat(_FLAG)
        return True
    except OSError:
        return False


def _maintenance_requested():
    """Return True if a 'maintenance.flag' file is present on the filesystem.

    Replaces the previous ``rp2.bootsel_button()`` check, which is broken
    on the Pico 2 W (RP2350): MicroPython issue #16908 reports it always
    returns 1 regardless of button state.  Using a file flag instead is
    deterministic and works on every MicroPython port.

    To enter maintenance mode:
        mpremote connect /dev/cu.usbmodem2101 fs touch :maintenance.flag
        mpremote connect /dev/cu.usbmodem2101 reset
    To re-arm lockdown:
        mpremote connect /dev/cu.usbmodem2101 fs rm :maintenance.flag
        mpremote connect /dev/cu.usbmodem2101 reset
    """
    try:
        os.stat(_MAINTENANCE_FLAG)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Lockdown layers
# ---------------------------------------------------------------------------

def _block_repl():
    """Replace sys.stdin with a null reader.

    mpremote and Thonny both open raw-REPL mode by sending Ctrl+A over
    USB-CDC and waiting for the 'raw REPL' banner on stdout.  They then
    stream Python source code over stdin.  Replacing stdin with a null
    reader that always returns empty bytes causes the raw-REPL handshake
    to stall so no commands can be injected.
    """
    class _NullReader:
        def read(self, n=-1):       return b""
        def readinto(self, buf):
            for i in range(len(buf)):
                buf[i] = 0
            return len(buf)
        def readline(self, n=-1):   return b""
        def write(self, buf):       return len(buf)
        def flush(self):            pass
        def close(self):            pass
        def fileno(self):           return -1

    sys.stdin = _NullReader()
    print(_LOG, "REPL stdin blocked — mpremote/Thonny commands disabled.")


def _mount_readonly():
    """Remount the flash filesystem read-only.

    Even if layer 1 is bypassed this prevents any tool from overwriting
    the encrypted .enc files.
    """
    # LittleFS2 path (Pico 2W / RP2350 default)
    try:
        import uos
        uos.mount(uos.VfsLfs2(uos.Flash()), "/", readonly=True)
        print(_LOG, "Filesystem remounted read-only (LittleFS2).")
        return
    except Exception:
        pass

    # flashbdev path (some older MicroPython builds)
    try:
        import uos, flashbdev
        uos.mount(flashbdev.bdev, "/", readonly=True)
        print(_LOG, "Filesystem remounted read-only (flashbdev).")
        return
    except Exception:
        pass

    print(_LOG, "WARN: read-only remount not available on this build "
                "— encryption is still the primary protection.")


def _try_storage_api():
    """Call storage.disable_usb_drive() if available (CircuitPython builds)."""
    try:
        import storage
        storage.disable_usb_drive()
        print(_LOG, "USB drive hidden via storage API.")
    except (ImportError, AttributeError):
        pass   # not available on stock MicroPython — silent skip


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def lock_usb():
    """Create the lockdown flag so the NEXT boot applies all security layers.

    Run once from the REPL after all encrypted files are in place:

        mpremote connect /dev/cu.usbmodem2101 exec \\
            "from boot import lock_usb; lock_usb()"

    Reboot after running this command.
    To undo: hold BOOTSEL on power-on to reach the ROM bootloader, then
    delete usb_locked.flag from the Pico filesystem.
    """
    try:
        with open(_FLAG, "w") as f:
            f.write("locked")
        print(_LOG, "Lockdown flag written.")
        print(_LOG, ">>> Reboot now to apply USB lockdown. <<<")
        print(_LOG, "    Soft bypass:  drop a 'maintenance.flag' file with mpremote.")
        print(_LOG, "    Hard recovery: hold the physical BOOTSEL button while")
        print(_LOG, "                   plugging in USB → ROM mass-storage mode →")
        print(_LOG, "                   drag a fresh MicroPython UF2 to wipe.")
    except OSError as exc:
        print(_LOG, "ERROR writing flag: {}".format(exc))


def unlock_usb():
    """Remove the lockdown flag (USB re-enabled after next reboot)."""
    try:
        os.remove(_FLAG)
        print(_LOG, "Flag removed — USB unlocked after reboot.")
    except OSError:
        print(_LOG, "No flag found — already unlocked.")


def status():
    """Print current lockdown state."""
    locked = _flag_exists()
    print(_LOG, "USB lockdown: {}".format("ACTIVE" if locked else "INACTIVE"))


# ---------------------------------------------------------------------------
# Boot-time execution
# ---------------------------------------------------------------------------

print(_LOG, "=" * 44)
print(_LOG, "SOMNI-Guard v0.4 — Secure Boot")
print(_LOG, "=" * 44)

if _maintenance_requested():
    # File-flag soft bypass.  Replaces the broken BOOTSEL check
    # (rp2.bootsel_button() always returns 1 on RP2350 — see #16908).
    print(_LOG, "maintenance.flag present — USB lockdown skipped.")
    print(_LOG, "Remove with:  mpremote fs rm :maintenance.flag  &&  mpremote reset")

elif _flag_exists():
    print(_LOG, "Lockdown active — applying security layers.")
    _try_storage_api()   # layer 1 (CircuitPython only, no-op otherwise)
    _mount_readonly()    # layer 2 (prevent file writes)
    _block_repl()        # layer 3 (block mpremote / Thonny REPL)
    print(_LOG, "Lockdown applied.")
    print(_LOG, "Hard recovery only:  physical BOOTSEL + power-cycle → reflash.")

else:
    print(_LOG, "Setup mode — USB fully open.")
    print(_LOG, "When ready, lock the device:")
    print(_LOG, '  mpremote exec "from boot import lock_usb; lock_usb()"')
    print(_LOG, "Then reboot.")

print(_LOG, "=" * 44)
