"""Notification service that integrates Telegram alerts with bot events."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Optional

from src.notifications.telegram import TelegramNotifier, get_notifier
from src.utils.logging import get_logger

logger = get_logger(__name__)


class NotificationService:
    """Centralized notification dispatcher for bot events."""

    def __init__(self, notifier: Optional[TelegramNotifier] = None):
        self.notifier = notifier or get_notifier()
        self._tasks: list[asyncio.Task] = []

    async def notify_execution_failure(
        self,
        execution_id: str,
        error: str,
        market_id: Optional[str] = None,
        severity: str = "ERROR",
    ) -> None:
        """Notify execution failure."""
        try:
            context = {
                "execution_id": execution_id[:16],
                "market_id": market_id[:16] if market_id else "N/A",
            }
            task = asyncio.create_task(
                self.notifier.notify_error(
                    error_type="EXECUTION_FAILED",
                    error_message=error,
                    context=context,
                    severity=severity,
                )
            )
            self._tasks.append(task)
        except Exception as exc:
            logger.warning("notify_execution_failure failed", error=str(exc))

    async def notify_partial_fill(
        self,
        execution_id: str,
        market_id: str,
        imbalance_usd: float,
        max_imbalance_usd: float,
    ) -> None:
        """Notify partial fill exposure."""
        try:
            task = asyncio.create_task(
                self.notifier.notify_risk_event(
                    event_type="PARTIAL_FILL",
                    message=f"Partial leg fill detected. Attempting recovery.",
                    current_value=f"${imbalance_usd:.2f}",
                    limit=f"${max_imbalance_usd:.2f}",
                )
            )
            self._tasks.append(task)
        except Exception as exc:
            logger.warning("notify_partial_fill failed", error=str(exc))

    async def notify_unwind_failed(
        self,
        execution_id: str,
        market_id: str,
        exposure_usd: float,
    ) -> None:
        """Notify failed emergency unwind (kill switch activated)."""
        try:
            task = asyncio.create_task(
                self.notifier.notify_risk_event(
                    event_type="EXPOSURE_REQUIRES_ATTENTION",
                    message=f"Emergency unwind failed. Kill switch activated.",
                    current_value=f"${exposure_usd:.2f} at risk",
                )
            )
            self._tasks.append(task)
        except Exception as exc:
            logger.warning("notify_unwind_failed failed", error=str(exc))

    async def notify_circuit_breaker_triggered(
        self,
        consecutive_failures: int,
        cooldown_minutes: int,
    ) -> None:
        """Notify circuit breaker activation."""
        try:
            task = asyncio.create_task(
                self.notifier.notify_risk_event(
                    event_type="CIRCUIT_BREAKER",
                    message=f"Circuit breaker activated after {consecutive_failures} consecutive failures.",
                    limit=f"{cooldown_minutes} min cooldown",
                )
            )
            self._tasks.append(task)
        except Exception as exc:
            logger.warning("notify_circuit_breaker_triggered failed", error=str(exc))

    async def notify_daily_loss_limit_exceeded(
        self,
        daily_pnl: Decimal,
        max_daily_loss: float,
    ) -> None:
        """Notify daily loss limit exceeded."""
        try:
            task = asyncio.create_task(
                self.notifier.notify_risk_event(
                    event_type="DAILY_LOSS_LIMIT",
                    message=f"Daily loss limit exceeded. Trading halted.",
                    current_value=f"${float(daily_pnl):.2f}",
                    limit=f"-${max_daily_loss:.2f}",
                )
            )
            self._tasks.append(task)
        except Exception as exc:
            logger.warning("notify_daily_loss_limit_exceeded failed", error=str(exc))

    async def notify_kill_switch(
        self,
        reason: str,
    ) -> None:
        """Notify kill switch activation."""
        try:
            task = asyncio.create_task(
                self.notifier.notify_risk_event(
                    event_type="KILL_SWITCH",
                    message=f"Kill switch activated: {reason}",
                )
            )
            self._tasks.append(task)
        except Exception as exc:
            logger.warning("notify_kill_switch failed", error=str(exc))

    async def notify_leg_timeout(
        self,
        token_id: str,
        execution_id: str,
        market_id: str,
    ) -> None:
        """Notify leg timeout."""
        try:
            context = {
                "token_id": token_id[:16],
                "execution_id": execution_id[:16],
                "market_id": market_id[:16],
            }
            task = asyncio.create_task(
                self.notifier.notify_error(
                    error_type="LEG_TIMEOUT",
                    error_message=f"Order leg timed out (timeout exceeded).",
                    context=context,
                    severity="WARNING",
                )
            )
            self._tasks.append(task)
        except Exception as exc:
            logger.warning("notify_leg_timeout failed", error=str(exc))

    async def notify_slippage_exceeded(
        self,
        market_id: str,
        prices: tuple,
        max_slippage_pct: float,
    ) -> None:
        """Notify slippage exceeded during revalidation."""
        try:
            context = {
                "market_id": market_id[:16],
                "prices": f"{prices[0]:.4f}, {prices[1]:.4f}",
                "slippage_limit": f"{max_slippage_pct*100:.2f}%",
            }
            task = asyncio.create_task(
                self.notifier.notify_error(
                    error_type="SLIPPAGE_EXCEEDED",
                    error_message=f"Opportunity prices moved beyond acceptable slippage during revalidation.",
                    context=context,
                    severity="WARNING",
                )
            )
            self._tasks.append(task)
        except Exception as exc:
            logger.warning("notify_slippage_exceeded failed", error=str(exc))

    async def notify_startup(self) -> None:
        """Notify bot startup."""
        try:
            task = asyncio.create_task(self.notifier.notify_startup())
            self._tasks.append(task)
        except Exception as exc:
            logger.warning("notify_startup failed", error=str(exc))

    async def notify_shutdown(self, reason: str = "Manual") -> None:
        """Notify bot shutdown."""
        try:
            await self.notifier.notify_shutdown(reason)
        except Exception as exc:
            logger.warning("notify_shutdown failed", error=str(exc))

    async def cleanup_tasks(self) -> None:
        """Wait for any pending notification tasks."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()


# Global instance
_service: Optional[NotificationService] = None


def get_notification_service() -> NotificationService:
    """Get or create the global notification service."""
    global _service
    if _service is None:
        _service = NotificationService()
    return _service


async def init_notification_service() -> NotificationService:
    """Initialize the notification service and send startup message."""
    global _service
    _service = NotificationService()
    await _service.notify_startup()
    return _service


async def shutdown_notification_service() -> None:
    """Shutdown the notification service gracefully."""
    global _service
    if _service:
        await _service.cleanup_tasks()
        await _service.notifier.close()
        _service = None
