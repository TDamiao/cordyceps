"""Database layer for Cordyceps using SQLAlchemy/SQLModel.

Provides:
- Engine and session helpers
- Core data models (Trade, Opportunity, Position)
- Simple init and upsert helpers
"""

from __future__ import annotations

import time
from typing import Optional, List

from sqlalchemy import Column, JSON, desc
from sqlmodel import Field, Session, SQLModel, create_engine, select


# ---------------------------------------------------------------------------
# Engine / Session helpers
# ---------------------------------------------------------------------------

def _get_db_url(settings: Optional[object] = None) -> str:
    """Resolve database URL from settings, falling back to SQLite file."""
    return getattr(settings, "database_url", "sqlite:///./cordyceps.db")


def get_engine(settings: Optional[object] = None):
    """Create a SQLAlchemy engine bound to the resolved DB URL."""
    return create_engine(_get_db_url(settings), echo=False, future=True)


def get_session(settings: Optional[object] = None, expire_on_commit: bool = False) -> Session:
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

    id: Optional[int] = Field(default=None, primary_key=True)
    trade_id: str = Field(index=True, description="Unique trade identifier")
    market_id: str = Field(index=True, description="Market condition ID")
    signal_type: str = Field(default="BUY_SET", description="BUY_SET or SELL_SET")
    token_ids: List[str] = Field(default_factory=list, sa_column=Column(JSON))
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
    timestamp: int = Field(default_factory=_now_ms)


class Opportunity(SQLModel, table=True):
    """Snapshot of a detected arbitrage opportunity."""

    __tablename__ = "opportunities"

    id: Optional[int] = Field(default=None, primary_key=True)
    market_id: str = Field(index=True, description="Market condition ID")
    signal_type: str = Field(description="BUY_SET or SELL_SET")
    token_ids: List[str] = Field(default_factory=list, sa_column=Column(JSON))
    prices: List[float] = Field(default_factory=list, sa_column=Column(JSON))
    net_edge: float = Field(description="Σ ask - Σ bid (edge before fees)")
    net_profit: float = Field(description="Expected net profit in USDC")
    max_size: float = Field(description="Maximum trade size before limits")
    status: str = Field(default="detected", description="Current opportunity status")
    timestamp: int = Field(default_factory=_now_ms)


class Position(SQLModel, table=True):
    """Open position tracking for a market token."""

    __tablename__ = "positions"

    id: Optional[int] = Field(default=None, primary_key=True)
    token_id: str = Field(index=True, description="Token identifier")
    market_id: str = Field(index=True, description="Market condition ID")
    side: str = Field(description="BUY or SELL")
    size: float = Field(default=0.0, ge=0, description="Position size")
    avg_price: float = Field(default=0.0, ge=0, description="Average fill price")
    unrealized_pnl: float = Field(default=0.0, description="Unrealized P&L")
    status: str = Field(default="open", description="Position status")
    entry_timestamp: int = Field(default_factory=_now_ms)


# ---------------------------------------------------------------------------
# Initialization and simple helpers
# ---------------------------------------------------------------------------

def init_db(settings: Optional[object] = None, drop_existing: bool = False) -> None:
    """Create all tables defined by the models.

    Args:
        settings: Optional settings object exposing ``database_url``.
        drop_existing: If True, drop all tables first (development only).
    """
    engine = get_engine(settings)
    if drop_existing:
        SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)


def create_session_db(settings: Optional[object] = None) -> Session:
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


def list_recent_trades(session: Session, limit: int = 50, after_ts: Optional[int] = None) -> list[Trade]:
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


def list_opportunities(session: Session, limit: int = 50, status: Optional[str] = None) -> list[Opportunity]:
    """List stored opportunities, optionally filtered by ``status``."""
    stmt = select(Opportunity).order_by(desc(Opportunity.timestamp))
    if status:
        stmt = stmt.where(Opportunity.status == status)
    return list(session.exec(stmt.limit(limit)).all())
