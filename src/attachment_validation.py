"""Validasi dan penyimpanan attachment invoice secara aman."""
from __future__ import annotations
import hashlib, os, tempfile
from pathlib import Path
from uuid import uuid4

ALLOWED={'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png'}
SIGNATURES={'.jpg':b'\xff\xd8\xff','.jpeg':b'\xff\xd8\xff','.png':b'\x89PNG\r\n\x1a\n'}
class AttachmentError(ValueError): pass
def validate_and_store(filename:str|None,mime:str|None,data:bytes,directory:Path,max_bytes:int)->tuple[str,Path]:
    """Validasi ekstensi/MIME/signature lalu tulis atomik dengan nama aman."""
    suffix=Path(filename or '').suffix.lower()
    if suffix not in ALLOWED or mime != ALLOWED[suffix]: raise AttachmentError('extension or MIME mismatch')
    if not data or len(data)>max_bytes or not data.startswith(SIGNATURES[suffix]): raise AttachmentError('empty, oversized, or corrupt attachment')
    digest=hashlib.sha256(data).hexdigest(); directory.mkdir(parents=True,exist_ok=True)
    target=directory/f'{uuid4()}-{digest[:12]}{suffix}'
    try:
        with tempfile.NamedTemporaryFile(dir=directory,delete=False) as f: f.write(data); temp=Path(f.name)
        os.replace(temp,target)
    except OSError as error: raise AttachmentError('atomic write failed') from error
    return digest,target
