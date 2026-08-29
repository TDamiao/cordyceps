"""
Arbitrage detection engine for Polymarket.

Implements Unity Constraint arbitrage detection with depth-aware VWAP:
- BUY_SET: When VWAP of ask prices < 1.0 across book levels
- SELL_SET: When VWAP of bid prices > 1.0 across book levels

Includes stale-book rejection, slippage guards, and leg-risk buffer.
"""

import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

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
    prices: list[Decimal]           # Best-level execution prices for each token
    sizes: list[Decimal]            # Available size at each best level
    max_size: Decimal               # Maximum uniform trade size
    total_cost: Decimal             # Total cost to enter position
    expected_payout: Decimal        # Expected payout (1.0 per set)
    gross_profit: Decimal           # Profit before fees
    fees: Decimal                   # Total fees
    net_profit: Decimal             # Profit after fees
    profit_pct: Decimal             # Profit as percentage
    # Depth-aware VWAP fields
    vwap_prices: list[Decimal] = field(default_factory=list)       # VWAP price per leg
    executable_quantity: Decimal = Decimal("0")                     # quantity we can fill
    edge: Decimal = Decimal("0")       # raw edge (BUY: 1-sum_vwap; SELL: sum_vwap-1)
    roi: Decimal = Decimal("0")        # net_profit / total_cost
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
    max_slippage_pct: Decimal = Decimal("0.005")      # Max slippage tolerance
    orderbook_stale_ms: int = 3000                     # Max age of orderbook before rejection
    leg_risk_buffer: Decimal = Decimal("0.0")          # Extra buffer subtracted from edge
    taker_fee: Decimal = Decimal(str(TradingConfig.TAKER_FEE))
    maker_fee: Decimal = Decimal(str(TradingConfig.MAKER_FEE))


class ArbitrageEngine:
    """
    Core arbitrage detection engine.

    Analyzes order books for binary markets to find Unity Constraint violations:
    - BUY_SET: Sum of best asks < 1.0 (discounted dollar)
    - SELL_SET: Sum of best bids > 1.0 (premium dollar)

    Depth-aware: walks multiple price levels to compute VWAP and
    identifies the executable quantity that remains profitable.
    """

    def __init__(self, config: ArbitrageConfig | None = None):
        settings = get_settings()
        self.config = config or ArbitrageConfig(
            min_profit_threshold=Decimal(str(settings.min_profit_threshold)),
            max_position_size=Decimal(str(settings.max_position_size)),
            max_slippage_pct=Decimal(str(settings.max_slippage_pct)),
            orderbook_stale_ms=settings.orderbook_stale_ms,
        )
        self._opportunities_found = 0
        self._opportunities_executed = 0
        self._stale_books_rejected = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_market(
        self,
        market_id: str,
        order_books: dict[str, OrderBook],
    ) -> ArbitrageOpportunity | None:
        if len(order_books) != 2:
            logger.debug("Skipping non-binary market", market_id=market_id)
            return None

        # Stale-book guard
        if self._has_stale_book(order_books):
            self._stale_books_rejected += 1
            return None

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

    @property
    def stats(self) -> dict:
        return {
            "opportunities_found": self._opportunities_found,
            "opportunities_executed": self._opportunities_executed,
            "stale_books_rejected": self._stale_books_rejected,
        }

    def reset_stats(self) -> None:
        self._opportunities_found = 0
        self._opportunities_executed = 0
        self._stale_books_rejected = 0

    # ------------------------------------------------------------------
    # Stale-book check
    # ------------------------------------------------------------------

    def _has_stale_book(self, order_books: dict[str, OrderBook]) -> bool:
        if self.config.orderbook_stale_ms <= 0:
            return False
        now_ms = int(time.time() * 1000)
        for book in order_books.values():
            if book.timestamp is None:
                continue  # No timestamp -> treat as fresh
            if now_ms - book.timestamp > self.config.orderbook_stale_ms:
                logger.debug(
                    "Stale book detected",
                    token_id=book.token_id,
                    age_ms=now_ms - book.timestamp,
                    max_ms=self.config.orderbook_stale_ms,
                )
                return True
        return False

    # ------------------------------------------------------------------
    # BUY SET – walk ask levels (cheapest first)
    # ------------------------------------------------------------------

    def _check_buy_set(
        self,
        market_id: str,
        order_books: dict[str, OrderBook],
    ) -> ArbitrageOpportunity | None:
        token_ids = list(order_books.keys())
        legs = [order_books[tid] for tid in token_ids]

        # Collect ask levels per leg (sorted cheapest-first = default)
        leg_levels: list[list[OrderBookLevel]] = []
        for book in legs:
            lvls = sorted(book.asks, key=lambda x: x.price)
            if not lvls:
                return None
            leg_levels.append(lvls)

        # Initial VWAP from first level only
        sum(
            leg_levels[i][0].price for i in range(len(token_ids))
        )

        # Walk levels until VWAP sum >= 1.0 (no more edge)
        cum_size = [Decimal("0") for _ in token_ids]
        cum_cost = [Decimal("0") for _ in token_ids]
        total_executable = Decimal("0")
        profitable = True
        filled_any = False

        for lvl_idx in range(min(len(leg) for leg in leg_levels)):
            avail = min(leg_levels[li][lvl_idx].size for li in range(len(token_ids)))
            remaining_budget = self.config.max_position_size - total_executable
            fill = min(avail, remaining_budget)

            if fill <= 0:
                break

            new_cum_size = [cum_size[i] + fill for i in range(len(token_ids))]
            new_cum_cost = [cum_cost[i] + fill * leg_levels[i][lvl_idx].price for i in range(len(token_ids))]
            new_total = total_executable + fill

            new_vwap = [new_cum_cost[i] / new_cum_size[i] for i in range(len(token_ids))]
            vwap_sum = sum(new_vwap)
            edge = Decimal("1") - vwap_sum

            if edge <= self.config.leg_risk_buffer:
                # This level is unprofitable - stop but keep fills so far
                if filled_any:
                    break
                profitable = False
                break

            # Accept this fill
            cum_size = new_cum_size
            cum_cost = new_cum_cost
            total_executable = new_total
            filled_any = True

        if total_executable < self.config.min_liquidity or not profitable or total_executable <= 0:
            return None

        # Compute final VWAP
        if any(c == Decimal("0") for c in cum_size):
            return None
        vwap_prices = [cum_cost[i] / cum_size[i] for i in range(len(token_ids))]
        vwap_prices = [v.quantize(Decimal("0.01")) for v in vwap_prices]
        vwap_sum = sum(vwap_prices)
        total_cost = vwap_sum * total_executable
        expected_payout = total_executable
        gross_profit = expected_payout - total_cost
        fees = self.config.taker_fee * total_cost * len(token_ids)
        net_profit = gross_profit - fees
        profit_pct = net_profit / total_cost if total_cost > 0 else Decimal("0")

        if profit_pct < self.config.min_profit_threshold:
            return None

        edge = Decimal("1") - vwap_sum
        roi = net_profit / total_cost if total_cost > 0 else Decimal("0")

        return ArbitrageOpportunity(
            market_id=market_id,
            signal_type=SignalType.BUY_SET,
            token_ids=token_ids,
            prices=[leg_levels[i][0].price for i in range(len(token_ids))],
            sizes=[leg_levels[i][0].size for i in range(len(token_ids))],
            max_size=total_executable,
            total_cost=total_cost,
            expected_payout=expected_payout,
            gross_profit=gross_profit,
            fees=fees,
            net_profit=net_profit,
            profit_pct=profit_pct,
            vwap_prices=vwap_prices,
            executable_quantity=total_executable,
            edge=edge,
            roi=roi,
        )

    # ------------------------------------------------------------------
    # SELL SET – walk bid levels (highest first)
    # ------------------------------------------------------------------

    def _check_sell_set(
        self,
        market_id: str,
        order_books: dict[str, OrderBook],
    ) -> ArbitrageOpportunity | None:
        token_ids = list(order_books.keys())
        legs = [order_books[tid] for tid in token_ids]

        leg_levels: list[list[OrderBookLevel]] = []
        for book in legs:
            lvls = sorted(book.bids, key=lambda x: x.price, reverse=True)
            if not lvls:
                return None
            leg_levels.append(lvls)

        # Initial VWAP from first level only
        sum(
            leg_levels[i][0].price for i in range(len(token_ids))
        )

        cum_size = [Decimal("0") for _ in token_ids]
        cum_rev = [Decimal("0") for _ in token_ids]
        total_executable = Decimal("0")
        profitable = True
        filled_any = False

        for lvl_idx in range(min(len(leg) for leg in leg_levels)):
            avail = min(leg_levels[li][lvl_idx].size for li in range(len(token_ids)))
            remaining_budget = self.config.max_position_size - total_executable
            fill = min(avail, remaining_budget)

            if fill <= 0:
                break

            new_cum_size = [cum_size[i] + fill for i in range(len(token_ids))]
            new_cum_rev = [cum_rev[i] + fill * leg_levels[i][lvl_idx].price for i in range(len(token_ids))]
            new_total = total_executable + fill

            new_vwap = [new_cum_rev[i] / new_cum_size[i] for i in range(len(token_ids))]
            vwap_sum = sum(new_vwap)
            edge = vwap_sum - Decimal("1")

            if edge <= self.config.leg_risk_buffer:
                if filled_any:
                    break
                profitable = False
                break

            cum_size = new_cum_size
            cum_rev = new_cum_rev
            total_executable = new_total
            filled_any = True

        if total_executable < self.config.min_liquidity or not profitable or total_executable <= 0:
            return None

        if any(c == Decimal("0") for c in cum_size):
            return None
        vwap_prices = [cum_rev[i] / cum_size[i] for i in range(len(token_ids))]
        vwap_prices = [v.quantize(Decimal("0.000001")) for v in vwap_prices]
        vwap_sum = sum(vwap_prices)
        total_revenue = vwap_sum * total_executable
        split_cost = total_executable
        gross_profit = total_revenue - split_cost
        fees = self.config.taker_fee * total_revenue * len(token_ids)
        net_profit = gross_profit - fees
        profit_pct = net_profit / split_cost if split_cost > 0 else Decimal("0")

        if profit_pct < self.config.min_profit_threshold:
            return None

        edge = vwap_sum - Decimal("1")
        roi = net_profit / split_cost if split_cost > 0 else Decimal("0")

        return ArbitrageOpportunity(
            market_id=market_id,
            signal_type=SignalType.SELL_SET,
            token_ids=token_ids,
            prices=[leg_levels[i][0].price for i in range(len(token_ids))],
            sizes=[leg_levels[i][0].size for i in range(len(token_ids))],
            max_size=total_executable,
            total_cost=split_cost,
            expected_payout=total_revenue,
            gross_profit=gross_profit,
            fees=fees,
            net_profit=net_profit,
            profit_pct=profit_pct,
            vwap_prices=vwap_prices,
            executable_quantity=total_executable,
            edge=edge,
            roi=roi,
        )

    # ------------------------------------------------------------------
    # Legacy compatibility
    # ------------------------------------------------------------------

    def calculate_optimal_size(
        self,
        order_books: dict[str, OrderBook],
        signal_type: SignalType,
        max_size: Decimal,
    ) -> Decimal:
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


# ------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------

def calculate_price_sum(order_books: dict[str, OrderBook], side: str) -> Decimal | None:
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
    price_sum = calculate_price_sum(order_books, "ask")
    return price_sum is not None and price_sum < Decimal("1")


def is_sell_opportunity(order_books: dict[str, OrderBook]) -> bool:
    price_sum = calculate_price_sum(order_books, "bid")
    return price_sum is not None and price_sum > Decimal("1")
