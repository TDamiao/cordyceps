#!/usr/bin/env python3
"""
Polymarket Arbitrage Bot - Main Entry Point

Orchestrates all components: observer, engine, executor, and settlement.
"""

import asyncio
import signal
from decimal import Decimal
from pathlib import Path

from src.client import PolymarketClient, authenticate
from src.database import Opportunity, get_engine
from src.engine import ArbitrageEngine
from src.engine.detector import calculate_price_sum
from src.execution import OrderExecutor, RateLimiter
from src.fees import FeeParameters, FeeService, calculate_taker_fee
from src.markets import MarketFetcher
from src.observer import MarketObserver
from src.paper_engine import PaperEngine
from src.runtime import RuntimeState, get_runtime
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

    def __init__(
        self,
        runtime: RuntimeState | None = None,
        paper_engine: PaperEngine | None = None,
    ):
        """Initialize the arbitrage bot."""
        self._runtime = runtime or get_runtime()
        self._settings = self._runtime.settings
        setup_logging()

        self._client: PolymarketClient | None = None
        self._observer: MarketObserver | None = None
        self._engine = ArbitrageEngine()
        self._fee_service = FeeService(
            self._settings.clob_api_url, self._settings.fee_fallback_rate
        )
        self._paper_engine = paper_engine or PaperEngine(settings=self._settings)
        self._executor: OrderExecutor | None = None
        self._settlement: SettlementAgent | None = None
        self._position_monitor: PositionMonitor | None = None

        self._metrics = MetricsTracker(
            persist_path=Path("data/trades.json"),
        )
        self._health = HealthMonitor(self._metrics)

        self._fetcher = MarketFetcher(gamma_host=self._settings.gamma_api_url)
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
                try:
                    auth_result = authenticate()
                    self._client = PolymarketClient(auth_result)
                    auth_address = auth_result.eoa_address
                except Exception as exc:
                    # Keep market data/dashboard online, but fail closed for orders.
                    self._client = PolymarketClient(public_only=True)
                    auth_address = None
                    logger.error("Live authentication unavailable", error=str(exc))

            self._observer = MarketObserver(
                on_book_update=self._on_book_update,
                on_opportunity=self._on_opportunity_detected,
            )

            rate_limiter = RateLimiter()

            from src.risk.manager import RiskManager

            risk_manager = RiskManager(self._settings, self._runtime)
            self._risk_manager = risk_manager

            # The legacy merge implementation targets pre-V2 collateral/contracts.
            # Fail closed until the V2 relayer flow receives a separate audit.
            ctf_contract = None
            if self._settings.use_atomic_merge:
                logger.warning("USE_ATOMIC_MERGE ignored: audited CLOB V2 merge is unavailable")

            self._executor = OrderExecutor(
                client=self._client,
                rate_limiter=rate_limiter,
                ctf_contract=ctf_contract,
                risk_manager=risk_manager,
                runtime=self._runtime,
                revalidator=self._revalidate_opportunity,
            )

            # Settlement is a live-only subsystem.
            # Legacy settlement code targets pre-V2 collateral/contracts. It is
            # deliberately kept disabled until a dedicated V2 relayer path is audited.
            logger.info("Settlement disabled pending audited CLOB V2 relayer integration")

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
                active_only=True,
                binary_only=True,
                limit=self._settings.market_limit,
            )

            if not markets:
                logger.warning("No active markets found")
                # Keep the observer alive: the scanner may recover from a
                # transient Gamma/proxy failure and populate markets later.
                observer_task = asyncio.create_task(self._run_observer())
                await self._shutdown_event.wait()
                observer_task.cancel()
                return

            logger.info("Found active markets", count=len(markets))

            observer_task = asyncio.create_task(self._run_observer())

            await asyncio.sleep(1)

            all_token_ids = []

            for market in markets[: self._settings.market_limit]:
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
            # Analyze immediately.  Fee metadata is refreshed once per market
            # in the background; blocking every book callback on HTTP makes a
            # fresh order book stale during network degradation.
            market = self._fetcher.cache.get_market(condition_id)
            if market and market.fees_enabled is False:
                fee_params = FeeParameters(
                    rate=Decimal("0"), exponent=Decimal("1"), source="gamma_fee_free"
                )
            else:
                fee_params = self._fee_service.get(condition_id)
                self._fee_service.refresh_in_background(condition_id)
            before = self._engine.stats
            opportunity = self._engine.analyze_market(
                condition_id, order_books, fee_params=fee_params
            )

            if not opportunity:
                after = self._engine.stats
                reasons = [
                    name
                    for name in (
                        "rejected_stale",
                        "rejected_liquidity",
                        "rejected_slippage",
                        "rejected_fee",
                        "rejected_profit",
                        "rejected_edge",
                        "rejected_risk",
                    )
                    if after.get(name, 0) > before.get(name, 0)
                ]
                self._persist_rejected(
                    condition_id,
                    order_books,
                    reasons[0] if reasons else "no_edge",
                    fee_params,
                )
                return

            logger.info(
                "Opportunity detected",
                market_id=condition_id,
                type=opportunity.signal_type.value,
                profit=str(opportunity.net_profit),
            )

            self._persist_opportunity(opportunity)

            if self._executor:
                if self._settings.trading_mode == "paper":
                    fill = await self._paper_engine.execute(opportunity)
                    realized_profit = fill.realized_profit
                    success = fill.success
                    execution_time_ms = fill.execution_time_ms
                else:
                    result = await self._executor.execute_opportunity(opportunity)
                    realized_profit = result.realized_profit
                    success = result.success
                    execution_time_ms = result.execution_time_ms

                self._metrics.create_trade_record(
                    market_id=condition_id,
                    signal_type=opportunity.signal_type.value,
                    token_ids=opportunity.token_ids,
                    size=opportunity.max_size,
                    total_cost=opportunity.total_cost,
                    expected_profit=opportunity.net_profit,
                    realized_profit=realized_profit,
                    fees=opportunity.fees,
                    success=success,
                    execution_time_ms=execution_time_ms,
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
        await self._fee_service.close()

        self._health.set_websocket_status(False)
        logger.info("Bot stopped")

    async def _revalidate_opportunity(self, opportunity):
        if not self._observer:
            return None
        books = self._observer.state.get_market_books(opportunity.market_id)
        if not books:
            return None
        market = self._fetcher.cache.get_market(opportunity.market_id)
        if market and market.fees_enabled is False:
            fee_params = FeeParameters(
                rate=Decimal("0"), exponent=Decimal("1"), source="gamma_fee_free"
            )
        else:
            fee_params = await self._fee_service.refresh(opportunity.market_id)
        return self._engine.analyze_market(opportunity.market_id, books, fee_params=fee_params)

    def _persist_opportunity(self, opportunity) -> None:
        try:
            from sqlmodel import Session

            with Session(get_engine(self._settings)) as session:
                session.add(
                    Opportunity(
                        market_id=opportunity.market_id,
                        signal_type=opportunity.signal_type.value,
                        token_ids=opportunity.token_ids,
                        prices=[float(value) for value in opportunity.prices],
                        best_prices=[float(value) for value in opportunity.prices],
                        vwap_prices=[float(value) for value in opportunity.vwap_prices],
                        gross_edge=float(opportunity.gross_edge),
                        net_edge=float(opportunity.net_edge),
                        fee=float(opportunity.fees),
                        slippage=float(opportunity.expected_slippage),
                        size=float(opportunity.max_size),
                        net_profit=float(opportunity.net_profit),
                        max_size=float(opportunity.max_size),
                        decision="paper" if self._settings.trading_mode == "paper" else "candidate",
                        status="detected",
                    )
                )
                session.commit()
        except Exception as exc:
            logger.warning("opportunity.persist_failed", error=str(exc))

    def _persist_rejected(
        self,
        condition_id: str,
        books: dict,
        reason: str,
        fee_params: FeeParameters | None = None,
    ) -> None:
        ask_sum = calculate_price_sum(books, "ask")
        bid_sum = calculate_price_sum(books, "bid")
        candidates = []
        if ask_sum is not None and ask_sum < 1:
            candidates.append(("BUY_SET", Decimal("1") - ask_sum, "ask"))
        if bid_sum is not None and bid_sum > 1:
            candidates.append(("SELL_SET", bid_sum - Decimal("1"), "bid"))
        if not candidates:
            return
        try:
            from sqlmodel import Session

            with Session(get_engine(self._settings)) as session:
                for signal, edge, side in candidates:
                    levels = [
                        book.best_ask if side == "ask" else book.best_bid for book in books.values()
                    ]
                    prices = [float(level.price) for level in levels if level]
                    executable_size = min(
                        (level.size for level in levels if level), default=Decimal("0")
                    )
                    decimal_prices = [Decimal(str(price)) for price in prices]
                    params = fee_params or self._fee_service.get(condition_id)
                    fee_per_share = sum(
                        calculate_taker_fee(Decimal("1"), price, params)
                        for price in decimal_prices
                    )
                    net_edge = (
                        edge
                        - fee_per_share
                        - Decimal(str(self._settings.leg_risk_buffer))
                    )
                    # This is an indicative candidate snapshot, not an
                    # executable result: the engine rejected it after fees,
                    # liquidity, slippage, or risk checks.
                    session.add(
                        Opportunity(
                            market_id=condition_id,
                            signal_type=signal,
                            token_ids=list(books),
                            prices=prices,
                            best_prices=prices,
                            vwap_prices=prices,
                            gross_edge=float(edge),
                            net_edge=float(net_edge),
                            fee=float(fee_per_share * executable_size),
                            size=float(executable_size),
                            max_size=float(executable_size),
                            net_profit=float(net_edge * executable_size),
                            decision="rejected",
                            rejection_reason=reason,
                            status="rejected",
                        )
                    )
                session.commit()
        except Exception as exc:
            logger.warning("opportunity.rejection_persist_failed", error=str(exc))

    def get_status(self) -> dict:
        """Get current bot status."""
        observer_stats = self._observer.state.stats if self._observer else {}
        return {
            "running": self._running,
            "health": self._health.get_health().to_dict(),
            "observer_stats": observer_stats,
            "engine_stats": self._engine.stats,
            "executor_stats": self._executor.stats if self._executor else {},
            "active_markets": len(self._active_markets),
            "paper": {
                "trade_count": self._paper_engine.trade_count,
                "total_profit": self._paper_engine.total_profit,
                "last_trade": (
                    self._paper_engine.fills[-1].timestamp if self._paper_engine.fills else None
                ),
            },
            "risk": (
                getattr(self, "_risk_manager", None).state
                if getattr(self, "_risk_manager", None)
                else {}
            ),
            "runtime": {
                "armed": self._runtime.armed,
                "kill_switch": self._runtime.kill_switch,
                "incomplete_exposure_usd": self._runtime.incomplete_exposure_usd,
            },
        }

    def apply_runtime_config(self) -> None:
        """Apply validated DB-backed parameters to long-lived components."""
        settings = self._runtime.settings
        self._settings = settings
        self._engine.config.min_profit_threshold = Decimal(str(settings.min_profit_threshold))
        self._engine.config.min_net_edge = Decimal(str(settings.min_net_edge))
        self._engine.config.min_net_profit_usd = Decimal(str(settings.min_net_profit_usd))
        self._engine.config.max_position_size = Decimal(str(settings.max_trade_usd))
        self._engine.config.min_liquidity = Decimal(str(settings.min_trade_shares))
        self._engine.config.min_trade_shares = Decimal(str(settings.min_trade_shares))
        self._engine.config.max_slippage_pct = Decimal(str(settings.max_slippage_pct))
        self._engine.config.orderbook_stale_ms = settings.orderbook_stale_ms
        self._paper_engine._latency_ms = settings.simulated_latency_ms
        if self._executor:
            self._executor._settings = settings
            self._executor._risk._settings = settings

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
