"""
Contract addresses for Polymarket on Polygon.

These are the official contract addresses from Polymarket's documentation.
"""

# Polygon Mainnet (Chain ID: 137)

# Conditional Token Framework (CTF) - Gnosis
# Used for splitPositions and mergePositions
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# USDC on Polygon (PoS)
# This is the collateral token for all Polymarket positions
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Neg Risk CTF Exchange (for neg-risk markets)
NEG_RISK_CTF_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# Neg Risk Adapter
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# Standard CTF Exchange
CTF_EXCHANGE_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Polymarket Relayer (for gasless transactions)
POLYMARKET_RELAYER = "0x..." # TODO: Get from Polymarket docs

# Binary partition for YES/NO markets
# YES = 0b01 = 1, NO = 0b10 = 2
BINARY_PARTITION = [1, 2]

# Zero bytes32 (parent collection ID for root positions)
PARENT_COLLECTION_ID = "0x0000000000000000000000000000000000000000000000000000000000000000"
