# Cordyceps – Polymarket Arbitrage Engine

Automated microstructure arbitrage engine for Polymarket's Central Limit Order Book (CLOB), leveraging the Gnosis Conditional Token Framework (CTF).

## What It Does

Cordyceps exploits structural inefficiencies in binary prediction markets by detecting when the sum of outcome prices deviates from \$1.00:

| Strategy | Trigger | Action |
|----------|---------|--------|
| **Buy Set** | Σ ask < \$1.00 | Buy all outcomes cheap → merge for \$1.00 |
| **Sell Set** | Σ bid > \$1.00 | Split \$1.00 → sell outcomes for premium |

## Architecture

```
WebSocket Feed → State Manager → Depth-Aware Engine → Executor → Settlement
       │               │                 │                │            │
       └── Market WS ──┘                 └── VWAP calc    └── FOK     └── Merge
```

- **Market Observer** – real-time order book mirroring via WebSocket
- **Arbitrage Engine** – depth-aware VWAP calculation across price levels
- **Risk Manager** – circuit breaker, daily loss limits, slippage guards
- **Paper Simulator** – simulated execution mode with configurable failure injection
- **Settlement Agent** – atomic CTF merge for capital recycling

## Quick Start

### Prerequisites

- Python 3.11+ (or Docker)
- Polymarket account with proxy wallet
- USDC on Polygon

### Install

```bash
pip install -e ".[dev]"
```

### Configure

```bash
cp .env.example .env
# Edit .env with your credentials
```

### Run

```bash
# Paper mode (default, safe)
python -m uvicorn src.api_server:app --reload

# Live mode
TRADING_MODE=live LIVE_TRADING_ENABLED=true DRY_RUN=false \
  PRIVATE_KEY=0x... PROXY_ADDRESS=0x... \
  python -m uvicorn src.api_server:app
```

## Docker

### Quick Start

```bash
# Start with PostgreSQL
cp .env.example .env
docker compose up -d

# Verify
curl http://localhost:8000/health
```

### Production Deploy

```bash
# Set strong passwords
export POSTGRES_PASSWORD=$(openssl rand -hex 16)
export PRIVATE_KEY=0x...
export PROXY_ADDRESS=0x...

# Build and start
docker compose up -d --build

# Monitor
docker compose logs -f bot
curl http://localhost:8000/health
```

### Image Features

| Feature | Detail |
|---------|--------|
| Base | `python:3.13-slim` |
| User | Non-root `botuser` |
| Healthcheck | `/health` endpoint every 30s |
| Volumes | `bot_data`, `bot_logs`, `pgdata` |
| Resources | CPU/memory limits via Compose deploy |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `LIVE_TRADING_ENABLED` | `false` | Must be `true` for live mode |
| `PRIVATE_KEY` | – | EOA private key (required for live) |
| `PROXY_ADDRESS` | – | Polymarket proxy wallet |
| `DATABASE_URL` | `sqlite:///./cordyceps.db` | PostgreSQL for production |
| `MAX_TRADE_USD` | `1.0` | Max USDC per trade |
| `MAX_DAILY_LOSS_USD` | `1.0` | Daily loss limit |
| `ORDERBOOK_STALE_MS` | `3000` | Reject stale order books |

## Safety Features

- **Kill Switch** – `KILL_SWITCH=true` pauses all trading
- **Paper/Live Guards** – live mode requires explicit opt-in flags
- **Circuit Breaker** – pauses after N consecutive failures
- **Stale Book Detection** – rejects data older than threshold
- **Leg Risk Buffer** – minimum edge required after fees

## API

```
GET /health   →  mode, database, websocket, scanner status
GET /status   →  lightweight liveness probe
```

## Tests

```bash
# Run full suite
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=src --cov-report=term-missing
```

### Test Coverage

| Area | Test Class / File |
|------|-------------------|
| VWAP depth analysis | `test_depth_aware.py::TestBuySetDepthAnalysis`, `TestSellSetDepthAnalysis` |
| Minimum order / liquidity | `test_config_guards.py::TestMinTradeAndStaleGuards` |
| Max trade size / position | `test_config_guards.py::TestTradeSizeLimits`, `TestEngineRespectsLimits` |
| Stale book rejection | `test_config_guards.py::TestEngineRespectsLimits`, `test_depth_aware.py` |
| Partial fill | `test_depth_aware.py::TestPaperSimulatorLegFailure` |
| Leg failure injection | `test_depth_aware.py::TestPaperSimulatorLegFailure` |
| Kill switch | `test_config_guards.py::TestKillSwitch` |
| Paper/live guards | `test_config_guards.py::TestPaperLiveGuards` |
| Circuit breaker | `test_config_guards.py::TestRiskManagerCircuitBreaker` |
| Leg risk buffer | `test_depth_aware.py::TestLegRisk` |
| Fees per leg | `test_depth_aware.py::TestLegRisk` |
| Paper simulator logging | `test_depth_aware.py::TestPaperSimulatorLogging` |

## CI/CD

GitHub Actions workflow (`.github/workflows/ci.yml`):

1. **Lint** – `ruff check` + `black --check`
2. **Test** – `pytest` across Python 3.11, 3.12, 3.13 with coverage
3. **Docker Build** – Buildx with GHA cache, smoke-test health endpoint

## Project Structure

```
cordyceps/
├── src/
│   ├── config.py           # Settings with paper/live guards
│   ├── main.py             # ArbitrageBot orchestrator
│   ├── api_server.py       # FastAPI health endpoint
│   ├── paper_engine.py     # Paper trading engine
│   ├── scanner.py          # Market discovery loop
│   ├── database.py         # SQLAlchemy models
│   ├── client/             # CLOB API client
│   ├── engine/             # VWAP depth-aware detector
│   ├── execution/          # Order executor + paper simulator
│   ├── observer/           # WebSocket + state manager
│   ├── settlement/         # CTF merge agent
│   ├── risk/               # Circuit breaker + loss limits
│   └── utils/              # Logging, metrics, health
├── tests/                  # pytest suite (39+ tests)
├── docs/DOCKPLOY.md        # Deployment guide
├── Dockerfile              # Slim non-root production image
├── docker-compose.yml      # Bot + PostgreSQL with resource limits
├── .dockerignore           # Build context exclusions
├── .github/workflows/      # CI: lint, test, docker build
└── .env.example            # Template for environment config
```

## Disclaimer

This software is for educational purposes. Trading on prediction markets involves risk. Use at your own discretion.

## License

MIT
