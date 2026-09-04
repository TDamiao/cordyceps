"""Daily summary job: computes trades and P&L, sends to Telegram."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlmodel import Session, select

from src.config import Settings, get_settings
from src.database import PaperTrade, get_engine
from src.notifications.telegram import TelegramNotifier, get_notifier
from src.utils.logging import get_logger

logger = get_logger(__name__)


class DailySummaryJob:
    """Computes daily trading summary and sends via Telegram."""

    def __init__(
        self,
        settings: Settings | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._notifier = notifier or get_notifier()

    async def run(self) -> None:
        """Execute the daily summary job."""
        try:
            summary = self.compute_daily_summary()
            if summary is None:
                logger.info("daily_summary.no_trades_today")
                return

            await self._notifier.notify_daily_summary(
                total_trades=summary["total_trades"],
                winning_trades=summary["winning_trades"],
                losing_trades=summary["losing_trades"],
                total_pnl=Decimal(str(summary["total_pnl"])),
                total_pnl_pct=summary["total_pnl_pct"],
                favorite_trades=summary["favorite_trades"],
                favorite_pnl=Decimal(str(summary["favorite_pnl"])),
                arb_trades=summary["arb_trades"],
                arb_pnl=Decimal(str(summary["arb_pnl"])),
            )
            logger.info(
                "daily_summary.sent",
                total_trades=summary["total_trades"],
                total_pnl=summary["total_pnl"],
            )
        except Exception as e:
            logger.error("daily_summary.failed", error=str(e))

    def compute_daily_summary(self) -> dict | None:
        """Compute daily summary from database trades.

        Returns:
            Dict with summary stats, or None if no trades today.
        """
        try:
            with Session(get_engine(self._settings)) as session:
                # Get trades from today (UTC)
                now_utc = datetime.now(UTC)
                start_of_day_ms = int(
                    (now_utc.replace(hour=0, minute=0, second=0, microsecond=0))
                    .timestamp() * 1000
                )
                end_of_day_ms = int(
                    (now_utc.replace(hour=23, minute=59, second=59, microsecond=999999))
                    .timestamp() * 1000
                )

                stmt = select(PaperTrade).where(
                    PaperTrade.timestamp >= start_of_day_ms,
                    PaperTrade.timestamp <= end_of_day_ms,
                )
                trades = session.exec(stmt).all()

                if not trades:
                    return None

                # Compute statistics
                total_trades = len(trades)
                total_pnl = 0.0
                winning_trades = 0
                losing_trades = 0
                favorite_trades = 0
                favorite_pnl = 0.0
                arb_trades = 0
                arb_pnl = 0.0

                for trade in trades:
                    realized_pnl = trade.realized_pnl
                    total_pnl += realized_pnl

                    if realized_pnl > 0:
                        winning_trades += 1
                    elif realized_pnl < 0:
                        losing_trades += 1

                    # Categorize by strategy (favorite vs arbitrage)
                    # Favorite signal is "BUY_SET" or "SELL_SET" from favorite strategy
                    # For now, we treat all as arbitrage. If favorite info is stored in signal,
                    # we can distinguish later. For this version, assume all paper trades are arbitrage.
                    is_favorite = trade.signal == "FAVORITE"
                    if is_favorite:
                        favorite_trades += 1
                        favorite_pnl += realized_pnl
                    else:
                        arb_trades += 1
                        arb_pnl += realized_pnl

                # Compute win rate
                win_rate = (
                    (winning_trades / total_trades * 100) if total_trades > 0 else 0.0
                )

                return {
                    "total_trades": total_trades,
                    "winning_trades": winning_trades,
                    "losing_trades": losing_trades,
                    "total_pnl": total_pnl,
                    "total_pnl_pct": win_rate,  # Placeholder; real PnL % would need capital
                    "favorite_trades": favorite_trades,
                    "favorite_pnl": favorite_pnl,
                    "arb_trades": arb_trades,
                    "arb_pnl": arb_pnl,
                }

        except Exception as e:
            logger.error("daily_summary.compute_failed", error=str(e))
            return None


async def run_daily_summary_job(
    settings: Settings | None = None,
    notifier: TelegramNotifier | None = None,
) -> None:
    """Standalone entrypoint for daily summary job."""
    job = DailySummaryJob(settings=settings, notifier=notifier)
    await job.run()
