from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any


def setup_logger(
    name: str = "smart_finqa",
    log_file: Path | None = None,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """
    Setup structured logger with file and console handlers.

    Args:
        name: Logger name
        log_file: Optional log file path
        level: Logging level
        console: Whether to output to console

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Avoid duplicate handlers
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def log_operation(logger: logging.Logger, operation: str, **kwargs: Any) -> None:
    """Log structured operation with context."""
    context = " | ".join(f"{k}={v}" for k, v in kwargs.items())
    logger.info(f"{operation} | {context}")


def log_error(logger: logging.Logger, operation: str, error: Exception, **kwargs: Any) -> None:
    """Log structured error with context."""
    context = " | ".join(f"{k}={v}" for k, v in kwargs.items())
    logger.error(f"{operation} FAILED | {context} | error={type(error).__name__}: {error}")


# Global logger instance
_default_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    """Get or create default logger."""
    global _default_logger
    if _default_logger is None:
        _default_logger = setup_logger()
    return _default_logger
