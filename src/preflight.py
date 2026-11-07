"""Read-only deployment readiness checks; never call SMTP, IMAP, OCR, or Forms."""
from __future__ import annotations
import sqlite3
from contextlib import closing
from pathlib import Path
from src.config import load_config

def run_preflight(root: Path) -> tuple[list[str], list[str]]:
    """Mengembalikan blocker dan warning dari file/config/schema tanpa mengubah state."""
    blockers: list[str]=[]; warnings: list[str]=[]
    try:
        config=load_config(root/'resources'/'Config.xlsx')
        if 'your-form-url' in config.forms_url: blockers.append('FormsUrl masih placeholder')
        if config.approver_email.endswith('@example.com'): warnings.append('ApproverEmail masih contoh')
    except Exception as error: blockers.append(f'Config invalid: {type(error).__name__}')
    for name in ('Invoices.template.xlsx','ERP_Dummy.template.xlsx','ProcessLogs.template.xlsx','PurchaseOrders.csv'):
        if not (root/'resources'/name).exists(): blockers.append(f'Resource missing: {name}')
    if not (root/'data'/'automation.db').exists(): blockers.append('Database belum diinisialisasi')
    else:
        try:
            with closing(sqlite3.connect(root/'data'/'automation.db')) as con:
                columns={r[1] for r in con.execute('PRAGMA table_info(invoices)')}
                required={'correlation_id','posting_ref','posting_state','archive_path','approval_status'}
                missing=required-columns
                if missing: blockers.append('Schema missing: '+', '.join(sorted(missing)))
        except sqlite3.Error: blockers.append('Database tidak dapat dibaca')
    # Email intake adalah acceptance criterion; tanpa implementasinya live test belum siap.
    blockers.append('IMAP invoice intake belum diverifikasi/diaktifkan untuk live testing')
    warnings.append('POSTING_UNCERTAIN membutuhkan rekonsiliasi manual Google Forms.')
    return blockers,warnings
