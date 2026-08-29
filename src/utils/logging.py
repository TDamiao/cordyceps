"""
Structured logging configuration for Polymarket Arbitrage Bot.
"""

import logging
import sys
from typing import Any

import structlog
from structlog.types import Processor

from src.config import get_settings


def setup_logging() -> None:
    """Configure structured logging based on settings."""
    settings = get_settings()

    # Determine log level
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Configure processors based on format
    secret_values = tuple(
        value
        for value in (
            settings.private_key,
            settings.polymarket_api_secret,
            settings.polymarket_api_passphrase,
            settings.admin_token,
        )
        if value
    )

    def redact_secrets(_logger, _method_name, event_dict):
        sensitive_keys = {"private_key", "api_secret", "passphrase", "admin_token"}
        for key, value in list(event_dict.items()):
            if key.lower() in sensitive_keys:
                event_dict[key] = "[REDACTED]"
            elif isinstance(value, str):
                for secret in secret_values:
                    value = value.replace(secret, "[REDACTED]")
                event_dict[key] = value
        return event_dict

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        redact_secrets,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.log_format == "json":
        # JSON format for production
        processors: list[Processor] = [
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Console format for development
        processors = [
            *shared_processors,
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    # Configure structlog
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Also configure standard logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )


def get_logger(name: str | None = None, **initial_context: Any) -> structlog.BoundLogger:
    """
    Get a configured logger instance.

    Args:
        name: Logger name (usually __name__ of the module)
        **initial_context: Initial context variables to bind

    Returns:
        Configured structlog BoundLogger
    """
    logger = structlog.get_logger(name)
    if initial_context:
        logger = logger.bind(**initial_context)
    return logger
