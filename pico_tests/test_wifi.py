"""
test_wifi.py — Standalone Wi-Fi connectivity test for Raspberry Pi Pico 2 W.

Scans for visible networks, connects to the configured SSID, and prints
the assigned IP address.  Run this independently to verify Wi-Fi works
before running the full SOMNI-Guard firmware.

Usage:
    mpremote connect /dev/cu.usbmodem1101 run pico_tests/test_wifi.py

Edit SSID and PASSWORD below to match your network.
"""

import network
import time

# ── Configure these ──────────────────────────────────────────────────────────
SSID      = "SomniGuard_Net"    # change to your Wi-Fi network name
PASSWORD  = "jGzt4ATjBYysSmZn"  # change to your Wi-Fi password
TIMEOUT_S = 300                  # seconds to wait for connection
# ─────────────────────────────────────────────────────────────────────────────

# Keep the hardware WDT alive if main.py left one running from a previous boot.
# The WDT cannot be disabled once started; we just feed it to prevent resets.
_wdt = None
try:
    from machine import WDT
    _wdt = WDT(timeout=8000)
    print("[WIFI TEST] WDT detected from previous run — will feed it during test.")
except Exception:
    pass


def _feed():
    if _wdt is not None:
        _wdt.feed()


# CYW43 status code descriptions
_STATUS = {
    0:    "idle (link down)",
    1:    "connecting",
    2:    "wrong password",
    3:    "no AP found",
    4:    "connect failed",
    1010: "got IP (connected)",
    -1:   "connection failed",
    -2:   "no AP found",
    -3:   "wrong password (bad auth)",
}


def scan(wlan):
    print("\n[WIFI TEST] Scanning for networks…")
    _feed()
    try:
        nets = wlan.scan()
        _feed()
        if not nets:
            print("[WIFI TEST] No networks found.")
            return []
        found = []
        for n in nets:
            ssid = n[0].decode("utf-8") if isinstance(n[0], bytes) else n[0]
            rssi = n[3]
            found.append(ssid)
            marker = " ◄ TARGET" if ssid == SSID else ""
            print("  {:.<40} {:>4} dBm{}".format(ssid, rssi, marker))
        return found
    except Exception as e:
        print("[WIFI TEST] Scan error: {}".format(e))
        return []


def connect(wlan):
    visible = scan(wlan)

    if SSID not in visible:
        print("\n[WIFI TEST] '{}' not found in scan.".format(SSID))
        print("[WIFI TEST] Hint: make sure the Pi 5 gateway is running and the hotspot is up.")
        return False

    print("\n[WIFI TEST] Connecting to '{}'…".format(SSID))
    wlan.connect(SSID, PASSWORD)

    deadline = time.time() + TIMEOUT_S
    last_status = None
    while not wlan.isconnected():
        _feed()
        status = wlan.status()

        # Fail fast on definitive error codes — no point waiting out the timeout
        if status in (-3, -2, -1, 2, 3, 4):
            print()
            print("[WIFI TEST] Connection failed immediately.")
            _explain_status(status)
            return False

        if status != last_status:
            print("\n[WIFI TEST] status → {} ({})".format(
                status, _STATUS.get(status, "unknown")))
            last_status = status

        if time.time() > deadline:
            print()
            print("[WIFI TEST] Timed out after {}s.".format(TIMEOUT_S))
            _explain_status(status)
            return False

        print("  still waiting… {}s left   ".format(
            int(deadline - time.time())), end="\r")
        time.sleep(1)

    print()
    ip, mask, gw, dns = wlan.ifconfig()
    print("\n[WIFI TEST] Connected!")
    print("  IP Address : {}".format(ip))
    print("  Subnet mask: {}".format(mask))
    print("  Gateway    : {}".format(gw))
    print("  DNS        : {}".format(dns))
    try:
        print("  Signal     : {} dBm".format(wlan.status("rssi")))
    except Exception:
        pass
    return True


def _explain_status(status):
    msg = _STATUS.get(status, "unknown ({})".format(status))
    print("[WIFI TEST] Final status: {} — {}".format(status, msg))
    if status in (2, -3):
        print("[WIFI TEST] >>> WRONG PASSWORD <<<")
        print("[WIFI TEST] On your Pi 5, run:")
        print("[WIFI TEST]   cat somniguard_gateway/hotspot_credentials.json")
        print("[WIFI TEST] Copy the password into WIFI_PASSWORD in somniguard_pico/config.py")
    elif status in (3, -2):
        print("[WIFI TEST] >>> AP NOT FOUND <<<")
        print("[WIFI TEST] The AP disappeared mid-connect. Check the Pi 5 hotspot is still up.")
    elif status == 0:
        print("[WIFI TEST] >>> CONNECTION DROPPED (status=0) <<<")
        print("[WIFI TEST] This usually means wrong password on CYW43.")
        print("[WIFI TEST] On your Pi 5, run:")
        print("[WIFI TEST]   cat somniguard_gateway/hotspot_credentials.json")
        print("[WIFI TEST] Copy the password into WIFI_PASSWORD in somniguard_pico/config.py")


def main():
    print("=" * 50)
    print("  SOMNI-Guard Wi-Fi Test")
    print("  Target SSID : {}".format(SSID))
    print("  Timeout     : {}s".format(TIMEOUT_S))
    print("=" * 50)

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    _feed()

    if wlan.isconnected():
        ip = wlan.ifconfig()[0]
        print("[WIFI TEST] Already connected. IP: {}".format(ip))
        print("[WIFI TEST] PASSED — Wi-Fi is working.")
        return

    ok = connect(wlan)

    print()
    if ok:
        print("[WIFI TEST] PASSED — Wi-Fi is working.")
    else:
        print("[WIFI TEST] FAILED — not connected.")


main()
