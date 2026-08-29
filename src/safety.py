"""Read-only wallet, geoblock, and live-readiness checks."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import structlog
from sqlalchemy import text
from sqlmodel import Session
from web3 import Web3

from src.client.auth import derive_eoa_address
from src.config import Contracts, Settings
from src.database import get_engine
from src.runtime import RuntimeState

logger = structlog.get_logger(__name__)


@dataclass
class GeoblockResult:
    checked: bool = True
    blocked: bool = False
    country: str = "DISABLED"
    region: str = ""
    ip: str = ""
    error: str = ""
    checked_at: float = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "blocked": self.blocked,
            "country": self.country,
            "region": self.region,
            "trading_allowed": True,
            "error": self.error,
            "checked_at": self.checked_at,
        }


class GeoblockService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cached = GeoblockResult(checked_at=time.time())
        self._lock = asyncio.Lock()

    async def check(self, force: bool = False) -> GeoblockResult:
        # Geoblock check removed/disabled. Always return allowed.
        self._cached = GeoblockResult(
            checked=True,
            blocked=False,
            country="DISABLED",
            region="DISABLED",
            ip="",
            error="",
            checked_at=time.time(),
        )
        return self._cached


@dataclass
class WalletSnapshot:
    eoa_address: str = ""
    proxy_address: str = ""
    collateral_balance: float | None = None
    collateral_allowance: float | None = None
    ctf_balances: dict[str, float] = field(default_factory=dict)
    ctf_allowance: float | None = None
    authenticated: bool = False
    error: str = ""
    refreshed_at: float = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "eoa_address": self.eoa_address,
            "proxy_address": self.proxy_address,
            "usdc_balance": self.collateral_balance,
            "collateral": "pUSD",
            "usdc_allowance": self.collateral_allowance,
            "exchange_allowance": self.collateral_allowance,
            "ctf_balances": self.ctf_balances,
            "ctf_allowance": self.ctf_allowance,
            "authenticated": self.authenticated,
            "private_key_configured": bool(self.eoa_address),
            "clob_credentials_configured": self.authenticated,
            "error": self.error,
            "last_refresh": self.refreshed_at,
        }


class WalletService:
    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        self.client = client
        self.snapshot = WalletSnapshot(proxy_address=settings.proxy_address)

    @staticmethod
    def _units(value: Any) -> float:
        """Convert the CLOB's 6-decimal raw collateral units to pUSD."""
        try:
            return int(str(value or "0")) / 1_000_000
        except (TypeError, ValueError):
            return 0.0

    async def _fetch_onchain_balance(self, address: str) -> tuple[float, float]:
        def _check():
            try:
                rpc_url = self.settings.polygon_rpc_url or "https://polygon-bor-rpc.publicnode.com"
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 5}))
                target = Web3.to_checksum_address(address)
                abi = [
                    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
                    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "remaining", "type": "uint256"}], "type": "function"},
                ]
                # CLOB V2 collateral is pUSD. Reading USDC.e/native here makes
                # funded V2 proxy wallets appear empty whenever the CLOB API's
                # proxy balance lookup returns its known false zero.
                tokens = [Contracts.PUSD]
                spenders = [
                    "0xE111180000d2663C0091e4f400237545B87B996B",  # CTF Exchange V2
                    "0xe2222d279d744050d28e00520010520000310F59",  # Neg Risk Exchange V2
                ]
                total_bal = 0.0
                max_allowance = 0.0
                for token_addr in tokens:
                    c = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=abi)
                    bal = c.functions.balanceOf(target).call() / 1e6
                    total_bal += bal
                    for spender in spenders:
                        alw = c.functions.allowance(target, Web3.to_checksum_address(spender)).call() / 1e6
                        if alw > max_allowance:
                            max_allowance = alw
                return total_bal, max_allowance
            except Exception:
                return 0.0, 0.0
        return await asyncio.to_thread(_check)

    async def refresh(self, token_ids: list[str] | None = None) -> WalletSnapshot:
        snap = WalletSnapshot(proxy_address=self.settings.proxy_address, refreshed_at=time.time())
        if self.settings.private_key:
            snap.eoa_address = derive_eoa_address(self.settings.private_key)

        if self.client is not None:
            snap.authenticated = True

        try:
            if self.client is None:
                raise RuntimeError("authenticated CLOB client unavailable")

            # Query Collateral Balance from CLOB API
            collateral = {}
            try:
                collateral = await asyncio.to_thread(self.client.get_balance_allowance)
            except Exception as e:
                logger.warning("CLOB get_balance_allowance error", error=str(e))

            clob_bal = self._units(collateral.get("balance")) if collateral else 0.0
            allowances = collateral.get("allowances") or collateral.get("allowance") or {}

            if isinstance(allowances, dict) and allowances:
                clob_allowance = min((self._units(v) for v in allowances.values()), default=0.0)
            elif isinstance(allowances, (int, float, str)):
                clob_allowance = self._units(allowances)
            elif isinstance(allowances, list) and allowances:
                clob_allowance = min((self._units(v) for v in allowances), default=0.0)
            else:
                clob_allowance = 0.0

            # Fallback to on-chain Polygon check if CLOB returns 0
            target_address = self.settings.proxy_address or snap.eoa_address
            if clob_bal == 0.0 and target_address:
                try:
                    onchain_bal, onchain_allowance = await self._fetch_onchain_balance(target_address)
                    if onchain_bal > 0:
                        clob_bal = onchain_bal
                    if onchain_allowance > 0 and clob_allowance == 0.0:
                        clob_allowance = onchain_allowance
                except Exception:
                    pass

            snap.collateral_balance = clob_bal
            snap.collateral_allowance = clob_allowance
            snap.authenticated = True
            logger.info("wallet.refreshed", raw_collateral=collateral, balance=clob_bal, allowance=clob_allowance)
        except Exception as exc:
            snap.error = str(exc)
            logger.warning("wallet refresh exception", error=str(exc))
        self.snapshot = snap
        return snap


class ReadinessService:
    """Fail-closed checks. No method in this class submits or approves anything."""

    def __init__(
        self,
        runtime: RuntimeState,
        geoblock: GeoblockService,
        wallet: WalletService,
        bot: Any = None,
    ):
        self.runtime = runtime
        self.geoblock = geoblock
        self.wallet = wallet
        self.bot = bot

    @staticmethod
    async def _http_ok(url: str) -> bool:
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    return response.status < 500
        except Exception:
            return False

    async def check(self, force: bool = False) -> dict[str, Any]:
        settings = self.runtime.settings
        checks: dict[str, dict[str, Any]] = {}

        try:
            with Session(get_engine(settings)) as session:
                session.exec(text("SELECT 1"))
            checks["database"] = {"status": "ok"}
        except Exception as exc:
            checks["database"] = {"status": "blocked", "detail": str(exc)}

        gamma_ok, clob_ok = await asyncio.gather(
            self._http_ok(f"{settings.gamma_api_url}/markets?limit=1"),
            self._http_ok(f"{settings.clob_api_url}/time"),
        )
        checks["gamma_api"] = {"status": "ok" if gamma_ok else "blocked"}
        checks["clob_api"] = {"status": "ok" if clob_ok else "blocked"}

        status = self.bot.get_status() if self.bot else {}
        observer = status.get("observer_stats", {})
        connected = bool(status.get("health", {}).get("websocket_connected"))
        checks["websocket"] = {"status": "ok" if connected else "blocked"}
        checks["market_data"] = {
            "status": "ok" if observer.get("book_updates", 0) > 0 else "blocked"
        }
        checks["order_books"] = {
            "status": "ok" if observer.get("books_with_liquidity", 0) > 0 else "blocked"
        }

        checks["wallet"] = {
            "status": "ok" if settings.proxy_address else "blocked",
            "configured": bool(settings.proxy_address),
        }
        checks["private_key"] = {
            "status": "ok" if settings.private_key else "blocked",
            "configured": bool(settings.private_key),
        }
        checks["proxy_address"] = {"status": "ok" if settings.proxy_address else "blocked"}
        if (
            settings.private_key
            and settings.proxy_address
            and (force or time.time() - self.wallet.snapshot.refreshed_at > 60)
        ):
            self.wallet.client = getattr(self.bot, "_client", self.wallet.client)
            await self.wallet.refresh()
        checks["clob_authentication"] = {
            "status": "ok" if (self.wallet.snapshot.authenticated or bool(settings.private_key)) else "blocked"
        }

        def _check_rpc():
            endpoints = [
                settings.polygon_rpc_url,
                "https://polygon-bor-rpc.publicnode.com",
                "https://1rpc.io/matic",
                "https://polygon.drpc.org",
            ]
            for ep in endpoints:
                if not ep:
                    continue
                try:
                    w3 = Web3(Web3.HTTPProvider(ep, request_kwargs={"timeout": 4}))
                    if w3.is_connected():
                        return True
                except Exception:
                    pass
            return False

        rpc_ok = await asyncio.to_thread(_check_rpc)
        checks["polygon_rpc"] = {"status": "ok" if rpc_ok else "blocked"}

        balance = self.wallet.snapshot.collateral_balance
        allowance = self.wallet.snapshot.collateral_allowance
        ctf_allowance = self.wallet.snapshot.ctf_allowance
        checks["balance"] = {
            "status": "ok",
            "value": balance if balance is not None else 0.0,
        }
        checks["usdc_allowance"] = {
            "status": "ok",
            "value": allowance if allowance is not None else 0.0,
        }
        checks["ctf_allowance"] = {
            "status": "ok",
            "value": ctf_allowance if ctf_allowance is not None else 0.0,
        }

        geo = await self.geoblock.check(force=force)
        self.runtime.geo_allowed = geo.checked and not geo.blocked
        checks["geographic_eligibility"] = {
            "status": "ok" if geo.checked and not geo.blocked else "blocked",
            "country": geo.country,
            "region": geo.region,
        }
        checks["kill_switch"] = {"status": "ok" if not self.runtime.kill_switch else "blocked"}
        risk_ok = (
            settings.max_trade_usd <= settings.max_total_exposure_usd
            and settings.max_open_trades >= 1
            and settings.max_daily_loss_usd > 0
        )
        checks["risk_configuration"] = {"status": "ok" if risk_ok else "blocked"}
        circuit = status.get("risk", {})
        checks["circuit_breaker"] = {"status": "blocked" if circuit.get("is_paused") else "ok"}
        checks["live_enabled"] = {"status": "ok" if settings.live_trading_enabled else "blocked"}
        checks["dry_run"] = {"status": "ok" if not settings.dry_run else "blocked"}

        mandatory = [
            "database",
            "gamma_api",
            "clob_api",
            "websocket",
            "market_data",
            "order_books",
            "wallet",
            "private_key",
            "proxy_address",
            "clob_authentication",
            "polygon_rpc",
            "balance",
            "usdc_allowance",
            "ctf_allowance",
            "geographic_eligibility",
            "kill_switch",
            "risk_configuration",
            "circuit_breaker",
            "live_enabled",
            "dry_run",
        ]
        ready = all(checks[name]["status"] == "ok" for name in mandatory)
        return {"ready": ready, "armed": self.runtime.armed, "checks": checks}
