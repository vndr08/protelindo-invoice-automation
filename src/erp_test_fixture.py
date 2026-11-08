"""Fixture ERP terisolasi untuk membuktikan adapter browser tanpa menyentuh invoice live."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from src.database import Database


FIXTURE_INVOICE_NUMBER = "ERP-AUTO-TEST-001"
FIXTURE_CORRELATION_ID = str(uuid5(NAMESPACE_URL, "protelindo/erp-auto-component-test/v1"))


def prepare_erp_auto_test_fixture(database_path: Path, storage_dir: Path, fixture_id: str = "component-test-v1") -> dict[str, Any]:
    """Membuat satu invoice APPROVED khusus komponen ERP secara idempotent."""
    if not re.fullmatch(r"[a-z0-9-]{1,40}", fixture_id):
        raise ValueError("fixture_id must contain only lowercase letters, numbers, and hyphens")
    invoice_number = FIXTURE_INVOICE_NUMBER if fixture_id == "component-test-v1" else f"ERP-AUTO-{fixture_id.upper()}"
    correlation_id = FIXTURE_CORRELATION_ID if fixture_id == "component-test-v1" else str(uuid5(NAMESPACE_URL, f"protelindo/erp-auto-component-test/{fixture_id}"))
    storage_dir.mkdir(parents=True, exist_ok=True)
    attachment = storage_dir / f"erp-auto-{fixture_id}.jpg"
    # File hanya menjadi artefak lokal untuk menjaga schema invoice konsisten; tidak dikirim ke OCR.
    payload = b"\xff\xd8" + f"ERP-AUTO-{fixture_id}".encode() + b"\xff\xd9"
    if not attachment.exists():
        attachment.write_bytes(payload)
    file_hash = hashlib.sha256(attachment.read_bytes()).hexdigest()
    database = Database(database_path)
    database.initialise()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        existing = connection.execute(
            "SELECT * FROM invoices WHERE correlation_id = ?", (correlation_id,)
        ).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO invoices(correlation_id, file_path, file_hash, invoice_number,
                   vendor_name, po_number, amount, currency, status, approval_status, posting_state)
                   VALUES (?, ?, ?, ?, 'PT ERP Automation Test', 'ERP-AUTO-PO-001', '1000', 'IDR',
                   'APPROVED', 'APPROVED', 'NOT_POSTED')""",
                (correlation_id, str(attachment), file_hash, invoice_number),
            )
            Database.add_log(
                connection,
                correlation_id,
                "ERP_COMPONENT_TEST",
                "PREPARED",
                f"Isolated ERP component-test fixture {fixture_id}; not created by email intake or approval workflow",
            )
            existing = connection.execute(
                "SELECT * FROM invoices WHERE correlation_id = ?", (correlation_id,)
            ).fetchone()
            # contextlib.closing hanya menutup koneksi; commit dibuat eksplisit untuk fixture baru.
            connection.commit()
        return {
            "invoice_number": existing["invoice_number"],
            "correlation_id": existing["correlation_id"],
            "status": existing["status"],
            "approval_status": existing["approval_status"],
            "posting_state": existing["posting_state"],
            "posting_ref": existing["posting_ref"],
        }
