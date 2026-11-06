"""Email approval orchestration with a persistent SMTP outbox."""

from __future__ import annotations

import imaplib
import os
import re
import smtplib
from dataclasses import dataclass
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Protocol


class EmailConfigurationError(ValueError):
    """Raised when required mail settings are unavailable."""


class Mailer(Protocol):
    """Minimal SMTP contract, allowing tests to use a mock without real email."""

    def send(self, recipient: str, subject: str, body: str) -> None: ...


@dataclass(frozen=True)
class EmailSettings:
    """SMTP and IMAP settings read only from environment variables."""

    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    imap_host: str
    imap_port: int
    imap_username: str
    imap_password: str

    @classmethod
    def from_environment(cls) -> "EmailSettings":
        """Build settings without logging any credential value."""
        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise EmailConfigurationError(f"{name} is missing; add it to .env")
            return value

        return cls(
            smtp_host=required("SMTP_HOST"),
            smtp_port=int(os.getenv("SMTP_PORT", "587")),
            smtp_username=required("SMTP_USERNAME"),
            smtp_password=required("SMTP_PASSWORD"),
            imap_host=required("IMAP_HOST"),
            imap_port=int(os.getenv("IMAP_PORT", "993")),
            imap_username=required("IMAP_USERNAME"),
            imap_password=required("IMAP_PASSWORD"),
        )


class SmtpMailer:
    """SMTP implementation with timeout; it never logs message secrets or credentials."""

    def __init__(self, settings: EmailSettings) -> None:
        self.settings = settings

    def send(self, recipient: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.settings.smtp_username
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as client:
            client.starttls()
            client.login(self.settings.smtp_username, self.settings.smtp_password)
            client.send_message(message)

    def check_connectivity(self) -> None:
        """Verifikasi TLS, login, dan SMTP NOOP tanpa mengirim email."""
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as client:
            client.starttls()
            client.login(self.settings.smtp_username, self.settings.smtp_password)
            client.noop()


@dataclass(frozen=True)
class ApprovalReply:
    """Parsed, untrusted content from an incoming approval email."""

    sender_email: str
    token: str | None
    decision: str | None


def parse_reply(raw_message: bytes) -> ApprovalReply:
    """Parse a reply using the documented body format without executing email content."""
    message = message_from_bytes(raw_message)
    sender_email = parseaddr(message.get("From", ""))[1].strip().lower()
    # Hanya membaca reply terbaru agar APPROVE/REJECT dalam quoted message tidak dianggap keputusan baru.
    body = _latest_reply_segment(_plain_text_body(message))
    token_match = re.search(r"^\s*Approval\s+Token\s*:\s*([A-Za-z0-9_-]{20,})\s*$", body, re.IGNORECASE | re.MULTILINE)
    decision_match = re.search(r"^\s*(APPROVE|REJECT)\s*$", body, re.IGNORECASE | re.MULTILINE)
    return ApprovalReply(sender_email, token_match.group(1) if token_match else None, decision_match.group(1).upper() if decision_match else None)


def _plain_text_body(message: object) -> str:
    """Return text/plain email content while ignoring HTML and attachments."""
    if getattr(message, "is_multipart")():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = message.get_payload(decode=True) or b""
    return payload.decode(message.get_content_charset() or "utf-8", errors="replace")


def _latest_reply_segment(body: str) -> str:
    """Hilangkan quoted original message berdasarkan marker umum reply email."""
    kept: list[str] = []
    for line in body.splitlines():
        if line.startswith(">") or re.match(r"^On .+ wrote:$", line, re.IGNORECASE) or re.match(r"^-{2,}\s*Original Message", line, re.IGNORECASE):
            break
        kept.append(line)
    return "\n".join(kept)


class ImapInbox:
    """One-shot IMAP reader; callers schedule it rather than polling indefinitely."""

    def __init__(self, settings: EmailSettings) -> None:
        self.settings = settings

    def unread_messages(self) -> list[tuple[str, bytes]]:
        """Fetch unread messages once and mark them seen only after retrieval."""
        messages: list[tuple[str, bytes]] = []
        with imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port, timeout=30) as client:
            client.login(self.settings.imap_username, self.settings.imap_password)
            client.select("INBOX")
            status, data = client.uid("search", None, "UNSEEN")
            if status != "OK":
                raise OSError("IMAP search failed")
            for uid in data[0].split():
                status, payload = client.uid("fetch", uid, "(RFC822)")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                messages.append((uid.decode(), payload[0][1]))
                client.uid("store", uid, "+FLAGS", "\\Seen")
        return messages

    def check_connectivity(self) -> None:
        """Verifikasi TLS, login, dan akses INBOX tanpa membaca atau menandai email."""
        with imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port, timeout=30) as client:
            client.login(self.settings.imap_username, self.settings.imap_password)
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise OSError("IMAP INBOX access failed")


class ApprovalWorkflow:
    """Coordinates durable approval state with replaceable SMTP/IMAP adapters."""

    def __init__(self, database: object, logger: object) -> None:
        self.database = database
        self.logger = logger

    def initiate(self, threshold: object, approver_email: str, ttl_hours: int, mailer: Mailer | None) -> dict[str, int]:
        """Queue eligible requests, auto-approve lower amounts, then attempt outbox delivery."""
        # Membuat counter batch tanpa mencatat token atau credential.
        summary = {"auto_approved": 0, "pending": 0, "sent": 0, "failed": 0}
        # Mengambil hanya invoice yang sudah lolos validasi PO dari database.
        for invoice in self.database.pending_approval_invoices():
            # Menulis state approval dan outbox secara atomik sebelum SMTP dipanggil.
            outcome = self.database.prepare_approval(invoice, threshold, approver_email, ttl_hours)
            if outcome is None:
                continue
            if outcome["action"] == "AUTO_APPROVED":
                summary["auto_approved"] += 1
            else:
                summary["pending"] += 1
        if mailer:
            # Mengirim pesan yang telah aman berada di outbox persisten.
            delivery = self.deliver(mailer)
            summary["sent"] = delivery["sent"]
            summary["failed"] = delivery["failed"]
        return summary

    def deliver(self, mailer: Mailer, retry_failed: bool = False) -> dict[str, int]:
        """Deliver durable outbox messages with at most three explicit attempts."""
        # Membuat ringkasan hasil delivery satu batch.
        summary = {"sent": 0, "failed": 0}
        # Memproses hanya state READY atau FAILED yang dipilih secara eksplisit.
        for outbox_id in self.database.approval_outbox_ids(retry_failed=retry_failed):
            # Mengunci satu record menjadi SENDING agar worker lain tidak mengirim pesan yang sama.
            message = self.database.claim_outbox(outbox_id, retry_failed=retry_failed)
            if message is None:
                continue
            try:
                # Menyerahkan email ke SMTP; isi pesan/token tidak pernah masuk log.
                mailer.send(message["recipient"], message["subject"], message["body"])
                if self.database.mark_outbox_sent(outbox_id):
                    summary["sent"] += 1
            except (OSError, smtplib.SMTPException, TimeoutError) as error:
                # Menyimpan FAILED untuk retry eksplisit dan terbatas, tanpa mengubah status invoice pending.
                self.database.mark_outbox_failed(outbox_id, type(error).__name__)
                summary["failed"] += 1
                self.logger.warning("approval_delivery_failed outbox_id=%s error=%s", outbox_id, type(error).__name__)
        return summary

    def process_replies(self, messages: list[tuple[str, bytes]], approver_email: str) -> dict[str, int]:
        """Validate a finite batch of IMAP replies and return non-sensitive counters."""
        # Mengagregasi hasil reply tanpa menyimpan token dalam log.
        summary: dict[str, int] = {}
        # Memeriksa setiap email dari batch IMAP satu kali.
        for mailbox_uid, raw_message in messages:
            # Memparse sender, token, dan keputusan sebagai input yang belum dipercaya.
            reply = parse_reply(raw_message)
            # Database memvalidasi sender, token hash, expiry, serta duplicate secara atomik.
            result = self.database.record_approval_reply(mailbox_uid, reply.sender_email, reply.token, reply.decision, approver_email)
            summary[result] = summary.get(result, 0) + 1
            self.logger.info("approval_reply_result uid=%s result=%s", mailbox_uid, result)
        return summary
