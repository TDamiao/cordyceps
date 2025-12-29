"""
Configuration module for Polymarket Arbitrage Bot.

Loads settings from environment variables with validation using Pydantic.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # =========================================================================
    # Authentication
    # =========================================================================
    private_key: str = Field(
        ...,
        description="EOA private key for L1 authentication (hex string starting with 0x)",
    )
    proxy_address: str = Field(
        ...,
        description="Polymarket Proxy Wallet Address (Gnosis Safe)",
    )

    # Optional L2 credentials (auto-derived if not set)
    clob_api_key: str | None = Field(default=None, description="CLOB API Key")
    clob_api_secret: str | None = Field(default=None, description="CLOB API Secret")
    clob_api_passphrase: str | None = Field(default=None, description="CLOB API Passphrase")

    # =========================================================================
    # Network Configuration
    # =========================================================================
    polygon_rpc_url: str = Field(
        default="https://polygon-rpc.com",
        description="Polygon RPC URL for contract interactions",
    )
    chain_id: int = Field(
        default=137,
        description="Chain ID (137 = Polygon Mainnet)",
    )

    # =========================================================================
    # Trading Configuration
    # =========================================================================
    min_profit_threshold: float = Field(
        default=0.005,
        ge=0.0,
        le=0.5,
        description="Minimum profit threshold as decimal (0.005 = 0.5%)",
    )
    max_position_size: float = Field(
        default=100.0,
        gt=0.0,
        description="Maximum position size per trade in USDC",
    )
    dry_run: bool = Field(
        default=True,
        description="If True, log trades without executing",
    )
    
    # =========================================================================
    # HFT / Atomic Merge Settings
    # =========================================================================
    use_atomic_merge: bool = Field(
        default=True,
        description="If True, use mergePositions() for instant profit capture",
    )
    max_gas_price_gwei: float = Field(
        default=100.0,
        gt=0.0,
        description="Maximum gas price in gwei for merge transactions",
    )
    min_profit_vs_gas_ratio: float = Field(
        default=2.0,
        ge=1.0,
        description="Minimum ratio of profit to gas cost (e.g., 2.0 = profit must be 2x gas)",
    )

    # =========================================================================
    # Logging
    # =========================================================================
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Logging level",
    )
    log_format: Literal["json", "console"] = Field(
        default="console",
        description="Log output format",
    )

    # =========================================================================
    # Validators
    # =========================================================================
    @field_validator("private_key")
    @classmethod
    def validate_private_key(cls, v: str) -> str:
        """Validate private key format."""
        if not v.startswith("0x"):
            raise ValueError("Private key must start with '0x'")
        if len(v) != 66:  # 0x + 64 hex chars
            raise ValueError("Private key must be 66 characters (0x + 64 hex)")
        try:
            int(v, 16)
        except ValueError:
            raise ValueError("Private key must be valid hexadecimal")
        return v

    @field_validator("proxy_address")
    @classmethod
    def validate_proxy_address(cls, v: str) -> str:
        """Validate Ethereum address format."""
        if not v.startswith("0x"):
            raise ValueError("Proxy address must start with '0x'")
        if len(v) != 42:  # 0x + 40 hex chars
            raise ValueError("Proxy address must be 42 characters (0x + 40 hex)")
        try:
            int(v, 16)
        except ValueError:
            raise ValueError("Proxy address must be valid hexadecimal")
        return v


# =============================================================================
# API Endpoints (Constants)
# =============================================================================

class Endpoints:
    """Polymarket API endpoints."""

    # CLOB API
    CLOB_HOST = "https://clob.polymarket.com"
    CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # Gamma Markets API
    GAMMA_API = "https://gamma-api.polymarket.com"

    # Data API
    DATA_API = "https://data-api.polymarket.com"


# =============================================================================
# Contract Addresses (Polygon Mainnet)
# =============================================================================

class Contracts:
    """Smart contract addresses on Polygon."""

    # USDC on Polygon
    USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

    # Conditional Token Framework (CTF)
    CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

    # CTF Exchange
    CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8ED4093"

    # NegRisk Adapter
    NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

    # NegRisk CTF Exchange
    NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

    # Proxy Wallet Factory
    PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"


# =============================================================================
# Trading Constants
# =============================================================================

class TradingConfig:
    """Trading-related constants."""

    # Fee structure (as of documentation)
    TAKER_FEE = 0.0001  # 0.01% (1 basis point)
    MAKER_FEE = 0.0     # 0% for makers

    # Order types
    ORDER_TYPE_GTC = "GTC"  # Good Till Cancelled
    ORDER_TYPE_FOK = "FOK"  # Fill Or Kill
    ORDER_TYPE_GTD = "GTD"  # Good Till Date

    # Signature types
    SIGNATURE_TYPE_EOA = 0      # Direct EOA signature
    SIGNATURE_TYPE_POLY = 1     # Polymarket Proxy Wallet

    # Binary market partition (Yes/No)
    BINARY_PARTITION = [1, 2]


# =============================================================================
# Singleton Settings Instance
# =============================================================================

@lru_cache
def get_settings() -> Settings:
    """
    Get cached settings instance.

    Uses lru_cache to ensure settings are loaded only once.
    """
    return Settings()
