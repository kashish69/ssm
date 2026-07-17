#!/usr/bin/env python3
"""
RPi WiFi-QR Provisioner
------------------------
Continuously captures frames from the Pi Camera Module, scans them for a QR
code encoding WiFi credentials, connects to that network via NetworkManager
(nmcli), verifies internet connectivity by reaching Google, and logs the
outcome. Runs indefinitely — if a new QR code (new SSID/password) is shown
to the camera later, it repeats the whole connect + check + log cycle.

Tested against: Raspberry Pi OS Bookworm (NetworkManager is the default
network backend on Bookworm and later).

--------------------------------------------------------------------------
Install dependencies:

    sudo apt update
    sudo apt install -y python3-picamera2 python3-opencv python3-pyzbar \
                         network-manager libzbar0
    pip3 install requests --break-system-packages

nmcli needs privileges to add/activate connections. Either run this script
with sudo, or add your user to the netdev group / configure polkit rules
to allow nmcli without a password prompt.

--------------------------------------------------------------------------
Live preview:

If a display is available, a live camera preview window is shown while the
script runs (in addition to the QR scanning/logging, which keeps working in
the background). Detection order:

    - Desktop session present (DISPLAY or WAYLAND_DISPLAY env var set)
      -> Qt-based preview window (works locally or over VNC/X11 forwarding).
    - No desktop session, but running on the Pi's console with an HDMI
      monitor attached -> direct-to-screen DRM preview (no window manager
      needed).
    - Neither available (e.g. plain SSH session, no monitor) -> runs
      headless, no preview, everything else unaffected.

Qt preview needs: sudo apt install -y python3-pyqt5 python3-opengl

--------------------------------------------------------------------------
Supported QR code contents (checked in this order):

    1. Standard WiFi QR format (what most phones/routers generate):
         WIFI:T:WPA;S:MySSID;P:MyPassword;;

    2. JSON:
         {"ssid": "MySSID", "password": "MyPassword"}

    3. Generic key:value pairs (custom QR codes), e.g.:
         ssid:MySSID;pass:MyPassword

    4. CSV shorthand:
         MySSID,MyPassword

Run:
    python3 wifi_qr_provision.py
Stop:
    Ctrl+C
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from picamera2 import Picamera2, Preview
from pyzbar.pyzbar import decode as qr_decode

# ---------------------------------------------------------------- settings
LOG_FILE = Path("/home/pi/wifi_provision.log")   # change to a writable path
CAPTURE_INTERVAL = 1.5          # seconds between frame grabs
CONNECT_TIMEOUT = 25            # seconds to wait for nmcli to connect
GOOGLE_CHECK_URL = "https://www.google.com/generate_204"
GOOGLE_CHECK_TIMEOUT = 6
RETRY_COOLDOWN_ON_FAIL = 10     # seconds to wait after a failed attempt
RESCAN_TIMEOUT = 10             # seconds to wait for `nmcli wifi rescan` to return
RESCAN_SETTLE_TIME = 3          # seconds to let the rescan populate before connecting
# ---------------------------------------------------------------------------

logger = logging.getLogger("wifi_qr_provision")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)


def parse_wifi_qr(data: str):
    """Try the known QR formats and return (ssid, password) or None."""
    data = data.strip()

    # 1) Standard WIFI: QR format, e.g.:
    #      WIFI:T:WPA;S:MySSID;P:MyPassword;;
    #      WIFI:S:MySSID;T:WPA;H:false;;   (field order varies by generator)
    # Fields are matched directly with regex rather than split-by-";" so
    # field order doesn't matter and a malformed field (e.g. a missing
    # colon) can't corrupt neighbouring fields.
    if data.upper().startswith("WIFI:"):
        body = data[len("WIFI:"):]

        def _field(letter):
            # letter: (unescaped ';' or end of string), tolerating any
            # other single-char field prefix in between.
            m = re.search(rf'(?:^|;){letter}:((?:\\.|[^;\\])*)', body)
            if not m:
                return None
            # Unescape backslash-escaped delimiters (\; \, \: \\) per spec.
            return re.sub(r'\\(.)', r'\1', m.group(1))

        ssid = _field('S')
        if ssid:
            pwd = _field('P')
            return ssid, pwd or ""

    # 2) JSON
    try:
        obj = json.loads(data)
        if isinstance(obj, dict):
            obj_lc = {str(k).strip().lower(): v for k, v in obj.items()}
            ssid = obj_lc.get("ssid")
            if ssid:
                pwd = obj_lc.get("password") or obj_lc.get("pass") or obj_lc.get("pwd") or ""
                return ssid, pwd
    except (json.JSONDecodeError, TypeError):
        pass

    # 3) Generic "key:value;key:value" pairs, e.g.:
    #      ssid:Embedded;pass:Intello@Embed
    # Key names are matched case-insensitively; "pass"/"password"/"pwd" are
    # all accepted for the password field. Values are only split on the
    # FIRST colon in each ";"-separated chunk, so a password containing "@"
    # or other symbols (but not ";") passes through untouched.
    if ":" in data:
        kv = {}
        for part in data.split(";"):
            part = part.strip()
            if not part or ":" not in part:
                continue
            key, _, val = part.partition(":")
            kv[key.strip().lower()] = val.strip()
        ssid = kv.get("ssid")
        if ssid:
            pwd = kv.get("pass") or kv.get("password") or kv.get("pwd") or ""
            return ssid, pwd

    # 4) CSV "ssid,password"
    if "," in data:
        parts = [p.strip() for p in data.split(",", 1)]
        if len(parts) == 2 and parts[0]:
            return parts[0], parts[1]

    return None


def rescan_wifi() -> bool:
    """
    Force NetworkManager to refresh its WiFi scan list before connecting.
    A network that just appeared (e.g. a phone hotspot switched on right
    before showing the QR code) isn't always in NM's cached scan list yet,
    which makes `nmcli device wifi connect` fail with "no network with
    SSID found" even though the AP is actually broadcasting. Returns True
    if the rescan command itself succeeded (not a guarantee the target
    SSID was found — that's still checked by connect_wifi()).
    """
    logger.info("Rescanning for WiFi networks before connecting...")
    try:
        result = subprocess.run(
            ["sudo", "nmcli", "device", "wifi", "rescan"],
            capture_output=True, text=True, timeout=RESCAN_TIMEOUT
        )
        if result.returncode != 0:
            # NetworkManager rate-limits rescans (roughly one per ~30s) and
            # returns non-zero if one is already in progress/too recent.
            # That's not fatal — we just proceed with whatever is cached.
            logger.warning(f"WiFi rescan skipped/failed: {result.stderr.strip()}")
            return False
        time.sleep(RESCAN_SETTLE_TIME)  # give the scan a moment to populate
        return True
    except subprocess.TimeoutExpired:
        logger.warning("WiFi rescan timed out; proceeding with existing scan cache.")
        return False
    except Exception as e:
        logger.warning(f"WiFi rescan failed unexpectedly: {e}")
        return False


def connect_wifi(ssid: str, password: str) -> bool:
    """Use nmcli to (re)connect to a WiFi network. Returns True on success."""
    logger.info(f"Attempting to connect to SSID '{ssid}'...")
    try:
        # Drop any stale profile with the same name so nmcli doesn't reuse
        # old/wrong credentials for this SSID.
        subprocess.run(
            ["sudo", "nmcli", "connection", "delete", ssid],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        cmd = ["sudo", "nmcli", "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CONNECT_TIMEOUT
        )
        if result.returncode == 0:
            logger.info(f"nmcli reports success connecting to '{ssid}'.")
            return True
        logger.error(f"nmcli failed for '{ssid}': {result.stderr.strip()}")
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"Connection attempt to '{ssid}' timed out.")
        return False
    except Exception as e:
        logger.error(f"Unexpected error connecting to '{ssid}': {e}")
        return False


def check_internet() -> bool:
    """Check whether Google is reachable."""
    try:
        resp = requests.get(GOOGLE_CHECK_URL, timeout=GOOGLE_CHECK_TIMEOUT)
        ok = resp.status_code in (200, 204)
        logger.info(f"Google reachability check: {'OK' if ok else 'FAILED'} "
                    f"(status={resp.status_code})")
        return ok
    except requests.RequestException as e:
        logger.error(f"Google reachability check FAILED: {e}")
        return False


def start_preview_if_available(picam2: Picamera2) -> bool:
    """
    Show a live preview window if a display is available. Must be called
    after picam2.configure() and before picam2.start(). Returns True if a
    preview was started, False if running headless.
    """
    has_desktop = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    if has_desktop:
        try:
            picam2.start_preview(Preview.QTGL)
            logger.info("Live preview started (QTGL, desktop display detected).")
            return True
        except Exception as e:
            logger.warning(f"QTGL preview failed ({e}); trying QT fallback...")
            try:
                picam2.start_preview(Preview.QT)
                logger.info("Live preview started (QT fallback).")
                return True
            except Exception as e2:
                logger.warning(f"QT preview also failed ({e2}). Running headless.")
    """else:
        # No desktop session (e.g. plain console/SSH). If there's an HDMI
        # monitor attached, DRM can render straight to it without a window
        # manager. If there's genuinely no display, this will fail too.
        try:
            picam2.start_preview(Preview.DRM)
            logger.info("Live preview started (DRM, direct to HDMI).")
            return True
        except Exception as e:
            logger.info(f"No display available for preview ({e}). Running headless.")
    """

    picam2.start_preview(Preview.NULL)
    return False


def main():
    logger.info("Starting WiFi QR provisioning service.")

    picam2 = Picamera2()
    config = picam2.create_preview_configuration(main={"size": (1280, 720)})
    picam2.configure(config)
    preview_active = start_preview_if_available(picam2)
    picam2.start()
    time.sleep(2)  # let sensor warm up / auto-exposure settle

    last_ssid_tried = None

    try:
        while True:
            frame = picam2.capture_array()
            decoded_objects = qr_decode(frame)

            if not decoded_objects:
                time.sleep(CAPTURE_INTERVAL)
                continue

            for obj in decoded_objects:
                raw = obj.data.decode("utf-8", errors="ignore")
                parsed = parse_wifi_qr(raw)
                if not parsed:
                    logger.warning(f"QR detected but not a recognised WiFi "
                                    f"format: {raw[:80]!r}")
                    continue

                ssid, password = parsed

                # Skip re-processing the same QR back-to-back; a failed
                # attempt clears this so the same SSID can be retried.
                if ssid == last_ssid_tried:
                    continue

                logger.info(f"New QR code detected -> SSID='{ssid}'")
                last_ssid_tried = ssid

                rescan_wifi()

                if connect_wifi(ssid, password):
                    time.sleep(3)  # allow DHCP/routing to settle
                    if check_internet():
                        logger.info(f"SUCCESS: '{ssid}' connected and "
                                     f"internet is reachable.")
                    else:
                        logger.warning(f"'{ssid}' connected but internet "
                                         f"check FAILED.")
                else:
                    logger.error(f"FAILED to connect to '{ssid}'.")
                    last_ssid_tried = None
                    time.sleep(RETRY_COOLDOWN_ON_FAIL)

            time.sleep(CAPTURE_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Stopped by user (KeyboardInterrupt).")
    finally:
        if preview_active:
            picam2.stop_preview()
        picam2.stop()
        logger.info("Camera stopped. Exiting.")


if __name__ == "__main__":
    main()
