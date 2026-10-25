"""Logging setup for arr processes: one stderr handler with time, level and logger name.

See docs/01-architektur.md §2.
"""

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger to write to stderr at the given level."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
