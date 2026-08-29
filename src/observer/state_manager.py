"""
State manager for maintaining local order book mirrors.

Maintains real-time synchronized order books for multiple markets.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

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
        on_book_update: Callable[[str, OrderBook], None] | None = None,
        on_arb_opportunity: Callable[[str, dict[str, OrderBook]], None] | None = None,
    ):
        self.on_book_update = on_book_update
        self.on_arb_opportunity = on_arb_opportunity
        self._markets: dict[str, MarketState] = {}
        self._token_to_market: dict[str, str] = {}
        self._order_books: dict[str, OrderBook] = {}
        self._lock = asyncio.Lock()
        self._book_updates = 0
        self._complete_market_callbacks = 0
        self._untracked_updates = 0

    @property
    def markets(self) -> dict[str, MarketState]:
        return self._markets.copy()

    @property
    def order_books(self) -> dict[str, OrderBook]:
        return self._order_books.copy()

    @property
    def stats(self) -> dict:
        complete_markets = sum(1 for market in self._markets.values() if market.is_complete)
        books_with_liquidity = sum(
            1 for book in self._order_books.values() if book.bids or book.asks
        )
        return {
            "book_updates": self._book_updates,
            "complete_market_callbacks": self._complete_market_callbacks,
            "untracked_updates": self._untracked_updates,
            "tracked_tokens": len(self._order_books),
            "books_with_liquidity": books_with_liquidity,
            "complete_markets": complete_markets,
        }

    def register_market(self, condition_id: str, token_ids: list[str]) -> None:
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
        if condition_id not in self._markets:
            return

        market = self._markets.pop(condition_id)
        for tid in market.token_ids:
            self._token_to_market.pop(tid, None)
            self._order_books.pop(tid, None)

        logger.info("Market unregistered", condition_id=condition_id)

    def get_order_book(self, token_id: str) -> OrderBook | None:
        return self._order_books.get(token_id)

    def get_market_books(self, condition_id: str) -> dict[str, OrderBook] | None:
        market = self._markets.get(condition_id)
        if not market:
            return None

        return {
            tid: self._order_books.get(tid, OrderBook(token_id=tid)) for tid in market.token_ids
        }

    def handle_book_update(self, token_id: str, data: dict) -> None:
        if token_id not in self._order_books:
            self._untracked_updates += 1
            logger.debug("Received update for untracked token", token_id=token_id)
            return

        try:
            order_book = self._parse_book_update(token_id, data)
            self._order_books[token_id] = order_book
            self._book_updates += 1

            condition_id = self._token_to_market.get(token_id)
            if condition_id and condition_id in self._markets:
                market = self._markets[condition_id]
                market.order_books[token_id] = order_book
                market.last_update = time.time()

                if market.is_complete and self.on_arb_opportunity:
                    try:
                        loop = asyncio.get_running_loop()
                        self._complete_market_callbacks += 1
                        loop.create_task(
                            self.on_arb_opportunity(condition_id, market.order_books.copy())
                        )
                    except RuntimeError:
                        pass

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
        """Parse snapshots and incremental price changes."""
        if "bids" in data or "asks" in data:
            bids = [self._parse_level(bid) for bid in data.get("bids", [])]
            asks = [self._parse_level(ask) for ask in data.get("asks", [])]
        else:
            current = self._order_books.get(token_id, OrderBook(token_id=token_id))
            bids = list(current.bids)
            asks = list(current.asks)

        if "changes" in data:
            bid_by_price = {level.price: level for level in bids}
            ask_by_price = {level.price: level for level in asks}

            for change in data.get("changes", []):
                side = change.get("side", "").upper()
                level = self._parse_level(change)
                if side == "BUY":
                    levels = bid_by_price
                elif side == "SELL":
                    levels = ask_by_price
                else:
                    continue

                if level.size > 0:
                    levels[level.price] = level
                else:
                    levels.pop(level.price, None)

            bids = list(bid_by_price.values())
            asks = list(ask_by_price.values())

        bids = sorted([b for b in bids if b.size > 0], key=lambda x: x.price, reverse=True)
        asks = sorted([a for a in asks if a.size > 0], key=lambda x: x.price)

        timestamp = data.get("timestamp")
        try:
            timestamp_ms = int(timestamp) if timestamp is not None else int(time.time() * 1000)
        except (TypeError, ValueError):
            timestamp_ms = int(time.time() * 1000)

        return OrderBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
            timestamp=timestamp_ms,
        )

    def _parse_level(self, data: dict) -> OrderBookLevel:
        price = Decimal(str(data.get("price", "0")))
        size = Decimal(str(data.get("size", data.get("amount", "0"))))
        return OrderBookLevel(price=price, size=size)

    def get_all_tracked_tokens(self) -> list[str]:
        return list(self._order_books.keys())

    def clear(self) -> None:
        self._markets.clear()
        self._token_to_market.clear()
        self._order_books.clear()
        logger.info("State cleared")


class MarketObserver:
    """High-level market observer combining WebSocket and state management."""

    def __init__(
        self,
        on_book_update: Callable[[str, OrderBook], None] | None = None,
        on_opportunity: Callable[[str, dict[str, OrderBook]], None] | None = None,
    ):
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
        return self._ws.is_connected

    @property
    def state(self) -> StateManager:
        return self._state

    async def add_market(self, condition_id: str, token_ids: list[str]) -> None:
        self._state.register_market(condition_id, token_ids)
        await self._ws.subscribe(token_ids)
        logger.info(
            "Observing market",
            condition_id=condition_id,
            tokens=len(token_ids),
        )

    def remove_market(self, condition_id: str) -> None:
        self._state.unregister_market(condition_id)

    async def start(self) -> None:
        self._running = True
        await self._ws.start()

    async def stop(self) -> None:
        self._running = False
        await self._ws.stop()

    async def run(self) -> None:
        await self.start()
        try:
            await self._ws.listen()
        finally:
            await self.stop()
