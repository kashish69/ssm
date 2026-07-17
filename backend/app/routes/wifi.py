import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import require_session
from app.config import settings
from app.db import get_cursor
from app.mqtt_client import publish_wifi_config

router = APIRouter(prefix="/api/devices", tags=["wifi"], dependencies=[Depends(require_session)])


class WifiConfigRequest(BaseModel):
    ssid: str
    password: str


@router.post("/{device_id}/wifi-config", status_code=status.HTTP_202_ACCEPTED)
def set_wifi_config(device_id: str, body: WifiConfigRequest):
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown device")

    request_id = str(uuid.uuid4())
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO wifi_requests (request_id, device_id, ssid, status)
            VALUES (?, ?, ?, 'pending')
            """,
            (request_id, device_id, body.ssid),
        )

    publish_wifi_config(device_id, body.ssid, body.password, request_id)
    return {"request_id": request_id, "status": "pending"}


@router.get("/{device_id}/wifi-config/{request_id}")
def wifi_config_status(device_id: str, request_id: str):
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT request_id, ssid, status, requested_at, error_message
            FROM wifi_requests
            WHERE request_id = ? AND device_id = ?
            """,
            (request_id, device_id),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown request")

    result_status = row["status"]

    # The agent only reports success: a wrong password or an out-of-range SSID
    # just means it keeps retrying and never reports, so age the request out.
    if result_status == "pending":
        with get_cursor() as cur:
            cur.execute(
                "SELECT (julianday('now') - julianday(?)) * 86400 AS age_seconds",
                (row["requested_at"],),
            )
            age = cur.fetchone()["age_seconds"]
        if age > settings.wifi_timeout_seconds:
            with get_cursor() as cur:
                cur.execute(
                    """
                    UPDATE wifi_requests SET status = 'timeout', completed_at = datetime('now')
                    WHERE request_id = ? AND status = 'pending'
                    """,
                    (request_id,),
                )
            result_status = "timeout"

    return {
        "request_id": request_id,
        "ssid": row["ssid"],
        "status": result_status,
        "error_message": row["error_message"],
    }
