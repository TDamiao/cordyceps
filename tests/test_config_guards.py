"""
Tests for configuration safety guards, trade-size limits, and mode guards.

Covers:
- kill_switch: blocks trading when enabled
- paper/live mode guards: validates mutual exclusivity
- max_trade_usd / max_position_size: blocks oversized orders
- min_trade_shares: rejects tiny orders
- orderbook_stale_ms: rejects stale books
"""

import os
import pytest
from decimal import Decimal
from unittest.mock import patch

from src.config import Settings, get_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**overrides) -> Settings:
    """Construct a Settings instance with overridden env vars."""
    env = {
        "PRIVATE_KEY": "",
        "PROXY_ADDRESS": "",
        "TRADING_MODE": "paper",
        "LIVE_TRADING_ENABLED": "false",
        "KILL_SWITCH": "false",
        "DRY_RUN": "true",
        **{k.upper(): str(v) for k, v in overrides.items()},
    }
    get_settings.cache_clear()
    with patch.dict(os.environ, env, clear=True):
        return Settings()


# ---------------------------------------------------------------------------
# Kill-switch tests
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_kill_switch_default_off(self):
        s = _make_settings()
        assert s.kill_switch is False

    def test_kill_switch_can_be_enabled(self):
        s = _make_settings(kill_switch="true")
        assert s.kill_switch is True

    def test_kill_switch_flag_in_config(self):
        s = _make_settings(kill_switch="true")
        assert s.kill_switch is True
        # kill_switch should not conflict with any mode
        assert s.trading_mode == "paper"


# ---------------------------------------------------------------------------
# Paper / live mode guard tests
# ---------------------------------------------------------------------------

class TestPaperLiveGuards:
    def test_paper_mode_default(self):
        s = _make_settings()
        assert s.trading_mode == "paper"
        assert s.dry_run is True

    def test_live_mode_rejects_without_private_key(self):
        with pytest.raises(ValueError, match="PRIVATE_KEY"):
            _make_settings(
                trading_mode="live",
                live_trading_enabled="true",
                private_key="",
                dry_run="false",
            )

    def test_live_mode_rejects_with_dry_run(self):
        with pytest.raises(ValueError, match="DRY_RUN"):
            _make_settings(
                trading_mode="live",
                live_trading_enabled="true",
                private_key="0x" + "a" * 64,
                proxy_address="0x" + "b" * 40,
                dry_run="true",
            )

    def test_live_mode_requires_live_trading_enabled(self):
        with pytest.raises(ValueError, match="LIVE_TRADING_ENABLED"):
            _make_settings(
                trading_mode="live",
                live_trading_enabled="false",
                private_key="0x" + "a" * 64,
                proxy_address="0x" + "b" * 40,
                dry_run="false",
            )

    def test_paper_mode_rejects_live_trading_enabled(self):
        with pytest.raises(ValueError, match="LIVE_TRADING_ENABLED must be false"):
            _make_settings(
                trading_mode="paper",
                live_trading_enabled="true",
            )

    def test_paper_mode_no_private_key_required(self):
        s = _make_settings()
        assert s.trading_mode == "paper"
        assert s.private_key == ""


# ---------------------------------------------------------------------------
# Trade-size limit tests
# ---------------------------------------------------------------------------

class TestTradeSizeLimits:
    def test_max_trade_usd_default(self):
        s = _make_settings()
        assert s.max_trade_usd > 0

    def test_max_trade_usd_cannot_be_zero(self):
        with pytest.raises(ValueError):
            _make_settings(max_trade_usd="0")

    def test_max_position_size_default(self):
        s = _make_settings()
        assert s.max_position_size > 0

    def test_max_total_exposure_usd_default(self):
        s = _make_settings()
        assert s.max_total_exposure_usd > 0


# ---------------------------------------------------------------------------
# Min-trade / min-liquidity / stale-book tests
# ---------------------------------------------------------------------------

class TestMinTradeAndStaleGuards:
    def test_min_trade_shares_default(self):
        s = _make_settings()
        assert s.min_trade_shares > 0

    def test_orderbook_stale_ms_default(self):
        s = _make_settings()
        assert s.orderbook_stale_ms > 0

    def test_orderbook_stale_ms_can_be_zero(self):
        s = _make_settings(orderbook_stale_ms="0")
        assert s.orderbook_stale_ms == 0


# ---------------------------------------------------------------------------
# Engine respects trade-size limits
# ---------------------------------------------------------------------------

class TestEngineRespectsLimits:
    def _engine(self, **overrides):
        from src.engine.detector import ArbitrageConfig, ArbitrageEngine
        base = dict(
            taker_fee=Decimal("0"),
            min_profit_threshold=Decimal("0"),
            min_liquidity=Decimal("1"),
        )
        base.update(overrides)
        return ArbitrageEngine(ArbitrageConfig(**base))

    def test_max_position_size_caps_opportunity(self):
        engine = self._engine(max_position_size=Decimal("5"))
        from src.client.models import OrderBook, OrderBookLevel

        books = {
            "yes": OrderBook("yes", asks=[OrderBookLevel(Decimal("0.40"), Decimal("100"))]),
            "no": OrderBook("no", asks=[OrderBookLevel(Decimal("0.40"), Decimal("100"))]),
        }
        opp = engine.analyze_market("m-max", books)
        assert opp is not None
        assert opp.max_size == Decimal("5")

    def test_min_liquidity_rejects_thin_book(self):
        engine = self._engine(min_liquidity=Decimal("50"))
        from src.client.models import OrderBook, OrderBookLevel

        books = {
            "yes": OrderBook("yes", asks=[OrderBookLevel(Decimal("0.40"), Decimal("10"))]),
            "no": OrderBook("no", asks=[OrderBookLevel(Decimal("0.40"), Decimal("10"))]),
        }
        opp = engine.analyze_market("m-thin", books)
        assert opp is None

    def test_stale_book_rejected(self):
        import time
        engine = self._engine(orderbook_stale_ms=1000)
        from src.client.models import OrderBook, OrderBookLevel

        now_ms = int(time.time() * 1000)
        books = {
            "yes": OrderBook("yes", asks=[OrderBookLevel(Decimal("0.40"), Decimal("100"))], timestamp=now_ms - 5000),
            "no": OrderBook("no", asks=[OrderBookLevel(Decimal("0.40"), Decimal("100"))], timestamp=now_ms),
        }
        opp = engine.analyze_market("m-stale", books)
        assert opp is None
        assert engine.stats["stale_books_rejected"] >= 1

    def test_fresh_book_accepted(self):
        import time
        engine = self._engine(orderbook_stale_ms=10000)
        from src.client.models import OrderBook, OrderBookLevel

        now_ms = int(time.time() * 1000)
        books = {
            "yes": OrderBook("yes", asks=[OrderBookLevel(Decimal("0.40"), Decimal("100"))], timestamp=now_ms),
            "no": OrderBook("no", asks=[OrderBookLevel(Decimal("0.40"), Decimal("100"))], timestamp=now_ms),
        }
        opp = engine.analyze_market("m-fresh", books)
        assert opp is not None


# ---------------------------------------------------------------------------
# Risk manager circuit breaker
# ---------------------------------------------------------------------------

class TestRiskManagerCircuitBreaker:
    def test_circuit_breaker_triggers_after_threshold(self):
        from src.risk.manager import RiskManager, RiskState
        from unittest.mock import MagicMock, patch

        mock_settings = MagicMock()
        mock_settings.max_daily_loss = 1000.0
        mock_settings.circuit_breaker_failure_threshold = 3
        mock_settings.circuit_breaker_cooldown_minutes = 1

        with patch("src.risk.manager.get_settings", return_value=mock_settings):
            rm = RiskManager()
            rm._state = RiskState()

            for i in range(3):
                rm.record_failure(f"err{i}")

            assert rm._state.is_paused is True
            allowed, reason = rm.can_trade()
            assert not allowed
            assert "Circuit breaker" in reason
