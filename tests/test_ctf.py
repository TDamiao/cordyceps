"""
Tests for CTF contract wrapper.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.contracts.ctf import CTFContract

# Sample binary market
CONDITION_ID = "0x4b22fea47c3789b7e4d8fb5d2b3c20c025555555555555555555555555555555"
BINARY_PARTITION = [1, 2]


@pytest.fixture
def mock_web3():
    w3 = MagicMock()  # Remove spec=Web3 to avoid strict spec issues with dynamic attrs if any

    # Setup eth namespace
    w3.eth = MagicMock()
    w3.eth.contract = MagicMock()
    w3.eth.gas_price = 30000000000  # 30 gwei

    # Mock to_wei
    def mock_to_wei(val, unit):
        if unit == "gwei":
            return int(val * 10**9)
        return val

    w3.to_wei.side_effect = mock_to_wei

    # Mock account
    mock_account = MagicMock()
    mock_account.address = "0xUserAddress"
    w3.eth.account.from_key.return_value = mock_account

    # Mock transaction functions
    w3.eth.get_transaction_count.return_value = 1
    # Return 32 bytes for hash (0x1111...)
    tx_hash_bytes = b"\x11" * 32
    w3.eth.send_raw_transaction.return_value = tx_hash_bytes
    w3.eth.wait_for_transaction_receipt.return_value = {"status": 1, "gasUsed": 100000}
    w3.eth.estimate_gas.return_value = 150000

    # Mock keccak to return bytes
    w3.keccak.side_effect = lambda x: b"\x01" * 32
    # But wait, CTF wrapper accesses Web3.keccak (static) or self.w3.keccak?
    # In ctf.py: return Web3.keccak(packed) -> This calls static method on class

    return w3


@pytest.fixture
def mock_ctf_contract():
    contract = MagicMock()
    contract.functions.balanceOf.return_value.call.return_value = 20000000  # 20 shares
    contract.functions.mergePositions.return_value.build_transaction.return_value = {
        "to": "0xCTF",
        "data": "0x...",
        "gas": 200000,
    }
    return contract


@pytest.fixture
def ctf_wrapper(mock_web3, mock_ctf_contract):
    mock_web3.eth.contract.return_value = mock_ctf_contract
    wrapper = CTFContract(mock_web3, "0x" + "1" * 64)
    # Ensure initialized contracts are set
    wrapper.ctf = mock_ctf_contract
    wrapper.usdc = MagicMock()
    return wrapper


def test_can_merge(ctf_wrapper, mock_ctf_contract):
    """Test merge capability checking."""
    # YES balance = 20, NO balance = 20
    mock_ctf_contract.functions.balanceOf.return_value.call.return_value = 20

    can_merge, amount = ctf_wrapper.can_merge(CONDITION_ID)

    assert can_merge is True
    assert amount == 20


def test_can_merge_imbalanced(ctf_wrapper, mock_ctf_contract):
    """Test merge capability with unequal balances."""
    # Return different balances for different calls
    mock_ctf_contract.functions.balanceOf.return_value.call.side_effect = [30, 10]

    can_merge, amount = ctf_wrapper.can_merge(CONDITION_ID)

    assert can_merge is True
    assert amount == 10  # Min of 30 and 10


def test_cannot_merge(ctf_wrapper, mock_ctf_contract):
    """Test when merge is not possible."""
    # YES balance = 10, NO balance = 0
    mock_ctf_contract.functions.balanceOf.return_value.call.side_effect = [10, 0]

    can_merge, amount = ctf_wrapper.can_merge(CONDITION_ID)

    assert can_merge is False
    assert amount == 0


@pytest.mark.asyncio
async def test_merge_positions_success(ctf_wrapper, mock_ctf_contract):
    """Test successful merge execution."""
    # Mock successful balance check
    ctf_wrapper.can_merge = MagicMock(return_value=(True, 2000000))  # 2 shares (USDC 6 decimals)

    result = await ctf_wrapper.merge_positions(CONDITION_ID, amount=2000000)

    assert result.success is True
    assert result.tx_hash == "11" * 32  # Hex string of 32 bytes of 0x11
    assert result.amount_returned == Decimal("2")  # 2000000 / 10^6

    # Verify contract call
    mock_ctf_contract.functions.mergePositions.assert_called()


@pytest.mark.asyncio
async def test_merge_positions_gas_too_high(ctf_wrapper, mock_web3):
    """Test merge aborted due to high gas."""
    mock_web3.eth.gas_price = 200000000000  # 200 gwei

    # Mock successful balance check
    ctf_wrapper.can_merge = MagicMock(return_value=(True, 10))

    await ctf_wrapper.merge_positions(CONDITION_ID, max_gas_price_gwei=100)


@pytest.mark.asyncio
async def test_merge_positions_with_gas_override(ctf_wrapper, mock_ctf_contract, mock_web3):
    """Test merge with explicit gas price override."""
    # Mock successful balance check
    ctf_wrapper.can_merge = MagicMock(return_value=(True, 100))

    # Base gas is 30 gwei, we override with 50 gwei
    override_gas = int(50 * 10**9)

    result = await ctf_wrapper.merge_positions(
        CONDITION_ID, amount=100, gas_price_wei_override=override_gas
    )

    assert result.success is True

    # Extract the actual call arguments to build_transaction to verify gasPrice
    # inner call is: ctf.functions.mergePositions(...).build_transaction({ ... })

    # We need to find the mocks.
    # mock_ctf_contract.functions.mergePositions.return_value is the "ContractFunction" mock
    contract_function = mock_ctf_contract.functions.mergePositions.return_value

    # Verify build_transaction was called with our override
    call_kwargs = contract_function.build_transaction.call_args[0][0]
    assert call_kwargs["gasPrice"] == override_gas
    assert call_kwargs["gasPrice"] != mock_web3.eth.gas_price
