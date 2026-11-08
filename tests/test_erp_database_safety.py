"""Regression test untuk gate dan idempotensi state ERP di SQLite sementara."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from src.database import Database
import main


class ErpDatabaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "isolated.db"
        self.database = Database(self.path)
        self.database.initialise()

    def tearDown(self):
        self.temp.cleanup()

    def _invoice(self, correlation_id: str, approval_status: str, posting_state: str = "NOT_POSTED") -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    """INSERT INTO invoices(correlation_id, invoice_number, file_path, file_hash, status,
                       approval_status, posting_state) VALUES (?, ?, ?, ?,
                       'APPROVED', ?, ?)""",
                    (correlation_id, correlation_id, f"{correlation_id}.jpg", correlation_id, approval_status, posting_state),
                )

    def test_reserve_posting_requires_valid_approval(self):
        self._invoice("not-approved", "PENDING_APPROVAL")
        self.assertIsNone(self.database.reserve_posting("not-approved"))

    def test_reservation_is_idempotent_and_posting_uncertain_not_reselected(self):
        self._invoice("approved", "APPROVED")
        reserved = self.database.reserve_posting("approved")
        self.assertIsNotNone(reserved)
        self.assertIsNone(self.database.reserve_posting("approved"))
        self.database.set_posting_state("approved", "POSTING_UNCERTAIN", "confirmation unavailable")
        self.assertEqual(self.database.pending_erp_invoices(), [])

    def test_erp_post_cli_accepts_isolated_database_path(self):
        # Parser harus meneruskan path eksplisit, bukan mengunci command ke database utama.
        with patch("sys.argv", ["main.py", "erp-post", "--db-path", str(self.path)]):
            arguments = main.parse_args()
        self.assertEqual(arguments.db_path, self.path)

    def _failed_invoice(self, correlation_id="failed", posting_state="POSTING_FAILED"):
        self._invoice(correlation_id, "APPROVED", posting_state)
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("UPDATE invoices SET posting_ref = ?, posting_error = ? WHERE correlation_id = ?", (f"POST-{correlation_id}", "pre-submit failure", correlation_id))

    def test_failed_posting_recovery_preserves_ref_and_audits(self):
        self._failed_invoice()
        result = self.database.prepare_failed_posting_retry("failed")
        self.assertEqual(result["posting_ref"], "POST-failed")
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT posting_state, posting_ref, posting_error FROM invoices WHERE correlation_id = 'failed'").fetchone()
            audit = connection.execute("SELECT result, message FROM process_logs WHERE correlation_id = 'failed' AND step = 'ERP_POSTING_RECOVERY'").fetchone()
        self.assertEqual(row, ("NOT_POSTED", "POST-failed", None))
        self.assertEqual(audit[0], "NOT_POSTED")
        self.assertIn("verified no submission", audit[1])

    def test_recovery_dry_run_and_invalid_states_do_not_change_invoice(self):
        self._failed_invoice()
        before = self.database.prepare_failed_posting_retry("failed", dry_run=True)
        self.assertTrue(before["dry_run"])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT posting_state FROM invoices WHERE correlation_id = 'failed'").fetchone()[0], "POSTING_FAILED")
        for state in ("POSTED", "POSTING_IN_PROGRESS", "POSTING_UNCERTAIN"):
            self._failed_invoice(f"invalid-{state}", state)
            with self.assertRaises(ValueError):
                self.database.prepare_failed_posting_retry(f"invalid-{state}")

    def test_concurrent_recovery_allows_exactly_one_transition(self):
        self._failed_invoice()
        def recover():
            try:
                return Database(self.path).prepare_failed_posting_retry("failed")["state"]
            except ValueError:
                return "REJECTED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: recover(), range(2)))
        self.assertEqual(outcomes.count("NOT_POSTED"), 1)
        self.assertEqual(outcomes.count("REJECTED"), 1)

    def _uncertain_invoice(self, correlation_id="uncertain", approval_status="APPROVED"):
        self._invoice(correlation_id, approval_status, "POSTING_UNCERTAIN")
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("UPDATE invoices SET posting_ref = ? WHERE correlation_id = ?", (f"POST-{correlation_id}", correlation_id))

    def test_uncertain_reconciliation_dry_run_posted_and_audit_are_idempotent(self):
        self._uncertain_invoice()
        preview = self.database.reconcile_uncertain_posting("uncertain", "POST-uncertain", dry_run=True)
        self.assertTrue(preview["dry_run"])
        result = self.database.reconcile_uncertain_posting("uncertain", "POST-uncertain")
        self.assertEqual(result["state"], "POSTED")
        with closing(sqlite3.connect(self.path)) as connection:
            invoice = connection.execute("SELECT posting_state, posting_ref, posted_at FROM invoices WHERE correlation_id = 'uncertain'").fetchone()
            audits = connection.execute("SELECT COUNT(*) FROM process_logs WHERE correlation_id = 'uncertain' AND step = 'ERP_POSTING_RECONCILIATION'").fetchone()[0]
        self.assertEqual(invoice[0:2], ("POSTED", "POST-uncertain"))
        self.assertIsNotNone(invoice[2])
        self.assertEqual(audits, 1)
        with self.assertRaises(ValueError):
            self.database.reconcile_uncertain_posting("uncertain", "POST-uncertain")

    def test_reconciliation_rejects_reference_state_and_approval_mismatch(self):
        self._uncertain_invoice()
        with self.assertRaises(ValueError):
            self.database.reconcile_uncertain_posting("uncertain", "POST-wrong")
        self._uncertain_invoice("pending", "PENDING_APPROVAL")
        with self.assertRaises(ValueError):
            self.database.reconcile_uncertain_posting("pending", "POST-pending")
        self._failed_invoice("failed")
        with self.assertRaises(ValueError):
            self.database.reconcile_uncertain_posting("failed", "POST-failed")

    def test_concurrent_reconciliation_writes_one_audit_log(self):
        self._uncertain_invoice()
        def reconcile():
            try:
                return Database(self.path).reconcile_uncertain_posting("uncertain", "POST-uncertain")["state"]
            except ValueError:
                return "REJECTED"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: reconcile(), range(2)))
        self.assertEqual(outcomes.count("POSTED"), 1)
        self.assertEqual(outcomes.count("REJECTED"), 1)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM process_logs WHERE step = 'ERP_POSTING_RECONCILIATION'").fetchone()[0], 1)

    def test_reconcile_cli_requires_explicit_inputs(self):
        with patch("sys.argv", ["main.py", "erp-reconcile", "--db-path", str(self.path), "--invoice-number", "I", "--posting-ref", "P", "--confirmed-in-responses", "--dry-run"]):
            arguments = main.parse_args()
        self.assertTrue(arguments.confirmed_in_responses)
        self.assertTrue(arguments.dry_run)
