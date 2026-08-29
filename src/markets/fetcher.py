"""
Market data fetching and caching module.

Fetches market metadata from Gamma API and CLOB, builds mappings,
and caches results for efficient access.
"""

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import aiohttp

from src.config import Endpoints
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Token:
    """Token (outcome) in a market."""

    token_id: str
    outcome: str  # "Yes", "No", or custom outcome name
    price: Decimal | None = None
    winner: bool | None = None


@dataclass
class MarketInfo:
    """Complete market information."""

    condition_id: str
    question_id: str
    question: str
    slug: str
    tokens: list[Token]
    end_date: str | None = None
    active: bool = True
    closed: bool = False
    volume: Decimal = Decimal("0")
    liquidity: Decimal = Decimal("0")
    neg_risk: bool = False

    @property
    def token_ids(self) -> list[str]:
        """Get list of token IDs."""
        return [t.token_id for t in self.tokens]

    @property
    def is_binary(self) -> bool:
        """Check if this is a binary (Yes/No) market."""
        return len(self.tokens) == 2

    @property
    def is_tradeable(self) -> bool:
        """Check if market is tradeable."""
        return self.active and not self.closed


@dataclass
class MarketCache:
    """Cache for market data with TTL."""

    markets: dict[str, MarketInfo] = field(default_factory=dict)
    token_to_market: dict[str, str] = field(default_factory=dict)
    last_update: float = 0.0
    ttl_seconds: float = 300.0  # 5 minutes default TTL

    @property
    def is_stale(self) -> bool:
        """Check if cache is stale."""
        return time.time() - self.last_update > self.ttl_seconds

    def get_market(self, condition_id: str) -> MarketInfo | None:
        """Get market by condition ID."""
        return self.markets.get(condition_id)

    def get_market_by_token(self, token_id: str) -> MarketInfo | None:
        """Get market by token ID."""
        condition_id = self.token_to_market.get(token_id)
        if condition_id:
            return self.markets.get(condition_id)
        return None

    def update(self, markets: list[MarketInfo]) -> None:
        """Update cache with new market data."""
        self.markets.clear()
        self.token_to_market.clear()

        for market in markets:
            self.markets[market.condition_id] = market
            for token in market.tokens:
                self.token_to_market[token.token_id] = market.condition_id

        self.last_update = time.time()
        logger.info("Market cache updated", count=len(markets))


class MarketFetcher:
    """
    Fetches and caches market data from Polymarket APIs.

    Uses both Gamma API (for market metadata) and CLOB API (for trading data).
    """

    def __init__(
        self,
        gamma_host: str = Endpoints.GAMMA_API,
        cache_ttl: float = 300.0,
    ):
        """
        Initialize market fetcher.

        Args:
            gamma_host: Gamma API host URL
            cache_ttl: Cache time-to-live in seconds
        """
        self.gamma_host = gamma_host
        self._cache = MarketCache(ttl_seconds=cache_ttl)
        self._session: aiohttp.ClientSession | None = None

    @property
    def cache(self) -> MarketCache:
        """Access the market cache."""
        return self._cache

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_markets(
        self,
        active_only: bool = True,
        binary_only: bool = True,
        min_volume: float = 0,
        min_liquidity: float = 0,
        limit: int = 100,
    ) -> list[MarketInfo]:
        """
        Fetch markets from Gamma API.

        Args:
            active_only: Only return active markets
            binary_only: Only return binary (Yes/No) markets
            min_volume: Minimum volume filter
            min_liquidity: Minimum liquidity filter
            limit: Maximum number of markets to fetch

        Returns:
            List of MarketInfo objects
        """
        session = await self._get_session()

        params = {
            "limit": limit,
            "active": "true" if active_only else "false",
            "closed": "false",
        }

        try:
            url = f"{self.gamma_host}/markets"
            logger.debug("Fetching markets", url=url, params=params)

            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()

            markets = []
            for item in data:
                market = self._parse_market(item)

                if market is None:
                    continue

                # Apply filters
                if binary_only and not market.is_binary:
                    continue

                if min_volume > 0 and market.volume < Decimal(str(min_volume)):
                    continue

                if min_liquidity > 0 and market.liquidity < Decimal(str(min_liquidity)):
                    continue

                markets.append(market)

            # Update cache
            self._cache.update(markets)

            logger.info(
                "Markets fetched",
                total=len(data),
                filtered=len(markets),
            )

            return markets

        except aiohttp.ClientError as e:
            logger.error("Failed to fetch markets", error=str(e))
            return []

    async def fetch_market_by_id(self, condition_id: str) -> MarketInfo | None:
        """
        Fetch a single market by condition ID.

        Args:
            condition_id: Market condition ID

        Returns:
            MarketInfo or None if not found
        """
        # Check cache first
        cached = self._cache.get_market(condition_id)
        if cached and not self._cache.is_stale:
            return cached

        session = await self._get_session()

        try:
            url = f"{self.gamma_host}/markets/{condition_id}"
            logger.debug("Fetching market", condition_id=condition_id)

            async with session.get(url) as response:
                if response.status == 404:
                    return None
                response.raise_for_status()
                data = await response.json()

            return self._parse_market(data)

        except aiohttp.ClientError as e:
            logger.error("Failed to fetch market", condition_id=condition_id, error=str(e))
            return None

    async def fetch_markets_by_slug(self, slug: str) -> list[MarketInfo]:
        """
        Fetch markets by event slug.

        Args:
            slug: Event slug (e.g., "presidential-election-2024")

        Returns:
            List of MarketInfo objects for this event
        """
        session = await self._get_session()

        try:
            url = f"{self.gamma_host}/events"
            params = {"slug": slug}

            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()

            markets = []
            for event in data:
                for market_data in event.get("markets", []):
                    market = self._parse_market(market_data)
                    if market:
                        markets.append(market)

            return markets

        except aiohttp.ClientError as e:
            logger.error("Failed to fetch markets by slug", slug=slug, error=str(e))
            return []

    def _parse_market(self, data: dict[str, Any]) -> MarketInfo | None:
        """
        Parse market data from API response.

        Args:
            data: Raw API response data

        Returns:
            MarketInfo or None if parsing fails
        """
        try:
            # Gamma API uses camelCase field names
            condition_id = data.get("conditionId", "")
            if not condition_id:
                return None

            # Extract tokens from clobTokenIds and outcomes
            tokens = []
            clob_token_ids_raw = data.get("clobTokenIds", "")
            outcomes = data.get("outcomes", [])
            outcome_prices = data.get("outcomePrices", "")

            # Parse clobTokenIds - it's a JSON string like '["token1", "token2"]'
            import json

            if isinstance(clob_token_ids_raw, str) and clob_token_ids_raw:
                try:
                    clob_token_ids = json.loads(clob_token_ids_raw)
                except json.JSONDecodeError:
                    clob_token_ids = []
            elif isinstance(clob_token_ids_raw, list):
                clob_token_ids = clob_token_ids_raw
            else:
                clob_token_ids = []

            # Parse prices (comma-separated string)
            prices = []
            if outcome_prices:
                try:
                    prices = [Decimal(p.strip()) for p in outcome_prices.split(",")]
                except Exception:
                    prices = []

            for i, token_id in enumerate(clob_token_ids):
                outcome = outcomes[i] if i < len(outcomes) else f"Outcome {i}"
                price = prices[i] if i < len(prices) else None

                token = Token(
                    token_id=token_id,
                    outcome=outcome,
                    price=price,
                    winner=None,
                )
                tokens.append(token)

            if not tokens:
                return None

            # Build market info
            market = MarketInfo(
                condition_id=condition_id,
                question_id=data.get("questionID", ""),
                question=data.get("question", ""),
                slug=data.get("slug", ""),
                tokens=tokens,
                end_date=data.get("endDateIso"),
                active=data.get("active", True),
                closed=data.get("closed", False),
                volume=Decimal(str(data.get("volumeNum", 0) or 0)),
                liquidity=Decimal(str(data.get("liquidityNum", 0) or 0)),
                neg_risk=data.get("negRisk", False),
            )

            return market

        except Exception as e:
            logger.warning("Failed to parse market", error=str(e))
            return None

    def get_cached_market(self, condition_id: str) -> MarketInfo | None:
        """Get market from cache."""
        return self._cache.get_market(condition_id)

    def get_cached_market_by_token(self, token_id: str) -> MarketInfo | None:
        """Get market from cache by token ID."""
        return self._cache.get_market_by_token(token_id)

    def get_all_cached_markets(self) -> list[MarketInfo]:
        """Get all cached markets."""
        return list(self._cache.markets.values())

    def get_tradeable_markets(self) -> list[MarketInfo]:
        """Get all tradeable (active, not closed) markets from cache."""
        return [m for m in self._cache.markets.values() if m.is_tradeable]

    def get_binary_markets(self) -> list[MarketInfo]:
        """Get all binary markets from cache."""
        return [m for m in self._cache.markets.values() if m.is_binary]


async def fetch_all_markets(
    min_volume: float = 1000,
    min_liquidity: float = 500,
) -> list[MarketInfo]:
    """
    Convenience function to fetch all tradeable binary markets.

    Args:
        min_volume: Minimum volume filter
        min_liquidity: Minimum liquidity filter

    Returns:
        List of MarketInfo objects
    """
    fetcher = MarketFetcher()
    try:
        markets = await fetcher.fetch_markets(
            active_only=True,
            binary_only=True,
            min_volume=min_volume,
            min_liquidity=min_liquidity,
            limit=200,
        )
        return markets
    finally:
        await fetcher.close()
