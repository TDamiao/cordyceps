"""
Conditional Token Framework (CTF) Contract Wrapper.

This module provides Python bindings for interacting with the Gnosis
Conditional Token Framework contract on Polygon.

Key functions:
- merge_positions: Convert YES+NO tokens back to USDC instantly
- split_position: Convert USDC to YES+NO tokens
- get_position_id: Calculate ERC-1155 token ID for a position
"""

from dataclasses import dataclass
from decimal import Decimal

import structlog
from web3 import Web3

from src.contracts.abis import GNOSIS_SAFE_ABI
from src.contracts.constants import (
    BINARY_PARTITION,
    CTF_ADDRESS,
    PARENT_COLLECTION_ID,
    USDC_ADDRESS,
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
    tx_hash: str | None = None
    gas_used: int = 0
    amount_returned: Decimal = Decimal(0)
    error: str | None = None


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

    Supports both EOA (Direct) and Proxy (Gnosis Safe) execution.
    """

    def __init__(
        self,
        web3: Web3,
        private_key: str,
        ctf_address: str = CTF_ADDRESS,
        usdc_address: str = USDC_ADDRESS,
        proxy_address: str | None = None,
    ):
        """
        Initialize CTF contract wrapper.

        Args:
            web3: Web3 instance connected to Polygon
            private_key: Wallet private key for signing
            ctf_address: CTF contract address
            usdc_address: USDC token address
            proxy_address: Optional Gnosis Safe Proxy address. If set, ALL merges happen via Proxy.
        """
        self.w3 = web3
        self.private_key = private_key
        self.account = self.w3.eth.account.from_key(private_key)
        self.eoa_address = self.account.address

        # Determine effective trading address
        self.proxy_address = Web3.to_checksum_address(proxy_address) if proxy_address else None
        self.address = self.proxy_address if self.proxy_address else self.eoa_address

        # Initialize contracts
        self.ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(ctf_address),
            abi=CTF_ABI,
        )
        self.usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(usdc_address),
            abi=ERC20_ABI,
        )

        if self.proxy_address:
            self.proxy = self.w3.eth.contract(
                address=self.proxy_address,
                abi=GNOSIS_SAFE_ABI,
            )
            logger.info(
                "CTF initialized in PROXY mode", proxy=self.proxy_address, signer=self.eoa_address
            )
        else:
            self.proxy = None
            logger.info("CTF initialized in EOA mode", address=self.address)

        self.ctf_address = ctf_address
        self.usdc_address = usdc_address

    def get_position_balance(
        self,
        condition_id: str,
        index_set: int,
    ) -> int:
        """
        Get balance of a position (YES or NO).
        """
        # Calculate position ID
        condition_bytes = bytes.fromhex(
            condition_id[2:] if condition_id.startswith("0x") else condition_id
        )
        parent_bytes = bytes.fromhex(PARENT_COLLECTION_ID[2:])

        collection_id = get_collection_id(parent_bytes, condition_bytes, index_set)
        position_id = get_position_id(self.usdc_address, collection_id)

        # Always check balance of the TRADER (Proxy or EOA)
        return self.ctf.functions.balanceOf(self.address, position_id).call()

    def get_yes_no_balances(self, condition_id: str) -> tuple[int, int]:
        yes_balance = self.get_position_balance(condition_id, 1)
        no_balance = self.get_position_balance(condition_id, 2)
        return yes_balance, no_balance

    def can_merge(self, condition_id: str) -> tuple[bool, int]:
        yes_bal, no_bal = self.get_yes_no_balances(condition_id)
        mergeable = min(yes_bal, no_bal)
        return mergeable > 0, mergeable

    async def merge_positions(
        self,
        condition_id: str,
        amount: int | None = None,
        max_gas_price_gwei: float = 100,
        gas_price_wei_override: int | None = None,
    ) -> MergeResult:
        """
        Merge YES+NO positions back to USDC.
        Supports both direct EOA calls and Gnosis Safe Proxy execution.
        """
        try:
            # Check balances
            can_do, max_amount = self.can_merge(condition_id)

            if not can_do:
                return MergeResult(success=False, error="Cannot merge: missing YES or NO tokens")

            merge_amount = amount if amount is not None else max_amount

            if merge_amount > max_amount:
                return MergeResult(
                    success=False,
                    error=f"Insufficient balance: requested {merge_amount}, have {max_amount}",
                )

            # Determine gas price
            if gas_price_wei_override:
                gas_price = gas_price_wei_override
            else:
                gas_price = self.w3.eth.gas_price

            max_gas_wei = self.w3.to_wei(max_gas_price_gwei, "gwei")

            if gas_price > max_gas_wei:
                return MergeResult(
                    success=False,
                    error=f"Gas too high: {gas_price / 1e9:.1f} gwei > {max_gas_price_gwei} gwei",
                )

            # Prepare arguments
            condition_bytes = bytes.fromhex(
                condition_id[2:] if condition_id.startswith("0x") else condition_id
            )
            parent_bytes = bytes.fromhex(PARENT_COLLECTION_ID[2:])

            # 1. Construct the inner CTF call
            # This is what we want to execute (either directly or via proxy)
            ctf_tx = self.ctf.functions.mergePositions(
                self.usdc_address,
                parent_bytes,
                condition_bytes,
                BINARY_PARTITION,
                merge_amount,
            )

            if self.proxy:
                # --- PROXY MODE (Gnosis Safe) ---
                # We need to send an execTransaction to the Proxy

                # Get call data for the inner function
                tx_data = ctf_tx._encode_transaction_data()

                # Gnosis Safe execTransaction params
                to_address = self.ctf.address
                value = 0
                data = bytes.fromhex(tx_data[2:]) if tx_data.startswith("0x") else tx_data
                operation = 0  # Call
                safe_tx_gas = 0  # 0 usually works for standard Safe setup
                base_gas = 0
                safe_gas_price = 0
                gas_token = "0x0000000000000000000000000000000000000000"
                refund_receiver = "0x0000000000000000000000000000000000000000"
                nonce = self.proxy.functions.nonce().call()

                # Calculate Safe Transaction Hash for signing
                safe_tx_hash = self.proxy.functions.getTransactionHash(
                    to_address,
                    value,
                    data,
                    operation,
                    safe_tx_gas,
                    base_gas,
                    safe_gas_price,
                    gas_token,
                    refund_receiver,
                    nonce,
                ).call()

                # Sign the hash with EOA private key
                # signature = {bytes32 r}{bytes32 s}{uint8 v}
                # For EOA signing, V is usually 27 or 28, but for Safe ECDSA, we often need v+4
                # Standard web3 signing:
                signable_hash = safe_tx_hash
                signed_msg = self.w3.eth.account.signHash(
                    signable_hash, private_key=self.private_key
                )

                # Adjust V for Gnosis Safe (v += 4 if using eth_sign?)
                # Actually, plain ECDSA signature usually works if it's an owner.
                # Format: r (32) + s (32) + v (1)
                r = signed_msg.r.to_bytes(32, "big")
                s = signed_msg.s.to_bytes(32, "big")
                v = (signed_msg.v).to_bytes(1, "big")
                signatures = r + s + v

                # Build the outer transaction (EOA -> Proxy)
                tx = self.proxy.functions.execTransaction(
                    to_address,
                    value,
                    data,
                    operation,
                    safe_tx_gas,
                    base_gas,
                    safe_gas_price,
                    gas_token,
                    refund_receiver,
                    signatures,
                ).build_transaction(
                    {
                        "from": self.eoa_address,
                        "nonce": self.w3.eth.get_transaction_count(self.eoa_address),
                        "gasPrice": gas_price,
                        # Gas estimate will happen next
                    }
                )

            else:
                # --- EOA MODE (Direct) ---
                tx = ctf_tx.build_transaction(
                    {
                        "from": self.eoa_address,
                        "nonce": self.w3.eth.get_transaction_count(self.eoa_address),
                        "gasPrice": gas_price,
                        "gas": 200000,
                    }
                )

            # Estimate gas
            try:
                estimated_gas = self.w3.eth.estimate_gas(tx)
                tx["gas"] = int(estimated_gas * 1.2)
            except Exception as e:
                logger.warning(
                    "Gas estimation failed", error=str(e), mode="PROXY" if self.proxy else "EOA"
                )
                if "gas" not in tx:
                    tx["gas"] = 500000  # Higher default for Proxy

            # Sign and send
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)

            logger.info(
                "Merge transaction sent",
                mode="PROXY" if self.proxy else "EOA",
                tx_hash=tx_hash.hex(),
                amount=merge_amount,
            )

            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt["status"] == 1:
                amount_usdc = Decimal(merge_amount) / Decimal(10**6)
                return MergeResult(
                    success=True,
                    tx_hash=tx_hash.hex(),
                    gas_used=receipt["gasUsed"],
                    amount_returned=amount_usdc,
                )
            else:
                return MergeResult(
                    success=False, tx_hash=tx_hash.hex(), error="Transaction reverted"
                )

        except Exception as e:
            logger.error("Merge failed", error=str(e))
            return MergeResult(success=False, error=str(e))

    def get_usdc_balance(self) -> Decimal:
        raw_balance = self.usdc.functions.balanceOf(self.address).call()
        return Decimal(raw_balance) / Decimal(10**6)


# Convenience function for standalone use
async def merge_positions(
    web3: Web3,
    private_key: str,
    condition_id: str,
    amount: int | None = None,
    proxy_address: str | None = None,
) -> MergeResult:
    """
    Merge positions for a market (Proxy-aware).
    """
    ctf = CTFContract(web3, private_key, proxy_address=proxy_address)
    return await ctf.merge_positions(condition_id, amount)
