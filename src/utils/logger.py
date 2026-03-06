import logging
import os
from pathlib import Path


def get_logger(name: str) -> logging.Logger:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger = logging.getLogger(name)

    if not logger.handlers:
        logger.setLevel(log_level)
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Console handler
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        # File handler
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        fh = logging.FileHandler(log_dir / "viral_distributor.log")
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger
