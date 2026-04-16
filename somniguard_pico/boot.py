"""
boot.py — SOMNI-Guard secure boot configuration for Raspberry Pi Pico 2W (RP2350).

MicroPython executes boot.py before main.py on every power-on or reset.
This script handles:

  1. USB mass-storage lockdown  — prevents the Pico from appearing as a USB
     drive after initial setup so adversaries cannot browse or replace files
     even with physical access.
  2. Filesystem read-only mount — additional layer against unauthorised write
     access via the USB port.
  3. BOOTSEL bypass            — if the BOOTSEL button is held at boot, USB
     access is temporarily re-enabled so the operator can perform maintenance
     (e.g. flash new firmware or update the encrypted .enc files).

USB LOCKDOWN — how it works
---------------------------
pico-ducky (CircuitPython) uses ``storage.disable_usb_drive()``.
This project uses **MicroPython**, which does not include that CircuitPython
API by default.  The approach here therefore uses two complementary layers:

  Layer 1 — "disable_usb_drive" via usb_hid / usb_cdc (if the MicroPython
            build includes the adafruit_hid / usb_cdc modules):
              import storage; storage.disable_usb_drive()

  Layer 2 — Filesystem remounted as read-only via os.mount().  Even if the
            USB mass-storage interface remains visible, all write attempts
            will be rejected by the filesystem layer, protecting the
            encrypted .enc files from being overwritten.

  NOTE: For *complete* USB mass-storage removal (no drive appears at all),
        you need one of:
          a) CircuitPython firmware (which has the storage API).
          b) A custom MicroPython build compiled with
             MICROPY_HW_USB_MSC=0.
          c) The RP2350 OTP to permanently disable the USB boot interface.
        This boot.py achieves the best possible protection with stock
        MicroPython firmware.

To re-enable USB access for maintenance:
  → Hold the BOOTSEL button while connecting USB to the computer.
  → The RP2350 will enter its ROM bootloader (BOOTSEL mode), which bypasses
    boot.py entirely — USB mass-storage access is restored.
  → You can then delete ``usb_locked.flag`` and reflash as needed.

To activate lockdown (run once after initial setup is complete):
  >>> from boot import lock_usb; lock_usb()

Educational prototype — not a clinically approved device.
"""

import os

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USB_LOCK_FLAG  = "usb_locked.flag"
_LOG            = "[SOMNI][BOOT]"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_usb_locked():
    """Return True if the USB lockdown flag file is present."""
    try:
        os.stat(_USB_LOCK_FLAG)
        return True
    except OSError:
        return False


def _bootsel_pressed():
    """Return True if the BOOTSEL button is currently held down.

    On the RP2350, rp2.bootsel_button() reads the BOOTSEL GPIO without
    disturbing the boot process.  Returns False if the rp2 module is not
    available (e.g. running on CPython for testing).
    """
    try:
        import rp2
        return rp2.bootsel_button() == 1
    except (ImportError, AttributeError):
        return False


def _try_disable_usb_drive():
    """Best-effort attempt to disable USB mass-storage.

    Tries the CircuitPython ``storage`` API first (works if the board runs
    CircuitPython or a MicroPython build that includes it).  Falls back to
    a read-only remount of the filesystem which prevents write access even
    if the drive remains visible.

    Returns:
        str: Human-readable description of what was achieved.
    """
    # Attempt 1: CircuitPython / adafruit_hid storage API
    try:
        import storage
        storage.disable_usb_drive()
        return "USB drive disabled via storage API (CircuitPython mode)."
    except (ImportError, AttributeError):
        pass

    # Attempt 2: MicroPython read-only remount
    # This mounts the LittleFS partition as read-only so USB writes are
    # rejected even if the drive is still visible.
    try:
        import uos
        uos.mount(uos.VfsLfs2(uos.Flash()), "/", readonly=True)
        return "Filesystem remounted read-only (MicroPython fallback)."
    except Exception:
        pass

    # Attempt 3: Older MicroPython VFS API
    try:
        import uos
        uos.mount(uos.VfsFat(uos.Flash()), "/", readonly=True)
        return "Filesystem remounted read-only (FAT fallback)."
    except Exception:
        pass

    return ("USB drive still visible — stock MicroPython firmware detected. "
            "Files are protected by AES-256-CBC encryption. "
            "For complete USB disable, use CircuitPython or a custom "
            "MicroPython build with MICROPY_HW_USB_MSC=0.")


# ---------------------------------------------------------------------------
# Public API (callable from REPL)
# ---------------------------------------------------------------------------

def lock_usb():
    """Activate USB mass-storage lockdown.

    Creates the ``usb_locked.flag`` file.  On the NEXT boot, boot.py will
    read this flag and disable USB drive access.  Run this command once after
    all firmware files have been encrypted and deployed:

        >>> from boot import lock_usb; lock_usb()

    To undo: hold BOOTSEL during USB connection → enter bootloader →
    delete ``usb_locked.flag`` manually.

    Returns:
        None
    """
    try:
        with open(_USB_LOCK_FLAG, "w") as f:
            f.write("1")
        print(_LOG, "USB lockdown flag created.")
        print(_LOG, "IMPORTANT: Reboot to apply USB lockdown.")
        print(_LOG, "           Hold BOOTSEL during reset to bypass.")
    except OSError as exc:
        print(_LOG, "ERROR: Could not create lockdown flag: {}".format(exc))


def unlock_usb():
    """Remove the USB lockdown flag (re-enable USB mass-storage on next boot).

    Returns:
        None
    """
    try:
        os.remove(_USB_LOCK_FLAG)
        print(_LOG, "USB lockdown flag removed. Reboot to apply.")
    except OSError:
        print(_LOG, "No USB lockdown flag found — already unlocked.")


def usb_status():
    """Print the current USB lockdown status.

    Returns:
        None
    """
    if _is_usb_locked():
        print(_LOG, "USB lockdown: ACTIVE (usb_locked.flag present).")
    else:
        print(_LOG, "USB lockdown: INACTIVE (setup/maintenance mode).")


# ---------------------------------------------------------------------------
# Boot-time execution
# ---------------------------------------------------------------------------

print(_LOG, "========================================")
print(_LOG, "SOMNI-Guard v0.4 — Secure Boot")
print(_LOG, "========================================")

if _bootsel_pressed():
    # Operator is holding BOOTSEL — allow USB access for maintenance.
    print(_LOG, "BOOTSEL held — maintenance mode: USB access enabled.")
    print(_LOG, "Release BOOTSEL and reset to return to secure mode.")

elif _is_usb_locked():
    print(_LOG, "USB lockdown flag detected — applying security policy.")
    result = _try_disable_usb_drive()
    print(_LOG, result)

else:
    print(_LOG, "Setup mode: USB access enabled.")
    print(_LOG, "Run 'from boot import lock_usb; lock_usb()' after setup.")

print(_LOG, "========================================")
