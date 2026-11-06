"""Parsing MIME in-memory untuk pipeline email invoice."""
from __future__ import annotations
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from src.attachment_validation import ALLOWED, SIGNATURES

@dataclass(frozen=True)
class EmailMetadata:
    message_id: str | None; sender: str; subject: str; date: str | None
@dataclass(frozen=True)
class ExtractedAttachment:
    index: int; filename: str | None; content_type: str; payload: bytes; error: str | None

def parse_email_attachments(raw: bytes, max_bytes: int) -> tuple[EmailMetadata, list[ExtractedAttachment]]:
    """Memparse raw MIME dan memvalidasi semua attachment tanpa filesystem/network."""
    try:
        message=BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as error:
        raise ValueError('malformed MIME message') from error
    metadata=EmailMetadata(message.get('Message-ID'),parseaddr(message.get('From',''))[1],str(message.get('Subject','')),message.get('Date'))
    result=[]; index=0
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() != 'attachment': continue
        payload=part.get_payload(decode=True) or b''; filename=part.get_filename(); mime=part.get_content_type(); suffix=(filename or '').lower().rsplit('.',1)[-1] if '.' in (filename or '') else ''
        extension='.'+suffix if suffix else ''
        # Error per attachment tidak menghentikan attachment lain.
        error=None
        if extension not in ALLOWED or mime != ALLOWED.get(extension): error='extension or MIME mismatch'
        elif not payload or len(payload)>max_bytes or not payload.startswith(SIGNATURES[extension]): error='empty, oversized, or corrupt attachment'
        result.append(ExtractedAttachment(index,filename,mime,payload,error)); index+=1
    return metadata,result
