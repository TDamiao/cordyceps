"""
Contracts module for interacting with Polymarket smart contracts.

This module provides wrappers for:
- Conditional Token Framework (CTF) - mergePositions, splitPositions
- USDC collateral token
"""

from src.contracts.constants import (
    CTF_ADDRESS,
    NEG_RISK_ADAPTER_ADDRESS,
    NEG_RISK_CTF_ADDRESS,
    USDC_ADDRESS,
)
from src.contracts.ctf import (
    CTFContract,
    get_collection_id,
    get_position_id,
    merge_positions,
)

__all__ = [
    "CTFContract",
    "merge_positions",
    "get_position_id",
    "get_collection_id",
    "CTF_ADDRESS",
    "USDC_ADDRESS",
    "NEG_RISK_CTF_ADDRESS",
    "NEG_RISK_ADAPTER_ADDRESS",
]
