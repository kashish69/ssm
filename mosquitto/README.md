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

`mosquitto.conf` has no TLS config — it only binds `127.0.0.1:5335` on the
host (see `docker-compose.yml`), not reachable from outside the box directly.
Caddy (outside this repo) must terminate TLS for the public MQTT port and
proxy the decrypted TCP stream to `127.0.0.1:5335`, e.g. via Caddy's `layer4`
app:

```
{
    layer4 {
        <public-domain>:8883 {
            @mqtt tls
            route @mqtt {
                tls
                proxy 127.0.0.1:5335
            }
        }
    }
}
```
(exact syntax depends on your Caddy version/plugins — adjust to however
you're already fronting the backend on 8000.)

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
