from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import require_session
from app.db import get_cursor

router = APIRouter(prefix="/api/devices", tags=["devices"], dependencies=[Depends(require_session)])


@router.get("")
def list_devices():
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT d.device_id, d.display_name,
                   COALESCE(s.online, 0) AS online, s.last_seen
            FROM devices d
            LEFT JOIN device_status s ON s.device_id = d.device_id
            ORDER BY d.display_name
            """
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


@router.get("/{device_id}/status")
def device_status(device_id: str):
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT d.device_id, COALESCE(s.online, 0) AS online, s.last_seen
            FROM devices d
            LEFT JOIN device_status s ON s.device_id = d.device_id
            WHERE d.device_id = ?
            """,
            (device_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown device")
    return dict(row)
