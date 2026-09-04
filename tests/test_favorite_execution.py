"""
Tests for favorite compounding execution integration.

Tests the execute_opportunity method routing between ArbitrageOpportunity
and FavoriteOpportunity, ensuring proper execution and position tracking.

Run with: pytest tests/test_favorite_execution.py -v
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.execution.executor import (
    ExecutionResult,
    FavoriteExecutionResult,
    OrderExecutor,
    OrderResult,
    OrderStatus,
)
from src.engine.detector import ArbitrageOpportunity, SignalType
from src.engine.favorite import FavoriteOpportunity


class _MockRateLimiter:
    """Minimal async-compatible rate limiter mock."""

    def __init__(self):
        self.stats = {}

    async def acquire_order(self):
        return 0

    async def acquire_request(self):
        return 0

    def reset_stats(self):
        pass


class _MockSettings:
    """Mock settings for testing."""

    trading_mode = "paper"
    leg_timeout_ms = 2000
    emergency_slippage_pct = 0.01
    max_leg_imbalance_usd = 1.0
    max_trade_usd = 1000
    max_total_exposure_usd = 1000


def _make_favorite_opp(price="0.95"):
    """Create a valid FavoriteOpportunity for testing."""
    return FavoriteOpportunity(
        market_id="m1",
        market_question="Test market?",
        favorite_token_id="t1",
        underdog_token_id="t2",
        favorite_price=Decimal(price),
        underdog_price=Decimal("0.05"),
        favorite_bid=Decimal("0.94"),
        favorite_ask=Decimal(price),
        favorite_size=Decimal("1000"),
        time_to_resolution_h=24.0,
        implied_probability=Decimal("0.95"),
        expected_return_pct=Decimal("5.26"),
        position_size_usd=Decimal("50"),
        position_shares=Decimal("52.63"),
        fees_estimate=Decimal("0.5"),
        net_edge=Decimal("0.04"),
        is_profitable=True,
    )


def _make_arb_opp():
    """Create a valid ArbitrageOpportunity for testing."""
    return ArbitrageOpportunity(
        market_id="m1",
        signal_type=SignalType.BUY_SET,
        token_ids=["t1", "t2"],
        prices=[Decimal("0.45"), Decimal("0.45")],
        sizes=[Decimal("100"), Decimal("100")],
        max_size=Decimal("100"),
        total_cost=Decimal("90"),
        expected_payout=Decimal("100"),
        gross_profit=Decimal("10"),
        fees=Decimal("0.02"),
        net_profit=Decimal("9.98"),
        profit_pct=Decimal("0.11"),
    )


class TestExecuteOpportunityRouting:
    """Test that execute_opportunity correctly routes between opportunity types."""

    @pytest.mark.asyncio
    async def test_execute_favorite_opportunity_routes_correctly(self):
        """Test that FavoriteOpportunity routes to _execute_favorite."""
        mock_client = MagicMock()
        mock_client.create_fok_order = MagicMock(
            return_value={"success": True, "orderID": "order123", "status": "matched"}
        )
        mock_client.get_order = MagicMock(return_value={"size_matched": "52630000"})
        mock_client.cancel_order = MagicMock()

        with patch("src.execution.executor.RiskManager") as MockRisk, \
             patch("src.execution.executor.get_runtime") as mock_runtime, \
             patch("src.execution.executor.get_settings"):
            mock_rt = MagicMock()
            mock_rt.execution_lock = asyncio.Lock()
            mock_rt.active_executions = 0
            mock_rt.can_submit_live = MagicMock(return_value=(True, "OK"))
            mock_rt.incomplete_exposure_usd = 0
            mock_runtime.return_value = mock_rt

            mock_risk = MagicMock()
            mock_risk.validate_trade = MagicMock(return_value=(True, "OK"))
            mock_risk.add_favorite_position = MagicMock()
            mock_risk.record_success = MagicMock()
            mock_risk.record_failure = MagicMock()
            MockRisk.return_value = mock_risk

            executor = OrderExecutor(client=mock_client)
            executor._rate_limiter = _MockRateLimiter()

            opp = _make_favorite_opp()
            result = await executor.execute_opportunity(opp)

            assert isinstance(result, FavoriteExecutionResult)
            assert result.opportunity.market_id == "m1"
            assert mock_risk.add_favorite_position.assert_called_once() is None

    @pytest.mark.asyncio
    async def test_execute_arbitrage_opportunity_routes_correctly(self):
        """Test that ArbitrageOpportunity routes to _execute_arbitrage."""
        mock_client = MagicMock()
        mock_client.create_fok_order = MagicMock(
            return_value={"success": True, "orderID": "order123", "status": "matched"}
        )
        mock_client.get_order = MagicMock(return_value={"size_matched": "100000000"})
        mock_client.cancel_order = MagicMock()

        with patch("src.execution.executor.RiskManager") as MockRisk, \
             patch("src.execution.executor.get_runtime") as mock_runtime, \
             patch("src.execution.executor.get_settings"):
            mock_rt = MagicMock()
            mock_rt.execution_lock = asyncio.Lock()
            mock_rt.active_executions = 0
            mock_rt.can_submit_live = MagicMock(return_value=(True, "OK"))
            mock_rt.incomplete_exposure_usd = 0
            mock_runtime.return_value = mock_rt

            mock_risk = MagicMock()
            mock_risk.validate_trade = MagicMock(return_value=(True, "OK"))
            mock_risk.record_success = MagicMock()
            mock_risk.record_failure = MagicMock()
            MockRisk.return_value = mock_risk

            executor = OrderExecutor(client=mock_client)
            executor._rate_limiter = _MockRateLimiter()

            opp = _make_arb_opp()
            result = await executor.execute_opportunity(opp)

            assert isinstance(result, ExecutionResult)
            assert result.opportunity.market_id == "m1"


class TestFavoriteExecutionLogic:
    """Test favorite-specific execution logic."""

    def _setup_executor(self, mock_client, mock_risk=None, mock_runtime=None):
        """Create an executor with mocked dependencies."""
        with patch("src.execution.executor.RiskManager") as MockRisk, \
             patch("src.execution.executor.get_runtime") as mock_runtime_fn, \
             patch("src.execution.executor.get_settings"):
            mock_rt = mock_runtime or MagicMock()
            if mock_rt is not None:
                mock_rt.execution_lock = asyncio.Lock()
                mock_rt.active_executions = 0
                mock_rt.can_submit_live = MagicMock(return_value=(True, "OK"))
                mock_rt.incomplete_exposure_usd = 0
                mock_runtime_fn.return_value = mock_rt

            mock_risk_inst = mock_risk or MagicMock()
            if mock_risk is None:
                mock_risk_inst = MagicMock()
                mock_risk_inst.validate_trade = MagicMock(return_value=(True, "OK"))
                mock_risk_inst.add_favorite_position = MagicMock()
                mock_risk_inst.record_success = MagicMock()
                mock_risk_inst.record_failure = MagicMock()

            MockRisk.return_value = mock_risk_inst

            executor = OrderExecutor(client=mock_client)
            executor._rate_limiter = _MockRateLimiter()
            return executor, mock_risk_inst

    @pytest.mark.asyncio
    async def test_favorite_execution_success(self):
        """Test successful favorite position opening."""
        mock_client = MagicMock()
        mock_client.create_fok_order = MagicMock(
            return_value={"success": True, "orderID": "order123", "status": "matched"}
        )
        mock_client.get_order = MagicMock(return_value={"size_matched": "52630000"})
        mock_client.cancel_order = MagicMock()

        executor, mock_risk = self._setup_executor(mock_client)

        opp = _make_favorite_opp()
        result = await executor.execute_opportunity(opp)

        assert isinstance(result, FavoriteExecutionResult)
        assert result.order is not None
        assert result.order.status == OrderStatus.FILLED
        mock_risk.add_favorite_position.assert_called_once()

    @pytest.mark.asyncio
    async def test_favorite_execution_order_fail(self):
        """Test favorite execution when order fails."""
        mock_client = MagicMock()
        mock_client.create_fok_order = MagicMock(
            return_value={"success": False, "errorMsg": "insufficient liquidity"}
        )
        mock_client.cancel_order = MagicMock()

        executor, mock_risk = self._setup_executor(mock_client)

        opp = _make_favorite_opp()
        result = await executor.execute_opportunity(opp)

        assert isinstance(result, FavoriteExecutionResult)
        assert result.order is not None
        # Position should NOT be added when order fails
        mock_risk.add_favorite_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_favorite_execution_risk_validation_fail(self):
        """Test favorite execution when risk validation fails."""
        mock_client = MagicMock()
        mock_client.create_fok_order = MagicMock()

        executor, mock_risk = self._setup_executor(mock_client)
        mock_risk.validate_trade = MagicMock(
            return_value=(False, "exposure limit exceeded")
        )

        opp = _make_favorite_opp()
        result = await executor.execute_opportunity(opp)

        assert isinstance(result, FavoriteExecutionResult)
        assert result.order is None
        mock_risk.add_favorite_position.assert_not_called()
        mock_client.create_fok_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_favorite_execution_not_live(self):
        """Test favorite execution is aborted when not allowed to submit live."""
        mock_client = MagicMock()
        mock_client.create_fok_order = MagicMock()

        executor, mock_risk = self._setup_executor(mock_client)

        # Override can_submit_live to return False
        executor._runtime.can_submit_live = MagicMock(
            return_value=(False, "trading mode is paper")
        )

        opp = _make_favorite_opp()
        result = await executor.execute_opportunity(opp)

        assert isinstance(result, FavoriteExecutionResult)
        assert result.order is None
        mock_risk.add_favorite_position.assert_not_called()


class TestFavoritePositionDict:
    """Test the position dict format passed to RiskManager."""

    @pytest.mark.asyncio
    async def test_position_dict_has_all_fields(self):
        """Test that favorite position dict has all required fields."""
        mock_client = MagicMock()
        mock_client.create_fok_order = MagicMock(
            return_value={"success": True, "orderID": "order123", "status": "matched"}
        )
        mock_client.get_order = MagicMock(return_value={"size_matched": "52630000"})
        mock_client.cancel_order = MagicMock()

        with patch("src.execution.executor.RiskManager") as MockRisk, \
             patch("src.execution.executor.get_runtime") as mock_runtime, \
             patch("src.execution.executor.get_settings"):
            mock_rt = MagicMock()
            mock_rt.execution_lock = asyncio.Lock()
            mock_rt.active_executions = 0
            mock_rt.can_submit_live = MagicMock(return_value=(True, "OK"))
            mock_rt.incomplete_exposure_usd = 0
            mock_runtime.return_value = mock_rt

            mock_risk = MagicMock()
            mock_risk.validate_trade = MagicMock(return_value=(True, "OK"))
            mock_risk.add_favorite_position = MagicMock()
            MockRisk.return_value = mock_risk

            executor = OrderExecutor(client=mock_client)
            executor._rate_limiter = _MockRateLimiter()

            opp = _make_favorite_opp(price="0.92")
            result = await executor.execute_opportunity(opp)

            assert isinstance(result, FavoriteExecutionResult)
            assert result.order is not None

            call_args = mock_risk.add_favorite_position.call_args
            position_dict = call_args[0][0]

            # Verify all required fields exist
            required_fields = [
                "market_id", "market_question", "token_id", "entry_price",
                "entry_time", "size_shares", "size_usd",
                "take_profit_price", "stop_loss_price", "time_to_resolution_h",
            ]
            for field in required_fields:
                assert field in position_dict, f"Missing field: {field}"
            
            assert position_dict["market_id"] == "m1"
            assert position_dict["market_question"] == "Test market?"
            assert position_dict["token_id"] == "t1"
            assert position_dict["take_profit_price"] == 0.97
            assert position_dict["stop_loss_price"] == 0.80
