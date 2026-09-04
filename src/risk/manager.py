"""
Risk Manager module.

Handles circuit breakers, daily loss limits, and slippage protection.
"""
import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from src.config import Settings, get_settings
from src.runtime import RuntimeState
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Lazy import to avoid circular dependency
_notifier = None

def _get_notifier():
    global _notifier
    if _notifier is None:
        try:
            from src.notifications.telegram import get_notifier as _get_telegram_notifier
            _notifier = _get_telegram_notifier()
        except (ImportError, Exception):
            pass
    return _notifier


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
    _daily_loss_notified: bool = False


class RiskManager:
    """
    Manages trading risk and safety checks.

    Features:
    - Circuit Breaker: Pauses trading after N consecutive failures.
    - Daily Loss Limit: Stops trading if P&L drops below limit.
    - Slippage Check: Verifies spread prices before execution.
    """

    def __init__(self, settings: Settings | None = None, runtime: RuntimeState | None = None):
        """Initialize risk manager."""
        self._runtime = runtime
        self._settings = settings or (runtime.settings if runtime else get_settings())
        self._state = RiskState()
        self._current_exposure = Decimal("0")
        self._open_trades = 0
        self._favorite_positions = []

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

        if self._runtime:
            self._settings = self._runtime.settings
            allowed, reason = self._runtime.can_submit_live()
            if self._settings.trading_mode != "paper" and not allowed:
                return False, reason

        # 1. Check Circuit Breaker
        if self._state.is_paused:
            if time.time() < self._state.pause_until:
                remaining_mins = int((self._state.pause_until - time.time()) / 60)
                return False, f"Circuit breaker active. Paused for {remaining_mins} more mins."
            else:
                self._reset_circuit_breaker()

        # 2. Check Daily Loss
        if self._state.daily_pnl < Decimal(str(-self._settings.max_daily_loss)):
            # Notify daily loss limit hit via Telegram (only fire once per active breach)
            if not getattr(self._state, "_daily_loss_notified", False):
                notifier = _get_notifier()
                if notifier and notifier.config.enabled:
                    try:
                        asyncio.create_task(notifier.notify_risk_event(
                            event_type="DAILY_LOSS_LIMIT",
                            message=f"Daily loss limit exceeded: {self._state.daily_pnl} < -{self._settings.max_daily_loss}",
                            current_value=str(self._state.daily_pnl),
                            limit=f"-{self._settings.max_daily_loss}",
                        ))
                    except RuntimeError:
                        pass
                self._state._daily_loss_notified = True
            return (
                False,
                f"Daily loss limit exceeded: {self._state.daily_pnl} < -{self._settings.max_daily_loss}",
            )

        return True, "OK"

    def validate_trade(self, notional: Decimal) -> tuple[bool, str]:
        allowed, reason = self.can_trade()
        if not allowed:
            return allowed, reason
        if notional <= 0 or notional > Decimal(str(self._settings.max_trade_usd)):
            return False, "trade size exceeds MAX_TRADE_USD"
        if self._current_exposure + notional > Decimal(str(self._settings.max_total_exposure_usd)):
            return False, "global exposure limit exceeded"
        if self._open_trades >= self._settings.max_open_trades:
            return False, "maximum open trades reached"
        return True, "OK"

    def open_exposure(self, notional: Decimal) -> None:
        self._current_exposure += notional
        self._open_trades += 1

    def close_exposure(self, notional: Decimal) -> None:
        self._current_exposure = max(Decimal("0"), self._current_exposure - notional)
        self._open_trades = max(0, self._open_trades - 1)

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
            "Expected trade PnL recorded", trade_pnl=str(pnl), daily_pnl=str(self._state.daily_pnl)
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

        # Notify failure via Telegram
        notifier = _get_notifier()
        if notifier and notifier.config.enabled:
            try:
                asyncio.create_task(notifier.notify_error(
                    error_type="TRADE_FAILURE",
                    error_message=error_reason,
                    severity="ERROR",
                ))
            except RuntimeError:
                pass

        logger.warning(
            "Trade failure recorded",
            consecutive_failures=self._state.consecutive_failures,
            reason=error_reason,
        )

        # Trigger circuit breaker if threshold reached
        if self._state.consecutive_failures >= self._settings.circuit_breaker_failure_threshold:
            self._trigger_circuit_breaker()

    def pause_after_leg_risk(self, reason: str) -> None:
        """Every partial-leg incident gets a cooldown, even below the failure threshold."""
        self.record_failure(reason)
        if not self._state.is_paused:
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
        total_cost = yes_price + no_price

        if total_cost >= Decimal("1.0"):
            logger.warning(
                "Slippage check failed",
                yes_price=str(yes_price),
                no_price=str(no_price),
                total_cost=str(total_cost),
                threshold="1.0",
            )
            return False

        return True

    def _trigger_circuit_breaker(self):
        """Activate the circuit breaker."""
        self._state.is_paused = True
        cooldown_seconds = self._settings.circuit_breaker_cooldown_minutes * 60
        self._state.pause_until = time.time() + cooldown_seconds

        # Notify circuit breaker activation via Telegram
        notifier = _get_notifier()
        if notifier and notifier.config.enabled:
            try:
                asyncio.create_task(notifier.notify_risk_event(
                    event_type="CIRCUIT_BREAKER",
                    message=f"Circuit breaker activated after {self._state.consecutive_failures} consecutive failures.",
                    limit=f"{self._settings.circuit_breaker_cooldown_minutes} min cooldown",
                ))
            except RuntimeError:
                pass

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

        # Notify circuit breaker reset via Telegram
        notifier = _get_notifier()
        if notifier and notifier.config.enabled:
            try:
                asyncio.create_task(notifier.notify_risk_event(
                    event_type="CIRCUIT_BREAKER_RESET",
                    message="Circuit breaker reset. Trading resumed after cooldown.",
                ))
            except RuntimeError:
                pass

    def _check_daily_reset(self):
        """Reset daily stats if date has changed (UTC)."""
        current_date = datetime.now(UTC).strftime("%Y-%m-%d")
        if current_date != self._state.last_reset_date:
            logger.info(
                "Resetting daily risk stats",
                old_date=self._state.last_reset_date,
                new_date=current_date,
                final_previous_pnl=str(self._state.daily_pnl),
            )
            self._state.last_reset_date = current_date
            self._state.daily_pnl = Decimal("0")
            self._state.total_trades_today = 0
            self._state._daily_loss_notified = False

    @property
    def state(self) -> dict:
        return {
            "consecutive_failures": self._state.consecutive_failures,
            "daily_pnl": float(self._state.daily_pnl),
            "daily_loss": float(min(Decimal("0"), self._state.daily_pnl)),
            "is_paused": self._state.is_paused,
            "pause_until": self._state.pause_until,
            "current_exposure": float(self._current_exposure),
            "open_trades": self._open_trades,
        }

    def add_favorite_position(self, position: dict) -> None:
        """Add a favorite position to the risk manager."""
        self._favorite_positions.append(position)
        self.open_exposure(Decimal(str(position["size_usd"])))

    def update_favorite_position(self, market_id: str, current_price: Decimal, current_bid: Decimal) -> None:
        """Update a favorite position and check for TP/SL."""
        for pos in self._favorite_positions:
            if pos["market_id"] == market_id:
                pos["current_price"] = float(current_price)
                pos["unrealized_pnl_pct"] = float((current_price - Decimal(str(pos["entry_price"]))) / Decimal(str(pos["entry_price"])) * 100)

                if current_price >= Decimal(str(pos["take_profit_price"])):
                    pos["action"] = "TAKE_PROFIT"
                    self.close_exposure(Decimal(str(pos["size_usd"])))
                    return

                if current_bid <= Decimal(str(pos["stop_loss_price"])):
                    pos["action"] = "STOP_LOSS"
                    self.close_exposure(Decimal(str(pos["size_usd"])))
                    return

                elapsed_h = (time.time() - pos["entry_time"]) / 3600
                remaining_h = pos["time_to_resolution_h"] - elapsed_h
                if remaining_h <= 1 and current_price > Decimal(str(pos["entry_price"])):
                    pos["action"] = "TAKE_PROFIT"
                    self.close_exposure(Decimal(str(pos["size_usd"])))
                    return

                pos["action"] = "HOLD"
                return

    def get_favorite_positions(self) -> list:
        """Get all favorite positions."""
        return self._favorite_positions
