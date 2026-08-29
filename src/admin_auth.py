"""Minimal server-side admin sessions derived from ADMIN_TOKEN."""

from __future__ import annotations

import hmac
import secrets
import time

from fastapi import HTTPException, Request, status

from src.config import Settings


class AdminAuth:
    def __init__(self, settings: Settings, ttl_seconds: int = 8 * 3600):
        self.settings = settings
        self.ttl = ttl_seconds
        self._sessions: dict[str, float] = {}

    @property
    def configured(self) -> bool:
        return bool(self.settings.admin_token)

    def login(self, token: str) -> str:
        if not self.configured or not hmac.compare_digest(token, self.settings.admin_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token"
            )
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = time.time() + self.ttl
        return session_id

    def require(self, request: Request) -> None:
        if not self.configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ADMIN_TOKEN is not configured",
            )
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer ") and hmac.compare_digest(
            authorization[7:], self.settings.admin_token
        ):
            return
        session_id = request.cookies.get("cordyceps_admin", "")
        expires = self._sessions.get(session_id, 0)
        if session_id and expires > time.time():
            return
        self._sessions.pop(session_id, None)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required"
        )

    def logout(self, request: Request) -> None:
        self._sessions.pop(request.cookies.get("cordyceps_admin", ""), None)
