"""Tests for Scanner and PaperEngine modules."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.engine.detector import ArbitrageOpportunity, SignalType
from src.paper_engine import PaperEngine, PaperFill
from src.scanner import Scanner

# ---------------------------------------------------------------------------
# Scanner tests
# ---------------------------------------------------------------------------


class _MockMarketFetcher:
    def __init__(self, markets=None):
        self._markets = markets or []

    async def fetch_markets(self, **kwargs):
        return self._markets

    async def close(self):
        pass


class _MockToken:
    def __init__(self, token_id, outcome="Yes"):
        self.token_id = token_id
        self.outcome = outcome


class _MockMarket:
    def __init__(self, cid, token_ids):
        self.condition_id = cid
        self.question_id = f"q-{cid}"
        self.question = f"Test market {cid}"
        self.slug = f"test-{cid}"
        self.tokens = [_MockToken(tid) for tid in token_ids]

    @property
    def token_ids(self):
        return [t.token_id for t in self.tokens]


class _MockObserver:
    def __init__(self):
        self._registered: dict = {}
        self._ws = _MockWS()

    @property
    def state(self):
        return _MockStateManager(self._registered)


class _MockWS:
    async def subscribe(self, token_ids):
        pass


class _MockStateManager:
    def __init__(self, registered):
        self._registered = registered

    def register_market(self, condition_id, token_ids):
        self._registered[condition_id] = token_ids

    def get_market_books(self, condition_id):
        return None


# ---------------------------------------------------------------------------
# PaperEngine tests
# ---------------------------------------------------------------------------


def _make_opportunity():
    return ArbitrageOpportunity(
        market_id="test-market-1",
        signal_type=SignalType.BUY_SET,
        token_ids=["tok-yes", "tok-no"],
        prices=[Decimal("0.48"), Decimal("0.49")],
        sizes=[Decimal("100"), Decimal("100")],
        max_size=Decimal("100"),
        total_cost=Decimal("97"),
        expected_payout=Decimal("100"),
        gross_profit=Decimal("3"),
        fees=Decimal("0.01"),
        net_profit=Decimal("2.99"),
        profit_pct=Decimal("0.03"),
    )


class TestPaperEngine:
    def test_init_defaults(self):
        engine = PaperEngine()
        assert engine.trade_count == 0
        assert engine.total_profit == 0.0
        assert len(engine.fills) == 0

    @pytest.mark.asyncio
    async def test_execute_produces_fill(self):
        engine = PaperEngine(simulated_latency_ms=0)
        fill = await engine.execute(_make_opportunity())
        assert fill.market_id == "test-market-1"
        assert fill.side == "BUY"
        assert fill.size == 100.0
        assert fill.success is True
        assert engine.trade_count == 1

    @pytest.mark.asyncio
    async def test_execute_sell_set_side(self):
        opp = _make_opportunity()
        opp.signal_type = SignalType.SELL_SET
        engine = PaperEngine(simulated_latency_ms=0)
        fill = await engine.execute(opp)
        assert fill.side == "SELL"

    @pytest.mark.asyncio
    async def test_reset_clears_state(self):
        engine = PaperEngine(simulated_latency_ms=0)
        await engine.execute(_make_opportunity())
        assert engine.trade_count == 1
        engine.reset()
        assert engine.trade_count == 0

    def test_fill_dataclass_fields(self):
        fill = PaperFill(
            trade_id="t1",
            market_id="m1",
            signal_type="BUY_SET",
            token_ids=["a", "b"],
            side="BUY",
            size=10.0,
            price=0.5,
            expected_profit=1.0,
        )
        assert fill.trade_id == "t1"
        assert fill.success is True


# ---------------------------------------------------------------------------
# Scanner tests
# ---------------------------------------------------------------------------


class TestScanner:
    def test_scanner_not_running_by_default(self):
        scanner = Scanner()
        assert scanner.is_running is False

    @pytest.mark.asyncio
    async def test_scan_once_discovers_markets(self):
        market = _MockMarket("cid-1", ["tok-a", "tok-b"])
        fetcher = _MockMarketFetcher(markets=[market])
        observer = _MockObserver()
        scanner = Scanner(fetcher=fetcher, observer=observer)

        new_tokens = await scanner.scan_once()
        assert new_tokens == ["tok-a", "tok-b"]
        assert "cid-1" in scanner._tracked

    @pytest.mark.asyncio
    async def test_scan_once_skips_already_tracked(self):
        market = _MockMarket("cid-1", ["tok-a", "tok-b"])
        fetcher = _MockMarketFetcher(markets=[market])
        observer = _MockObserver()
        scanner = Scanner(fetcher=fetcher, observer=observer)
        scanner._tracked.add("cid-1")

        new_tokens = await scanner.scan_once()
        assert new_tokens == []

    @pytest.mark.asyncio
    async def test_scanner_start_and_stop(self):
        scanner = Scanner(scan_interval_seconds=100)
        await scanner.start()
        assert scanner.is_running is True
        await scanner.stop()
        assert scanner.is_running is False
