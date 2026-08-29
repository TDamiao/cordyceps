# syntax=docker/dockerfile:1

# Cordyceps – Polymarket Arbitrage Engine
# Multi-stage: build → slim production image (non-root, healthcheck)

# =====================================================================
# Build stage – install deps into wheels for layer caching
# =====================================================================
FROM python:3.13-slim AS builder

WORKDIR /build

# Install build-time system deps (gcc for C extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev pkg-config && \
    rm -rf /var/lib/apt/lists/*

# Copy dependency manifests first (cacheable layer)
COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /wheels -e . 2>/dev/null || \
    pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# =====================================================================
# Production stage – minimal runtime image
# =====================================================================
FROM python:3.13-slim AS production

LABEL maintainer="cordyceps-bot"
LABEL description="Cordyceps Polymarket Arbitrage Engine"
LABEL version="0.1.0"
LABEL org.opencontainers.image.source="https://github.com/your-org/cordyceps"

# Non-root user for security
RUN groupadd -r botuser && useradd -r -g botuser -d /app -s /sbin/nologin botuser

# Runtime deps only (curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && \
    rm -rf /var/lib/apt/lists/* && \
    apt-get clean

# Wheels from builder
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

WORKDIR /app

# Application code (copy only what's needed at runtime)
COPY src/ ./src/
COPY pyproject.toml ./

# Data directories (mounted volume targets)
RUN mkdir -p /app/data /app/logs && \
    chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Environment defaults
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV LOG_FORMAT=json
ENV TRADING_MODE=paper
ENV DATABASE_URL=sqlite:///./cordyceps.db

# Health check – waits for start_period before first probe
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:${PORT:-8000}/health || exit 1

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "src.api_server:app", "--host", "0.0.0.0", "--port", "8000"]
