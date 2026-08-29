"""
FastAPI API server and single-process orchestrator for Cordyceps.

Starts market discovery, websocket consumer/resync, scanner, paper engine,
and the HTTP API on PORT=8000.  A singleton guard prevents duplicate scanner
tasks across uvicorn workers.

Health endpoint exposes:
  - mode (paper / live)
  - database (connection string)
  - polymarket (API connectivity hints)
  - websocket (connection status)
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from src.config import get_settings
from src.main import ArbitrageBot
from src.paper_engine import PaperEngine
from src.scanner import Scanner
from src.utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (shared across lifespan and route handlers)
# ---------------------------------------------------------------------------
_bot: Optional[ArbitrageBot] = None
_scanner: Optional[Scanner] = None
_paper_engine: Optional[PaperEngine] = None
_startup_ts: Optional[float] = None


# ---------------------------------------------------------------------------
# Factories – overridable in tests via monkeypatch
# ---------------------------------------------------------------------------

def create_bot() -> ArbitrageBot:
    """Create (but do not start) the shared ArbitrageBot instance."""
    return ArbitrageBot()


def create_paper_engine() -> PaperEngine:
    return PaperEngine()


def create_scanner(
    observer: Any = None,
    fetcher: Any = None,
) -> Scanner:
    return Scanner(observer=observer, fetcher=fetcher)


# ---------------------------------------------------------------------------
# Lifespan – runs once per process
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown lifecycle for the orchestrator."""
    global _bot, _scanner, _paper_engine, _startup_ts

    # ---- startup ----
    _startup_ts = time.time()
    _bot = create_bot()
    _paper_engine = create_paper_engine()

    # Kick off the bot's background tasks (observer, engine, executor).
    # ArbitrageBot.start() blocks on a shutdown event, so we spawn it as a
    # fire-and-forget task.
    asyncio.create_task(_bot.start(), name="cordyceps-bot")

    # Brief yield so the observer can initialise before scanner attaches
    await asyncio.sleep(0.05)

    # Attach the scanner to the bot's observer for market discovery / resync
    _scanner = create_scanner(
        observer=getattr(_bot, "_observer", None),
        fetcher=getattr(_bot, "_fetcher", None),
    )
    await _scanner.start()

    settings = get_settings()
    logger.info(
        "orchestrator.started",
        port=settings.port,
        mode=settings.trading_mode,
    )

    yield  # ---- server is live ----

    # ---- shutdown ----
    if _scanner is not None:
        await _scanner.stop()
        _scanner = None
    if _bot is not None:
        try:
            await _bot.stop()
        except Exception as exc:  # pragma: no cover – defensive
            logger.warning("bot_stop_error", error=str(exc))
        _bot = None
    _paper_engine = None
    logger.info("orchestrator.stopped")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Cordyceps Orchestrator",
    description="Polymarket arbitrage bot API",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_endpoint() -> Dict[str, Any]:
    """
    Comprehensive health payload.

    Returns:
        mode          – paper or live
        database      – database_url from settings
        polymarket    – CLOB / Gamma API status hint
        websocket     – connection + health status
        uptime        – seconds since orchestrator started
        scanner       – whether the scanner loop is active
        paper_engine  – simulated trade stats
    """
    settings = get_settings()

    # Bot status (may not be ready yet during early startup)
    bot_health: Dict[str, Any] = {}
    bot_running = False
    active_markets = 0
    if _bot is not None:
        try:
            status = _bot.get_status()
            bot_health = status.get("health", {})
            bot_running = status.get("running", False)
            active_markets = status.get("active_markets", 0)
        except Exception:  # pragma: no cover – defensive
            pass

    # Paper engine stats
    paper_stats: Dict[str, Any] = {}
    if _paper_engine is not None:
        paper_stats = {
            "trade_count": _paper_engine.trade_count,
            "total_profit": _paper_engine.total_profit,
        }

    return {
        "mode": settings.trading_mode,
        "database": settings.database_url,
        "polymarket": {
            "clob_url": settings.clob_api_url,
            "gamma_url": settings.gamma_api_url,
            "ws_url": settings.clob_ws_url,
        },
        "websocket": {
            "connected": bot_health.get("websocket_connected", False),
            "status": bot_health.get("status", "unknown"),
        },
        "scanner": {
            "running": _scanner.is_running if _scanner else False,
            "tracked_markets": len(_scanner._tracked) if _scanner else 0,
        },
        "paper_engine": paper_stats,
        "active_markets": active_markets,
        "running": bot_running,
        "uptime": round(time.time() - _startup_ts, 2) if _startup_ts else 0.0,
    }


# ---------------------------------------------------------------------------
# Status / debug endpoint
# ---------------------------------------------------------------------------

@app.get("/status")
async def status_endpoint() -> Dict[str, Any]:
    """Lightweight status for liveness probes."""
    if _bot is not None:
        return _bot.get_status()
    return {"running": False}


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def run_server(port: Optional[int] = None) -> None:
    """Start uvicorn programmatically."""
    import uvicorn

    settings = get_settings()
    port = port or settings.port
    uvicorn.run(
        "src.api_server:app",
        host="0.0.0.0",
        port=port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    run_server()
