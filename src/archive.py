"""Archive final invoice files with content-hash verification."""
from __future__ import annotations
import hashlib, shutil, sqlite3
from contextlib import closing
from pathlib import Path

from src.time_utils import utc_now_iso

def _hash(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda:file.read(1024*1024),b''): digest.update(block)
    return digest.hexdigest()

def archive_final(database_path: Path, root: Path) -> dict[str,int]:
    """Pindahkan hanya state final ke folder hasil dan simpan path/hash untuk idempotency."""
    mapping={'POSTED':'success','REJECTED':'rejected','PO_VALIDATION_FAILED':'validation_failed','OCR_FAILED':'review'}; result={'archived':0,'skipped':0,'failed':0}
    with closing(sqlite3.connect(database_path)) as connection:
        for row in connection.execute("SELECT correlation_id,file_path,file_hash,status,posting_state FROM invoices WHERE archive_path IS NULL"):
            correlation_id,file_path,file_hash,status,posting_state=row
            # Keberhasilan ERP adalah posting_state POSTED; workflow status tetap APPROVED.
            category='success' if posting_state == 'POSTED' else mapping.get(status)
            if not category: result['skipped']+=1; continue
            source=Path(file_path)
            try:
                if not source.exists(): raise FileNotFoundError(source)
                if _hash(source)!=file_hash: raise ValueError('hash mismatch')
                target=root/category/source.name
                target.parent.mkdir(parents=True,exist_ok=True)
                if target.exists() and _hash(target)!=file_hash: target=target.with_name(f'{source.stem}-{file_hash[:8]}{source.suffix}')
                if not target.exists(): shutil.move(str(source),str(target))
                now = utc_now_iso()
                connection.execute("UPDATE invoices SET archive_path=?, archive_hash=?, updated_at=? WHERE correlation_id=?",(str(target),file_hash,now,correlation_id))
                connection.execute("INSERT INTO process_logs(correlation_id,step,result,message,created_at) VALUES(?,?,?,?,?)",(correlation_id,'ARCHIVE','SUCCESS',str(target),now)); result['archived']+=1
            except Exception as error:
                connection.execute("INSERT INTO process_logs(correlation_id,step,result,message,created_at) VALUES(?,?,?,?,?)",(correlation_id,'ARCHIVE','FAILED',type(error).__name__,utc_now_iso())); result['failed']+=1
        connection.commit()
    return result
