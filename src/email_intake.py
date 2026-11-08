"""Parsing MIME in-memory untuk pipeline email invoice."""
from __future__ import annotations
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from src.attachment_validation import ALLOWED, SIGNATURES
from src.durable_storage import store_attachment
from src.imap_adapter import ImapMessage, ReadOnlyImapAdapter
from src.imap_recovery import classify_recovery_error, recover
from pathlib import Path
from src.database import Database
import hashlib, logging, sqlite3
from contextlib import closing

LOGGER = logging.getLogger(__name__)

STORAGE_SUFFIXES = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
}

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

def register_parsed_email(raw: bytes, mailbox_identity: str, uidvalidity: str, uid: str, db_path: Path, max_bytes: int) -> tuple[int, EmailMetadata, list[ExtractedAttachment]]:
    """Memparse dan mendaftarkan message UID sekali tanpa menyimpan attachment/file."""
    metadata,attachments=parse_email_attachments(raw,max_bytes)
    database=Database(db_path); database.initialise()
    # Insert baru atau gunakan kembali identity yang sama saat batch diulang.
    message_id=database.register_imap_message(mailbox_identity,uidvalidity,uid,metadata.message_id,metadata.sender,metadata.subject,metadata.date)
    if message_id is None:
        message_id=database.imap_message_id(mailbox_identity,uidvalidity,uid)
    if message_id is None: raise RuntimeError('IMAP message identity lookup failed')
    return message_id,metadata,attachments

def register_parsed_attachments(parsed: tuple[int, EmailMetadata, list[ExtractedAttachment]], db_path: Path) -> list[dict]:
    """Mendaftarkan metadata attachment secara idempotent tanpa menyimpan payload ke SQLite."""
    message_id,_,attachments=parsed; database=Database(db_path); database.initialise(); results=[]
    for attachment in attachments:
        digest=hashlib.sha256(attachment.payload).hexdigest()
        status='FAILED' if attachment.error else 'DISCOVERED'
        database.register_imap_attachment(message_id,attachment.index,attachment.filename,digest,None,attachment.content_type,status,attachment.error)
        # Unique constraint dapat menolak duplicate; baca record existing tanpa mereset state.
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory=sqlite3.Row
            row=connection.execute('SELECT id,processing_status FROM imap_attachments WHERE message_db_id=? AND attachment_index=?',(message_id,attachment.index)).fetchone()
        # Payload baru dari UID yang sama dapat mengaktifkan ulang attachment yang meminta re-fetch.
        if row['processing_status']=='NEEDS_REFETCH' and not attachment.error:
            database.reactivate_needs_refetch_attachment(row['id'])
            row={'id':row['id'],'processing_status':'DISCOVERED'}
        results.append({
            'attachment_id':row['id'],
            'status':row['processing_status'],
            'payload':attachment.payload,
            'filename':attachment.filename,
            'mime_type':attachment.content_type,
            'sha256':digest,
            'validation_error':attachment.error,
        })
    return results

def store_registered_attachments(registered: list[dict], db_path: Path, storage_directory: Path) -> list[dict]:
    """Menyimpan attachment DISCOVERED/FILE_STORED dan melanjutkan batch saat satu item gagal."""
    database=Database(db_path); database.initialise(); results=[]
    for attachment in registered:
        attachment_id=attachment['attachment_id']; current_status=attachment['status']
        result={**attachment, 'storage_path':None, 'error_class':None, 'storage_error':None}
        # State final atau tahap sesudah storage tidak boleh ditulis ulang.
        if current_status in {'FAILED','INVOICE_REGISTERED','COMPLETED'}:
            results.append(result); continue
        if current_status not in {'DISCOVERED','FILE_STORED'}:
            results.append(result); continue
        try:
            # Suffix berasal dari MIME tervalidasi, bukan raw filename dari pengirim.
            suffix=STORAGE_SUFFIXES.get(attachment.get('mime_type'))
            if suffix is None:
                raise ValueError('unsupported attachment MIME type for storage')
            path=store_attachment(db_path,attachment_id,attachment['payload'],suffix,storage_directory)
            result.update(status='FILE_STORED',storage_path=path)
        except Exception as error:
            # Klasifikasi menentukan state tanpa retry langsung dalam batch yang sama.
            classification=classify_recovery_error(error)
            next_status=('FAILED' if classification=='PERMANENT' else
                         'NEEDS_REFETCH' if classification=='NEEDS_REFETCH' else current_status)
            reason=f'{type(error).__name__}: {error}'
            database.record_imap_attachment_error(attachment_id,classification,reason,next_status)
            result.update(status=next_status,error_class=classification,storage_error=reason)
            LOGGER.warning('attachment_storage_failed',extra={
                'attachment_id':attachment_id,
                'error_class':classification,
                'processing_status':next_status,
            })
        results.append(result)
    return results

def finalize_stored_attachments(stored: list[dict], db_path: Path) -> list[dict]:
    """Mendaftarkan attachment FILE_STORED ke invoice lalu memfinalisasi audit attachment."""
    database=Database(db_path); database.initialise(); results=[]
    for attachment in stored:
        attachment_id=attachment['attachment_id']; current_status=attachment['status']
        result={**attachment, 'invoice':None, 'error_class':None, 'finalization_error':None}
        # State terminal/non-eligible tidak boleh memicu invoice registration baru.
        if current_status in {'COMPLETED','FAILED','NEEDS_REFETCH'}:
            results.append(result); continue
        if current_status not in {'FILE_STORED','INVOICE_REGISTERED'}:
            results.append(result); continue
        try:
            # Registrasi hanya diperlukan saat file telah durable; fungsi repository idempotent.
            if current_status=='FILE_STORED':
                result['invoice']=database.register_stored_attachment_invoice(attachment_id)
            # INVOICE_REGISTERED cukup melanjutkan finalisasi relasi yang sudah ada.
            database.finalize_imap_attachment(attachment_id)
            result['status']='COMPLETED'
        except Exception as error:
            # Satu kegagalan dicatat tanpa menghentikan attachment lain pada batch yang sama.
            classification=classify_recovery_error(error)
            next_status=('FAILED' if classification=='PERMANENT' else
                         'NEEDS_REFETCH' if classification=='NEEDS_REFETCH' else current_status)
            reason=f'{type(error).__name__}: {error}'
            database.record_imap_attachment_error(attachment_id,classification,reason,next_status)
            result.update(status=next_status,error_class=classification,finalization_error=reason)
            LOGGER.warning('attachment_finalization_failed',extra={
                'attachment_id':attachment_id,
                'error_class':classification,
                'processing_status':next_status,
            })
        results.append(result)
    return results

def _process_imap_message(message: ImapMessage, mailbox_identity: str, db_path: Path, storage_directory: Path, max_bytes: int) -> list[dict]:
    """Menjalankan pipeline offline lengkap untuk satu raw message IMAP yang telah di-fetch read-only."""
    parsed=register_parsed_email(message.raw,mailbox_identity,message.uidvalidity,message.uid,db_path,max_bytes)
    registered=register_parsed_attachments(parsed,db_path)
    stored=store_registered_attachments(registered,db_path,storage_directory)
    return finalize_stored_attachments(stored,db_path)

def run_email_intake(adapter: ReadOnlyImapAdapter, mailbox_identity: str, db_path: Path, storage_directory: Path, max_bytes: int, selected_uid: str | None = None, selected_uidvalidity: str | None = None) -> dict[str,int]:
    """Memproses satu batch IMAP read-only sampai invoice RECEIVED tanpa polling tak terbatas."""
    if max_bytes <= 0: raise ValueError('max_bytes must be positive')
    if bool(selected_uid) != bool(selected_uidvalidity):
        raise ValueError('selected_uid and selected_uidvalidity must be supplied together')
    database=Database(db_path); database.initialise()
    summary={'messages':0,'message_errors':0,'attachments':0,'completed':0,'imap_errors':0,'recovered':0,'uidvalidity_mismatch':0}

    if selected_uid:
        # Mode test hanya FETCH UID eksplisit; tidak SEARCH/menyentuh email UNSEEN lain.
        try:
            selected=adapter.fetch_uids([selected_uid])
        except Exception as error:
            summary['imap_errors']+=1
            LOGGER.warning('imap_intake_connection_failed',extra={'error_class':classify_recovery_error(error)})
            return summary
        candidates=[]
        for message in selected:
            if message.uidvalidity==selected_uidvalidity:
                candidates.append(message)
            else:
                summary['uidvalidity_mismatch']+=1
                LOGGER.warning('imap_selected_uidvalidity_changed',extra={'uid':selected_uid})
    else:
        candidates=None

    if candidates is None:
        # Selesaikan state durable lokal lebih dahulu; fungsi recovery tidak membutuhkan payload baru.
        before=recover(database); summary['recovered']+=before['completed']
        refetch_rows=[item for item in database.pending_imap_attachments()
                      if item['processing_status']=='NEEDS_REFETCH' and item['mailbox_identity']==mailbox_identity]
        expected_refetch={(item['uidvalidity'],item['uid']) for item in refetch_rows}
        try:
            # NEEDS_REFETCH memakai UID asal, bukan mencari email baru atau mengubah flag mailbox.
            refetched=adapter.fetch_uids(sorted({uid for _,uid in expected_refetch})) if expected_refetch else []
            unseen=adapter.fetch_unseen()
        except Exception as error:
            # Credential/timeout/mailbox error tidak mengubah state attachment dan dapat dicoba pada batch berikutnya.
            summary['imap_errors']+=1
            LOGGER.warning('imap_intake_connection_failed',extra={'error_class':classify_recovery_error(error)})
            return summary

        # UID dari UIDVALIDITY lama tidak boleh dikaitkan dengan message baru setelah mailbox reset.
        valid_refetched=[]
        for message in refetched:
            if (message.uidvalidity,message.uid) in expected_refetch:
                valid_refetched.append(message)
            else:
                LOGGER.warning('imap_refetch_uidvalidity_changed',extra={'uid':message.uid})
        candidates=valid_refetched+unseen

    processed=set()
    for message in candidates:
        identity=(message.uidvalidity,message.uid)
        if identity in processed: continue
        processed.add(identity)
        try:
            final= _process_imap_message(message,mailbox_identity,db_path,storage_directory,max_bytes)
            summary['messages']+=1; summary['attachments']+=len(final)
            summary['completed']+=sum(item['status']=='COMPLETED' for item in final)
            LOGGER.info('imap_intake_message_processed',extra={'uid':message.uid,'attachments':len(final)})
        except Exception as error:
            # Raw MIME gagal tidak ditandai selesai; karena read-only, message tetap tersedia untuk batch berikutnya.
            summary['message_errors']+=1
            LOGGER.warning('imap_intake_message_failed',extra={
                'uid':message.uid,
                'error_class':classify_recovery_error(error),
            })
    if not selected_uid:
        after=recover(database); summary['recovered']+=after['completed']
    return summary
