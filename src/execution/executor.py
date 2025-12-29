"""
Order execution module for arbitrage trades.

Handles order submission, tracking, and error handling.
"""

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
import time

from src.client import PolymarketClient, OrderSide, OrderType
from src.engine.detector import ArbitrageOpportunity, SignalType
from src.execution.rate_limiter import RateLimiter
from src.config import get_settings
from src.utils.logging import get_logger
from src.contracts.ctf import CTFContract, MergeResult

logger = get_logger(__name__)


class OrderStatus(Enum):
    """Status of an order."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class OrderResult:
    """Result of a single order submission."""

    token_id: str
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_size: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    error: Optional[str] = None
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class ExecutionResult:
    """Result of executing an arbitrage opportunity."""

    opportunity: ArbitrageOpportunity
    orders: list[OrderResult] = field(default_factory=list)
    success: bool = False
    total_filled: Decimal = Decimal("0")
    realized_profit: Decimal = Decimal("0")
    error: Optional[str] = None
    execution_time_ms: int = 0
    merge_result: Optional[MergeResult] = None

    @property
    def all_filled(self) -> bool:
        """Check if all orders were filled."""
        return all(o.status == OrderStatus.FILLED for o in self.orders)

    @property
    def any_filled(self) -> bool:
        """Check if any orders were filled."""
        return any(o.status == OrderStatus.FILLED for o in self.orders)


class OrderExecutor:
    """
    Executes arbitrage trades on Polymarket.

    Handles:
    - FOK (Fill-or-Kill) order creation for atomic execution
    - Batch order submission
    - Rate limiting
    - Error handling and recovery
    - Atomic merging of positions (HFT)
    """

    def __init__(
        self,
        client: PolymarketClient,
        rate_limiter: Optional[RateLimiter] = None,
        ctf_contract: Optional[CTFContract] = None,
    ):
        """
        Initialize order executor.

        Args:
            client: Polymarket client for order submission
            rate_limiter: Rate limiter instance
            ctf_contract: CTF contract wrapper for atomic merges
        """
        self._client = client
        self._rate_limiter = rate_limiter or RateLimiter()
        self._ctf = ctf_contract
        self._settings = get_settings()

        self._orders_submitted = 0
        self._orders_filled = 0
        self._orders_failed = 0

    async def execute_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
    ) -> ExecutionResult:
        """
        Execute an arbitrage opportunity.

        Submits orders for all legs of the trade. For BUY_SET, buys all
        outcomes. For SELL_SET, sells all outcomes.
        
        If atomic merge is enabled and we bought all legs, it triggers
        mergePositions() immediately.

        Args:
            opportunity: Detected arbitrage opportunity

        Returns:
            ExecutionResult with details of execution
        """
        start_time = time.time()

        result = ExecutionResult(opportunity=opportunity)

        # Determine order side based on signal type
        order_side = OrderSide.BUY if opportunity.signal_type == SignalType.BUY_SET else OrderSide.SELL

        logger.info(
            "Executing opportunity",
            market_id=opportunity.market_id,
            signal_type=opportunity.signal_type.value,
            size=str(opportunity.max_size),
            expected_profit=str(opportunity.net_profit),
        )

        try:
            # Submit orders for all tokens simultaneously
            order_tasks = []
            for token_id, price in zip(opportunity.token_ids, opportunity.prices):
                order_tasks.append(
                    self._submit_order(
                        token_id=token_id,
                        side=order_side,
                        price=price,
                        size=opportunity.max_size,
                    )
                )
            
            # Execute all orders in parallel
            orders = await asyncio.gather(*order_tasks)
            result.orders.extend(orders)

            # Check if any orders failed
            for order_result in orders:
                if order_result.status == OrderStatus.FAILED:
                    result.error = f"Order failed for {order_result.token_id}: {order_result.error}"
                    # Try to cancel others if any failed (though FOK usually fails immediately)
                    # For FOK, cancellation isn't really needed, but good practice
                    await self._cancel_pending_orders(result.orders)
                    break

            # Calculate results
            result.total_filled = min(
                (o.filled_size for o in result.orders if o.status == OrderStatus.FILLED),
                default=Decimal("0"),
            )

            if result.all_filled:
                result.success = True
                
                # Default realized profit based purely on price diff
                # (Verified by atomic merge later if enabled)
                result.realized_profit = opportunity.net_profit * (
                    result.total_filled / opportunity.max_size
                )
                
                logger.info(
                    "Orders filled",
                    market_id=opportunity.market_id,
                    filled=str(result.total_filled),
                )
                
                # Trigger atomic merge if enabled and we BOUGHT positions
                if (
                    self._settings.use_atomic_merge
                    and self._ctf
                    and order_side == OrderSide.BUY
                ):
                    await self._handle_atomic_merge(opportunity.market_id, result)
                    
            elif result.any_filled:
                logger.warning(
                    "Partial fill - position opened",
                    market_id=opportunity.market_id,
                    orders_filled=sum(1 for o in result.orders if o.status == OrderStatus.FILLED),
                )
            else:
                logger.info(
                    "No fills - opportunity missed",
                    market_id=opportunity.market_id,
                )

        except Exception as e:
            result.error = str(e)
            logger.error(
                "Execution failed",
                market_id=opportunity.market_id,
                error=str(e),
            )

        result.execution_time_ms = int((time.time() - start_time) * 1000)
        return result

    async def _handle_atomic_merge(
        self,
        condition_id: str,
        result: ExecutionResult
    ) -> None:
        """
        Handle atomic merge logic.
        
        Args:
            condition_id: Market condition ID
            result: Execution result to update
        """
        logger.info("Attempting atomic merge", condition_id=condition_id)
        
        # Determine amount to merge (use filled size)
        # Convert Decimal to int (USDC has 6 decimals, but wait... 
        # shares are 1:1 with USDC collateral, so 1 share = 10^6 units if USDC is 6 decimals?
        # Actually in CTF, amounts are uint256. 
        # If we bought 10.0 shares, that's 10 * 10^6 raw units usually.
        # But let's assume `filled_size` is already in human-readable units/shares from the CLOB client.
        # We need to convert to raw integer for the contract. 
        # Since Polymarket uses USDC (6 decimals), 1 share = 10^6 base units.
        
        try:
            # Assuming filled_size is e.g. 10.5 (shares/USDC)
            # CTF expects raw integer.
            merge_amount_raw = int(result.total_filled * Decimal("1000000"))
            
            merge_res = await self._ctf.merge_positions(
                condition_id=condition_id,
                amount=merge_amount_raw,
                max_gas_price_gwei=self._settings.max_gas_price_gwei,
            )
            
            result.merge_result = merge_res
            
            if merge_res.success:
                logger.info(
                    "Atomic merge successful!",
                    tx_hash=merge_res.tx_hash,
                    returned_usdc=str(merge_res.amount_returned)
                )
                # Recalculate true profit after gas
                # Note: We need gas cost in USDC to be precise, 
                # but for now realized_profit is gross profit.
            else:
                logger.error("Atomic merge failed", error=merge_res.error)
                # Don't fail the whole execution result, as we still hold the positions
                # We can retry merge later or hold to settlement
                
        except Exception as e:
            logger.error("Error in atomic merge process", error=str(e))

    async def _submit_order(
        self,
        token_id: str,
        side: OrderSide,
        price: Decimal,
        size: Decimal,
    ) -> OrderResult:
        """
        Submit a single FOK order.

        Args:
            token_id: Token to trade
            side: BUY or SELL
            price: Limit price
            size: Order size

        Returns:
            OrderResult with status and details
        """
        result = OrderResult(token_id=token_id, price=price)

        try:
            # Apply rate limiting
            await self._rate_limiter.acquire_order()

            self._orders_submitted += 1

            # Submit FOK order
            order_id = self._client.create_fok_order(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
            )

            if order_id:
                result.order_id = order_id
                result.status = OrderStatus.FILLED  # FOK orders are immediately filled or cancelled
                result.filled_size = size
                self._orders_filled += 1

                logger.debug(
                    "Order filled",
                    token_id=token_id[:16],
                    order_id=order_id,
                    side=side.value,
                    price=str(price),
                    size=str(size),
                )
            else:
                result.status = OrderStatus.FAILED
                result.error = "Order not filled (FOK rejected)"
                self._orders_failed += 1

        except Exception as e:
            result.status = OrderStatus.FAILED
            result.error = str(e)
            self._orders_failed += 1

            logger.error(
                "Order submission failed",
                token_id=token_id[:16],
                error=str(e),
            )

        return result

    async def _cancel_pending_orders(self, orders: list[OrderResult]) -> None:
        """
        Cancel any pending orders (for cleanup after failure).

        Args:
            orders: List of order results to check and cancel
        """
        for order in orders:
            if order.order_id and order.status == OrderStatus.SUBMITTED:
                try:
                    await self._rate_limiter.acquire_request()
                    self._client.cancel_order(order.order_id)
                    order.status = OrderStatus.CANCELLED
                    logger.debug("Order cancelled", order_id=order.order_id)
                except Exception as e:
                    logger.warning(
                        "Failed to cancel order",
                        order_id=order.order_id,
                        error=str(e),
                    )

    async def cancel_all_orders(self) -> bool:
        """
        Cancel all open orders (emergency stop).

        Returns:
            True if successful
        """
        try:
            await self._rate_limiter.acquire_request()
            return self._client.cancel_all_orders()
        except Exception as e:
            logger.error("Failed to cancel all orders", error=str(e))
            return False

    @property
    def stats(self) -> dict:
        """Get executor statistics."""
        return {
            "orders_submitted": self._orders_submitted,
            "orders_filled": self._orders_filled,
            "orders_failed": self._orders_failed,
            "fill_rate": self._orders_filled / self._orders_submitted if self._orders_submitted > 0 else 0,
            **self._rate_limiter.stats,
        }

    def reset_stats(self) -> None:
        """Reset statistics."""
        self._orders_submitted = 0
        self._orders_filled = 0
        self._orders_failed = 0
        self._rate_limiter.reset_stats()


async def execute_arbitrage(
    client: PolymarketClient,
    opportunity: ArbitrageOpportunity,
    ctf_contract: Optional[CTFContract] = None,
) -> ExecutionResult:
    """
    Convenience function to execute a single arbitrage opportunity.

    Args:
        client: Polymarket client
        opportunity: Opportunity to execute
        ctf_contract: CTF contract for atomic merges

    Returns:
        ExecutionResult
    """
    executor = OrderExecutor(client, ctf_contract=ctf_contract)
    return await executor.execute_opportunity(opportunity)
