"""Structured logging for the orchestrator."""

import logging
import sys


def setup_logger(name: str = "network-swarm", level: str = "INFO") -> logging.Logger:
    """Set up a logger with console output."""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
        logger.addHandler(handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger by name."""
    return logging.getLogger(f"network-swarm.{name}")
