"""Process-local safety controls backed by validated PostgreSQL configuration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sqlalchemy import or_
from sqlmodel import Session, select

from src.config import Settings, get_settings
from src.database import Execution, get_engine, init_db, load_runtime_config, save_runtime_config


@dataclass
class RuntimeState:
    """Mutable state that starts disarmed until startup readiness succeeds."""

    settings: Settings
    armed: bool = False
    kill_switch: bool = False
    geo_allowed: bool = True
    incomplete_exposure_usd: float = 0.0
    active_executions: int = 0
    execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def load(cls, base: Settings | None = None) -> RuntimeState:
        base = base or get_settings()
        init_db(base)
        with Session(get_engine(base)) as session:
            persisted = load_runtime_config(session)
            incomplete = session.exec(
                select(Execution).where(
                    or_(
                        Execution.state.in_(["PARTIAL", "HEDGING"]),
                        Execution.failure_reason.contains("EXPOSURE REQUIRES ATTENTION"),
                    )
                )
            ).all()
        settings = base.with_runtime_overrides(persisted)
        exposure = sum(max(row.filled_quantity * row.average_price, 0.01) for row in incomplete)
        return cls(
            settings=settings,
            armed=False,
            kill_switch=base.kill_switch or bool(incomplete),
            incomplete_exposure_usd=exposure,
        )

    def update_config(self, values: dict) -> dict[str, int | float]:
        unknown = set(values) - set(self.settings.RUNTIME_FIELDS)
        if unknown:
            raise ValueError(f"Unknown or non-editable settings: {', '.join(sorted(unknown))}")
        updated = self.settings.with_runtime_overrides(values)
        with Session(get_engine(self.settings)) as session:
            save_runtime_config(session, values)
        self.settings = updated
        return self.settings.runtime_values()

    def can_submit_live(self) -> tuple[bool, str]:
        if self.settings.trading_mode == "paper":
            return False, "paper mode"
        if not self.settings.live_trading_enabled:
            return False, "LIVE_TRADING_ENABLED is false"
        if not self.armed:
            return False, "live trading is disarmed"
        if self.kill_switch:
            return False, "kill switch is active"
        if not self.geo_allowed:
            return False, "geographic eligibility is not confirmed"
        if self.incomplete_exposure_usd > 0:
            return False, "incomplete exposure requires attention"
        return True, "ok"

    def arm(self) -> None:
        self.armed = True

    def disarm(self) -> None:
        self.armed = False

    def kill(self) -> None:
        self.kill_switch = True
        self.armed = False
        # Notify kill switch activation via Telegram
        self._notify_kill_switch("Kill switch activated via runtime")

    def resume(self) -> None:
        self.kill_switch = False
        # Notify kill switch deactivation via Telegram
        self._notify_kill_switch("Kill switch deactivated", activated=False)

    def _notify_kill_switch(self, reason: str, activated: bool = True) -> None:
        """Fire-and-forget Telegram notification for kill switch state change."""
        try:
            from src.notifications.telegram import get_notifier

            notifier = get_notifier()
            if notifier and notifier.config.enabled:
                asyncio.create_task(
                    notifier.notify_risk_event(
                        event_type="KILL_SWITCH",
                        message=f"Kill switch {'activated' if activated else 'deactivated'}: {reason}",
                    )
                )
        except (ImportError, Exception):
            pass


_runtime: RuntimeState | None = None


def get_runtime() -> RuntimeState:
    global _runtime
    if _runtime is None:
        _runtime = RuntimeState.load()
    return _runtime


def reset_runtime() -> None:
    """Reset process state; the orchestrator may auto-arm after readiness."""
    global _runtime
    _runtime = None
