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
from src.config import get_settings
from src.fees import FeeParameters, calculate_taker_fee
from src.utils.logging import get_logger

logger = get_logger(__name__)


class SignalType(Enum):
    """Type of arbitrage signal."""

    BUY_SET = "BUY_SET"  # Buy all outcomes (sum of asks < 1)
    SELL_SET = "SELL_SET"  # Sell all outcomes (sum of bids > 1)


@dataclass
class ArbitrageOpportunity:
    """Detected arbitrage opportunity."""

    market_id: str
    signal_type: SignalType
    token_ids: list[str]
    prices: list[Decimal]  # Best-level execution prices for each token
    sizes: list[Decimal]  # Available size at each best level
    max_size: Decimal  # Maximum uniform trade size
    total_cost: Decimal  # Total cost to enter position
    expected_payout: Decimal  # Expected payout (1.0 per set)
    gross_profit: Decimal  # Profit before fees
    fees: Decimal  # Total fees
    net_profit: Decimal  # Profit after fees
    profit_pct: Decimal  # Profit as percentage
    # Depth-aware VWAP fields
    vwap_prices: list[Decimal] = field(default_factory=list)  # VWAP price per leg
    executable_quantity: Decimal = Decimal("0")  # quantity we can fill
    edge: Decimal = Decimal("0")  # raw edge (BUY: 1-sum_vwap; SELL: sum_vwap-1)
    roi: Decimal = Decimal("0")  # net_profit / total_cost
    gross_edge: Decimal = Decimal("0")
    expected_slippage: Decimal = Decimal("0")
    leg_risk_buffer: Decimal = Decimal("0")
    net_edge: Decimal = Decimal("0")
    fee_source: str = "fallback"
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    @property
    def is_profitable(self) -> bool:
        """Check if opportunity is profitable after fees."""
        return self.net_profit > 0


@dataclass
class ArbitrageConfig:
    """Configuration for arbitrage detection."""

    min_profit_threshold: Decimal = Decimal("0.005")  # 0.5% minimum profit
    max_position_size: Decimal = Decimal("1000")  # Max USDC per trade
    min_liquidity: Decimal = Decimal("10")  # Min size at price level
    max_slippage_pct: Decimal = Decimal("0.005")  # Max slippage tolerance
    orderbook_stale_ms: int = 3000  # Max age of orderbook before rejection
    leg_risk_buffer: Decimal = Decimal("0.0")  # Extra buffer subtracted from edge
    # Compatibility override for tests. Production treats this as a V2 curve rate,
    # never as a flat percentage of notional.
    taker_fee: Decimal = Decimal("0.072")
    fee_exponent: Decimal = Decimal("1")
    maker_fee: Decimal = Decimal("0")
    min_net_edge: Decimal = Decimal("0")
    min_net_profit_usd: Decimal = Decimal("0")
    min_trade_shares: Decimal = Decimal("1")


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
            max_position_size=Decimal(str(settings.max_trade_usd)),
            min_liquidity=Decimal(str(settings.min_trade_shares)),
            max_slippage_pct=Decimal(str(settings.max_slippage_pct)),
            orderbook_stale_ms=settings.orderbook_stale_ms,
            leg_risk_buffer=Decimal(str(settings.leg_risk_buffer)),
            taker_fee=Decimal(str(settings.fee_fallback_rate)),
            min_net_edge=Decimal(str(settings.min_net_edge)),
            min_net_profit_usd=Decimal(str(settings.min_net_profit_usd)),
            min_trade_shares=Decimal(str(settings.min_trade_shares)),
        )
        self._metrics = {
            "markets_analyzed": 0,
            "buy_opportunities_raw": 0,
            "sell_opportunities_raw": 0,
            "opportunities_found": 0,
            "opportunities_executed": 0,
            "rejected_stale": 0,
            "rejected_liquidity": 0,
            "rejected_edge": 0,
            "rejected_profit": 0,
            "rejected_slippage": 0,
            "rejected_fee": 0,
            "rejected_risk": 0,
            "best_buy_edge_seen": 0.0,
            "best_sell_edge_seen": 0.0,
            "best_net_edge_seen": 0.0,
            "best_net_profit_seen": 0.0,
            "avg_buy_ask_sum": 0.0,
            "avg_sell_bid_sum": 0.0,
        }
        self._buy_sum_total = Decimal("0")
        self._sell_sum_total = Decimal("0")
        self._closest: list[dict] = []
        self._fee_params: FeeParameters | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_market(
        self,
        market_id: str,
        order_books: dict[str, OrderBook],
        fee_params: FeeParameters | None = None,
    ) -> ArbitrageOpportunity | None:
        self._metrics["markets_analyzed"] += 1
        self._fee_params = fee_params
        if len(order_books) != 2:
            logger.debug("Skipping non-binary market", market_id=market_id)
            return None

        # Stale-book guard
        if self._has_stale_book(order_books):
            self._metrics["rejected_stale"] += 1
            return None

        ask_sum = calculate_price_sum(order_books, "ask")
        bid_sum = calculate_price_sum(order_books, "bid")
        count = self._metrics["markets_analyzed"]
        if ask_sum is not None:
            self._buy_sum_total += ask_sum
            self._metrics["avg_buy_ask_sum"] = float(self._buy_sum_total / count)
            raw = Decimal("1") - ask_sum
            self._metrics["best_buy_edge_seen"] = max(
                self._metrics["best_buy_edge_seen"], float(raw)
            )
            if raw > 0:
                self._metrics["buy_opportunities_raw"] += 1
                self._record_closest_raw(market_id, SignalType.BUY_SET, order_books, raw, "ask")
        if bid_sum is not None:
            self._sell_sum_total += bid_sum
            self._metrics["avg_sell_bid_sum"] = float(self._sell_sum_total / count)
            raw = bid_sum - Decimal("1")
            self._metrics["best_sell_edge_seen"] = max(
                self._metrics["best_sell_edge_seen"], float(raw)
            )
            if raw > 0:
                self._metrics["sell_opportunities_raw"] += 1
                self._record_closest_raw(market_id, SignalType.SELL_SET, order_books, raw, "bid")

        buy_opp = self._check_buy_set(market_id, order_books)
        if buy_opp and buy_opp.is_profitable:
            self._record_found(buy_opp)
            logger.info(
                "BUY_SET opportunity found",
                market_id=market_id,
                profit_pct=f"{buy_opp.profit_pct:.4f}",
                size=str(buy_opp.max_size),
            )
            return buy_opp

        sell_opp = self._check_sell_set(market_id, order_books)
        if sell_opp and sell_opp.is_profitable:
            self._record_found(sell_opp)
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
            **self._metrics,
            "stale_books_rejected": self._metrics["rejected_stale"],
            "closest_opportunities": list(self._closest),
        }

    def reset_stats(self) -> None:
        for key in self._metrics:
            self._metrics[key] = 0.0 if key.startswith(("best_", "avg_")) else 0
        self._buy_sum_total = Decimal("0")
        self._sell_sum_total = Decimal("0")
        self._closest.clear()

    def _record_found(self, opportunity: ArbitrageOpportunity) -> None:
        self._metrics["opportunities_found"] += 1
        self._metrics["best_net_edge_seen"] = max(
            self._metrics["best_net_edge_seen"], float(opportunity.net_edge)
        )
        self._metrics["best_net_profit_seen"] = max(
            self._metrics["best_net_profit_seen"], float(opportunity.net_profit)
        )
        row = {
            "market_id": opportunity.market_id,
            "signal": opportunity.signal_type.value,
            "net_edge": float(opportunity.net_edge),
            "net_profit": float(opportunity.net_profit),
            "timestamp": opportunity.timestamp,
        }
        self._closest = [
            existing
            for existing in self._closest
            if not (
                existing["market_id"] == opportunity.market_id
                and existing["signal"] == opportunity.signal_type.value
            )
        ]
        self._closest.append(row)
        self._closest = sorted(self._closest, key=lambda row: row["net_edge"], reverse=True)[:20]

    def _record_closest_raw(
        self,
        market_id: str,
        signal: SignalType,
        books: dict[str, OrderBook],
        gross_edge: Decimal,
        side: str,
    ) -> None:
        levels = [book.best_ask if side == "ask" else book.best_bid for book in books.values()]
        if any(level is None for level in levels):
            return
        prices = [level.price for level in levels if level]
        size = min(level.size for level in levels if level)
        fee_per_share, _ = self._fees(Decimal("1"), prices)
        net_edge = gross_edge - fee_per_share - self.config.leg_risk_buffer
        row = {
            "market_id": market_id,
            "signal": signal.value,
            "gross_edge": float(gross_edge),
            "net_edge": float(net_edge),
            "net_profit": float(net_edge * size),
            "timestamp": int(time.time() * 1000),
        }
        self._closest = [
            existing
            for existing in self._closest
            if not (existing["market_id"] == market_id and existing["signal"] == signal.value)
        ]
        self._closest.append(row)
        self._closest = sorted(self._closest, key=lambda item: item["net_edge"], reverse=True)[:20]

    def _fees(self, quantity: Decimal, prices: list[Decimal]) -> tuple[Decimal, str]:
        params = self._fee_params or FeeParameters(
            rate=self.config.taker_fee,
            exponent=self.config.fee_exponent,
            source="fallback",
        )
        try:
            return (
                sum(calculate_taker_fee(quantity, price, params) for price in prices),
                params.source,
            )
        except ValueError:
            self._metrics["rejected_fee"] += 1
            return Decimal("Infinity"), params.source

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
        sum(leg_levels[i][0].price for i in range(len(token_ids)))

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
            new_cum_cost = [
                cum_cost[i] + fill * leg_levels[i][lvl_idx].price for i in range(len(token_ids))
            ]
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

        if total_executable < max(self.config.min_liquidity, self.config.min_trade_shares):
            self._metrics["rejected_liquidity"] += 1
            return None
        if not profitable or total_executable <= 0:
            self._metrics["rejected_edge"] += 1
            return None

        # Compute final VWAP
        if any(c == Decimal("0") for c in cum_size):
            return None
        vwap_prices = [cum_cost[i] / cum_size[i] for i in range(len(token_ids))]
        vwap_prices = [v.quantize(Decimal("0.01")) for v in vwap_prices]
        vwap_sum = sum(vwap_prices)
        best_sum = sum(leg_levels[i][0].price for i in range(len(token_ids)))
        total_cost = vwap_sum * total_executable
        expected_payout = total_executable
        gross_edge = Decimal("1") - best_sum
        gross_profit = gross_edge * total_executable
        expected_slippage = max(Decimal("0"), (vwap_sum - best_sum) * total_executable)
        if (
            self.config.max_slippage_pct > 0
            and total_cost
            and expected_slippage / total_cost > self.config.max_slippage_pct
        ):
            self._metrics["rejected_slippage"] += 1
            return None
        fees, fee_source = self._fees(total_executable, vwap_prices)
        leg_buffer = self.config.leg_risk_buffer * total_executable
        net_profit = gross_profit - expected_slippage - fees - leg_buffer
        profit_pct = net_profit / total_cost if total_cost > 0 else Decimal("0")
        net_edge = net_profit / total_executable if total_executable else Decimal("0")

        if net_edge < max(self.config.min_profit_threshold, self.config.min_net_edge):
            self._metrics["rejected_edge"] += 1
            return None
        if net_profit < self.config.min_net_profit_usd:
            self._metrics["rejected_profit"] += 1
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
            gross_edge=gross_edge,
            expected_slippage=expected_slippage,
            leg_risk_buffer=leg_buffer,
            net_edge=net_edge,
            fee_source=fee_source,
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
        sum(leg_levels[i][0].price for i in range(len(token_ids)))

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
            new_cum_rev = [
                cum_rev[i] + fill * leg_levels[i][lvl_idx].price for i in range(len(token_ids))
            ]
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

        if total_executable < max(self.config.min_liquidity, self.config.min_trade_shares):
            self._metrics["rejected_liquidity"] += 1
            return None
        if not profitable or total_executable <= 0:
            self._metrics["rejected_edge"] += 1
            return None

        if any(c == Decimal("0") for c in cum_size):
            return None
        vwap_prices = [cum_rev[i] / cum_size[i] for i in range(len(token_ids))]
        vwap_prices = [v.quantize(Decimal("0.000001")) for v in vwap_prices]
        vwap_sum = sum(vwap_prices)
        best_sum = sum(leg_levels[i][0].price for i in range(len(token_ids)))
        total_revenue = vwap_sum * total_executable
        split_cost = total_executable
        gross_edge = best_sum - Decimal("1")
        gross_profit = gross_edge * total_executable
        expected_slippage = max(Decimal("0"), (best_sum - vwap_sum) * total_executable)
        if (
            self.config.max_slippage_pct > 0
            and split_cost
            and expected_slippage / split_cost > self.config.max_slippage_pct
        ):
            self._metrics["rejected_slippage"] += 1
            return None
        fees, fee_source = self._fees(total_executable, vwap_prices)
        leg_buffer = self.config.leg_risk_buffer * total_executable
        net_profit = gross_profit - expected_slippage - fees - leg_buffer
        profit_pct = net_profit / split_cost if split_cost > 0 else Decimal("0")
        net_edge = net_profit / total_executable if total_executable else Decimal("0")

        if net_edge < max(self.config.min_profit_threshold, self.config.min_net_edge):
            self._metrics["rejected_edge"] += 1
            return None
        if net_profit < self.config.min_net_profit_usd:
            self._metrics["rejected_profit"] += 1
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
            gross_edge=gross_edge,
            expected_slippage=expected_slippage,
            leg_risk_buffer=leg_buffer,
            net_edge=net_edge,
            fee_source=fee_source,
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
