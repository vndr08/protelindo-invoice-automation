"""Uji export dan archive dengan database serta filesystem sementara yang terisolasi."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from openpyxl import load_workbook

import main
from src.archive import archive_final
from src.database import Database
from src.export import ExportError, _atomic_workbook, export_all


ROOT = Path(__file__).resolve().parents[1]


class ExportArchiveIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "isolated.db"
        self.database = Database(self.db_path)
        self.database.initialise()

    def tearDown(self):
        self.temp.cleanup()

    def _invoice(self, correlation_id: str, state: str, status: str = "APPROVED") -> Path:
        source = self.root / f"{correlation_id}.jpg"
        source.write_bytes(correlation_id.encode())
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """INSERT INTO invoices(correlation_id, file_path, file_hash, invoice_number,
                       vendor_name, po_number, amount, currency, status, approval_status,
                       posting_state, posting_ref, posted_at)
                       VALUES (?, ?, ?, ?, 'Vendor', 'PO-1', '1200000', 'IDR', ?, 'APPROVED', ?, ?, ?)""",
                    (correlation_id, str(source), digest, f"INV-{correlation_id}", status,
                     state, f"POST-{correlation_id}", "2026-11-07T00:00:00+00:00" if state == "POSTED" else None),
                )
        return source

    def test_isolated_export_keeps_template_mapping_and_rerun_has_no_duplicate_rows(self):
        self._invoice("posted", "POSTED")
        self._invoice("rejected", "NOT_POSTED", "REJECTED")
        self.database.log_event("posted", "ERP_POSTING_RECOVERY", "NOT_POSTED", "verified no submission")
        self.database.log_event("posted", "ERP_POSTING_RECONCILIATION", "POSTED", "manual confirmation")
        output = self.root / "output"
        export_all(self.db_path, ROOT / "resources", output)
        export_all(self.db_path, ROOT / "resources", output)
        invoices = load_workbook(output / "Invoices.xlsx", data_only=True).active
        erp = load_workbook(output / "ERP_Dummy.xlsx", data_only=True).active
        logs = load_workbook(output / "ProcessLogs.xlsx", data_only=True).active
        self.assertEqual(tuple(cell.value for cell in invoices[1]), ("CorrelationId", "FilePath", "InvoiceNumber", "VendorName", "PO_Number", "Amount", "Currency", "MatchStatus", "NeedsApproval", "ApprovalStatus", "PostingRef", "PostedAt", "Status", "CreatedAt", "UpdatedAt"))
        self.assertEqual(invoices.max_row, 3)
        self.assertEqual(erp.max_row, 2)
        self.assertEqual(erp.cell(2, 1).value, "INV-posted")
        self.assertEqual(erp.cell(2, 8).value, "POST-posted")
        self.assertIn("ERP_POSTING_RECOVERY", [logs.cell(row, 3).value for row in range(2, logs.max_row + 1)])
        self.assertIn("ERP_POSTING_RECONCILIATION", [logs.cell(row, 3).value for row in range(2, logs.max_row + 1)])

    def test_archive_posted_to_success_and_rerun_is_idempotent(self):
        posted_file = self._invoice("posted", "POSTED")
        pending_file = self._invoice("pending", "NOT_POSTED")
        archive_root = self.root / "archive"
        first = archive_final(self.db_path, archive_root)
        second = archive_final(self.db_path, archive_root)
        self.assertEqual(first, {"archived": 1, "skipped": 1, "failed": 0})
        self.assertEqual(second, {"archived": 0, "skipped": 1, "failed": 0})
        self.assertFalse(posted_file.exists())
        self.assertTrue((archive_root / "success" / posted_file.name).exists())
        self.assertTrue(pending_file.exists())
        with closing(sqlite3.connect(self.db_path)) as connection:
            archived = connection.execute("SELECT archive_path, archive_hash FROM invoices WHERE correlation_id = 'posted'").fetchone()
        self.assertEqual(Path(archived[0]), archive_root / "success" / posted_file.name)
        self.assertEqual(archived[1], hashlib.sha256(b"posted").hexdigest())

    def test_final_export_uses_archive_path_and_includes_archive_audit(self):
        source = self._invoice("posted", "POSTED")
        archive_root = self.root / "archive"
        self.assertEqual(archive_final(self.db_path, archive_root)["archived"], 1)
        output = self.root / "output"
        export_all(self.db_path, ROOT / "resources", output)
        # Rerun export mengganti workbook atomik, bukan menambah baris.
        export_all(self.db_path, ROOT / "resources", output)
        invoices_book = load_workbook(output / "Invoices.xlsx", data_only=True)
        logs_book = load_workbook(output / "ProcessLogs.xlsx", data_only=True)
        try:
            invoices, logs = invoices_book.active, logs_book.active
            self.assertEqual(invoices.max_row, 2)
            self.assertEqual(Path(invoices.cell(2, 2).value), archive_root / "success" / source.name)
            archive_rows = [(logs.cell(row, 3).value, logs.cell(row, 4).value) for row in range(2, logs.max_row + 1)]
            self.assertIn(("ARCHIVE", "SUCCESS"), archive_rows)
        finally:
            invoices_book.close()
            logs_book.close()

    def test_archive_failure_is_audited_and_exported_without_moving_file(self):
        source = self._invoice("missing", "POSTED")
        source.unlink()
        archive_root, output = self.root / "archive", self.root / "output"
        self.assertEqual(archive_final(self.db_path, archive_root), {"archived": 0, "skipped": 0, "failed": 1})
        export_all(self.db_path, ROOT / "resources", output)
        logs_book = load_workbook(output / "ProcessLogs.xlsx", data_only=True)
        try:
            logs = logs_book.active
            self.assertIn(("ARCHIVE", "FAILED"), [(logs.cell(row, 3).value, logs.cell(row, 4).value) for row in range(2, logs.max_row + 1)])
        finally:
            logs_book.close()

    def test_export_and_archive_cli_accept_isolated_paths(self):
        with patch("sys.argv", ["main.py", "export-excel", "--db-path", str(self.db_path), "--output-dir", str(self.root / "output")]):
            export_args = main.parse_args()
        with patch("sys.argv", ["main.py", "archive-invoices", "--db-path", str(self.db_path), "--archive-dir", str(self.root / "archive")]):
            archive_args = main.parse_args()
        self.assertEqual(export_args.db_path, self.db_path)
        self.assertEqual(archive_args.archive_dir, self.root / "archive")

    def test_atomic_export_closes_workbook_and_cleans_temp_after_save_failure(self):
        workbook = Mock()
        workbook.active.max_row = 1
        workbook.save.side_effect = OSError("simulated save failure")
        output = self.root / "output" / "Invoices.xlsx"
        with patch("src.export.load_workbook", return_value=workbook), self.assertRaises(ExportError):
            _atomic_workbook(self.root / "template.xlsx", output, [])
        workbook.close.assert_called_once()
        self.assertEqual(list(output.parent.glob("*.xlsx")), [])

    def test_atomic_export_closes_workbook_and_cleans_temp_after_replace_failure(self):
        workbook = Mock()
        workbook.active.max_row = 1
        output = self.root / "output" / "Invoices.xlsx"
        with patch("src.export.load_workbook", return_value=workbook), \
             patch("src.export.os.replace", side_effect=OSError("simulated replace failure")), \
             self.assertRaises(ExportError):
            _atomic_workbook(self.root / "template.xlsx", output, [])
        workbook.close.assert_called_once()
        self.assertEqual(list(output.parent.glob("*.xlsx")), [])
