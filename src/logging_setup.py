"""Centralised console and file logging."""

from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(root: Path) -> logging.Logger:
    logger = logging.getLogger("invoice_automation")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(root / "logs" / "automation.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger
