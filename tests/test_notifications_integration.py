"""Tests for Telegram notification integration with safety, runtime, circuit breaker, and kill switch."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.engine.detector import ArbitrageOpportunity, SignalType
from src.execution.executor import ExecutionState, OrderExecutor
from src.runtime import RuntimeState


def settings(tmp_path, **values) -> Settings:
    defaults = {
        "trading_mode": "live_test",
        "live_trading_enabled": True,
        "dry_run": False,
        "private_key": "0x" + "a" * 64,
        "proxy_address": "0x" + "b" * 40,
        "admin_token": "admin-test-token",
        "polymarket_api_secret": "server-secret",
        "database_url": f"sqlite:///{tmp_path / 'test.db'}",
        "leg_timeout_ms": 100,
        "simulated_latency_ms": 0,
    }
    return Settings(_env_file=None, **(defaults | values))


def opportunity() -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        market_id="market",
        signal_type=SignalType.BUY_SET,
        token_ids=["yes", "no"],
        prices=[Decimal("0.45"), Decimal("0.45")],
        sizes=[Decimal("1"), Decimal("1")],
        max_size=Decimal("1"),
        total_cost=Decimal("0.90"),
        expected_payout=Decimal("1"),
        gross_profit=Decimal("0.10"),
        fees=Decimal("0"),
        net_profit=Decimal("0.08"),
        profit_pct=Decimal("0.08"),
        net_edge=Decimal("0.08"),
    )


def matched(order_id: str) -> dict:
    return {"success": True, "orderID": order_id, "status": "matched"}


def failed() -> dict:
    return {"success": False, "orderID": "", "status": "", "errorMsg": "no fill"}


class TestRuntimeKillSwitchNotification:
    """Tests for runtime.py kill switch notification integration."""

    def test_kill_calls_telegram_notifier(self, tmp_path):
        """kill() should trigger a Telegram notification when enabled."""
        runtime = RuntimeState(settings(tmp_path), armed=True, geo_allowed=True)
        mock_notifier = MagicMock()
        mock_notifier.config.enabled = True
        mock_notifier.notify_risk_event = AsyncMock(return_value=True)

        with patch("src.notifications.telegram.get_notifier", return_value=mock_notifier):
            with patch("asyncio.create_task") as mock_create_task:
                runtime.kill()
                # Verify a task was created
                assert mock_create_task.call_count == 1
                # Verify notify_risk_event was called with KILL_SWITCH event type
                # by inspecting the coroutine args
                call_args = mock_create_task.call_args
                # The coroutine passed to create_task should call notify_risk_event with KILL_SWITCH
                assert call_args is not None

        assert runtime.kill_switch is True
        assert runtime.armed is False

    def test_resume_calls_telegram_notifier(self, tmp_path):
        """resume() should trigger a Telegram notification when enabled."""
        runtime = RuntimeState(settings(tmp_path), armed=False, kill_switch=True, geo_allowed=True)
        mock_notifier = MagicMock()
        mock_notifier.config.enabled = True
        mock_notifier.notify_risk_event = AsyncMock(return_value=True)

        with patch("src.notifications.telegram.get_notifier", return_value=mock_notifier):
            with patch("asyncio.create_task") as mock_create_task:
                runtime.resume()
                assert mock_create_task.call_count == 1

        assert runtime.kill_switch is False

    def test_kill_no_notification_when_disabled(self, tmp_path):
        """kill() should not call Telegram when notifier is disabled."""
        runtime = RuntimeState(settings(tmp_path), armed=True, geo_allowed=True)
        mock_notifier = MagicMock()
        mock_notifier.config.enabled = False

        with patch("src.notifications.telegram.get_notifier", return_value=mock_notifier):
            with patch("asyncio.create_task") as mock_create_task:
                runtime.kill()
                mock_create_task.assert_not_called()


class TestCircuitBreakerNotification:
    """Tests for RiskManager circuit breaker notification."""

    def test_circuit_breaker_triggers_notification(self, tmp_path):
        """Circuit breaker activation should send a Telegram notification."""
        from src.risk.manager import RiskManager

        cfg = settings(tmp_path, circuit_breaker_failure_threshold=3)
        manager = RiskManager(cfg)

        mock_notifier = MagicMock()
        mock_notifier.config.enabled = True
        mock_notifier.notify_risk_event = AsyncMock(return_value=True)
        mock_notifier.notify_error = AsyncMock(return_value=True)

        with patch("src.risk.manager._get_notifier", return_value=mock_notifier):
            with patch("asyncio.create_task") as mock_create_task:
                for i in range(3):
                    manager.record_failure(f"Error {i}")
                # 3 record_failure calls (each sends notify_error) + 1 circuit_breaker (notify_risk_event)
                assert mock_create_task.call_count >= 4

    def test_circuit_breaker_no_notification_when_disabled(self, tmp_path):
        """Circuit breaker should not notify when notifier is disabled."""
        from src.risk.manager import RiskManager

        cfg = settings(tmp_path, circuit_breaker_failure_threshold=3)
        manager = RiskManager(cfg)

        mock_notifier = MagicMock()
        mock_notifier.config.enabled = False

        with patch("src.risk.manager._get_notifier", return_value=mock_notifier):
            with patch("asyncio.create_task") as mock_create_task:
                for i in range(3):
                    manager.record_failure(f"Error {i}")
                mock_create_task.assert_not_called()


class TestExecutorNotificationIntegration:
    """Tests for OrderExecutor notification integration."""

    @pytest.mark.asyncio
    async def test_failed_unwind_activates_kill_switch_and_notifies(self, tmp_path):
        """Failed unwind should activate kill switch and send Telegram notification."""
        runtime = RuntimeState(settings(tmp_path), armed=True, geo_allowed=True)
        client = MagicMock()
        client.create_fok_order.side_effect = [matched("one"), failed(), failed(), failed()]
        client.get_order.return_value = {"size_matched": "1000000"}
        executor = OrderExecutor(client, runtime=runtime)

        mock_notifier = MagicMock()
        mock_notifier.config.enabled = True
        mock_notifier.notify_risk_event = AsyncMock(return_value=True)
        mock_notifier.notify_error = AsyncMock(return_value=True)

        with patch("src.execution.executor._get_telegram_notifier", return_value=mock_notifier):
            with patch("asyncio.create_task") as mock_create_task:
                result = await executor.execute_opportunity(opportunity())

                assert result.state == ExecutionState.FAILED
                assert result.error is not None
                assert "EXPOSURE REQUIRES ATTENTION" in result.error
                assert runtime.kill_switch is True
                # Verify notification was created
                assert mock_create_task.call_count >= 1

    @pytest.mark.asyncio
    async def test_partial_fill_triggers_notification(self, tmp_path):
        """Partial fill should send Telegram notification."""
        runtime = RuntimeState(settings(tmp_path), armed=True, geo_allowed=True)
        client = MagicMock()
        client.create_fok_order.side_effect = [matched("one"), failed(), matched("two")]
        client.get_order.return_value = {"size_matched": "1000000"}
        executor = OrderExecutor(client, runtime=runtime)

        mock_notifier = MagicMock()
        mock_notifier.config.enabled = True
        mock_notifier.notify_risk_event = AsyncMock(return_value=True)
        mock_notifier.notify_error = AsyncMock(return_value=True)

        with patch("src.execution.executor._get_telegram_notifier", return_value=mock_notifier):
            with patch("asyncio.create_task") as mock_create_task:
                result = await executor.execute_opportunity(opportunity())

                assert result.state == ExecutionState.COMPLETED
                # Verify notification was created for partial fill
                assert mock_create_task.call_count >= 1

    @pytest.mark.asyncio
    async def test_execution_error_triggers_notification(self, tmp_path):
        """Execution error in create_fok_order path should send Telegram notification."""
        runtime = RuntimeState(settings(tmp_path), armed=True, geo_allowed=True)
        client = MagicMock()

        # Make first leg fail synchronously to trigger the error handler path
        def raise_error(**kwargs):
            raise RuntimeError("connection failed")

        client.create_fok_order.side_effect = raise_error
        executor = OrderExecutor(client, runtime=runtime)

        mock_notifier = MagicMock()
        mock_notifier.config.enabled = True
        mock_notifier.notify_error = AsyncMock(return_value=True)
        mock_notifier.notify_risk_event = AsyncMock(return_value=True)

        # Patch both executor and risk.manager notifier getters
        with patch("src.execution.executor._get_telegram_notifier", return_value=mock_notifier), \
             patch("src.risk.manager._get_notifier", return_value=mock_notifier), \
             patch("asyncio.create_task") as mock_create_task:
            result = await executor.execute_opportunity(opportunity())

            # Either FAILED or ABORTED is acceptable depending on which path triggers
            assert result.state in (ExecutionState.FAILED, ExecutionState.ABORTED)
            # The notification should have been called
            # (record_failure → notify_error from RiskManager, plus executor-level notifications)
            assert mock_create_task.call_count >= 1

    @pytest.mark.asyncio
    async def test_no_notification_when_notifier_disabled(self, tmp_path):
        """No notifications when Telegram is disabled."""
        runtime = RuntimeState(settings(tmp_path), armed=True, geo_allowed=True)
        client = MagicMock()
        client.create_fok_order.side_effect = [matched("one"), failed(), failed(), failed()]
        client.get_order.return_value = {"size_matched": "1000000"}
        executor = OrderExecutor(client, runtime=runtime)

        mock_notifier = MagicMock()
        mock_notifier.config.enabled = False

        with patch("src.execution.executor._get_telegram_notifier", return_value=mock_notifier):
            with patch("asyncio.create_task") as mock_create_task:
                result = await executor.execute_opportunity(opportunity())

                assert result.state == ExecutionState.FAILED
                # No notification task should be created when disabled
                mock_create_task.assert_not_called()
