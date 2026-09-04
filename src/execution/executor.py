"""Fail-closed multi-leg execution state machine for CLOB V2."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, StrEnum

from sqlmodel import Session, select

from src.client import OrderSide, PolymarketClient
from src.config import get_settings
from src.database import Execution, ExecutionLeg, RiskEvent, get_engine
from src.engine.detector import ArbitrageOpportunity, SignalType
from src.engine.favorite import (
    FavoriteAction,
    FavoriteEngine,
    FavoriteOpportunity,
    FavoritePosition,
)
from src.execution.rate_limiter import RateLimiter
from src.risk.manager import RiskManager
from src.runtime import RuntimeState, get_runtime
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Lazy import to avoid circular dependency
_telegram_notifier = None


def _get_telegram_notifier():
    """Get the Telegram notifier singleton (lazy)."""
    global _telegram_notifier
    if _telegram_notifier is None:
        try:
            from src.notifications.telegram import get_notifier
            _telegram_notifier = get_notifier()
        except (ImportError, Exception):
            pass
    return _telegram_notifier


class OrderStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ExecutionState(StrEnum):
    DETECTED = "DETECTED"
    VALIDATING = "VALIDATING"
    SUBMITTING = "SUBMITTING"
    PARTIAL = "PARTIAL"
    HEDGING = "HEDGING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    FAILED = "FAILED"
    POSITION_OPEN = "POSITION_OPEN"
    POSITION_MONITORING = "POSITION_MONITORING"


@dataclass
class OrderResult:
    token_id: str
    order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_size: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    error: str | None = None
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class ExecutionResult:
    """Result for arbitrage opportunities (BUY_SET/SELL_SET)."""
    opportunity: ArbitrageOpportunity
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: ExecutionState = ExecutionState.DETECTED
    orders: list[OrderResult] = field(default_factory=list)
    success: bool = False
    total_filled: Decimal = Decimal("0")
    realized_profit: Decimal = Decimal("0")
    error: str | None = None
    execution_time_ms: int = 0
    merge_result: object | None = None

    @property
    def all_filled(self) -> bool:
        return bool(self.orders) and all(
            order.status == OrderStatus.FILLED for order in self.orders
        )

    @property
    def any_filled(self) -> bool:
        return any(
            order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
            and order.filled_size > 0
            for order in self.orders
        )


@dataclass
class FavoriteExecutionResult:
    """Result for favorite compounding opportunities."""
    opportunity: FavoriteOpportunity
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: ExecutionState = ExecutionState.POSITION_OPEN
    order: OrderResult | None = None
    position: FavoritePosition | None = None
    success: bool = False
    filled_size: Decimal = Decimal("0")
    realized_profit: Decimal = Decimal("0")
    error: str | None = None
    execution_time_ms: int = 0
    action: FavoriteAction | None = None

    @property
    def is_open(self) -> bool:
        """True if position is open and being monitored."""
        return self.success and self.state == ExecutionState.POSITION_OPEN


class OrderExecutor:
    """
    Serializes live execution, revalidates, and resolves partial-leg exposure.

    Supports both ArbitrageOpportunity (multi-leg) and FavoriteOpportunity
    (single-leg favorite compounding) execution.
    """

    def __init__(
        self,
        client: PolymarketClient,
        rate_limiter: RateLimiter | None = None,
        ctf_contract: object | None = None,
        risk_manager: RiskManager | None = None,
        runtime: RuntimeState | None = None,
        revalidator: (
            Callable[[ArbitrageOpportunity], Awaitable[ArbitrageOpportunity | None]] | None
        ) = None,
        favorite_engine: FavoriteEngine | None = None,
    ):
        # Use provided favorite_engine or create new instance
        if favorite_engine is None:
            from src.engine.favorite import FavoriteEngine as _FavoriteEngine
            favorite_engine = _FavoriteEngine()

        self._client = client
        self._rate_limiter = rate_limiter or RateLimiter()
        self._runtime = runtime or get_runtime()
        self._settings = self._runtime.settings if runtime else get_settings()
        self._risk = risk_manager or RiskManager(self._settings, self._runtime)
        self._revalidator = revalidator
        self._ctf = ctf_contract
        self._favorite_engine = favorite_engine
        self._orders_submitted = 0
        self._orders_filled = 0
        self._orders_failed = 0
        self._active_state: ExecutionState | None = None

    def _send_error_notification(
        self, error_type: str, error_message: str, severity: str = "ERROR", context: dict | None = None
    ) -> None:
        """Fire-and-forget Telegram error notification."""
        notifier = _get_telegram_notifier()
        if notifier and notifier.config.enabled:
            try:
                asyncio.create_task(notifier.notify_error(
                    error_type=error_type,
                    error_message=error_message,
                    severity=severity,
                    context=context,
                ))
            except RuntimeError:
                pass

    def _send_risk_notification(
        self, event_type: str, message: str, current_value: str | None = None, limit: str | None = None
    ) -> None:
        """Fire-and-forget Telegram risk event notification."""
        notifier = _get_telegram_notifier()
        if notifier and notifier.config.enabled:
            try:
                asyncio.create_task(notifier.notify_risk_event(
                    event_type=event_type,
                    message=message,
                    current_value=current_value,
                    limit=limit,
                ))
            except RuntimeError:
                pass

    async def execute_opportunity(
        self, opportunity: ArbitrageOpportunity | FavoriteOpportunity
    ) -> ExecutionResult | FavoriteExecutionResult:
        """
        Execute a detected opportunity.

        Automatically detects and routes between:
        - ArbitrageOpportunity (BUY_SET/SELL_SET): multi-leg execution with partial recovery
        - FavoriteOpportunity: single-leg buy with TP/SL position tracking
        """
        # Route based on type
        if isinstance(opportunity, FavoriteOpportunity):
            return await self._execute_favorite(opportunity)
        else:
            return await self._execute_arbitrage(opportunity)

    async def _execute_favorite(
        self, opportunity: FavoriteOpportunity
    ) -> ExecutionResult | FavoriteExecutionResult:
        """
        Execute a favorite compounding opportunity.

        This is a single-leg buy of the favorite token with:
        - Position tracking in RiskManager
        - TP/SL monitoring
        - Time-based exit (< 1h to resolution)
        """
        result = FavoriteExecutionResult(opportunity=opportunity)
        start = time.monotonic()

        # Create trackable position
        position = self._favorite_engine.create_position(opportunity)
        result.position = position
        result.state = ExecutionState.POSITION_OPEN

        if self._runtime.execution_lock.locked():
            result.error = "another arbitrage execution is active"
            result.state = ExecutionState.ABORTED
            return result

        async with self._runtime.execution_lock:
            self._runtime.active_executions += 1
            try:
                self._transition_favorite(result, ExecutionState.VALIDATING)
                allowed, reason = self._runtime.can_submit_live()
                if not allowed:
                    result.error = reason
                    result.state = ExecutionState.ABORTED
                    return result

                # Validate trade size against risk limits
                notional = opportunity.position_size_usd
                allowed, reason = self._risk.validate_trade(notional)
                if not allowed:
                    result.error = reason
                    self._transition_favorite(result, ExecutionState.ABORTED)
                    return result

                self._transition_favorite(result, ExecutionState.SUBMITTING)

                # Single-leg buy of the favorite token
                order_result = await self._submit_order(
                    token_id=opportunity.favorite_token_id,
                    side=OrderSide.BUY,
                    price=opportunity.favorite_price,
                    size=opportunity.position_shares,
                )
                result.order = order_result

                if order_result.status == OrderStatus.FILLED:
                    # Add position to risk manager only AFTER successful fill
                    position_dict = {
                        "market_id": position.market_id,
                        "market_question": position.market_question,
                        "token_id": position.token_id,
                        "entry_price": float(position.entry_price),
                        "entry_time": position.entry_time,
                        "size_shares": float(position.size_shares),
                        "size_usd": float(position.size_usd),
                        "take_profit_price": float(position.take_profit_price),
                        "stop_loss_price": float(position.stop_loss_price),
                        "time_to_resolution_h": position.time_to_resolution_h,
                    }
                    self._risk.add_favorite_position(position_dict)

                    result.filled_size = order_result.filled_size
                    result.success = True

                    # Realized profit will be calculated when position closes
                    result.realized_profit = Decimal("0")

                    self._transition_favorite(result, ExecutionState.POSITION_OPEN)
                    logger.info(
                        "Favorite position opened",
                        market_id=opportunity.market_id,
                        token_id=opportunity.favorite_token_id,
                        size_usd=str(opportunity.position_size_usd),
                        entry_price=str(opportunity.favorite_price),
                    )
                else:
                    result.error = order_result.error or "order not filled"
                    self._risk.record_failure(result.error)
                    self._transition_favorite(result, ExecutionState.FAILED)

                return result

            except Exception as exc:
                result.error = str(exc)
                self._risk.record_failure(result.error)
                self._transition_favorite(result, ExecutionState.FAILED)
                self._send_error_notification(
                    "FAVORITE_EXECUTION_FAILED",
                    str(exc),
                    severity="ERROR",
                    context={
                        "execution_id": result.execution_id[:16],
                        "market_id": opportunity.market_id[:16],
                    },
                )
                return result
            finally:
                result.execution_time_ms = int((time.monotonic() - start) * 1000)
                self._runtime.active_executions = max(0, self._runtime.active_executions - 1)

    def _transition_favorite(
        self, result: FavoriteExecutionResult, state: ExecutionState
    ) -> None:
        """Transition favorite execution to new state."""
        result.state = state
        self._active_state = (
            state
            if state
            in {
                ExecutionState.VALIDATING,
                ExecutionState.SUBMITTING,
                ExecutionState.POSITION_OPEN,
                ExecutionState.POSITION_MONITORING,
            }
            else None
        )

    async def _execute_arbitrage(
        self, opportunity: ArbitrageOpportunity
    ) -> ExecutionResult | FavoriteExecutionResult:
        """Execute arbitrage opportunity (existing multi-leg logic)."""
        result = ExecutionResult(opportunity=opportunity)
        start = time.monotonic()
        self._persist_execution(result)

        if self._runtime.execution_lock.locked():
            result.error = "another arbitrage execution is active"
            self._transition(result, ExecutionState.ABORTED)
            return result

        async with self._runtime.execution_lock:
            self._runtime.active_executions += 1
            try:
                self._transition(result, ExecutionState.VALIDATING)
                allowed, reason = self._runtime.can_submit_live()
                if not allowed:
                    result.error = reason
                    self._transition(result, ExecutionState.ABORTED)
                    return result

                validated = (
                    await self._revalidator(opportunity) if self._revalidator else opportunity
                )
                if validated is None:
                    result.error = "opportunity disappeared during revalidation"
                    self._transition(result, ExecutionState.ABORTED)
                    return result
                result.opportunity = validated

                notional = validated.total_cost
                allowed, reason = self._risk.validate_trade(notional)
                if not allowed:
                    result.error = reason
                    self._transition(result, ExecutionState.ABORTED)
                    return result

                self._transition(result, ExecutionState.SUBMITTING)
                side = (
                    OrderSide.BUY if validated.signal_type == SignalType.BUY_SET else OrderSide.SELL
                )
                tasks = [
                    asyncio.wait_for(
                        self._submit_order(token_id, side, price, validated.max_size),
                        timeout=self._settings.leg_timeout_ms / 1000,
                    )
                    for token_id, price in zip(validated.token_ids, validated.prices)
                ]
                raw_orders = await asyncio.gather(*tasks, return_exceptions=True)
                for token_id, price, raw in zip(validated.token_ids, validated.prices, raw_orders):
                    if isinstance(raw, BaseException):
                        order = OrderResult(
                            token_id=token_id,
                            price=price,
                            status=OrderStatus.FAILED,
                            error="leg timeout" if isinstance(raw, TimeoutError) else str(raw),
                        )
                        result.orders.append(order)
                        # Notify leg timeout
                        if isinstance(raw, TimeoutError):
                            self._send_error_notification(
                                "LEG_TIMEOUT",
                                "Order leg timed out (timeout exceeded).",
                                severity="WARNING",
                                context={
                                    "token_id": token_id[:16],
                                    "execution_id": result.execution_id[:16],
                                    "market_id": result.opportunity.market_id[:16],
                                },
                            )
                    else:
                        result.orders.append(raw)
                self._persist_legs(result)

                if result.all_filled:
                    self._complete(result)
                    return result
                if result.any_filled:
                    self._transition(result, ExecutionState.PARTIAL)
                    await self._recover_partial(result, side)
                    return result

                result.error = result.error or "no leg filled"
                self._risk.record_failure(result.error)
                self._transition(result, ExecutionState.FAILED)
                return result
            except Exception as exc:
                result.error = str(exc)
                self._risk.record_failure(result.error)
                self._transition(result, ExecutionState.FAILED)
                # Notify execution error
                self._send_error_notification(
                    "EXECUTION_FAILED",
                    str(exc),
                    severity="ERROR",
                    context={
                        "execution_id": result.execution_id[:16],
                        "market_id": result.opportunity.market_id[:16],
                    },
                )
                return result
            finally:
                result.execution_time_ms = int((time.monotonic() - start) * 1000)
                self._runtime.active_executions = max(0, self._runtime.active_executions - 1)
                self._persist_execution(result)

    def _complete(self, result: ExecutionResult) -> None:
        result.total_filled = min(order.filled_size for order in result.orders)
        result.realized_profit = result.opportunity.net_profit * (
            result.total_filled / result.opportunity.max_size
        )
        result.success = True
        self._risk.record_success(result.realized_profit)
        self._transition(result, ExecutionState.COMPLETED)

    async def _recover_partial(self, result: ExecutionResult, side: OrderSide) -> None:
        """One completion attempt, then one unwind attempt. No aggressive retries."""
        imbalance = sum(
            order.filled_size * order.price
            for order in result.orders
            if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
        )
        self._runtime.incomplete_exposure_usd = float(imbalance)
        self._risk.pause_after_leg_risk("partial fill")
        self._record_risk(result, "partial_fill", f"Partial exposure: {imbalance} pUSD")

        # Notify partial fill
        self._send_risk_notification(
            "PARTIAL_FILL",
            "Partial leg fill detected. Attempting recovery.",
            current_value=f"${imbalance:.2f}",
            limit=f"${self._settings.max_leg_imbalance_usd:.2f}",
        )

        if imbalance > Decimal(str(self._settings.max_leg_imbalance_usd)):
            result.error = "leg imbalance exceeds configured maximum"

        self._transition(result, ExecutionState.HEDGING)
        failed = [order for order in result.orders if order.status == OrderStatus.FAILED]
        for order in failed:
            factor = (
                Decimal("1") + Decimal(str(self._settings.emergency_slippage_pct))
                if side == OrderSide.BUY
                else Decimal("1") - Decimal(str(self._settings.emergency_slippage_pct))
            )
            emergency_price = min(Decimal("0.9999"), max(Decimal("0.0001"), order.price * factor))
            replacement = await self._submit_order(
                order.token_id, side, emergency_price, result.opportunity.max_size
            )
            if replacement.status == OrderStatus.FILLED:
                result.orders[result.orders.index(order)] = replacement

        if result.all_filled:
            self._runtime.incomplete_exposure_usd = 0
            self._complete(result)
            return

        unwind_ok = True
        reverse = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
        for order in list(result.orders):
            if order.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                continue
            factor = (
                Decimal("1") - Decimal(str(self._settings.emergency_slippage_pct))
                if reverse == OrderSide.SELL
                else Decimal("1") + Decimal(str(self._settings.emergency_slippage_pct))
            )
            unwind_price = min(Decimal("0.9999"), max(Decimal("0.0001"), order.price * factor))
            unwind = await self._submit_order(
                order.token_id, reverse, unwind_price, order.filled_size
            )
            unwind_ok = unwind_ok and unwind.status == OrderStatus.FILLED

        if unwind_ok:
            self._runtime.incomplete_exposure_usd = 0
            result.error = "partial fill unwound; realized loss requires reconciliation"
            self._transition(result, ExecutionState.ABORTED)
        else:
            result.error = "EXPOSURE REQUIRES ATTENTION: emergency unwind failed"
            self._runtime.kill()
            self._record_risk(result, "unwind_failed", result.error, severity="critical")
            self._transition(result, ExecutionState.FAILED)
            # Notify unwind failure (kill switch activated)
            self._send_risk_notification(
                "EXPOSURE_REQUIRES_ATTENTION",
                "Emergency unwind failed. Kill switch activated.",
                current_value=f"${float(imbalance):.2f} at risk",
            )

    async def _submit_order(
        self, token_id: str, side: OrderSide, price: Decimal, size: Decimal
    ) -> OrderResult:
        result = OrderResult(token_id=token_id, price=price, status=OrderStatus.SUBMITTED)
        await self._rate_limiter.acquire_order()
        self._orders_submitted += 1
        response = await asyncio.to_thread(
            self._client.create_fok_order,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
        )
        # Legacy test doubles may return just an id. Production requires CLOB's matched status.
        if isinstance(response, str):
            response = {"success": True, "orderID": response, "status": "matched"}
        response = response or {}
        result.order_id = response.get("orderID")
        status = str(response.get("status", "")).lower()
        if response.get("success") and status == "matched":
            filled = size
            if result.order_id:
                try:
                    details = await asyncio.to_thread(self._client.get_order, result.order_id)
                    matched_raw = details.get("size_matched")
                    if matched_raw is not None:
                        filled = Decimal(str(matched_raw)) / Decimal("1000000")
                except Exception:
                    # Matched FOK is authoritative; detail lookup is best-effort.
                    pass
            result.filled_size = min(size, filled)
            result.status = (
                OrderStatus.FILLED if result.filled_size >= size else OrderStatus.PARTIALLY_FILLED
            )
            self._orders_filled += 1
        else:
            if result.order_id and status in {"live", "delayed"}:
                await asyncio.to_thread(self._client.cancel_order, result.order_id)
                result.status = OrderStatus.CANCELLED
            else:
                result.status = OrderStatus.FAILED
            result.error = response.get("errorMsg") or f"FOK was not matched (status={status})"
            self._orders_failed += 1
        return result

    def _transition(self, result: ExecutionResult, state: ExecutionState) -> None:
        result.state = state
        self._active_state = (
            state
            if state
            in {
                ExecutionState.VALIDATING,
                ExecutionState.SUBMITTING,
                ExecutionState.PARTIAL,
                ExecutionState.HEDGING,
            }
            else None
        )
        self._persist_execution(result)
        logger.info("execution.state", execution_id=result.execution_id, state=state.value)

    def _persist_execution(self, result: ExecutionResult) -> None:
        try:
            with Session(get_engine(self._settings)) as session:
                row = session.exec(
                    select(Execution).where(Execution.execution_id == result.execution_id)
                ).first()
                if row is None:
                    row = Execution(
                        execution_id=result.execution_id,
                        market_id=result.opportunity.market_id,
                        mode=self._settings.trading_mode,
                    )
                row.state = result.state.value
                filled_orders = [order for order in result.orders if order.filled_size > 0]
                row.filled_quantity = float(
                    result.total_filled
                    or sum((order.filled_size for order in filled_orders), Decimal("0"))
                )
                if filled_orders:
                    total_quantity = sum(
                        (order.filled_size for order in filled_orders), Decimal("0")
                    )
                    row.average_price = float(
                        sum(
                            (order.filled_size * order.price for order in filled_orders),
                            Decimal("0"),
                        )
                        / total_quantity
                    )
                row.fees = float(result.opportunity.fees)
                row.realized_pnl = float(result.realized_profit)
                row.latency_ms = result.execution_time_ms
                row.failure_reason = result.error or ""
                row.updated_at = int(time.time() * 1000)
                session.add(row)
                session.commit()
        except Exception as exc:
            logger.warning("execution.persist_failed", error=str(exc))

    def _persist_legs(self, result: ExecutionResult) -> None:
        try:
            with Session(get_engine(self._settings)) as session:
                for order in result.orders:
                    session.add(
                        ExecutionLeg(
                            execution_id=result.execution_id,
                            token_id=order.token_id,
                            status=order.status.value,
                            order_id=order.order_id or "",
                            requested_quantity=float(result.opportunity.max_size),
                            filled_quantity=float(order.filled_size),
                            limit_price=float(order.price),
                            average_price=float(order.price),
                            error=order.error or "",
                        )
                    )
                session.commit()
        except Exception as exc:
            logger.warning("execution.legs_persist_failed", error=str(exc))

    def _record_risk(
        self, result: ExecutionResult, event_type: str, message: str, severity: str = "warning"
    ) -> None:
        try:
            with Session(get_engine(self._settings)) as session:
                session.add(
                    RiskEvent(
                        severity=severity,
                        event_type=event_type,
                        message=message,
                        execution_id=result.execution_id,
                    )
                )
                session.commit()
        except Exception:
            pass

    async def cancel_all_orders(self) -> bool:
        try:
            await self._rate_limiter.acquire_request()
            return await asyncio.to_thread(self._client.cancel_all_orders)
        except Exception as exc:
            logger.error("Failed to cancel all orders", error=str(exc))
            return False

    @property
    def stats(self) -> dict:
        return {
            "orders_submitted": self._orders_submitted,
            "orders_filled": self._orders_filled,
            "orders_failed": self._orders_failed,
            "active_state": self._active_state.value if self._active_state else None,
            "fill_rate": (
                self._orders_filled / self._orders_submitted if self._orders_submitted else 0
            ),
            **self._rate_limiter.stats,
        }

    def reset_stats(self) -> None:
        self._orders_submitted = self._orders_filled = self._orders_failed = 0
        self._rate_limiter.reset_stats()


async def execute_arbitrage(
    client: PolymarketClient,
    opportunity: ArbitrageOpportunity,
    ctf_contract: object | None = None,
) -> ExecutionResult:
    """Execute an arbitrage opportunity using the OrderExecutor."""
    return await OrderExecutor(client, ctf_contract=ctf_contract).execute_opportunity(opportunity)


async def execute_favorite(
    client: PolymarketClient,
    opportunity: FavoriteOpportunity,
    ctf_contract: object | None = None,
) -> FavoriteExecutionResult:
    """Execute a favorite compounding opportunity using the OrderExecutor."""
    return await OrderExecutor(client, ctf_contract=ctf_contract).execute_opportunity(opportunity)
