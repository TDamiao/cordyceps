"""
Paper trading engine.

Simulates execution of detected arbitrage opportunities without hitting the live
CLOB. Records simulated trades in a local SQLite database via the Trade model.

The engine deliberately keeps state in memory for performance, with a small
async flush to the DB for persistence.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from decimal import Decimal

from sqlmodel import Session

from src.config import get_settings
from src.database import Trade, get_engine
from src.engine.detector import ArbitrageOpportunity, SignalType
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class PaperFill:
    """A simulated trade fill."""

    trade_id: str
    market_id: str
    signal_type: str
    token_ids: list[str]
    side: str
    size: float
    price: float
    expected_profit: float
    realized_profit: float = 0.0
    success: bool = True
    execution_time_ms: int = 0
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))


class PaperEngine:
    """Simulated trade executor used when trading_mode is paper."""


    def __init__(self, simulated_latency_ms: int | None = None) -> None:
        settings = get_settings()
        self._latency_ms = (
            simulated_latency_ms
            if simulated_latency_ms is not None
            else settings.simulated_latency_ms
        )
        self._fills: list[PaperFill] = []
        self._lock = asyncio.Lock()
        self._trade_seq = 0

    @property
    def fills(self) -> list[PaperFill]:
        return list(self._fills)

    @property
    def total_profit(self) -> float:
        return float(sum(f.realized_profit for f in self._fills))

    @property
    def trade_count(self) -> int:
        return len(self._fills)

    async def execute(self, opportunity: ArbitrageOpportunity) -> PaperFill:
        """Simulate executing an opportunity and return the recorded fill."""
        if self._latency_ms > 0:
            await asyncio.sleep(self._latency_ms / 1000.0)

        async with self._lock:
            self._trade_seq += 1
            trade_id = f"paper-{int(time.time())}-{self._trade_seq}"
            side = "BUY" if opportunity.signal_type == SignalType.BUY_SET else "SELL"
            size = float(opportunity.max_size)
            avg_price = float(
                sum(opportunity.prices) / max(len(opportunity.prices), 1)
            )
            expected_profit = float(opportunity.net_profit)

            # Simulate small slippage skew in paper mode
            slippage = 0.99 if side == "BUY" else 1.01
            realized_profit = float(
                opportunity.net_profit * Decimal(str(slippage))
                if side == "BUY"
                else opportunity.net_profit / Decimal(str(slippage))
            )

            fill = PaperFill(
                trade_id=trade_id,
                market_id=opportunity.market_id,
                signal_type=opportunity.signal_type.value,
                token_ids=list(opportunity.token_ids),
                side=side,
                size=size,
                price=avg_price,
                expected_profit=expected_profit,
                realized_profit=realized_profit,
                success=True,
                execution_time_ms=self._latency_ms,
            )
            self._fills.append(fill)

        self._persist(fill)
        logger.info(
            "paper.executed",
            trade_id=fill.trade_id,
            market_id=fill.market_id,
            side=fill.side,
            size=fill.size,
            expected_profit=fill.expected_profit,
            realized_profit=fill.realized_profit,
        )
        return fill

    def _persist(self, fill: PaperFill) -> None:
        """Persist the fill to the database if engine is configured for it."""
        # Minimal persistence; if sqlalchemy is available we upsert the Trade row
        try:
            with Session(get_engine()) as session:
                session.add(
                    Trade(
                        trade_id=fill.trade_id,
                        market_id=fill.market_id,
                        signal_type=fill.signal_type,
                        token_ids=fill.token_ids,
                        side=fill.side,
                        size=fill.size,
                        price=fill.price,
                        total_cost=fill.size * fill.price,
                        expected_profit=fill.expected_profit,
                        realized_profit=fill.realized_profit,
                        success=fill.success,
                        execution_time_ms=fill.execution_time_ms,
                        status="filled",
                    )
                )
                session.commit()
        except Exception:  # pragma: no cover - db env guard
            logger.debug("paper.persist_skipped")

    def reset(self) -> None:
        self._fills.clear()
        self._trade_seq = 0
