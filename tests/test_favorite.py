"""
Tests for Favorite Compounding strategy engine and Telegram notifier.

Run with:
    pytest tests/test_favorite.py -v
    pytest tests/test_telegram.py -v
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ------------------------------------------------------------------
# Favorite Engine
# ------------------------------------------------------------------


class TestFavoriteConfig:
    def test_defaults(self):
        from src.engine.favorite import FavoriteConfig

        cfg = FavoriteConfig()
        assert cfg.min_probability == Decimal("0.90")
        assert cfg.min_price == Decimal("0.85")
        assert cfg.max_price == Decimal("0.98")
        assert cfg.min_size_usd == Decimal("5.0")
        assert cfg.take_profit == Decimal("0.97")
        assert cfg.stop_loss == Decimal("0.80")
        assert cfg.max_time_to_resolution_h == 72


class TestFavoriteOpportunity:
    def test_price_cents(self):
        from src.engine.favorite import FavoriteOpportunity

        opp = FavoriteOpportunity(
            market_id="m1",
            market_question="Q",
            favorite_token_id="t1",
            underdog_token_id="t2",
            favorite_price=Decimal("0.95"),
            underdog_price=Decimal("0.05"),
            favorite_bid=Decimal("0.945"),
            favorite_ask=Decimal("0.95"),
            favorite_size=Decimal("1000"),
            time_to_resolution_h=24.0,
            implied_probability=Decimal("0.95"),
            expected_return_pct=Decimal("5.263157894736842"),
            position_size_usd=Decimal("50"),
            position_shares=Decimal("52.63157894736842"),
            fees_estimate=Decimal("0.5"),
            net_edge=Decimal("0.04"),
            is_profitable=True,
        )
        assert opp.price_cents == 95


class TestFavoriteEngine:
    def _make_book(self, ask_price, bid_price, ask_size=1000, bid_size=1000, token_id="token-1"):
        from src.client.models import OrderBook, OrderBookLevel

        return OrderBook(
            token_id=token_id,
            asks=[OrderBookLevel(price=Decimal(str(ask_price)), size=Decimal(str(ask_size)))],
            bids=[OrderBookLevel(price=Decimal(str(bid_price)), size=Decimal(str(bid_size)))],
            timestamp=int(1000 * __import__("time").time()),
        )

    def test_rejects_non_binary(self):
        from src.engine.favorite import FavoriteEngine

        engine = FavoriteEngine()
        opp = engine.analyze_market("m1", "Q?", {}, 24.0)
        assert opp is None

    def test_rejects_time_to_resolution_too_large(self):
        from src.engine.favorite import FavoriteEngine

        engine = FavoriteEngine()
        # Favorite at 0.95 (prob=0.95 >= 0.90), in price range [0.85, 0.98], sufficient liquidity
        books = {
            "t1": self._make_book(0.95, 0.94),
            "t2": self._make_book(0.05, 0.04),
        }
        opp = engine.analyze_market("m1", "Q?", books, 999.0)
        assert opp is None
        assert engine.get_metrics()["rejected_time"] == 1

    def test_rejects_price_out_of_range(self):
        """Price below 0.85 or above 0.98 should be rejected."""
        from src.engine.favorite import FavoriteEngine

        engine = FavoriteEngine()
        # Favorite at 0.80 < 0.85 -> rejected for price
        books = {
            "t1": self._make_book(0.80, 0.79, ask_size=1000, bid_size=1000),
            "t2": self._make_book(0.20, 0.19, ask_size=1000, bid_size=1000),
        }
        opp = engine.analyze_market("m1", "Q?", books, 24.0)
        assert opp is None
        assert engine.get_metrics()["rejected_price"] == 1

    def test_rejects_price_above_max(self):
        """Price above 0.98 should be rejected."""
        from src.engine.favorite import FavoriteEngine

        engine = FavoriteEngine()
        # Favorite at 0.99 > 0.98 -> rejected for price
        books = {
            "t1": self._make_book(0.99, 0.98, ask_size=1000, bid_size=1000),
            "t2": self._make_book(0.01, 0.005, ask_size=1000, bid_size=1000),
        }
        opp = engine.analyze_market("m1", "Q?", books, 24.0)
        assert opp is None
        assert engine.get_metrics()["rejected_price"] == 1

    def test_rejects_low_probability(self):
        """Price passes [0.85, 0.98] but probability (price itself) < 0.90."""
        from src.engine.favorite import FavoriteEngine

        engine = FavoriteEngine()
        # Favorite at 0.88 (in price range) but prob=0.88 < 0.90 -> rejected for probability
        books = {
            "t1": self._make_book(0.88, 0.87),
            "t2": self._make_book(0.12, 0.11),
        }
        opp = engine.analyze_market("m1", "Q?", books, 24.0)
        assert opp is None
        assert engine.get_metrics()["rejected_probability"] == 1

    def test_rejects_low_liquidity(self):
        from src.engine.favorite import FavoriteEngine

        engine = FavoriteEngine()
        books = {
            "t1": self._make_book(0.95, 0.94, ask_size=1, bid_size=1),
            "t2": self._make_book(0.05, 0.04, ask_size=1, bid_size=1),
        }
        opp = engine.analyze_market("m1", "Q?", books, 24.0)
        assert opp is None
        assert engine.get_metrics()["rejected_liquidity"] == 1

    def test_rejects_negative_net_edge(self):
        from src.engine.favorite import FavoriteEngine

        engine = FavoriteEngine()
        book_a = self._make_book(0.95, 0.94, ask_size=10000, bid_size=10000)
        book_b = self._make_book(0.05, 0.04, ask_size=10000, bid_size=10000)

        # Force fees by passing fee params that blow the edge.
        from src.fees import FeeParameters

        fee_params = FeeParameters(rate=Decimal("5.0"), exponent=Decimal("1"), source="test")

        opp = engine.analyze_market(
            "m1",
            "Q?",
            {"t1": book_a, "t2": book_b},
            24.0,
            fee_params=fee_params,
        )
        assert opp is None
        assert engine.get_metrics()["rejected_fee"] == 1

    def test_returns_opportunity_on_valid_favorite(self):
        from src.engine.favorite import FavoriteEngine

        engine = FavoriteEngine()
        book_a = self._make_book(0.95, 0.94, ask_size=10000, bid_size=10000)
        book_b = self._make_book(0.05, 0.04, ask_size=10000, bid_size=10000)
        opp = engine.analyze_market("m1", "Favorite market?", {"t1": book_a, "t2": book_b}, 12.0)
        assert opp is not None
        assert opp.is_profitable is True
        assert opp.favorite_price == Decimal("0.95")
        assert Decimal("0.85") <= opp.favorite_price <= Decimal("0.98")
        assert engine.get_metrics()["opportunities_found"] == 1

    def test_check_position_take_profit(self):
        from src.engine.favorite import FavoriteEngine, FavoritePosition, FavoriteAction

        engine = FavoriteEngine()
        position = FavoritePosition(
            market_id="m1",
            market_question="Q?",
            token_id="t1",
            entry_price=Decimal("0.90"),
            entry_time=__import__("time").time() - 100,
            size_shares=Decimal("100"),
            size_usd=Decimal("90"),
            take_profit_price=Decimal("0.97"),
            stop_loss_price=Decimal("0.80"),
            time_to_resolution_h=24.0,
        )
        action = engine.check_position(position, Decimal("0.97"), Decimal("0.96"))
        assert action == FavoriteAction.TAKE_PROFIT
        assert position.action == FavoriteAction.TAKE_PROFIT

    def test_check_position_stop_loss(self):
        from src.engine.favorite import FavoriteEngine, FavoritePosition, FavoriteAction

        engine = FavoriteEngine()
        position = FavoritePosition(
            market_id="m1",
            market_question="Q?",
            token_id="t1",
            entry_price=Decimal("0.90"),
            entry_time=__import__("time").time() - 100,
            size_shares=Decimal("100"),
            size_usd=Decimal("90"),
            take_profit_price=Decimal("0.97"),
            stop_loss_price=Decimal("0.80"),
            time_to_resolution_h=24.0,
        )
        action = engine.check_position(position, Decimal("0.75"), Decimal("0.75"))
        assert action == FavoriteAction.STOP_LOSS
        assert position.action == FavoriteAction.STOP_LOSS

    def test_check_position_time_exit(self):
        from src.engine.favorite import FavoriteEngine, FavoritePosition, FavoriteAction

        engine = FavoriteEngine()
        position = FavoritePosition(
            market_id="m1",
            market_question="Q?",
            token_id="t1",
            entry_price=Decimal("0.90"),
            entry_time=__import__("time").time() - 87000,
            size_shares=Decimal("100"),
            size_usd=Decimal("90"),
            take_profit_price=Decimal("0.97"),
            stop_loss_price=Decimal("0.80"),
            time_to_resolution_h=2.0,
        )
        action = engine.check_position(position, Decimal("0.91"), Decimal("0.905"))
        assert action == FavoriteAction.TAKE_PROFIT

    def test_create_position_from_opportunity(self):
        from src.engine.favorite import FavoriteEngine, FavoriteOpportunity

        engine = FavoriteEngine()
        opp = FavoriteOpportunity(
            market_id="m1",
            market_question="Q?",
            favorite_token_id="t1",
            underdog_token_id="t2",
            favorite_price=Decimal("0.95"),
            underdog_price=Decimal("0.05"),
            favorite_bid=Decimal("0.94"),
            favorite_ask=Decimal("0.95"),
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
        pos = engine.create_position(opp)
        assert pos.entry_price == Decimal("0.95")
        assert pos.take_profit_price == Decimal("0.97")
        assert pos.stop_loss_price == Decimal("0.80")
        assert pos.size_usd == Decimal("50")


# ------------------------------------------------------------------
# Telegram Notifier
# ------------------------------------------------------------------


class TestTelegramConfig:
    def test_from_env_disabled_without_creds(self):
        from src.notifications.telegram import TelegramConfig

        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "src.notifications.telegram.get_settings",
                return_value=MagicMock(
                    telegram_bot_token="", telegram_chat_id=""
                ),
            ):
                cfg = TelegramConfig.from_env()
        assert cfg.enabled is False
        assert cfg.bot_token == ""
        assert cfg.chat_id == ""


class TestTelegramNotifier:
    @pytest.fixture()
    def notifier(self):
        from src.notifications.telegram import TelegramConfig, TelegramNotifier

        cfg = TelegramConfig(bot_token="TOKEN", chat_id="123", enabled=True)
        return TelegramNotifier(config=cfg)

    @pytest.mark.asyncio
    async def test_disabled_skips_send(self, notifier):
        notifier.config.enabled = False
        assert await notifier.send_message("hello") is False

    @pytest.mark.asyncio
    async def test_send_message_success(self, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"ok":true}')

        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_post_cm)

        notifier._session = mock_session
        result = await notifier.send_message("<b>bold</b>", parse_mode="HTML")
        assert result is True

    @pytest.mark.asyncio
    async def test_send_message_failure_returns_false(self, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 400
        mock_resp.text = AsyncMock(return_value='{"ok":false,"error":"bad request"}')

        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_post_cm)

        notifier._session = mock_session
        result = await notifier.send_message("text")
        assert result is False

    @pytest.mark.asyncio
    async def test_notify_startup(self, notifier):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"ok":true}')

        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_post_cm)

        notifier._session = mock_session
        with patch(
            "src.notifications.telegram.get_settings",
            return_value=MagicMock(
                trading_mode="paper",
                max_total_exposure_usd=2.0,
                max_trade_usd=1.0,
                enable_favorite_strategy=False,
            ),
        ):
            result = await notifier.notify_startup()
        assert result is True
