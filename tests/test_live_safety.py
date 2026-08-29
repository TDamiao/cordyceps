"""Safety regression tests; all network and order paths are mocked."""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Settings
from src.engine.detector import ArbitrageOpportunity, SignalType
from src.execution.executor import ExecutionState, OrderExecutor
from src.fees import FeeParameters, calculate_taker_fee
from src.runtime import RuntimeState
from src.safety import GeoblockResult, GeoblockService, ReadinessService, WalletSnapshot


def settings(tmp_path, **values) -> Settings:
    defaults = {
        "trading_mode": "live_test",
        "live_trading_enabled": True,
        "dry_run": False,
        "private_key": "0x" + "a" * 64,
        "proxy_address": "0x" + "b" * 40,
        "admin_token": "admin-test-token",
        "polymarket_api_secret": "server-secret",
        "database_url": f"sqlite:///{tmp_path / 'test.db'}",
        "leg_timeout_ms": 100,
        "simulated_latency_ms": 0,
    }
    return Settings(_env_file=None, **(defaults | values))


def opportunity() -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        market_id="market",
        signal_type=SignalType.BUY_SET,
        token_ids=["yes", "no"],
        prices=[Decimal("0.45"), Decimal("0.45")],
        sizes=[Decimal("1"), Decimal("1")],
        max_size=Decimal("1"),
        total_cost=Decimal("0.90"),
        expected_payout=Decimal("1"),
        gross_profit=Decimal("0.10"),
        fees=Decimal("0"),
        net_profit=Decimal("0.08"),
        profit_pct=Decimal("0.08"),
        net_edge=Decimal("0.08"),
    )


def matched(order_id: str) -> dict:
    return {"success": True, "orderID": order_id, "status": "matched"}


def failed() -> dict:
    return {"success": False, "orderID": "", "status": "", "errorMsg": "no fill"}


class TestFees:
    def test_official_v2_curve(self):
        params = FeeParameters(rate=Decimal("0.25"), exponent=Decimal("2"))
        assert calculate_taker_fee(Decimal("100"), Decimal("0.5"), params) == Decimal("1.5625")
        assert calculate_taker_fee(Decimal("100"), Decimal("0.3"), params) == Decimal("1.102500")

    def test_invalid_fee_inputs_fail_closed(self):
        with pytest.raises(ValueError):
            calculate_taker_fee(
                Decimal("1"),
                Decimal("1"),
                FeeParameters(rate=Decimal("0.07"), exponent=Decimal("1")),
            )


class TestRuntimeSafety:
    def test_live_test_defaults_are_conservative(self, tmp_path):
        cfg = settings(tmp_path)
        assert cfg.max_trade_usd == 1
        assert cfg.max_total_exposure_usd == 2
        assert cfg.max_daily_loss_usd == 1
        assert cfg.max_open_trades == 1
        assert cfg.max_leg_imbalance_usd == 1

    def test_inconsistent_config_is_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="MAX_TRADE_USD"):
            settings(tmp_path, max_trade_usd=3, max_total_exposure_usd=2)
        with pytest.raises(ValueError):
            settings(tmp_path, max_trade_usd=float("nan"))

    def test_restart_is_disarmed(self, tmp_path):
        cfg = settings(tmp_path)
        runtime = RuntimeState.load(cfg)
        runtime.arm()
        assert runtime.armed
        restarted = RuntimeState.load(cfg)
        assert restarted.armed is False

    def test_runtime_config_persists_without_secrets(self, tmp_path):
        cfg = settings(tmp_path)
        runtime = RuntimeState.load(cfg)
        runtime.update_config({"max_trade_usd": 0.5})
        restarted = RuntimeState.load(cfg)
        assert restarted.settings.max_trade_usd == 0.5
        assert restarted.settings.private_key == cfg.private_key

    def test_kill_switch_disarms_and_blocks(self, tmp_path):
        runtime = RuntimeState(settings(tmp_path), armed=True, geo_allowed=True)
        runtime.kill()
        assert runtime.armed is False
        assert runtime.can_submit_live() == (False, "live trading is disarmed")


class TestLegExecution:
    @pytest.mark.asyncio
    async def test_partial_fill_completes_once_then_cools_down(self, tmp_path):
        runtime = RuntimeState(settings(tmp_path), armed=True, geo_allowed=True)
        client = MagicMock()
        client.create_fok_order.side_effect = [matched("one"), failed(), matched("two")]
        client.get_order.return_value = {"size_matched": "1000000"}
        executor = OrderExecutor(client, runtime=runtime)
        result = await executor.execute_opportunity(opportunity())
        assert result.state == ExecutionState.COMPLETED
        assert result.success is True
        assert executor._risk.state["is_paused"] is True
        assert runtime.incomplete_exposure_usd == 0

    @pytest.mark.asyncio
    async def test_failed_unwind_activates_kill_switch(self, tmp_path):
        runtime = RuntimeState(settings(tmp_path), armed=True, geo_allowed=True)
        client = MagicMock()
        client.create_fok_order.side_effect = [matched("one"), failed(), failed(), failed()]
        client.get_order.return_value = {"size_matched": "1000000"}
        executor = OrderExecutor(client, runtime=runtime)
        result = await executor.execute_opportunity(opportunity())
        assert result.state == ExecutionState.FAILED
        assert "EXPOSURE REQUIRES ATTENTION" in result.error
        assert runtime.kill_switch is True

    @pytest.mark.asyncio
    async def test_leg_timeout_fails_without_fill(self, tmp_path):
        runtime = RuntimeState(settings(tmp_path), armed=True, geo_allowed=True)
        client = MagicMock()

        def slow_order(**kwargs):
            time.sleep(0.2)
            return matched("late")

        client.create_fok_order.side_effect = slow_order
        executor = OrderExecutor(client, runtime=runtime)
        result = await executor.execute_opportunity(opportunity())
        assert result.state == ExecutionState.FAILED
        assert not result.any_filled

    @pytest.mark.asyncio
    async def test_live_test_global_lock(self, tmp_path):
        runtime = RuntimeState(settings(tmp_path), armed=True, geo_allowed=True)
        executor = OrderExecutor(MagicMock(), runtime=runtime)
        await runtime.execution_lock.acquire()
        try:
            result = await executor.execute_opportunity(opportunity())
        finally:
            runtime.execution_lock.release()
        assert result.state == ExecutionState.ABORTED
        assert "active" in result.error


@pytest.mark.asyncio
async def test_readiness_is_fail_closed_and_never_returns_secrets(tmp_path):
    cfg = settings(tmp_path)
    runtime = RuntimeState(cfg, armed=False)
    geoblock = MagicMock()
    geoblock.check = AsyncMock(
        return_value=GeoblockResult(checked=True, blocked=False, country="BR")
    )
    wallet = MagicMock()
    wallet.snapshot = WalletSnapshot(
        eoa_address="0x" + "a" * 40,
        proxy_address=cfg.proxy_address,
        collateral_balance=10,
        collateral_allowance=10,
        ctf_allowance=10,
        authenticated=True,
        refreshed_at=time.time(),
    )
    bot = MagicMock()
    bot.get_status.return_value = {
        "health": {"websocket_connected": True},
        "observer_stats": {"book_updates": 5, "books_with_liquidity": 2},
        "risk": {"is_paused": False},
    }
    service = ReadinessService(runtime, geoblock, wallet, bot)
    with (
        patch.object(service, "_http_ok", AsyncMock(return_value=True)),
        patch("src.safety.Web3") as web3,
    ):
        web3.return_value.is_connected.return_value = True
        result = await service.check()
    assert result["ready"] is True
    serialized = str(result) + str(wallet.snapshot.public_dict())
    assert cfg.private_key not in serialized
    assert "server-secret" not in serialized


def test_geoblock_disabled_allows_trading(tmp_path):
    runtime = RuntimeState(settings(tmp_path), armed=True, geo_allowed=True)
    geo = GeoblockResult()
    assert geo.public_dict()["trading_allowed"] is True
    assert runtime.can_submit_live() == (True, "ok")


@pytest.mark.asyncio
async def test_geoblock_service_always_returns_allowed(tmp_path):
    service = GeoblockService(settings(tmp_path))
    result = await service.check()
    assert result.blocked is False
    assert result.checked is True
    assert result.country == "DISABLED"
    assert result.public_dict()["trading_allowed"] is True


def test_secret_fields_are_not_editable_runtime_config(tmp_path):
    runtime = RuntimeState.load(settings(tmp_path))
    with pytest.raises(ValueError):
        runtime.update_config({"private_key": "exfiltrate"})
    assert "private_key" not in runtime.settings.runtime_values()
    assert "polymarket_api_secret" not in runtime.settings.runtime_values()
