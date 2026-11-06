"""Configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook


class ConfigError(ValueError):
    """Raised when the Excel configuration is missing or invalid."""


@dataclass(frozen=True)
class AppConfig:
    threshold_amount: Decimal
    forms_url: str
    excel_erp_path: str
    approver_email: str


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    workbook = load_workbook(path, read_only=True, data_only=True)
    worksheet = workbook.active
    values = {
        str(row[0]).strip(): str(row[1]).strip()
        for row in worksheet.iter_rows(min_row=2, values_only=True)
        if row[0] is not None and row[1] is not None
    }
    workbook.close()

    required = {"ThresholdAmount", "FormsUrl", "ExcelERPPath", "ApproverEmail"}
    missing = required - values.keys()
    if missing:
        raise ConfigError(f"Missing required setting(s): {', '.join(sorted(missing))}")

    try:
        threshold = Decimal(values["ThresholdAmount"])
    except InvalidOperation as error:
        raise ConfigError("ThresholdAmount must be numeric") from error
    if threshold < 0:
        raise ConfigError("ThresholdAmount cannot be negative")
    if "@" not in values["ApproverEmail"]:
        raise ConfigError("ApproverEmail must be a valid email address")
    if not values["FormsUrl"].startswith(("https://", "http://")):
        raise ConfigError("FormsUrl must start with http:// or https://")

    return AppConfig(
        threshold_amount=threshold,
        forms_url=values["FormsUrl"],
        excel_erp_path=values["ExcelERPPath"],
        approver_email=values["ApproverEmail"],
    )
