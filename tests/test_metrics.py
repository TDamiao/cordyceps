"""
Tests for the utils/metrics module.

Run with: pytest tests/test_metrics.py -v
"""

from decimal import Decimal


class TestTradeRecord:
    """Tests for TradeRecord dataclass."""

    def test_trade_record_creation(self):
        """Test TradeRecord creation."""
        from src.utils.metrics import TradeRecord

        record = TradeRecord(
            trade_id="trade_001",
            market_id="market_123",
            signal_type="BUY_SET",
            token_ids=["yes", "no"],
            size=Decimal("100"),
            total_cost=Decimal("95"),
            expected_profit=Decimal("5"),
            realized_profit=Decimal("4.98"),
            fees=Decimal("0.02"),
            success=True,
            execution_time_ms=150,
        )

        assert record.trade_id == "trade_001"
        assert record.success
        assert record.realized_profit == Decimal("4.98")

    def test_trade_record_to_dict(self):
        """Test TradeRecord serialization."""
        from src.utils.metrics import TradeRecord

        record = TradeRecord(
            trade_id="trade_001",
            market_id="market_123",
            signal_type="BUY_SET",
            token_ids=["yes", "no"],
            size=Decimal("100"),
            total_cost=Decimal("95"),
            expected_profit=Decimal("5"),
            realized_profit=Decimal("4.98"),
            fees=Decimal("0.02"),
            success=True,
            execution_time_ms=150,
        )

        d = record.to_dict()
        assert d["trade_id"] == "trade_001"
        assert d["size"] == "100"  # Decimal converted to string

    def test_trade_record_from_dict(self):
        """Test TradeRecord deserialization."""
        from src.utils.metrics import TradeRecord

        data = {
            "trade_id": "trade_001",
            "market_id": "market_123",
            "signal_type": "BUY_SET",
            "token_ids": ["yes", "no"],
            "size": "100",
            "total_cost": "95",
            "expected_profit": "5",
            "realized_profit": "4.98",
            "fees": "0.02",
            "success": True,
            "execution_time_ms": 150,
            "timestamp": 1234567890,
        }

        record = TradeRecord.from_dict(data)
        assert record.size == Decimal("100")
        assert record.realized_profit == Decimal("4.98")


class TestPerformanceMetrics:
    """Tests for PerformanceMetrics."""

    def test_metrics_creation(self):
        """Test PerformanceMetrics creation."""
        from src.utils.metrics import PerformanceMetrics

        metrics = PerformanceMetrics()

        assert metrics.total_trades == 0
        assert metrics.net_profit == Decimal("0")
        assert metrics.win_rate == 0.0

    def test_metrics_to_dict(self):
        """Test PerformanceMetrics serialization."""
        from src.utils.metrics import PerformanceMetrics

        metrics = PerformanceMetrics(
            total_trades=10,
            successful_trades=8,
            net_profit=Decimal("50"),
        )

        d = metrics.to_dict()
        assert d["total_trades"] == 10
        assert d["net_profit"] == "50"


class TestMetricsTracker:
    """Tests for MetricsTracker."""

    def test_tracker_initialization(self):
        """Test MetricsTracker initialization."""
        from src.utils.metrics import MetricsTracker

        tracker = MetricsTracker()
        metrics = tracker.get_metrics()

        assert metrics.total_trades == 0

    def test_record_trade(self):
        """Test recording a trade."""
        from src.utils.metrics import MetricsTracker, TradeRecord

        tracker = MetricsTracker()

        record = TradeRecord(
            trade_id="trade_001",
            market_id="market_123",
            signal_type="BUY_SET",
            token_ids=["yes", "no"],
            size=Decimal("100"),
            total_cost=Decimal("95"),
            expected_profit=Decimal("5"),
            realized_profit=Decimal("4.98"),
            fees=Decimal("0.02"),
            success=True,
            execution_time_ms=150,
        )

        tracker.record_trade(record)
        metrics = tracker.get_metrics()

        assert metrics.total_trades == 1
        assert metrics.successful_trades == 1
        assert metrics.total_profit == Decimal("4.98")

    def test_create_trade_record(self):
        """Test creating and recording a trade record."""
        from src.utils.metrics import MetricsTracker

        tracker = MetricsTracker()

        record = tracker.create_trade_record(
            market_id="market_123",
            signal_type="BUY_SET",
            token_ids=["yes", "no"],
            size=Decimal("100"),
            total_cost=Decimal("95"),
            expected_profit=Decimal("5"),
            realized_profit=Decimal("4.98"),
            fees=Decimal("0.02"),
            success=True,
            execution_time_ms=150,
        )

        assert record.trade_id.startswith("trade_")
        assert tracker.get_metrics().total_trades == 1

    def test_get_recent_trades(self):
        """Test getting recent trades."""
        from src.utils.metrics import MetricsTracker, TradeRecord

        tracker = MetricsTracker()

        for i in range(5):
            record = TradeRecord(
                trade_id=f"trade_{i}",
                market_id="market_123",
                signal_type="BUY_SET",
                token_ids=["yes", "no"],
                size=Decimal("100"),
                total_cost=Decimal("95"),
                expected_profit=Decimal("5"),
                realized_profit=Decimal("5"),
                fees=Decimal("0"),
                success=True,
                execution_time_ms=100,
            )
            tracker.record_trade(record)

        recent = tracker.get_recent_trades(3)
        assert len(recent) == 3
        assert recent[-1].trade_id == "trade_4"

    def test_win_rate_calculation(self):
        """Test win rate is calculated correctly."""
        from src.utils.metrics import MetricsTracker, TradeRecord

        tracker = MetricsTracker()

        # 3 successful, 1 failed = 75% win rate
        for i in range(4):
            record = TradeRecord(
                trade_id=f"trade_{i}",
                market_id="market_123",
                signal_type="BUY_SET",
                token_ids=["yes", "no"],
                size=Decimal("100"),
                total_cost=Decimal("95"),
                expected_profit=Decimal("5"),
                realized_profit=Decimal("5") if i < 3 else Decimal("0"),
                fees=Decimal("0"),
                success=i < 3,
                execution_time_ms=100,
            )
            tracker.record_trade(record)

        metrics = tracker.get_metrics()
        assert metrics.win_rate == 0.75

    def test_reset(self):
        """Test resetting tracker."""
        from src.utils.metrics import MetricsTracker, TradeRecord

        tracker = MetricsTracker()

        record = TradeRecord(
            trade_id="trade_001",
            market_id="market_123",
            signal_type="BUY_SET",
            token_ids=["yes", "no"],
            size=Decimal("100"),
            total_cost=Decimal("95"),
            expected_profit=Decimal("5"),
            realized_profit=Decimal("5"),
            fees=Decimal("0"),
            success=True,
            execution_time_ms=100,
        )
        tracker.record_trade(record)
        tracker.reset()

        assert tracker.get_metrics().total_trades == 0


class TestHealthMonitor:
    """Tests for HealthMonitor."""

    def test_health_monitor_initialization(self):
        """Test HealthMonitor initialization."""
        from src.utils.metrics import HealthMonitor, MetricsTracker

        tracker = MetricsTracker()
        monitor = HealthMonitor(tracker)

        health = monitor.get_health()
        assert health.status in ["healthy", "degraded", "unhealthy"]

    def test_websocket_status(self):
        """Test WebSocket status affects health."""
        from src.utils.metrics import HealthMonitor, MetricsTracker

        tracker = MetricsTracker()
        monitor = HealthMonitor(tracker)

        monitor.set_websocket_status(False)
        health = monitor.get_health()
        assert health.status == "unhealthy"

        monitor.set_websocket_status(True)
        health = monitor.get_health()
        assert health.status == "healthy"

    def test_error_recording(self):
        """Test error recording affects health."""
        from src.utils.metrics import HealthMonitor, MetricsTracker

        tracker = MetricsTracker()
        monitor = HealthMonitor(tracker, error_threshold=2)

        monitor.set_websocket_status(True)

        # Record errors
        for i in range(3):
            monitor.record_error(f"Test error {i}")

        health = monitor.get_health()
        assert health.status == "degraded"
        assert health.errors_last_hour >= 3

    def test_health_json(self):
        """Test health JSON output."""
        import json

        from src.utils.metrics import HealthMonitor, MetricsTracker

        tracker = MetricsTracker()
        monitor = HealthMonitor(tracker)

        json_str = monitor.get_health_json()
        data = json.loads(json_str)

        assert "status" in data
        assert "uptime_seconds" in data
        assert "metrics" in data
