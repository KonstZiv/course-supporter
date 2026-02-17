"""Structured logging configuration.

JSON output for production, colored console for development/testing.
Call configure_logging() once at application startup (e.g. in FastAPI lifespan).
"""

import logging
import sys

import structlog

SENSITIVE_KEYS: frozenset[str] = frozenset(
    {"api_key", "key_hash", "password", "secret", "token", "authorization"}
)


def _redact_sensitive_keys(
    logger: logging.Logger,
    method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """Redact values of sensitive keys in log events."""
    for key in event_dict:
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict


def configure_logging(
    environment: str = "development",
    log_level: str = "INFO",
) -> None:
    """Configure structlog processor chain and stdlib root logger.

    Args:
        environment: 'production' for JSON output, anything else
            for colored console output.
        log_level: Python log level name (DEBUG, INFO, WARNING, etc.).
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _redact_sensitive_keys,
    ]

    if environment == "production":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level.upper())

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("aiobotocore").setLevel(logging.WARNING)
