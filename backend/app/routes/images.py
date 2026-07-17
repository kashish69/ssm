from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import require_session
from app.db import get_cursor
from app.s3 import presigned_get_url

router = APIRouter(prefix="/api/devices", tags=["images"], dependencies=[Depends(require_session)])


@router.get("/{device_id}/latest")
def latest_image(device_id: str):
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, s3_key, captured_at FROM images
            WHERE device_id = ?
            ORDER BY captured_at DESC LIMIT 1
            """,
            (device_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No captures yet for this device")
    return {
        "id": row["id"],
        "captured_at": row["captured_at"],
        "image_url": presigned_get_url(row["s3_key"]),
    }


@router.get("/{device_id}/history")
def image_history(device_id: str, limit: int = Query(default=20, le=100), offset: int = Query(default=0, ge=0)):
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, s3_key, captured_at FROM images
            WHERE device_id = ?
            ORDER BY captured_at DESC LIMIT ? OFFSET ?
            """,
            (device_id, limit, offset),
        )
        rows = cur.fetchall()
    return [
        {"id": r["id"], "captured_at": r["captured_at"], "image_url": presigned_get_url(r["s3_key"])}
        for r in rows
    ]
