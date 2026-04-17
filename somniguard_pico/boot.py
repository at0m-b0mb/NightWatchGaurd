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

ESCAPE HATCH
------------
Hold BOOTSEL while plugging in USB → enters RP2350 ROM bootloader,
bypasses boot.py entirely, and restores full USB access.

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
_FLAG   = "usb_locked.flag"
_LOG    = "[SOMNI][BOOT]"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flag_exists():
    try:
        os.stat(_FLAG)
        return True
    except OSError:
        return False


def _bootsel_held():
    """True if the BOOTSEL button is held at power-on."""
    try:
        import rp2
        return bool(rp2.bootsel_button())
    except (ImportError, AttributeError):
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
        print(_LOG, "    Hold BOOTSEL on next power-on to bypass.")
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

if _bootsel_held():
    print(_LOG, "BOOTSEL held — maintenance mode, USB open.")
    print(_LOG, "Release BOOTSEL and reset to return to secure mode.")

elif _flag_exists():
    print(_LOG, "Lockdown active — applying security layers.")
    _try_storage_api()   # layer 1 (CircuitPython only, no-op otherwise)
    _mount_readonly()    # layer 2 (prevent file writes)
    _block_repl()        # layer 3 (block mpremote / Thonny REPL)
    print(_LOG, "Lockdown applied.")

else:
    print(_LOG, "Setup mode — USB fully open.")
    print(_LOG, "When ready, lock the device:")
    print(_LOG, '  mpremote exec "from boot import lock_usb; lock_usb()"')
    print(_LOG, "Then reboot.")

print(_LOG, "=" * 44)
