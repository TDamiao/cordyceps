"""Minimal in-memory sessions for the GitHub-authenticated admin."""

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
        self._oauth_states: dict[str, tuple[str, float]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.settings.github_client_id and self.settings.github_key)

    def begin_github_login(self) -> tuple[str, str]:
        """Create a one-use OAuth state and PKCE verifier."""
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        self._oauth_states[state] = (verifier, time.time() + 10 * 60)
        return state, verifier

    def consume_github_state(self, state: str) -> str:
        """Validate and consume a one-use OAuth state, returning its verifier."""
        verifier, expires = self._oauth_states.pop(state, ("", 0))
        if not verifier or expires <= time.time():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired GitHub login state",
            )
        return verifier

    def create_session(self) -> str:
        session_id = secrets.token_urlsafe(32)
        self._sessions[session_id] = time.time() + self.ttl
        return session_id

    def login(self, token: str) -> str:
        if not self.settings.admin_token or not hmac.compare_digest(
            token, self.settings.admin_token
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token"
            )
        return self.create_session()

    def require(self, request: Request) -> None:
        if not self.configured and not self.settings.admin_token:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GitHub OAuth is not configured",
            )
        authorization = request.headers.get("authorization", "")
        if (
            self.settings.admin_token
            and authorization.startswith("Bearer ")
            and hmac.compare_digest(authorization[7:], self.settings.admin_token)
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
