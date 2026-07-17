from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.auth import create_session_token, verify_passkey
from app.config import settings
from app.rate_limit import limiter

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    passkey: str


@router.post("/login")
@limiter.limit("10/minute")
def login(request: Request, body: LoginRequest, response: Response):
    if not verify_passkey(body.passkey):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid passkey")

    token = create_session_token()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.session_max_age_seconds,
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(key=settings.session_cookie_name)
    return {"ok": True}
