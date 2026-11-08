"""Centralised console and file logging."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from src.time_utils import WIB


class WibFormatter(logging.Formatter):
    """Menampilkan timestamp log dalam WIB tanpa memakai timezone mesin."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, WIB)
        return moment.strftime("%Y-%m-%d %H:%M:%S WIB")


def configure_logging(root: Path) -> logging.Logger:
    logger = logging.getLogger("invoice_automation")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = WibFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(root / "logs" / "automation.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger
