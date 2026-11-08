"""Integration ringan untuk konsistensi timestamp SQLite, Excel, dan log WIB."""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from openpyxl import load_workbook

from src.database import Database, InvoiceRegistration
from src.export import export_all
from src.logging_setup import WibFormatter


class TimestampPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "automation.db"
        self.database = Database(self.database_path)
        self.database.initialise()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_invoice_approval_erp_and_audit_writes_use_offset_utc(self) -> None:
        """Timestamp proses inti baru selalu UTC ISO dengan offset eksplisit."""
        correlation_id = str(uuid4())
        self.assertTrue(self.database.register_invoice(InvoiceRegistration(correlation_id, "/tmp/invoice.png", "a" * 64)))
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute("UPDATE invoices SET status='PO_VALIDATED' WHERE correlation_id=?", (correlation_id,))
        invoice = {"correlation_id": correlation_id, "amount": 100, "invoice_number": "INV-TIME-1", "vendor_name": "PT Test", "po_number": "PO-1", "currency": "IDR"}
        self.assertEqual(self.database.prepare_approval(invoice, 100, "approver@example.test", 1)["action"], "AUTO_APPROVED")
        reserved = self.database.reserve_posting(correlation_id)
        self.assertIsNotNone(reserved)
        self.database.set_posting_state(correlation_id, "POSTED")
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                invoice_row = connection.execute("SELECT created_at,updated_at,posted_at FROM invoices WHERE correlation_id=?", (correlation_id,)).fetchone()
                log_rows = connection.execute("SELECT created_at FROM process_logs WHERE correlation_id=?", (correlation_id,)).fetchall()
        self.assertTrue(all(value.endswith("+00:00") for value in invoice_row))
        self.assertTrue(log_rows)
        self.assertTrue(all(row[0].endswith("+00:00") for row in log_rows))

    def test_excel_uses_native_wib_datetime_and_explicit_format(self) -> None:
        """Excel menerima datetime lokal WIB agar filter/sort tetap bertipe tanggal."""
        timestamp = "2026-01-01T20:30:10+00:00"
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                """INSERT INTO invoices(correlation_id,file_path,file_hash,invoice_number,vendor_name,po_number,amount,currency,
                   status,approval_status,posting_state,posting_ref,posted_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid4()), "/tmp/timestamp.png", "b" * 64, "INV-TIME-2", "PT Test", "PO-2", "1", "IDR",
                 "APPROVED", "APPROVED", "POSTED", "POST-TIME", timestamp, timestamp, timestamp),
                )
                connection.execute("INSERT INTO process_logs(correlation_id,step,result,message,created_at) VALUES(?,?,?,?,?)", (None, "TEST", "SUCCESS", "timestamp", timestamp))
        output = self.root / "output"
        export_all(self.database_path, Path("resources"), output)
        workbook = load_workbook(output / "Invoices.xlsx", data_only=True)
        try:
            sheet = workbook.active
            expected = datetime(2026, 1, 2, 3, 30, 10)
            for column in (12, 14, 15):
                self.assertEqual(sheet.cell(2, column).value, expected)
                self.assertEqual(sheet.cell(2, column).number_format, 'yyyy-mm-dd hh:mm:ss "WIB"')
        finally:
            workbook.close()
        erp = load_workbook(output / "ERP_Dummy.xlsx", data_only=True)
        try:
            for column in (6, 9):
                self.assertEqual(erp.active.cell(2, column).value, datetime(2026, 1, 2, 3, 30, 10))
                self.assertEqual(erp.active.cell(2, column).number_format, 'yyyy-mm-dd hh:mm:ss "WIB"')
        finally:
            erp.close()
        logs = load_workbook(output / "ProcessLogs.xlsx", data_only=True)
        try:
            self.assertEqual(logs.active.cell(2, 1).value, datetime(2026, 1, 2, 3, 30, 10))
            self.assertEqual(logs.active.cell(2, 1).number_format, 'yyyy-mm-dd hh:mm:ss "WIB"')
        finally:
            logs.close()

    def test_log_formatter_displays_wib_without_machine_timezone(self) -> None:
        """Timestamp logging memakai WIB eksplisit terlepas dari timezone OS."""
        record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
        record.created = datetime(2026, 1, 1, 20, 30, tzinfo=timezone.utc).timestamp()
        self.assertEqual(WibFormatter().formatTime(record), "2026-01-02 03:30:00 WIB")
