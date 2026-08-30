"""Polymarket CLOB V2 per-market fee calculation."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

import aiohttp


@dataclass(frozen=True)
class FeeParameters:
    rate: Decimal
    exponent: Decimal
    taker_only: bool = True
    source: str = "clob"


def calculate_taker_fee(
    shares: Decimal,
    price: Decimal,
    params: FeeParameters,
) -> Decimal:
    """Return C × rate × (p × (1-p))^exponent in pUSD.

    This mirrors the official V2 SDK. Invalid parameters fail closed.
    """
    if shares < 0 or not Decimal("0") < price < Decimal("1"):
        raise ValueError("shares must be non-negative and price must be between 0 and 1")
    if params.rate < 0 or params.exponent < 0:
        raise ValueError("fee parameters cannot be negative")
    return shares * params.rate * (price * (Decimal("1") - price)) ** params.exponent


class FeeService:
    """Caches CLOB market fee curves; missing data uses a visible conservative curve."""

    def __init__(self, clob_url: str, fallback_rate: float = 0.072, ttl_seconds: int = 300):
        self._url = clob_url.rstrip("/")
        self._fallback = FeeParameters(
            rate=Decimal(str(fallback_rate)), exponent=Decimal("1"), source="fallback"
        )
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, FeeParameters]] = {}

    def get(self, condition_id: str) -> FeeParameters:
        cached = self._cache.get(condition_id)
        if cached and time.monotonic() - cached[0] < self._ttl:
            return cached[1]
        return self._fallback

    async def refresh(
        self, condition_id: str, session: aiohttp.ClientSession | None = None
    ) -> FeeParameters:
        owns_session = session is None
        # Use Dokploy's HTTP(S)_PROXY for the CLOB fee endpoint as well.
        session = session or aiohttp.ClientSession(trust_env=True)
        try:
            async with session.get(
                f"{self._url}/clob-markets/{condition_id}", timeout=5
            ) as response:
                response.raise_for_status()
                payload = await response.json()
            details = payload.get("fd")
            if not isinstance(details, dict) or "r" not in details or "e" not in details:
                raise ValueError("fee details missing from CLOB market response")
            params = FeeParameters(
                rate=Decimal(str(details["r"])),
                exponent=Decimal(str(details["e"])),
                taker_only=bool(details.get("to", True)),
                source="clob",
            )
            calculate_taker_fee(Decimal("1"), Decimal("0.5"), params)
            self._cache[condition_id] = (time.monotonic(), params)
            return params
        except Exception:
            self._cache[condition_id] = (time.monotonic(), self._fallback)
            return self._fallback
        finally:
            if owns_session:
                await session.close()
