import io
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from PIL import Image, UnidentifiedImageError

from app.auth import require_device_key
from app.config import settings
from app.db import get_cursor
from app.s3 import put_image

router = APIRouter(prefix="/api/devices", tags=["upload"])


@router.post("/{device_id}/upload", dependencies=[Depends(require_device_key)])
async def upload_image(
    device_id: str,
    request: Request,
    request_id: str = Form(...),
    file: UploadFile = File(...),
):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.upload_max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Image too large")

    body = await file.read(settings.upload_max_bytes + 1)
    if len(body) > settings.upload_max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Image too large")

    try:
        Image.open(io.BytesIO(body)).verify()
    except UnidentifiedImageError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a valid image")

    with get_cursor() as cur:
        cur.execute(
            "SELECT status FROM capture_requests WHERE request_id = ? AND device_id = ?",
            (request_id, device_id),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown request_id for this device")
    if row["status"] != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Request is not pending")

    image_id = str(uuid.uuid4())
    s3_key = put_image(f"{device_id}/{request_id}.jpg", body, "image/jpeg")

    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO images (id, device_id, request_id, s3_key, content_type, size_bytes)
            VALUES (?, ?, ?, ?, 'image/jpeg', ?)
            """,
            (image_id, device_id, request_id, s3_key, len(body)),
        )
        cur.execute(
            """
            UPDATE capture_requests
            SET status = 'completed', completed_at = datetime('now'), image_id = ?
            WHERE request_id = ?
            """,
            (image_id, request_id),
        )

    return {"ok": True, "image_id": image_id}
