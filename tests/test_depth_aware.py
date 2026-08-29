"""
Tests for the depth-aware arbitrage detector and paper simulator.

Run with: pytest tests/test_depth_aware.py -v
"""

import asyncio
import json
import time
from decimal import Decimal

import pytest

from src.client.models import OrderBook, OrderBookLevel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_book(token_id, bids=None, asks=None, timestamp=None) -> OrderBook:
    """Build an OrderBook from [(price, size), ...] level lists."""
    return OrderBook(
        token_id=token_id,
        bids=[OrderBookLevel(Decimal(p), Decimal(s)) for p, s in (bids or [])],
        asks=[OrderBookLevel(Decimal(p), Decimal(s)) for p, s in (asks or [])],
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# Depth-aware detector
# ---------------------------------------------------------------------------

class TestDepthAwareConfig:
    def test_defaults(self):
        from src.engine.detector import ArbitrageConfig

        cfg = ArbitrageConfig()
        assert cfg.max_slippage_pct == Decimal("0.005")
        assert cfg.orderbook_stale_ms == 3000
        assert cfg.leg_risk_buffer == Decimal("0.0")

    def test_custom(self):
        from src.engine.detector import ArbitrageConfig

        cfg = ArbitrageConfig(
            max_slippage_pct=Decimal("0.01"),
            orderbook_stale_ms=1500,
            leg_risk_buffer=Decimal("0.02"),
        )
        assert cfg.max_slippage_pct == Decimal("0.01")
        assert cfg.orderbook_stale_ms == 1500
        assert cfg.leg_risk_buffer == Decimal("0.02")


class TestOpportunityDepthFields:
    def test_opportunity_exposes_depth_attributes(self):
        from src.engine.detector import ArbitrageOpportunity, SignalType

        opp = ArbitrageOpportunity(
            market_id="m",
            signal_type=SignalType.BUY_SET,
            token_ids=["a", "b"],
            prices=[Decimal("0.40"), Decimal("0.40")],
            vwap_prices=[Decimal("0.40"), Decimal("0.40")],
            sizes=[Decimal("10"), Decimal("10")],
            max_size=Decimal("10"),
            executable_quantity=Decimal("10"),
            total_cost=Decimal("8"),
            expected_payout=Decimal("10"),
            gross_profit=Decimal("2"),
            fees=Decimal("0"),
            net_profit=Decimal("2"),
            profit_pct=Decimal("0.25"),
            edge=Decimal("0.20"),
            roi=Decimal("0.25"),
        )
        assert opp.edge == Decimal("0.20")
        assert opp.roi == Decimal("0.25")
        assert opp.vwap_prices == [Decimal("0.40"), Decimal("0.40")]
        assert opp.executable_quantity == Decimal("10")
        assert opp.is_profitable


class TestBuySetDepthAnalysis:
    def _engine(self, **overrides):
        from src.engine.detector import ArbitrageConfig, ArbitrageEngine

        base = dict(
            taker_fee=Decimal("0"),
            min_profit_threshold=Decimal("0.001"),
            min_liquidity=Decimal("1"),
            max_position_size=Decimal("200"),
            max_slippage_pct=Decimal("0"),
        )
        base.update(overrides)
        return ArbitrageEngine(ArbitrageConfig(**base))

    def test_vwap_asks_and_depth_limited_size(self):
        """Buy depth: limited by unprofitable depth, VWAP reflects filled levels."""
        engine = self._engine()
        order_books = {
            "yes": make_book("yes", asks=[("0.40", "10"), ("0.80", "1000")]),
            "no": make_book("no", asks=[("0.40", "10"), ("0.80", "1000")]),
        }
        opp = engine.analyze_market("m-buy", order_books)
        assert opp is not None
        assert opp.max_size == Decimal("10")
        assert opp.executable_quantity == Decimal("10")
        assert opp.vwap_prices == [Decimal("0.40"), Decimal("0.40")]
        # edge = (1 - sum_vwap) = 0.20
        assert opp.edge == Decimal("0.20")
        assert opp.roi > Decimal("0")
        assert opp.net_profit == opp.gross_profit  # no fees

    def test_deeper_levels_increase_vwap_and_size(self):
        """Walking two levels: quantity rises, VWAP worsens but stays profitable."""
        engine = self._engine()
        order_books = {
            "yes": make_book("yes", asks=[("0.40", "10"), ("0.46", "20")]),
            "no": make_book("no", asks=[("0.40", "10"), ("0.46", "20")]),
        }
        opp = engine.analyze_market("m-buy2", order_books)
        assert opp is not None
        # Both legs consistent depth -> 10 + 20 = 30
        assert opp.max_size == Decimal("30")
        # VWAP = (10*0.40 + 20*0.46) / 30 = 13.2/30 = 0.44
        assert opp.vwap_prices == [Decimal("0.44"), Decimal("0.44")]
        # edge = 1 - 0.88 = 0.12
        assert opp.edge == Decimal("0.12")

    def test_stale_book_rejected(self):
        engine = self._engine(orderbook_stale_ms=1000)
        now_ms = int(time.time() * 1000)
        order_books = {
            "yes": make_book("yes", asks=[("0.45", "100")], timestamp=now_ms - 5000),
            "no": make_book("no", asks=[("0.45", "100")], timestamp=now_ms),
        }
        assert engine.analyze_market("m-stale", order_books) is None
        assert engine.stats["stale_books_rejected"] == 1

    def test_slippage_threshold_blocks_deep_unprofitable_depths(self):
        """
        If committed VWAP sum slips past 1.0 (no edge left), detection stops,
        keeping only profitable quantity.
        """
        engine = self._engine(max_position_size=Decimal("1000"))
        order_books = {
            "yes": make_book("yes", asks=[("0.40", "10"), ("0.90", "1000")]),
            "no": make_book("no", asks=[("0.40", "10"), ("0.90", "1000")]),
        }
        opp = engine.analyze_market("m-slip", order_books)
        assert opp is not None
        assert opp.max_size == Decimal("10")
        assert opp.edge == Decimal("0.20")

    def test_no_opportunity_when_best_ask_sum_ge_edge_minus_buffer(self):
        engine = self._engine(min_profit_threshold=Decimal("0"))
        order_books = {
            "yes": make_book("yes", asks=[("0.50", "100")]),
            "no": make_book("no", asks=[("0.50", "100")]),
        }
        assert engine.analyze_market("m-fair", order_books) is None


class TestSellSetDepthAnalysis:
    def _engine(self, **overrides):
        from src.engine.detector import ArbitrageConfig, ArbitrageEngine

        base = dict(
            taker_fee=Decimal("0"),
            min_profit_threshold=Decimal("0.001"),
            min_liquidity=Decimal("1"),
            max_position_size=Decimal("200"),
            max_slippage_pct=Decimal("0"),
        )
        base.update(overrides)
        return ArbitrageEngine(ArbitrageConfig(**base))

    def test_vwap_bids_and_depth_limited_size(self):
        engine = self._engine()
        # level-0 gives a premium (0.60+0.60=1.20 => edge 0.20); walking deeper
        # adds size at 0.50, pulling VWAP down toward 0.505.
        order_books = {
            "yes": make_book("yes", bids=[("0.60", "5"), ("0.50", "95")]),
            "no": make_book("no", bids=[("0.60", "5"), ("0.50", "95")]),
        }
        opp = engine.analyze_market("m-sell", order_books)
        assert opp is not None
        # Engine walks both levels: 5+95=100 total. VWAP = (5*0.60+95*0.50)/100 = 0.505
        assert opp.max_size == Decimal("100")
        assert opp.vwap_prices == [Decimal("0.505"), Decimal("0.505")]
        # sell edge = sum(vwap_bid) - 1 = 1.01 - 1 = 0.01
        assert opp.edge == Decimal("0.01")
        assert opp.net_profit > Decimal("0")

    def test_sell_slippage_blocks_premium_dried_up(self):
        engine = self._engine(max_position_size=Decimal("1000"))
        order_books = {
            "yes": make_book("yes", bids=[("0.60", "5"), ("0.10", "1000")]),
            "no": make_book("no", bids=[("0.60", "5"), ("0.10", "1000")]),
        }
        opp = engine.analyze_market("m-sell-slip", order_books)
        assert opp is not None
        assert opp.max_size == Decimal("5")
        assert opp.edge == Decimal("0.20")


class TestLegRisk:
    def test_leg_risk_buffer_filters_thin_edge(self):
        from src.engine.detector import ArbitrageConfig, ArbitrageEngine

        # edge = 0.06, buffer = 0.06 -> not > buffer, rejected
        engine = ArbitrageEngine(ArbitrageConfig(
            taker_fee=Decimal("0"),
            min_profit_threshold=Decimal("0"),
            min_liquidity=Decimal("1"),
            max_slippage_pct=Decimal("0"),
            leg_risk_buffer=Decimal("0.06"),
        ))
        order_books = {
            "yes": make_book("yes", asks=[("0.47", "100")]),
            "no": make_book("no", asks=[("0.47", "100")]),
        }
        assert engine.analyze_market("m-risk", order_books) is None

    def test_leg_risk_buffer_allows_larger_edge(self):
        from src.engine.detector import ArbitrageConfig, ArbitrageEngine

        # edge = 0.10 > buffer = 0.06 -> allowed
        engine = ArbitrageEngine(ArbitrageConfig(
            taker_fee=Decimal("0"),
            min_profit_threshold=Decimal("0"),
            min_liquidity=Decimal("1"),
            max_slippage_pct=Decimal("0"),
            leg_risk_buffer=Decimal("0.06"),
        ))
        order_books = {
            "yes": make_book("yes", asks=[("0.45", "100")]),
            "no": make_book("no", asks=[("0.45", "100")]),
        }
        opp = engine.analyze_market("m-ok", order_books)
        assert opp is not None

    def test_fees_applied_per_leg(self):
        from src.engine.detector import ArbitrageConfig, ArbitrageEngine

        engine = ArbitrageEngine(ArbitrageConfig(
            taker_fee=Decimal("0.01"),
            min_profit_threshold=Decimal("0"),
            min_liquidity=Decimal("1"),
            max_position_size=Decimal("100"),
            max_slippage_pct=Decimal("0"),
        ))
        order_books = {
            "yes": make_book("yes", asks=[("0.45", "100")]),
            "no": make_book("no", asks=[("0.45", "100")]),
        }
        opp = engine.analyze_market("m-fee", order_books)
        assert opp is not None
        assert opp.fees > Decimal("0")
        assert opp.net_profit < opp.gross_profit
        # roi is net-based
        assert opp.roi == opp.net_profit / opp.total_cost


# ---------------------------------------------------------------------------
# Paper simulator
# ---------------------------------------------------------------------------

class TestPaperSimulatorHappyPath:
    @pytest.mark.asyncio
    async def test_full_fill_success(self, tmp_path):
        from src.engine.detector import ArbitrageOpportunity, SignalType
        from src.execution.paper import PaperSimulator

        opp = ArbitrageOpportunity(
            market_id="m-paper",
            signal_type=SignalType.BUY_SET,
            token_ids=["t1", "t2"],
            prices=[Decimal("0.40"), Decimal("0.40")],
            vwap_prices=[Decimal("0.40"), Decimal("0.40")],
            sizes=[Decimal("100"), Decimal("100")],
            max_size=Decimal("100"),
            executable_quantity=Decimal("100"),
            total_cost=Decimal("80"),
            expected_payout=Decimal("100"),
            gross_profit=Decimal("20"),
            fees=Decimal("0"),
            net_profit=Decimal("20"),
            profit_pct=Decimal("0.25"),
            edge=Decimal("0.20"),
            roi=Decimal("0.25"),
        )
        sim = PaperSimulator(
            latency_ms=0,
            base_fill_probability=1.0,
            leg_failure_probability=0.0,
            log_path=str(tmp_path / "paper_log.jsonl"),
        )
        result = await sim.execute(opp)

        assert result.success
        assert result.all_filled
        assert result.realized_profit > Decimal("0")
        assert result.realized_profit == opp.net_profit


class TestPaperSimulatorLegFailure:
    @pytest.mark.asyncio
    async def test_all_leg_failure_marks_result_failed(self, tmp_path):
        from src.engine.detector import ArbitrageOpportunity, SignalType
        from src.execution.paper import PaperSimulator, OrderStatus

        opp = ArbitrageOpportunity(
            market_id="m-fail",
            signal_type=SignalType.BUY_SET,
            token_ids=["t1", "t2"],
            prices=[Decimal("0.40"), Decimal("0.40")],
            vwap_prices=[Decimal("0.40"), Decimal("0.40")],
            sizes=[Decimal("100"), Decimal("100")],
            max_size=Decimal("100"),
            executable_quantity=Decimal("100"),
            total_cost=Decimal("80"),
            expected_payout=Decimal("100"),
            gross_profit=Decimal("20"),
            fees=Decimal("0"),
            net_profit=Decimal("20"),
            profit_pct=Decimal("0.25"),
            edge=Decimal("0.20"),
            roi=Decimal("0.25"),
        )
        sim = PaperSimulator(
            latency_ms=0,
            base_fill_probability=1.0,
            leg_failure_probability=1.0,  # force first leg to fail
            log_path=str(tmp_path / "paper_log.jsonl"),
        )
        result = await sim.execute(opp)

        assert not result.success
        assert not result.all_filled
        # With leg_failure_probability=1.0 ALL legs fail -> nothing filled
        assert not result.any_filled
        assert any(o.status == OrderStatus.FAILED for o in result.orders)
        assert "leg" in (result.leg_risk or "").lower() or result.leg_risk

    @pytest.mark.asyncio
    async def test_partial_fill_reduces_realized_profit(self, tmp_path):
        from src.engine.detector import ArbitrageOpportunity, SignalType
        from src.execution.paper import PaperSimulator, OrderStatus

        opp = ArbitrageOpportunity(
            market_id="m-partial",
            signal_type=SignalType.BUY_SET,
            token_ids=["t1", "t2"],
            prices=[Decimal("0.40"), Decimal("0.40")],
            vwap_prices=[Decimal("0.40"), Decimal("0.40")],
            sizes=[Decimal("10"), Decimal("10")],
            max_size=Decimal("10"),
            executable_quantity=Decimal("10"),
            total_cost=Decimal("8"),
            expected_payout=Decimal("10"),
            gross_profit=Decimal("2"),
            fees=Decimal("0"),
            net_profit=Decimal("2"),
            profit_pct=Decimal("0.25"),
            edge=Decimal("0.20"),
            roi=Decimal("0.25"),
        )
        sim = PaperSimulator(
            latency_ms=0,
            base_fill_probability=1.0,   # all legs pass fill gate
            leg_failure_probability=0.0,
            fill_fraction_jitter=0.5,     # each fill is 50-100% of executable qty
            log_path=str(tmp_path / "paper_log.jsonl"),
        )
        result = await sim.execute(opp)

        # All legs should at least partially fill
        assert result.any_filled
        assert result.total_filled <= opp.executable_quantity
        # Partial fill -> realized < full profit
        assert result.realized_profit <= opp.net_profit


class TestPaperSimulatorLatency:
    @pytest.mark.asyncio
    async def test_latency_is_applied(self, tmp_path):
        from src.engine.detector import ArbitrageOpportunity, SignalType
        from src.execution.paper import PaperSimulator

        opp = ArbitrageOpportunity(
            market_id="m-lat",
            signal_type=SignalType.BUY_SET,
            token_ids=["t1", "t2"],
            prices=[Decimal("0.40"), Decimal("0.40")],
            vwap_prices=[Decimal("0.40"), Decimal("0.40")],
            sizes=[Decimal("100"), Decimal("100")],
            max_size=Decimal("100"),
            executable_quantity=Decimal("100"),
            total_cost=Decimal("80"),
            expected_payout=Decimal("100"),
            gross_profit=Decimal("20"),
            fees=Decimal("0"),
            net_profit=Decimal("20"),
            profit_pct=Decimal("0.25"),
            edge=Decimal("0.20"),
            roi=Decimal("0.25"),
        )
        sim = PaperSimulator(
            latency_ms=60,
            base_fill_probability=1.0,
            leg_failure_probability=0.0,
            log_path=str(tmp_path / "paper_log.jsonl"),
        )
        start = time.monotonic()
        result = await sim.execute(opp)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert result.execution_time_ms >= 60
        assert elapsed_ms >= 60


class TestPaperSimulatorLogging:
    @pytest.mark.asyncio
    async def test_structured_log_written(self, tmp_path):
        from src.engine.detector import ArbitrageOpportunity, SignalType
        from src.execution.paper import PaperSimulator

        opp = ArbitrageOpportunity(
            market_id="m-log",
            signal_type=SignalType.BUY_SET,
            token_ids=["t1", "t2"],
            prices=[Decimal("0.40"), Decimal("0.40")],
            vwap_prices=[Decimal("0.40"), Decimal("0.40")],
            sizes=[Decimal("100"), Decimal("100")],
            max_size=Decimal("100"),
            executable_quantity=Decimal("100"),
            total_cost=Decimal("80"),
            expected_payout=Decimal("100"),
            gross_profit=Decimal("20"),
            fees=Decimal("0"),
            net_profit=Decimal("20"),
            profit_pct=Decimal("0.25"),
            edge=Decimal("0.20"),
            roi=Decimal("0.25"),
        )
        log_path = str(tmp_path / "paper_log.jsonl")
        sim = PaperSimulator(
            latency_ms=0,
            base_fill_probability=1.0,
            leg_failure_probability=0.0,
            log_path=log_path,
        )
        await sim.execute(opp)

        lines = open(log_path).read().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["market_id"] == "m-log"
        assert record["success"] is True
        assert "realized" in record
        assert "signal" in record
        assert "size" in record
