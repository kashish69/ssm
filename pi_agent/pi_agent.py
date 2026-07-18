#!/usr/bin/env python3
"""
RPi remote-capture agent
-------------------------
Runs three concerns in one process:

  1. WiFi auto-connect loop: every WIFI_SCAN_INTERVAL seconds, checks nmcli
     for the target SSID and connects if not already on it. With no UI-pushed
     override active, the target is drawn from WIFI_CANDIDATES — a priority-
     ordered list of known networks — trying the next entry after
     WIFI_MAX_ATTEMPTS failures on the current one (falls back to a single
     WIFI_SSID/WIFI_PASSWORD network if WIFI_CANDIDATES isn't set). The
     target is overridden at runtime (no restart needed) if a wifi_config
     command arrives over MQTT and gets written to WIFI_OVERRIDE_FILE.
     A pushed network is only a candidate until it actually connects: on
     success it's reported back over MQTT and remembered as last_good; after
     WIFI_MAX_ATTEMPTS failures the agent reports the failure and reverts to
     last_good, so bad credentials from the UI can't strand a remote device.

  2. MQTT client: persistent connection to the broker with a Last Will
     (offline) + retained "online" status on connect, subscribed to this
     device's capture and wifi-config command topics.

  3. Capture worker: on a capture command, takes a still with Picamera2 and
     uploads it to the backend over HTTPS. Reports failures back over MQTT
     rather than leaving the backend to wait out its full timeout.

Patterns reused from wifi_qr_provision.py: logging setup (RotatingFileHandler
+ same formatter), Picamera2 configure/start/warm-up sequence, the
requests.get-based internet reachability check, ALL_CAPS settings block
convention (here sourced from env vars, since this script talks to the
internet with credentials, unlike the WiFi-QR provisioner).

Install dependencies:

    sudo apt update
    sudo apt install -y python3-picamera2 network-manager
    pip3 install requests paho-mqtt --break-system-packages

Create the config dir this user can write (WIFI_OVERRIDE_FILE lives here, and
it's where systemd's EnvironmentFile is kept) — without it, wifi_config
commands fail with PermissionError:

    sudo mkdir -p /etc/pi-agent && sudo chown $USER /etc/pi-agent

Config (a .env next to this script is auto-loaded for direct runs; under
systemd the EnvironmentFile provides these and takes precedence):
    MQTT_BROKER_HOST, MQTT_BROKER_PORT (default 443), MQTT_WS_PATH (default /mqtt)
    MQTT_TLS_CA_CERT (optional)
    DEVICE_ID, DEVICE_API_KEY
    BACKEND_UPLOAD_URL
    WIFI_SSID, WIFI_PASSWORD (single default network)
    WIFI_CANDIDATES (optional; "ssid:pw;ssid:pw;..." priority list tried when
        no UI-pushed override is active — overrides WIFI_SSID/WIFI_PASSWORD
        if set; falls back to them as a single-entry list if not)
    WIFI_OVERRIDE_FILE (default /etc/pi-agent/wifi_override.json)
    WIFI_SCAN_INTERVAL (default 2 seconds)
    WIFI_MAX_ATTEMPTS (default 3, before reverting to the last known-good SSID,
        or advancing to the next WIFI_CANDIDATES entry)
    LOG_FILE (default /home/pi/pi_agent.log)

Run:
    python3 pi_agent.py
Stop:
    Ctrl+C (or SIGTERM, e.g. via systemd)
"""

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import paho.mqtt.client as mqtt
import requests
from picamera2 import Picamera2

# ---------------------------------------------------------------- settings
def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ. Existing env
    vars are NOT overwritten, so a systemd EnvironmentFile still takes
    precedence. Supports # comments, blank lines, and quoted values."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


# Load a .env sitting next to this script (for direct `python3 pi_agent.py`
# runs; under systemd the EnvironmentFile provides these instead).
_load_dotenv(Path(__file__).resolve().parent / ".env")


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"Missing required env var: {name}", file=sys.stderr)
        sys.exit(1)
    return value


MQTT_BROKER_HOST = _require_env("MQTT_BROKER_HOST")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", "443"))
MQTT_TLS_CA_CERT = os.environ.get("MQTT_TLS_CA_CERT") or None
# MQTT-over-WebSocket path (Caddy reverse-proxies this path to the broker's
# websockets listener; TLS terminated by Caddy on 443).
MQTT_WS_PATH = os.environ.get("MQTT_WS_PATH", "/mqtt")

DEVICE_ID = _require_env("DEVICE_ID")
DEVICE_API_KEY = _require_env("DEVICE_API_KEY")

BACKEND_UPLOAD_URL = _require_env("BACKEND_UPLOAD_URL")

WIFI_SSID = os.environ.get("WIFI_SSID", "")
WIFI_PASSWORD = os.environ.get("WIFI_PASSWORD", "")
# Priority-ordered list of networks to try when no UI-pushed override is
# active, e.g. "WIFI-J3SNSU:deploysitepw;test:testpw" — same "a:b;c:d" list
# convention as DEVICE_SEEDS in the backend. Falls back to a single-entry
# list built from WIFI_SSID/WIFI_PASSWORD when unset, so existing
# single-network deployments behave exactly as before.
WIFI_CANDIDATES: list[dict] = []
for _entry in os.environ.get("WIFI_CANDIDATES", "").split(";"):
    _entry = _entry.strip()
    if not _entry:
        continue
    _ssid, _, _password = _entry.partition(":")  # partition: a WiFi password may itself contain ':'
    if _ssid.strip():
        WIFI_CANDIDATES.append({"ssid": _ssid.strip(), "password": _password})
if not WIFI_CANDIDATES and WIFI_SSID:
    WIFI_CANDIDATES.append({"ssid": WIFI_SSID, "password": WIFI_PASSWORD})
WIFI_OVERRIDE_FILE = Path(os.environ.get("WIFI_OVERRIDE_FILE", "/etc/pi-agent/wifi_override.json"))
WIFI_SCAN_INTERVAL = float(os.environ.get("WIFI_SCAN_INTERVAL", "2"))
# Failed attempts on a pushed network before giving up and reverting to the
# last known-good one (a wrong SSID/password must not strand a remote device).
WIFI_MAX_ATTEMPTS = int(os.environ.get("WIFI_MAX_ATTEMPTS", "3"))
WIFI_CONNECT_TIMEOUT = 25
WIFI_RESCAN_TIMEOUT = 10
WIFI_RESCAN_SETTLE_TIME = 3

LOG_FILE = Path(os.environ.get("LOG_FILE", "/home/pi/pi_agent.log"))
UPLOAD_TIMEOUT = 20
UPLOAD_MAX_RETRIES = 2
GOOGLE_CHECK_URL = "https://www.google.com/generate_204"
GOOGLE_CHECK_TIMEOUT = 6
# ---------------------------------------------------------------------------

logger = logging.getLogger("pi_agent")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
_fh.setFormatter(_fmt)
logger.addHandler(_fh)

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

stop_event = threading.Event()
capture_queue: "queue.Queue[dict]" = queue.Queue(maxsize=2)

# Set once the MQTT client exists, so wifi_loop (started earlier, since the
# network has to come up before MQTT can connect) can report results.
_mqtt_client = None
# request_id -> last status reported ("connected"/"failed"). A dict (not a
# set) so a later "connected" can still be sent after an earlier "failed" for
# the same request — give-up no longer clears request_id, so a target that
# fails, gets given up on, then later succeeds (network reappears) needs to
# report that success too, not just its first failure.
_reported_wifi_requests: dict[str, str] = {}
# Consecutive connect failures for the current target, so we can give up on an
# unreachable network instead of chasing it forever.
_wifi_failures: dict = {"target": None, "count": 0}
# Index into WIFI_CANDIDATES for the network currently being tried when no
# override is active. Resets to 0 on every restart, so a reboot always
# retries the highest-priority candidate first.
_candidate_index = 0


# ------------------------------------------------------------------- wifi
def _read_override() -> dict:
    if not WIFI_OVERRIDE_FILE.exists():
        return {}
    try:
        data = json.loads(WIFI_OVERRIDE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read WiFi override file, falling back to defaults: {e}")
        return {}


def get_wifi_target() -> tuple[str, str, str | None, dict | None]:
    """Runtime WiFi target: (ssid, password, request_id, last_good).

    The override file (written by a wifi_config MQTT command) takes precedence
    over WIFI_CANDIDATES, no restart needed. request_id is None once a target
    has been confirmed, so we only report a result once. last_good is the
    last target we actually connected to — what we fall back to if a pushed
    network turns out to be unreachable. With no override active, the current
    entry from WIFI_CANDIDATES is tried (see _candidate_index / wifi_loop).
    """
    data = _read_override()
    last_good = data.get("last_good")
    if data.get("ssid"):
        return data["ssid"], data.get("password", ""), data.get("request_id"), last_good
    if WIFI_CANDIDATES:
        c = WIFI_CANDIDATES[_candidate_index % len(WIFI_CANDIDATES)]
        return c["ssid"], c["password"], None, last_good
    return "", "", None, last_good


def write_wifi_override(
    ssid: str,
    password: str,
    request_id: str | None = None,
    last_good: dict | None = None,
) -> bool:
    """Persist the runtime WiFi target. Returns False (and logs) instead of
    raising, so a filesystem problem can't propagate into the MQTT callback.

    request_id is stored alongside so wifi_loop — the only place that learns
    whether the connect actually succeeded — can report the result back.
    last_good is preserved across writes unless explicitly replaced, so a bad
    pushed network can never erase our way home."""
    payload = {"ssid": ssid, "password": password, "request_id": request_id}
    carried = last_good if last_good is not None else _read_override().get("last_good")
    if carried:
        payload["last_good"] = carried
    try:
        WIFI_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = WIFI_OVERRIDE_FILE.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload))
        tmp_path.replace(WIFI_OVERRIDE_FILE)
    except PermissionError:
        logger.error(
            f"No permission to write {WIFI_OVERRIDE_FILE}. Create the directory and "
            f"give this user ownership, e.g. "
            f"`sudo mkdir -p {WIFI_OVERRIDE_FILE.parent} && sudo chown $USER {WIFI_OVERRIDE_FILE.parent}`, "
            f"or point WIFI_OVERRIDE_FILE at a writable path."
        )
        return False
    except OSError as e:
        logger.error(f"Failed to write {WIFI_OVERRIDE_FILE}: {e}")
        return False
    logger.info(f"WiFi override updated -> target SSID '{ssid}'")
    return True


def _persist_last_good(ssid: str, password: str) -> None:
    """Record the last network we actually connected to, without touching
    the current top-level target. Called on every successful connection
    (env-default or UI-pushed) so _give_up_on_wifi always has an accurate
    fallback — not just whatever .env's WIFI_SSID happens to be right now,
    which can be stale if .env was edited but the agent hasn't restarted.
    Deliberately doesn't promote ssid/password to the top-level override:
    that would make an env-default connection "sticky," silently outranking
    a later legitimate .env edit until the override file is cleared."""
    data = _read_override()
    data["last_good"] = {"ssid": ssid, "password": password}
    try:
        WIFI_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = WIFI_OVERRIDE_FILE.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data))
        tmp_path.replace(WIFI_OVERRIDE_FILE)
    except OSError as e:
        logger.warning(f"Failed to persist last-good WiFi target: {e}")


def get_current_ssid() -> str | None:
    """Currently active WiFi SSID, or None if not associated to any network."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if line.startswith("yes:"):
                return line.split(":", 1)[1]
    except Exception as e:
        logger.warning(f"Failed to query current WiFi SSID: {e}")
    return None


def rescan_wifi() -> bool:
    logger.info("Rescanning for WiFi networks before connecting...")
    try:
        result = subprocess.run(
            ["sudo", "nmcli", "device", "wifi", "rescan"],
            capture_output=True, text=True, timeout=WIFI_RESCAN_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning(f"WiFi rescan skipped/failed: {result.stderr.strip()}")
            return False
        time.sleep(WIFI_RESCAN_SETTLE_TIME)
        return True
    except subprocess.TimeoutExpired:
        logger.warning("WiFi rescan timed out; proceeding with existing scan cache.")
        return False
    except Exception as e:
        logger.warning(f"WiFi rescan failed unexpectedly: {e}")
        return False


def connect_wifi(ssid: str, password: str) -> tuple[bool, str]:
    """Returns (connected, error_message). The message is reported back to the
    UI when we give up, so it needs to survive out of here."""
    logger.info(f"Attempting to connect to SSID '{ssid}'...")
    try:
        subprocess.run(
            ["sudo", "nmcli", "connection", "delete", ssid],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cmd = ["sudo", "nmcli", "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=WIFI_CONNECT_TIMEOUT)
        if result.returncode == 0:
            logger.info(f"nmcli reports success connecting to '{ssid}'.")
            return True, ""
        error = result.stderr.strip() or "nmcli failed"
        logger.error(f"nmcli failed for '{ssid}': {error}")
        return False, error
    except subprocess.TimeoutExpired:
        logger.error(f"Connection attempt to '{ssid}' timed out.")
        return False, "connection attempt timed out"
    except Exception as e:
        logger.error(f"Unexpected error connecting to '{ssid}': {e}")
        return False, str(e)


def check_internet() -> bool:
    try:
        resp = requests.get(GOOGLE_CHECK_URL, timeout=GOOGLE_CHECK_TIMEOUT)
        return resp.status_code in (200, 204)
    except requests.RequestException:
        return False


def publish_wifi_result(request_id: str | None, ssid: str, connected: bool, message: str = "") -> None:
    """Report a wifi_config outcome back to the backend so the UI can confirm
    it. Skips an exact repeat (e.g. give-up re-firing 'failed' for the same
    still-unreachable target every WIFI_MAX_ATTEMPTS cycle) and skips anything
    after a 'connected' has already been reported (terminal) — but a later
    'connected' following an earlier 'failed' IS sent, so a target that gives
    up and then later succeeds (network reappears) still gets its success
    reported. If the MQTT link is down mid-switch, paho queues the QoS-1
    message and delivers it on reconnect."""
    if not request_id or _mqtt_client is None:
        return
    new_status = "connected" if connected else "failed"
    prev_status = _reported_wifi_requests.get(request_id)
    if prev_status == new_status or prev_status == "connected":
        return
    try:
        _mqtt_client.publish(
            f"devices/{DEVICE_ID}/evt/wifi_result",
            json.dumps({
                "request_id": request_id,
                "status": new_status,
                "ssid": ssid,
                "message": message,
                "ts": time.time(),
            }),
            qos=1,
        )
        _reported_wifi_requests[request_id] = new_status
        logger.info(f"Reported WiFi result for request {request_id}: connected={connected}")
    except Exception as e:
        logger.warning(f"Failed to publish WiFi result for request {request_id}: {e}")


def _note_wifi_failure(target_ssid: str) -> int:
    if _wifi_failures["target"] != target_ssid:
        _wifi_failures["target"] = target_ssid
        _wifi_failures["count"] = 0
    _wifi_failures["count"] += 1
    return _wifi_failures["count"]


def _reset_wifi_failures() -> None:
    _wifi_failures["target"] = None
    _wifi_failures["count"] = 0


def _confirm_wifi_target(ssid: str, password: str, request_id: str | None) -> None:
    """A target we actually reached. Always persisted as last_good — the
    fallback _give_up_on_wifi reverts to — regardless of whether this was a
    UI-pushed or env-default connection. Only UI-pushed connections (those
    with a request_id) additionally get reported over MQTT and promoted to
    the top-level override target; clearing request_id there also stops us
    re-reporting on later reconnects."""
    _persist_last_good(ssid, password)
    if not request_id:
        return
    publish_wifi_result(request_id, ssid, True)
    write_wifi_override(ssid, password, None, last_good={"ssid": ssid, "password": password})
    _reset_wifi_failures()


def _give_up_on_wifi(failed_ssid: str, request_id: str, error: str, last_good: dict | None) -> None:
    """Stop chasing an unreachable network and go back to one we know works,
    so a bad SSID/password pushed from the UI can't strand the device."""
    fallback = last_good
    if not fallback and WIFI_SSID:
        fallback = {"ssid": WIFI_SSID, "password": WIFI_PASSWORD}

    if fallback and fallback.get("ssid") and fallback["ssid"] != failed_ssid:
        logger.error(
            f"Giving up on '{failed_ssid}' after {WIFI_MAX_ATTEMPTS} attempts; "
            f"reverting to last known-good '{fallback['ssid']}'."
        )
        publish_wifi_result(
            request_id, failed_ssid, False, f"{error} (reverted to {fallback['ssid']})"
        )
        write_wifi_override(fallback["ssid"], fallback.get("password", ""), None, last_good=last_good)
    else:
        # Nothing better to fall back to — report it, then keep retrying the
        # SAME target as-is. Deliberately does NOT rewrite the override file:
        # there's nothing better to write, and re-deriving the password here
        # (rather than using the one that was actually just attempted) is
        # exactly what caused a real bug — when the failing target was the
        # .env default (no override file ever written, since a pure env-
        # default target has no request_id and _confirm_wifi_target never
        # persists on success), re-reading a nonexistent file silently baked
        # an empty password into a brand-new override file, permanently
        # shadowing a correct .env password with a blank one. Not touching
        # the file when there's nothing to improve avoids this whole class of
        # bug: whatever's already on disk (or correctly absent) stays correct.
        logger.error(
            f"Giving up on '{failed_ssid}' after {WIFI_MAX_ATTEMPTS} attempts; "
            f"no known-good network to revert to, will keep retrying as-is."
        )
        publish_wifi_result(request_id, failed_ssid, False, error)
    _reset_wifi_failures()


def wifi_loop() -> None:
    while not stop_event.is_set():
        target_ssid, target_password, request_id, last_good = get_wifi_target()
        if target_ssid:
            current_ssid = get_current_ssid()
            if current_ssid == target_ssid:
                # Already on the requested network — that counts as applied.
                _confirm_wifi_target(target_ssid, target_password, request_id)
            else:
                logger.info(f"Not on target SSID (current={current_ssid!r}, target={target_ssid!r}); reconnecting.")
                rescan_wifi()
                connected, error = connect_wifi(target_ssid, target_password)
                if connected:
                    time.sleep(3)
                    check_internet()
                    _confirm_wifi_target(target_ssid, target_password, request_id)
                else:
                    # Count failures (and give up after WIFI_MAX_ATTEMPTS)
                    # regardless of request_id — not just for UI-pushed
                    # requests. request_id-gating this used to be able to trap
                    # the agent permanently: _give_up_on_wifi's "no known-good
                    # fallback" path clears request_id but re-targets the SAME
                    # failed SSID, and if failure-counting only ran when
                    # request_id was set, that first give-up call disabled all
                    # future ones — the agent would retry forever with no
                    # chance to notice a fallback becoming available later
                    # (e.g. .env's WIFI_SSID after a restart). Reporting a
                    # result over MQTT still only happens when request_id is
                    # set (see publish_wifi_result), so this is safe to run
                    # unconditionally. Don't report the first failure: a cold
                    # scan cache fails once and succeeds on retry — only give
                    # up (and revert) after the target has really proven
                    # unreachable.
                    if not _read_override().get("ssid") and len(WIFI_CANDIDATES) > 1:
                        # No UI-pushed override active and more than one
                        # candidate configured: cycle to the next one instead
                        # of the give-up/last_good-revert machinery below,
                        # which is about recovering from a bad *pushed*
                        # target and doesn't apply here — there's nothing to
                        # revert away from, just another known network to try.
                        if _note_wifi_failure(target_ssid) >= WIFI_MAX_ATTEMPTS:
                            global _candidate_index
                            _candidate_index = (_candidate_index + 1) % len(WIFI_CANDIDATES)
                            logger.error(
                                f"Giving up on candidate '{target_ssid}' after {WIFI_MAX_ATTEMPTS} "
                                f"attempts; trying next candidate."
                            )
                            _reset_wifi_failures()
                    elif _note_wifi_failure(target_ssid) >= WIFI_MAX_ATTEMPTS:
                        _give_up_on_wifi(target_ssid, request_id, error, last_good)
        stop_event.wait(WIFI_SCAN_INTERVAL)


# ------------------------------------------------------------------- mqtt
def build_mqtt_client() -> mqtt.Client:
    status_topic = f"devices/{DEVICE_ID}/status"
    cmd_capture_topic = f"devices/{DEVICE_ID}/cmd/capture"
    cmd_wifi_topic = f"devices/{DEVICE_ID}/cmd/wifi_config"

    client = mqtt.Client(client_id=DEVICE_ID, protocol=mqtt.MQTTv311, clean_session=False,
                         transport="websockets")
    client.ws_set_options(path=MQTT_WS_PATH)
    client.username_pw_set(DEVICE_ID, DEVICE_API_KEY)
    if MQTT_TLS_CA_CERT:
        client.tls_set(ca_certs=MQTT_TLS_CA_CERT)
    else:
        client.tls_set()
    client.will_set(
        status_topic,
        payload=json.dumps({"state": "offline", "ts": time.time()}),
        qos=1, retain=True,
    )

    def on_connect(c, userdata, flags, rc):
        if rc != 0:
            logger.error(f"MQTT connect failed, rc={rc}")
            return
        logger.info("MQTT connected.")
        c.subscribe(cmd_capture_topic, qos=1)
        c.subscribe(cmd_wifi_topic, qos=1)
        c.publish(status_topic, json.dumps({"state": "online", "ts": time.time()}), qos=1, retain=True)

    def on_disconnect(c, userdata, rc):
        logger.warning(f"MQTT disconnected, rc={rc}")

    def on_message(c, userdata, msg):
        # Anything raised here propagates out of paho's network loop and kills
        # the client thread, leaving the agent alive but deaf to all further
        # commands (and still retained "online"). Never let that happen.
        try:
            _dispatch_message(msg)
        except Exception:
            logger.exception(f"Error handling MQTT message on {msg.topic}")

    def _dispatch_message(msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning(f"Bad MQTT payload on {msg.topic}")
            return

        if msg.topic == cmd_capture_topic:
            try:
                capture_queue.put_nowait(payload)
            except queue.Full:
                logger.warning(f"Capture queue full, dropping request {payload.get('request_id')}")
        elif msg.topic == cmd_wifi_topic:
            ssid, password = payload.get("ssid"), payload.get("password", "")
            if ssid:
                write_wifi_override(ssid, password, payload.get("request_id"))

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


def publish_capture_error(client: mqtt.Client, request_id: str, message: str) -> None:
    client.publish(
        f"devices/{DEVICE_ID}/evt/capture_result",
        json.dumps({"request_id": request_id, "status": "error", "message": message}),
        qos=1,
    )


# ---------------------------------------------------------------- capture
def capture_still(picam2: Picamera2) -> bytes:
    import io

    stream = io.BytesIO()
    picam2.capture_file(stream, format="jpeg")
    return stream.getvalue()


def upload_image(request_id: str, jpeg_bytes: bytes) -> None:
    last_error = None
    for attempt in range(1, UPLOAD_MAX_RETRIES + 2):
        try:
            resp = requests.post(
                BACKEND_UPLOAD_URL,
                headers={"X-Device-Id": DEVICE_ID, "X-Device-Key": DEVICE_API_KEY},
                data={"request_id": request_id},
                files={"file": (f"{request_id}.jpg", jpeg_bytes, "image/jpeg")},
                timeout=UPLOAD_TIMEOUT,
            )
            resp.raise_for_status()
            logger.info(f"Uploaded capture for request_id={request_id}")
            return
        except requests.RequestException as e:
            last_error = e
            logger.warning(f"Upload attempt {attempt} failed for request_id={request_id}: {e}")
            time.sleep(2)
    raise RuntimeError(f"Upload failed after retries: {last_error}")


def capture_worker(picam2: Picamera2, mqtt_client: mqtt.Client) -> None:
    while not stop_event.is_set():
        try:
            item = capture_queue.get(timeout=1)
        except queue.Empty:
            continue

        request_id = item.get("request_id")
        if not request_id:
            continue

        try:
            logger.info(f"Capturing still for request_id={request_id}")
            jpeg_bytes = capture_still(picam2)
            upload_image(request_id, jpeg_bytes)
        except Exception as e:
            logger.error(f"Capture/upload failed for request_id={request_id}: {e}")
            publish_capture_error(mqtt_client, request_id, str(e))


# -------------------------------------------------------------------- main
def main() -> None:
    logger.info("Starting pi_agent.")

    picam2 = Picamera2()
    config = picam2.create_still_configuration(main={"size": (1920, 1080)})
    picam2.configure(config)
    picam2.start()
    time.sleep(2)  # let sensor warm up / auto-exposure settle

    def handle_signal(signum, frame):
        logger.info(f"Received signal {signum}, shutting down.")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    wifi_thread = threading.Thread(target=wifi_loop, daemon=True)
    wifi_thread.start()

    global _mqtt_client
    mqtt_client = build_mqtt_client()
    _mqtt_client = mqtt_client
    # connect() blocks and raises immediately on any DNS/network failure —
    # fatal if wifi_loop (started above) hasn't connected yet, which crashed
    # the whole agent on every cold boot / network switch before it could even
    # try. connect_async() + loop_start() defers the first attempt to paho's
    # background thread, which retries with the backoff set below instead of
    # raising — same pattern the backend's mqtt_client.py already uses.
    mqtt_client.connect_async(MQTT_BROKER_HOST, MQTT_BROKER_PORT)
    mqtt_client.loop_start()

    worker_thread = threading.Thread(target=capture_worker, args=(picam2, mqtt_client), daemon=True)
    worker_thread.start()

    try:
        stop_event.wait()
    finally:
        # A clean MQTT DISCONNECT suppresses the Last Will, so publish the
        # retained "offline" status explicitly here — otherwise a graceful
        # shutdown (SIGTERM/Ctrl+C) leaves the device stuck "online" in the UI.
        try:
            info = mqtt_client.publish(
                f"devices/{DEVICE_ID}/status",
                json.dumps({"state": "offline", "ts": time.time()}),
                qos=1, retain=True,
            )
            info.wait_for_publish(timeout=2)
        except Exception as e:
            logger.warning(f"Failed to publish offline status on shutdown: {e}")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        picam2.stop()
        logger.info("pi_agent stopped.")


if __name__ == "__main__":
    main()
