"""
Tests for the settlement module (CTF merge and position monitoring).

Run with: pytest tests/test_settlement.py -v
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


class TestPosition:
    """Tests for Position dataclass."""

    def test_position_creation(self):
        """Test Position creation."""
        from src.settlement.agent import Position

        pos = Position(
            token_id="123456",
            condition_id="0xabc123",
            balance=Decimal("100"),
        )

        assert pos.token_id == "123456"
        assert pos.condition_id == "0xabc123"
        assert pos.balance == Decimal("100")


class TestCompleteSet:
    """Tests for CompleteSet dataclass."""

    def test_complete_set_creation(self):
        """Test CompleteSet creation."""
        from src.settlement.agent import CompleteSet

        cs = CompleteSet(
            condition_id="0xabc123",
            token_ids=["token_yes", "token_no"],
            amount=Decimal("50"),
        )

        assert cs.condition_id == "0xabc123"
        assert len(cs.token_ids) == 2
        assert cs.amount == Decimal("50")


class TestMergeResult:
    """Tests for MergeResult dataclass."""

    def test_merge_result_creation(self):
        """Test MergeResult creation."""
        from src.settlement.agent import MergeResult

        result = MergeResult(
            condition_id="0xabc123",
            amount=Decimal("100"),
            tx_hash="0x1234",
            success=True,
        )

        assert result.condition_id == "0xabc123"
        assert result.amount == Decimal("100")
        assert result.success
        assert result.tx_hash == "0x1234"

    def test_merge_result_failed(self):
        """Test MergeResult for failed merge."""
        from src.settlement.agent import MergeResult

        result = MergeResult(
            condition_id="0xabc123",
            amount=Decimal("100"),
            success=False,
            error="Transaction reverted",
        )

        assert not result.success
        assert result.error == "Transaction reverted"


class TestSettlementAgent:
    """Tests for SettlementAgent."""

    @pytest.fixture
    def mock_web3(self):
        """Create mock Web3 instance."""
        with patch("src.settlement.agent.Web3") as mock:
            mock_instance = MagicMock()
            mock.return_value = mock_instance
            mock.HTTPProvider = MagicMock()
            mock.to_checksum_address = lambda x: x
            mock.to_bytes = lambda hexstr: bytes.fromhex(
                hexstr[2:] if hexstr.startswith("0x") else hexstr
            )
            yield mock

    @pytest.fixture
    def mock_account(self):
        """Create mock Account."""
        with patch("src.settlement.agent.Account") as mock:
            mock_acc = MagicMock()
            mock_acc.address = "0x1234567890abcdef1234567890abcdef12345678"
            mock.from_key.return_value = mock_acc
            yield mock

    def test_agent_initialization(self, mock_web3, mock_account):
        """Test agent initialization."""
        from src.settlement.agent import SettlementAgent

        # Mock settings
        with patch("src.settlement.agent.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(polygon_rpc_url="https://polygon-rpc.com")

            agent = SettlementAgent(
                private_key="0x" + "a" * 64,
                dry_run=True,
            )

            assert agent.address is not None
            assert agent.stats["merges_executed"] == 0

    def test_stats(self, mock_web3, mock_account):
        """Test stats tracking."""
        from src.settlement.agent import SettlementAgent

        with patch("src.settlement.agent.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(polygon_rpc_url="https://polygon-rpc.com")

            agent = SettlementAgent(
                private_key="0x" + "a" * 64,
                dry_run=True,
            )

            stats = agent.stats
            assert "merges_executed" in stats
            assert "total_merged_usdc" in stats

            agent.reset_stats()
            assert agent.stats["merges_executed"] == 0


class TestPositionMonitor:
    """Tests for PositionMonitor."""

    def test_monitor_add_market(self):
        """Test adding markets to monitor."""
        from unittest.mock import MagicMock

        from src.settlement.agent import PositionMonitor

        mock_agent = MagicMock()
        monitor = PositionMonitor(settlement_agent=mock_agent)

        monitor.add_market("cond_123", ["token_yes", "token_no"])
        assert "cond_123" in monitor._markets

    def test_monitor_remove_market(self):
        """Test removing markets from monitor."""
        from unittest.mock import MagicMock

        from src.settlement.agent import PositionMonitor

        mock_agent = MagicMock()
        monitor = PositionMonitor(settlement_agent=mock_agent)

        monitor.add_market("cond_123", ["token_yes", "token_no"])
        monitor.remove_market("cond_123")
        assert "cond_123" not in monitor._markets

    def test_monitor_stop(self):
        """Test stopping monitor."""
        from unittest.mock import MagicMock

        from src.settlement.agent import PositionMonitor

        mock_agent = MagicMock()
        monitor = PositionMonitor(settlement_agent=mock_agent)

        monitor._running = True
        monitor.stop()
        assert not monitor._running
