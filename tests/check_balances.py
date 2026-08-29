#!/usr/bin/env python3
"""
Wallet Balance Checker.

Verifies the balances of both EOA (Signer) and Proxy (Trader) wallets.
"""

import os
import sys
from decimal import Decimal

from web3 import Web3

# Add project root to path
sys.path.append(os.getcwd())

from src.config import get_settings
from src.contracts.ctf import ERC20_ABI, USDC_ADDRESS


def check_balances():
    print("🔍 Checking Wallet Balances...")

    settings = get_settings()
    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url))

    if not w3.is_connected():
        print("❌ Failed to connect to Polygon RPC")
        return

    # 1. EOA Details
    account = w3.eth.account.from_key(settings.private_key)
    eoa_address = account.address

    # 2. Proxy Details
    proxy_address = settings.proxy_address

    # helper to get USDC
    usdc_contract = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)

    def get_matic(addr):
        return w3.from_wei(w3.eth.get_balance(addr), "ether")

    def get_usdc(addr):
        bal = usdc_contract.functions.balanceOf(addr).call()
        return Decimal(bal) / Decimal(10**6)

    # Fetch Balances
    eoa_matic = get_matic(eoa_address)
    eoa_usdc = get_usdc(eoa_address)

    proxy_matic = get_matic(proxy_address)
    proxy_usdc = get_usdc(proxy_address)

    print("\n" + "=" * 50)
    print(f"👤 EOA (Signer): {eoa_address}")
    print(f"   MATIC: {eoa_matic:,.4f}  (Pays Gas)")
    print(f"   USDC:  {eoa_usdc:,.2f}")

    if eoa_matic < 1:
        print("   ⚠️  LOW MATIC! Send at least 5 MATIC to this address for gas.")
    else:
        print("   ✅ Gas funds look good.")

    print("-" * 50)
    print(f"🤖 Proxy (Trader): {proxy_address}")
    print(f"   MATIC: {proxy_matic:,.4f}")
    print(f"   USDC:  {proxy_usdc:,.2f}  (Trading Capital)")

    if proxy_usdc < 10:
        print("   ⚠️  LOW USDC! Send USDC (Polygon) here to trade.")
    else:
        print("   ✅ Trading funds look good.")

    print("=" * 50 + "\n")


if __name__ == "__main__":
    check_balances()
