"""Application logging.

Access logs are off in production because they pair an IP with the time of a
vote. Never log an IP address, X-Forwarded-For, a cookie or a voter token.
Log ids, statuses and error classes.
"""

from __future__ import annotations

import logging
import sys

LOGGER_NAME = "votations"

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """Attach a single stderr handler to the application logger. Idempotent."""
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%Y-%m-%dT%H:%M:%S%z"))
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False
    _configured = True


def get_logger(name: str = "") -> logging.Logger:
    """A child of the application logger, e.g. ``get_logger("imports")``."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)
