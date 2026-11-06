"""Penyimpanan attachment durable untuk state DISCOVERED ke FILE_STORED."""
from __future__ import annotations
import hashlib, os, sqlite3, tempfile
from pathlib import Path

def store_attachment(database: Path, attachment_id: int, data: bytes, suffix: str, root: Path) -> Path:
    """Menulis file atomik, memverifikasi hash, lalu mengubah state database."""
    digest=hashlib.sha256(data).hexdigest()
    with sqlite3.connect(database) as con:
        row=con.execute('SELECT sha256,storage_path FROM imap_attachments WHERE id=?',(attachment_id,)).fetchone()
        if not row: raise ValueError('attachment metadata not found')
        if row[0]!=digest: raise ValueError('attachment hash mismatch')
        target=root/digest[:2]/f'{attachment_id}-{digest}{suffix}'
        target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest()!=digest: raise ValueError('existing file corrupt')
        else:
            temp: Path | None = None
            try:
                # Tulis ke file temporary, flush dan fsync sebelum rename atomik.
                with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as f:
                    f.write(data); f.flush(); os.fsync(f.fileno()); temp=Path(f.name)
                os.replace(temp,target)
            except OSError:
                # Temporary file tidak boleh tertinggal jika write/replace gagal.
                if temp and temp.exists(): temp.unlink()
                raise
        con.execute("UPDATE imap_attachments SET storage_path=?,processing_status='FILE_STORED',error_reason=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(str(target),attachment_id))
    return target
