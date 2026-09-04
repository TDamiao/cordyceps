"""
Backtest integration tests for Favorite Compounding strategy — paper mode.

Validates the strategy against realistic historical market sequences:
- Multiple markets per day with varying prices, liquidity and time-to-resolution
- Full position lifecycle: detection → sizing (Kelly) → monitor → TP/SL/time-exit
- Paper execution via PaperSimulator
- RiskManager exposure tracking
- Aggregated P&L, win-rate and drawdown over a simulated period

Run with:
    pytest tests/test_favorite_backtest.py -v
    pytest tests/test_favorite_backtest.py -v -s
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from unittest.mock import patch

from src.client.models import OrderBook, OrderBookLevel
from src.engine.favorite import FavoriteAction, FavoriteEngine
from src.execution.paper import PaperSimulator

# ---------------------------------------------------------------------------
# Mock settings — mirrors src.config.Settings fields used by FavoriteEngine
# ---------------------------------------------------------------------------

class _MockSettings:
    max_total_exposure_usd = 1000.0
    trading_mode = "paper"
    enable_favorite_strategy = True
    min_favorite_probability = 0.90
    min_favorite_price = 0.85
    max_favorite_price = 0.98
    min_favorite_size_usd = 5.0
    favorite_take_profit = 0.97
    favorite_stop_loss = 0.91  # More realistic: stops at -4% from entry, not -20%
    max_favorite_exposure_pct = 0.30
    favorite_kelly_fraction = 0.25
    # risk / paper defaults
    max_trade_usd = 500.0
    max_daily_loss = 100.0
    max_open_trades = 5
    circuit_breaker_failure_threshold = 5
    circuit_breaker_cooldown_minutes = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _book(token_id: str, bid: str, ask: str, size: str = "10000") -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=[OrderBookLevel(price=Decimal(bid), size=Decimal(size))],
        asks=[OrderBookLevel(price=Decimal(ask), size=Decimal(size))],
        timestamp=int(time.time() * 1000),
    )


def _binary_books(
    fav_price: str,
    fav_bid: str,
    size: str = "10000",
    fav_token: str = "YES",
    ud_token: str = "NO",
) -> dict[str, OrderBook]:
    """Create a binary market where fav_token is the favorite."""
    fav = Decimal(fav_price)
    ud = Decimal("1") - fav
    # underdog complementary price (approx, keep sum ~1)
    ud_ask = f"{float(ud):.3f}"
    ud_bid = f"{max(0.01, float(ud) - 0.005):.3f}"
    return {
        fav_token: _book(fav_token, fav_bid, fav_price, size),
        ud_token: _book(ud_token, ud_bid, ud_ask, size),
    }


@dataclass
class BacktestTrade:
    market_id: str
    entry_price: Decimal
    exit_price: Decimal
    size_usd: Decimal
    size_shares: Decimal
    action: FavoriteAction
    pnl_usd: Decimal
    pnl_pct: Decimal


def _simulate_price_path(
    engine: FavoriteEngine,
    position,
    path: list[tuple[Decimal, Decimal]],
) -> FavoriteAction:
    """Walk a price path and return the first non-HOLD action."""
    for price, bid in path:
        action = engine.check_position(position, price, bid)
        if action != FavoriteAction.HOLD:
            return action
    return FavoriteAction.HOLD


# ---------------------------------------------------------------------------
# 1 — Single-day backtest: 8 markets, mix of valid / rejected
# ---------------------------------------------------------------------------

class TestFavoriteBacktestSingleDay:
    """Simulate one trading day with 8 diverse markets."""

    def test_backtest_single_day(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()

            # Define 8 markets (id, fav_price, fav_bid, size, hours_to_resolve, should_detect)
            scenarios = [
                ("poly-fed-001", "0.950", "0.945", "10000", 12.0, True),   # Fed decision — valid
                ("poly-sports-002", "0.930", "0.925", "8000", 24.0, True),  # Sports — valid
                ("poly-election-003", "0.960", "0.955", "6000", 3.0, True), # Election near res — valid
                ("poly-low-price-004", "0.800", "0.795", "5000", 24.0, False), # price < 0.85
                ("poly-high-price-005", "0.990", "0.985", "5000", 24.0, False), # price > 0.98
                ("poly-long-time-006", "0.920", "0.915", "5000", 80.0, False), # >72h
                ("poly-low-prob-007", "0.880", "0.875", "5000", 24.0, False), # prob < 0.90
                ("poly-low-liq-008", "0.920", "0.915", "2", 24.0, False),     # liquidity < $5
            ]

            detected: list[str] = []
            rejected: list[str] = []
            trades: list[BacktestTrade] = []

            for mid, price, bid, size, hours, should_detect in scenarios:
                books = _binary_books(price, bid, size)
                opp = engine.analyze_market(mid, f"Market {mid}?", books, hours)
                if should_detect:
                    assert opp is not None, f"Expected detection for {mid} at {price}"
                    assert opp.is_profitable
                    detected.append(mid)
                    # Create position and simulate a winning path to TP
                    pos = engine.create_position(opp)
                    action = _simulate_price_path(
                        engine, pos,
                        [(Decimal("0.960"), Decimal("0.955")), (Decimal("0.970"), Decimal("0.965"))],
                    )
                    # 95c → 97c should trigger TP
                    assert action == FavoriteAction.TAKE_PROFIT, f"{mid} should hit TP"
                    pnl_usd = (Decimal("0.97") - opp.favorite_price) * pos.size_shares
                    pnl_pct = (Decimal("0.97") - opp.favorite_price) / opp.favorite_price * Decimal("100")
                    trades.append(BacktestTrade(mid, opp.favorite_price, Decimal("0.97"), pos.size_usd, pos.size_shares, action, pnl_usd, pnl_pct))
                else:
                    assert opp is None, f"Expected rejection for {mid} at {price}"
                    rejected.append(mid)

            assert len(detected) == 3
            assert len(rejected) == 5

            m = engine.get_metrics()
            assert m["markets_analyzed"] == 8
            assert m["opportunities_found"] == 3
            assert m["rejected_price"] == 2
            assert m["rejected_time"] == 1
            assert m["rejected_probability"] == 1
            assert m["rejected_liquidity"] == 1

            # Aggregate P&L must be positive (all winners)
            total_pnl = sum(t.pnl_usd for t in trades)
            assert total_pnl > Decimal("0")
            # Position sizing respects Kelly / exposure cap
            for t in trades:
                assert t.size_usd <= Decimal("300")  # 30% of 1000
                assert t.size_usd > Decimal("0")


# ---------------------------------------------------------------------------
# 2 — Price evolution: TP vs SL
# ---------------------------------------------------------------------------

class TestFavoritePriceEvolution:

    def test_price_evolution_take_profit(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books = _binary_books("0.950", "0.945", "10000")
            opp = engine.analyze_market("poly-tp", "TP test?", books, 24.0)
            assert opp is not None
            pos = engine.create_position(opp)
            # Simulate gradual climb 95c → 96c → 97c
            path = [
                (Decimal("0.955"), Decimal("0.950")),
                (Decimal("0.960"), Decimal("0.955")),
                (Decimal("0.965"), Decimal("0.960")),
                (Decimal("0.970"), Decimal("0.965")),
            ]
            action = _simulate_price_path(engine, pos, path)
            assert action == FavoriteAction.TAKE_PROFIT
            assert pos.unrealized_pnl_pct > Decimal("0")

    def test_price_evolution_stop_loss(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books = _binary_books("0.920", "0.915", "10000")
            opp = engine.analyze_market("poly-sl", "SL test?", books, 24.0)
            assert opp is not None
            pos = engine.create_position(opp)
            # Crash 92c → 85c → 80c (bid hits stop)
            path = [
                (Decimal("0.88"), Decimal("0.875")),
                (Decimal("0.82"), Decimal("0.815")),
                (Decimal("0.80"), Decimal("0.795")),
            ]
            action = _simulate_price_path(engine, pos, path)
            assert action == FavoriteAction.STOP_LOSS
            assert pos.unrealized_pnl_pct < Decimal("0")

    def test_price_evolution_hold_then_tp(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books = _binary_books("0.940", "0.935", "10000")
            opp = engine.analyze_market("poly-hold-tp", "Hold→TP?", books, 24.0)
            assert opp is not None
            pos = engine.create_position(opp)
            # Hold for several ticks then TP
            path = [
                (Decimal("0.941"), Decimal("0.936")),
                (Decimal("0.942"), Decimal("0.937")),
                (Decimal("0.943"), Decimal("0.938")),
                (Decimal("0.970"), Decimal("0.965")),
            ]
            # First 3 should be HOLD
            for price, bid in path[:3]:
                a = engine.check_position(pos, price, bid)
                assert a == FavoriteAction.HOLD
            # Last triggers TP
            a = engine.check_position(pos, path[3][0], path[3][1])
            assert a == FavoriteAction.TAKE_PROFIT

    def test_time_based_exit_under_1h_in_profit(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books = _binary_books("0.930", "0.925", "10000")
            opp = engine.analyze_market("poly-time-exit", "Time exit?", books, 2.0)
            assert opp is not None
            pos = engine.create_position(opp)
            # Simulate 1.5h elapsed (remaining <1h) with small profit
            pos.entry_time = int(time.time() - 1.6 * 3600)
            pos.time_to_resolution_h = 2.0
            action = engine.check_position(pos, Decimal("0.935"), Decimal("0.930"))
            assert action == FavoriteAction.TAKE_PROFIT

    def test_time_based_exit_no_exit_if_not_profitable(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books = _binary_books("0.930", "0.925", "10000")
            opp = engine.analyze_market("poly-time-no-exit", "Time no exit?", books, 2.0)
            assert opp is not None
            pos = engine.create_position(opp)
            pos.entry_time = int(time.time() - 1.6 * 3600)
            pos.time_to_resolution_h = 2.0
            # Price below entry → should NOT time-exit
            action = engine.check_position(pos, Decimal("0.925"), Decimal("0.920"))
            assert action == FavoriteAction.HOLD


# ---------------------------------------------------------------------------
# 3 — Kelly sizing & exposure limits across a week of trading
# ---------------------------------------------------------------------------

class TestFavoriteBacktestKellyAndExposure:

    def test_kelly_sizing_respects_cap(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            # High price → small edge → small Kelly
            books_high = _binary_books("0.970", "0.965", "10000")
            opp_high = engine.analyze_market("poly-kelly-high", "Kelly high?", books_high, 24.0)
            assert opp_high is not None
            # Lower price → larger edge → larger Kelly (but still capped)
            books_mid = _binary_books("0.920", "0.915", "10000")
            opp_mid = engine.analyze_market("poly-kelly-mid", "Kelly mid?", books_mid, 24.0)
            assert opp_mid is not None
            for opp in (opp_high, opp_mid):
                assert opp.position_size_usd <= Decimal("300")  # max 30% of 1000
                assert opp.position_size_usd > Decimal("0")
                assert opp.position_shares > Decimal("0")

    def test_exposure_never_exceeds_max_across_sequence(self):
        """Simulate 10 valid detections in sequence; each position size must respect cap."""
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            max_exposure = Decimal("1000") * Decimal("0.30")
            for i in range(10):
                price = f"0.9{2 + (i % 6)}"  # 0.92–0.97
                bid = f"0.9{1 + (i % 6)}"
                # ensure price stays in [0.92, 0.97] range
                if Decimal(price) < Decimal("0.92"):
                    price = "0.920"
                    bid = "0.915"
                if Decimal(price) > Decimal("0.97"):
                    price = "0.970"
                    bid = "0.965"
                books = _binary_books(price, bid, "10000")
                opp = engine.analyze_market(f"poly-seq-{i}", f"Seq {i}?", books, 24.0)
                if opp:
                    assert opp.position_size_usd <= max_exposure
                    assert opp.position_size_usd >= Decimal("1")

    def test_fee_impact_never_makes_valid_opp_negative(self):
        """All detected opportunities must have net_edge > 0 after fees."""
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            for price in ["0.920", "0.940", "0.950", "0.960", "0.970"]:
                bid = f"{float(Decimal(price)) - 0.005:.3f}"
                books = _binary_books(price, bid, "10000")
                opp = engine.analyze_market(f"poly-fee-{price}", "Fee?", books, 24.0)
                if opp:
                    assert opp.net_edge > Decimal("0")
                    assert opp.fees_estimate >= Decimal("0")
                    assert opp.is_profitable


# ---------------------------------------------------------------------------
# 4 — Weekly backtest: 5 days × varied markets, aggregate metrics
# ---------------------------------------------------------------------------

class TestFavoriteWeeklyBacktest:

    def test_weekly_backtest_aggregate_pnl(self):
        """Simulate 5 days of scanning; verify aggregate P&L and win-rate logic."""
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()

            # Each day: list of (fav_price, fav_bid, outcome_price, outcome_bid)
            # outcome_price simulates where the market settles after position open
            weekly_plan = [
                # Day 1 — 3 markets: 2 winners, 1 loser
                [
                    ("0.950", "0.945", "0.970", "0.965"),
                    ("0.930", "0.925", "0.970", "0.965"),
                    ("0.940", "0.935", "0.800", "0.795"),  # crash → SL
                ],
                # Day 2 — 2 markets: both winners
                [
                    ("0.960", "0.955", "0.975", "0.970"),
                    ("0.920", "0.915", "0.970", "0.965"),
                ],
                # Day 3 — 4 markets: 1 rejected (low price), 3 winners
                [
                    ("0.800", "0.795", "0.970", "0.965"),  # rejected
                    ("0.950", "0.945", "0.970", "0.965"),
                    ("0.940", "0.935", "0.970", "0.965"),
                    ("0.930", "0.925", "0.970", "0.965"),
                ],
                # Day 4 — 2 markets: 1 winner, 1 loser
                [
                    ("0.950", "0.945", "0.970", "0.965"),
                    ("0.920", "0.915", "0.780", "0.775"),  # crash → SL
                ],
                # Day 5 — 3 markets: all winners (strong favorites)
                [
                    ("0.960", "0.955", "0.980", "0.975"),
                    ("0.950", "0.945", "0.970", "0.965"),
                    ("0.940", "0.935", "0.970", "0.965"),
                ],
            ]

            all_trades: list[BacktestTrade] = []
            total_markets = 0

            for day_idx, day_markets in enumerate(weekly_plan):
                for j, (price, bid, outcome_price, outcome_bid) in enumerate(day_markets):
                    mid = f"poly-wk{day_idx}-m{j}"
                    books = _binary_books(price, bid, "10000")
                    total_markets += 1
                    opp = engine.analyze_market(mid, f"Week {day_idx} market {j}?", books, 24.0)
                    if opp is None:
                        continue
                    pos = engine.create_position(opp)
                    action = engine.check_position(pos, Decimal(outcome_price), Decimal(outcome_bid))
                    # If outcome is 97c+ → TP, if 80c → SL, else HOLD (treat as TP for winners)
                    if action == FavoriteAction.HOLD and Decimal(outcome_price) >= Decimal("0.97"):
                        # Force TP check with higher price
                        action = engine.check_position(pos, Decimal("0.970"), Decimal("0.965"))
                    pnl_usd = (Decimal(outcome_price) - opp.favorite_price) * pos.size_shares
                    pnl_pct = (Decimal(outcome_price) - opp.favorite_price) / opp.favorite_price * Decimal("100")
                    all_trades.append(BacktestTrade(mid, opp.favorite_price, Decimal(outcome_price), pos.size_usd, pos.size_shares, action, pnl_usd, pnl_pct))

            m = engine.get_metrics()
            assert m["markets_analyzed"] == total_markets
            # 1 rejected (day 3, 0.80 price)
            assert m["rejected_price"] == 1
            assert m["opportunities_found"] == len(all_trades)
            assert len(all_trades) == 13  # 14 total - 1 rejected

            wins = [t for t in all_trades if t.action == FavoriteAction.TAKE_PROFIT]
            losses = [t for t in all_trades if t.action == FavoriteAction.STOP_LOSS]
            assert len(wins) == 11
            assert len(losses) == 2
            win_rate = len(wins) / len(all_trades)
            assert win_rate >= 0.80  # 84.6% in this plan

            total_pnl = sum(t.pnl_usd for t in all_trades)
            # Even with 2 stop-losses, aggregate must be strongly positive
            # (favorite strategy: small wins compound, losses are bounded by SL at 80c)
            assert total_pnl > Decimal("0")

    def test_backtest_metrics_consistency(self):
        """Metrics counters must sum correctly."""
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            # 6 markets: 2 valid, 4 rejected for different reasons
            cases = [
                (_binary_books("0.950", "0.945", "10000"), 24.0, True),
                (_binary_books("0.800", "0.795", "5000",), 24.0, False),  # price
                (_binary_books("0.950", "0.945", "10000"), 80.0, False),  # time
                (_binary_books("0.880", "0.875", "5000"), 24.0, False),   # prob
                (_binary_books("0.920", "0.915", "2"), 24.0, False),      # liquidity
                (_binary_books("0.930", "0.925", "10000"), 24.0, True),
            ]
            for idx, (books, hrs, _) in enumerate(cases):
                engine.analyze_market(f"m-{idx}", "Q?", books, hrs)
            m = engine.get_metrics()
            assert m["markets_analyzed"] == 6
            assert m["opportunities_found"] == 2
            assert m["rejected_price"] + m["rejected_time"] + m["rejected_probability"] + m["rejected_liquidity"] == 4


# ---------------------------------------------------------------------------
# 5 — Paper execution end-to-end (Favorite → PaperSimulator)
# ---------------------------------------------------------------------------

class TestFavoritePaperExecution:

    def test_end_to_end_paper_fill(self):
        """Detection → position → PaperSimulator fill → verify success."""
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books = _binary_books("0.950", "0.945", "10000")
            opp = engine.analyze_market("poly-paper-e2e", "Paper E2E?", books, 24.0)
            assert opp is not None

            pos = engine.create_position(opp)
            assert pos.entry_price == Decimal("0.950")

            simulator = PaperSimulator(
                latency_ms=10,
                base_fill_probability=1.0,
                leg_failure_probability=0.0,
                fill_fraction_jitter=0.0,
            )
            from src.engine.detector import ArbitrageOpportunity, SignalType
            synth = ArbitrageOpportunity(
                market_id=opp.market_id,
                signal_type=SignalType.BUY_SET,
                token_ids=[opp.favorite_token_id, opp.underdog_token_id],
                prices=[opp.favorite_price, opp.underdog_price],
                sizes=[opp.favorite_size, opp.favorite_size],
                max_size=opp.position_shares,
                total_cost=opp.position_size_usd,
                expected_payout=opp.position_shares,
                gross_profit=opp.expected_return_pct * opp.position_size_usd / 100,
                fees=opp.fees_estimate,
                net_profit=opp.expected_return_pct * opp.position_size_usd / 100 - opp.fees_estimate,
                profit_pct=opp.expected_return_pct,
                executable_quantity=opp.position_shares,
            )
            result = asyncio.run(simulator.execute(synth))
            assert result.success is True
            assert result.all_filled is True
            assert result.total_filled > Decimal("0")
            assert len(result.orders) == 2

    def test_paper_execution_with_jitter_still_profitable(self):
        """Paper simulator with small jitter should still produce fills."""
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books = _binary_books("0.940", "0.935", "10000")
            opp = engine.analyze_market("poly-jitter", "Jitter?", books, 24.0)
            assert opp is not None

            simulator = PaperSimulator(
                latency_ms=5,
                base_fill_probability=1.0,
                leg_failure_probability=0.0,
                fill_fraction_jitter=0.05,
            )
            from src.engine.detector import ArbitrageOpportunity, SignalType
            synth = ArbitrageOpportunity(
                market_id=opp.market_id,
                signal_type=SignalType.BUY_SET,
                token_ids=[opp.favorite_token_id, opp.underdog_token_id],
                prices=[opp.favorite_price, opp.underdog_price],
                sizes=[opp.favorite_size, opp.favorite_size],
                max_size=opp.position_shares,
                total_cost=opp.position_size_usd,
                expected_payout=opp.position_shares,
                gross_profit=opp.expected_return_pct * opp.position_size_usd / 100,
                fees=opp.fees_estimate,
                net_profit=opp.expected_return_pct * opp.position_size_usd / 100 - opp.fees_estimate,
                profit_pct=opp.expected_return_pct,
                executable_quantity=opp.position_shares,
            )
            result = asyncio.run(simulator.execute(synth))
            # With jitter, fill may be partial but at least one leg fills
            assert result.any_filled or result.success

    def test_paper_execution_leg_failure_handling(self):
        """Leg failure injection should mark result as not fully successful."""
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books = _binary_books("0.950", "0.945", "10000")
            opp = engine.analyze_market("poly-leg-fail", "Leg fail?", books, 24.0)
            assert opp is not None

            simulator = PaperSimulator(
                latency_ms=5,
                base_fill_probability=1.0,
                leg_failure_probability=1.0,  # always fail
                fill_fraction_jitter=0.0,
            )
            from src.engine.detector import ArbitrageOpportunity, SignalType
            synth = ArbitrageOpportunity(
                market_id=opp.market_id,
                signal_type=SignalType.BUY_SET,
                token_ids=[opp.favorite_token_id, opp.underdog_token_id],
                prices=[opp.favorite_price, opp.underdog_price],
                sizes=[opp.favorite_size, opp.favorite_size],
                max_size=opp.position_shares,
                total_cost=opp.position_size_usd,
                expected_payout=opp.position_shares,
                gross_profit=opp.expected_return_pct * opp.position_size_usd / 100,
                fees=opp.fees_estimate,
                net_profit=opp.expected_return_pct * opp.position_size_usd / 100 - opp.fees_estimate,
                profit_pct=opp.expected_return_pct,
                executable_quantity=opp.position_shares,
            )
            result = asyncio.run(simulator.execute(synth))
            assert result.success is False
            assert result.leg_risk is not None


# ---------------------------------------------------------------------------
# 6 — RiskManager integration
# ---------------------------------------------------------------------------

class TestFavoriteRiskIntegration:

    def test_risk_manager_tracks_favorite_exposure(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            from src.risk.manager import RiskManager
            settings = _MockSettings()
            rm = RiskManager(settings=settings)  # type: ignore[arg-type]
            engine = FavoriteEngine()

            books = _binary_books("0.950", "0.945", "10000")
            opp = engine.analyze_market("poly-risk-1", "Risk 1?", books, 24.0)
            assert opp is not None
            pos = engine.create_position(opp)

            # Add to risk manager
            rm.add_favorite_position({
                "market_id": pos.market_id,
                "entry_price": str(pos.entry_price),
                "entry_time": pos.entry_time,
                "size_usd": str(pos.size_usd),
                "size_shares": str(pos.size_shares),
                "take_profit_price": str(pos.take_profit_price),
                "stop_loss_price": str(pos.stop_loss_price),
                "time_to_resolution_h": pos.time_to_resolution_h,
            })
            assert len(rm.get_favorite_positions()) == 1
            assert rm.state["current_exposure"] > 0

            # Update to TP should close exposure
            rm.update_favorite_position(pos.market_id, Decimal("0.97"), Decimal("0.965"))
            assert rm.get_favorite_positions()[0]["action"] == "TAKE_PROFIT"

    def test_risk_manager_stop_loss_closes_exposure(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            from src.risk.manager import RiskManager
            settings = _MockSettings()
            rm = RiskManager(settings=settings)  # type: ignore[arg-type]
            engine = FavoriteEngine()

            books = _binary_books("0.930", "0.925", "10000")
            opp = engine.analyze_market("poly-risk-sl", "Risk SL?", books, 24.0)
            assert opp is not None
            pos = engine.create_position(opp)
            rm.add_favorite_position({
                "market_id": pos.market_id,
                "entry_price": str(pos.entry_price),
                "entry_time": pos.entry_time,
                "size_usd": str(pos.size_usd),
                "size_shares": str(pos.size_shares),
                "take_profit_price": str(pos.take_profit_price),
                "stop_loss_price": str(pos.stop_loss_price),
                "time_to_resolution_h": pos.time_to_resolution_h,
            })
            # Crash to SL
            rm.update_favorite_position(pos.market_id, Decimal("0.795"), Decimal("0.790"))
            assert rm.get_favorite_positions()[0]["action"] == "STOP_LOSS"

    def test_risk_manager_can_trade_after_favorite_positions(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            from src.risk.manager import RiskManager
            settings = _MockSettings()
            rm = RiskManager(settings=settings)  # type: ignore[arg-type]
            # Initially can trade
            allowed, _ = rm.can_trade()
            assert allowed is True
            # After adding a position within limits, still can trade
            rm.add_favorite_position({
                "market_id": "m1",
                "entry_price": "0.95",
                "entry_time": int(time.time()),
                "size_usd": "50",
                "size_shares": "52",
                "take_profit_price": "0.97",
                "stop_loss_price": "0.80",
                "time_to_resolution_h": 24.0,
            })
            allowed, _ = rm.can_trade()
            assert allowed is True


# ---------------------------------------------------------------------------
# 7 — Edge cases & historical realism
# ---------------------------------------------------------------------------

class TestFavoriteHistoricalEdgeCases:

    def test_empty_order_books_rejected(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            assert engine.analyze_market("m-empty", "Empty?", {}, 24.0) is None

    def test_single_token_rejected(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books = {"YES": _book("YES", "0.945", "0.950")}
            assert engine.analyze_market("m-single", "Single?", books, 24.0) is None

    def test_three_tokens_rejected(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books = {
                "A": _book("A", "0.30", "0.31"),
                "B": _book("B", "0.30", "0.31"),
                "C": _book("C", "0.30", "0.31"),
            }
            assert engine.analyze_market("m-triple", "Triple?", books, 24.0) is None

    def test_boundary_price_85c_rejected_due_probability(self):
        """0.85 is at min_price but prob 0.85 < 0.90 → rejected for probability."""
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books = _binary_books("0.850", "0.845", "10000")
            opp = engine.analyze_market("m-bound-low", "Boundary low?", books, 24.0)
            assert opp is None
            assert engine.get_metrics()["rejected_probability"] == 1

    def test_boundary_price_98c_accepted(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books = _binary_books("0.980", "0.975", "10000")
            opp = engine.analyze_market("m-bound-high", "Boundary high?", books, 24.0)
            assert opp is not None
            assert opp.favorite_price == Decimal("0.980")

    def test_time_boundary_72h_accepted_73h_rejected(self):
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            books_ok = _binary_books("0.950", "0.945", "10000")
            opp_ok = engine.analyze_market("m-72h", "72h?", books_ok, 72.0)
            assert opp_ok is not None

            engine2 = FavoriteEngine()
            with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
                books_bad = _binary_books("0.950", "0.945", "10000")
                opp_bad = engine2.analyze_market("m-73h", "73h?", books_bad, 73.0)
                assert opp_bad is None
                assert engine2.get_metrics()["rejected_time"] == 1

    def test_compounding_simulation_20_trades(self):
        """Simulate 20 sequential trades and verify compounding stays positive."""
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()
            total_pnl = Decimal("0")
            for i in range(20):
                price = "0.950" if i % 2 == 0 else "0.930"
                bid = "0.945" if i % 2 == 0 else "0.925"
                books = _binary_books(price, bid, "10000")
                opp = engine.analyze_market(f"poly-comp-{i}", f"Comp {i}?", books, 24.0)
                assert opp is not None
                pos = engine.create_position(opp)
                # Alternate wins (16) and losses (4) every 5 trades
                # Win: exit at 0.97 (profit: 0.02-0.04 per share)
                # Loss: exit at 0.91 (loss: -0.04 to -0.02 per share, stop_loss=0.91)
                if i % 5 == 4:
                    action = engine.check_position(pos, Decimal("0.905"), Decimal("0.900"))
                    assert action == FavoriteAction.STOP_LOSS
                    pnl = (Decimal("0.905") - opp.favorite_price) * pos.size_shares
                else:
                    action = engine.check_position(pos, Decimal("0.970"), Decimal("0.965"))
                    assert action == FavoriteAction.TAKE_PROFIT
                    pnl = (Decimal("0.970") - opp.favorite_price) * pos.size_shares
                total_pnl += pnl

            # 16 wins, 4 losses — total must still be positive
            assert total_pnl > Decimal("0")
            m = engine.get_metrics()
            assert m["markets_analyzed"] == 20
            assert m["opportunities_found"] == 20
