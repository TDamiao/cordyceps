#!/usr/bin/env python3
"""
Polymarket Arbitrage Bot - Main Entry Point

Orchestrates all components: observer, engine, executor, and settlement.
"""

import asyncio
import signal
from pathlib import Path

from src.client import PolymarketClient, authenticate
from src.config import get_settings
from src.engine import ArbitrageEngine
from src.execution import OrderExecutor, RateLimiter
from src.markets import MarketFetcher
from src.observer import MarketObserver
from src.settlement import PositionMonitor, SettlementAgent
from src.utils.logging import get_logger, setup_logging
from src.utils.metrics import HealthMonitor, MetricsTracker

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

        self._client: PolymarketClient | None = None
        self._observer: MarketObserver | None = None
        self._engine = ArbitrageEngine()
        self._executor: OrderExecutor | None = None
        self._settlement: SettlementAgent | None = None
        self._position_monitor: PositionMonitor | None = None

        self._metrics = MetricsTracker(
            persist_path=Path("data/trades.json"),
        )
        self._health = HealthMonitor(self._metrics)

        self._fetcher = MarketFetcher()
        self._active_markets: dict[str, list[str]] = {}

        self._running = False
        self._shutdown_event = asyncio.Event()

    async def initialize(self) -> bool:
        """Initialize all components."""
        try:
            logger.info(
                "Initializing arbitrage bot...",
                mode=self._settings.trading_mode,
            )

            # Paper mode must be completely wallet-free. Public CLOB market
            # data is enough for order books and simulated execution.
            if self._settings.trading_mode == "paper":
                self._client = PolymarketClient(public_only=True)
                logger.info("Paper mode: authentication skipped")
                auth_address = None
            else:
                auth_result = authenticate()
                self._client = PolymarketClient(auth_result)
                auth_address = auth_result.eoa_address

            self._observer = MarketObserver(
                on_book_update=self._on_book_update,
                on_opportunity=self._on_opportunity_detected,
            )

            rate_limiter = RateLimiter()

            from src.risk.manager import RiskManager

            risk_manager = RiskManager()

            # On-chain merge requires a signing key and must never initialize
            # in paper mode, even when USE_ATOMIC_MERGE=true.
            ctf_contract = None
            if self._settings.trading_mode == "live" and self._settings.use_atomic_merge:
                try:
                    from web3 import Web3

                    from src.contracts import CTF_ADDRESS, CTFContract

                    w3 = Web3(Web3.HTTPProvider(self._settings.polygon_rpc_url))

                    ctf_contract = CTFContract(
                        web3=w3,
                        private_key=self._settings.private_key,
                        ctf_address=CTF_ADDRESS,
                        proxy_address=self._settings.proxy_address,
                    )
                    logger.info("Atomic merge enabled with CTF contract")
                except Exception as e:
                    logger.error("Failed to initialize CTF contract", error=str(e))
                    logger.warning("Atomic merge disabled due to initialization error")
            elif self._settings.trading_mode == "paper":
                logger.info("Paper mode: atomic merge disabled")

            self._executor = OrderExecutor(
                client=self._client,
                rate_limiter=rate_limiter,
                ctf_contract=ctf_contract,
                risk_manager=risk_manager,
            )

            # Settlement is a live-only subsystem.
            if self._settings.trading_mode == "live" and not self._settings.dry_run:
                self._settlement = SettlementAgent(
                    private_key=self._settings.private_key,
                    dry_run=False,
                )
                self._position_monitor = PositionMonitor(self._settlement)
            else:
                logger.info("Settlement disabled", mode=self._settings.trading_mode)

            logger.info(
                "Bot initialized",
                address=auth_address,
                mode=self._settings.trading_mode,
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
            logger.info("Fetching active markets...")
            markets = await self._fetcher.fetch_markets(
                active_only=False,
                binary_only=True,
            )

            if not markets:
                logger.warning("No active markets found")
                return

            logger.info("Found active markets", count=len(markets))

            observer_task = asyncio.create_task(self._run_observer())

            await asyncio.sleep(1)

            all_token_ids = []

            for market in markets[:50]:
                self._observer.state.register_market(market.condition_id, market.token_ids)
                self._active_markets[market.condition_id] = market.token_ids
                all_token_ids.extend(market.token_ids)

                if self._position_monitor:
                    self._position_monitor.add_market(
                        market.condition_id,
                        market.token_ids,
                    )

                logger.info(
                    "Market registered",
                    condition_id=market.condition_id,
                    tokens=len(market.token_ids),
                )

            if all_token_ids:
                await self._observer._ws.subscribe(all_token_ids)
                logger.info("Subscribed to all markets", total_tokens=len(all_token_ids))

            tasks = [observer_task]

            if self._position_monitor:
                tasks.append(asyncio.create_task(self._position_monitor.start()))

            await self._shutdown_event.wait()

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
        pass

    async def _on_opportunity_detected(
        self,
        condition_id: str,
        order_books: dict,
    ) -> None:
        """Handle detected arbitrage opportunity."""
        if not self._running:
            return

        try:
            opportunity = self._engine.analyze_market(condition_id, order_books)

            if not opportunity:
                return

            logger.info(
                "Opportunity detected",
                market_id=condition_id,
                type=opportunity.signal_type.value,
                profit=str(opportunity.net_profit),
            )

            if self._executor:
                if self._settings.trading_mode == "paper":
                    from src.execution.paper import PaperSimulator

                    simulator = PaperSimulator(
                        latency_ms=self._settings.simulated_latency_ms,
                        log_path="data/paper_trades.jsonl",
                    )
                    result = await simulator.execute(opportunity)
                else:
                    from src.execution import execute_arbitrage

                    result = await execute_arbitrage(self._client, opportunity)

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

    def signal_handler(sig, frame):
        logger.info("Shutdown signal received")
        bot.shutdown()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        pass

    logger.info("Arbitrage bot exited")


if __name__ == "__main__":
    main()
