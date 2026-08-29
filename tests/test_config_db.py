from __future__ import annotations

import os
from pathlib import Path

import pytest


class TestSettings:
    def test_paper_mode_does_not_require_private_key(self, monkeypatch):
        monkeypatch.delenv("PRIVATE_KEY", raising=False)
        monkeypatch.delenv("PROXY_ADDRESS", raising=False)
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

        from src.config import get_settings

        get_settings.cache_clear()
        settings = get_settings()

        assert settings.trading_mode == "paper"
        assert settings.live_trading_enabled is False
        assert settings.private_key == ""
        assert settings.proxy_address == ""

    def test_live_mode_requires_private_key_and_proxy(self, monkeypatch):
        monkeypatch.setenv("TRADING_MODE", "live")
        monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.delenv("PRIVATE_KEY", raising=False)
        monkeypatch.delenv("PROXY_ADDRESS", raising=False)

        from src.config import get_settings

        get_settings.cache_clear()
        with pytest.raises(ValueError):
            get_settings()


class TestDatabase:
    def test_init_db_creates_tables(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TRADING_MODE", "paper")
        monkeypatch.setenv("LIVE_TRADING_ENABLED", "false")
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cordyceps.db'}")

        from src.config import get_settings
        from src.database import init_db, get_engine

        get_settings.cache_clear()
        settings = get_settings()
        init_db(settings)

        engine = get_engine(settings)
        assert engine is not None
        assert (tmp_path / "cordyceps.db").exists()

    def test_db_models_have_table_names(self):
        from src.database import Opportunity, Position, Trade

        assert Trade.__tablename__ == "trades"
        assert Opportunity.__tablename__ == "opportunities"
        assert Position.__tablename__ == "positions"
