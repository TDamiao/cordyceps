"""
Paper trading simulator for Polymarket arbitrage.

Simulates order execution without hitting the live CLOB.  Used in paper mode
and in tests to validate execution logic without network I/O.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from src.engine.detector import ArbitrageOpportunity
from src.utils.logging import get_logger

logger = get_logger(__name__)


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
class SimulationResult:
    success: bool = False
    orders: list[OrderResult] = field(default_factory=list)
    total_filled: Decimal = Decimal("0")
    realized_profit: Decimal = Decimal("0")
    execution_time_ms: int = 0
    error: str | None = None
    leg_risk: str | None = None

    @property
    def all_filled(self) -> bool:
        return all(o.status == OrderStatus.FILLED for o in self.orders)

    @property
    def any_filled(self) -> bool:
        return any(
            o.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) for o in self.orders
        )


class PaperSimulator:
    """
    Simulates order execution in paper mode.

    Supports:
    - Fill probability control (simulates partial fills)
    - Leg failure injection (simulates single-leg rejection)
    - Latency simulation
    - Structured JSON log output
    """

    def __init__(
        self,
        latency_ms: int = 0,
        base_fill_probability: float = 1.0,
        leg_failure_probability: float = 0.0,
        fill_fraction_jitter: float = 0.0,
        log_path: str | None = None,
    ) -> None:
        self._latency_ms = latency_ms
        self._fill_prob = base_fill_probability
        self._leg_fail_prob = leg_failure_probability
        self._jitter = fill_fraction_jitter
        self._log_path = log_path

    async def execute(self, opportunity: ArbitrageOpportunity) -> SimulationResult:
        """Simulate executing an arbitrage opportunity."""
        start = time.time()
        await asyncio.sleep(self._latency_ms / 1000.0)

        orders: list[OrderResult] = []
        any_failed = False

        for i, (token_id, price) in enumerate(zip(opportunity.token_ids, opportunity.prices)):
            order = OrderResult(
                token_id=token_id,
                order_id=f"paper-{int(time.time())}-{i}",
                price=price,
            )

            # Leg failure injection applies to ALL legs independently
            should_fail = self._leg_fail_prob > 0 and random.random() < self._leg_fail_prob

            if should_fail:
                order.status = OrderStatus.FAILED
                order.error = "simulated leg failure"
                any_failed = True
                orders.append(order)
                continue

            # Fill check
            if random.random() < self._fill_prob:
                fill_frac = 1.0 - (self._jitter * random.random()) if self._jitter else 1.0
                fill_frac = max(0.0, min(1.0, fill_frac))
                filled = opportunity.executable_quantity * Decimal(str(fill_frac))
                order.filled_size = filled
                if fill_frac >= 0.99:
                    order.status = OrderStatus.FILLED
                else:
                    order.status = OrderStatus.PARTIALLY_FILLED
            else:
                order.status = OrderStatus.FAILED
                order.error = "no fill"
                any_failed = True

            orders.append(order)

        # Compute results
        filled_sizes = [o.filled_size for o in orders if o.status in (OrderStatus.FILLED,)]
        min_filled = min(filled_sizes) if filled_sizes else Decimal("0")
        success = not any_failed and min_filled > Decimal("0")

        if success:
            realized = (
                opportunity.net_profit * (min_filled / opportunity.max_size)
                if opportunity.max_size
                else Decimal("0")
            )
        else:
            realized = Decimal("0")

        leg_risk = None
        if any_failed:
            leg_risk = "leg execution failed"

        result = SimulationResult(
            success=success,
            orders=orders,
            total_filled=min_filled,
            realized_profit=realized,
            execution_time_ms=int((time.time() - start) * 1000),
            leg_risk=leg_risk,
        )

        if self._log_path:
            self._write_log(opportunity, result)

        return result

    def _write_log(self, opp: ArbitrageOpportunity, result: SimulationResult) -> None:
        """Append structured log entry."""
        try:
            entry = {
                "event": "paper_trade",
                "market_id": opp.market_id,
                "signal": opp.signal_type.value,
                "size": str(opp.executable_quantity),
                "success": result.success,
                "realized_profit": str(result.realized_profit),
                "realized": str(result.realized_profit),
                "ts": int(time.time() * 1000),
            }
            with open(self._log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass
