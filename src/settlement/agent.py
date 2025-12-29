"""
Settlement agent for capital recycling via CTF contract.

Merges complete sets of outcome tokens back to USDC to unlock capital.
"""

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional
import time

from web3 import Web3
from web3.contract import Contract
from eth_account import Account
from eth_account.signers.local import LocalAccount

from src.config import Contracts, get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


# Minimal ABI for CTF contract merge operations
CTF_ABI = [
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "mergePositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "splitPosition",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# Minimal ABI for ERC20 (USDC)
ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class Position:
    """A position in a conditional token."""

    token_id: str
    condition_id: str
    balance: Decimal


@dataclass
class CompleteSet:
    """A complete set of positions that can be merged."""

    condition_id: str
    token_ids: list[str]
    amount: Decimal  # Maximum mergeable amount (min of all balances)


@dataclass
class MergeResult:
    """Result of a merge operation."""

    condition_id: str
    amount: Decimal
    tx_hash: Optional[str] = None
    success: bool = False
    error: Optional[str] = None
    gas_used: int = 0
    timestamp: int = field(default_factory=lambda: int(time.time()))


class SettlementAgent:
    """
    Handles settlement via CTF contract mergePositions.

    After acquiring complete sets of outcome tokens from arbitrage,
    this agent merges them back to USDC to recycle capital.
    """

    def __init__(
        self,
        private_key: str,
        rpc_url: Optional[str] = None,
        dry_run: bool = True,
    ):
        """
        Initialize settlement agent.

        Args:
            private_key: EOA private key for signing transactions
            rpc_url: Polygon RPC endpoint
            dry_run: If True, don't submit actual transactions
        """
        settings = get_settings()
        self._private_key = private_key
        self._rpc_url = rpc_url or settings.polygon_rpc_url
        self._dry_run = dry_run

        # Initialize Web3
        self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
        self._account: LocalAccount = Account.from_key(private_key)
        self._address = self._account.address

        # Initialize contracts
        self._ctf = self._w3.eth.contract(
            address=Web3.to_checksum_address(Contracts.CTF),
            abi=CTF_ABI,
        )

        self._usdc = self._w3.eth.contract(
            address=Web3.to_checksum_address(Contracts.USDC),
            abi=ERC20_ABI,
        )

        # Stats
        self._merges_executed = 0
        self._total_merged = Decimal("0")

        logger.info(
            "Settlement agent initialized",
            address=self._address,
            dry_run=dry_run,
        )

    @property
    def address(self) -> str:
        """Get agent wallet address."""
        return self._address

    def get_usdc_balance(self) -> Decimal:
        """
        Get current USDC balance.

        Returns:
            USDC balance (6 decimals)
        """
        balance = self._usdc.functions.balanceOf(self._address).call()
        return Decimal(balance) / Decimal("1000000")  # 6 decimals

    def get_token_balance(self, token_id: str) -> Decimal:
        """
        Get balance of a conditional token.

        Args:
            token_id: Token ID (as decimal string)

        Returns:
            Token balance
        """
        token_id_int = int(token_id)
        balance = self._ctf.functions.balanceOf(self._address, token_id_int).call()
        return Decimal(balance) / Decimal("1000000")  # 6 decimals

    def check_complete_set(
        self,
        condition_id: str,
        token_ids: list[str],
    ) -> Optional[CompleteSet]:
        """
        Check if we have a complete set that can be merged.

        Args:
            condition_id: Market condition ID
            token_ids: List of all outcome token IDs

        Returns:
            CompleteSet if mergeable, None otherwise
        """
        balances = []
        for token_id in token_ids:
            balance = self.get_token_balance(token_id)
            if balance <= 0:
                return None  # Missing one outcome
            balances.append(balance)

        # Minimum balance (max amount we can merge)
        min_balance = min(balances)

        if min_balance <= 0:
            return None

        logger.debug(
            "Complete set found",
            condition_id=condition_id,
            amount=str(min_balance),
        )

        return CompleteSet(
            condition_id=condition_id,
            token_ids=token_ids,
            amount=min_balance,
        )

    async def merge_positions(
        self,
        condition_id: str,
        amount: Decimal,
    ) -> MergeResult:
        """
        Merge outcome tokens back to USDC.

        Args:
            condition_id: Market condition ID
            amount: Amount to merge (in USDC terms)

        Returns:
            MergeResult with transaction details
        """
        result = MergeResult(condition_id=condition_id, amount=amount)

        if self._dry_run:
            logger.info(
                "[DRY RUN] Would merge positions",
                condition_id=condition_id,
                amount=str(amount),
            )
            result.success = True
            result.tx_hash = "0x" + "d" * 64  # Dummy hash
            return result

        try:
            # Convert to on-chain units (6 decimals)
            amount_raw = int(amount * Decimal("1000000"))

            # Build transaction
            condition_bytes = Web3.to_bytes(hexstr=condition_id)

            tx = self._ctf.functions.mergePositions(
                condition_bytes,
                amount_raw,
            ).build_transaction({
                "from": self._address,
                "nonce": self._w3.eth.get_transaction_count(self._address),
                "gas": 300000,
                "gasPrice": self._w3.eth.gas_price,
            })

            # Sign and send
            signed_tx = self._w3.eth.account.sign_transaction(tx, self._private_key)
            tx_hash = self._w3.eth.send_raw_transaction(signed_tx.raw_transaction)

            # Wait for receipt
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            result.tx_hash = tx_hash.hex()
            result.gas_used = receipt.gasUsed
            result.success = receipt.status == 1

            if result.success:
                self._merges_executed += 1
                self._total_merged += amount

                logger.info(
                    "Positions merged successfully",
                    tx_hash=result.tx_hash,
                    amount=str(amount),
                    gas_used=result.gas_used,
                )
            else:
                result.error = "Transaction reverted"
                logger.error("Merge transaction reverted", tx_hash=result.tx_hash)

        except Exception as e:
            result.error = str(e)
            logger.error("Failed to merge positions", error=str(e))

        return result

    async def process_complete_sets(
        self,
        markets: dict[str, list[str]],
    ) -> list[MergeResult]:
        """
        Check and merge all complete sets for given markets.

        Args:
            markets: Dict of condition_id -> list of token_ids

        Returns:
            List of MergeResults
        """
        results = []

        for condition_id, token_ids in markets.items():
            complete_set = self.check_complete_set(condition_id, token_ids)

            if complete_set:
                result = await self.merge_positions(
                    condition_id=condition_id,
                    amount=complete_set.amount,
                )
                results.append(result)

        return results

    @property
    def stats(self) -> dict:
        """Get settlement statistics."""
        return {
            "merges_executed": self._merges_executed,
            "total_merged_usdc": str(self._total_merged),
            "usdc_balance": str(self.get_usdc_balance()) if not self._dry_run else "N/A (dry run)",
        }

    def reset_stats(self) -> None:
        """Reset statistics."""
        self._merges_executed = 0
        self._total_merged = Decimal("0")


class PositionMonitor:
    """
    Monitors positions for complete sets and triggers settlements.

    Runs as a background task to continuously check for mergeable positions.
    """

    def __init__(
        self,
        settlement_agent: SettlementAgent,
        check_interval: float = 30.0,
    ):
        """
        Initialize position monitor.

        Args:
            settlement_agent: Settlement agent for merging
            check_interval: Seconds between checks
        """
        self._agent = settlement_agent
        self._check_interval = check_interval
        self._markets: dict[str, list[str]] = {}
        self._running = False

    def add_market(self, condition_id: str, token_ids: list[str]) -> None:
        """Add a market to monitor."""
        self._markets[condition_id] = token_ids

    def remove_market(self, condition_id: str) -> None:
        """Remove a market from monitoring."""
        self._markets.pop(condition_id, None)

    async def start(self) -> None:
        """Start monitoring loop."""
        self._running = True

        logger.info("Position monitor started", markets=len(self._markets))

        while self._running:
            try:
                results = await self._agent.process_complete_sets(self._markets)

                for result in results:
                    if result.success:
                        logger.info(
                            "Auto-merged complete set",
                            condition_id=result.condition_id,
                            amount=str(result.amount),
                        )

            except Exception as e:
                logger.error("Error in position monitor", error=str(e))

            await asyncio.sleep(self._check_interval)

    def stop(self) -> None:
        """Stop monitoring loop."""
        self._running = False
        logger.info("Position monitor stopped")
