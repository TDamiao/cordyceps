#!/usr/bin/env python3
"""
Polymarket Arbitrage Bot - Main Entry Point

Orchestrates all components: observer, engine, executor, and settlement.
"""

import asyncio
import signal
import sys
from pathlib import Path
from typing import Optional

from src.client import PolymarketClient, authenticate
from src.config import get_settings
from src.engine import ArbitrageEngine, ArbitrageOpportunity
from src.execution import OrderExecutor, RateLimiter
from src.markets import MarketFetcher
from src.observer import MarketObserver
from src.settlement import SettlementAgent, PositionMonitor
from src.utils.logging import get_logger, setup_logging
from src.utils.metrics import MetricsTracker, HealthMonitor

logger = get_logger(__name__)


class ArbitrageBot:
    """
    Main arbitrage bot orchestrator.

    Coordinates all subsystems:
    - Market Observer: Real-time orderbook data via WebSocket
    - Arbitrage Engine: Opportunity detection
    - Order Executor: Trade execution
    - Settlement Agent: Capital recycling
    """

    def __init__(self):
        """Initialize the arbitrage bot."""
        self._settings = get_settings()
        setup_logging()

        # Initialize components
        self._client: Optional[PolymarketClient] = None
        self._observer: Optional[MarketObserver] = None
        self._engine = ArbitrageEngine()
        self._executor: Optional[OrderExecutor] = None
        self._settlement: Optional[SettlementAgent] = None
        self._position_monitor: Optional[PositionMonitor] = None

        # Metrics and monitoring
        self._metrics = MetricsTracker(
            persist_path=Path("data/trades.json"),
        )
        self._health = HealthMonitor(self._metrics)

        # Market data
        self._fetcher = MarketFetcher()
        self._active_markets: dict[str, list[str]] = {}

        # Control
        self._running = False
        self._shutdown_event = asyncio.Event()

    async def initialize(self) -> bool:
        """
        Initialize all components.

        Returns:
            True if initialization succeeded
        """
        try:
            logger.info("Initializing arbitrage bot...")

            # Create authenticated client
            auth_result = authenticate()
            self._client = PolymarketClient(auth_result)

            # Initialize observer with callbacks
            self._observer = MarketObserver(
                on_book_update=self._on_book_update,
                on_opportunity=self._on_opportunity_detected,
            )

            # Initialize executor with rate limiting
            rate_limiter = RateLimiter()
            self._executor = OrderExecutor(self._client, rate_limiter)

            # Initialize settlement (only if not dry run)
            if not self._settings.dry_run:
                self._settlement = SettlementAgent(
                    private_key=self._settings.private_key,
                    dry_run=False,
                )
                self._position_monitor = PositionMonitor(self._settlement)
            else:
                logger.info("Settlement disabled in dry-run mode")

            logger.info(
                "Bot initialized",
                address=auth_result.eoa_address,
                dry_run=self._settings.dry_run,
            )
            return True

        except Exception as e:
            logger.error("Failed to initialize bot", error=str(e))
            return False

    async def start(self) -> None:
        """Start the bot."""
        if not await self.initialize():
            logger.error("Initialization failed, exiting")
            return

        self._running = True
        self._health.set_websocket_status(False)

        try:
            # Fetch active markets
            logger.info("Fetching active markets...")
            markets = await self._fetcher.fetch_markets(
                active_only=False,  # Include all markets for now
                binary_only=True,
            )

            if not markets:
                logger.warning("No active markets found")
                return

            logger.info("Found active markets", count=len(markets))

            # Start observer first before subscribing
            observer_task = asyncio.create_task(self._run_observer())
            
            # Give WebSocket time to connect
            await asyncio.sleep(1)

            # Subscribe to markets
            for market in markets[:50]:  # Limit to top 50 markets
                await self._observer.add_market(market.condition_id, market.token_ids)
                self._active_markets[market.condition_id] = market.token_ids

                # Add to position monitor
                if self._position_monitor:
                    self._position_monitor.add_market(
                        market.condition_id,
                        market.token_ids,
                    )

            # Start subsystems
            tasks = [observer_task]

            if self._position_monitor:
                tasks.append(asyncio.create_task(self._position_monitor.start()))

            # Wait for shutdown
            await self._shutdown_event.wait()

            # Cancel tasks
            for task in tasks:
                task.cancel()

        except Exception as e:
            logger.error("Bot error", error=str(e))
            self._health.record_error(str(e))
        finally:
            await self.stop()

    async def _run_observer(self) -> None:
        """Run the market observer."""
        try:
            self._health.set_websocket_status(True)
            await self._observer.run()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Observer error", error=str(e))
            self._health.record_error(str(e))
            self._health.set_websocket_status(False)

    def _on_book_update(self, token_id: str, book) -> None:
        """Handle order book update."""
        # The MarketObserver already updates state manager
        pass

    async def _on_opportunity_detected(
        self,
        condition_id: str,
        order_books: dict,
    ) -> None:
        """
        Handle detected arbitrage opportunity.

        Args:
            condition_id: Market condition ID
            order_books: Order books for all outcomes
        """
        if not self._running:
            return

        try:
            # Analyze for opportunity
            opportunity = self._engine.analyze_market(condition_id, order_books)

            if not opportunity:
                return

            logger.info(
                "Opportunity detected",
                market_id=condition_id,
                type=opportunity.signal_type.value,
                profit=str(opportunity.net_profit),
            )

            # Execute if we have an executor
            if self._executor:
                from src.execution import execute_arbitrage
                result = await execute_arbitrage(self._client, opportunity)

                # Record trade
                self._metrics.create_trade_record(
                    market_id=condition_id,
                    signal_type=opportunity.signal_type.value,
                    token_ids=opportunity.token_ids,
                    size=opportunity.max_size,
                    total_cost=opportunity.total_cost,
                    expected_profit=opportunity.net_profit,
                    realized_profit=result.realized_profit,
                    fees=opportunity.fees,
                    success=result.success,
                    execution_time_ms=result.execution_time_ms,
                )

        except Exception as e:
            logger.error(
                "Failed to handle opportunity",
                market_id=condition_id,
                error=str(e),
            )
            self._health.record_error(str(e))

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        logger.info("Stopping arbitrage bot...")
        self._running = False

        if self._observer:
            await self._observer.stop()

        if self._position_monitor:
            self._position_monitor.stop()

        await self._fetcher.close()

        self._health.set_websocket_status(False)
        logger.info("Bot stopped")

    def get_status(self) -> dict:
        """Get current bot status."""
        return {
            "running": self._running,
            "health": self._health.get_health().to_dict(),
            "engine_stats": self._engine.stats,
            "executor_stats": self._executor.stats if self._executor else {},
            "active_markets": len(self._active_markets),
        }

    def shutdown(self) -> None:
        """Trigger graceful shutdown."""
        self._shutdown_event.set()


def main():
    """Main entry point."""
    bot = ArbitrageBot()

    # Handle shutdown signals
    def signal_handler(sig, frame):
        logger.info("Shutdown signal received")
        bot.shutdown()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Run bot
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        pass

    logger.info("Arbitrage bot exited")


if __name__ == "__main__":
    main()
