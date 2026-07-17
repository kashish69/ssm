import json
import logging

import paho.mqtt.client as mqtt

from app.config import settings
from app.db import get_cursor

logger = logging.getLogger("mqtt_client")

_client: mqtt.Client | None = None


def _on_connect(client, userdata, flags, rc, properties=None):
    if rc != 0:
        logger.error(f"MQTT connect failed, rc={rc}")
        return
    logger.info("MQTT connected; subscribing to device status/event topics")
    client.subscribe("devices/+/status", qos=1)
    client.subscribe("devices/+/evt/#", qos=1)


def _on_disconnect(client, userdata, rc, properties=None):
    logger.warning(f"MQTT disconnected, rc={rc}")


def _on_message(client, userdata, msg):
    # Anything raised here propagates out of paho's network loop and kills the
    # client thread — the backend would keep serving HTTP while silently going
    # deaf to every device status and event. Never let that happen.
    try:
        _dispatch_message(msg)
    except Exception:
        logger.exception(f"Error handling MQTT message on {msg.topic}")


def _dispatch_message(msg):
    topic_parts = msg.topic.split("/")
    if len(topic_parts) < 3:
        return
    device_id = topic_parts[1]

    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(f"Bad MQTT payload on {msg.topic}")
        return

    if topic_parts[2] == "status":
        online = payload.get("state") == "online"
        with get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_status (device_id, online, last_seen, updated_at)
                VALUES (?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(device_id) DO UPDATE SET
                    online = excluded.online,
                    last_seen = datetime('now'),
                    updated_at = datetime('now')
                """,
                (device_id, 1 if online else 0),
            )

    elif topic_parts[2:4] == ["evt", "wifi_result"]:
        request_id = payload.get("request_id")
        if not request_id:
            return
        connected = payload.get("status") == "connected"
        logger.info(
            f"WiFi result from {device_id} for request {request_id}: "
            f"{'connected' if connected else 'failed'} (ssid={payload.get('ssid')!r})"
        )
        # Settle 'pending', but also let a late 'connected' correct a request we
        # already aged out to 'timeout' — the device switching networks can take
        # longer than the UI waits, and it did succeed. A stale 'failed' must not
        # clobber a settled result, hence the status filter.
        allowed = "('pending','timeout')" if connected else "('pending')"
        with get_cursor() as cur:
            cur.execute(
                f"""
                UPDATE wifi_requests
                SET status = ?, error_message = ?, completed_at = datetime('now')
                WHERE request_id = ? AND device_id = ? AND status IN {allowed}
                """,
                (
                    "connected" if connected else "failed",
                    None if connected else payload.get("message", "device failed to connect"),
                    request_id,
                    device_id,
                ),
            )

    elif topic_parts[2:4] == ["evt", "capture_result"]:
        request_id = payload.get("request_id")
        if payload.get("status") == "error" and request_id:
            with get_cursor() as cur:
                cur.execute(
                    """
                    UPDATE capture_requests
                    SET status = 'failed', error_message = ?, completed_at = datetime('now')
                    WHERE request_id = ? AND device_id = ? AND status = 'pending'
                    """,
                    (payload.get("message", "device reported error"), request_id, device_id),
                )


def start() -> None:
    global _client
    client = mqtt.Client(client_id="backend-service", protocol=mqtt.MQTTv311)
    client.username_pw_set(settings.mqtt_service_username, settings.mqtt_service_password)
    if settings.mqtt_use_tls:
        if settings.mqtt_tls_ca_cert:
            client.tls_set(ca_certs=settings.mqtt_tls_ca_cert)
        else:
            client.tls_set()
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message = _on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    # connect_async() + loop_start() lets the background network thread handle
    # the initial connection (and retries, with the backoff above) rather than
    # raising synchronously here and crashing the whole app on a transient
    # failure (e.g. mosquitto not ready yet, a restart, a network blip).
    client.connect_async(settings.mqtt_broker_host, settings.mqtt_broker_port)
    client.loop_start()
    _client = client


def stop() -> None:
    if _client is not None:
        _client.loop_stop()
        _client.disconnect()


def publish_capture_command(device_id: str, request_id: str) -> None:
    if _client is None:
        raise RuntimeError("MQTT client not started")
    payload = json.dumps({"request_id": request_id, "issued_at": _now_iso()})
    _client.publish(f"devices/{device_id}/cmd/capture", payload, qos=1)


def publish_wifi_config(device_id: str, ssid: str, password: str, request_id: str) -> None:
    if _client is None:
        raise RuntimeError("MQTT client not started")
    payload = json.dumps({"request_id": request_id, "ssid": ssid, "password": password})
    _client.publish(f"devices/{device_id}/cmd/wifi_config", payload, qos=1, retain=False)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
