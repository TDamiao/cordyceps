"""
Tests for the execution module (rate limiting and order execution).

Run with: pytest tests/test_execution.py -v
"""

import asyncio
from decimal import Decimal
from unittest.mock import MagicMock

import pytest


class TestTokenBucket:
    """Tests for TokenBucket rate limiter."""

    @pytest.mark.asyncio
    async def test_bucket_initial_capacity(self):
        """Test bucket starts with full capacity."""
        from src.execution.rate_limiter import TokenBucket

        bucket = TokenBucket(rate=10.0, capacity=20)
        assert bucket.available >= 19  # May have small time drift

    @pytest.mark.asyncio
    async def test_bucket_acquire_immediate(self):
        """Test acquiring tokens when available."""
        from src.execution.rate_limiter import TokenBucket

        bucket = TokenBucket(rate=10.0, capacity=20)
        wait_time = await bucket.acquire(1)
        assert wait_time == 0  # Should be immediate

    @pytest.mark.asyncio
    async def test_bucket_refill(self):
        """Test bucket refills over time."""
        from src.execution.rate_limiter import TokenBucket

        bucket = TokenBucket(rate=100.0, capacity=10)  # Fast refill

        # Consume all tokens
        for _ in range(10):
            await bucket.acquire(1)

        # Should have refilled some
        await asyncio.sleep(0.05)
        assert bucket.available > 0


class TestRateLimiter:
    """Tests for RateLimiter."""

    def test_limiter_initialization(self):
        """Test rate limiter initialization."""
        from src.execution.rate_limiter import RateLimitConfig, RateLimiter

        config = RateLimitConfig(requests_per_second=5.0)
        limiter = RateLimiter(config=config)

        assert limiter.config.requests_per_second == 5.0

    @pytest.mark.asyncio
    async def test_acquire_request(self):
        """Test acquiring request permit."""
        from src.execution.rate_limiter import RateLimiter

        limiter = RateLimiter()
        wait_time = await limiter.acquire_request()
        assert wait_time >= 0

    @pytest.mark.asyncio
    async def test_acquire_order(self):
        """Test acquiring order permit."""
        from src.execution.rate_limiter import RateLimiter

        limiter = RateLimiter()
        wait_time = await limiter.acquire_order()
        assert wait_time >= 0

    def test_stats(self):
        """Test stats tracking."""
        from src.execution.rate_limiter import RateLimiter

        limiter = RateLimiter()
        stats = limiter.stats

        assert "total_wait_time" in stats
        assert "wait_count" in stats
        assert "request_tokens" in stats


class TestOrderResult:
    """Tests for OrderResult dataclass."""

    def test_order_result_creation(self):
        """Test OrderResult creation."""
        from src.execution.executor import OrderResult, OrderStatus

        result = OrderResult(
            token_id="token_123",
            order_id="order_456",
            status=OrderStatus.FILLED,
            filled_size=Decimal("100"),
            price=Decimal("0.55"),
        )

        assert result.token_id == "token_123"
        assert result.status == OrderStatus.FILLED
        assert result.filled_size == Decimal("100")


class TestExecutionResult:
    """Tests for ExecutionResult dataclass."""

    def test_execution_result_all_filled(self):
        """Test all_filled property."""
        from src.engine.detector import ArbitrageOpportunity, SignalType
        from src.execution.executor import ExecutionResult, OrderResult, OrderStatus

        opp = ArbitrageOpportunity(
            market_id="m1",
            signal_type=SignalType.BUY_SET,
            token_ids=["t1", "t2"],
            prices=[Decimal("0.45"), Decimal("0.45")],
            sizes=[Decimal("100"), Decimal("100")],
            max_size=Decimal("100"),
            total_cost=Decimal("90"),
            expected_payout=Decimal("100"),
            gross_profit=Decimal("10"),
            fees=Decimal("0.02"),
            net_profit=Decimal("9.98"),
            profit_pct=Decimal("0.11"),
        )

        result = ExecutionResult(
            opportunity=opp,
            orders=[
                OrderResult(token_id="t1", status=OrderStatus.FILLED, filled_size=Decimal("100")),
                OrderResult(token_id="t2", status=OrderStatus.FILLED, filled_size=Decimal("100")),
            ],
            success=True,
        )

        assert result.all_filled
        assert result.any_filled

    def test_execution_result_partial_fill(self):
        """Test partial fill detection."""
        from src.engine.detector import ArbitrageOpportunity, SignalType
        from src.execution.executor import ExecutionResult, OrderResult, OrderStatus

        opp = ArbitrageOpportunity(
            market_id="m1",
            signal_type=SignalType.BUY_SET,
            token_ids=["t1", "t2"],
            prices=[Decimal("0.45"), Decimal("0.45")],
            sizes=[Decimal("100"), Decimal("100")],
            max_size=Decimal("100"),
            total_cost=Decimal("90"),
            expected_payout=Decimal("100"),
            gross_profit=Decimal("10"),
            fees=Decimal("0.02"),
            net_profit=Decimal("9.98"),
            profit_pct=Decimal("0.11"),
        )

        result = ExecutionResult(
            opportunity=opp,
            orders=[
                OrderResult(token_id="t1", status=OrderStatus.FILLED, filled_size=Decimal("100")),
                OrderResult(token_id="t2", status=OrderStatus.FAILED),
            ],
        )

        assert not result.all_filled
        assert result.any_filled


class TestOrderExecutor:
    """Tests for OrderExecutor."""

    def test_executor_initialization(self):
        """Test executor initialization."""
        from src.execution.executor import OrderExecutor

        mock_client = MagicMock()
        executor = OrderExecutor(client=mock_client)

        assert executor.stats["orders_submitted"] == 0
        assert executor.stats["orders_filled"] == 0

    def test_stats_tracking(self):
        """Test stats are tracked correctly."""
        from src.execution.executor import OrderExecutor

        mock_client = MagicMock()
        executor = OrderExecutor(client=mock_client)

        stats = executor.stats
        assert "orders_submitted" in stats
        assert "orders_filled" in stats
        assert "fill_rate" in stats

        executor.reset_stats()
        assert executor.stats["orders_submitted"] == 0


class TestOrderStatus:
    """Tests for OrderStatus enum."""

    def test_order_status_values(self):
        """Test OrderStatus enum values."""
        from src.execution.executor import OrderStatus

        assert OrderStatus.PENDING.value == "pending"
        assert OrderStatus.FILLED.value == "filled"
        assert OrderStatus.FAILED.value == "failed"
        assert OrderStatus.CANCELLED.value == "cancelled"
