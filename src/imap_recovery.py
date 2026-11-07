"""Recovery offline attachment IMAP tanpa network atau payload baru."""
from __future__ import annotations
import errno
import sqlite3
from src.database import Database

def classify_recovery_error(exc: Exception) -> str:
    """Mengklasifikasikan error tanpa membuat retry loop di batch saat ini."""
    if isinstance(exc, sqlite3.OperationalError) and any(word in str(exc).lower() for word in ('locked','busy')): return 'TRANSIENT'
    if isinstance(exc, FileNotFoundError): return 'NEEDS_REFETCH'
    if isinstance(exc, OSError) and exc.errno in (errno.EAGAIN,errno.EBUSY,errno.EINTR): return 'TRANSIENT'
    if isinstance(exc, ValueError): return 'PERMANENT'
    return 'UNKNOWN'

def recover(database: Database) -> dict[str,int]:
    """Lanjutkan FILE_STORED/INVOICE_REGISTERED per attachment tanpa menghentikan batch."""
    result={'completed':0,'deferred':0,'failed':0}
    for item in database.recoverable_imap_attachments():
        try:
            if item['processing_status']=='DISCOVERED': result['deferred']+=1; continue
            if item['processing_status']=='FILE_STORED': database.register_stored_attachment_invoice(item['id'])
            if database.finalize_imap_attachment(item['id']): result['completed']+=1
        except Exception as error:
            # Attachment lain tetap lanjut; state ditentukan oleh klasifikasi yang eksplisit.
            classification=classify_recovery_error(error)
            status='NEEDS_REFETCH' if classification=='NEEDS_REFETCH' else ('FAILED' if classification=='PERMANENT' else item['processing_status'])
            database.record_imap_attachment_error(item['id'],classification,type(error).__name__,status)
            result['failed']+=1
    return result
