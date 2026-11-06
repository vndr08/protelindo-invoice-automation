"""SQLite persistence layer. Excel exports will be derived from this data."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4


class _TransactionClosing:
    """Menggabungkan commit/rollback sqlite context dengan penutupan connection."""
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
    def __enter__(self) -> sqlite3.Connection:
        self.connection.__enter__()
        return self.connection
    def __exit__(self, *args: object) -> bool:
        try:
            return self.connection.__exit__(*args)
        finally:
            self.connection.close()


def closing(connection: sqlite3.Connection) -> _TransactionClosing:
    """Menjaga pola existing tetap atomik sekaligus memastikan close."""
    return _TransactionClosing(connection)


@dataclass(frozen=True)
class InvoiceRegistration:
    correlation_id: str
    file_path: str
    file_hash: str


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialise(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS invoices (
                    id INTEGER PRIMARY KEY,
                    correlation_id TEXT NOT NULL UNIQUE,
                    file_path TEXT NOT NULL UNIQUE,
                    file_hash TEXT NOT NULL UNIQUE,
                    invoice_number TEXT,
                    vendor_name TEXT,
                    po_number TEXT,
                    amount TEXT,
                    currency TEXT,
                    match_status TEXT NOT NULL DEFAULT 'PENDING',
                    needs_approval INTEGER NOT NULL DEFAULT 0,
                    approval_status TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
                    posting_ref TEXT UNIQUE,
                    posted_at TEXT,
                    status TEXT NOT NULL DEFAULT 'RECEIVED',
                    ocr_text TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS process_logs (
                    id INTEGER PRIMARY KEY,
                    correlation_id TEXT,
                    step TEXT NOT NULL,
                    result TEXT NOT NULL,
                    message TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(correlation_id) REFERENCES invoices(correlation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
                CREATE INDEX IF NOT EXISTS idx_logs_correlation ON process_logs(correlation_id);
                """
            )
            # Membaca kolom existing agar migrasi tetap kompatibel dengan database yang sudah dibuat sebelumnya.
            existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(invoices)")}
            # Menambahkan alasan validasi sekali saja untuk audit failure yang terstruktur.
            if "validation_reason" not in existing_columns:
                connection.execute("ALTER TABLE invoices ADD COLUMN validation_reason TEXT")
            # Menambahkan timestamp validasi sekali saja untuk traceability proses.
            if "validated_at" not in existing_columns:
                connection.execute("ALTER TABLE invoices ADD COLUMN validated_at TEXT")
            if "posting_state" not in existing_columns:
                connection.execute("ALTER TABLE invoices ADD COLUMN posting_state TEXT NOT NULL DEFAULT 'NOT_POSTED'")
            if "posting_error" not in existing_columns:
                connection.execute("ALTER TABLE invoices ADD COLUMN posting_error TEXT")
            if "archive_path" not in existing_columns:
                connection.execute("ALTER TABLE invoices ADD COLUMN archive_path TEXT")
            if "archive_hash" not in existing_columns:
                connection.execute("ALTER TABLE invoices ADD COLUMN archive_hash TEXT")
            attachment_columns = {row[1] for row in connection.execute("PRAGMA table_info(imap_attachments)")}
            if attachment_columns and "invoice_id" not in attachment_columns:
                connection.execute("ALTER TABLE imap_attachments ADD COLUMN invoice_id INTEGER")
            # Membuat tabel request approval; satu invoice hanya boleh memiliki satu request.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_requests (
                    id INTEGER PRIMARY KEY,
                    correlation_id TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL UNIQUE,
                    approver_email TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decision TEXT,
                    decided_by TEXT,
                    decided_at TEXT,
                    FOREIGN KEY(correlation_id) REFERENCES invoices(correlation_id)
                )
                """
            )
            # Membuat outbox persisten sebelum SMTP dipanggil agar delivery dapat dipulihkan.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_outbox (
                    id INTEGER PRIMARY KEY,
                    approval_request_id INTEGER NOT NULL UNIQUE,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    delivery_state TEXT NOT NULL DEFAULT 'READY',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    FOREIGN KEY(approval_request_id) REFERENCES approval_requests(id)
                )
                """
            )
            # Menyimpan setiap email reply agar duplicate message tidak mengubah keputusan dua kali.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_receipts (
                    id INTEGER PRIMARY KEY,
                    mailbox_uid TEXT NOT NULL UNIQUE,
                    sender_email TEXT,
                    result TEXT NOT NULL,
                    detail TEXT,
                    received_at TEXT NOT NULL
                )
                """
            )
            # Mempercepat pencarian outbox yang siap dikirim.
            connection.execute("CREATE INDEX IF NOT EXISTS idx_outbox_state ON approval_outbox(delivery_state)")
            # Metadata IMAP dipisahkan dari invoice agar satu email dapat membawa banyak attachment.
            connection.execute("""CREATE TABLE IF NOT EXISTS imap_messages (
                id INTEGER PRIMARY KEY, mailbox_identity TEXT NOT NULL, uidvalidity TEXT NOT NULL, uid TEXT NOT NULL,
                message_id TEXT, sender TEXT, subject TEXT, received_at TEXT, processing_status TEXT NOT NULL DEFAULT 'RECEIVED',
                error_reason TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(mailbox_identity, uidvalidity, uid))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS imap_attachments (
                id INTEGER PRIMARY KEY, message_db_id INTEGER NOT NULL, attachment_index INTEGER NOT NULL, original_filename TEXT,
                sha256 TEXT NOT NULL, storage_path TEXT, invoice_id INTEGER, processing_status TEXT NOT NULL DEFAULT 'PENDING', error_reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(message_db_id, attachment_index), FOREIGN KEY(message_db_id) REFERENCES imap_messages(id))""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_imap_attachment_state ON imap_attachments(processing_status)")

    def invoice_count(self) -> int:
        with closing(sqlite3.connect(self.path)) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0])

    def register_imap_message(self, mailbox: str, uidvalidity: str, uid: str, message_id: str | None, sender: str | None, subject: str | None, received_at: str | None) -> int | None:
        """Mendaftarkan identity email sekali; None berarti message sudah pernah terlihat."""
        with closing(sqlite3.connect(self.path)) as connection:
            try:
                cursor=connection.execute("INSERT INTO imap_messages(mailbox_identity,uidvalidity,uid,message_id,sender,subject,received_at) VALUES(?,?,?,?,?,?,?)",(mailbox,uidvalidity,uid,message_id,sender,subject,received_at))
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                return None

    def register_imap_attachment(self, message_db_id: int, attachment_index: int, original_filename: str | None, sha256: str, storage_path: str | None) -> bool:
        """Menyimpan metadata attachment; uniqueness index mencegah duplikasi saat restart."""
        with closing(sqlite3.connect(self.path)) as connection:
            try:
                connection.execute("INSERT INTO imap_attachments(message_db_id,attachment_index,original_filename,sha256,storage_path) VALUES(?,?,?,?,?)",(message_db_id,attachment_index,original_filename,sha256,storage_path))
                return True
            except sqlite3.IntegrityError:
                return False

    def register_stored_attachment_invoice(self, attachment_id: int) -> dict[str, Any]:
        """Mendaftarkan FILE_STORED sebagai invoice RECEIVED secara atomik dan idempotent."""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            attachment = connection.execute("SELECT * FROM imap_attachments WHERE id=?", (attachment_id,)).fetchone()
            if attachment is None:
                raise ValueError("attachment metadata not found")
            # Retry setelah sukses wajib mengembalikan invoice yang sama, tanpa insert ulang.
            if attachment["processing_status"] == "INVOICE_REGISTERED" and attachment["invoice_id"]:
                invoice = connection.execute("SELECT * FROM invoices WHERE id=?", (attachment["invoice_id"],)).fetchone()
                if invoice is not None:
                    return dict(invoice)
            if attachment["processing_status"] != "FILE_STORED":
                raise ValueError("attachment is not FILE_STORED")
            path = Path(attachment["storage_path"] or "")
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != attachment["sha256"]:
                raise ValueError("stored attachment missing or hash mismatch")
            existing = connection.execute("SELECT * FROM invoices WHERE file_hash=?", (attachment["sha256"],)).fetchone()
            if existing is None:
                correlation_id = str(uuid4())
                cursor = connection.execute("INSERT INTO invoices(correlation_id,file_path,file_hash,status) VALUES(?,?,?,'RECEIVED')", (correlation_id,str(path),attachment["sha256"]))
                invoice_id = cursor.lastrowid
                invoice = connection.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            else:
                invoice = existing; invoice_id = existing["id"]
            connection.execute("UPDATE imap_attachments SET invoice_id=?,processing_status='INVOICE_REGISTERED',updated_at=? WHERE id=?", (invoice_id,datetime.now(timezone.utc).isoformat(),attachment_id))
            self.add_log(connection, invoice["correlation_id"], "IMAP_INVOICE_REGISTRATION", "SUCCESS", "Stored attachment registered for OCR")
            return dict(invoice)

    def update_imap_attachment(self, message_db_id: int, attachment_index: int, status: str, error: str | None = None) -> None:
        """Mencatat hasil processing attachment tanpa menghapus audit asal email."""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE imap_attachments SET processing_status=?,error_reason=?,updated_at=? WHERE message_db_id=? AND attachment_index=?",(status,error,datetime.now(timezone.utc).isoformat(),message_db_id,attachment_index))

    def pending_imap_attachments(self) -> list[dict[str, Any]]:
        """Mengambil attachment pending/failed untuk recovery adapter tahap berikutnya."""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory=sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT a.*,m.mailbox_identity,m.uidvalidity,m.uid FROM imap_attachments a JOIN imap_messages m ON m.id=a.message_db_id WHERE a.processing_status IN ('PENDING','FAILED') ORDER BY a.id")]

    def register_invoice(self, invoice: InvoiceRegistration) -> bool:
        """Insert an inbound invoice. Returns False when its content already exists."""
        try:
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute(
                    """
                    INSERT INTO invoices (correlation_id, file_path, file_hash, status)
                    VALUES (?, ?, ?, 'RECEIVED')
                    """,
                    (invoice.correlation_id, invoice.file_path, invoice.file_hash),
                )
                self.add_log(
                    connection,
                    invoice.correlation_id,
                    "EMAIL_INTAKE",
                    "SUCCESS",
                    f"Invoice registered from {invoice.file_path}",
                )
            return True
        except sqlite3.IntegrityError:
            return False

    @staticmethod
    def add_log(
        connection: sqlite3.Connection,
        correlation_id: str | None,
        step: str,
        result: str,
        message: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO process_logs (correlation_id, step, result, message)
            VALUES (?, ?, ?, ?)
            """,
            (correlation_id, step, result, message),
        )

    def log_event(self, correlation_id: str | None, step: str, result: str, message: str) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            self.add_log(connection, correlation_id, step, result, message)

    def invoices(self) -> list[dict[str, Any]]:
        """Convenience read method for diagnostics and future export services."""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM invoices ORDER BY id")]

    def pending_ocr_invoices(self, include_failed: bool = False) -> list[dict[str, Any]]:
        """Return new invoices and, when explicitly requested, previous OCR failures."""
        # Membuat koneksi baca ke database lokal.
        with closing(sqlite3.connect(self.path)) as connection:
            # Mengaktifkan akses kolom berdasarkan nama.
            connection.row_factory = sqlite3.Row
            # Menentukan status yang boleh diproses ulang berdasarkan pilihan pengguna.
            statuses = ("RECEIVED", "OCR_FAILED") if include_failed else ("RECEIVED",)
            # Membuat placeholder SQLite dengan jumlah yang sesuai dengan status yang dipilih.
            placeholders = ", ".join("?" for _ in statuses)
            # Mengambil invoice yang siap masuk tahap OCR.
            rows = connection.execute(
                f"SELECT correlation_id, file_path FROM invoices WHERE status IN ({placeholders}) ORDER BY id",
                statuses,
            )
            # Mengubah setiap SQLite row menjadi dictionary biasa.
            return [dict(row) for row in rows]

    def save_ocr_result(self, correlation_id: str, ocr_text: str, extracted: Any) -> None:
        """Persist raw OCR text and regex fields for one successful invoice."""
        # Membuat timestamp UTC yang eksplisit untuk audit trail.
        updated_at = datetime.now(timezone.utc).isoformat()
        # Membuka transaksi SQLite untuk pembaruan invoice dan audit log.
        with closing(sqlite3.connect(self.path)) as connection:
            # Menyimpan output OCR, field hasil regex, dan status tahap OCR.
            connection.execute(
                """
                UPDATE invoices
                SET invoice_number = ?, vendor_name = ?, po_number = ?, amount = ?, currency = ?,
                    ocr_text = ?, status = 'EXTRACTED', updated_at = ?
                WHERE correlation_id = ?
                """,
                (
                    extracted.invoice_number,
                    extracted.vendor_name,
                    extracted.po_number,
                    str(extracted.amount) if extracted.amount is not None else None,
                    extracted.currency,
                    ocr_text,
                    updated_at,
                    correlation_id,
                ),
            )
            # Mencatat keberhasilan OCR tanpa menaruh isi invoice pada process log.
            self.add_log(connection, correlation_id, "OCR_EXTRACTION", "SUCCESS", "OCR and regex extraction completed")

    def mark_ocr_failed(self, correlation_id: str, message: str) -> None:
        """Mark a failed OCR attempt without losing the original inbound invoice."""
        # Membuat timestamp UTC untuk perubahan status error.
        updated_at = datetime.now(timezone.utc).isoformat()
        # Membuka transaksi untuk mengubah status dan menyimpan audit error.
        with closing(sqlite3.connect(self.path)) as connection:
            # Menandai invoice supaya tidak diproses ulang diam-diam pada run berikutnya.
            connection.execute(
                "UPDATE invoices SET status = 'OCR_FAILED', updated_at = ? WHERE correlation_id = ?",
                (updated_at, correlation_id),
            )
            # Menyimpan pesan error untuk troubleshooting.
            self.add_log(connection, correlation_id, "OCR_EXTRACTION", "FAILED", message)

    def extracted_invoices(self) -> list[dict[str, Any]]:
        """Return successfully OCR'd invoices for local regex re-extraction."""
        # Membuka koneksi baca untuk data OCR yang telah tersimpan.
        with closing(sqlite3.connect(self.path)) as connection:
            # Mengaktifkan akses nilai result berdasarkan nama kolom.
            connection.row_factory = sqlite3.Row
            # Mengambil teks OCR untuk seluruh invoice yang sudah diekstrak.
            rows = connection.execute(
                "SELECT correlation_id, ocr_text FROM invoices WHERE status = 'EXTRACTED' ORDER BY id"
            )
            # Mengembalikan hasil query sebagai list dictionary.
            return [dict(row) for row in rows]

    def pending_po_validation_invoices(self) -> list[dict[str, Any]]:
        """Return only OCR-complete invoices which have not been PO-validated."""
        # Membuka koneksi baca ke database lokal.
        with closing(sqlite3.connect(self.path)) as connection:
            # Mengaktifkan akses result berdasarkan nama kolom.
            connection.row_factory = sqlite3.Row
            # Membatasi query ke EXTRACTED agar re-run tidak memproses transaksi validasi yang sama.
            rows = connection.execute(
                """
                SELECT correlation_id, invoice_number, vendor_name, po_number, amount, currency
                FROM invoices WHERE status = 'EXTRACTED' ORDER BY id
                """
            )
            # Mengubah rows SQLite menjadi dictionary yang dipakai validator.
            return [dict(row) for row in rows]

    def save_po_validation_result(self, correlation_id: str, result: Any) -> bool:
        """Persist one PO result atomically and return False when it was already handled."""
        # Membuat timestamp UTC yang eksplisit untuk audit validation.
        validated_at = datetime.now(timezone.utc).isoformat()
        # Menetapkan status workflow berdasarkan hasil validator.
        workflow_status = "PO_VALIDATED" if result.is_valid else "PO_VALIDATION_FAILED"
        # Menetapkan hasil structured log berdasarkan sukses atau gagal.
        log_result = "SUCCESS" if result.is_valid else "FAILED"
        # Membuka transaksi yang memperbarui invoice dan menulis satu audit log.
        with closing(sqlite3.connect(self.path)) as connection:
            # Mengubah record hanya bila tetap berada pada tahap EXTRACTED untuk menjamin idempotency.
            cursor = connection.execute(
                """
                UPDATE invoices
                SET match_status = ?, validation_reason = ?, validated_at = ?, status = ?, updated_at = ?
                WHERE correlation_id = ? AND status = 'EXTRACTED'
                """,
                (result.match_status, result.reason, validated_at, workflow_status, validated_at, correlation_id),
            )
            # Menghentikan tanpa menulis log tambahan jika record sudah ditangani run lain/sebelumnya.
            if cursor.rowcount == 0:
                return False
            # Menulis audit log hanya untuk perubahan state yang benar-benar baru.
            self.add_log(connection, correlation_id, "PO_VALIDATION", log_result, result.reason)
        # Menandai bahwa satu hasil validasi baru berhasil disimpan.
        return True

    def pending_approval_invoices(self) -> list[dict[str, Any]]:
        """Return only PO-approved invoices that have not entered the approval workflow."""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT correlation_id, invoice_number, vendor_name, po_number, amount, currency
                FROM invoices WHERE status = 'PO_VALIDATED' AND approval_status = 'NOT_REQUIRED' ORDER BY id
                """
            )
            invoices = [dict(row) for row in rows]
            # Mengubah nominal SQLite menjadi Decimal sebelum dibandingkan dengan threshold Excel.
            for invoice in invoices:
                invoice["amount"] = Decimal(invoice["amount"])
            return invoices

    def pending_erp_invoices(self) -> list[dict[str, Any]]:
        """Mengambil hanya approval final yang belum pernah diposting atau uncertain."""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("""SELECT correlation_id, invoice_number, vendor_name, po_number, amount, currency, approval_status, posting_ref FROM invoices
            WHERE status = 'APPROVED' AND approval_status IN ('APPROVED', 'AUTO_APPROVED') AND posting_state = 'NOT_POSTED' ORDER BY id""")
            return [dict(row) for row in rows]

    def reserve_posting(self, correlation_id: str) -> dict[str, Any] | None:
        """Membuat PostingRef persisten sebelum browser membuka form."""
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM invoices WHERE correlation_id = ? AND posting_state = 'NOT_POSTED' AND status = 'APPROVED'", (correlation_id,)).fetchone()
            if not row:
                return None
            posting_ref = f"POST-{secrets.token_hex(8).upper()}"
            changed = connection.execute("UPDATE invoices SET posting_ref = ?, posting_state = 'POSTING_PENDING', updated_at = ? WHERE correlation_id = ? AND posting_state = 'NOT_POSTED'", (posting_ref, now, correlation_id))
            if not changed.rowcount:
                return None
            data = dict(row); data["posting_ref"] = posting_ref
            self.add_log(connection, correlation_id, "ERP_POSTING", "POSTING_PENDING", "Posting reference reserved")
            return data

    def set_posting_state(self, correlation_id: str, state: str, error: str | None = None) -> None:
        """Menyimpan state posting dan audit tanpa mengubah decision approval."""
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE invoices SET posting_state = ?, posting_error = ?, posted_at = CASE WHEN ? = 'POSTED' THEN ? ELSE posted_at END, updated_at = ? WHERE correlation_id = ?", (state, error, state, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), correlation_id))
            self.add_log(connection, correlation_id, "ERP_POSTING", state, error or state)

    def prepare_approval(self, invoice: dict[str, Any], threshold: Any, approver_email: str, ttl_hours: int) -> dict[str, str] | None:
        """Atomically auto-approve or create a pending request and durable outbox entry."""
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        with closing(sqlite3.connect(self.path)) as connection:
            amount = invoice["amount"]
            if amount <= threshold:
                cursor = connection.execute(
                    """UPDATE invoices SET needs_approval = 0, approval_status = 'AUTO_APPROVED', status = 'APPROVED', updated_at = ?
                    WHERE correlation_id = ? AND status = 'PO_VALIDATED' AND approval_status = 'NOT_REQUIRED'""",
                    (now_text, invoice["correlation_id"]),
                )
                if cursor.rowcount == 0:
                    return None
                self.add_log(connection, invoice["correlation_id"], "EMAIL_APPROVAL", "AUTO_APPROVED", "Amount is at or below configured threshold")
                return {"action": "AUTO_APPROVED"}
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            expires_at = (now + timedelta(hours=ttl_hours)).isoformat()
            cursor = connection.execute(
                """UPDATE invoices SET needs_approval = 1, approval_status = 'PENDING_APPROVAL', status = 'PENDING_APPROVAL', updated_at = ?
                WHERE correlation_id = ? AND status = 'PO_VALIDATED' AND approval_status = 'NOT_REQUIRED'""",
                (now_text, invoice["correlation_id"]),
            )
            if cursor.rowcount == 0:
                return None
            request_cursor = connection.execute(
                """INSERT INTO approval_requests (correlation_id, token_hash, approver_email, requested_at, expires_at)
                VALUES (?, ?, ?, ?, ?)""",
                (invoice["correlation_id"], token_hash, approver_email.lower(), now_text, expires_at),
            )
            subject = f"Approval required: invoice {invoice.get('invoice_number') or invoice['correlation_id']}"
            body = (
                "Please reply using exactly this format:\n\n"
                "APPROVE\n"
                f"Approval Token: {token}\n\n"
                "or\n\n"
                "REJECT\n"
                f"Approval Token: {token}\n\n"
                f"Invoice Number: {invoice.get('invoice_number') or '-'}\n"
                f"Vendor: {invoice.get('vendor_name') or '-'}\n"
                f"PO Number: {invoice.get('po_number') or '-'}\n"
                f"Amount: {invoice['amount']} {invoice.get('currency') or ''}\n"
                f"Correlation ID: {invoice['correlation_id']}\n"
            )
            connection.execute(
                """INSERT INTO approval_outbox (approval_request_id, recipient, subject, body, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (request_cursor.lastrowid, approver_email.lower(), subject, body, now_text),
            )
            self.add_log(connection, invoice["correlation_id"], "EMAIL_APPROVAL", "PENDING_APPROVAL", "Approval request queued in persistent outbox")
            return {"action": "PENDING_APPROVAL"}

    def approval_outbox_ids(self, retry_failed: bool = False) -> list[int]:
        """Return candidates without claiming them; claim_outbox is the concurrency guard."""
        states = ("READY", "FAILED") if retry_failed else ("READY",)
        placeholders = ", ".join("?" for _ in states)
        with closing(sqlite3.connect(self.path)) as connection:
            return [row[0] for row in connection.execute(f"SELECT id FROM approval_outbox WHERE delivery_state IN ({placeholders}) ORDER BY id", states)]

    def claim_outbox(self, outbox_id: int, retry_failed: bool = False) -> dict[str, Any] | None:
        """Claim one READY/FAILED message once, leaving uncertain SENDING messages untouched."""
        allowed = ("READY", "FAILED") if retry_failed else ("READY",)
        placeholders = ", ".join("?" for _ in allowed)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                f"""SELECT o.*, r.correlation_id FROM approval_outbox o JOIN approval_requests r ON r.id = o.approval_request_id
                WHERE o.id = ? AND o.delivery_state IN ({placeholders}) AND o.attempt_count < 3""",
                (outbox_id, *allowed),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                f"UPDATE approval_outbox SET delivery_state = 'SENDING', attempt_count = attempt_count + 1 WHERE id = ? AND delivery_state IN ({placeholders})",
                (outbox_id, *allowed),
            )
            return dict(row) if updated.rowcount else None

    def mark_outbox_sent(self, outbox_id: int) -> bool:
        """Mark a successfully submitted SMTP message sent exactly once."""
        now_text = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute(
                "SELECT r.correlation_id FROM approval_outbox o JOIN approval_requests r ON r.id = o.approval_request_id WHERE o.id = ?",
                (outbox_id,),
            ).fetchone()
            cursor = connection.execute("UPDATE approval_outbox SET delivery_state = 'SENT', sent_at = ?, last_error = NULL WHERE id = ? AND delivery_state = 'SENDING'", (now_text, outbox_id))
            if not cursor.rowcount:
                return False
            self.add_log(connection, row[0], "EMAIL_DELIVERY", "SENT", "Approval email accepted by SMTP server")
            return True

    def mark_outbox_failed(self, outbox_id: int, error: str) -> None:
        """Persist a safe, bounded error summary for explicit retry."""
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT r.correlation_id FROM approval_outbox o JOIN approval_requests r ON r.id = o.approval_request_id WHERE o.id = ?", (outbox_id,)).fetchone()
            connection.execute("UPDATE approval_outbox SET delivery_state = 'FAILED', last_error = ? WHERE id = ? AND delivery_state = 'SENDING'", (error[:500], outbox_id))
            if row:
                self.add_log(connection, row[0], "EMAIL_DELIVERY", "FAILED", "SMTP delivery failed; explicit retry required")

    def record_approval_reply(self, mailbox_uid: str, sender_email: str, token: str | None, decision: str | None, approver_email: str) -> str:
        """Validate and apply one reply atomically; duplicate UID/decision causes no state change."""
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest() if token else None
        with closing(sqlite3.connect(self.path)) as connection:
            existing = connection.execute("SELECT 1 FROM approval_receipts WHERE mailbox_uid = ?", (mailbox_uid,)).fetchone()
            if existing:
                return "DUPLICATE_EMAIL"
            if sender_email.lower() != approver_email.lower():
                return self._save_receipt(connection, mailbox_uid, sender_email, "UNAUTHORIZED_APPROVER", "Sender does not match configured approver", now_text)
            if decision not in {"APPROVE", "REJECT"} or not token_hash:
                return self._save_receipt(connection, mailbox_uid, sender_email, "MALFORMED_REPLY", "Reply must contain decision and token", now_text)
            request = connection.execute("SELECT * FROM approval_requests WHERE token_hash = ?", (token_hash,)).fetchone()
            if request is None:
                return self._save_receipt(connection, mailbox_uid, sender_email, "INVALID_TOKEN", "Token does not match an approval request", now_text)
            if datetime.fromisoformat(request[5]) <= now:
                return self._save_receipt(connection, mailbox_uid, sender_email, "EXPIRED_TOKEN", "Approval token has expired", now_text)
            if request[6] is not None:
                return self._save_receipt(connection, mailbox_uid, sender_email, "DUPLICATE_DECISION", "Approval request is already decided", now_text)
            approval_status = "APPROVED" if decision == "APPROVE" else "REJECTED"
            connection.execute("UPDATE approval_requests SET decision = ?, decided_by = ?, decided_at = ? WHERE id = ?", (decision, sender_email.lower(), now_text, request[0]))
            connection.execute("UPDATE invoices SET approval_status = ?, status = ?, updated_at = ? WHERE correlation_id = ?", (approval_status, approval_status, now_text, request[1]))
            self.add_log(connection, request[1], "EMAIL_APPROVAL_REPLY", approval_status, f"Decision received from configured approver: {approval_status}")
            return self._save_receipt(connection, mailbox_uid, sender_email, approval_status, "Valid approval decision applied", now_text)

    @staticmethod
    def _save_receipt(connection: sqlite3.Connection, mailbox_uid: str, sender_email: str, result: str, detail: str, received_at: str) -> str:
        """Save a receipt before returning its result to the IMAP workflow."""
        connection.execute("INSERT INTO approval_receipts (mailbox_uid, sender_email, result, detail, received_at) VALUES (?, ?, ?, ?, ?)", (mailbox_uid, sender_email, result, detail, received_at))
        return result
