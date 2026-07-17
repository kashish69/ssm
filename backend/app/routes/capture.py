import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import require_session
from app.config import settings
from app.db import get_cursor
from app.mqtt_client import publish_capture_command
from app.rate_limit import check_device_cooldown
from app.s3 import presigned_get_url

router = APIRouter(prefix="/api/devices", tags=["capture"], dependencies=[Depends(require_session)])


@router.post("/{device_id}/capture", status_code=status.HTTP_202_ACCEPTED)
def trigger_capture(device_id: str):
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(s.online, 0) AS online
            FROM devices d LEFT JOIN device_status s ON s.device_id = d.device_id
            WHERE d.device_id = ?
            """,
            (device_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown device")
    if not row["online"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device is offline")

    check_device_cooldown(device_id)

    request_id = str(uuid.uuid4())
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO capture_requests (request_id, device_id, status)
            VALUES (?, ?, 'pending')
            """,
            (request_id, device_id),
        )

    publish_capture_command(device_id, request_id)
    return {"request_id": request_id, "status": "pending"}


@router.get("/{device_id}/capture/{request_id}")
def capture_status(device_id: str, request_id: str):
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT cr.request_id, cr.status, cr.requested_at, cr.error_message,
                   i.s3_key, i.captured_at
            FROM capture_requests cr
            LEFT JOIN images i ON i.id = cr.image_id
            WHERE cr.request_id = ? AND cr.device_id = ?
            """,
            (request_id, device_id),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown request")

    result_status = row["status"]

    if result_status == "pending":
        with get_cursor() as cur:
            cur.execute(
                "SELECT (julianday('now') - julianday(?)) * 86400 AS age_seconds",
                (row["requested_at"],),
            )
            age = cur.fetchone()["age_seconds"]
        if age > settings.capture_timeout_seconds:
            with get_cursor() as cur:
                cur.execute(
                    """
                    UPDATE capture_requests SET status = 'timeout', completed_at = datetime('now')
                    WHERE request_id = ? AND status = 'pending'
                    """,
                    (request_id,),
                )
            result_status = "timeout"

    response = {"request_id": request_id, "status": result_status, "error_message": row["error_message"]}
    if result_status == "completed" and row["s3_key"]:
        response["image_url"] = presigned_get_url(row["s3_key"])
        response["captured_at"] = row["captured_at"]
    return response
