"""Orkestrasi satu siklus end-to-end dengan boundary environment test yang ketat."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.config import load_config
from src.database import Database


@dataclass(frozen=True)
class FinalE2EPaths:
    """Seluruh lokasi mutable untuk satu fresh end-to-end test."""
    database: Path
    incoming: Path
    output: Path
    processed: Path


def validate_isolated_paths(paths: FinalE2EPaths, project_root: Path) -> None:
    """Mencegah orchestrator test mengarah ke data operasi atau path arbitrer."""
    allowed = (project_root / "data" / "final-e2e-test").resolve()
    for path in (paths.database, paths.incoming, paths.output, paths.processed):
        if not path.resolve().is_relative_to(allowed):
            raise ValueError("Final E2E paths must remain under data/final-e2e-test")


def prepare_final_e2e_environment(paths: FinalE2EPaths, project_root: Path) -> None:
    """Membuat direktori test dan SQLite baru/tahan-rerun tanpa layanan eksternal."""
    validate_isolated_paths(paths, project_root)
    for directory in (paths.incoming, paths.output, paths.processed):
        directory.mkdir(parents=True, exist_ok=True)
    Database(paths.database).initialise()


def preflight_final_e2e(paths: FinalE2EPaths, project_root: Path) -> dict[str, object]:
    """Preflight lokal read-only; tidak membuat DB, koneksi mail, OCR, atau browser."""
    validate_isolated_paths(paths, project_root)
    config = load_config(project_root / "resources" / "Config.xlsx")
    required = ("Invoices.template.xlsx", "ERP_Dummy.template.xlsx", "ProcessLogs.template.xlsx", "PurchaseOrders.csv")
    missing = [name for name in required if not (project_root / "resources" / name).is_file()]
    # Gunakan aturan placeholder yang sama dengan preflight deployment agar dry-run
    # tidak menyatakan siap ketika konfigurasi publik belum diisi reviewer.
    forms_url_configured = bool(config.forms_url.strip()) and "your-form-url" not in config.forms_url.lower()
    approver_email_configured = bool(config.approver_email.strip()) and not config.approver_email.lower().endswith("@example.com")
    return {
        "paths_isolated": True,
        "database_exists": paths.database.exists(),
        "forms_url_configured": forms_url_configured,
        "approver_email_configured": approver_email_configured,
        "templates_missing": missing,
        "ready": not missing and forms_url_configured and approver_email_configured,
    }


def run_stages(stages: dict[str, Callable[[], None]], resume: bool = False) -> list[str]:
    """Menjalankan tahap berurutan; resume sengaja tidak memanggil Gmail intake lagi."""
    # Archive harus selesai lebih dahulu agar FilePath dan audit ARCHIVE pada Excel mencerminkan state final.
    order = ("ocr", "po_validation", "approval", "erp", "archive", "export") if resume else (
        "email_intake", "ocr", "po_validation", "approval", "erp", "archive", "export"
    )
    completed: list[str] = []
    for name in order:
        stages[name]()
        completed.append(name)
    return completed
