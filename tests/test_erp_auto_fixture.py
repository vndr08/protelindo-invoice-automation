"""Uji fixture ERP otomatis tetap terisolasi dan idempotent."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.erp_test_fixture import FIXTURE_CORRELATION_ID, FIXTURE_INVOICE_NUMBER, prepare_erp_auto_test_fixture


class ErpAutoFixtureTests(unittest.TestCase):
    def test_fixture_is_approved_isolated_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path, storage_dir = root / "automation.db", root / "incoming"
            first = prepare_erp_auto_test_fixture(database_path, storage_dir)
            second = prepare_erp_auto_test_fixture(database_path, storage_dir)
            self.assertEqual(first, second)
            self.assertEqual(first["invoice_number"], FIXTURE_INVOICE_NUMBER)
            self.assertEqual(first["correlation_id"], FIXTURE_CORRELATION_ID)
            self.assertEqual((first["status"], first["approval_status"], first["posting_state"], first["posting_ref"]), ("APPROVED", "APPROVED", "NOT_POSTED", None))
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM process_logs WHERE step = 'ERP_COMPONENT_TEST'").fetchone()[0], 1)

    def test_different_fixture_identity_creates_distinct_invoice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = prepare_erp_auto_test_fixture(root / "automation.db", root / "incoming", "component-test-v1")
            second = prepare_erp_auto_test_fixture(root / "automation.db", root / "incoming", "run-3")
            self.assertNotEqual(first["invoice_number"], second["invoice_number"])
            self.assertNotEqual(first["correlation_id"], second["correlation_id"])
