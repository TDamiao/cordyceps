"""FastAPI orchestrator and authenticated operational dashboard."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc
from sqlmodel import Session, select

from src.admin_auth import AdminAuth
from src.config import get_settings
from src.database import (
    Execution,
    Opportunity,
    PaperTrade,
    RiskEvent,
    SystemEvent,
    add_system_event,
    get_engine,
    init_db,
)
from src.main import ArbitrageBot
from src.paper_engine import PaperEngine
from src.runtime import RuntimeState, get_runtime, reset_runtime
from src.safety import GeoblockService, ReadinessService, WalletService
from src.scanner import Scanner
from src.utils.logging import get_logger

logger = get_logger(__name__)
WEB_DIR = Path(__file__).with_name("web")

_bot: ArbitrageBot | None = None
_scanner: Scanner | None = None
_paper_engine: PaperEngine | None = None
_runtime: RuntimeState | None = None
_admin: AdminAuth | None = None
_geoblock: GeoblockService | None = None
_wallet: WalletService | None = None
_readiness: ReadinessService | None = None
_startup_ts: float | None = None


def create_bot() -> ArbitrageBot:
    return ArbitrageBot(runtime=_runtime, paper_engine=_paper_engine)


def create_paper_engine() -> PaperEngine:
    return PaperEngine(settings=_runtime.settings if _runtime else None)


def create_scanner(observer: Any = None, fetcher: Any = None) -> Scanner:
    settings = _runtime.settings if _runtime else get_settings()
    return Scanner(
        observer=observer,
        fetcher=fetcher,
        scan_interval_seconds=settings.scan_interval_seconds,
        market_limit=settings.market_limit,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _admin, _bot, _geoblock, _paper_engine, _readiness, _runtime, _scanner, _startup_ts, _wallet

    _startup_ts = time.time()
    reset_runtime()
    _runtime = get_runtime()  # Always starts disarmed, even when persisted config exists.
    init_db(_runtime.settings)
    _admin = AdminAuth(_runtime.settings)
    _paper_engine = create_paper_engine()
    _bot = create_bot()
    bot_task = asyncio.create_task(_bot.start(), name="cordyceps-bot")
    await asyncio.sleep(0.05)
    _scanner = create_scanner(
        observer=getattr(_bot, "_observer", None), fetcher=getattr(_bot, "_fetcher", None)
    )
    await _scanner.start()
    _geoblock = GeoblockService(_runtime.settings)
    _wallet = WalletService(_runtime.settings, getattr(_bot, "_client", None))
    _readiness = ReadinessService(_runtime, _geoblock, _wallet, _bot)
    logger.info(
        "orchestrator.started", port=_runtime.settings.port, mode=_runtime.settings.trading_mode
    )
    try:
        yield
    finally:
        if _runtime:
            _runtime.disarm()
        if _scanner:
            await _scanner.stop()
        if _bot:
            _bot.shutdown()
            try:
                await asyncio.wait_for(bot_task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                bot_task.cancel()
        _scanner = None
        _bot = None
        _paper_engine = None
        logger.info("orchestrator.stopped")


app = FastAPI(title="Cordyceps", version="0.2.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


def _require_admin(request: Request) -> None:
    if _admin is None:
        raise HTTPException(status_code=503, detail="Application is starting")
    _admin.require(request)


def _token_ids() -> list[str]:
    if not _bot or not getattr(_bot, "_observer", None):
        return []
    return _bot._observer.state.get_all_tracked_tokens()


def _login_html(error: str = "") -> str:
    settings = _runtime.settings if _runtime else get_settings()
    configured = bool(settings.github_client_id and settings.github_key)
    notice = f'<p class="login-error">{html.escape(error)}</p>' if error else ""
    if configured:
        action = '<a class="github-button" href="/auth/github">Continuar com GitHub</a>'
    else:
        missing = []
        if not settings.github_client_id:
            missing.append("GITHUB_CLIENT_ID")
        if not settings.github_key:
            missing.append("github_key")
        missing_text = ", ".join(missing)
        action = f'<button class="github-button" disabled>Falta configurar: {missing_text}</button>'
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="robots" content="noindex,nofollow,noarchive">
    <title>Cordyceps — Login</title><link rel="stylesheet" href="/assets/dashboard.css">
    <link rel="icon" type="image/svg+xml" href="/assets/favicon.svg"></head>
    <body class="login"><main class="login-card">
      <img src="/assets/favicon.svg" width="48" height="48" alt="">
      <p class="login-eyebrow">ACESSO ADMINISTRATIVO</p><h1>Cordyceps</h1>
      <p class="login-copy">Entre com a conta GitHub autorizada para acessar o painel.</p>
      {notice}{action}<p class="login-hint">Apenas @tdamiao tem acesso.</p>
    </main></body></html>"""


async def _github_identity(code: str, verifier: str) -> str:
    settings = _runtime.settings
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        async with session.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_key,
                "code": code,
                "redirect_uri": settings.github_redirect_uri,
                "code_verifier": verifier,
            },
        ) as response:
            token_payload = await response.json(content_type=None)
            if response.status != 200 or not token_payload.get("access_token"):
                logger.warning("github.oauth_token_failed", status=response.status)
                raise HTTPException(status_code=401, detail="GitHub authentication failed")
        async with session.get(
            "https://api.github.com/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token_payload['access_token']}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as response:
            profile = await response.json(content_type=None)
            if response.status != 200 or not profile.get("login"):
                logger.warning("github.user_lookup_failed", status=response.status)
                raise HTTPException(status_code=401, detail="Could not verify GitHub user")
            return str(profile["login"])


@app.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> Response:
    if _admin is None:
        raise HTTPException(status_code=503, detail="Application is starting")
    if error:
        return HTMLResponse(_login_html("Login cancelado ou negado pelo GitHub."), status_code=401)
    if not code:
        return HTMLResponse(_login_html())
    try:
        verifier = _admin.consume_github_state(state)
        username = await _github_identity(code, verifier)
    except HTTPException as exc:
        return HTMLResponse(_login_html(str(exc.detail)), status_code=exc.status_code)
    if username.casefold() != _runtime.settings.github_allowed_user.casefold():
        logger.warning("github.user_denied", username=username)
        return HTMLResponse(_login_html("Esta conta GitHub não está autorizada."), status_code=403)
    session_id = _admin.create_session()
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "cordyceps_admin",
        session_id,
        httponly=True,
        secure=_runtime.settings.github_redirect_uri.startswith("https://"),
        samesite="lax",
        max_age=8 * 3600,
    )
    return response


@app.get("/auth/github")
async def github_login() -> RedirectResponse:
    if _admin is None:
        raise HTTPException(status_code=503, detail="Application is starting")
    settings = _runtime.settings
    if not _admin.configured:
        missing = []
        if not settings.github_client_id:
            missing.append("GITHUB_CLIENT_ID")
        if not settings.github_key:
            missing.append("github_key")
        raise HTTPException(
            status_code=503,
            detail=f"GitHub OAuth is not configured; missing: {', '.join(missing)}",
        )
    state, verifier = _admin.begin_github_login()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    query = urlencode(
        {
            "client_id": settings.github_client_id,
            "redirect_uri": settings.github_redirect_uri,
            "state": state,
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
            "allow_signup": "false",
        }
    )
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{query}", status_code=302)


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    _require_admin(request)
    _admin.logout(request)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("cordyceps_admin")
    return response


@app.get("/")
async def dashboard(request: Request):
    try:
        _require_admin(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        raise
    return FileResponse(WEB_DIR / "dashboard.html")


@app.get("/markets")
async def markets_page(request: Request):
    try:
        _require_admin(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        raise
    return FileResponse(WEB_DIR / "markets.html")


@app.get("/opportunities")
async def opportunities_page(request: Request):
    try:
        _require_admin(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        raise
    return FileResponse(WEB_DIR / "opportunities.html")


@app.get("/trades")
async def trades_page(request: Request):
    try:
        _require_admin(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        raise
    return FileResponse(WEB_DIR / "trades.html")


@app.get("/healthcheck")
async def healthcheck_html(request: Request):
    return FileResponse(WEB_DIR / "health.html")


@app.get("/health")
async def health_endpoint() -> dict[str, Any]:
    settings = _runtime.settings if _runtime else get_settings()
    status = _bot.get_status() if _bot else {}
    observer = status.get("observer_stats", {})
    paper = status.get("paper") or {
        "trade_count": _paper_engine.trade_count if _paper_engine else 0,
        "total_profit": _paper_engine.total_profit if _paper_engine else 0,
    }
    return {
        "status": status.get("health", {}).get("status", "starting"),
        "mode": settings.trading_mode,
        "running": status.get("running", False),
        "database": "configured",
        "websocket": {"connected": status.get("health", {}).get("websocket_connected", False)},
        "scanner": {
            "running": _scanner.is_running if _scanner else False,
            "tracked_markets": len(_scanner._tracked) if _scanner else 0,
        },
        "paper_engine": paper,
        "books_with_liquidity": observer.get("books_with_liquidity", 0),
        "active_markets": status.get("active_markets", 0),
        "uptime": round(time.time() - _startup_ts, 2) if _startup_ts else 0,
    }


@app.get("/status")
async def status_endpoint(request: Request) -> dict[str, Any]:
    health = await health_endpoint()
    return {key: health[key] for key in ("status", "mode", "running", "websocket", "uptime")}


@app.get("/api/status")
async def api_status(request: Request) -> dict[str, Any]:
    _require_admin(request)
    settings = _runtime.settings
    status = _bot.get_status() if _bot else {}
    observer = status.get("observer_stats", {})
    engine = status.get("engine_stats", {})
    risk = status.get("risk", {})
    geo = await _geoblock.check()
    _runtime.geo_allowed = geo.checked and not geo.blocked
    wallet = _wallet.snapshot.public_dict()
    health_name = status.get("health", {}).get("status", "stopped")
    if _runtime.kill_switch or geo.blocked:
        health_name = "blocked"
    return {
        "name": settings.app_name,
        "mode": settings.trading_mode,
        "status": health_name,
        "running": status.get("running", False),
        "armed": _runtime.armed,
        "kill_switch": _runtime.kill_switch,
        "exposure_requires_attention": _runtime.incomplete_exposure_usd > 0,
        "uptime": round(time.time() - _startup_ts, 2) if _startup_ts else 0,
        "websocket": status.get("health", {}).get("websocket_connected", False),
        "clob_status": (
            "healthy" if status.get("health", {}).get("websocket_connected", False) else "stopped"
        ),
        "scanner": _scanner.is_running if _scanner else False,
        "markets": status.get("active_markets", 0),
        "tokens": observer.get("tracked_tokens", 0),
        "books_with_liquidity": observer.get("books_with_liquidity", 0),
        "book_updates": observer.get("book_updates", 0),
        "strategy": engine,
        "trades": status.get("health", {}).get("metrics", {}),
        "paper": status.get("paper", {}),
        "risk": risk,
        "current_exposure": risk.get("current_exposure", 0),
        "incomplete_exposure": _runtime.incomplete_exposure_usd,
        "geoblock": geo.public_dict(),
        "wallet": wallet,
        "secrets": {
            "private_key_configured": bool(settings.private_key),
            "clob_credentials_configured": bool(
                settings.polymarket_api_key
                and settings.polymarket_api_secret
                and settings.polymarket_api_passphrase
            ),
        },
    }


CONFIG_DESCRIPTIONS = {
    "max_trade_usd": "Máximo em pUSD utilizado por oportunidade.",
    "max_total_exposure_usd": "Máximo exposto simultaneamente.",
    "max_daily_loss_usd": "Perda diária máxima antes de bloquear operações.",
    "max_open_trades": "Número máximo de execuções abertas.",
    "min_profit_threshold": "Retorno mínimo legado, aplicado junto ao edge líquido.",
    "min_net_edge": "Vantagem mínima após fees, slippage e leg risk.",
    "min_net_profit_usd": "Lucro líquido mínimo por oportunidade.",
    "max_slippage_pct": "Slippage máximo aceito na revalidação.",
    "orderbook_stale_ms": "Idade máxima do order book.",
    "min_trade_shares": "Quantidade mínima de shares.",
    "max_leg_imbalance_usd": "Exposição direcional máxima durante recuperação.",
    "leg_timeout_ms": "Timeout de cada perna.",
    "circuit_breaker_failure_threshold": "Falhas consecutivas antes do circuit breaker.",
    "circuit_breaker_cooldown_minutes": "Cooldown após circuit breaker.",
    "simulated_latency_ms": "Latência aplicada ao paper engine.",
    "market_limit": "Máximo de mercados monitorados.",
    "scan_interval_seconds": "Intervalo de descoberta de mercados.",
}


@app.get("/api/config")
async def get_config(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return {
        "values": _runtime.settings.runtime_values(),
        "descriptions": CONFIG_DESCRIPTIONS,
        "profile": "Live Test - $10 Wallet",
    }


@app.put("/api/config")
async def update_config(request: Request, values: dict[str, Any] = Body(...)) -> dict[str, Any]:
    _require_admin(request)
    try:
        updated = _runtime.update_config(values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if _bot:
        _bot.apply_runtime_config()
    if _scanner:
        _scanner._market_limit = _runtime.settings.market_limit
        _scanner._interval = _runtime.settings.scan_interval_seconds
    with Session(get_engine(_runtime.settings)) as session:
        add_system_event(
            session,
            "runtime_config_updated",
            "Operational configuration updated",
            component="admin",
            details={"fields": sorted(values)},
        )
    return {"values": updated, "restart_required": False}


@app.get("/api/readiness")
async def readiness(request: Request, refresh: bool = False) -> dict[str, Any]:
    _require_admin(request)
    return await _readiness.check(force=refresh)


@app.post("/api/wallet/refresh")
async def refresh_wallet(request: Request) -> dict[str, Any]:
    _require_admin(request)
    _wallet.client = getattr(_bot, "_client", None)
    return (await _wallet.refresh(_token_ids())).public_dict()


@app.post("/api/control/kill")
async def kill(request: Request) -> dict[str, Any]:
    _require_admin(request)
    _runtime.kill()
    with Session(get_engine(_runtime.settings)) as session:
        add_system_event(
            session, "kill_switch", "Kill switch activated", severity="warning", component="admin"
        )
    return {"kill_switch": True, "armed": False}


@app.post("/api/control/resume")
async def resume(request: Request) -> dict[str, Any]:
    _require_admin(request)
    _runtime.resume()
    return {"kill_switch": False, "armed": False}


@app.post("/api/control/arm")
async def arm(request: Request, payload: dict[str, str] = Body(...)) -> dict[str, Any]:
    _require_admin(request)
    if _runtime.settings.trading_mode not in {"live_test", "live"}:
        raise HTTPException(status_code=409, detail="TRADING_MODE is not live_test or live")
    if payload.get("confirmation") != "CORDYCEPS LIVE":
        raise HTTPException(status_code=422, detail="Confirmation phrase does not match")
    result = await _readiness.check(force=True)
    if not result["ready"]:
        raise HTTPException(status_code=409, detail={"message": "Live readiness failed", **result})
    _runtime.arm()
    return {"armed": True, "mode": _runtime.settings.trading_mode}


@app.post("/api/control/disarm")
async def disarm(request: Request) -> dict[str, Any]:
    _require_admin(request)
    _runtime.disarm()
    return {"armed": False}


@app.get("/api/history")
async def history(request: Request, limit: int = 20) -> dict[str, Any]:
    _require_admin(request)
    limit = max(1, min(limit, 100))
    with Session(get_engine(_runtime.settings)) as session:
        return {
            "opportunities": [
                row.model_dump(mode="json")
                for row in session.exec(
                    select(Opportunity).order_by(desc(Opportunity.timestamp)).limit(limit)
                ).all()
            ],
            "executions": [
                row.model_dump(mode="json")
                for row in session.exec(
                    select(Execution).order_by(desc(Execution.created_at)).limit(limit)
                ).all()
            ],
            "paper_trades": [
                row.model_dump(mode="json")
                for row in session.exec(
                    select(PaperTrade).order_by(desc(PaperTrade.timestamp)).limit(limit)
                ).all()
            ],
            "risk_events": [
                row.model_dump(mode="json")
                for row in session.exec(
                    select(RiskEvent).order_by(desc(RiskEvent.timestamp)).limit(limit)
                ).all()
            ],
            "system_events": [
                row.model_dump(mode="json")
                for row in session.exec(
                    select(SystemEvent).order_by(desc(SystemEvent.timestamp)).limit(limit)
                ).all()
            ],
        }


def run_server(port: int | None = None) -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "src.api_server:app",
        host="0.0.0.0",
        port=port or settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    run_server()
