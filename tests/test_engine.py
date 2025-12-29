"""
Tests for the arbitrage engine module.

Run with: pytest tests/test_engine.py -v
"""

import pytest
from decimal import Decimal


class TestArbitrageOpportunity:
    """Tests for ArbitrageOpportunity dataclass."""

    def test_opportunity_creation(self):
        """Test ArbitrageOpportunity creation."""
        from src.engine.detector import ArbitrageOpportunity, SignalType

        opp = ArbitrageOpportunity(
            market_id="market_123",
            signal_type=SignalType.BUY_SET,
            token_ids=["yes", "no"],
            prices=[Decimal("0.45"), Decimal("0.50")],
            sizes=[Decimal("100"), Decimal("150")],
            max_size=Decimal("100"),
            total_cost=Decimal("95"),
            expected_payout=Decimal("100"),
            gross_profit=Decimal("5"),
            fees=Decimal("0.02"),
            net_profit=Decimal("4.98"),
            profit_pct=Decimal("0.0524"),
        )

        assert opp.market_id == "market_123"
        assert opp.signal_type == SignalType.BUY_SET
        assert opp.is_profitable
        assert len(opp.token_ids) == 2

    def test_opportunity_not_profitable(self):
        """Test unprofitable opportunity detection."""
        from src.engine.detector import ArbitrageOpportunity, SignalType

        opp = ArbitrageOpportunity(
            market_id="market_123",
            signal_type=SignalType.BUY_SET,
            token_ids=["yes", "no"],
            prices=[Decimal("0.50"), Decimal("0.50")],
            sizes=[Decimal("100"), Decimal("100")],
            max_size=Decimal("100"),
            total_cost=Decimal("100"),
            expected_payout=Decimal("100"),
            gross_profit=Decimal("0"),
            fees=Decimal("0.02"),
            net_profit=Decimal("-0.02"),
            profit_pct=Decimal("-0.0002"),
        )

        assert not opp.is_profitable


class TestArbitrageConfig:
    """Tests for ArbitrageConfig."""

    def test_default_config(self):
        """Test default configuration values."""
        from src.engine.detector import ArbitrageConfig

        config = ArbitrageConfig()

        assert config.min_profit_threshold == Decimal("0.005")
        assert config.max_position_size == Decimal("1000")
        assert config.taker_fee > 0

    def test_custom_config(self):
        """Test custom configuration."""
        from src.engine.detector import ArbitrageConfig

        config = ArbitrageConfig(
            min_profit_threshold=Decimal("0.01"),
            max_position_size=Decimal("500"),
        )

        assert config.min_profit_threshold == Decimal("0.01")
        assert config.max_position_size == Decimal("500")


class TestArbitrageEngine:
    """Tests for ArbitrageEngine."""

    def _create_order_book(self, token_id: str, bid_price: str, ask_price: str, size: str = "100"):
        """Helper to create an order book."""
        from src.client.models import OrderBook, OrderBookLevel

        return OrderBook(
            token_id=token_id,
            bids=[OrderBookLevel(price=Decimal(bid_price), size=Decimal(size))],
            asks=[OrderBookLevel(price=Decimal(ask_price), size=Decimal(size))],
        )

    def test_engine_initialization(self):
        """Test engine initialization."""
        from src.engine.detector import ArbitrageEngine, ArbitrageConfig

        config = ArbitrageConfig(min_profit_threshold=Decimal("0.01"))
        engine = ArbitrageEngine(config=config)

        assert engine.config.min_profit_threshold == Decimal("0.01")
        assert engine.stats["opportunities_found"] == 0

    def test_detect_buy_set_opportunity(self):
        """Test BUY_SET opportunity detection (sum of asks < 1)."""
        from src.engine.detector import ArbitrageEngine, ArbitrageConfig, SignalType

        config = ArbitrageConfig(
            min_profit_threshold=Decimal("0.001"),  # Very low threshold for test
            taker_fee=Decimal("0"),  # No fees for simplicity
        )
        engine = ArbitrageEngine(config=config)

        # Create order books where asks sum to 0.90 (opportunity)
        order_books = {
            "yes_token": self._create_order_book("yes_token", "0.43", "0.45"),
            "no_token": self._create_order_book("no_token", "0.43", "0.45"),
        }

        opp = engine.analyze_market("market_123", order_books)

        assert opp is not None
        assert opp.signal_type == SignalType.BUY_SET
        assert opp.total_cost < opp.expected_payout
        assert opp.is_profitable

    def test_detect_sell_set_opportunity(self):
        """Test SELL_SET opportunity detection (sum of bids > 1)."""
        from src.engine.detector import ArbitrageEngine, ArbitrageConfig, SignalType

        config = ArbitrageConfig(
            min_profit_threshold=Decimal("0.001"),
            taker_fee=Decimal("0"),
        )
        engine = ArbitrageEngine(config=config)

        # Create order books where bids sum to 1.10 (opportunity)
        order_books = {
            "yes_token": self._create_order_book("yes_token", "0.55", "0.60"),
            "no_token": self._create_order_book("no_token", "0.55", "0.60"),
        }

        opp = engine.analyze_market("market_123", order_books)

        assert opp is not None
        assert opp.signal_type == SignalType.SELL_SET
        assert opp.expected_payout > opp.total_cost
        assert opp.is_profitable

    def test_no_opportunity(self):
        """Test no opportunity when prices are fair."""
        from src.engine.detector import ArbitrageEngine, ArbitrageConfig

        config = ArbitrageConfig(min_profit_threshold=Decimal("0.001"))
        engine = ArbitrageEngine(config=config)

        # Create order books with fair prices (sum = 1)
        order_books = {
            "yes_token": self._create_order_book("yes_token", "0.49", "0.51"),
            "no_token": self._create_order_book("no_token", "0.49", "0.51"),
        }

        opp = engine.analyze_market("market_123", order_books)

        assert opp is None

    def test_skip_non_binary_market(self):
        """Test that non-binary markets are skipped."""
        from src.engine.detector import ArbitrageEngine

        engine = ArbitrageEngine()

        # Create order books for 3 outcomes (not binary)
        order_books = {
            "token_a": self._create_order_book("token_a", "0.30", "0.32"),
            "token_b": self._create_order_book("token_b", "0.30", "0.32"),
            "token_c": self._create_order_book("token_c", "0.30", "0.32"),
        }

        opp = engine.analyze_market("market_123", order_books)

        assert opp is None

    def test_opportunity_respects_liquidity(self):
        """Test that opportunity size is limited by liquidity."""
        from src.engine.detector import ArbitrageEngine, ArbitrageConfig

        config = ArbitrageConfig(
            min_profit_threshold=Decimal("0.001"),
            taker_fee=Decimal("0"),
            max_position_size=Decimal("1000"),
        )
        engine = ArbitrageEngine(config=config)

        # One token has limited liquidity
        order_books = {
            "yes_token": self._create_order_book("yes_token", "0.43", "0.45", size="50"),
            "no_token": self._create_order_book("no_token", "0.43", "0.45", size="200"),
        }

        opp = engine.analyze_market("market_123", order_books)

        assert opp is not None
        assert opp.max_size == Decimal("50")  # Limited by smaller side

    def test_opportunity_respects_max_position(self):
        """Test that opportunity size is limited by max position size."""
        from src.engine.detector import ArbitrageEngine, ArbitrageConfig

        config = ArbitrageConfig(
            min_profit_threshold=Decimal("0.001"),
            taker_fee=Decimal("0"),
            max_position_size=Decimal("25"),  # Small max position
        )
        engine = ArbitrageEngine(config=config)

        order_books = {
            "yes_token": self._create_order_book("yes_token", "0.43", "0.45", size="100"),
            "no_token": self._create_order_book("no_token", "0.43", "0.45", size="100"),
        }

        opp = engine.analyze_market("market_123", order_books)

        assert opp is not None
        assert opp.max_size == Decimal("25")  # Limited by config

    def test_fees_reduce_profit(self):
        """Test that fees are correctly applied."""
        from src.engine.detector import ArbitrageEngine, ArbitrageConfig

        config = ArbitrageConfig(
            min_profit_threshold=Decimal("0.001"),
            taker_fee=Decimal("0.01"),  # 1% fee
        )
        engine = ArbitrageEngine(config=config)

        order_books = {
            "yes_token": self._create_order_book("yes_token", "0.43", "0.45"),
            "no_token": self._create_order_book("no_token", "0.43", "0.45"),
        }

        opp = engine.analyze_market("market_123", order_books)

        if opp:
            assert opp.fees > 0
            assert opp.net_profit < opp.gross_profit

    def test_stats_tracking(self):
        """Test stats are tracked correctly."""
        from src.engine.detector import ArbitrageEngine, ArbitrageConfig

        config = ArbitrageConfig(
            min_profit_threshold=Decimal("0.001"),
            taker_fee=Decimal("0"),
        )
        engine = ArbitrageEngine(config=config)

        order_books = {
            "yes_token": self._create_order_book("yes_token", "0.43", "0.45"),
            "no_token": self._create_order_book("no_token", "0.43", "0.45"),
        }

        engine.analyze_market("market_1", order_books)
        engine.analyze_market("market_2", order_books)

        assert engine.stats["opportunities_found"] == 2

        engine.reset_stats()
        assert engine.stats["opportunities_found"] == 0


class TestHelperFunctions:
    """Tests for helper functions."""

    def _create_order_book(self, token_id: str, bid_price: str, ask_price: str):
        """Helper to create an order book."""
        from src.client.models import OrderBook, OrderBookLevel

        return OrderBook(
            token_id=token_id,
            bids=[OrderBookLevel(price=Decimal(bid_price), size=Decimal("100"))],
            asks=[OrderBookLevel(price=Decimal(ask_price), size=Decimal("100"))],
        )

    def test_calculate_price_sum(self):
        """Test price sum calculation."""
        from src.engine.detector import calculate_price_sum

        order_books = {
            "yes": self._create_order_book("yes", "0.45", "0.47"),
            "no": self._create_order_book("no", "0.52", "0.54"),
        }

        ask_sum = calculate_price_sum(order_books, "ask")
        bid_sum = calculate_price_sum(order_books, "bid")

        assert ask_sum == Decimal("1.01")  # 0.47 + 0.54
        assert bid_sum == Decimal("0.97")  # 0.45 + 0.52

    def test_is_buy_opportunity(self):
        """Test buy opportunity detection."""
        from src.engine.detector import is_buy_opportunity

        # Opportunity: asks sum to 0.90
        order_books = {
            "yes": self._create_order_book("yes", "0.43", "0.45"),
            "no": self._create_order_book("no", "0.43", "0.45"),
        }

        assert is_buy_opportunity(order_books)

        # No opportunity: asks sum to 1.02
        order_books_no = {
            "yes": self._create_order_book("yes", "0.49", "0.51"),
            "no": self._create_order_book("no", "0.49", "0.51"),
        }

        assert not is_buy_opportunity(order_books_no)

    def test_is_sell_opportunity(self):
        """Test sell opportunity detection."""
        from src.engine.detector import is_sell_opportunity

        # Opportunity: bids sum to 1.10
        order_books = {
            "yes": self._create_order_book("yes", "0.55", "0.57"),
            "no": self._create_order_book("no", "0.55", "0.57"),
        }

        assert is_sell_opportunity(order_books)

        # No opportunity: bids sum to 0.98
        order_books_no = {
            "yes": self._create_order_book("yes", "0.49", "0.51"),
            "no": self._create_order_book("no", "0.49", "0.51"),
        }

        assert not is_sell_opportunity(order_books_no)
