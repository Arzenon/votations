"""Application logging.

Deliberately address-free. Uvicorn's access log and Caddy's are both switched off
in production because they pair a client IP with the timestamp of a vote; this
logger exists so that operational failures are still visible, and it must stay
within that constraint.

**Never log a client address, an X-Forwarded-For value, a cookie, or a voter
token.** Log law ids, import ids, statuses and error classes — things that
describe the system, not the person using it.
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
