"""
Pytest configuration and shared fixtures.
"""

import os
import sys
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def setup_test_env():
    """Set up test environment variables."""
    # Only set if not already set (for integration tests)
    if not os.getenv("PRIVATE_KEY"):
        os.environ["PRIVATE_KEY"] = "0x" + "0" * 64  # Dummy key for unit tests
    if not os.getenv("PROXY_ADDRESS"):
        os.environ["PROXY_ADDRESS"] = "0x" + "0" * 40  # Dummy address


@pytest.fixture
def mock_settings():
    """Provide mock settings for testing."""
    from unittest.mock import MagicMock

    settings = MagicMock()
    settings.private_key = "0x" + "a" * 64
    settings.proxy_address = "0x" + "b" * 40
    settings.chain_id = 137
    settings.dry_run = True
    settings.log_level = "DEBUG"
    settings.log_format = "console"

    return settings
