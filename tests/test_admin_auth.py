"""Tests for the GitHub OAuth admin session boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.admin_auth import AdminAuth
from src.config import Settings


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request({"type": "http", "headers": headers or []})


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


@pytest.mark.asyncio
async def test_github_callback_accepts_only_configured_user(monkeypatch) -> None:
    import src.api_server as api

    settings = Settings(
        github_client_id="client",
        github_key="secret",
        github_allowed_user="tdamiao",
    )
    auth = AdminAuth(settings)
    state, _ = auth.begin_github_login()
    monkeypatch.setattr(api, "_admin", auth)
    monkeypatch.setattr(api, "_runtime", SimpleNamespace(settings=settings))

    async def unauthorized_identity(code: str, verifier: str) -> str:
        return "someone-else"

    monkeypatch.setattr(api, "_github_identity", unauthorized_identity)
    response = await api.login_page(_request(), code="code", state=state)

    assert response.status_code == 403
    assert b"n\xc3\xa3o est\xc3\xa1 autorizada" in response.body
