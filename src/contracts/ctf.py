"""
Conditional Token Framework (CTF) Contract Wrapper.

This module provides Python bindings for interacting with the Gnosis
Conditional Token Framework contract on Polygon.

Key functions:
- merge_positions: Convert YES+NO tokens back to USDC instantly
- split_position: Convert USDC to YES+NO tokens
- get_position_id: Calculate ERC-1155 token ID for a position
"""

from decimal import Decimal
from typing import Optional
from dataclasses import dataclass
from eth_typing import HexStr
from web3 import Web3
from web3.contract import Contract
from web3.types import TxReceipt

import structlog

from src.contracts.constants import (
    CTF_ADDRESS,
    USDC_ADDRESS,
    BINARY_PARTITION,
    PARENT_COLLECTION_ID,
)

logger = structlog.get_logger(__name__)

# CTF Contract ABI (minimal - only functions we need)
CTF_ABI = [
    {
        "inputs": [
            {"internalType": "contract IERC20", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "partition", "type": "uint256[]"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "mergePositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "contract IERC20", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "partition", "type": "uint256[]"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "splitPosition",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "account", "type": "address"},
            {"internalType": "uint256", "name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# ERC20 ABI for USDC approval
ERC20_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "account", "type": "address"},
        ],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass
class MergeResult:
    """Result of a merge operation."""
    
    success: bool
    tx_hash: Optional[str] = None
    gas_used: int = 0
    amount_returned: Decimal = Decimal(0)
    error: Optional[str] = None


def get_collection_id(
    parent_collection_id: bytes,
    condition_id: bytes,
    index_set: int,
) -> bytes:
    """
    Calculate the collection ID for a position.
    
    CollectionId = keccak256(parentCollectionId, conditionId, indexSet)
    
    Args:
        parent_collection_id: Parent collection (0x00...00 for root)
        condition_id: Market condition ID
        index_set: Outcome index (1=YES, 2=NO for binary)
        
    Returns:
        32-byte collection ID
    """
    # Pack and hash according to CTF spec
    packed = parent_collection_id + condition_id + index_set.to_bytes(32, "big")
    return Web3.keccak(packed)


def get_position_id(
    collateral_token: str,
    collection_id: bytes,
) -> int:
    """
    Calculate the ERC-1155 position ID.
    
    PositionId = keccak256(collateralToken, collectionId)
    
    Args:
        collateral_token: USDC address
        collection_id: Collection ID from get_collection_id
        
    Returns:
        Position ID as uint256
    """
    collateral_bytes = bytes.fromhex(collateral_token[2:].lower().zfill(40))
    packed = collateral_bytes + collection_id
    return int.from_bytes(Web3.keccak(packed), "big")


class CTFContract:
    """
    Wrapper for Conditional Token Framework contract.
    
    Provides methods for:
    - Merging YES+NO positions back to USDC
    - Checking position balances
    - Approving token transfers
    """
    
    def __init__(
        self,
        web3: Web3,
        private_key: str,
        ctf_address: str = CTF_ADDRESS,
        usdc_address: str = USDC_ADDRESS,
    ):
        """
        Initialize CTF contract wrapper.
        
        Args:
            web3: Web3 instance connected to Polygon
            private_key: Wallet private key for signing
            ctf_address: CTF contract address
            usdc_address: USDC token address
        """
        self.w3 = web3
        self.private_key = private_key
        self.account = self.w3.eth.account.from_key(private_key)
        self.address = self.account.address
        
        # Initialize contracts
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(ctf_address),
            abi=CTF_ABI,
        )
        self.usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(usdc_address),
            abi=ERC20_ABI,
        )
        
        self.ctf_address = ctf_address
        self.usdc_address = usdc_address
        
        logger.info(
            "CTF contract initialized",
            address=self.address,
            ctf=ctf_address,
        )
    
    def get_position_balance(
        self,
        condition_id: str,
        index_set: int,
    ) -> int:
        """
        Get balance of a position (YES or NO).
        
        Args:
            condition_id: Market condition ID (hex string)
            index_set: 1 for YES, 2 for NO
            
        Returns:
            Balance in raw units (6 decimals for USDC-backed)
        """
        # Calculate position ID
        condition_bytes = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)
        parent_bytes = bytes.fromhex(PARENT_COLLECTION_ID[2:])
        
        collection_id = get_collection_id(parent_bytes, condition_bytes, index_set)
        position_id = get_position_id(self.usdc_address, collection_id)
        
        return self.ctf.functions.balanceOf(self.address, position_id).call()
    
    def get_yes_no_balances(self, condition_id: str) -> tuple[int, int]:
        """
        Get both YES and NO balances for a market.
        
        Args:
            condition_id: Market condition ID
            
        Returns:
            (yes_balance, no_balance) in raw units
        """
        yes_balance = self.get_position_balance(condition_id, 1)
        no_balance = self.get_position_balance(condition_id, 2)
        return yes_balance, no_balance
    
    def can_merge(self, condition_id: str) -> tuple[bool, int]:
        """
        Check if we can merge positions and how many.
        
        Args:
            condition_id: Market condition ID
            
        Returns:
            (can_merge, amount) - True if we have both YES and NO
        """
        yes_bal, no_bal = self.get_yes_no_balances(condition_id)
        
        # Can only merge the minimum of both
        mergeable = min(yes_bal, no_bal)
        
        return mergeable > 0, mergeable
    
    async def merge_positions(
        self,
        condition_id: str,
        amount: Optional[int] = None,
        max_gas_price_gwei: float = 100,
    ) -> MergeResult:
        """
        Merge YES+NO positions back to USDC.
        
        This burns equal amounts of YES and NO tokens and
        returns the equivalent USDC to the wallet.
        
        Args:
            condition_id: Market condition ID
            amount: Amount to merge (None = max available)
            max_gas_price_gwei: Maximum gas price to use
            
        Returns:
            MergeResult with transaction details
        """
        try:
            # Check balances
            can_do, max_amount = self.can_merge(condition_id)
            
            if not can_do:
                return MergeResult(
                    success=False,
                    error="Cannot merge: missing YES or NO tokens",
                )
            
            # Use provided amount or max available
            merge_amount = amount if amount is not None else max_amount
            
            if merge_amount > max_amount:
                return MergeResult(
                    success=False,
                    error=f"Insufficient balance: requested {merge_amount}, have {max_amount}",
                )
            
            # Check gas price
            gas_price = self.w3.eth.gas_price
            max_gas_wei = self.w3.to_wei(max_gas_price_gwei, "gwei")
            
            if gas_price > max_gas_wei:
                return MergeResult(
                    success=False,
                    error=f"Gas too high: {gas_price / 1e9:.1f} gwei > {max_gas_price_gwei} gwei",
                )
            
            # Prepare condition_id as bytes32
            condition_bytes = bytes.fromhex(
                condition_id[2:] if condition_id.startswith("0x") else condition_id
            )
            parent_bytes = bytes.fromhex(PARENT_COLLECTION_ID[2:])
            
            # Build transaction
            tx = self.ctf.functions.mergePositions(
                self.usdc_address,  # collateralToken
                parent_bytes,       # parentCollectionId
                condition_bytes,    # conditionId
                BINARY_PARTITION,   # partition [1, 2]
                merge_amount,       # amount
            ).build_transaction({
                "from": self.address,
                "nonce": self.w3.eth.get_transaction_count(self.address),
                "gas": 200000,  # Estimate, will be refined
                "gasPrice": gas_price,
            })
            
            # Estimate gas
            try:
                estimated_gas = self.w3.eth.estimate_gas(tx)
                tx["gas"] = int(estimated_gas * 1.2)  # 20% buffer
            except Exception as e:
                logger.warning("Gas estimation failed", error=str(e))
            
            # Sign and send
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            
            logger.info(
                "Merge transaction sent",
                tx_hash=tx_hash.hex(),
                amount=merge_amount,
                condition_id=condition_id,
            )
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            
            if receipt["status"] == 1:
                # USDC has 6 decimals
                amount_usdc = Decimal(merge_amount) / Decimal(10**6)
                
                logger.info(
                    "Merge successful",
                    tx_hash=tx_hash.hex(),
                    gas_used=receipt["gasUsed"],
                    amount_usdc=str(amount_usdc),
                )
                
                return MergeResult(
                    success=True,
                    tx_hash=tx_hash.hex(),
                    gas_used=receipt["gasUsed"],
                    amount_returned=amount_usdc,
                )
            else:
                return MergeResult(
                    success=False,
                    tx_hash=tx_hash.hex(),
                    error="Transaction reverted",
                )
                
        except Exception as e:
            logger.error("Merge failed", error=str(e))
            return MergeResult(
                success=False,
                error=str(e),
            )
    
    def get_usdc_balance(self) -> Decimal:
        """Get USDC balance in wallet."""
        raw_balance = self.usdc.functions.balanceOf(self.address).call()
        return Decimal(raw_balance) / Decimal(10**6)


# Convenience function for standalone use
async def merge_positions(
    web3: Web3,
    private_key: str,
    condition_id: str,
    amount: Optional[int] = None,
) -> MergeResult:
    """
    Merge positions for a market.
    
    Convenience wrapper around CTFContract.merge_positions().
    """
    ctf = CTFContract(web3, private_key)
    return await ctf.merge_positions(condition_id, amount)
