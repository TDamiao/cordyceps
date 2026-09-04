from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from src.fees import FeeParameters, FeeService


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self):
        return None

    async def json(self):
        return self.payload


class _Session:
    def __init__(self):
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return _Response({"fd": {"r": "0.01", "e": "1", "to": True}})


@pytest.mark.asyncio
async def test_refresh_uses_cached_fee_parameters():
    service = FeeService("https://clob.test", ttl_seconds=300)
    session = _Session()

    first = await service.refresh("condition", session=session)
    second = await service.refresh("condition", session=session)

    assert first == FeeParameters(
        rate=Decimal("0.01"), exponent=Decimal("1"), taker_only=True, source="clob"
    )
    assert second is first
    assert session.calls == 1


@pytest.mark.asyncio
async def test_background_refresh_is_deduplicated(monkeypatch):
    service = FeeService("https://clob.test")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_refresh(_condition_id):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return service.get("condition")

    monkeypatch.setattr(service, "refresh", slow_refresh)
    service.refresh_in_background("condition")
    service.refresh_in_background("condition")
    await started.wait()

    assert calls == 1
    assert len(service._refresh_tasks) == 1

    release.set()
    await asyncio.sleep(0)
    await service.close()
