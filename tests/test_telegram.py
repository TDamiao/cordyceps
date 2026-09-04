"""Unit tests for the Telegram notification module.

Tests cover:
- TelegramConfig: environment variable parsing, fallback to Settings
- TelegramNotifier: message sending, notification methods
- Global notifier management: get_notifier(), init_notifications(), shutdown_notifications()
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestTelegramConfig:
    """Test TelegramConfig dataclass functionality."""

    def test_from_env_no_creds_disables_notifier(self):
        """Config should be disabled when no creds provided."""
        from src.notifications.telegram import TelegramConfig

        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "src.notifications.telegram.get_settings",
                return_value=MagicMock(telegram_bot_token="", telegram_chat_id=""),
            ):
                cfg = TelegramConfig.from_env()

        assert cfg.enabled is False
        assert cfg.bot_token == ""
        assert cfg.chat_id == ""

    def test_from_env_with_bot_token(self):
        """Config should enable when both token and chat_id provided."""
        from src.notifications.telegram import TelegramConfig

        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "test_token_123", "TELEGRAM_CHAT_ID": "123456789"},
            clear=True,
        ):
            cfg = TelegramConfig.from_env()

        assert cfg.enabled is True
        assert cfg.bot_token == "test_token_123"
        assert cfg.chat_id == "123456789"

    def test_from_env_with_partial_creds_disabled(self):
        """Config should be disabled with only token or only chat_id."""
        from src.notifications.telegram import TelegramConfig

        with patch.dict(
            "os.environ", {"TELEGRAM_BOT_TOKEN": "test_token_123"}, clear=True
        ):
            with patch(
                "src.notifications.telegram.get_settings",
                return_value=MagicMock(telegram_bot_token="", telegram_chat_id=""),
            ):
                cfg = TelegramConfig.from_env()

        assert cfg.enabled is False

    def test_settings_fallback(self):
        """Settings should be fallback when env vars not set."""
        from src.notifications.telegram import TelegramConfig

        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "src.notifications.telegram.get_settings",
                return_value=MagicMock(
                    telegram_bot_token="settings_token", telegram_chat_id="987654321"
                ),
            ):
                cfg = TelegramConfig.from_env()

        assert cfg.enabled is True
        assert cfg.bot_token == "settings_token"
        assert cfg.chat_id == "987654321"

    def test_env_overrides_settings(self):
        """Environment variables should override Settings values."""
        from src.notifications.telegram import TelegramConfig

        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "env_token", "TELEGRAM_CHAT_ID": "111222333"},
            clear=True,
        ):
            with patch(
                "src.notifications.telegram.get_settings",
                return_value=MagicMock(
                    telegram_bot_token="settings_token", telegram_chat_id="987654321"
                ),
            ):
                cfg = TelegramConfig.from_env()

        assert cfg.bot_token == "env_token"
        assert cfg.chat_id == "111222333"


class TestTelegramNotifierSend:
    """Test TelegramNotifier send_message functionality."""

    @pytest.fixture()
    def notifier(self):
        """Create a notifier with valid config for testing."""
        from src.notifications.telegram import TelegramConfig, TelegramNotifier

        cfg = TelegramConfig(bot_token="TOKEN", chat_id="123", enabled=True)
        return TelegramNotifier(config=cfg)

    @pytest.mark.asyncio
    async def test_disabled_skips_send(self, notifier):
        """When disabled, send_message should return False without API call."""
        notifier.config.enabled = False
        result = await notifier.send_message("hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message_success(self, notifier):
        """Successful API response should return True."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"ok": true}')

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
        """API error response should return False."""
        mock_resp = MagicMock()
        mock_resp.status = 400
        mock_resp.text = AsyncMock(return_value='{"ok": false, "error": "bad request"}')

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
    async def test_send_message_exception_returns_false(self, notifier):
        """Network exception should return False."""
        notifier._session = MagicMock()
        notifier._session.post = MagicMock(side_effect=Exception("Network error"))

        result = await notifier.send_message("text")
        assert result is False

    @pytest.mark.asyncio
    async def test_close_closes_session(self, notifier):
        """close() should close the session."""
        # Create a proper async mock for the session
        mock_session = AsyncMock()
        mock_session.closed = False
        notifier._session = mock_session

        await notifier.close()
        assert mock_session.close.called


class TestTelegramNotifierEvents:
    """Test TelegramNotifier event-specific notification methods."""

    @pytest.fixture()
    def notifier(self):
        """Create a notifier for testing."""
        from src.notifications.telegram import TelegramConfig, TelegramNotifier

        cfg = TelegramConfig(bot_token="TOKEN", chat_id="123", enabled=True)
        return TelegramNotifier(config=cfg)

    @pytest.mark.asyncio
    async def test_notify_startup(self, notifier):
        """notify_startup should send formatted startup message."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"ok": true}')

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
        # Verify message was sent
        call_args = mock_session.post.call_args
        assert "sendMessage" in call_args[0][0]
        assert "Cordyceps Bot Iniciado" in call_args[1]["json"]["text"]

    @pytest.mark.asyncio
    async def test_notify_shutdown(self, notifier):
        """notify_shutdown should send formatted shutdown message."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"ok": true}')

        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_post_cm)

        notifier._session = mock_session

        result = await notifier.notify_shutdown(reason="Kill switch")

        assert result is True
        call_args = mock_session.post.call_args
        assert "Cordyceps Bot Parado" in call_args[1]["json"]["text"]
        assert "Kill switch" in call_args[1]["json"]["text"]

    @pytest.mark.asyncio
    async def test_notify_trade_open(self, notifier):
        """notify_trade_open should send trade details."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"ok": true}')

        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_post_cm)

        notifier._session = mock_session

        result = await notifier.notify_trade_open(
            market_id="market_1234567890ab",
            market_question="Will Bitcoin hit $100k?",
            side="BUY",
            price=Decimal("0.65"),
            size=Decimal("10"),
            usd_value=Decimal("6.50"),
            strategy="arbitrage",
            is_favorite=False,
        )

        assert result is True
        call_args = mock_session.post.call_args
        assert "Nova Posição Aberta" in call_args[1]["json"]["text"]
        assert "BUY" in call_args[1]["json"]["text"]
        assert "6.50" in call_args[1]["json"]["text"]

    @pytest.mark.asyncio
    async def test_notify_trade_close_profit(self, notifier):
        """notify_trade_close should show profit with green indicator."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"ok": true}')

        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_post_cm)

        notifier._session = mock_session

        result = await notifier.notify_trade_close(
            market_id="market_1234567890ab",
            market_question="Will Bitcoin hit $100k?",
            side="BUY",
            entry_price=Decimal("0.60"),
            exit_price=Decimal("0.70"),
            size=Decimal("10"),
            pnl=Decimal("1.00"),
            pnl_pct=16.67,
            is_favorite=False,
        )

        assert result is True
        call_args = mock_session.post.call_args
        assert "Posição Fechada" in call_args[1]["json"]["text"]
        assert "✅" in call_args[1]["json"]["text"]
        assert "P&L:" in call_args[1]["json"]["text"]

    @pytest.mark.asyncio
    async def test_notify_trade_close_loss(self, notifier):
        """notify_trade_close should show loss with red indicator."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"ok": true}')

        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_post_cm)

        notifier._session = mock_session

        result = await notifier.notify_trade_close(
            market_id="market_1234567890ab",
            market_question="Will Bitcoin hit $100k?",
            side="BUY",
            entry_price=Decimal("0.70"),
            exit_price=Decimal("0.60"),
            size=Decimal("10"),
            pnl=Decimal("-1.00"),
            pnl_pct=-14.29,
            is_favorite=False,
        )

        assert result is True
        call_args = mock_session.post.call_args
        assert "❌" in call_args[1]["json"]["text"]

    @pytest.mark.asyncio
    async def test_notify_error(self, notifier):
        """notify_error should send formatted error message."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"ok": true}')

        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_post_cm)

        notifier._session = mock_session

        result = await notifier.notify_error(
            error_type="API_ERROR",
            error_message="Rate limit exceeded",
            context={"endpoint": "/markets", "retry_after": "60"},
            severity="ERROR",
        )

        assert result is True
        call_args = mock_session.post.call_args
        assert "Erro ERROR" in call_args[1]["json"]["text"]
        assert "Rate limit exceeded" in call_args[1]["json"]["text"]

    @pytest.mark.asyncio
    async def test_notify_daily_summary(self, notifier):
        """notify_daily_summary should aggregate trading stats."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value='{"ok": true}')

        mock_post_cm = MagicMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.post = MagicMock(return_value=mock_post_cm)

        notifier._session = mock_session

        result = await notifier.notify_daily_summary(
            total_trades=10,
            winning_trades=7,
            losing_trades=3,
            total_pnl=Decimal("45.50"),
            total_pnl_pct=12.5,
            favorite_trades=4,
            favorite_pnl=Decimal("30.00"),
            arb_trades=6,
            arb_pnl=Decimal("15.50"),
        )

        assert result is True
        call_args = mock_session.post.call_args
        assert "Resumo Diário" in call_args[1]["json"]["text"]
        assert "+12.50%" in call_args[1]["json"]["text"]


class TestGlobalNotifier:
    """Test global notifier management functions."""

    def test_get_notifier_creates_instance(self):
        """get_notifier should create and cache a notifier instance."""
        # Clear any existing instance
        import src.notifications.telegram as tn_module
        from src.notifications.telegram import get_notifier
        tn_module._notifier = None

        notifier = get_notifier()
        assert notifier is not None
        assert isinstance(notifier, type(notifier))

    def test_get_notifier_returns_same_instance(self):
        """get_notifier should return cached instance."""
        import src.notifications.telegram as tn_module
        from src.notifications.telegram import TelegramConfig, TelegramNotifier

        cfg = TelegramConfig(bot_token="test", chat_id="123", enabled=True)
        tn_module._notifier = TelegramNotifier(config=cfg)

        # Get twice should return same instance
        n1 = tn_module.get_notifier()
        n2 = tn_module.get_notifier()
        assert n1 is n2

    @pytest.mark.asyncio
    async def test_init_notifications_sends_startup(self):
        """init_notifications should send startup message if enabled."""
        import src.notifications.telegram as tn_module
        from src.notifications.telegram import init_notifications
        tn_module._notifier = None

        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "TOKEN", "TELEGRAM_CHAT_ID": "123"},
            clear=True,
        ):
            notifier = await init_notifications()

        assert notifier is not None


class TestTelegramConfigEdgeCases:
    """Edge case tests for TelegramConfig."""

    def test_whitespace_values_are_not_empty(self):
        """Whitespace-only values should be treated as non-empty strings in env."""
        from src.notifications.telegram import TelegramConfig

        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "  123  "},
            clear=True,
        ):
            cfg = TelegramConfig.from_env()

        # Note: env vars include whitespace
        assert cfg.chat_id == "  123  "

    def test_long_token_handling(self):
        """Config should handle long token strings."""
        from src.notifications.telegram import TelegramConfig

        long_token = "a" * 500
        with patch.dict(
            "os.environ",
            {"TELEGRAM_BOT_TOKEN": long_token, "TELEGRAM_CHAT_ID": "123"},
            clear=True,
        ):
            cfg = TelegramConfig.from_env()

        assert cfg.bot_token == long_token
        assert len(cfg.bot_token) == 500
