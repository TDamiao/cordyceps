# Polymarket Arbitrage Bot

Automated Microstructure Arbitrage Engine on Gnosis CTF (Polymarket).

## Overview

This bot exploits structural inefficiencies in Polymarket's Central Limit Order Book (CLOB) using:
- **Atomic Unity Arbitrage** - Buy complete sets when `Σ Ask_i < 1.0`
- **Synthetic Minting** - Sell complete sets when `Σ Bid_i > 1.0`

## Quick Start

### Prerequisites

- Python 3.11+
- Conda or virtual environment
- Polymarket account with proxy wallet
- USDC on Polygon

### Installation

```bash
# Create and activate conda environment
conda create -n polym-env python=3.14
conda activate polym-env

# Install dependencies
pip install -e ".[dev]"
```

### Configuration

```bash
# Copy environment template
cp .env.example .env

# Edit with your credentials
# - PRIVATE_KEY: Your EOA private key
# - PROXY_ADDRESS: Your Polymarket proxy wallet address
```

### Usage

```bash
# Dry run mode (logs opportunities without trading)
python -m src.main --dry-run

# Live trading (USE WITH CAUTION!)
python -m src.main
```

## Project Structure

```
polymarket-bot/
├── src/
│   ├── config.py          # Configuration & constants
│   ├── main.py            # Entry point
│   ├── client/            # CLOB API client
│   ├── observer/          # WebSocket market observer
│   ├── engine/            # Arbitrage detection
│   ├── execution/         # Order execution
│   ├── settlement/        # CTF merge/split
│   ├── markets/           # Market data
│   └── utils/             # Logging & helpers
└── tests/                 # Test suite
```

## Documentation

See [AGENT.md](AGENT.md) for full technical documentation.

## ⚠️ Disclaimer

This software is for educational purposes. Trading on prediction markets involves risk. Use at your own discretion.

## License

MIT
