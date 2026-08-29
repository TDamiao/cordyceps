# syntax=docker/dockerfile:1

# Cordyceps – Polymarket Arbitrage Engine
# Multi-stage production image for Dockploy.

FROM python:3.13-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev pkg-config && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m pip install --no-cache-dir --upgrade pip wheel && \
    python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.13-slim AS production

LABEL maintainer="cordyceps-bot"
LABEL description="Cordyceps Polymarket Arbitrage Engine"
LABEL version="0.1.0"
LABEL org.opencontainers.image.source="https://github.com/TDamiao/cordyceps"

RUN groupadd -r botuser && useradd -r -g botuser -d /app -s /sbin/nologin botuser

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels

WORKDIR /app
COPY src/ ./src/
COPY pyproject.toml README.md ./

RUN mkdir -p /app/data /app/logs && chown -R botuser:botuser /app

USER botuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_FORMAT=json \
    TRADING_MODE=paper \
    PORT=8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "src.api_server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
