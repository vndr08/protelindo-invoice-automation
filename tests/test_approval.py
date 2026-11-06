"""Integration-style approval tests using temporary SQLite and mocked mail adapters."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.approval import ApprovalWorkflow, EmailSettings, ImapInbox
from src.approval import parse_reply
from src.database import Database, InvoiceRegistration


class FakeLogger:
    """Logger dummy agar test tidak menulis log ke terminal."""
    def info(self, *args: object, **kwargs: object) -> None: pass
    def warning(self, *args: object, **kwargs: object) -> None: pass


class FakeMailer:
    """SMTP mock yang dapat sukses atau gagal secara deterministik."""
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.sent: list[tuple[str, str, str]] = []

    def send(self, recipient: str, subject: str, body: str) -> None:
        if self.should_fail:
            raise OSError("simulated SMTP failure")
        self.sent.append((recipient, subject, body))


def reply_bytes(sender: str, decision: str, token: str) -> bytes:
    """Membuat email reply mock dalam format yang didokumentasikan."""
    message = EmailMessage()
    message["From"] = sender
    message.set_content(f"{decision}\nApproval Token: {token}\n")
    return message.as_bytes()


class ApprovalWorkflowTests(unittest.TestCase):
    """Memverifikasi state machine approval, outbox, dan reply handling."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "automation.db")
        self.database.initialise()
        self.workflow = ApprovalWorkflow(self.database, FakeLogger())
        self.approver = "approver@example.com"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seed_invoice(self, correlation_id: str, amount: str, status: str = "PO_VALIDATED") -> None:
        """Menyiapkan invoice dengan hasil PO validation untuk test berikutnya."""
        registration = InvoiceRegistration(correlation_id, f"/tmp/{correlation_id}.jpg", f"hash-{correlation_id}")
        self.assertTrue(self.database.register_invoice(registration))
        with sqlite3.connect(self.database.path) as connection:
            connection.execute(
                """UPDATE invoices SET invoice_number = ?, vendor_name = ?, po_number = ?, amount = ?, currency = ?, status = ?
                WHERE correlation_id = ?""",
                ("INV-1", "PT Maju Jaya", "PO-001", amount, "IDR", status, correlation_id),
            )

    def outbox_token(self) -> str:
        """Mengambil token test dari body outbox tanpa menaruh token pada log aplikasi."""
        with sqlite3.connect(self.database.path) as connection:
            body = connection.execute("SELECT body FROM approval_outbox").fetchone()[0]
        return re.search(r"Approval Token: ([A-Za-z0-9_-]+)", body).group(1)

    def invoice_state(self, correlation_id: str) -> tuple[str, str]:
        """Mengambil status workflow dan approval invoice untuk assertion."""
        with sqlite3.connect(self.database.path) as connection:
            return connection.execute("SELECT status, approval_status FROM invoices WHERE correlation_id = ?", (correlation_id,)).fetchone()

    def test_threshold_auto_approval_and_pending_email(self) -> None:
        """Amount sama threshold auto approve; di atas threshold membuat request SMTP."""
        self.seed_invoice("low", "1000000")
        self.seed_invoice("high", "1000001")
        mailer = FakeMailer()
        summary = self.workflow.initiate(Decimal("1000000"), self.approver, 24, mailer)
        self.assertEqual(summary, {"auto_approved": 1, "pending": 1, "sent": 1, "failed": 0})
        self.assertEqual(self.invoice_state("low"), ("APPROVED", "AUTO_APPROVED"))
        self.assertEqual(self.invoice_state("high"), ("PENDING_APPROVAL", "PENDING_APPROVAL"))
        self.assertEqual(len(mailer.sent), 1)

    def test_valid_approve_and_reject_are_applied_once(self) -> None:
        """Reply APPROVE/REJECT valid mengubah state sekali saja."""
        self.seed_invoice("approve", "1200000")
        self.workflow.initiate(Decimal("1000000"), self.approver, 24, FakeMailer())
        token = self.outbox_token()
        result = self.workflow.process_replies([("1", reply_bytes(self.approver, "APPROVE", token))], self.approver)
        self.assertEqual(result, {"APPROVED": 1})
        self.assertEqual(self.invoice_state("approve"), ("APPROVED", "APPROVED"))
        duplicate = self.workflow.process_replies([("2", reply_bytes(self.approver, "APPROVE", token))], self.approver)
        self.assertEqual(duplicate, {"DUPLICATE_DECISION": 1})
        self.assertEqual(self.invoice_state("approve"), ("APPROVED", "APPROVED"))

    def test_valid_reject_blocks_invoice_from_future_erp_stage(self) -> None:
        """Reply REJECT valid menempatkan invoice pada status terminal REJECTED."""
        self.seed_invoice("reject-valid", "1200000")
        self.workflow.initiate(Decimal("1000000"), self.approver, 24, FakeMailer())
        token = self.outbox_token()
        result = self.workflow.process_replies([("1", reply_bytes(self.approver, "REJECT", token))], self.approver)
        self.assertEqual(result, {"REJECTED": 1})
        self.assertEqual(self.invoice_state("reject-valid"), ("REJECTED", "REJECTED"))

    def test_reject_unauthorized_invalid_and_expired_replies(self) -> None:
        """Semua reply invalid tidak boleh mengubah status pending invoice."""
        self.seed_invoice("reject", "1200000")
        self.workflow.initiate(Decimal("1000000"), self.approver, 24, FakeMailer())
        token = self.outbox_token()
        result = self.workflow.process_replies([("1", reply_bytes("intruder@example.com", "APPROVE", token))], self.approver)
        self.assertEqual(result, {"UNAUTHORIZED_APPROVER": 1})
        result = self.workflow.process_replies([("2", reply_bytes(self.approver, "APPROVE", "wrong-token-value-1234567890"))], self.approver)
        self.assertEqual(result, {"INVALID_TOKEN": 1})
        malformed_message = EmailMessage()
        malformed_message["From"] = self.approver
        malformed_message.set_content("Please approve this invoice")
        result = self.workflow.process_replies([("malformed", malformed_message.as_bytes())], self.approver)
        self.assertEqual(result, {"MALFORMED_REPLY": 1})
        with sqlite3.connect(self.database.path) as connection:
            connection.execute("UPDATE approval_requests SET expires_at = ?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        result = self.workflow.process_replies([("3", reply_bytes(self.approver, "REJECT", token))], self.approver)
        self.assertEqual(result, {"EXPIRED_TOKEN": 1})
        self.assertEqual(self.invoice_state("reject"), ("PENDING_APPROVAL", "PENDING_APPROVAL"))

    def test_smtp_failure_is_durable_and_po_failed_is_never_selected(self) -> None:
        """SMTP gagal tersimpan sebagai FAILED dan invoice PO gagal tidak masuk workflow."""
        self.seed_invoice("smtp", "1200000")
        self.seed_invoice("po-failed", "1200000", "PO_VALIDATION_FAILED")
        summary = self.workflow.initiate(Decimal("1000000"), self.approver, 24, FakeMailer(should_fail=True))
        self.assertEqual(summary, {"auto_approved": 0, "pending": 1, "sent": 0, "failed": 1})
        self.assertEqual(self.invoice_state("smtp"), ("PENDING_APPROVAL", "PENDING_APPROVAL"))
        self.assertEqual(self.invoice_state("po-failed"), ("PO_VALIDATION_FAILED", "NOT_REQUIRED"))
        with sqlite3.connect(self.database.path) as connection:
            self.assertEqual(connection.execute("SELECT delivery_state FROM approval_outbox").fetchone()[0], "FAILED")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM approval_outbox").fetchone()[0], 1)
        second_run = self.workflow.initiate(Decimal("1000000"), self.approver, 24, FakeMailer())
        self.assertEqual(second_run, {"auto_approved": 0, "pending": 0, "sent": 0, "failed": 0})


class ImapInboxTests(unittest.TestCase):
    """Memverifikasi adapter IMAP satu kali menggunakan mock, tanpa jaringan nyata."""

    def test_reads_unseen_batch_once(self) -> None:
        """IMAP mock mengembalikan satu email UNSEEN dan menyimpannya sebagai seen."""
        class FakeImap:
            def __enter__(self): return self
            def __exit__(self, *args: object): return None
            def login(self, *args: object): return "OK", []
            def select(self, *args: object): return "OK", []
            def uid(self, command: str, *args: object):
                if command == "search": return "OK", [b"42"]
                if command == "fetch": return "OK", [(b"header", b"From: approver@example.com\n\nAPPROVE\nApproval Token: abcdefghijklmnopqrstuvwxyz\n")]
                return "OK", []
        settings = EmailSettings("smtp", 587, "u", "p", "imap", 993, "u", "p")
        with patch("src.approval.imaplib.IMAP4_SSL", return_value=FakeImap()):
            messages = ImapInbox(settings).unread_messages()
        self.assertEqual(messages[0][0], "42")


class ReplyParserTests(unittest.TestCase):
    """Memastikan parser hanya menerima keputusan pada reply terbaru."""
    def test_ignores_quoted_original_decision(self) -> None:
        raw = b"From: approver@example.com\n\nLooks good\n\nOn yesterday wrote:\n> APPROVE\n> Approval Token: abcdefghijklmnopqrstuvwxyz\n"
        result = parse_reply(raw)
        self.assertIsNone(result.decision)
        self.assertIsNone(result.token)

    def test_parses_new_reply_before_quoted_original(self) -> None:
        raw = b"From: approver@example.com\n\nREJECT\nApproval Token: abcdefghijklmnopqrstuvwxyz\n\n> APPROVE\n> Approval Token: ignored-token-abcdefghijklmnopqrstuvwxyz\n"
        result = parse_reply(raw)
        self.assertEqual(result.decision, "REJECT")
        self.assertEqual(result.token, "abcdefghijklmnopqrstuvwxyz")
