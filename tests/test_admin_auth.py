"""Tests for the GitHub OAuth admin session boundary.

Updated for token-based auth (no more OAuth flow).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.admin_auth import AdminAuth
from src.config import Settings


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request({"type": "http", "headers": headers or []})


def _request_cookie(cookie_value: str) -> Request:
    """Create a request with a session cookie."""
    return Request(
        {"type": "http", "headers": [(b"cookie", f"cordyceps_admin={cookie_value}".encode())]}
    )


def test_github_state_is_one_use() -> None:
    auth = AdminAuth(Settings(github_client_id="client", github_key="secret"))
    state, verifier = auth.begin_github_login()

    assert auth.consume_github_state(state) == verifier
    with pytest.raises(HTTPException, match="Invalid or expired"):
        auth.consume_github_state(state)


def test_empty_bearer_never_authenticates() -> None:
    auth = AdminAuth(Settings(github_client_id="client", github_key="secret"))

    with pytest.raises(HTTPException) as exc:
        auth.require(_request([(b"authorization", b"Bearer ")]))

    assert exc.value.status_code == 401


def test_invalid_token_rejected() -> None:
    """Invalid admin token is rejected."""
    auth = AdminAuth(Settings(admin_token="valid_token"))

    with pytest.raises(HTTPException) as exc:
        auth.require(_request([(b"authorization", b"Bearer wrong_token")]))

    assert exc.value.status_code == 401


def test_valid_token_accepted() -> None:
    """Valid admin token is accepted."""
    auth = AdminAuth(Settings(admin_token="valid_token"))

    # Should not raise
    auth.require(_request([(b"authorization", b"Bearer valid_token")]))


def test_missing_token_rejected() -> None:
    """Missing admin token is rejected."""
    auth = AdminAuth(Settings(admin_token="valid_token"))

    with pytest.raises(HTTPException) as exc:
        auth.require(_request())

    assert exc.value.status_code == 401


def test_cookie_based_auth() -> None:
    """Session cookie authentication works."""
    auth = AdminAuth(Settings(admin_token="valid_token"))

    # Create a valid session
    session_id = auth.login("valid_token")

    # Session cookie should be accepted
    auth.require(_request_cookie(session_id))


def test_invalid_session_cookie_rejected() -> None:
    """Invalid session cookie is rejected."""
    auth = AdminAuth(Settings(admin_token="valid_token"))

    with pytest.raises(HTTPException) as exc:
        auth.require(_request_cookie("invalid_session_id"))

    assert exc.value.status_code == 401