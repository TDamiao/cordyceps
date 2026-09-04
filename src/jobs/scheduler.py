"""Background job scheduler."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from datetime import time as dt_time

from src.config import Settings, get_settings
from src.jobs.daily_summary import DailySummaryJob
from src.utils.logging import get_logger

logger = get_logger(__name__)


class JobScheduler:
    """Schedules background jobs like daily summary."""

    def __init__(
        self,
        settings: Settings | None = None,
        daily_summary_time: dt_time = dt_time(hour=20, minute=0),  # 20:00 UTC = 17:00 BRT
    ) -> None:
        self._settings = settings or get_settings()
        self._daily_summary_time = daily_summary_time
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_run_daily: datetime | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the scheduler in background."""
        if self.is_running:
            return

        async def _scheduler_loop() -> None:
            logger.info(
                "scheduler.started",
                daily_summary_time=self._daily_summary_time.isoformat(),
            )

            while not self._stop.is_set():
                now_utc = datetime.now(UTC)
                await self._check_and_run_daily_summary(now_utc)

                # Sleep for 1 minute before next check
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60.0)
                except TimeoutError:
                    pass

        self._task = asyncio.create_task(_scheduler_loop(), name="cordyceps-scheduler")

    async def _check_and_run_daily_summary(self, now_utc: datetime) -> None:
        """Check if it's time for daily summary and run it."""
        # Check if it's the scheduled time (20:00 UTC)
        target_time_utc = datetime.combine(now_utc.date(), self._daily_summary_time, tzinfo=UTC)

        # If we've already run today, skip
        if self._last_run_daily and self._last_run_daily.date() == now_utc.date():
            return

        # Check if current time is at or past the scheduled time
        if now_utc >= target_time_utc:
            logger.info("scheduler.running_daily_summary", now=now_utc.isoformat())
            try:
                job = DailySummaryJob(self._settings)
                await job.run()
                self._last_run_daily = now_utc
                logger.info("scheduler.daily_summary_complete")
            except Exception as e:
                logger.error("scheduler.daily_summary_failed", error=str(e))

    async def stop(self) -> None:
        """Stop the scheduler."""
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        logger.info("scheduler.stopped")
