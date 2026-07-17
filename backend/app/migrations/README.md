# Migrations / Seeding

No traditional schema-migration tool is used (SQLite schema is created/kept up to
date automatically by `init_db()` on startup via `CREATE TABLE IF NOT EXISTS`).
"Migration" here means the two seed scripts below, which populate data from
`.env` into the database. Both are idempotent — safe to re-run any time you add
a device or passkey.

## Passkeys — `seed_passkeys.py`

Controls who can log into the app.

**.env format:**
```
APP_PASSKEYS=correct-horse-battery-staple,another-family-members-passkey
```
Comma-separated list of plaintext passkeys. Each is SHA-256 hashed before being
stored — the plaintext never touches the database.

**Run (inside the running container):**
```
docker compose exec backend python -m app.migrations.seed_passkeys
```

**To add a new passkey later:** append it to `APP_PASSKEYS` in `.env`, restart
the container (or just re-run the command above — it reads the env at run
time), then re-run the seed command. Existing passkeys are untouched
(`ON CONFLICT ... DO NOTHING` on the hash).

**To revoke a passkey:** remove it from `.env` and delete the corresponding
row directly, e.g.:
```
docker compose exec backend python -c "
from app.auth import hash_secret
from app.db import get_cursor
with get_cursor() as cur:
    cur.execute('DELETE FROM passkeys WHERE passkey_hash = ?', (hash_secret('the-old-passkey'),))
"
```

## Devices — `seed_devices.py`

Pre-provisions which Raspberry Pi devices exist. There is no runtime device
registration endpoint by design — a device must be seeded here before it can
authenticate.

**.env format:**
```
DEVICE_SEEDS=front-porch:Front Porch Cam:sK3f...longrandomkey;backyard:Backyard Cam:xQ9...longrandomkey
```
Semicolon-separated entries, each `device_id:display_name:api_key`
(colon-separated). Generate each `api_key` with something like:
```
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```
The `api_key` is what you put in the matching Pi's `pi_agent` env
(`DEVICE_API_KEY`) and use as its Mosquitto password — keep it secret.

**Run (inside the running container):**
```
docker compose exec backend python -m app.migrations.seed_devices
```

**To add a new device:** append an entry to `DEVICE_SEEDS`, re-run the seed
command, then add the matching Mosquitto credential (see
`mosquitto/README.md` if present, or `mosquitto/acl.conf` / your mosquitto
password file) so the device can actually connect to the broker with that
same `device_id` / `api_key` pair.

**To update a device's name or rotate its key:** edit its entry in
`DEVICE_SEEDS` (upsert on `device_id`), re-run the seed command, update the
Pi's env and Mosquitto credential to match.
