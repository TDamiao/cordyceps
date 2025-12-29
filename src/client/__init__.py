"""Client module for Polymarket CLOB API interactions."""

from src.client.auth import (
    AuthenticatedClient,
    AuthenticationError,
    authenticate,
    authenticate_with_explicit_creds,
)
from src.client.clob_client import PolymarketClient
from src.client.models import (
    Market,
    MarketOutcome,
    MarketType,
    OrderBook,
    OrderBookLevel,
    OrderSide,
    OrderType,
    Position,
    TradeResult,
    TradingSignal,
)

__all__ = [
    # Auth
    "AuthenticatedClient",
    "AuthenticationError",
    "authenticate",
    "authenticate_with_explicit_creds",
    # Client
    "PolymarketClient",
    # Models
    "Market",
    "MarketOutcome",
    "MarketType",
    "OrderBook",
    "OrderBookLevel",
    "OrderSide",
    "OrderType",
    "Position",
    "TradeResult",
    "TradingSignal",
]
