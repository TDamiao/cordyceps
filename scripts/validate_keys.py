#!/usr/bin/env python3
"""
Polymarket API Key Validation Script

This script validates your .env configuration and tests the connection
to Polymarket's CLOB API.

Usage:
    python scripts/validate_keys.py
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


def print_header(title: str) -> None:
    """Print a formatted header."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_result(name: str, success: bool, message: str = "") -> None:
    """Print a test result."""
    status = "✅" if success else "❌"
    print(f"  {status} {name}")
    if message:
        print(f"     └─ {message}")


def validate_env_vars() -> bool:
    """Validate required environment variables."""
    import os

    print_header("Step 1: Checking Environment Variables")

    private_key = os.getenv("PRIVATE_KEY", "")
    proxy_address = os.getenv("PROXY_ADDRESS", "")

    all_valid = True

    # Check PRIVATE_KEY
    if not private_key:
        print_result("PRIVATE_KEY", False, "Not set in .env")
        all_valid = False
    elif not private_key.startswith("0x"):
        print_result("PRIVATE_KEY", False, "Must start with '0x'")
        all_valid = False
    # elif len(private_key) != 66:
        # print_result("PRIVATE_KEY", False, f"Invalid length: {len(private_key)} (expected 66)")
        # all_valid = False
    else:
        print_result("PRIVATE_KEY", True, f"Format valid (0x...{private_key[-4:]})")

    # Check PROXY_ADDRESS
    if not proxy_address:
        print_result("PROXY_ADDRESS", False, "Not set in .env")
        all_valid = False
    elif not proxy_address.startswith("0x"):
        print_result("PROXY_ADDRESS", False, "Must start with '0x'")
        all_valid = False
    elif len(proxy_address) != 42:
        print_result("PROXY_ADDRESS", False, f"Invalid length: {len(proxy_address)} (expected 42)")
        all_valid = False
    else:
        print_result("PROXY_ADDRESS", True, f"{proxy_address[:6]}...{proxy_address[-4:]}")

    return all_valid


def validate_wallet_derivation() -> tuple[bool, str | None]:
    """Validate that we can derive the wallet address from the private key."""
    import os

    from eth_account import Account

    print_header("Step 2: Validating Private Key")

    private_key = os.getenv("PRIVATE_KEY", "")

    try:
        account = Account.from_key(private_key)
        print_result("Wallet Derivation", True, f"EOA Address: {account.address}")
        return True, account.address
    except Exception as e:
        print_result("Wallet Derivation", False, str(e))
        return False, None


def validate_clob_connection() -> bool:
    """Test connection to Polymarket CLOB API."""
    import os

    from py_clob_client.client import ClobClient

    print_header("Step 3: Testing CLOB API Connection")

    private_key = os.getenv("PRIVATE_KEY", "")
    proxy_address = os.getenv("PROXY_ADDRESS", "")
    chain_id = int(os.getenv("CHAIN_ID", "137"))

    try:
        # Initialize client
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=chain_id,
            signature_type=1,  # Proxy wallet
            funder=proxy_address,
        )
        print_result("Client Initialized", True)

        # Test public endpoint - get server time
        server_time = client.get_server_time()
        print_result("Server Connection", True, f"Server time: {server_time}")

        return True

    except Exception as e:
        print_result("CLOB Connection", False, str(e))
        return False


def validate_api_credentials() -> bool:
    """Test API credential derivation."""
    import os

    from py_clob_client.client import ClobClient

    print_header("Step 4: Deriving API Credentials")

    private_key = os.getenv("PRIVATE_KEY", "")
    proxy_address = os.getenv("PROXY_ADDRESS", "")
    chain_id = int(os.getenv("CHAIN_ID", "137"))

    try:
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=chain_id,
            signature_type=1,
            funder=proxy_address,
        )

        # Derive or create API credentials
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        print_result("API Key Derived", True, f"Key: {creds.api_key[:8]}...")
        print_result("API Secret", True, "Successfully derived")
        print_result("API Passphrase", True, "Successfully derived")

        return True

    except Exception as e:
        print_result("API Credentials", False, str(e))
        return False


def validate_market_access() -> bool:
    """Test fetching market data."""
    import os

    from py_clob_client.client import ClobClient

    print_header("Step 5: Testing Market Data Access")

    private_key = os.getenv("PRIVATE_KEY", "")
    proxy_address = os.getenv("PROXY_ADDRESS", "")
    chain_id = int(os.getenv("CHAIN_ID", "137"))

    try:
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=chain_id,
            signature_type=1,
            funder=proxy_address,
        )

        # Derive credentials
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        # Fetch some markets
        markets = client.get_markets()

        if markets:
            print_result("Market Access", True, f"Successfully retrieved {len(markets)} markets")
        else:
            print_result("Market Access", True, "API accessible (no active markets found)")

        return True

    except Exception as e:
        print_result("Market Access", False, str(e))
        return False


def main() -> None:
    """Run all validation checks."""
    print("\n" + "🔐 POLYMARKET API KEY VALIDATOR 🔐".center(60))

    results = []

    # Step 1: Environment variables
    results.append(("Environment Variables", validate_env_vars()))

    if not results[-1][1]:
        print("\n❌ Fix your .env file before continuing.\n")
        sys.exit(1)

    # Step 2: Wallet derivation
    success, _ = validate_wallet_derivation()
    results.append(("Wallet Derivation", success))

    if not success:
        print("\n❌ Invalid private key. Check your .env file.\n")
        sys.exit(1)

    # Step 3: CLOB connection
    results.append(("CLOB Connection", validate_clob_connection()))

    # Step 4: API credentials
    results.append(("API Credentials", validate_api_credentials()))

    # Step 5: Market access
    results.append(("Market Access", validate_market_access()))

    # Summary
    print_header("Summary")
    all_passed = all(r[1] for r in results)

    for name, passed in results:
        print_result(name, passed)

    if all_passed:
        print("\n✅ All checks passed! Your configuration is ready.\n")
        print("Next steps:")
        print("  1. Ensure your proxy wallet has USDC on Polygon")
        print("  2. Run the bot in dry-run mode first:")
        print("     python -m src.main --dry-run\n")
    else:
        print("\n⚠️  Some checks failed. Review the errors above.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
