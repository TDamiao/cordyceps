"""
Risk Manager module.

Handles circuit breakers, daily loss limits, and slippage protection.
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from src.config import get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RiskState:
    """Tracks current risk state (in-memory)."""
    consecutive_failures: int = 0
    daily_pnl: Decimal = Decimal("0")
    last_failure_time: float = 0
    is_paused: bool = False
    pause_until: float = 0
    total_trades_today: int = 0
    last_reset_date: str = field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%d"))


class RiskManager:
    """
    Manages trading risk and safety checks.

    Features:
    - Circuit Breaker: Pauses trading after N consecutive failures.
    - Daily Loss Limit: Stops trading if P&L drops below limit.
    - Slippage Check: Verifies spread prices before execution.
    """

    def __init__(self):
        """Initialize risk manager."""
        self._settings = get_settings()
        self._state = RiskState()

        logger.info(
            "RiskManager initialized",
            max_daily_loss=self._settings.max_daily_loss,
            failure_threshold=self._settings.circuit_breaker_failure_threshold,
            cooldown_mins=self._settings.circuit_breaker_cooldown_minutes,
        )

    def can_trade(self) -> tuple[bool, str]:
        """
        Check if trading is allowed primarily based on circuit breaker/loss limits.

        Returns:
            (allowed, reason)
        """
        self._check_daily_reset()

        # 1. Check Circuit Breaker
        if self._state.is_paused:
            if time.time() < self._state.pause_until:
                remaining_mins = int((self._state.pause_until - time.time()) / 60)
                return False, f"Circuit breaker active. Paused for {remaining_mins} more mins."
            else:
                self._reset_circuit_breaker()

        # 2. Check Daily Loss
        if self._state.daily_pnl < Decimal(str(-self._settings.max_daily_loss)):
            return False, f"Daily loss limit exceeded: {self._state.daily_pnl} < -{self._settings.max_daily_loss}"

        return True, "OK"

    def record_success(self, pnl: Decimal = Decimal("0")):
        """
        Record a successful trade execution.

        Args:
            pnl: Realized Profit/Loss from the trade
        """
        self._check_daily_reset()

        self._state.consecutive_failures = 0
        self._state.daily_pnl += pnl
        self._state.total_trades_today += 1

        logger.info(
            "Expected trade PnL recorded",
            trade_pnl=str(pnl),
            daily_pnl=str(self._state.daily_pnl)
        )

    def record_failure(self, error_reason: str):
        """
        Record a failed trade execution (order rejected, merge failed, etc).

        Args:
            error_reason: Description of the error
        """
        self._check_daily_reset()

        self._state.consecutive_failures += 1
        self._state.last_failure_time = time.time()

        logger.warning(
            "Trade failure recorded",
            consecutive_failures=self._state.consecutive_failures,
            reason=error_reason
        )

        # Trigger circuit breaker if threshold reached
        if self._state.consecutive_failures >= self._settings.circuit_breaker_failure_threshold:
            self._trigger_circuit_breaker()

    def check_slippage(self, yes_price: Decimal, no_price: Decimal) -> bool:
        """
        Check if current prices still offer profitable spread.

        Args:
            yes_price: Best ask for YES
            no_price: Best ask for NO

        Returns:
            True if trade is safe (profitable or within tolerance), False otherwise
        """
        # We want to buy cheaply. sum(prices) < 1.0 is profit.
        # If sum > 1.0, we lose money.
        total_cost = yes_price + no_price

        # Hard limit: Don't buy if we are guaranteed to lose money (sum > 1.0)
        # unless user has some weird strategy, but for arb, >1.0 is bad.
        if total_cost >= Decimal("1.0"):
            logger.warning(
                "Slippage check failed",
                yes_price=str(yes_price),
                no_price=str(no_price),
                total_cost=str(total_cost),
                threshold="1.0"
            )
            return False

        return True

    def _trigger_circuit_breaker(self):
        """Activate the circuit breaker."""
        self._state.is_paused = True
        cooldown_seconds = self._settings.circuit_breaker_cooldown_minutes * 60
        self._state.pause_until = time.time() + cooldown_seconds

        logger.error(
            "CIRCUIT BREAKER TRIGGERED",
            failures=self._state.consecutive_failures,
            pause_minutes=self._settings.circuit_breaker_cooldown_minutes,
        )

    def _reset_circuit_breaker(self):
        """Reset the circuit breaker after cooldown."""
        self._state.is_paused = False
        self._state.consecutive_failures = 0
        self._state.pause_until = 0
        logger.info("Circuit breaker reset. Resuming trading.")

    def _check_daily_reset(self):
        """Reset daily stats if date has changed (UTC)."""
        current_date = datetime.now(UTC).strftime("%Y-%m-%d")
        if current_date != self._state.last_reset_date:
            logger.info(
                "Resetting daily risk stats",
                old_date=self._state.last_reset_date,
                new_date=current_date,
                final_previous_pnl=str(self._state.daily_pnl)
            )
            self._state.last_reset_date = current_date
            self._state.daily_pnl = Decimal("0")
            self._state.total_trades_today = 0
            # We do NOT reset consecutive failures or active circuit breaker on day change
            # (safety first), but could be argued either way. keeping strict for now.
