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


@dataclass(frozen=True)
class ApprovalReplyDiagnostic:
    """Diagnostic non-sensitif untuk satu reply; tidak menyimpan body, sender, atau token."""
    uid: str
    content_type: str
    content_transfer_encoding: str
    decision_detected: bool
    token_label_detected: bool
    reply_format_valid: bool
    failure_category: str | None


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
                payload=part.get_payload(decode=True)
                if payload is None:
                    raw=part.get_payload()
                    return raw if isinstance(raw,str) else ''
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
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


def diagnose_reply(raw_message: bytes, uid: str) -> ApprovalReplyDiagnostic:
    """Mengklasifikasikan format MIME reply tanpa membocorkan isi/sender/token atau mengubah database."""
    message=message_from_bytes(raw_message)
    body=_plain_text_body(message); latest=_latest_reply_segment(body); parsed=parse_reply(raw_message)
    decision=bool(re.search(r"^\s*(APPROVE|REJECT)\s*$",latest,re.IGNORECASE|re.MULTILINE))
    token_label=bool(re.search(r"^\s*Approval\s+Token\s*:",latest,re.IGNORECASE|re.MULTILINE))
    valid=parsed.decision is not None and parsed.token is not None
    category=None
    if not body: category='NO_TEXT_PLAIN_CONTENT'
    elif not latest.strip() and body.strip(): category='QUOTED_CONTENT_ONLY'
    elif not decision and re.search(r"APPROVE|REJECT",body,re.IGNORECASE): category='DECISION_ONLY_IN_QUOTED_CONTENT'
    elif not decision: category='MISSING_DECISION'
    elif not token_label: category='MISSING_TOKEN_LABEL'
    elif not valid: category='INVALID_TOKEN_FORMAT'
    return ApprovalReplyDiagnostic(
        uid=uid,content_type=message.get_content_type(),
        content_transfer_encoding=(message.get('Content-Transfer-Encoding') or 'none').lower(),
        decision_detected=decision,token_label_detected=token_label,
        reply_format_valid=valid,failure_category=category,
    )


class ImapInbox:
    """Pembaca reply IMAP terbatas dan read-only; tidak pernah mengubah flag mailbox."""

    def __init__(self, settings: EmailSettings, timeout_seconds: int = 30) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _payload(payload: object) -> bytes | None:
        """Mengambil bytes dari respons FETCH tanpa mengasumsikan struktur provider tertentu."""
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], tuple): return None
        data=payload[0][1]
        return data if isinstance(data,bytes) else None

    @staticmethod
    def _quote_search_value(value: str) -> str:
        """Membuat quoted-string RFC 3501; imaplib tidak melakukan quoting otomatis."""
        return '"'+value.replace('\\','\\\\').replace('"','\\"')+'"'

    def _search_criteria(self, approver_email: str | None) -> tuple[str,...]:
        """Membangun atom SEARCH yang valid tanpa mengandalkan filter ini untuk autentikasi."""
        if not approver_email: return ('UNSEEN',)
        return ('UNSEEN','FROM',self._quote_search_value(approver_email),'TEXT',self._quote_search_value('Approval Token:'))

    def _fetch_reply_raw(self, client: object, uid: bytes) -> bytes | None:
        """Merekonstruksi MIME dari header penting dan BODY.PEEK[TEXT], tanpa RFC822/attachment."""
        header_spec='(BODY.PEEK[HEADER.FIELDS (FROM CONTENT-TYPE CONTENT-TRANSFER-ENCODING MIME-VERSION)])'
        status,header_payload=client.uid('fetch',uid,header_spec)
        if status!='OK': return None
        status,text_payload=client.uid('fetch',uid,'(BODY.PEEK[TEXT])')
        header=self._payload(header_payload); text=self._payload(text_payload)
        if status!='OK' or header is None or text is None: return None
        return header+b'\r\n'+text

    def unread_messages(self, approver_email: str | None = None, max_messages: int = 10, processed_uids: set[str] | None = None) -> list[tuple[str, bytes]]:
        """Ambil maksimal reply kandidat terbaru tanpa RFC822/attachment/full mailbox fetch."""
        if not 1 <= max_messages <= 100: raise ValueError('max_messages must be between 1 and 100')
        processed_uids=processed_uids or set(); messages: list[tuple[str,bytes]]=[]
        with imaplib.IMAP4_SSL(self.settings.imap_host,self.settings.imap_port,timeout=self.timeout_seconds) as client:
            client.login(self.settings.imap_username,self.settings.imap_password)
            # Read-only dan BODY.PEEK memastikan message tidak menjadi Seen.
            status,_=client.select('INBOX',readonly=True)
            if status!='OK': raise OSError('IMAP INBOX read-only access failed')
            # Filter server hanya optimasi; sender dan token selalu divalidasi ulang di database.
            status,data=client.uid('search',None,*self._search_criteria(approver_email))
            if status!='OK': raise OSError(f'IMAP search failed with server status {status}')
            uids=(data[0] if data else b'').split()
            # Ambil UID terbaru yang belum punya receipt; batch berikutnya kemudian dapat melanjutkan yang lebih lama.
            selected=[]
            for uid in reversed(uids):
                uid_text=uid.decode()
                if uid_text in processed_uids: continue
                selected.append(uid)
                if len(selected)==max_messages: break
            for uid in selected:
                # Header MIME penting diperlukan agar multipart/encoding dapat diurai tanpa RFC822 penuh.
                raw=self._fetch_reply_raw(client,uid)
                if raw is not None: messages.append((uid.decode(),raw))
        return messages

    def diagnose_uid(self, uid: str) -> ApprovalReplyDiagnostic:
        """Diagnostic satu UID melalui IMAP read-only; tidak memproses reply maupun menyentuh database."""
        if not re.fullmatch(r'\d+',uid): raise ValueError('UID diagnostic must be numeric')
        with imaplib.IMAP4_SSL(self.settings.imap_host,self.settings.imap_port,timeout=self.timeout_seconds) as client:
            client.login(self.settings.imap_username,self.settings.imap_password)
            status,_=client.select('INBOX',readonly=True)
            if status!='OK': raise OSError('IMAP INBOX read-only access failed')
            raw=self._fetch_reply_raw(client,uid.encode())
            if raw is None: raise OSError('IMAP diagnostic fetch failed')
            return diagnose_reply(raw,uid)

    def check_connectivity(self) -> None:
        """Verifikasi TLS, login, dan akses INBOX tanpa membaca atau menandai email."""
        with imaplib.IMAP4_SSL(self.settings.imap_host, self.settings.imap_port, timeout=self.timeout_seconds) as client:
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
