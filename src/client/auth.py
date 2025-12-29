"""
Authentication module for Polymarket CLOB API.

Handles L1/L2 authentication and API credential management.
"""

import os
from dataclasses import dataclass
from typing import Optional

from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from src.config import Endpoints, TradingConfig, get_settings
from src.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class AuthenticatedClient:
    """Container for authenticated CLOB client and metadata."""

    client: ClobClient
    eoa_address: str
    proxy_address: str
    api_creds: ApiCreds


class AuthenticationError(Exception):
    """Raised when authentication fails."""

    pass


def derive_eoa_address(private_key: str) -> str:
    """
    Derive the EOA (Externally Owned Account) address from a private key.

    Args:
        private_key: Hex-encoded private key starting with '0x'

    Returns:
        Checksummed Ethereum address

    Raises:
        AuthenticationError: If the private key is invalid
    """
    try:
        account = Account.from_key(private_key)
        return account.address
    except Exception as e:
        raise AuthenticationError(f"Invalid private key: {e}") from e


def create_clob_client(
    private_key: str,
    proxy_address: str,
    chain_id: int = 137,
    host: str = Endpoints.CLOB_HOST,
) -> ClobClient:
    """
    Create and initialize a CLOB client.

    Args:
        private_key: EOA private key for signing
        proxy_address: Polymarket proxy wallet address
        chain_id: Blockchain chain ID (137 for Polygon mainnet)
        host: CLOB API host URL

    Returns:
        Initialized ClobClient instance
    """
    client = ClobClient(
        host=host,
        key=private_key,
        chain_id=chain_id,
        signature_type=TradingConfig.SIGNATURE_TYPE_POLY,
        funder=proxy_address,
    )
    return client


def derive_api_credentials(client: ClobClient) -> ApiCreds:
    """
    Derive or create L2 API credentials from the client's private key.

    The CLOB API uses a two-level authentication system:
    - L1: Direct private key signatures (for creating API keys)
    - L2: API key/secret/passphrase (for trading operations)

    This function derives L2 credentials deterministically from the L1 key.

    Args:
        client: Initialized ClobClient with private key

    Returns:
        ApiCreds containing api_key, api_secret, and api_passphrase

    Raises:
        AuthenticationError: If credential derivation fails
    """
    try:
        creds = client.create_or_derive_api_creds()
        logger.info(
            "API credentials derived",
            api_key_prefix=creds.api_key[:8] + "...",
        )
        return creds
    except Exception as e:
        raise AuthenticationError(f"Failed to derive API credentials: {e}") from e


def authenticate() -> AuthenticatedClient:
    """
    Perform full authentication flow and return an authenticated client.

    This is the main entry point for authentication. It:
    1. Loads settings from environment
    2. Validates the private key
    3. Creates the CLOB client
    4. Derives API credentials
    5. Sets up the client for trading

    Returns:
        AuthenticatedClient with fully configured client and metadata

    Raises:
        AuthenticationError: If any authentication step fails
    """
    settings = get_settings()

    logger.info("Starting authentication flow")

    # Step 1: Derive EOA address from private key
    eoa_address = derive_eoa_address(settings.private_key)
    logger.info("EOA address derived", eoa_address=eoa_address)

    # Step 2: Create CLOB client
    client = create_clob_client(
        private_key=settings.private_key,
        proxy_address=settings.proxy_address,
        chain_id=settings.chain_id,
    )
    logger.info("CLOB client created", proxy_address=settings.proxy_address)

    # Step 3: Test connection
    try:
        server_time = client.get_server_time()
        logger.debug("Server connection verified", server_time=server_time)
    except Exception as e:
        raise AuthenticationError(f"Failed to connect to CLOB server: {e}") from e

    # Step 4: Derive API credentials
    api_creds = derive_api_credentials(client)
    client.set_api_creds(api_creds)

    logger.info(
        "Authentication successful",
        eoa_address=eoa_address,
        proxy_address=settings.proxy_address,
    )

    return AuthenticatedClient(
        client=client,
        eoa_address=eoa_address,
        proxy_address=settings.proxy_address,
        api_creds=api_creds,
    )


def authenticate_with_explicit_creds(
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    private_key: Optional[str] = None,
    proxy_address: Optional[str] = None,
) -> AuthenticatedClient:
    """
    Authenticate using explicit API credentials instead of deriving them.

    Use this when you have pre-generated API credentials and want to skip
    the derivation step.

    Args:
        api_key: L2 API key
        api_secret: L2 API secret
        api_passphrase: L2 API passphrase
        private_key: Optional private key (uses settings if not provided)
        proxy_address: Optional proxy address (uses settings if not provided)

    Returns:
        AuthenticatedClient with configured client

    Raises:
        AuthenticationError: If authentication fails
    """
    settings = get_settings()

    pk = private_key or settings.private_key
    proxy = proxy_address or settings.proxy_address

    eoa_address = derive_eoa_address(pk)

    client = create_clob_client(
        private_key=pk,
        proxy_address=proxy,
        chain_id=settings.chain_id,
    )

    api_creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )
    client.set_api_creds(api_creds)

    logger.info(
        "Authentication with explicit credentials successful",
        eoa_address=eoa_address,
    )

    return AuthenticatedClient(
        client=client,
        eoa_address=eoa_address,
        proxy_address=proxy,
        api_creds=api_creds,
    )
