import os
import sys

from py_clob_client.client import ClobClient

sys.path.append(os.getcwd())
from src.config import get_settings


def check_proxy_ownership():
    settings = get_settings()

    print(f"Checking Proxy for EOA: {settings.private_key[:6]}...{settings.private_key[-4:]}")

    # Initialize client WITHOUT a proxy first, to see if it can find one or derive one
    host = "https://clob.polymarket.com/"
    key = settings.private_key
    chain_id = 137

    client = ClobClient(
        host=host,
        key=key,
        chain_id=chain_id,
        signature_type=1, # POLYGON
    )

    try:
        # Ask API for the proxy associated with this EOA
        # The library usually does this internally or we can try to derive credentials
        # create_or_derive_api_creds usually triggers proxy check
        client.create_or_derive_api_creds()
        print("✅ Credentials derived successfully.")

        # Checking computed proxy...
        # The ClobClient doesn't expose 'get_proxy' easily without making a request
        # But we can check what the user Configured vs what the API expects

        print(f"Configured Proxy in .env: {settings.proxy_address}")

    except Exception as e:
        print(f"❌ Error deriving credentials: {e}")
        print("This usually means the EOA has no Proxy or the API is rejecting it.")

if __name__ == "__main__":
    check_proxy_ownership()
