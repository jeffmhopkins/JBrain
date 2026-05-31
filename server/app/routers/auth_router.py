"""Login / logout / current-user endpoints."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_user, verify_credentials
from ..config import get_settings
from ..db import get_conn

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginIn, request: Request):
    user_id = verify_credentials(body.username, body.password)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    request.session["user_id"] = user_id
    request.session["username"] = body.username
    return {"ok": True, "username": body.username}


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return {"authenticated": False, "brain_name": get_settings().brain_name}
    row = get_conn().execute(
        "SELECT username FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return {
        "authenticated": True,
        "username": row["username"] if row else None,
        "brain_name": get_settings().brain_name,
    }
