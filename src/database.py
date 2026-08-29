"""Database layer for Cordyceps using SQLAlchemy/SQLModel.

Provides:
- Engine and session helpers
- Core data models (Trade, Opportunity, Position)
- Simple init and upsert helpers
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

from sqlalchemy import JSON, BigInteger, Column, UniqueConstraint, desc, inspect, text
from sqlmodel import Field, Session, SQLModel, create_engine, select

# ---------------------------------------------------------------------------
# Engine / Session helpers
# ---------------------------------------------------------------------------


def _get_db_url(settings: object | None = None) -> str:
    """Resolve database URL from settings, falling back to SQLite file."""
    return getattr(settings, "database_url", "sqlite:///./cordyceps.db")


def get_engine(settings: object | None = None):
    """Create a SQLAlchemy engine bound to the resolved DB URL."""
    return _engine_for_url(_get_db_url(settings))


@lru_cache(maxsize=8)
def _engine_for_url(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(
        url, echo=False, future=True, pool_pre_ping=True, connect_args=connect_args
    )


def get_session(settings: object | None = None, expire_on_commit: bool = False) -> Session:
    """Return a new SQLModel Session bound to the configured engine."""
    return Session(get_engine(settings), expire_on_commit=expire_on_commit)


def _now_ms() -> int:
    """Current timestamp in milliseconds (convenient for DB records)."""
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------


class Trade(SQLModel, table=True):
    """Persistent record of a trade execution."""

    __tablename__ = "trades"

    id: int | None = Field(default=None, primary_key=True)
    trade_id: str = Field(index=True, description="Unique trade identifier")
    market_id: str = Field(index=True, description="Market condition ID")
    signal_type: str = Field(default="BUY_SET", description="BUY_SET or SELL_SET")
    token_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    side: str = Field(default="BUY", description="Order side")
    size: float = Field(default=0.0, ge=0, description="Trade size in USDC")
    price: float = Field(default=0.0, ge=0, description="Limit price per token")
    total_cost: float = Field(default=0.0, ge=0, description="Total cost in USDC")
    expected_profit: float = Field(default=0.0, ge=0, description="Expected profit in USDC")
    realized_profit: float = Field(default=0.0, ge=0, description="Realized P&L in USDC")
    fees: float = Field(default=0.0, ge=0, description="Fees paid in USDC")
    success: bool = Field(default=False, description="Whether the trade filled")
    execution_time_ms: int = Field(default=0, ge=0, description="Execution latency")
    status: str = Field(default="pending", description="Order status")
    timestamp: int = Field(default_factory=_now_ms, sa_type=BigInteger)


class Opportunity(SQLModel, table=True):
    """Snapshot of a detected arbitrage opportunity."""

    __tablename__ = "opportunities"

    id: int | None = Field(default=None, primary_key=True)
    market_id: str = Field(index=True, description="Market condition ID")
    signal_type: str = Field(description="BUY_SET or SELL_SET")
    token_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    prices: list[float] = Field(default_factory=list, sa_column=Column(JSON))
    market: str = Field(default="")
    best_prices: list[float] = Field(default_factory=list, sa_column=Column(JSON))
    vwap_prices: list[float] = Field(default_factory=list, sa_column=Column(JSON))
    gross_edge: float = Field(default=0)
    net_edge: float = Field(default=0)
    fee: float = Field(default=0)
    slippage: float = Field(default=0)
    size: float = Field(default=0)
    net_profit: float = Field(default=0)
    max_size: float = Field(default=0)
    decision: str = Field(default="rejected", index=True)
    rejection_reason: str = Field(default="")
    status: str = Field(default="detected")
    timestamp: int = Field(default_factory=_now_ms, sa_type=BigInteger)


class Position(SQLModel, table=True):
    """Open position tracking for a market token."""

    __tablename__ = "positions"

    id: int | None = Field(default=None, primary_key=True)
    token_id: str = Field(index=True, description="Token identifier")
    market_id: str = Field(index=True, description="Market condition ID")
    side: str = Field(description="BUY or SELL")
    size: float = Field(default=0.0, ge=0, description="Position size")
    avg_price: float = Field(default=0.0, ge=0, description="Average fill price")
    unrealized_pnl: float = Field(default=0.0, description="Unrealized P&L")
    status: str = Field(default="open", description="Position status")
    entry_timestamp: int = Field(default_factory=_now_ms, sa_type=BigInteger)


class RuntimeConfig(SQLModel, table=True):
    """Validated non-secret operational override."""

    __tablename__ = "runtime_config"
    __table_args__ = (UniqueConstraint("key", name="uq_runtime_config_key"),)

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True)
    value: Any = Field(sa_column=Column(JSON, nullable=False))
    updated_at: int = Field(default_factory=_now_ms, sa_type=BigInteger)


class Execution(SQLModel, table=True):
    __tablename__ = "executions"

    id: int | None = Field(default=None, primary_key=True)
    execution_id: str = Field(index=True)
    opportunity_id: int | None = Field(default=None, foreign_key="opportunities.id")
    market_id: str = Field(default="", index=True)
    mode: str = Field(default="paper")
    state: str = Field(default="DETECTED", index=True)
    filled_quantity: float = Field(default=0)
    average_price: float = Field(default=0)
    fees: float = Field(default=0)
    realized_pnl: float = Field(default=0)
    latency_ms: int = Field(default=0)
    failure_reason: str = Field(default="")
    created_at: int = Field(default_factory=_now_ms, sa_type=BigInteger)
    updated_at: int = Field(default_factory=_now_ms, sa_type=BigInteger)


class ExecutionLeg(SQLModel, table=True):
    __tablename__ = "execution_legs"

    id: int | None = Field(default=None, primary_key=True)
    execution_id: str = Field(index=True)
    token_id: str = Field(index=True)
    side: str = Field(default="BUY")
    status: str = Field(default="PENDING")
    order_id: str = Field(default="")
    requested_quantity: float = Field(default=0)
    filled_quantity: float = Field(default=0)
    limit_price: float = Field(default=0)
    average_price: float = Field(default=0)
    fees: float = Field(default=0)
    latency_ms: int = Field(default=0)
    error: str = Field(default="")
    timestamp: int = Field(default_factory=_now_ms, sa_type=BigInteger)


class PaperTrade(SQLModel, table=True):
    __tablename__ = "paper_trades"

    id: int | None = Field(default=None, primary_key=True)
    trade_id: str = Field(index=True)
    market_id: str = Field(index=True)
    signal: str = Field(default="BUY_SET")
    token_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    size: float = Field(default=0)
    total_cost: float = Field(default=0)
    fees: float = Field(default=0)
    expected_profit: float = Field(default=0)
    realized_pnl: float = Field(default=0)
    success: bool = Field(default=False)
    latency_ms: int = Field(default=0)
    timestamp: int = Field(default_factory=_now_ms, sa_type=BigInteger)


class RiskEvent(SQLModel, table=True):
    __tablename__ = "risk_events"

    id: int | None = Field(default=None, primary_key=True)
    severity: str = Field(default="warning", index=True)
    event_type: str = Field(index=True)
    message: str
    execution_id: str = Field(default="")
    details: dict = Field(default_factory=dict, sa_column=Column(JSON))
    timestamp: int = Field(default_factory=_now_ms, sa_type=BigInteger)


class SystemEvent(SQLModel, table=True):
    __tablename__ = "system_events"

    id: int | None = Field(default=None, primary_key=True)
    severity: str = Field(default="info", index=True)
    component: str = Field(default="system", index=True)
    event_type: str = Field(index=True)
    message: str
    details: dict = Field(default_factory=dict, sa_column=Column(JSON))
    timestamp: int = Field(default_factory=_now_ms, sa_type=BigInteger)


# ---------------------------------------------------------------------------
# Initialization and simple helpers
# ---------------------------------------------------------------------------


def init_db(settings: object | None = None, drop_existing: bool = False) -> None:
    """Create all tables defined by the models.

    Args:
        settings: Optional settings object exposing ``database_url``.
        drop_existing: If True, drop all tables first (development only).
    """
    engine = get_engine(settings)
    if drop_existing:
        SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    _migrate_schema(engine)


def _migrate_schema(engine) -> None:
    """Add v0.2 opportunity columns and ensure BIGINT timestamp columns."""
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    dialect = engine.dialect.name
    json_type = "JSONB" if dialect == "postgresql" else "JSON"

    with engine.begin() as connection:
        # 1. Opportunities columns
        if "opportunities" in tables:
            existing = {column["name"] for column in inspector.get_columns("opportunities")}
            additions = {
                "market": "VARCHAR NOT NULL DEFAULT ''",
                "best_prices": f"{json_type} NOT NULL DEFAULT '[]'",
                "vwap_prices": f"{json_type} NOT NULL DEFAULT '[]'",
                "gross_edge": "FLOAT NOT NULL DEFAULT 0",
                "fee": "FLOAT NOT NULL DEFAULT 0",
                "slippage": "FLOAT NOT NULL DEFAULT 0",
                "size": "FLOAT NOT NULL DEFAULT 0",
                "decision": "VARCHAR NOT NULL DEFAULT 'rejected'",
                "rejection_reason": "VARCHAR NOT NULL DEFAULT ''",
            }
            for column, definition in additions.items():
                if column not in existing:
                    connection.execute(
                        text(f'ALTER TABLE opportunities ADD COLUMN "{column}" {definition}')
                    )

        # 2. If postgresql, ensure all timestamp columns are BIGINT
        if dialect == "postgresql":
            ts_columns = [
                ("runtime_config", "updated_at"),
                ("trades", "timestamp"),
                ("opportunities", "timestamp"),
                ("positions", "entry_timestamp"),
                ("executions", "created_at"),
                ("executions", "updated_at"),
                ("execution_legs", "timestamp"),
                ("paper_trades", "timestamp"),
                ("risk_events", "timestamp"),
                ("system_events", "timestamp"),
            ]
            for tbl, col in ts_columns:
                if tbl in tables:
                    try:
                        connection.execute(
                            text(f'ALTER TABLE {tbl} ALTER COLUMN "{col}" TYPE BIGINT')
                        )
                    except Exception:
                        pass


def create_session_db(settings: object | None = None) -> Session:
    """Convenience: ensure DB is initialised and return a session."""
    init_db(settings)
    return get_session(settings)


# ---------------------------------------------------------------------------
# ORM convenience helpers (used by other modules)
# ---------------------------------------------------------------------------


def upsert_trade(session: Session, trade: Trade) -> Trade:
    """Insert or update a ``Trade`` identified by ``trade_id``."""
    existing = session.exec(select(Trade).where(Trade.trade_id == trade.trade_id)).first()
    if existing:
        for k, v in trade.model_dump().items():
            setattr(existing, k, v)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def list_recent_trades(
    session: Session, limit: int = 50, after_ts: int | None = None
) -> list[Trade]:
    """Retrieve recent trades, optionally filtered by a timestamp threshold."""
    stmt = select(Trade).order_by(desc(Trade.timestamp))
    if after_ts is not None:
        stmt = stmt.where(Trade.timestamp > after_ts)
    return list(session.exec(stmt.limit(limit)).all())


def upsert_opportunity(session: Session, opp: Opportunity) -> Opportunity:
    """Insert or update an ``Opportunity`` identified by market_id + signal_type."""
    existing = session.exec(
        select(Opportunity).where(
            Opportunity.market_id == opp.market_id,
            Opportunity.signal_type == opp.signal_type,
        )
    ).first()
    if existing:
        for k, v in opp.model_dump().items():
            setattr(existing, k, v)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing
    session.add(opp)
    session.commit()
    session.refresh(opp)
    return opp


def list_opportunities(
    session: Session, limit: int = 50, status: str | None = None
) -> list[Opportunity]:
    """List stored opportunities, optionally filtered by ``status``."""
    stmt = select(Opportunity).order_by(desc(Opportunity.timestamp))
    if status:
        stmt = stmt.where(Opportunity.status == status)
    return list(session.exec(stmt.limit(limit)).all())


def load_runtime_config(session: Session) -> dict[str, object]:
    """Return persisted operational overrides; this table never stores secrets."""
    return {row.key: row.value for row in session.exec(select(RuntimeConfig)).all()}


def save_runtime_config(session: Session, values: dict[str, object]) -> None:
    for key, value in values.items():
        row = session.exec(select(RuntimeConfig).where(RuntimeConfig.key == key)).first()
        if row is None:
            row = RuntimeConfig(key=key, value=value)
        else:
            row.value = value
            row.updated_at = _now_ms()
        session.add(row)
    session.commit()


def add_system_event(
    session: Session,
    event_type: str,
    message: str,
    *,
    severity: str = "info",
    component: str = "system",
    details: dict | None = None,
) -> SystemEvent:
    event = SystemEvent(
        event_type=event_type,
        message=message,
        severity=severity,
        component=component,
        details=details or {},
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event
