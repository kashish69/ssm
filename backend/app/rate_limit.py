from fastapi import HTTPException, status
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings
from app.db import get_cursor

limiter = Limiter(key_func=get_remote_address)


def check_device_cooldown(device_id: str) -> None:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT status, requested_at FROM capture_requests
            WHERE device_id = ?
            ORDER BY requested_at DESC LIMIT 1
            """,
            (device_id,),
        )
        row = cur.fetchone()
    if row is None:
        return
    if row["status"] == "pending":
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Capture already in progress")

    with get_cursor() as cur:
        cur.execute(
            """
            SELECT (julianday('now') - julianday(?)) * 86400 AS age_seconds
            """,
            (row["requested_at"],),
        )
        age = cur.fetchone()["age_seconds"]
    if age < settings.capture_cooldown_seconds:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Cooldown active, try again shortly")
