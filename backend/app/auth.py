import hashlib
import hmac

from fastapi import Cookie, Header, HTTPException, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings
from app.db import get_cursor

_serializer = URLSafeTimedSerializer(settings.session_secret, salt="session-cookie")


def hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verify_passkey(raw_passkey: str) -> bool:
    candidate = hash_secret(raw_passkey)
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM passkeys WHERE passkey_hash = ?", (candidate,))
        return cur.fetchone() is not None


def create_session_token() -> str:
    return _serializer.dumps({"authenticated": True})


def verify_session_token(token: str) -> bool:
    try:
        _serializer.loads(token, max_age=settings.session_max_age_seconds)
        return True
    except (BadSignature, SignatureExpired):
        return False


def require_session(session: str | None = Cookie(default=None, alias="session")) -> None:
    if not session or not verify_session_token(session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


def require_device_key(
    device_id: str,
    x_device_id: str = Header(...),
    x_device_key: str = Header(...),
) -> None:
    if x_device_id != device_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Device mismatch")
    with get_cursor() as cur:
        cur.execute("SELECT api_key_hash FROM devices WHERE device_id = ?", (device_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown device")
    candidate = hash_secret(x_device_key)
    if not hmac.compare_digest(candidate, row["api_key_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid device key")
