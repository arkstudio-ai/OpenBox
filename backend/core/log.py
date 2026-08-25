import logging
import sys


def create_logger(name: str) -> logging.Logger:
    """Create a structured logger."""
    logger = logging.getLogger(f"openbox.{name}")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    # Every OpenBox logger owns exactly one formatted handler. Without this,
    # `openbox.tool.computer` also propagated to `openbox.tool` (which has its
    # own handler), printing every tool line twice and double-counting metrics.
    logger.propagate = False
    return logger
