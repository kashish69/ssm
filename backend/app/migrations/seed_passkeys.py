"""
Seed app-login passkeys from the APP_PASSKEYS env var into the passkeys table.

Run inside the container:
    docker compose exec backend python -m app.migrations.seed_passkeys

Idempotent: re-running with the same APP_PASSKEYS is a no-op (unique on hash).
See migrations/README.md for the expected env format.
"""

from app.auth import hash_secret
from app.config import settings
from app.db import get_cursor, init_db


def run() -> None:
    init_db()
    raw_passkeys = [p.strip() for p in settings.app_passkeys.split(",") if p.strip()]
    if not raw_passkeys:
        print("APP_PASSKEYS is empty; nothing to seed.")
        return

    with get_cursor() as cur:
        for i, passkey in enumerate(raw_passkeys):
            passkey_hash = hash_secret(passkey)
            cur.execute(
                """
                INSERT INTO passkeys (passkey_hash, label)
                VALUES (?, ?)
                ON CONFLICT(passkey_hash) DO NOTHING
                """,
                (passkey_hash, f"env-seed-{i}"),
            )
    print(f"Seeded {len(raw_passkeys)} passkey(s) from APP_PASSKEYS.")


if __name__ == "__main__":
    run()
