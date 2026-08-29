"""
Metrics and P&L tracking for the arbitrage bot.

Tracks trade history, performance metrics, and provides analytics.
"""

import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class TradeRecord:
    """Record of a single trade execution."""

    trade_id: str
    market_id: str
    signal_type: str  # BUY_SET or SELL_SET
    token_ids: list[str]
    size: Decimal
    total_cost: Decimal
    expected_profit: Decimal
    realized_profit: Decimal
    fees: Decimal
    success: bool
    execution_time_ms: int
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        # Convert Decimal to string for JSON
        for key in ["size", "total_cost", "expected_profit", "realized_profit", "fees"]:
            d[key] = str(d[key])
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "TradeRecord":
        """Create from dictionary."""
        for key in ["size", "total_cost", "expected_profit", "realized_profit", "fees"]:
            data[key] = Decimal(data[key])
        return cls(**data)


@dataclass
class PerformanceMetrics:
    """Aggregated performance metrics."""

    total_trades: int = 0
    successful_trades: int = 0
    failed_trades: int = 0
    total_volume: Decimal = Decimal("0")
    total_profit: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")
    net_profit: Decimal = Decimal("0")
    avg_execution_time_ms: float = 0.0
    best_trade_profit: Decimal = Decimal("0")
    worst_trade_profit: Decimal = Decimal("0")
    win_rate: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "total_trades": self.total_trades,
            "successful_trades": self.successful_trades,
            "failed_trades": self.failed_trades,
            "total_volume": str(self.total_volume),
            "total_profit": str(self.total_profit),
            "total_fees": str(self.total_fees),
            "net_profit": str(self.net_profit),
            "avg_execution_time_ms": round(self.avg_execution_time_ms, 2),
            "best_trade_profit": str(self.best_trade_profit),
            "worst_trade_profit": str(self.worst_trade_profit),
            "win_rate": round(self.win_rate * 100, 2),
        }


class MetricsTracker:
    """
    Tracks trading metrics and P&L over time.

    Maintains trade history and computes performance analytics.
    """

    def __init__(
        self,
        max_history: int = 1000,
        persist_path: Path | None = None,
    ):
        """
        Initialize metrics tracker.

        Args:
            max_history: Maximum number of trades to keep in memory
            persist_path: Optional path to persist trade history
        """
        self._trades: deque[TradeRecord] = deque(maxlen=max_history)
        self._persist_path = persist_path
        self._start_time = time.time()
        self._trade_counter = 0

        # Load existing history if available
        if persist_path and persist_path.exists():
            self._load_history()

    def record_trade(self, trade: TradeRecord) -> None:
        """
        Record a trade execution.

        Args:
            trade: Trade record to add
        """
        self._trades.append(trade)
        self._trade_counter += 1

        logger.info(
            "Trade recorded",
            trade_id=trade.trade_id,
            success=trade.success,
            profit=str(trade.realized_profit),
        )

        # Persist if configured
        if self._persist_path:
            self._save_history()

    def create_trade_record(
        self,
        market_id: str,
        signal_type: str,
        token_ids: list[str],
        size: Decimal,
        total_cost: Decimal,
        expected_profit: Decimal,
        realized_profit: Decimal,
        fees: Decimal,
        success: bool,
        execution_time_ms: int,
    ) -> TradeRecord:
        """Create and record a new trade record."""
        trade = TradeRecord(
            trade_id=f"trade_{self._trade_counter + 1}_{int(time.time() * 1000)}",
            market_id=market_id,
            signal_type=signal_type,
            token_ids=token_ids,
            size=size,
            total_cost=total_cost,
            expected_profit=expected_profit,
            realized_profit=realized_profit,
            fees=fees,
            success=success,
            execution_time_ms=execution_time_ms,
        )
        self.record_trade(trade)
        return trade

    def get_metrics(self) -> PerformanceMetrics:
        """
        Calculate current performance metrics.

        Returns:
            PerformanceMetrics with aggregated stats
        """
        metrics = PerformanceMetrics()

        if not self._trades:
            return metrics

        execution_times = []

        for trade in self._trades:
            metrics.total_trades += 1
            metrics.total_volume += trade.size
            metrics.total_fees += trade.fees
            execution_times.append(trade.execution_time_ms)

            if trade.success:
                metrics.successful_trades += 1
                metrics.total_profit += trade.realized_profit
                metrics.net_profit += trade.realized_profit

                if trade.realized_profit > metrics.best_trade_profit:
                    metrics.best_trade_profit = trade.realized_profit

                if trade.realized_profit < metrics.worst_trade_profit or metrics.worst_trade_profit == 0:
                    metrics.worst_trade_profit = trade.realized_profit
            else:
                metrics.failed_trades += 1

        if execution_times:
            metrics.avg_execution_time_ms = sum(execution_times) / len(execution_times)

        if metrics.total_trades > 0:
            metrics.win_rate = metrics.successful_trades / metrics.total_trades

        return metrics

    def get_recent_trades(self, count: int = 10) -> list[TradeRecord]:
        """Get most recent trades."""
        trades = list(self._trades)
        return trades[-count:] if len(trades) >= count else trades

    def get_profit_by_period(self, period_hours: int = 24) -> Decimal:
        """Get profit for a specific time period."""
        cutoff = int((time.time() - period_hours * 3600) * 1000)
        return sum(
            t.realized_profit
            for t in self._trades
            if t.timestamp >= cutoff and t.success
        )

    def _save_history(self) -> None:
        """Save trade history to disk."""
        if not self._persist_path:
            return

        try:
            data = [t.to_dict() for t in self._trades]
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._persist_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save trade history", error=str(e))

    def _load_history(self) -> None:
        """Load trade history from disk."""
        if not self._persist_path or not self._persist_path.exists():
            return

        try:
            with open(self._persist_path) as f:
                data = json.load(f)
            for item in data:
                trade = TradeRecord.from_dict(item)
                self._trades.append(trade)
            logger.info("Loaded trade history", count=len(self._trades))
        except Exception as e:
            logger.warning("Failed to load trade history", error=str(e))

    @property
    def uptime_seconds(self) -> float:
        """Get bot uptime in seconds."""
        return time.time() - self._start_time

    def reset(self) -> None:
        """Reset all metrics."""
        self._trades.clear()
        self._trade_counter = 0
        self._start_time = time.time()


@dataclass
class HealthStatus:
    """Health status of the bot."""

    status: str  # "healthy", "degraded", "unhealthy"
    uptime_seconds: float
    websocket_connected: bool
    last_trade_time: int | None
    trades_last_hour: int
    errors_last_hour: int
    metrics: PerformanceMetrics
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "status": self.status,
            "uptime_seconds": round(self.uptime_seconds, 2),
            "websocket_connected": self.websocket_connected,
            "last_trade_time": self.last_trade_time,
            "trades_last_hour": self.trades_last_hour,
            "errors_last_hour": self.errors_last_hour,
            "metrics": self.metrics.to_dict(),
            "timestamp": self.timestamp,
        }


class HealthMonitor:
    """
    Monitors bot health and provides status endpoints.
    """

    def __init__(
        self,
        metrics_tracker: MetricsTracker,
        error_threshold: int = 10,
    ):
        """
        Initialize health monitor.

        Args:
            metrics_tracker: Metrics tracker instance
            error_threshold: Errors per hour before degraded status
        """
        self._metrics = metrics_tracker
        self._error_threshold = error_threshold
        self._websocket_connected = False
        self._errors: deque[int] = deque(maxlen=1000)

    def record_error(self, error_msg: str) -> None:
        """Record an error occurrence."""
        self._errors.append(int(time.time() * 1000))
        logger.error("Error recorded", error=error_msg)

    def set_websocket_status(self, connected: bool) -> None:
        """Update WebSocket connection status."""
        self._websocket_connected = connected

    def get_health(self) -> HealthStatus:
        """Get current health status."""
        metrics = self._metrics.get_metrics()
        recent = self._metrics.get_recent_trades(1)

        # Count errors in last hour
        hour_ago = int((time.time() - 3600) * 1000)
        errors_last_hour = sum(1 for e in self._errors if e >= hour_ago)

        # Count trades in last hour
        trades_last_hour = sum(
            1 for t in self._metrics._trades if t.timestamp >= hour_ago
        )

        # Determine status
        if not self._websocket_connected:
            status = "unhealthy"
        elif errors_last_hour > self._error_threshold:
            status = "degraded"
        else:
            status = "healthy"

        return HealthStatus(
            status=status,
            uptime_seconds=self._metrics.uptime_seconds,
            websocket_connected=self._websocket_connected,
            last_trade_time=recent[-1].timestamp if recent else None,
            trades_last_hour=trades_last_hour,
            errors_last_hour=errors_last_hour,
            metrics=metrics,
        )

    def get_health_json(self) -> str:
        """Get health status as JSON string."""
        return json.dumps(self.get_health().to_dict(), indent=2)
