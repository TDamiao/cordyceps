"""
CLOB Client wrapper for Polymarket API.

Provides a high-level interface for trading operations.
"""

from decimal import Decimal
from typing import Any, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType as ClobOrderType

from src.client.auth import AuthenticatedClient, authenticate
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
)
from src.config import TradingConfig, get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


class PolymarketClient:
    """
    High-level client for Polymarket CLOB operations.

    Wraps the py-clob-client with additional functionality:
    - Automatic authentication
    - Type-safe models
    - Error handling
    - Logging
    """

    def __init__(self, auth_client: Optional[AuthenticatedClient] = None):
        """
        Initialize the Polymarket client.

        Args:
            auth_client: Pre-authenticated client, or None to auto-authenticate
        """
        if auth_client is None:
            auth_client = authenticate()

        self._auth = auth_client
        self._client = auth_client.client
        self._settings = get_settings()

        logger.info(
            "PolymarketClient initialized",
            eoa=self._auth.eoa_address,
            proxy=self._auth.proxy_address,
        )

    @property
    def client(self) -> ClobClient:
        """Access the underlying ClobClient."""
        return self._client

    @property
    def eoa_address(self) -> str:
        """Get the EOA (signing) address."""
        return self._auth.eoa_address

    @property
    def proxy_address(self) -> str:
        """Get the proxy (trading) address."""
        return self._auth.proxy_address

    # =========================================================================
    # Market Data
    # =========================================================================

    def get_order_book(self, token_id: str) -> OrderBook:
        """
        Fetch the order book for a token.

        Args:
            token_id: Token ID to fetch order book for

        Returns:
            OrderBook with bids and asks
        """
        try:
            raw = self._client.get_order_book(token_id)

            bids = [
                OrderBookLevel(
                    price=Decimal(str(b.price)),
                    size=Decimal(str(b.size)),
                )
                for b in (raw.bids or [])
            ]

            asks = [
                OrderBookLevel(
                    price=Decimal(str(a.price)),
                    size=Decimal(str(a.size)),
                )
                for a in (raw.asks or [])
            ]

            return OrderBook(
                token_id=token_id,
                bids=sorted(bids, key=lambda x: x.price, reverse=True),
                asks=sorted(asks, key=lambda x: x.price),
                timestamp=raw.timestamp if hasattr(raw, 'timestamp') else None,
            )

        except Exception as e:
            logger.error("Failed to fetch order book", token_id=token_id, error=str(e))
            return OrderBook(token_id=token_id)

    def get_order_books(self, token_ids: list[str]) -> dict[str, OrderBook]:
        """
        Fetch order books for multiple tokens.

        Args:
            token_ids: List of token IDs

        Returns:
            Dict mapping token_id to OrderBook
        """
        result = {}
        for token_id in token_ids:
            result[token_id] = self.get_order_book(token_id)
        return result

    def get_price(self, token_id: str) -> Optional[Decimal]:
        """
        Get the current mid price for a token.

        Args:
            token_id: Token ID

        Returns:
            Mid price or None if unavailable
        """
        try:
            raw = self._client.get_midpoint(token_id)
            return Decimal(str(raw.mid)) if raw and raw.mid else None
        except Exception as e:
            logger.warning("Failed to get price", token_id=token_id, error=str(e))
            return None

    # =========================================================================
    # Order Management
    # =========================================================================

    def create_order(
        self,
        token_id: str,
        side: OrderSide,
        price: Decimal,
        size: Decimal,
        order_type: OrderType = OrderType.GTC,
    ) -> Optional[str]:
        """
        Create and submit an order.

        Args:
            token_id: Token to trade
            side: BUY or SELL
            price: Limit price
            size: Order size
            order_type: GTC, FOK, or GTD

        Returns:
            Order ID if successful, None otherwise
        """
        if self._settings.dry_run:
            logger.info(
                "DRY RUN - Order not submitted",
                token_id=token_id,
                side=side.value,
                price=str(price),
                size=str(size),
            )
            return "dry_run_order_id"

        try:
            # Build order args
            order_args = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=side.value,
            )

            # Create signed order
            signed_order = self._client.create_order(order_args)

            # Submit order
            response = self._client.post_order(signed_order, order_type=order_type.value)

            order_id = response.get("orderID") if response else None

            logger.info(
                "Order submitted",
                order_id=order_id,
                token_id=token_id,
                side=side.value,
                price=str(price),
                size=str(size),
            )

            return order_id

        except Exception as e:
            logger.error(
                "Order submission failed",
                token_id=token_id,
                error=str(e),
            )
            return None

    def create_fok_order(
        self,
        token_id: str,
        side: OrderSide,
        price: Decimal,
        size: Decimal,
    ) -> Optional[str]:
        """
        Create a Fill-or-Kill order (for arbitrage execution).

        Args:
            token_id: Token to trade
            side: BUY or SELL
            price: Limit price
            size: Order size

        Returns:
            Order ID if successful, None otherwise
        """
        return self.create_order(
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            order_type=OrderType.FOK,
        )

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order.

        Args:
            order_id: Order ID to cancel

        Returns:
            True if cancelled successfully
        """
        try:
            self._client.cancel(order_id)
            logger.info("Order cancelled", order_id=order_id)
            return True
        except Exception as e:
            logger.error("Failed to cancel order", order_id=order_id, error=str(e))
            return False

    def cancel_all_orders(self) -> bool:
        """
        Cancel all open orders.

        Returns:
            True if all orders cancelled successfully
        """
        try:
            self._client.cancel_all()
            logger.info("All orders cancelled")
            return True
        except Exception as e:
            logger.error("Failed to cancel all orders", error=str(e))
            return False

    # =========================================================================
    # Account / Positions
    # =========================================================================

    def get_open_orders(self) -> list[dict[str, Any]]:
        """
        Get all open orders.

        Returns:
            List of open order dictionaries
        """
        try:
            return self._client.get_orders() or []
        except Exception as e:
            logger.error("Failed to get open orders", error=str(e))
            return []

    def get_trades(self) -> list[dict[str, Any]]:
        """
        Get recent trades.

        Returns:
            List of trade dictionaries
        """
        try:
            return self._client.get_trades() or []
        except Exception as e:
            logger.error("Failed to get trades", error=str(e))
            return []

    # =========================================================================
    # Utility
    # =========================================================================

    def get_server_time(self) -> int:
        """Get current server timestamp."""
        return self._client.get_server_time()

    def is_connected(self) -> bool:
        """Check if client can reach the server."""
        try:
            self.get_server_time()
            return True
        except Exception:
            return False
