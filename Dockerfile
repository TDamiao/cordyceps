# syntax=docker/dockerfile:1

# Polymarket Arbitrage Bot
# Multi-stage build for minimal production image

# ===== Build Stage =====
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /app/wheels -e .

# ===== Production Stage =====
FROM python:3.12-slim AS production

# Labels
LABEL maintainer="polymarket-bot"
LABEL description="Polymarket Arbitrage Bot"
LABEL version="0.1.0"

# Create non-root user for security
RUN groupadd -r botuser && useradd -r -g botuser botuser

WORKDIR /app

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy wheels from builder and install
COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache-dir /wheels/*

# Copy application code
COPY src/ ./src/
COPY pyproject.toml ./

# Create data directory for persistence
RUN mkdir -p /app/data && chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV LOG_FORMAT=json

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=5)" || exit 1

# Default command
CMD ["python", "-m", "src.main"]
