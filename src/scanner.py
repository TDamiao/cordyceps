"""
Scanner - periodically scans markets for arbitrage opportunities.

Lightweight wrapper around the existing MarketObserver + engine. It:
  * fetches the latest market metadata from the Gamma API
  * subscribes the WebSocket consumer to new tokens
  * triggers periodic resync to keep order books fresh
"""

from __future__ import annotations

import asyncio
import time

from src.markets import MarketFetcher
from src.observer import MarketObserver
from src.utils.logging import get_logger

logger = get_logger(__name__)


class Scanner:
    """Periodically scan the marketplace for tradeable markets and resync."""

    def __init__(
        self,
        fetcher: MarketFetcher | None = None,
        observer: MarketObserver | None = None,
        scan_interval_seconds: float = 60.0,
        market_limit: int = 50,
    ) -> None:
        self._fetcher = fetcher or MarketFetcher()
        self._observer = observer
        self._interval = scan_interval_seconds
        self._market_limit = market_limit
        self._tracked: set[str] = set()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def attach_observer(self, observer: MarketObserver) -> None:
        self._observer = observer

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def scan_once(self) -> list[str]:
        """Run a single market scan and subscribe to any new markets.

        Returns:
            List of newly subscribed token IDs.
        """
        try:
            markets = await self._fetcher.fetch_markets(
                active_only=True,
                binary_only=True,
                limit=self._market_limit,
            )
        except Exception as exc:  # pragma: no cover - network error path
            logger.error("scanner.fetch_failed", error=str(exc))
            return []

        new_tokens: list[str] = []
        for market in markets[: self._market_limit]:
            if market.condition_id in self._tracked:
                continue
            if self._observer is None:
                self._tracked.add(market.condition_id)
                continue
            try:
                self._observer.state.register_market(
                    market.condition_id, market.token_ids
                )
                await self._observer._ws.subscribe(market.token_ids)
                new_tokens.extend(market.token_ids)
                self._tracked.add(market.condition_id)
                logger.info(
                    "scanner.subscribed",
                    condition_id=market.condition_id,
                    tokens=len(market.token_ids),
                )
            except Exception as exc:  # pragma: no cover - best-effort
                logger.warning(
                    "scanner.subscribe_failed",
                    condition_id=market.condition_id,
                    error=str(exc),
                )
        return new_tokens

    async def resync(self) -> None:
        """Force a resync of all currently tracked markets."""
        if self._observer is None:
            return
        for condition_id in list(self._tracked):
            books = self._observer.state.get_market_books(condition_id)
            if not books:
                continue
            token_ids = [tid for tid, book in books.items() if book.bids or book.asks]
            if token_ids:
                try:
                    await self._observer._ws.subscribe(token_ids)
                except Exception as exc:  # pragma: no cover - best-effort
                    logger.warning(
                        "scanner.resync_failed",
                        condition_id=condition_id,
                        error=str(exc),
                    )
        logger.info("scanner.resync_complete", markets=len(self._tracked))

    async def start(self) -> None:
        if self.is_running:
            return

        async def _loop() -> None:
            while not self._stop.is_set():
                start = time.time()
                try:
                    await self.scan_once()
                except Exception as exc:  # pragma: no cover - guard
                    logger.error("scanner.iteration_failed", error=str(exc))
                # Resync halfway between scans to refresh order books.
                await asyncio.sleep(self._interval / 2)
                try:
                    await self.resync()
                except Exception as exc:  # pragma: no cover - guard
                    logger.error("scanner.resync_failed", error=str(exc))
                elapsed = time.time() - start
                sleep_for = max(0.0, self._interval - elapsed)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                except TimeoutError:
                    pass

        self._task = asyncio.create_task(_loop(), name="cordyceps-scanner")
        logger.info("scanner.started", interval_seconds=self._interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass
        self._task = None
        try:
            await self._fetcher.close()
        except Exception:  # pragma: no cover
            pass
        logger.info("scanner.stopped")
