"""
Tests for the observer module (WebSocket and state management).

Run with: pytest tests/test_observer.py -v
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch


class TestStateManager:
    """Tests for StateManager."""

    def test_register_market(self):
        """Test market registration."""
        from src.observer.state_manager import StateManager

        manager = StateManager()
        manager.register_market(
            condition_id="cond_123",
            token_ids=["token_yes", "token_no"],
        )

        assert "cond_123" in manager.markets
        assert len(manager.order_books) == 2
        assert "token_yes" in manager.order_books
        assert "token_no" in manager.order_books

    def test_unregister_market(self):
        """Test market unregistration."""
        from src.observer.state_manager import StateManager

        manager = StateManager()
        manager.register_market("cond_123", ["token_yes", "token_no"])
        manager.unregister_market("cond_123")

        assert "cond_123" not in manager.markets
        assert len(manager.order_books) == 0

    def test_get_order_book(self):
        """Test getting order book for token."""
        from src.observer.state_manager import StateManager

        manager = StateManager()
        manager.register_market("cond_123", ["token_yes"])

        book = manager.get_order_book("token_yes")
        assert book is not None
        assert book.token_id == "token_yes"

    def test_get_order_book_untracked(self):
        """Test getting order book for untracked token returns None."""
        from src.observer.state_manager import StateManager

        manager = StateManager()
        book = manager.get_order_book("untracked_token")
        assert book is None

    def test_handle_book_update(self):
        """Test handling order book updates."""
        from src.observer.state_manager import StateManager

        callback_data = {}

        def on_update(token_id, book):
            callback_data["token_id"] = token_id
            callback_data["book"] = book

        manager = StateManager(on_book_update=on_update)
        manager.register_market("cond_123", ["token_yes"])

        manager.handle_book_update("token_yes", {
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [{"price": "0.57", "size": "150"}],
        })

        assert callback_data["token_id"] == "token_yes"
        assert len(callback_data["book"].bids) == 1
        assert len(callback_data["book"].asks) == 1
        assert callback_data["book"].best_bid.price == Decimal("0.55")
        assert callback_data["book"].best_ask.price == Decimal("0.57")

    def test_handle_book_update_untracked(self):
        """Test that updates for untracked tokens are ignored."""
        from src.observer.state_manager import StateManager

        callback_called = []

        def on_update(token_id, book):
            callback_called.append(token_id)

        manager = StateManager(on_book_update=on_update)
        manager.handle_book_update("untracked_token", {"bids": [], "asks": []})

        assert len(callback_called) == 0

    def test_market_is_complete(self):
        """Test market completeness detection."""
        from src.observer.state_manager import StateManager

        opportunity_data = {}

        def on_opportunity(condition_id, books):
            opportunity_data["condition_id"] = condition_id
            opportunity_data["books"] = books

        manager = StateManager(on_arb_opportunity=on_opportunity)
        manager.register_market("cond_123", ["token_yes", "token_no"])

        # First update - not complete yet
        manager.handle_book_update("token_yes", {
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.47", "size": "100"}],
        })
        assert "condition_id" not in opportunity_data

        # Second update - now complete
        manager.handle_book_update("token_no", {
            "bids": [{"price": "0.50", "size": "100"}],
            "asks": [{"price": "0.52", "size": "100"}],
        })
        assert opportunity_data["condition_id"] == "cond_123"
        assert len(opportunity_data["books"]) == 2

    def test_clear(self):
        """Test clearing all state."""
        from src.observer.state_manager import StateManager

        manager = StateManager()
        manager.register_market("cond_123", ["token_yes", "token_no"])
        manager.clear()

        assert len(manager.markets) == 0
        assert len(manager.order_books) == 0


class TestWebSocketConfig:
    """Tests for WebSocket configuration."""

    def test_default_config(self):
        """Test default WebSocket configuration values."""
        from src.observer.websocket import WebSocketConfig

        config = WebSocketConfig()

        assert "ws" in config.url.lower()
        assert config.ping_interval == 30.0
        assert config.max_reconnect_attempts == 10

    def test_custom_config(self):
        """Test custom WebSocket configuration."""
        from src.observer.websocket import WebSocketConfig

        config = WebSocketConfig(
            ping_interval=60.0,
            max_reconnect_attempts=5,
        )

        assert config.ping_interval == 60.0
        assert config.max_reconnect_attempts == 5


class TestConnectionState:
    """Tests for connection state enum."""

    def test_connection_states(self):
        """Test connection state values."""
        from src.observer.websocket import ConnectionState

        assert ConnectionState.DISCONNECTED.value == "disconnected"
        assert ConnectionState.CONNECTING.value == "connecting"
        assert ConnectionState.CONNECTED.value == "connected"
        assert ConnectionState.RECONNECTING.value == "reconnecting"


class TestMarketState:
    """Tests for MarketState dataclass."""

    def test_market_state_is_complete(self):
        """Test MarketState completeness check."""
        from src.observer.state_manager import MarketState
        from src.client.models import OrderBook

        state = MarketState(
            condition_id="cond_123",
            token_ids=["token_yes", "token_no"],
        )

        assert not state.is_complete

        state.order_books["token_yes"] = OrderBook(token_id="token_yes")
        assert not state.is_complete

        state.order_books["token_no"] = OrderBook(token_id="token_no")
        assert state.is_complete
