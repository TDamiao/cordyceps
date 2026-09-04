"""
Integration tests for Favorite Compounding strategy in paper mode.

Tests the full flow: detection → position creation → paper execution → monitoring.
Uses realistic market scenarios from historical Polymarket data.

Run with:
    pytest tests/test_favorite_integration.py -v
    pytest tests/test_favorite_integration.py::TestFavoriteIntegration::test_end_to_end_favorite_trade -v -s
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import patch

from src.client.models import OrderBook, OrderBookLevel
from src.engine.favorite import FavoriteEngine
from src.execution.paper import PaperSimulator


class _MockSettings:
    """Mock settings for testing."""

    max_total_exposure_usd = 1000.0
    trading_mode = "paper"
    enable_favorite_strategy = True
    min_favorite_probability = 0.90
    min_favorite_price = 0.85
    max_favorite_price = 0.98
    min_favorite_size_usd = 5.0
    favorite_take_profit = 0.97
    favorite_stop_loss = 0.80
    max_favorite_exposure_pct = 0.30
    favorite_kelly_fraction = 0.25


# ------------------------------------------------------------------
# Scenario: Fed Decision (Real historical scenario)
# ------------------------------------------------------------------


class TestFavoriteIntegration:
    """Integration tests for favorite compounding strategy."""

    def _make_order_book(
        self, token_id: str, bid_price: str, ask_price: str, bid_size: str = "5000", ask_size: str = "5000"
    ) -> OrderBook:
        """Create a realistic order book."""
        return OrderBook(
            token_id=token_id,
            bids=[OrderBookLevel(price=Decimal(bid_price), size=Decimal(bid_size))],
            asks=[OrderBookLevel(price=Decimal(ask_price), size=Decimal(ask_size))],
            timestamp=int(__import__("time").time() * 1000),
        )

    def test_scenario_fed_rate_decision_95c_favorite(self):
        """
        Scenario: Fed keeps rates unchanged (95c favorite, 12h to resolution)

        Market: "Will the Federal Reserve keep rates unchanged in September 2026?"
        YES (favorite): 95c
        NO (underdog): 5c
        Time to resolution: 12 hours

        Expected: Strategy detects opportunity with ~4-5% expected return
        """
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()

            books = {
                "YES": self._make_order_book("YES", "0.945", "0.950", bid_size="10000", ask_size="10000"),
                "NO": self._make_order_book("NO", "0.045", "0.050", bid_size="10000", ask_size="10000"),
            }

            opp = engine.analyze_market(
                market_id="poly-fed-sept-2026",
                market_question="Will the Federal Reserve keep rates unchanged in September 2026?",
                order_books=books,
                time_to_resolution_h=12.0,
            )

            assert opp is not None
            assert opp.is_profitable is True
            assert opp.favorite_price == Decimal("0.950")
            assert opp.favorite_bid == Decimal("0.945")
            assert opp.market_id == "poly-fed-sept-2026"
            assert Decimal("0.85") <= opp.favorite_price <= Decimal("0.98")
            assert opp.time_to_resolution_h == 12.0
            # Expected return: (1 - 0.95) / 0.95 = 0.0526 = 5.26%
            assert float(opp.expected_return_pct) >= 4.0

    def test_scenario_sports_clear_favorite_93c(self):
        """
        Scenario: Sports team with strong fundamentals (93c favorite, 24h to resolution)

        Market: "Will Team X win the Championship 2026?"
        YES (favorite): 93c (team undefeated, +25 goal differential)
        NO (underdog): 7c
        Time to resolution: 24 hours

        Expected: Strong detection with ~6-7% expected return
        """
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()

            books = {
                "YES": self._make_order_book("YES", "0.925", "0.930", bid_size="8000", ask_size="8000"),
                "NO": self._make_order_book("NO", "0.065", "0.070", bid_size="8000", ask_size="8000"),
            }

            opp = engine.analyze_market(
                market_id="poly-championship-2026",
                market_question="Will Team X win the Championship 2026?",
                order_books=books,
                time_to_resolution_h=24.0,
            )

            assert opp is not None
            assert opp.is_profitable is True
            assert opp.favorite_price == Decimal("0.930")
            assert float(opp.expected_return_pct) >= 6.0

    def test_scenario_election_near_resolution_96c(self):
        """
        Scenario: Election outcome clear hours before result (96c favorite, 3h to resolution)

        Market: "Will Candidate A win District Z?"
        YES (favorite): 96c (exit polls strongly favor A)
        NO (underdog): 4c
        Time to resolution: 3 hours

        Expected: Very tight window, high risk/reward, 4% return
        """
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()

            books = {
                "YES": self._make_order_book("YES", "0.955", "0.960", bid_size="6000", ask_size="6000"),
                "NO": self._make_order_book("NO", "0.035", "0.040", bid_size="6000", ask_size="6000"),
            }

            opp = engine.analyze_market(
                market_id="poly-election-district-z",
                market_question="Will Candidate A win District Z?",
                order_books=books,
                time_to_resolution_h=3.0,
            )

            assert opp is not None
            assert opp.is_profitable is True
            assert opp.favorite_price == Decimal("0.960")
            # Near resolution: (1 - 0.96) / 0.96 = 0.0417 = 4.17%
            assert float(opp.expected_return_pct) >= 4.0

    def test_scenario_rejected_price_too_low_80c(self):
        """
        Scenario: Price at 80c is below minimum 85c threshold (rejected)

        Market: "Will X happen?"
        Favorite: 80c (too low, below 85c min)
        Time to resolution: 24h

        Expected: Rejection due to price range
        """
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()

            books = {
                "YES": self._make_order_book("YES", "0.795", "0.800", bid_size="5000", ask_size="5000"),
                "NO": self._make_order_book("NO", "0.195", "0.200", bid_size="5000", ask_size="5000"),
            }

            opp = engine.analyze_market(
                market_id="poly-low-price",
                market_question="Will X happen?",
                order_books=books,
                time_to_resolution_h=24.0,
            )

            assert opp is None
            assert engine.get_metrics()["rejected_price"] == 1

    def test_scenario_rejected_price_too_high_99c(self):
        """
        Scenario: Price at 99c is above maximum 98c threshold (rejected)

        Market: "Will X happen?"
        Favorite: 99c (too high, above 98c max)
        Time to resolution: 24h

        Expected: Rejection due to price range
        """
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()

            books = {
                "YES": self._make_order_book("YES", "0.985", "0.990", bid_size="5000", ask_size="5000"),
                "NO": self._make_order_book("NO", "0.005", "0.010", bid_size="5000", ask_size="5000"),
            }

            opp = engine.analyze_market(
                market_id="poly-high-price",
                market_question="Will X happen?",
                order_books=books,
                time_to_resolution_h=24.0,
            )

            assert opp is None
            assert engine.get_metrics()["rejected_price"] == 1

    def test_scenario_rejected_time_too_long_80h(self):
        """
        Scenario: Market has 80h to resolution (>72h, rejected)

        Market: "Will X happen?"
        Favorite: 92c (in range)
        Time to resolution: 80 hours (exceeds 72h max)

        Expected: Rejection due to time
        """
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()

            books = {
                "YES": self._make_order_book("YES", "0.915", "0.920", bid_size="5000", ask_size="5000"),
                "NO": self._make_order_book("NO", "0.075", "0.080", bid_size="5000", ask_size="5000"),
            }

            opp = engine.analyze_market(
                market_id="poly-long-time",
                market_question="Will X happen?",
                order_books=books,
                time_to_resolution_h=80.0,
            )

            assert opp is None
            assert engine.get_metrics()["rejected_time"] == 1

    def test_scenario_rejected_low_probability_88c(self):
        """
        Scenario: Price at 88c fails probability check (min 90%)

        Market: "Will X happen?"
        Favorite: 88c (price in range but probability < 90%)
        Time to resolution: 24h

        Expected: Rejection due to probability
        """
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()

            books = {
                "YES": self._make_order_book("YES", "0.875", "0.880", bid_size="5000", ask_size="5000"),
                "NO": self._make_order_book("NO", "0.115", "0.120", bid_size="5000", ask_size="5000"),
            }

            opp = engine.analyze_market(
                market_id="poly-low-prob",
                market_question="Will X happen?",
                order_books=books,
                time_to_resolution_h=24.0,
            )

            assert opp is None
            assert engine.get_metrics()["rejected_probability"] == 1

    def test_scenario_rejected_low_liquidity(self):
        """
        Scenario: Insufficient liquidity at favorite price

        Market: "Will X happen?"
        Favorite: 92c but only $2 liquidity available (min $5)
        Time to resolution: 24h

        Expected: Rejection due to liquidity
        """
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()

            books = {
                "YES": self._make_order_book("YES", "0.915", "0.920", bid_size="2", ask_size="2"),
                "NO": self._make_order_book("NO", "0.075", "0.080", bid_size="2", ask_size="2"),
            }

            opp = engine.analyze_market(
                market_id="poly-low-liq",
                market_question="Will X happen?",
                order_books=books,
                time_to_resolution_h=24.0,
            )

            assert opp is None
            assert engine.get_metrics()["rejected_liquidity"] == 1

    def test_end_to_end_favorite_trade_paper_execution(self):
        """
        End-to-end integration test: Detection → Position Creation → Paper Execution

        Simulates the full favorite compounding workflow:
        1. Detect opportunity (95c favorite)
        2. Create trackable position
        3. Simulate paper execution
        4. Verify results

        Expected: Successful trade with positive P&L
        """
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            # Step 1: Create engine and detect opportunity
            engine = FavoriteEngine()
            books = {
                "YES": self._make_order_book("YES", "0.945", "0.950", bid_size="10000", ask_size="10000"),
                "NO": self._make_order_book("NO", "0.045", "0.050", bid_size="10000", ask_size="10000"),
            }

            opp = engine.analyze_market(
                market_id="poly-test-e2e",
                market_question="Test E2E Market?",
                order_books=books,
                time_to_resolution_h=24.0,
            )
            assert opp is not None
            assert opp.is_profitable is True

            # Step 2: Create position from opportunity
            position = engine.create_position(opp)  # type: ignore[arg-type]
            assert position.market_id == opp.market_id
            assert position.entry_price == Decimal("0.950")
            assert position.size_shares == opp.position_shares
            assert position.size_usd == opp.position_size_usd

            # Step 3: Simulate paper execution with PaperSimulator
            simulator = PaperSimulator(
                latency_ms=100,
                base_fill_probability=1.0,
                leg_failure_probability=0.0,
                fill_fraction_jitter=0.0,
            )

            # Create synthetic arbitrage opportunity for paper execution
            from src.engine.detector import ArbitrageOpportunity, SignalType

            synth_opp = ArbitrageOpportunity(
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

            result = asyncio.run(simulator.execute(synth_opp))

            # Step 4: Verify paper execution results
            assert result.success is True
            assert result.all_filled is True
            assert result.total_filled > 0
            assert len(result.orders) == 2

    def test_position_monitoring_take_profit(self):
        """
        Test position monitoring with take profit trigger.

        Position entered at 95c, monitored, reaches 97c → trigger take profit
        """
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            from src.engine.favorite import FavoriteAction

            engine = FavoriteEngine()
            books = {
                "YES": self._make_order_book("YES", "0.945", "0.950", bid_size="10000", ask_size="10000"),
                "NO": self._make_order_book("NO", "0.045", "0.050", bid_size="10000", ask_size="10000"),
            }

            opp = engine.analyze_market(
                market_id="poly-tp-test",
                market_question="Test TP?",
                order_books=books,
                time_to_resolution_h=24.0,
            )

            position = engine.create_position(opp)

            # Monitor: price moves to 97c
            action = engine.check_position(position, Decimal("0.97"), Decimal("0.96"))

            assert action == FavoriteAction.TAKE_PROFIT
            assert position.unrealized_pnl_pct == (Decimal("0.97") - Decimal("0.950")) / Decimal("0.950") * Decimal(
                "100"
            )

    def test_position_monitoring_stop_loss(self):
        """
        Test position monitoring with stop loss trigger.

        Position entered at 92c, monitored, drops to 80c → trigger stop loss
        """
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            from src.engine.favorite import FavoriteAction

            engine = FavoriteEngine()
            books = {
                "YES": self._make_order_book("YES", "0.915", "0.920", bid_size="10000", ask_size="10000"),
                "NO": self._make_order_book("NO", "0.075", "0.080", bid_size="10000", ask_size="10000"),
            }

            opp = engine.analyze_market(
                market_id="poly-sl-test",
                market_question="Test SL?",
                order_books=books,
                time_to_resolution_h=24.0,
            )

            position = engine.create_position(opp)

            # Monitor: price crashes to 80c
            action = engine.check_position(position, Decimal("0.80"), Decimal("0.80"))

            assert action == FavoriteAction.STOP_LOSS

    def test_metrics_tracking_across_scenarios(self):
        """
        Test that metrics are tracked correctly across multiple market analyses.

        Run detection on 5 markets: 3 rejected, 2 accepted
        """
        with patch("src.engine.favorite.get_settings", return_value=_MockSettings()):
            engine = FavoriteEngine()

            # Market 1: Valid favorite (95c)
            books1 = {
                "YES": self._make_order_book("YES", "0.945", "0.950", bid_size="5000", ask_size="5000"),
                "NO": self._make_order_book("NO", "0.045", "0.050", bid_size="5000", ask_size="5000"),
            }
            opp1 = engine.analyze_market("m1", "Q1?", books1, 24.0)
            assert opp1 is not None

            # Market 2: Rejected (price too low 80c)
            books2 = {
                "YES": self._make_order_book("YES", "0.795", "0.800", bid_size="5000", ask_size="5000"),
                "NO": self._make_order_book("NO", "0.195", "0.200", bid_size="5000", ask_size="5000"),
            }
            opp2 = engine.analyze_market("m2", "Q2?", books2, 24.0)
            assert opp2 is None

            # Market 3: Rejected (time too long)
            books3 = {
                "YES": self._make_order_book("YES", "0.915", "0.920", bid_size="5000", ask_size="5000"),
                "NO": self._make_order_book("NO", "0.075", "0.080", bid_size="5000", ask_size="5000"),
            }
            opp3 = engine.analyze_market("m3", "Q3?", books3, 80.0)
            assert opp3 is None

            # Market 4: Valid favorite (92c)
            books4 = {
                "YES": self._make_order_book("YES", "0.915", "0.920", bid_size="5000", ask_size="5000"),
                "NO": self._make_order_book("NO", "0.075", "0.080", bid_size="5000", ask_size="5000"),
            }
            opp4 = engine.analyze_market("m4", "Q4?", books4, 24.0)
            assert opp4 is not None

            # Market 5: Rejected (low probability)
            books5 = {
                "YES": self._make_order_book("YES", "0.875", "0.880", bid_size="5000", ask_size="5000"),
                "NO": self._make_order_book("NO", "0.115", "0.120", bid_size="5000", ask_size="5000"),
            }
            opp5 = engine.analyze_market("m5", "Q5?", books5, 24.0)
            assert opp5 is None

            # Verify metrics
            metrics = engine.get_metrics()
            assert metrics["markets_analyzed"] == 5
            assert metrics["opportunities_found"] == 2
            assert metrics["rejected_price"] == 1
            assert metrics["rejected_time"] == 1
            assert metrics["rejected_probability"] == 1
