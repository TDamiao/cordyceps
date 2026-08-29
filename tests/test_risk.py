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
