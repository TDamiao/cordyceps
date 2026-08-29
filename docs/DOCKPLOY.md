# Dockploy – Deployment Guide

This document describes how to deploy Cordyceps using Docker, Dockploy (self-hosted PaaS), or manual server setup.

## Docker Deployment

### Local Development

```bash
# Clone and configure
cp .env.example .env
# Edit .env with your credentials

# Start with PostgreSQL
docker compose up -d

# Verify
curl http://localhost:8000/health
```

### Production (single server)

```bash
# Set strong passwords
export POSTGRES_PASSWORD=$(openssl rand -hex 16)
export PRIVATE_KEY=0x...
export PROXY_ADDRESS=0x...

# Deploy
docker compose up -d --build

# Monitor
docker compose logs -f bot
docker compose exec postgres psql -U cordyceps -d cordyceps
```

### Health Check

The bot exposes a `/health` endpoint at `$PORT` (default 8000):

```json
{
  "mode": "paper",
  "database": "postgresql+psycopg://...",
  "websocket": {"connected": true, "status": "healthy"},
  "scanner": {"running": true, "tracked_markets": 42},
  "paper_engine": {"trade_count": 0, "total_profit": 0},
  "active_markets": 42,
  "running": true,
  "uptime": 3600.0
}
```

### Docker Image Details

| Property | Value |
|----------|-------|
| Base image | `python:3.13-slim` |
| Build | Multi-stage (builder → production) |
| User | Non-root `botuser` (UID created at build) |
| Healthcheck | `curl -sf http://localhost:8000/health` |
| Start period | 15 seconds |
| Volumes | `/app/data`, `/app/logs` |
| Build context | Excludes `.git/`, `.venv/`, `tests/`, docs via `.dockerignore` |

### Resource Limits

Both services have CPU and memory limits configured in `docker-compose.yml`:

| Service | CPU Limit | Memory Limit | CPU Reserve | Memory Reserve |
|---------|-----------|--------------|-------------|----------------|
| bot | 1.0 | 512M | 0.25 | 128M |
| postgres | 1.0 | 256M | 0.1 | 64M |

## Dockploy (Self-Hosted PaaS)

### Prerequisites

- Dockploy installed on a VPS (Hetzner, DigitalOcean, etc.)
- A domain name pointing to your server
- Docker installed

### Setup

1. **Add a new Application** in Dockploy:
   - Source: Git repository (`your-org/cordyceps`)
   - Buildpack: Dockerfile (target: `production`)
   - Port: 8000

2. **Add a PostgreSQL service**:
   - Image: `postgres:16-alpine`
   - Environment: `POSTGRES_USER=cordyceps`, `POSTGRES_PASSWORD=<random>`
   - Volume: `/var/lib/postgresql/data`

3. **Configure environment variables** on the bot service:
   ```
   TRADING_MODE=paper
   DATABASE_URL=postgresql+psycopg://cordyceps:<password>@postgres:5432/cordyceps
   PRIVATE_KEY=<your-key>
   PROXY_ADDRESS=<your-proxy>
   LIVE_TRADING_ENABLED=false
   PORT=8000
   ```

4. **Deploy** and verify health endpoint:
   ```bash
   curl https://your-domain.com/health
   ```

### Dockploy Environment Mapping

Dockploy injects environment variables directly. Map these:

| Dockploy Setting | Env Var |
|------------------|---------|
| App Port | `PORT` |
| Secret: Private Key | `PRIVATE_KEY` |
| Secret: Proxy Address | `PROXY_ADDRESS` |
| DB Host | Used in `DATABASE_URL` |
| DB Password | Used in `DATABASE_URL` |

## GCP Cloud Run

```bash
# Build and push to Artifact Registry
gcloud builds submit --tag us-central1-docker.pkg.dev/PROJECT/cordyceps/bot:latest

# Deploy to Cloud Run
gcloud run deploy cordyceps \
  --image us-central1-docker.pkg.dev/PROJECT/cordyceps/bot:latest \
  --port 8000 \
  --min-instances 0 \
  --max-instances 1 \
  --set-secrets="PRIVATE_KEY=PRIVATE_KEY:latest,PROXY_ADDRESS=PROXY_ADDRESS:latest" \
  --set-env-vars="TRADING_MODE=paper,DATABASE_URL=sqlite:///./cordyceps.db"
```

## Environment Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TRADING_MODE` | No | `paper` | `paper` or `live` |
| `LIVE_TRADING_ENABLED` | For live | `false` | Must be `true` for live mode |
| `KILL_SWITCH` | No | `false` | Pauses all trading |
| `PRIVATE_KEY` | For live | `""` | EOA private key |
| `PROXY_ADDRESS` | For live | `""` | Polymarket proxy wallet |
| `DATABASE_URL` | No | `sqlite:///./cordyceps.db` | PostgreSQL for production |
| `PORT` | No | `8000` | HTTP server port |
| `MAX_TRADE_USD` | No | `1.0` | Max USDC per trade |
| `MAX_TOTAL_EXPOSURE_USD` | No | `5.0` | Max total open exposure |
| `MAX_DAILY_LOSS_USD` | No | `1.0` | Daily loss limit |
| `MAX_POSITION_SIZE` | No | `100.0` | Max position size |
| `MIN_TRADE_SHARES` | No | `1.0` | Minimum order quantity |
| `ORDERBOOK_STALE_MS` | No | `3000` | Max orderbook age |
| `SIMULATED_LATENCY_MS` | No | `0` | Paper mode latency |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | No | `5` | Failures before pause |
| `CIRCUIT_BREAKER_COOLDOWN_MINUTES` | No | `15` | Pause duration |
| `POSTGRES_PASSWORD` | For docker | `cordyceps` | PostgreSQL password |
| `LOG_FORMAT` | No | `console` | `json` or `console` |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Security Notes

- **Never commit `.env`** – it's gitignored by default
- **Use strong passwords** for PostgreSQL in production
- **Paper mode is the default** – live mode requires explicit opt-in
- **Kill switch** can be set at runtime to immediately pause trading
- **The Docker image runs as non-root** (`botuser`)
- **Build context is minimal** – `.dockerignore` excludes secrets, tests, and docs
- **Resource limits** prevent runaway memory/CPU usage
- **Log rotation** configured via Docker json-file driver

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `LIVE_TRADING_ENABLED=true is required when TRADING_MODE=live` | Set both `TRADING_MODE=live` and `LIVE_TRADING_ENABLED=true` |
| `PRIVATE_KEY is required when TRADING_MODE=live` | Add your private key to `.env` |
| `Database connection refused` | Ensure PostgreSQL container is running and healthy |
| Health endpoint returns 503 | Wait for start_period (15s) then check logs |
| Circuit breaker active | Wait for cooldown or manually restart |
| `Cannot connect to Docker daemon` | Start Docker Desktop or `systemctl start docker` |
| Container keeps restarting | Check logs: `docker compose logs bot --tail=50` |
| Out of memory | Increase `deploy.resources.limits.memory` in docker-compose.yml |
