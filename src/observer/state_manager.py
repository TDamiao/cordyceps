"""
State manager for maintaining local order book mirrors.

Maintains real-time synchronized order books for multiple markets.
"""

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional

from src.client.models import OrderBook, OrderBookLevel
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class MarketState:
    """State for a single market (all outcomes)."""

    condition_id: str
    token_ids: list[str]
    order_books: dict[str, OrderBook] = field(default_factory=dict)
    last_update: float = 0.0

    @property
    def is_complete(self) -> bool:
        """Check if we have order books for all tokens."""
        return all(tid in self.order_books for tid in self.token_ids)


class StateManager:
    """
    Manages local order book state for multiple markets.

    Receives updates from WebSocket and maintains synchronized order books.
    Provides callbacks when significant changes occur.
    """

    def __init__(
        self,
        on_book_update: Optional[Callable[[str, OrderBook], None]] = None,
        on_arb_opportunity: Optional[Callable[[str, dict[str, OrderBook]], None]] = None,
    ):
        """
        Initialize state manager.

        Args:
            on_book_update: Callback when any order book updates
            on_arb_opportunity: Callback when a market has all books ready
        """
        self.on_book_update = on_book_update
        self.on_arb_opportunity = on_arb_opportunity

        self._markets: dict[str, MarketState] = {}
        self._token_to_market: dict[str, str] = {}
        self._order_books: dict[str, OrderBook] = {}
        self._lock = asyncio.Lock()

    @property
    def markets(self) -> dict[str, MarketState]:
        """Get all tracked markets."""
        return self._markets.copy()

    @property
    def order_books(self) -> dict[str, OrderBook]:
        """Get all order books."""
        return self._order_books.copy()

    def register_market(self, condition_id: str, token_ids: list[str]) -> None:
        """
        Register a market to track.

        Args:
            condition_id: Market condition ID
            token_ids: List of token IDs for this market
        """
        if condition_id in self._markets:
            logger.debug("Market already registered", condition_id=condition_id)
            return

        self._markets[condition_id] = MarketState(
            condition_id=condition_id,
            token_ids=token_ids,
        )

        for tid in token_ids:
            self._token_to_market[tid] = condition_id
            self._order_books[tid] = OrderBook(token_id=tid)

        logger.info(
            "Market registered",
            condition_id=condition_id,
            tokens=len(token_ids),
        )

    def unregister_market(self, condition_id: str) -> None:
        """
        Unregister a market from tracking.

        Args:
            condition_id: Market condition ID
        """
        if condition_id not in self._markets:
            return

        market = self._markets.pop(condition_id)

        for tid in market.token_ids:
            self._token_to_market.pop(tid, None)
            self._order_books.pop(tid, None)

        logger.info("Market unregistered", condition_id=condition_id)

    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """
        Get order book for a token.

        Args:
            token_id: Token ID

        Returns:
            OrderBook or None if not tracked
        """
        return self._order_books.get(token_id)

    def get_market_books(self, condition_id: str) -> Optional[dict[str, OrderBook]]:
        """
        Get all order books for a market.

        Args:
            condition_id: Market condition ID

        Returns:
            Dict of token_id -> OrderBook, or None if market not tracked
        """
        market = self._markets.get(condition_id)
        if not market:
            return None

        return {
            tid: self._order_books.get(tid, OrderBook(token_id=tid))
            for tid in market.token_ids
        }

    def handle_book_update(self, token_id: str, data: dict) -> None:
        """
        Handle an order book update from WebSocket.

        Args:
            token_id: Token ID being updated
            data: Raw update data from WebSocket
        """
        if token_id not in self._order_books:
            logger.debug("Received update for untracked token", token_id=token_id)
            return

        try:
            order_book = self._parse_book_update(token_id, data)
            self._order_books[token_id] = order_book

            # Update market state
            condition_id = self._token_to_market.get(token_id)
            if condition_id and condition_id in self._markets:
                market = self._markets[condition_id]
                market.order_books[token_id] = order_book
                market.last_update = time.time()

                # Check if market is complete and notify
                if market.is_complete and self.on_arb_opportunity:
                    # Schedule async callback properly
                    import asyncio
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.on_arb_opportunity(condition_id, market.order_books.copy()))
                    except RuntimeError:
                        # No event loop, skip async callback
                        pass

            # Notify book update callback
            if self.on_book_update:
                self.on_book_update(token_id, order_book)

            logger.debug(
                "Order book updated",
                token_id=token_id[:16],
                bids=len(order_book.bids),
                asks=len(order_book.asks),
            )

        except Exception as e:
            logger.error(
                "Failed to process book update",
                token_id=token_id,
                error=str(e),
            )

    def _parse_book_update(self, token_id: str, data: dict) -> OrderBook:
        """
        Parse order book data from WebSocket message.

        Args:
            token_id: Token ID
            data: Raw data from WebSocket

        Returns:
            Parsed OrderBook
        """
        bids = []
        asks = []

        # Handle different message formats
        if "bids" in data:
            for bid in data.get("bids", []):
                bids.append(self._parse_level(bid))

        if "asks" in data:
            for ask in data.get("asks", []):
                asks.append(self._parse_level(ask))

        # Handle price_change format
        if "changes" in data:
            for change in data.get("changes", []):
                side = change.get("side", "").upper()
                level = self._parse_level(change)

                if side == "BUY":
                    bids.append(level)
                elif side == "SELL":
                    asks.append(level)

        # Sort and filter zero-size levels
        bids = sorted([b for b in bids if b.size > 0], key=lambda x: x.price, reverse=True)
        asks = sorted([a for a in asks if a.size > 0], key=lambda x: x.price)

        return OrderBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
            timestamp=int(time.time() * 1000),
        )

    def _parse_level(self, data: dict) -> OrderBookLevel:
        """Parse a single order book level."""
        price = Decimal(str(data.get("price", "0")))
        size = Decimal(str(data.get("size", data.get("amount", "0"))))
        return OrderBookLevel(price=price, size=size)

    def get_all_tracked_tokens(self) -> list[str]:
        """Get list of all tracked token IDs."""
        return list(self._order_books.keys())

    def clear(self) -> None:
        """Clear all state."""
        self._markets.clear()
        self._token_to_market.clear()
        self._order_books.clear()
        logger.info("State cleared")


class MarketObserver:
    """
    High-level market observer that combines WebSocket and state management.

    Provides a simple interface to:
    1. Subscribe to markets
    2. Receive real-time order book updates
    3. Monitor for arbitrage opportunities
    """

    def __init__(
        self,
        on_book_update: Optional[Callable[[str, OrderBook], None]] = None,
        on_opportunity: Optional[Callable[[str, dict[str, OrderBook]], None]] = None,
    ):
        """
        Initialize market observer.

        Args:
            on_book_update: Callback for order book changes
            on_opportunity: Callback when market is ready for arb check
        """
        from src.observer.websocket import MarketWebSocket

        self._state = StateManager(
            on_book_update=on_book_update,
            on_arb_opportunity=on_opportunity,
        )

        self._ws = MarketWebSocket(
            on_book_update=self._state.handle_book_update,
        )

        self._running = False

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._ws.is_connected

    @property
    def state(self) -> StateManager:
        """Access the state manager."""
        return self._state

    async def add_market(self, condition_id: str, token_ids: list[str]) -> None:
        """
        Add a market to observe.

        Args:
            condition_id: Market condition ID
            token_ids: List of token IDs for market outcomes
        """
        self._state.register_market(condition_id, token_ids)
        await self._ws.subscribe(token_ids)

        logger.info(
            "Observing market",
            condition_id=condition_id,
            tokens=len(token_ids),
        )

    def remove_market(self, condition_id: str) -> None:
        """
        Stop observing a market.

        Args:
            condition_id: Market condition ID
        """
        self._state.unregister_market(condition_id)

    async def start(self) -> None:
        """Start the observer."""
        self._running = True
        await self._ws.start()

    async def stop(self) -> None:
        """Stop the observer."""
        self._running = False
        await self._ws.stop()

    async def run(self) -> None:
        """Run the observer (blocking)."""
        await self.start()
        try:
            await self._ws.listen()
        finally:
            await self.stop()
