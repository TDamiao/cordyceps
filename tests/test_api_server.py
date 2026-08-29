"""Tests for the FastAPI API server and orchestration entrypoint."""

from __future__ import annotations

import asyncio
import sys

import pytest

# ---------------------------------------------------------------------------
# Fake bot / scanner / paper engine used by tests
# ---------------------------------------------------------------------------


class _FakeBot:
    def __init__(self):
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True
        await asyncio.sleep(0)

    async def stop(self):
        self.stopped = True

    def shutdown(self):
        self.stopped = True

    def get_status(self):
        return {
            "running": True,
            "health": {
                "websocket_connected": True,
                "status": "healthy",
            },
            "active_markets": 5,
        }


class _FakeScanner:
    def __init__(self, **kwargs):
        self._tracked: set = {"m1", "m2"}
        self._running = True

    @property
    def is_running(self):
        return self._running

    async def start(self):
        self._running = True

    async def stop(self):
        self._running = False


class _FakePaperEngine:
    def __init__(self):
        self.trade_count = 3
        self.total_profit = 0.12


# ---------------------------------------------------------------------------
# Fixture: import the api_server module with a controlled env
# ---------------------------------------------------------------------------


@pytest.fixture
def api_module(monkeypatch):
    """Import src.api_server with a controlled environment."""
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "0" * 64)
    monkeypatch.setenv("PROXY_ADDRESS", "0x" + "0" * 40)
    sys.modules.pop("src.api_server", None)
    import importlib

    mod = importlib.import_module("src.api_server")
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_mode_database_and_websocket(api_module, monkeypatch):
    """Health endpoint exposes mode, database, and websocket status."""
    monkeypatch.setattr(api_module, "_bot", _FakeBot())
    monkeypatch.setattr(api_module, "_scanner", _FakeScanner())
    monkeypatch.setattr(api_module, "_paper_engine", _FakePaperEngine())
    monkeypatch.setattr(api_module, "_startup_ts", 1000.0)

    payload = await api_module.health_endpoint()

    assert payload["mode"] == "paper"
    assert payload["database"] == "sqlite:///./cordyceps.db"
    assert payload["websocket"]["connected"] is True
    assert payload["websocket"]["status"] == "healthy"
    assert payload["scanner"]["running"] is True


@pytest.mark.asyncio
async def test_health_shows_paper_engine_stats(api_module, monkeypatch):
    monkeypatch.setattr(api_module, "_bot", _FakeBot())
    monkeypatch.setattr(api_module, "_scanner", _FakeScanner())
    monkeypatch.setattr(api_module, "_paper_engine", _FakePaperEngine())

    payload = await api_module.health_endpoint()

    assert payload["paper_engine"]["trade_count"] == 3
    assert payload["paper_engine"]["total_profit"] == pytest.approx(0.12)


@pytest.mark.asyncio
async def test_lifespan_creates_single_bot_and_shuts_down(api_module, monkeypatch):
    """The lifespan context manager creates one bot and stops it on exit."""
    created_bots = []
    created_papers = []

    def fake_create_bot():
        b = _FakeBot()
        created_bots.append(b)
        return b

    def fake_create_paper():
        p = _FakePaperEngine()
        created_papers.append(p)
        return p

    def fake_create_scanner(**kwargs):
        return _FakeScanner(**kwargs)

    monkeypatch.setattr(api_module, "create_bot", fake_create_bot)
    monkeypatch.setattr(api_module, "create_paper_engine", fake_create_paper)
    monkeypatch.setattr(api_module, "create_scanner", fake_create_scanner)

    # Reset module state
    monkeypatch.setattr(api_module, "_bot", None)
    monkeypatch.setattr(api_module, "_scanner", None)
    monkeypatch.setattr(api_module, "_paper_engine", None)
    monkeypatch.setattr(api_module, "_startup_ts", None)

    # Enter and exit the lifespan context
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def noop_lifespan(app):
        yield

    # Use the actual lifespan function
    async with api_module.lifespan(api_module.app):
        assert api_module._bot is created_bots[0]
        assert api_module._scanner is not None
        assert api_module._paper_engine is created_papers[0]
        assert len(created_bots) == 1

        # Health should work during lifespan
        payload = await api_module.health_endpoint()
        assert payload["mode"] == "paper"

    # After exiting lifespan: bot stopped, globals cleared
    assert created_bots[0].stopped is True
    assert api_module._bot is None
    assert api_module._scanner is None
    assert api_module._paper_engine is None
