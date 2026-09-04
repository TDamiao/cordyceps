"""Validated environment and runtime configuration for Cordyceps."""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, ClassVar, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server-owned settings. Secrets are never part of runtime config APIs."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "Cordyceps"
    environment: str = "production"
    port: int = Field(default=8000, ge=1, le=65535)
    trading_mode: Literal["paper", "live_test", "live"] = "paper"
    live_trading_enabled: bool = False
    kill_switch: bool = False
    dry_run: bool = True
    admin_token: str = ""
    github_client_id: str = ""
    github_key: str = ""
    github_redirect_uri: str = "https://cordyceps.tdamiao.com/login"
    github_allowed_user: str = "tdamiao"

    database_url: str = "sqlite:///./cordyceps.db"
    polygon_rpc_url: str = "https://polygon-bor-rpc.publicnode.com"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    geoblock_cache_seconds: int = Field(default=300, ge=30, le=3600)
    chain_id: int = 137
    signature_type: int = Field(default=1, ge=0, le=3)

    max_trade_usd: float = Field(default=1.0, gt=0, le=100_000)
    max_total_exposure_usd: float = Field(default=2.0, gt=0, le=1_000_000)
    max_daily_loss_usd: float = Field(default=1.0, gt=0, le=100_000)
    max_open_trades: int = Field(default=1, ge=1, le=100)
    min_profit_threshold: float = Field(default=0.005, ge=0, le=1)
    min_net_edge: float = Field(default=0.01, ge=0, le=1)
    min_net_profit_usd: float = Field(default=0.01, ge=0, le=100_000)
    max_slippage_pct: float = Field(default=0.005, gt=0, le=0.25)
    emergency_slippage_pct: float = Field(default=0.01, ge=0, le=0.25)
    orderbook_stale_ms: int = Field(default=3000, ge=100, le=300_000)
    min_trade_shares: float = Field(default=1.0, gt=0, le=1_000_000)
    max_position_size: float = Field(default=100.0, gt=0, le=1_000_000)
    max_leg_imbalance_usd: float = Field(default=1.0, ge=0, le=100_000)
    leg_timeout_ms: int = Field(default=2000, ge=100, le=60_000)
    leg_risk_buffer: float = Field(default=0.002, ge=0, le=0.25)
    circuit_breaker_failure_threshold: int = Field(default=3, ge=1, le=100)
    circuit_breaker_cooldown_minutes: int = Field(default=15, ge=1, le=1440)
    simulated_latency_ms: int = Field(default=250, ge=0, le=60_000)
    market_limit: int = Field(default=50, ge=1, le=500)
    scan_interval_seconds: float = Field(default=60.0, ge=1, le=3600)
    fee_fallback_rate: float = Field(default=0.072, gt=0, le=1)

    # --- Favorite Compounding Strategy ---
    enable_favorite_strategy: bool = Field(default=False)
    min_favorite_probability: float = Field(default=0.90, ge=0.80, le=0.99)
    min_favorite_price: float = Field(default=0.85, ge=0.70, le=0.95)
    max_favorite_price: float = Field(default=0.98, ge=0.90, le=0.99)
    min_favorite_size_usd: float = Field(default=5.0, gt=0)
    favorite_take_profit: float = Field(default=0.97, ge=0.90, le=0.99)
    favorite_stop_loss: float = Field(default=0.80, ge=0.50, le=0.90)
    max_favorite_exposure_pct: float = Field(default=0.30, ge=0.10, le=0.50)
    favorite_kelly_fraction: float = Field(default=0.25, ge=0.05, le=1.0)

    # --- Telegram Notifications ---
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    private_key: str = ""
    proxy_address: str = ""
    polymarket_api_key: str = Field(default="", validation_alias="CLOB_API_KEY")
    polymarket_api_secret: str = Field(default="", validation_alias="CLOB_API_SECRET")
    polymarket_api_passphrase: str = Field(default="", validation_alias="CLOB_API_PASSPHRASE")

    use_atomic_merge: bool = False
    max_gas_price_gwei: float = Field(default=100, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    RUNTIME_FIELDS: ClassVar[tuple[str, ...]] = (
        "max_trade_usd",
        "max_total_exposure_usd",
        "max_daily_loss_usd",
        "max_open_trades",
        "min_profit_threshold",
        "min_net_edge",
        "min_net_profit_usd",
        "max_slippage_pct",
        "orderbook_stale_ms",
        "min_trade_shares",
        "max_leg_imbalance_usd",
        "leg_timeout_ms",
        "circuit_breaker_failure_threshold",
        "circuit_breaker_cooldown_minutes",
        "simulated_latency_ms",
        "market_limit",
        "scan_interval_seconds",
        "enable_favorite_strategy",
        "min_favorite_probability",
        "min_favorite_price",
        "max_favorite_price",
        "min_favorite_size_usd",
        "favorite_take_profit",
        "favorite_stop_loss",
        "max_favorite_exposure_pct",
        "favorite_kelly_fraction",
    )

    @field_validator("private_key")
    @classmethod
    def validate_private_key(cls, value: str) -> str:
        if not value:
            return value
        if not value.startswith("0x") or len(value) != 66:
            raise ValueError("Private key must be 0x followed by 64 hexadecimal characters")
        try:
            int(value[2:], 16)
        except ValueError as exc:
            raise ValueError("Private key must be hexadecimal") from exc
        return value

    @field_validator("proxy_address")
    @classmethod
    def validate_proxy_address(cls, value: str) -> str:
        if not value:
            return value
        if not value.startswith("0x") or len(value) != 42:
            raise ValueError("Proxy address must be a 20-byte hex address")
        try:
            int(value[2:], 16)
        except ValueError as exc:
            raise ValueError("Proxy address must be hexadecimal") from exc
        return value

    @model_validator(mode="after")
    def validate_consistency(self) -> Settings:
        if self.max_trade_usd > self.max_total_exposure_usd:
            raise ValueError("MAX_TRADE_USD must be <= MAX_TOTAL_EXPOSURE_USD")
        if self.max_leg_imbalance_usd > self.max_total_exposure_usd:
            raise ValueError("MAX_LEG_IMBALANCE_USD must be <= MAX_TOTAL_EXPOSURE_USD")
        if self.emergency_slippage_pct < self.max_slippage_pct:
            raise ValueError("EMERGENCY_SLIPPAGE_PCT must be >= MAX_SLIPPAGE_PCT")
        if self.trading_mode == "paper" and self.live_trading_enabled:
            raise ValueError("LIVE_TRADING_ENABLED must be false in paper mode")
        if self.trading_mode == "live":
            if not self.live_trading_enabled:
                raise ValueError("LIVE_TRADING_ENABLED=true is required in live mode")
            if not self.private_key:
                raise ValueError("PRIVATE_KEY is required in live mode")
            if not self.proxy_address:
                raise ValueError("PROXY_ADDRESS is required in live mode")
            if self.dry_run:
                raise ValueError("DRY_RUN must be false in live mode")
        return self

    def runtime_values(self) -> dict[str, int | float]:
        return {name: getattr(self, name) for name in self.RUNTIME_FIELDS}

    def with_runtime_overrides(self, values: dict[str, Any]) -> Settings:
        allowed = {key: value for key, value in values.items() if key in self.RUNTIME_FIELDS}
        for key, value in allowed.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"{key} must be finite")
        return self.__class__.model_validate({**self.model_dump(), **allowed})

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
    def polygon_rpc(self) -> str:
        return self.polygon_rpc_url


class Endpoints:
    CLOB_HOST = "https://clob.polymarket.com"
    CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    GAMMA_API = "https://gamma-api.polymarket.com"
    DATA_API = "https://data-api.polymarket.com"


class Contracts:
    """Stable V2 protocol addresses used by read-only checks."""

    PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    USDC = PUSD  # Backward-compatible name; V2 collateral is pUSD, not USDC.e.
    CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"


class TradingConfig:
    ORDER_TYPE_GTC = "GTC"
    ORDER_TYPE_FOK = "FOK"
    ORDER_TYPE_GTD = "GTD"
    SIGNATURE_TYPE_EOA = 0
    SIGNATURE_TYPE_POLY = 1
    BINARY_PARTITION = [1, 2]


@lru_cache
def get_settings() -> Settings:
    return Settings()
