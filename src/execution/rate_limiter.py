"""
Rate limiter for Polymarket CLOB API.

Implements token bucket algorithm to respect API rate limits.
"""

import asyncio
import time
from dataclasses import dataclass

from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    # CLOB API limits (from documentation)
    requests_per_second: float = 10.0  # General API limit
    orders_per_second: float = 5.0     # Order submission limit
    burst_size: int = 20               # Max burst capacity


class TokenBucket:
    """
    Token bucket rate limiter.

    Allows bursting up to capacity, then refills at a steady rate.
    """

    def __init__(
        self,
        rate: float,
        capacity: int,
    ):
        """
        Initialize token bucket.

        Args:
            rate: Tokens added per second
            capacity: Maximum bucket size
        """
        self.rate = rate
        self.capacity = capacity
        self._tokens = float(capacity)
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> float:
        """
        Acquire tokens, waiting if necessary.

        Args:
            tokens: Number of tokens to acquire

        Returns:
            Time waited in seconds
        """
        async with self._lock:
            wait_time = 0.0

            # Refill bucket based on elapsed time
            now = time.monotonic()
            elapsed = now - self._last_update
            self._tokens = min(
                self.capacity,
                self._tokens + elapsed * self.rate
            )
            self._last_update = now

            # Check if we need to wait
            if self._tokens < tokens:
                # Calculate wait time
                deficit = tokens - self._tokens
                wait_time = deficit / self.rate

                await asyncio.sleep(wait_time)

                # Refill after waiting
                self._tokens = min(
                    self.capacity,
                    self._tokens + wait_time * self.rate
                )

            # Consume tokens
            self._tokens -= tokens

            return wait_time

    @property
    def available(self) -> float:
        """Get current available tokens."""
        now = time.monotonic()
        elapsed = now - self._last_update
        return min(
            self.capacity,
            self._tokens + elapsed * self.rate
        )


class RateLimiter:
    """
    Rate limiter for CLOB API requests.

    Provides separate buckets for general requests and order submissions.
    """

    def __init__(self, config: RateLimitConfig | None = None):
        """
        Initialize rate limiter.

        Args:
            config: Rate limit configuration
        """
        self.config = config or RateLimitConfig()

        self._request_bucket = TokenBucket(
            rate=self.config.requests_per_second,
            capacity=self.config.burst_size,
        )

        self._order_bucket = TokenBucket(
            rate=self.config.orders_per_second,
            capacity=self.config.burst_size // 2,
        )

        self._total_waits = 0.0
        self._wait_count = 0

    async def acquire_request(self) -> float:
        """
        Acquire permission for a general API request.

        Returns:
            Time waited in seconds
        """
        wait_time = await self._request_bucket.acquire()
        if wait_time > 0:
            self._total_waits += wait_time
            self._wait_count += 1
            logger.debug("Rate limit wait (request)", wait_time=wait_time)
        return wait_time

    async def acquire_order(self) -> float:
        """
        Acquire permission for an order submission.

        This consumes from both request and order buckets.

        Returns:
            Time waited in seconds
        """
        # Must satisfy both limits
        request_wait = await self._request_bucket.acquire()
        order_wait = await self._order_bucket.acquire()

        total_wait = request_wait + order_wait
        if total_wait > 0:
            self._total_waits += total_wait
            self._wait_count += 1
            logger.debug("Rate limit wait (order)", wait_time=total_wait)

        return total_wait

    @property
    def stats(self) -> dict:
        """Get rate limiter statistics."""
        return {
            "total_wait_time": self._total_waits,
            "wait_count": self._wait_count,
            "avg_wait_time": self._total_waits / self._wait_count if self._wait_count > 0 else 0,
            "request_tokens": self._request_bucket.available,
            "order_tokens": self._order_bucket.available,
        }

    def reset_stats(self) -> None:
        """Reset statistics."""
        self._total_waits = 0.0
        self._wait_count = 0
