"""
Integration tests for authentication and client setup.

Run with: pytest tests/test_auth.py -v
"""

import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock


class TestAuthentication:
    """Tests for authentication module."""

    def test_derive_eoa_address_valid_key(self):
        """Test EOA address derivation with a valid private key."""
        from src.client.auth import derive_eoa_address

        # Use a known test private key (DO NOT use in production!)
        test_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        address = derive_eoa_address(test_key)

        assert address.startswith("0x")
        assert len(address) == 42

    def test_derive_eoa_address_invalid_key(self):
        """Test that invalid private key raises error."""
        from src.client.auth import derive_eoa_address, AuthenticationError

        with pytest.raises(AuthenticationError):
            derive_eoa_address("invalid_key")

    def test_derive_eoa_address_wrong_length(self):
        """Test that wrong length key raises error."""
        from src.client.auth import derive_eoa_address, AuthenticationError

        with pytest.raises(AuthenticationError):
            derive_eoa_address("0x1234")


class TestModels:
    """Tests for data models."""

    def test_order_book_level_creation(self):
        """Test OrderBookLevel creation."""
        from src.client.models import OrderBookLevel

        level = OrderBookLevel(price=Decimal("0.55"), size=Decimal("100"))

        assert level.price == Decimal("0.55")
        assert level.size == Decimal("100")

    def test_order_book_best_bid_ask(self):
        """Test OrderBook best bid/ask properties."""
        from src.client.models import OrderBook, OrderBookLevel

        book = OrderBook(
            token_id="test_token",
            bids=[
                OrderBookLevel(price=Decimal("0.50"), size=Decimal("100")),
                OrderBookLevel(price=Decimal("0.49"), size=Decimal("200")),
            ],
            asks=[
                OrderBookLevel(price=Decimal("0.52"), size=Decimal("150")),
                OrderBookLevel(price=Decimal("0.53"), size=Decimal("250")),
            ],
        )

        assert book.best_bid.price == Decimal("0.50")
        assert book.best_ask.price == Decimal("0.52")
        assert book.mid_price == Decimal("0.51")
        assert book.spread == Decimal("0.02")

    def test_order_book_empty(self):
        """Test OrderBook with no bids/asks."""
        from src.client.models import OrderBook

        book = OrderBook(token_id="test_token")

        assert book.best_bid is None
        assert book.best_ask is None
        assert book.mid_price is None
        assert book.spread is None

    def test_order_side_enum(self):
        """Test OrderSide enum values."""
        from src.client.models import OrderSide

        assert OrderSide.BUY.value == "BUY"
        assert OrderSide.SELL.value == "SELL"

    def test_order_type_enum(self):
        """Test OrderType enum values."""
        from src.client.models import OrderType

        assert OrderType.GTC.value == "GTC"
        assert OrderType.FOK.value == "FOK"
        assert OrderType.GTD.value == "GTD"

    def test_trading_signal_model(self):
        """Test TradingSignal Pydantic model."""
        from src.client.models import TradingSignal

        signal = TradingSignal(
            market_id="test_market",
            signal_type="BUY_SET",
            token_ids=["token1", "token2"],
            prices=["0.45", "0.50"],
            size="100",
            expected_profit="0.05",
            total_cost="0.95",
            timestamp=1234567890,
        )

        assert signal.market_id == "test_market"
        assert signal.signal_type == "BUY_SET"
        assert len(signal.token_ids) == 2


class TestConfig:
    """Tests for configuration module."""

    def test_endpoints_constants(self):
        """Test that endpoint constants are defined."""
        from src.config import Endpoints

        assert Endpoints.CLOB_HOST == "https://clob.polymarket.com"
        assert "ws" in Endpoints.CLOB_WS.lower()

    def test_contracts_constants(self):
        """Test that contract addresses are valid."""
        from src.config import Contracts

        assert Contracts.USDC.startswith("0x")
        assert len(Contracts.USDC) == 42
        assert Contracts.CTF.startswith("0x")

    def test_trading_config_constants(self):
        """Test trading configuration values."""
        from src.config import TradingConfig

        assert TradingConfig.TAKER_FEE == 0.0001
        assert TradingConfig.MAKER_FEE == 0.0
        assert TradingConfig.SIGNATURE_TYPE_POLY == 1
