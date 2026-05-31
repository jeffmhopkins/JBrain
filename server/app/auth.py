"""Session auth: verify credentials, gate routes via a FastAPI dependency.

Sessions are signed cookies (Starlette SessionMiddleware). Single-user/admin in
v1, but the users table supports more later.
"""
from __future__ import annotations

from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status

from .db import get_conn, ph


def verify_credentials(username: str, password: str) -> int | None:
    """Return the user id on success, else None."""
    row = get_conn().execute(
        "SELECT id, password_hash FROM users WHERE username = ?", (username,)
    ).fetchone()
    if not row:
        return None
    try:
        ph.verify(row["password_hash"], password)
    except VerifyMismatchError:
        return None
    return row["id"]


def require_user(request: Request) -> int:
    """Dependency that enforces an authenticated session."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    return user_id


CurrentUser = Depends(require_user)
