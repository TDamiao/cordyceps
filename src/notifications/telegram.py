"""
Telegram notifications for Cordyceps bot.

Sends all bot events to Telegram: trades, profits, losses, errors, status updates.
Uses TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import aiohttp
from structlog import get_logger

from src.config import get_settings

logger = get_logger(__name__)


@dataclass
class TelegramConfig:
    """Telegram bot configuration."""
    bot_token: str
    chat_id: str
    enabled: bool = True

    @classmethod
    def from_env(cls) -> TelegramConfig:
        """Create config from environment variables."""
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        # Also check settings
        settings = get_settings()
        if not bot_token:
            bot_token = getattr(settings, "telegram_bot_token", "")
        if not chat_id:
            chat_id = getattr(settings, "telegram_chat_id", "")

        return cls(
            bot_token=bot_token,
            chat_id=chat_id,
            enabled=bool(bot_token and chat_id)
        )


class TelegramNotifier:
    """Send notifications to Telegram."""

    BASE_URL = "https://api.telegram.org/bot"

    def __init__(self, config: TelegramConfig | None = None):
        self.config = config or TelegramConfig.from_env()
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message to Telegram."""
        if not self.config.enabled:
            logger.debug("Telegram not enabled, skipping message")
            return False

        url = f"{self.BASE_URL}{self.config.bot_token}/sendMessage"
        payload = {
            "chat_id": self.config.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            session = await self._get_session()
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                else:
                    error_text = await resp.text()
                    logger.error("telegram_send_failed", status=resp.status, error=error_text)
                    return False
        except Exception as e:
            logger.error("telegram_send_exception", error=str(e))
            return False

    # --- Event-specific notification methods ---

    async def notify_startup(self) -> bool:
        """Notify bot startup."""
        settings = get_settings()
        text = (
            f"🚀 <b>Cordyceps Bot Iniciado</b>\n\n"
            f"📊 <b>Modo:</b> {settings.trading_mode.upper()}\n"
            f"💰 <b>Capital:</b> ${settings.max_total_exposure_usd:.2f}\n"
            f"📈 <b>Trade Max:</b> ${settings.max_trade_usd:.2f}\n"
            f"🎯 <b>Estratégia Favorite:</b> {'Ativa' if getattr(settings, 'enable_favorite_strategy', False) else 'Inativa'}\n"
            f"⏰ <b>Horário:</b> {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
        )
        return await self.send_message(text)

    async def notify_shutdown(self, reason: str = "Manual") -> bool:
        """Notify bot shutdown."""
        text = (
            f"🛑 <b>Cordyceps Bot Parado</b>\n\n"
            f"📝 <b>Motivo:</b> {reason}\n"
            f"⏰ <b>Horário:</b> {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
        )
        return await self.send_message(text)

    async def notify_trade_open(
        self,
        market_id: str,
        market_question: str,
        side: str,  # "BUY" or "SELL"
        price: Decimal,
        size: Decimal,
        usd_value: Decimal,
        strategy: str = "arbitrage",
        is_favorite: bool = False,
    ) -> bool:
        """Notify trade opened."""
        emoji = "🎯" if is_favorite else "⚡"
        strat_label = "Favorite Compounding" if is_favorite else "Unity Arbitrage"

        text = (
            f"{emoji} <b>Nova Posição Aberta</b> [{strat_label}]\n\n"
            f"📊 <b>Mercado:</b> {market_question[:80]}...\n"
            f"🆔 <b>ID:</b> <code>{market_id[:16]}...</code>\n"
            f"📈 <b>Lado:</b> {side}\n"
            f"💲 <b>Preço:</b> {price:.4f} ({float(price)*100:.1f}¢)\n"
            f"📦 <b>Tamanho:</b> {size:.2f} shares\n"
            f"💰 <b>Valor:</b> ${usd_value:.2f}\n"
            f"⏰ <b>Horário:</b> {datetime.now().strftime('%H:%M:%S')}"
        )
        return await self.send_message(text)

    async def notify_trade_close(
        self,
        market_id: str,
        market_question: str,
        side: str,
        entry_price: Decimal,
        exit_price: Decimal,
        size: Decimal,
        pnl: Decimal,
        pnl_pct: float,
        is_favorite: bool = False,
        hold_duration_min: int | None = None,
    ) -> bool:
        """Notify trade closed with P&L."""
        is_profit = pnl > 0
        emoji = "✅" if is_profit else "❌"
        pnl_emoji = "🟢" if is_profit else "🔴"
        strat_label = "Favorite" if is_favorite else "Arb"

        duration_text = ""
        if hold_duration_min:
            duration_text = f"\n⏱ <b>Duração:</b> {hold_duration_min} min"

        text = (
            f"{emoji} <b>Posição Fechada</b> [{strat_label}] {pnl_emoji}\n\n"
            f"📊 <b>Mercado:</b> {market_question[:80]}...\n"
            f"🆔 <b>ID:</b> <code>{market_id[:16]}...</code>\n"
            f"📈 <b>Entrada:</b> {entry_price:.4f} ({float(entry_price)*100:.1f}¢)\n"
            f"📉 <b>Saída:</b> {exit_price:.4f} ({float(exit_price)*100:.1f}¢)\n"
            f"📦 <b>Tamanho:</b> {size:.2f} shares\n"
            f"{pnl_emoji} <b>P&L:</b> ${pnl:.2f} ({pnl_pct:+.2f}%)\n"
            f"{duration_text}\n"
            f"⏰ <b>Horário:</b> {datetime.now().strftime('%H:%M:%S')}"
        )
        return await self.send_message(text)

    async def notify_favorite_position_update(
        self,
        market_id: str,
        market_question: str,
        current_price: Decimal,
        entry_price: Decimal,
        unrealized_pnl_pct: float,
        action: str = "HOLD",  # HOLD, TAKE_PROFIT, STOP_LOSS
    ) -> bool:
        """Notify favorite position status update."""
        emoji_map = {"HOLD": "📊", "TAKE_PROFIT": "🎯", "STOP_LOSS": "🛑"}
        emoji = emoji_map.get(action, "📊")

        text = (
            f"{emoji} <b>Favorite Position Update</b> [{action}]\n\n"
            f"📊 <b>Mercado:</b> {market_question[:80]}...\n"
            f"🆔 <b>ID:</b> <code>{market_id[:16]}...</code>\n"
            f"📈 <b>Entrada:</b> {entry_price:.4f} ({float(entry_price)*100:.1f}¢)\n"
            f"📊 <b>Atual:</b> {current_price:.4f} ({float(current_price)*100:.1f}¢)\n"
            f"📈 <b>P&L Não Realizado:</b> {unrealized_pnl_pct:+.2f}%\n"
            f"⏰ <b>Horário:</b> {datetime.now().strftime('%H:%M:%S')}"
        )
        return await self.send_message(text)

    async def notify_daily_summary(
        self,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        total_pnl: Decimal,
        total_pnl_pct: float,
        favorite_trades: int = 0,
        favorite_pnl: Decimal = Decimal("0"),
        arb_trades: int = 0,
        arb_pnl: Decimal = Decimal("0"),
    ) -> bool:
        """Notify daily summary."""
        is_profit = total_pnl > 0
        emoji = "📈" if is_profit else "📉"

        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

        text = (
            f"{emoji} <b>Resumo Diário - Cordyceps</b>\n\n"
            f"📊 <b>Total Trades:</b> {total_trades}\n"
            f"✅ <b>Vitórias:</b> {winning_trades}\n"
            f"❌ <b>Derrotas:</b> {losing_trades}\n"
            f"📈 <b>Win Rate:</b> {win_rate:.1f}%\n\n"
            f"💰 <b>P&L Total:</b> ${total_pnl:.2f} ({total_pnl_pct:+.2f}%)\n\n"
            f"🎯 <b>Favorite:</b> {favorite_trades} trades, ${favorite_pnl:.2f}\n"
            f"⚡ <b>Arbitrage:</b> {arb_trades} trades, ${arb_pnl:.2f}\n\n"
            f"📅 <b>Data:</b> {datetime.now().strftime('%d/%m/%Y')}"
        )
        return await self.send_message(text)

    async def notify_error(
        self,
        error_type: str,
        error_message: str,
        context: dict[str, Any] | None = None,
        severity: str = "ERROR",  # ERROR, WARNING, CRITICAL
    ) -> bool:
        """Notify error or warning."""
        severity_emoji = {"ERROR": "🔴", "WARNING": "🟡", "CRITICAL": "🚨"}.get(severity, "🔴")

        context_text = ""
        if context:
            context_text = "\n📋 <b>Contexto:</b>\n" + "\n".join(
                f"  • <b>{k}:</b> <code>{v}</code>" for k, v in context.items()
            )

        text = (
            f"{severity_emoji} <b>Erro {severity}</b> [{error_type}]\n\n"
            f"📝 <b>Mensagem:</b> {error_message}\n"
            f"{context_text}\n"
            f"⏰ <b>Horário:</b> {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
        )
        return await self.send_message(text)

    async def notify_risk_event(
        self,
        event_type: str,  # KILL_SWITCH, CIRCUIT_BREAKER, DAILY_LOSS_LIMIT, EXPOSURE_LIMIT
        message: str,
        current_value: str | None = None,
        limit: str | None = None,
    ) -> bool:
        """Notify risk management event."""
        text = (
            f"🛡 <b>Evento de Risco: {event_type}</b>\n\n"
            f"📝 <b>Mensagem:</b> {message}\n"
        )
        if current_value:
            text += f"📊 <b>Valor Atual:</b> {current_value}\n"
        if limit:
            text += f"🚧 <b>Limite:</b> {limit}\n"
        text += f"⏰ <b>Horário:</b> {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
        return await self.send_message(text)

    async def notify_market_scan(
        self,
        markets_scanned: int,
        opportunities_found: int,
        opportunities_executed: int,
        favorite_candidates: int = 0,
    ) -> bool:
        """Notify periodic market scan summary."""
        if opportunities_found == 0 and favorite_candidates == 0:
            # Don't spam on empty scans
            return True

        text = (
            f"🔍 <b>Scan de Mercado Concluído</b>\n\n"
            f"📊 <b>Mercados Verificados:</b> {markets_scanned}\n"
            f"🎯 <b>Oportunidades Encontradas:</b> {opportunities_found}\n"
            f"⚡ <b>Executadas:</b> {opportunities_executed}\n"
            f"💎 <b>Favoritos Candidatos:</b> {favorite_candidates}\n"
            f"⏰ <b>Horário:</b> {datetime.now().strftime('%H:%M:%S')}"
        )
        return await self.send_message(text)

    async def notify_arbitrage_opportunity(
        self,
        market_id: str,
        market_question: str,
        signal_type: str,
        edge: float,
        yes_price: Decimal,
        no_price: Decimal,
        yes_bid: Decimal,
        no_bid: Decimal,
    ) -> bool:
        """Notify arbitrage opportunity detected."""
        text = (
            f"⚡ <b>Oportunidade de Arbitragem</b> [{signal_type}]\n\n"
            f"📊 <b>Mercado:</b> {market_question[:80]}...\n"
            f"🆔 <b>ID:</b> <code>{market_id[:16]}...</code>\n"
            f"📈 <b>Edge:</b> {edge*100:.3f}%\n"
            f"💲 <b>YES:</b> Ask {yes_price:.4f} / Bid {yes_bid:.4f}\n"
            f"💲 <b>NO:</b> Ask {no_price:.4f} / Bid {no_bid:.4f}\n"
            f"⏰ <b>Horário:</b> {datetime.now().strftime('%H:%M:%S')}"
        )
        return await self.send_message(text)


# Global notifier instance
_notifier: TelegramNotifier | None = None


def get_notifier() -> TelegramNotifier:
    """Get or create the global Telegram notifier."""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier


async def init_notifications() -> TelegramNotifier:
    """Initialize notifications and send startup message."""
    notifier = get_notifier()
    if notifier.config.enabled:
        await notifier.notify_startup()
    return notifier


async def shutdown_notifications():
    """Shutdown notifications gracefully."""
    global _notifier
    if _notifier:
        await _notifier.close()
        _notifier = None
