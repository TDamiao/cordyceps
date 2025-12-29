"""
Arbitrage detection engine for Polymarket.

Implements Unity Constraint arbitrage detection:
- BUY_SET: When sum of ask prices < 1.0 (buy all outcomes cheap)
- SELL_SET: When sum of bid prices > 1.0 (sell all outcomes expensive)
"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Optional
import time

from src.client.models import OrderBook, OrderBookLevel
from src.config import TradingConfig, get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SignalType(Enum):
    """Type of arbitrage signal."""

    BUY_SET = "BUY_SET"    # Buy all outcomes (sum of asks < 1)
    SELL_SET = "SELL_SET"  # Sell all outcomes (sum of bids > 1)


@dataclass
class ArbitrageOpportunity:
    """Detected arbitrage opportunity."""

    market_id: str
    signal_type: SignalType
    token_ids: list[str]
    prices: list[Decimal]  # Execution prices for each token
    sizes: list[Decimal]   # Available size at each price level
    max_size: Decimal      # Maximum uniform trade size
    total_cost: Decimal    # Total cost to enter position
    expected_payout: Decimal  # Expected payout (1.0 per set)
    gross_profit: Decimal  # Profit before fees
    fees: Decimal          # Total fees
    net_profit: Decimal    # Profit after fees
    profit_pct: Decimal    # Profit as percentage
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    @property
    def is_profitable(self) -> bool:
        """Check if opportunity is profitable after fees."""
        return self.net_profit > 0


@dataclass
class ArbitrageConfig:
    """Configuration for arbitrage detection."""

    min_profit_threshold: Decimal = Decimal("0.005")  # 0.5% minimum profit
    max_position_size: Decimal = Decimal("1000")      # Max USDC per trade
    min_liquidity: Decimal = Decimal("10")            # Min size at price level
    taker_fee: Decimal = Decimal(str(TradingConfig.TAKER_FEE))
    maker_fee: Decimal = Decimal(str(TradingConfig.MAKER_FEE))


class ArbitrageEngine:
    """
    Core arbitrage detection engine.

    Analyzes order books for binary markets to find Unity Constraint violations:
    - BUY_SET: Sum of best asks < 1.0 (discounted dollar)
    - SELL_SET: Sum of best bids > 1.0 (premium dollar)
    """

    def __init__(self, config: Optional[ArbitrageConfig] = None):
        """
        Initialize arbitrage engine.

        Args:
            config: Engine configuration
        """
        settings = get_settings()
        self.config = config or ArbitrageConfig(
            min_profit_threshold=Decimal(str(settings.min_profit_threshold)),
            max_position_size=Decimal(str(settings.max_position_size)),
        )
        self._opportunities_found = 0
        self._opportunities_executed = 0

    def analyze_market(
        self,
        market_id: str,
        order_books: dict[str, OrderBook],
    ) -> Optional[ArbitrageOpportunity]:
        """
        Analyze a market for arbitrage opportunities.

        Args:
            market_id: Market condition ID
            order_books: Dict of token_id -> OrderBook for all outcomes

        Returns:
            ArbitrageOpportunity if found, None otherwise
        """
        if len(order_books) != 2:
            logger.debug("Skipping non-binary market", market_id=market_id)
            return None

        # Check for BUY_SET opportunity
        buy_opp = self._check_buy_set(market_id, order_books)
        if buy_opp and buy_opp.is_profitable:
            self._opportunities_found += 1
            logger.info(
                "BUY_SET opportunity found",
                market_id=market_id,
                profit_pct=f"{buy_opp.profit_pct:.4f}",
                size=str(buy_opp.max_size),
            )
            return buy_opp

        # Check for SELL_SET opportunity
        sell_opp = self._check_sell_set(market_id, order_books)
        if sell_opp and sell_opp.is_profitable:
            self._opportunities_found += 1
            logger.info(
                "SELL_SET opportunity found",
                market_id=market_id,
                profit_pct=f"{sell_opp.profit_pct:.4f}",
                size=str(sell_opp.max_size),
            )
            return sell_opp

        return None

    def _check_buy_set(
        self,
        market_id: str,
        order_books: dict[str, OrderBook],
    ) -> Optional[ArbitrageOpportunity]:
        """
        Check for BUY_SET opportunity (buy complete set for < $1).

        When sum of all ask prices < 1.0, we can buy all outcomes
        and merge them for $1 profit.
        """
        token_ids = list(order_books.keys())
        best_asks: list[Optional[OrderBookLevel]] = []

        for token_id in token_ids:
            book = order_books[token_id]
            if not book.best_ask:
                return None  # No liquidity on one side
            best_asks.append(book.best_ask)

        # Calculate total cost to buy complete set
        total_ask_price = sum(ask.price for ask in best_asks)

        if total_ask_price >= Decimal("1"):
            return None  # No opportunity

        # Calculate maximum size (limited by smallest ask size)
        min_ask_size = min(ask.size for ask in best_asks)
        max_size = min(min_ask_size, self.config.max_position_size)

        if max_size < self.config.min_liquidity:
            return None  # Not enough liquidity

        # Calculate economics
        total_cost = total_ask_price * max_size
        expected_payout = max_size  # Each complete set = $1
        gross_profit = expected_payout - total_cost

        # Calculate fees (taker fee on each leg)
        fees = self.config.taker_fee * total_cost * len(token_ids)
        net_profit = gross_profit - fees
        profit_pct = net_profit / total_cost if total_cost > 0 else Decimal("0")

        if profit_pct < self.config.min_profit_threshold:
            return None  # Below threshold

        return ArbitrageOpportunity(
            market_id=market_id,
            signal_type=SignalType.BUY_SET,
            token_ids=token_ids,
            prices=[ask.price for ask in best_asks],
            sizes=[ask.size for ask in best_asks],
            max_size=max_size,
            total_cost=total_cost,
            expected_payout=expected_payout,
            gross_profit=gross_profit,
            fees=fees,
            net_profit=net_profit,
            profit_pct=profit_pct,
        )

    def _check_sell_set(
        self,
        market_id: str,
        order_books: dict[str, OrderBook],
    ) -> Optional[ArbitrageOpportunity]:
        """
        Check for SELL_SET opportunity (sell complete set for > $1).

        When sum of all bid prices > 1.0, we can split $1 into outcomes
        and sell them for profit.
        """
        token_ids = list(order_books.keys())
        best_bids: list[Optional[OrderBookLevel]] = []

        for token_id in token_ids:
            book = order_books[token_id]
            if not book.best_bid:
                return None  # No liquidity on one side
            best_bids.append(book.best_bid)

        # Calculate total revenue from selling complete set
        total_bid_price = sum(bid.price for bid in best_bids)

        if total_bid_price <= Decimal("1"):
            return None  # No opportunity

        # Calculate maximum size (limited by smallest bid size)
        min_bid_size = min(bid.size for bid in best_bids)
        max_size = min(min_bid_size, self.config.max_position_size)

        if max_size < self.config.min_liquidity:
            return None  # Not enough liquidity

        # Calculate economics
        total_revenue = total_bid_price * max_size
        split_cost = max_size  # Cost to split $1 into outcomes
        gross_profit = total_revenue - split_cost

        # Calculate fees (taker fee on each leg)
        fees = self.config.taker_fee * total_revenue * len(token_ids)
        net_profit = gross_profit - fees
        profit_pct = net_profit / split_cost if split_cost > 0 else Decimal("0")

        if profit_pct < self.config.min_profit_threshold:
            return None  # Below threshold

        return ArbitrageOpportunity(
            market_id=market_id,
            signal_type=SignalType.SELL_SET,
            token_ids=token_ids,
            prices=[bid.price for bid in best_bids],
            sizes=[bid.size for bid in best_bids],
            max_size=max_size,
            total_cost=split_cost,
            expected_payout=total_revenue,
            gross_profit=gross_profit,
            fees=fees,
            net_profit=net_profit,
            profit_pct=profit_pct,
        )

    def calculate_optimal_size(
        self,
        order_books: dict[str, OrderBook],
        signal_type: SignalType,
        max_size: Decimal,
    ) -> Decimal:
        """
        Calculate optimal trade size considering depth of book.

        Walks through multiple price levels to find the size that
        maximizes profit while respecting liquidity constraints.

        Args:
            order_books: Order books for all outcomes
            signal_type: BUY_SET or SELL_SET
            max_size: Maximum position size

        Returns:
            Optimal trade size
        """
        # For now, use simple first-level analysis
        # TODO: Implement multi-level optimization
        if signal_type == SignalType.BUY_SET:
            min_size = min(
                book.best_ask.size if book.best_ask else Decimal("0")
                for book in order_books.values()
            )
        else:
            min_size = min(
                book.best_bid.size if book.best_bid else Decimal("0")
                for book in order_books.values()
            )

        return min(min_size, max_size)

    @property
    def stats(self) -> dict:
        """Get engine statistics."""
        return {
            "opportunities_found": self._opportunities_found,
            "opportunities_executed": self._opportunities_executed,
        }

    def reset_stats(self) -> None:
        """Reset engine statistics."""
        self._opportunities_found = 0
        self._opportunities_executed = 0


def calculate_price_sum(order_books: dict[str, OrderBook], side: str) -> Optional[Decimal]:
    """
    Calculate sum of best prices across all order books.

    Args:
        order_books: Dict of token_id -> OrderBook
        side: "ask" or "bid"

    Returns:
        Sum of prices, or None if any book lacks liquidity
    """
    total = Decimal("0")

    for book in order_books.values():
        if side == "ask":
            if not book.best_ask:
                return None
            total += book.best_ask.price
        else:
            if not book.best_bid:
                return None
            total += book.best_bid.price

    return total


def is_buy_opportunity(order_books: dict[str, OrderBook]) -> bool:
    """Check if sum of asks < 1 (buy opportunity)."""
    price_sum = calculate_price_sum(order_books, "ask")
    return price_sum is not None and price_sum < Decimal("1")


def is_sell_opportunity(order_books: dict[str, OrderBook]) -> bool:
    """Check if sum of bids > 1 (sell opportunity)."""
    price_sum = calculate_price_sum(order_books, "bid")
    return price_sum is not None and price_sum > Decimal("1")
