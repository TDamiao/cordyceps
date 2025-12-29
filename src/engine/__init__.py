"""Engine module for arbitrage detection and signal generation."""

from src.engine.detector import (
    ArbitrageConfig,
    ArbitrageEngine,
    ArbitrageOpportunity,
    SignalType,
    calculate_price_sum,
    is_buy_opportunity,
    is_sell_opportunity,
)

__all__ = [
    "ArbitrageConfig",
    "ArbitrageEngine",
    "ArbitrageOpportunity",
    "SignalType",
    "calculate_price_sum",
    "is_buy_opportunity",
    "is_sell_opportunity",
]
