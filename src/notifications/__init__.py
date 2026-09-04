"""Notifications module for Cordyceps bot.

Handles sending alerts to Telegram for errors, risk events, trades, and status updates.
"""

from src.notifications.telegram import (
    TelegramConfig,
    TelegramNotifier,
    get_notifier,
    init_notifications,
    shutdown_notifications,
)

__all__ = [
    "TelegramNotifier",
    "TelegramConfig",
    "get_notifier",
    "init_notifications",
    "shutdown_notifications",
]
