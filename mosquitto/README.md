# Mosquitto setup

Broker listens on **5335, plaintext** (`allow_anonymous false` still
enforced). TLS is terminated in front of it by Caddy (managed outside this
repo), not by mosquitto itself — same pattern as the backend on port 8000.
Two kinds of credentials, both in `mosquitto/config/passwd` (mosquitto's
hashed password file, not plaintext — generate/append entries with
`mosquitto_passwd`):

## Backend service account

One shared account the backend container uses to subscribe to all device
status/event topics and publish commands.

```
mosquitto_passwd -b mosquitto/config/passwd backend-service <MQTT_SERVICE_PASSWORD>
```
Set the same password in `.env` as `MQTT_SERVICE_PASSWORD` (used by
`app/mqtt_client.py`).

## Per-device accounts

Each Pi authenticates as `username=<device_id>`, `password=<device_api_key>`
— the **same api_key** you seeded for that device via `DEVICE_SEEDS` (see
`../backend/app/migrations/README.md`). Add it to the Mosquitto password file:

```
mosquitto_passwd -b mosquitto/config/passwd front-porch <the-device's-api-key>
```

`acl.conf` uses `pattern` rules keyed on `%u` (the connecting username), so
no per-device ACL entries are needed — adding the password-file entry is the
only extra step beyond the DB seed.

## TLS (handled by Caddy, not mosquitto)

`mosquitto.conf` has no TLS config. It binds two local listeners (see
`docker-compose.yml`), neither reachable from outside the box directly:

- `127.0.0.1:5335` — plain MQTT, used by the backend over the docker network.
- `127.0.0.1:9001` — MQTT-over-WebSocket, for external/Pi clients via Caddy.

Pi devices connect **MQTT-over-WebSocket (WSS)** through the existing Caddy
site on 443 — no `caddy-l4` plugin, no extra open port, reuses the backend's
TLS cert. Add a `/mqtt` route to the same site block that fronts the backend:

```
<public-domain> {
    handle /mqtt* {
        reverse_proxy 127.0.0.1:9001
    }
    handle {
        reverse_proxy 127.0.0.1:8000
    }
}
```
Caddy passes the WebSocket upgrade automatically. The Pi agent connects with
`MQTT_BROKER_PORT=443`, `MQTT_WS_PATH=/mqtt` (see `pi_agent/.env.example`).

The backend container itself talks to mosquitto directly over the internal
docker network (`MQTT_BROKER_HOST=mosquitto`, `MQTT_BROKER_PORT=5335`,
`MQTT_USE_TLS=false` in `.env`) — no TLS needed for that hop since it never
leaves the docker network.

After editing `passwd` or `acl.conf`, apply the change:
```
docker compose restart mosquitto
```
(Mosquitto also reloads `passwd`/`acl.conf` on SIGHUP, but a plain restart is
the simplest reliable option here.)
