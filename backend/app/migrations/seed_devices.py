"""
Seed pre-provisioned devices from the DEVICE_SEEDS env var into the devices table.

Run inside the container:
    docker compose exec backend python -m app.migrations.seed_devices

Idempotent: re-running upserts by device_id (display_name/api_key updated in place).
See migrations/README.md for the expected env format.
"""

from app.auth import hash_secret
from app.config import settings
from app.db import get_cursor, init_db


def run() -> None:
    init_db()
    entries = [e.strip() for e in settings.device_seeds.split(";") if e.strip()]
    if not entries:
        print("DEVICE_SEEDS is empty; nothing to seed.")
        return

    with get_cursor() as cur:
        for entry in entries:
            parts = entry.split(":")
            if len(parts) != 3:
                print(f"Skipping malformed DEVICE_SEEDS entry: {entry!r}")
                continue
            device_id, display_name, api_key = (p.strip() for p in parts)
            cur.execute(
                """
                INSERT INTO devices (device_id, display_name, api_key_hash)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    api_key_hash = excluded.api_key_hash
                """,
                (device_id, display_name, hash_secret(api_key)),
            )
            cur.execute(
                """
                INSERT INTO device_status (device_id, online)
                VALUES (?, 0)
                ON CONFLICT(device_id) DO NOTHING
                """,
                (device_id,),
            )
    print(f"Seeded {len(entries)} device(s) from DEVICE_SEEDS.")


if __name__ == "__main__":
    run()
