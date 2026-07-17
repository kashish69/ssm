#!/usr/bin/env python3
"""
RPi remote-capture agent
-------------------------
Runs three concerns in one process:

  1. WiFi auto-connect loop: every WIFI_SCAN_INTERVAL seconds, checks nmcli
     for the target SSID and connects if not already on it. Target SSID/
     password default to WIFI_SSID/WIFI_PASSWORD env vars, but are
     overridden at runtime (no restart needed) if a wifi_config command
     arrives over MQTT and gets written to WIFI_OVERRIDE_FILE.

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

Config (a .env next to this script is auto-loaded for direct runs; under
systemd the EnvironmentFile provides these and takes precedence):
    MQTT_BROKER_HOST, MQTT_BROKER_PORT (default 8883), MQTT_TLS_CA_CERT (optional)
    DEVICE_ID, DEVICE_API_KEY
    BACKEND_UPLOAD_URL
    WIFI_SSID, WIFI_PASSWORD
    WIFI_OVERRIDE_FILE (default /etc/pi-agent/wifi_override.json)
    WIFI_SCAN_INTERVAL (default 2 seconds)
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
WIFI_OVERRIDE_FILE = Path(os.environ.get("WIFI_OVERRIDE_FILE", "/etc/pi-agent/wifi_override.json"))
WIFI_SCAN_INTERVAL = float(os.environ.get("WIFI_SCAN_INTERVAL", "2"))
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


# ------------------------------------------------------------------- wifi
def get_wifi_target() -> tuple[str, str]:
    """Runtime target SSID/password: override file (from a wifi_config MQTT
    command) takes precedence over the env-var defaults, no restart needed."""
    if WIFI_OVERRIDE_FILE.exists():
        try:
            data = json.loads(WIFI_OVERRIDE_FILE.read_text())
            return data["ssid"], data.get("password", "")
        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.warning(f"Failed to read WiFi override file, falling back to defaults: {e}")
    return WIFI_SSID, WIFI_PASSWORD


def write_wifi_override(ssid: str, password: str) -> None:
    WIFI_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = WIFI_OVERRIDE_FILE.with_suffix(".tmp")
    tmp_path.write_text(json.dumps({"ssid": ssid, "password": password}))
    tmp_path.replace(WIFI_OVERRIDE_FILE)
    logger.info(f"WiFi override updated -> target SSID '{ssid}'")


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


def connect_wifi(ssid: str, password: str) -> bool:
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
    try:
        resp = requests.get(GOOGLE_CHECK_URL, timeout=GOOGLE_CHECK_TIMEOUT)
        return resp.status_code in (200, 204)
    except requests.RequestException:
        return False


def wifi_loop() -> None:
    while not stop_event.is_set():
        target_ssid, target_password = get_wifi_target()
        if target_ssid:
            current_ssid = get_current_ssid()
            if current_ssid != target_ssid:
                logger.info(f"Not on target SSID (current={current_ssid!r}, target={target_ssid!r}); reconnecting.")
                rescan_wifi()
                if connect_wifi(target_ssid, target_password):
                    time.sleep(3)
                    check_internet()
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
                write_wifi_override(ssid, password)

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

    mqtt_client = build_mqtt_client()
    mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT)
    mqtt_client.loop_start()

    worker_thread = threading.Thread(target=capture_worker, args=(picam2, mqtt_client), daemon=True)
    worker_thread.start()

    try:
        stop_event.wait()
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        picam2.stop()
        logger.info("pi_agent stopped.")


if __name__ == "__main__":
    main()
