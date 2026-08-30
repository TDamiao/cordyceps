"""
Tests for the markets module (fetching and caching).

Run with: pytest tests/test_markets.py -v
"""

from decimal import Decimal


class TestToken:
    """Tests for Token dataclass."""

    def test_token_creation(self):
        """Test Token creation."""
        from src.markets.fetcher import Token

        token = Token(
            token_id="token_123",
            outcome="Yes",
            price=Decimal("0.55"),
        )

        assert token.token_id == "token_123"
        assert token.outcome == "Yes"
        assert token.price == Decimal("0.55")
        assert token.winner is None


class TestMarketInfo:
    """Tests for MarketInfo dataclass."""

    def test_market_info_creation(self):
        """Test MarketInfo creation."""
        from src.markets.fetcher import MarketInfo, Token

        tokens = [
            Token(token_id="yes_token", outcome="Yes"),
            Token(token_id="no_token", outcome="No"),
        ]

        market = MarketInfo(
            condition_id="cond_123",
            question_id="q_123",
            question="Will it rain tomorrow?",
            slug="rain-tomorrow",
            tokens=tokens,
            active=True,
            volume=Decimal("10000"),
        )

        assert market.condition_id == "cond_123"
        assert len(market.tokens) == 2
        assert market.is_binary
        assert market.is_tradeable

    def test_market_token_ids(self):
        """Test token_ids property."""
        from src.markets.fetcher import MarketInfo, Token

        tokens = [
            Token(token_id="token_a", outcome="Yes"),
            Token(token_id="token_b", outcome="No"),
        ]

        market = MarketInfo(
            condition_id="cond_123",
            question_id="q_123",
            question="Test?",
            slug="test",
            tokens=tokens,
        )

        assert market.token_ids == ["token_a", "token_b"]

    def test_market_is_binary(self):
        """Test is_binary property."""
        from src.markets.fetcher import MarketInfo, Token

        # Binary market
        binary_market = MarketInfo(
            condition_id="cond_1",
            question_id="q_1",
            question="Binary?",
            slug="binary",
            tokens=[Token("t1", "Yes"), Token("t2", "No")],
        )
        assert binary_market.is_binary

        # Multi-outcome market (not binary)
        multi_market = MarketInfo(
            condition_id="cond_2",
            question_id="q_2",
            question="Multi?",
            slug="multi",
            tokens=[Token("t1", "A"), Token("t2", "B"), Token("t3", "C")],
        )
        assert not multi_market.is_binary

    def test_market_is_tradeable(self):
        """Test is_tradeable property."""
        from src.markets.fetcher import MarketInfo, Token

        tokens = [Token("t1", "Yes")]

        # Active and not closed
        active_market = MarketInfo(
            condition_id="c1",
            question_id="q1",
            question="?",
            slug="s",
            tokens=tokens,
            active=True,
            closed=False,
        )
        assert active_market.is_tradeable

        # Not active
        inactive_market = MarketInfo(
            condition_id="c2",
            question_id="q2",
            question="?",
            slug="s",
            tokens=tokens,
            active=False,
            closed=False,
        )
        assert not inactive_market.is_tradeable

        # Closed
        closed_market = MarketInfo(
            condition_id="c3",
            question_id="q3",
            question="?",
            slug="s",
            tokens=tokens,
            active=True,
            closed=True,
        )
        assert not closed_market.is_tradeable


class TestMarketCache:
    """Tests for MarketCache."""

    def test_cache_update(self):
        """Test cache update."""
        from src.markets.fetcher import MarketCache, MarketInfo, Token

        cache = MarketCache()

        markets = [
            MarketInfo(
                condition_id="cond_1",
                question_id="q_1",
                question="Q1?",
                slug="q1",
                tokens=[Token("t1", "Yes"), Token("t2", "No")],
            ),
            MarketInfo(
                condition_id="cond_2",
                question_id="q_2",
                question="Q2?",
                slug="q2",
                tokens=[Token("t3", "Yes"), Token("t4", "No")],
            ),
        ]

        cache.update(markets)

        assert len(cache.markets) == 2
        assert len(cache.token_to_market) == 4
        assert cache.last_update > 0

    def test_cache_get_market(self):
        """Test getting market from cache."""
        from src.markets.fetcher import MarketCache, MarketInfo, Token

        cache = MarketCache()
        market = MarketInfo(
            condition_id="cond_123",
            question_id="q_123",
            question="Test?",
            slug="test",
            tokens=[Token("t1", "Yes")],
        )
        cache.update([market])

        result = cache.get_market("cond_123")
        assert result is not None
        assert result.question == "Test?"

        assert cache.get_market("nonexistent") is None

    def test_cache_get_market_by_token(self):
        """Test getting market by token ID."""
        from src.markets.fetcher import MarketCache, MarketInfo, Token

        cache = MarketCache()
        market = MarketInfo(
            condition_id="cond_123",
            question_id="q_123",
            question="Test?",
            slug="test",
            tokens=[Token("token_yes", "Yes"), Token("token_no", "No")],
        )
        cache.update([market])

        result = cache.get_market_by_token("token_yes")
        assert result is not None
        assert result.condition_id == "cond_123"

        assert cache.get_market_by_token("unknown_token") is None

    def test_cache_is_stale(self):
        """Test cache staleness check."""
        import time

        from src.markets.fetcher import MarketCache

        cache = MarketCache(ttl_seconds=0.1)  # 100ms TTL
        cache.last_update = time.time()

        assert not cache.is_stale

        time.sleep(0.15)  # Wait for TTL to expire
        assert cache.is_stale


class TestMarketFetcher:
    """Tests for MarketFetcher."""

    def test_fetcher_initialization(self):
        """Test MarketFetcher initialization."""
        from src.markets.fetcher import MarketFetcher

        fetcher = MarketFetcher()

        assert fetcher.gamma_host is not None
        assert fetcher.cache is not None

    def test_parse_market(self):
        """Test market parsing from API response."""
        from src.markets.fetcher import MarketFetcher

        fetcher = MarketFetcher()

        # Use Gamma API format (camelCase, clobTokenIds as JSON string)
        data = {
            "conditionId": "cond_123",
            "questionID": "q_123",
            "question": "Will it rain?",
            "slug": "rain",
            "clobTokenIds": '["yes_id", "no_id"]',  # JSON string
            "outcomes": ["Yes", "No"],
            "outcomePrices": "0.55, 0.45",
            "active": True,
            "closed": False,
            "volumeNum": 10000,
            "liquidityNum": 5000,
            "negRisk": False,
            "feesEnabled": False,
        }

        market = fetcher._parse_market(data)

        assert market is not None
        assert market.condition_id == "cond_123"
        assert market.question == "Will it rain?"
        assert len(market.tokens) == 2
        assert market.tokens[0].price == Decimal("0.55")
        assert market.volume == Decimal("10000")
        assert market.fees_enabled is False

    def test_parse_market_no_tokens(self):
        """Test parsing market with no tokens returns None."""
        from src.markets.fetcher import MarketFetcher

        fetcher = MarketFetcher()

        data = {
            "conditionId": "cond_123",
            "questionID": "q_123",
            "question": "Empty market?",
            "slug": "empty",
            "clobTokenIds": "[]",  # Empty JSON array
            "outcomes": [],
        }

        market = fetcher._parse_market(data)
        assert market is None

    def test_get_cached_helpers(self):
        """Test cache helper methods."""
        from src.markets.fetcher import MarketFetcher, MarketInfo, Token

        fetcher = MarketFetcher()

        # Create and cache some markets
        markets = [
            MarketInfo(
                condition_id="c1",
                question_id="q1",
                question="?",
                slug="s",
                tokens=[Token("t1", "Yes"), Token("t2", "No")],
                active=True,
            ),
            MarketInfo(
                condition_id="c2",
                question_id="q2",
                question="?",
                slug="s",
                tokens=[Token("t3", "A"), Token("t4", "B"), Token("t5", "C")],
                active=True,
            ),
        ]
        fetcher.cache.update(markets)

        assert len(fetcher.get_all_cached_markets()) == 2
        assert len(fetcher.get_tradeable_markets()) == 2
        assert len(fetcher.get_binary_markets()) == 1  # Only c1
