"""
Favorite Compounding Strategy Engine for Polymarket.

Targets binary markets where one outcome is a heavy favorite (85-98c)
approaching resolution (<72h). We buy the favorite expecting it to
resolve to $1.00, compounding small edges over many trades.

Key insight (post March 2026 fee reform):
- Fee curve peaks at 50/50 (~$1.00 per 100 shares)
- Fee curve drops dramatically at extremes (0c and 100c)
- Buying at 95c costs ~$0.03 in fees vs ~$1.00 at 50c
- Win rate ~88-92% with 4-6% return per trade
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from src.client.models import OrderBook
from src.config import get_settings
from src.fees import FeeParameters, calculate_taker_fee
from src.utils.logging import get_logger

logger = get_logger(__name__)


class FavoriteAction(Enum):
    """Action for favorite position management."""

    BUY = "BUY"
    HOLD = "HOLD"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"


@dataclass
class FavoriteConfig:
    """Configuration for favorite compounding strategy."""

    min_probability: Decimal = Decimal("0.90")
    min_price: Decimal = Decimal("0.85")
    max_price: Decimal = Decimal("0.98")
    min_size_usd: Decimal = Decimal("5.0")
    max_exposure_pct: Decimal = Decimal("0.30")
    kelly_fraction: Decimal = Decimal("0.25")
    take_profit: Decimal = Decimal("0.97")
    stop_loss: Decimal = Decimal("0.80")
    max_time_to_resolution_h: int = 72


@dataclass
class FavoriteOpportunity:
    """Detected favorite compounding opportunity."""

    market_id: str
    market_question: str
    favorite_token_id: str
    underdog_token_id: str
    favorite_price: Decimal
    underdog_price: Decimal
    favorite_bid: Decimal
    favorite_ask: Decimal
    favorite_size: Decimal
    time_to_resolution_h: float
    implied_probability: Decimal
    expected_return_pct: Decimal
    position_size_usd: Decimal
    position_shares: Decimal
    fees_estimate: Decimal
    net_edge: Decimal
    is_profitable: bool
    rejection_reason: str | None = None
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    @property
    def price_cents(self) -> int:
        """Price in cents (95c = 95)."""
        return int(float(self.favorite_price) * 100)


@dataclass
class FavoritePosition:
    """Open favorite position for monitoring."""

    market_id: str
    market_question: str
    token_id: str
    entry_price: Decimal
    entry_time: int
    size_shares: Decimal
    size_usd: Decimal
    take_profit_price: Decimal
    stop_loss_price: Decimal
    time_to_resolution_h: float
    unrealized_pnl_pct: Decimal = Decimal("0")
    current_price: Decimal | None = None
    action: FavoriteAction = FavoriteAction.HOLD


class FavoriteEngine:
    """
    Favorite Compounding Detection Engine.

    Finds binary markets where:
    1. One outcome is a heavy favorite (85-98c)
    2. Time to resolution < 72 hours
    3. Sufficient liquidity at favorite price
    4. Positive expected value after fees
    """

    def __init__(self, config: FavoriteConfig | None = None):
        settings = get_settings()
        self.config = config or FavoriteConfig(
            min_probability=Decimal(str(settings.min_favorite_probability)),
            min_price=Decimal(str(settings.min_favorite_price)),
            max_price=Decimal(str(settings.max_favorite_price)),
            min_size_usd=Decimal(str(settings.min_favorite_size_usd)),
            max_exposure_pct=Decimal(str(settings.max_favorite_exposure_pct)),
            kelly_fraction=Decimal(str(settings.favorite_kelly_fraction)),
            take_profit=Decimal(str(settings.favorite_take_profit)),
            stop_loss=Decimal(str(settings.favorite_stop_loss)),
        )
        self._metrics = {
            "markets_analyzed": 0,
            "favorite_candidates": 0,
            "opportunities_found": 0,
            "rejected_time": 0,
            "rejected_price": 0,
            "rejected_probability": 0,
            "rejected_liquidity": 0,
            "rejected_edge": 0,
            "rejected_fee": 0,
        }
        self._fee_params: FeeParameters | None = None

    def analyze_market(
        self,
        market_id: str,
        market_question: str,
        order_books: dict[str, OrderBook],
        time_to_resolution_h: float,
        fee_params: FeeParameters | None = None,
    ) -> FavoriteOpportunity | None:
        """
        Analyze a binary market for favorite compounding opportunity.

        Args:
            market_id: Polymarket market ID
            market_question: Market question text
            order_books: Dict of token_id -> OrderBook (must be exactly 2)
            time_to_resolution_h: Hours until market resolution
            fee_params: Optional fee parameters from CLOB

        Returns:
            FavoriteOpportunity if found, None otherwise
        """
        self._metrics["markets_analyzed"] += 1
        self._fee_params = fee_params

        if len(order_books) != 2:
            return None

        # Time filter
        if time_to_resolution_h > self.config.max_time_to_resolution_h:
            self._metrics["rejected_time"] += 1
            return None

        # Get the two tokens
        tokens = list(order_books.keys())
        token_a, token_b = tokens[0], tokens[1]
        book_a, book_b = order_books[token_a], order_books[token_b]

        # Find best prices using OrderBook properties
        if book_a.best_ask is None or book_b.best_ask is None:
            return None
        if book_a.best_bid is None or book_b.best_bid is None:
            return None

        ask_a = book_a.best_ask.price
        ask_b = book_b.best_ask.price
        bid_a = book_a.best_bid.price
        bid_b = book_b.best_bid.price

        # Determine favorite (higher ask = higher probability)
        if ask_a > ask_b:
            fav_token, ud_token = token_a, token_b
            fav_price, ud_price = ask_a, ask_b
            fav_bid = bid_a
            fav_book = book_a
        else:
            fav_token, ud_token = token_b, token_a
            fav_price, ud_price = ask_b, ask_a
            fav_bid = bid_b
            fav_book = book_b

        # Implied probability: price = probability of YES
        implied_prob = fav_price

        # Price range filter
        if fav_price < self.config.min_price or fav_price > self.config.max_price:
            self._metrics["rejected_price"] += 1
            return None

        # Probability filter
        if implied_prob < self.config.min_probability:
            self._metrics["rejected_probability"] += 1
            return None

        # Liquidity: sum shares at or near best ask
        available_size = Decimal("0")
        for level in fav_book.asks:
            if level.price <= fav_price * Decimal("1.005"):
                available_size += level.size
            else:
                break

        if available_size * fav_price < self.config.min_size_usd:
            self._metrics["rejected_liquidity"] += 1
            return None

        # Expected return if resolved to $1
        expected_return = (Decimal("1") - fav_price) / fav_price

        # Kelly position sizing
        bankroll = Decimal(str(get_settings().max_total_exposure_usd))
        max_exposure = bankroll * self.config.max_exposure_pct
        kelly_pct = self._calculate_kelly_fraction(implied_prob, expected_return)

        position_size_usd = min(
            max_exposure,
            kelly_pct * bankroll,
            available_size * fav_price * Decimal("0.5"),
        )
        position_shares = position_size_usd / fav_price

        # Fee estimate
        fees_estimate = self._estimate_fees(fav_price, position_shares)
        net_edge = expected_return - (fees_estimate / position_size_usd)

        if net_edge <= 0:
            self._metrics["rejected_fee"] += 1
            return None

        self._metrics["favorite_candidates"] += 1
        self._metrics["opportunities_found"] += 1

        logger.info(
            "Favorite opportunity found",
            market_id=market_id,
            price_cents=int(float(fav_price) * 100),
            prob=float(implied_prob),
            time_to_res_h=time_to_resolution_h,
            expected_return_pct=float(expected_return * 100),
            position_usd=float(position_size_usd),
        )

        return FavoriteOpportunity(
            market_id=market_id,
            market_question=market_question,
            favorite_token_id=fav_token,
            underdog_token_id=ud_token,
            favorite_price=fav_price,
            underdog_price=ud_price,
            favorite_bid=fav_bid,
            favorite_ask=fav_price,
            favorite_size=available_size,
            time_to_resolution_h=time_to_resolution_h,
            implied_probability=implied_prob,
            expected_return_pct=expected_return * Decimal("100"),
            position_size_usd=position_size_usd,
            position_shares=position_shares,
            fees_estimate=fees_estimate,
            net_edge=net_edge,
            is_profitable=True,
        )

    def create_position(self, opp: FavoriteOpportunity) -> FavoritePosition:
        """Create a trackable position from an opportunity."""
        return FavoritePosition(
            market_id=opp.market_id,
            market_question=opp.market_question,
            token_id=opp.favorite_token_id,
            entry_price=opp.favorite_price,
            entry_time=int(time.time()),
            size_shares=opp.position_shares,
            size_usd=opp.position_size_usd,
            take_profit_price=self.config.take_profit,
            stop_loss_price=self.config.stop_loss,
            time_to_resolution_h=opp.time_to_resolution_h,
        )

    def check_position(
        self,
        position: FavoritePosition,
        current_price: Decimal,
        current_bid: Decimal,
    ) -> FavoriteAction:
        """Check position against TP/SL/time thresholds."""
        position.current_price = current_price
        position.unrealized_pnl_pct = (
            (current_price - position.entry_price) / position.entry_price * Decimal("100")
        )

        # Take profit
        if current_price >= position.take_profit_price:
            position.action = FavoriteAction.TAKE_PROFIT
            return FavoriteAction.TAKE_PROFIT

        # Stop loss (using bid price — what we could actually sell at)
        if current_bid <= position.stop_loss_price:
            position.action = FavoriteAction.STOP_LOSS
            return FavoriteAction.STOP_LOSS

        # Time-based exit: < 1h to resolution, in profit
        elapsed_h = (time.time() - position.entry_time) / 3600
        remaining_h = position.time_to_resolution_h - elapsed_h
        if remaining_h <= 1 and current_price > position.entry_price:
            position.action = FavoriteAction.TAKE_PROFIT
            return FavoriteAction.TAKE_PROFIT

        position.action = FavoriteAction.HOLD
        return FavoriteAction.HOLD

    def get_metrics(self) -> dict:
        return self._metrics.copy()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _estimate_fees(self, price: Decimal, size: Decimal) -> Decimal:
        """Estimate taker fees. Uses CLOB endpoint when available, empirical curve otherwise."""
        if self._fee_params:
            return calculate_taker_fee(size, price, self._fee_params)

        # Empirical post-March-2026 fee curve
        p = float(price)
        if p >= 0.95:
            fee_per_share = Decimal("0.0003")
        elif p >= 0.90:
            fee_per_share = Decimal("0.0005")
        elif p >= 0.85:
            fee_per_share = Decimal("0.001")
        else:
            fee_per_share = Decimal("0.005")
        return fee_per_share * size

    def _calculate_kelly_fraction(self, win_prob: Decimal, avg_win: Decimal) -> Decimal:
        """
        Kelly fraction for position sizing.
        Kelly = (p*b - q) / b
        Win: resolve to $1.  Loss: hit stop_loss.
        """
        p = float(win_prob)
        q = 1 - p
        b = float(avg_win) / max(float((win_prob - self.config.stop_loss) / win_prob), 0.001)
        kelly = (p * b - q) / b if b > 0 else 0
        return Decimal(str(max(0, min(kelly * float(self.config.kelly_fraction), 0.10))))


def create_favorite_engine() -> FavoriteEngine:
    """Create favorite engine with default settings."""
    return FavoriteEngine()
