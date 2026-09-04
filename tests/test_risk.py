"""
Unit tests for RiskManager.
"""

import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.config import Settings
from src.risk.manager import RiskManager, RiskState


@pytest.fixture
def mock_settings():
    """Mock configuration settings."""
    settings = MagicMock(spec=Settings)
    settings.max_daily_loss = 50.0
    settings.circuit_breaker_failure_threshold = 3
    settings.circuit_breaker_cooldown_minutes = 1  # Short for testing
    settings.max_slippage_tolerance = 0.02
    return settings


@pytest.fixture
def risk_manager(mock_settings):
    """Initialize RiskManager with mocked settings."""
    with patch("src.risk.manager.get_settings", return_value=mock_settings):
        manager = RiskManager()
        # Reset state for clean test
        manager._state = RiskState()
        return manager


class TestRiskManager:
    """Tests for RiskManager logic."""

    def test_initial_state(self, risk_manager):
        """Test that trading is allowed initially."""
        allowed, reason = risk_manager.can_trade()
        assert allowed is True
        assert reason == "OK"
        assert risk_manager._state.consecutive_failures == 0

    def test_circuit_breaker_trigger(self, risk_manager):
        """Test that circuit breaker triggers after N failures."""
        # Record 2 failures (threshold is 3)
        risk_manager.record_failure("Error 1")
        risk_manager.record_failure("Error 2")

        # Should still be allowed
        allowed, _ = risk_manager.can_trade()
        assert allowed is True
        assert risk_manager._state.consecutive_failures == 2

        # Record 3rd failure
        risk_manager.record_failure("Error 3")

        # Should now be paused
        assert risk_manager._state.is_paused is True
        allowed, reason = risk_manager.can_trade()
        assert allowed is False
        assert "Circuit breaker active" in reason

    def test_circuit_breaker_cooldown(self, risk_manager):
        """Test that circuit breaker resets after cooldown."""
        # Trigger it
        for i in range(3):
            risk_manager.record_failure(f"Error {i}")

        assert risk_manager._state.is_paused is True

        # Mock time forward satisfy cooldown (61 seconds)
        future_time = time.time() + 61
        with patch("time.time", return_value=future_time):
            allowed, reason = risk_manager.can_trade()
            assert allowed is True
            assert risk_manager._state.is_paused is False
            assert risk_manager._state.consecutive_failures == 0

    def test_daily_loss_limit(self, risk_manager):
        """Test that trading stops if daily loss exceeded."""
        # Lose $40 (Limit $50)
        risk_manager._state.daily_pnl = Decimal("-40")
        assert risk_manager.can_trade()[0] is True

        # Lose another $15 (Total -$55)
        risk_manager._state.daily_pnl = Decimal("-55")

        allowed, reason = risk_manager.can_trade()
        assert allowed is False
        assert "Daily loss limit exceeded" in reason

    def test_success_resets_failures(self, risk_manager):
        """Test that a successful trade resets consecutive failure count."""
        risk_manager.record_failure("Oops")
        risk_manager.record_failure("Oops 2")
        assert risk_manager._state.consecutive_failures == 2

        risk_manager.record_success(Decimal("1.0"))
        assert risk_manager._state.consecutive_failures == 0
        assert risk_manager._state.daily_pnl == Decimal("1.0")

    def test_slippage_check(self, risk_manager):
        """Test slippage protection logic."""
        # Profitable spread
        assert risk_manager.check_slippage(Decimal("0.4"), Decimal("0.5")) is True

        # Break even
        assert risk_manager.check_slippage(Decimal("0.5"), Decimal("0.5")) is False

        # Loss
        assert risk_manager.check_slippage(Decimal("0.6"), Decimal("0.5")) is False

    def test_add_favorite_position(self, risk_manager):
        """Test adding a favorite position updates exposure."""
        position = {
            "market_id": "m1",
            "market_question": "Test market?",
            "token_id": "t1",
            "entry_price": 0.90,
            "entry_time": time.time(),
            "size_shares": 100.0,
            "size_usd": 90.0,
            "take_profit_price": 0.97,
            "stop_loss_price": 0.80,
            "time_to_resolution_h": 24.0,
        }

        initial_exposure = risk_manager._current_exposure
        risk_manager.add_favorite_position(position)

        assert len(risk_manager.get_favorite_positions()) == 1
        assert risk_manager._current_exposure == initial_exposure + Decimal("90.0")
        assert risk_manager._open_trades == 1

    def test_update_favorite_position_hold(self, risk_manager):
        """Test updating position that should hold."""
        position = {
            "market_id": "m1",
            "market_question": "Test market?",
            "token_id": "t1",
            "entry_price": 0.90,
            "entry_time": time.time(),
            "size_shares": 100.0,
            "size_usd": 90.0,
            "take_profit_price": 0.97,
            "stop_loss_price": 0.80,
            "time_to_resolution_h": 24.0,
        }
        risk_manager.add_favorite_position(position)

        # Price moved slightly up but not at TP
        risk_manager.update_favorite_position("m1", Decimal("0.92"), Decimal("0.915"))

        positions = risk_manager.get_favorite_positions()
        assert positions[0]["action"] == "HOLD"
        assert positions[0]["current_price"] == 0.92
        assert risk_manager._open_trades == 1  # Still open

    def test_update_favorite_position_take_profit(self, risk_manager):
        """Test take profit triggers correctly."""
        position = {
            "market_id": "m1",
            "market_question": "Test market?",
            "token_id": "t1",
            "entry_price": 0.90,
            "entry_time": time.time(),
            "size_shares": 100.0,
            "size_usd": 90.0,
            "take_profit_price": 0.97,
            "stop_loss_price": 0.80,
            "time_to_resolution_h": 24.0,
        }
        risk_manager.add_favorite_position(position)

        # Price hits take profit
        risk_manager.update_favorite_position("m1", Decimal("0.97"), Decimal("0.965"))

        positions = risk_manager.get_favorite_positions()
        assert positions[0]["action"] == "TAKE_PROFIT"
        assert risk_manager._open_trades == 0  # Closed
        assert risk_manager._current_exposure == Decimal("0")

    def test_update_favorite_position_stop_loss(self, risk_manager):
        """Test stop loss triggers correctly."""
        position = {
            "market_id": "m1",
            "market_question": "Test market?",
            "token_id": "t1",
            "entry_price": 0.90,
            "entry_time": time.time(),
            "size_shares": 100.0,
            "size_usd": 90.0,
            "take_profit_price": 0.97,
            "stop_loss_price": 0.80,
            "time_to_resolution_h": 24.0,
        }
        risk_manager.add_favorite_position(position)

        # Bid price hits stop loss
        risk_manager.update_favorite_position("m1", Decimal("0.78"), Decimal("0.77"))

        positions = risk_manager.get_favorite_positions()
        assert positions[0]["action"] == "STOP_LOSS"
        assert risk_manager._open_trades == 0  # Closed
        assert risk_manager._current_exposure == Decimal("0")

    def test_update_favorite_position_time_exit(self, risk_manager):
        """Test time-based exit when < 1h to resolution and in profit."""
        entry_time = time.time() - 23 * 3600  # 23 hours ago
        position = {
            "market_id": "m1",
            "market_question": "Test market?",
            "token_id": "t1",
            "entry_price": 0.90,
            "entry_time": entry_time,
            "size_shares": 100.0,
            "size_usd": 90.0,
            "take_profit_price": 0.97,
            "stop_loss_price": 0.80,
            "time_to_resolution_h": 24.0,
        }
        risk_manager.add_favorite_position(position)

        # Price slightly up, < 1h to resolution
        risk_manager.update_favorite_position("m1", Decimal("0.91"), Decimal("0.905"))

        positions = risk_manager.get_favorite_positions()
        assert positions[0]["action"] == "TAKE_PROFIT"
        assert risk_manager._open_trades == 0  # Closed
