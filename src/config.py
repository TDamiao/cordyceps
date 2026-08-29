"""Configuration for Cordyceps - Polymarket Arbitrage Engine.

This module keeps compatibility with the existing codebase while adding:
- explicit paper/live mode guard
- risk limits
- database URL
- API endpoints
- no private key requirement for paper mode
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------------------------------------------------------------
    # App / mode
    # ---------------------------------------------------------------------
    app_name: str = Field(default="Cordyceps")
    environment: str = Field(default="production")
    port: int = Field(default=8000, ge=1, le=65535)

    trading_mode: Literal["paper", "live"] = Field(default="paper")
    live_trading_enabled: bool = Field(default=False)
    kill_switch: bool = Field(default=False)
    dry_run: bool = Field(default=True)

    # ---------------------------------------------------------------------
    # Persistence / endpoints
    # ---------------------------------------------------------------------
    database_url: str = Field(default="sqlite:///./cordyceps.db")
    polygon_rpc_url: str = Field(default="https://polygon-rpc.com")
    gamma_api_url: str = Field(default="https://gamma-api.polymarket.com")
    clob_api_url: str = Field(default="https://clob.polymarket.com")
    clob_ws_url: str = Field(default="wss://ws-subscriptions-clob.polymarket.com/ws/market")
    chain_id: int = Field(default=137)

    # ---------------------------------------------------------------------
    # Risk management
    # ---------------------------------------------------------------------
    max_trade_usd: float = Field(default=1.0, gt=0)
    max_total_exposure_usd: float = Field(default=5.0, gt=0)
    max_daily_loss_usd: float = Field(default=1.0, gt=0)
    max_slippage_pct: float = Field(default=0.005, ge=0)
    max_position_size: float = Field(default=100.0, gt=0)
    min_profit_threshold: float = Field(default=0.005, ge=0.0)
    # Trade-quality limits
    min_trade_shares: float = Field(default=1.0, gt=0)
    min_net_edge: float = Field(default=0.01, ge=0)
    min_net_profit_usd: float = Field(default=0.01, ge=0)
    # Book-freshness guard
    orderbook_stale_ms: int = Field(default=3000, ge=0)
    simulated_latency_ms: int = Field(default=250, ge=0)
    circuit_breaker_failure_threshold: int = Field(default=5, ge=1)
    circuit_breaker_cooldown_minutes: int = Field(default=15, ge=1)

    # ---------------------------------------------------------------------
    # Optional auth material
    # ---------------------------------------------------------------------
    private_key: str = Field(default="")
    proxy_address: str = Field(default="")
    polymarket_api_key: str = Field(default="")
    polymarket_api_secret: str = Field(default="")
    polymarket_api_passphrase: str = Field(default="")

    # ---------------------------------------------------------------------
    # Logging
    # ---------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    log_format: Literal["json", "console"] = Field(default="console")

    @model_validator(mode="after")
    def validate_mode(self):
        """Enforce live-mode safety guard."""
        if self.trading_mode == "live":
            if not self.live_trading_enabled:
                raise ValueError("LIVE_TRADING_ENABLED=true is required when TRADING_MODE=live")
            if not self.private_key:
                raise ValueError("PRIVATE_KEY is required when TRADING_MODE=live")
            if not self.proxy_address:
                raise ValueError("PROXY_ADDRESS is required when TRADING_MODE=live")
            if self.dry_run:
                raise ValueError("DRY_RUN must be false when TRADING_MODE=live")
        else:
            if self.live_trading_enabled:
                raise ValueError("LIVE_TRADING_ENABLED must be false when TRADING_MODE=paper")
        return self

    @field_validator("private_key")
    @classmethod
    def validate_private_key(cls, v: str) -> str:
        """Validate private key format when supplied."""
        if not v:
            return v
        if not v.startswith("0x"):
            raise ValueError("Private key must start with '0x'")
        if len(v) != 66:
            raise ValueError("Private key must be 66 characters (0x + 64 hex)")
        try:
            int(v, 16)
        except ValueError as exc:
            raise ValueError("Private key must be valid hexadecimal") from exc
        return v

    @field_validator("proxy_address")
    @classmethod
    def validate_proxy_address(cls, v: str) -> str:
        """Validate Ethereum address format when supplied."""
        if not v:
            return v
        if not v.startswith("0x"):
            raise ValueError("Proxy address must start with '0x'")
        if len(v) != 42:
            raise ValueError("Proxy address must be 42 characters (0x + 40 hex)")
        try:
            int(v, 16)
        except ValueError as exc:
            raise ValueError("Proxy address must be valid hexadecimal") from exc
        return v

    # ------------------------------------------------------------------
    # Compatibility aliases used by the existing codebase
    # ------------------------------------------------------------------
    @property
    def max_daily_loss(self) -> float:
        return self.max_daily_loss_usd

    @property
    def max_slippage_tolerance(self) -> float:
        return self.max_slippage_pct

    @property
    def max_position_size_usd(self) -> float:
        return self.max_position_size

    @property
    def circuit_breaker_failure_threshold_value(self) -> int:
        return self.circuit_breaker_failure_threshold

    @property
    def circuit_breaker_cooldown_minutes_value(self) -> int:
        return self.circuit_breaker_cooldown_minutes

    @property
    def polygon_rpc(self) -> str:
        return self.polygon_rpc_url


class Endpoints:
    """Polymarket API endpoints."""

    CLOB_HOST = "https://clob.polymarket.com"
    CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    GAMMA_API = "https://gamma-api.polymarket.com"
    DATA_API = "https://data-api.polymarket.com"


class Contracts:
    """Smart contract addresses on Polygon."""

    USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8ED4093"
    NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
    NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
    PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"


class TradingConfig:
    """Trading-related constants."""

    TAKER_FEE = 0.0001
    MAKER_FEE = 0.0
    ORDER_TYPE_GTC = "GTC"
    ORDER_TYPE_FOK = "FOK"
    ORDER_TYPE_GTD = "GTD"
    SIGNATURE_TYPE_EOA = 0
    SIGNATURE_TYPE_POLY = 1
    BINARY_PARTITION = [1, 2]


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
