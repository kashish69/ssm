from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import require_session
from app.db import get_cursor
from app.mqtt_client import publish_wifi_config

router = APIRouter(prefix="/api/devices", tags=["wifi"], dependencies=[Depends(require_session)])


class WifiConfigRequest(BaseModel):
    ssid: str
    password: str


@router.post("/{device_id}/wifi-config")
def set_wifi_config(device_id: str, body: WifiConfigRequest):
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown device")

    publish_wifi_config(device_id, body.ssid, body.password)
    return {"ok": True}
