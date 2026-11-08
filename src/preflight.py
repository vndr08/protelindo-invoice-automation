"""Read-only deployment readiness checks; never call SMTP, IMAP, OCR, or Forms."""
from __future__ import annotations
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from dotenv import dotenv_values
from src.config import load_config


def _imap_local_readiness(root: Path) -> tuple[list[str], list[str]]:
    """Memeriksa kesiapan lokal IMAP tanpa login, network call, atau membuka credential."""
    blockers: list[str]=[]; warnings: list[str]=[]
    required_modules={
        'src/imap_adapter.py':'adapter read-only IMAP',
        'src/email_intake.py':'orchestration email intake',
        'main.py':'command email-intake',
    }
    for relative,label in required_modules.items():
        if not (root/relative).is_file(): blockers.append(f'IMAP module missing: {label}')
    main_file=root/'main.py'
    if main_file.is_file() and 'email-intake' not in main_file.read_text(encoding='utf-8'):
        blockers.append('IMAP command missing: email-intake')

    # dotenv_values tidak memasukkan password ke environment ataupun output terminal.
    file_values=dotenv_values(root/'.env') if (root/'.env').is_file() else {}
    def setting(name: str) -> str:
        return str(os.getenv(name) or file_values.get(name) or '').strip()

    required=('IMAP_HOST','IMAP_PORT','IMAP_USERNAME','IMAP_PASSWORD')
    missing=[name for name in required if not setting(name)]
    if missing: blockers.append('IMAP configuration missing: '+', '.join(missing))
    else:
        try:
            if int(setting('IMAP_PORT')) < 1: raise ValueError
        except ValueError:
            blockers.append('IMAP_PORT must be a positive integer')

    # Nilai ini dapat memakai default CLI, namun bila diset wajib valid agar batch aman.
    for name in ('IMAP_INTAKE_TIMEOUT_SECONDS','IMAP_ATTACHMENT_MAX_BYTES'):
        value=setting(name)
        if value:
            try:
                if int(value) < 1: raise ValueError
            except ValueError:
                blockers.append(f'{name} must be a positive integer')
    if not setting('IMAP_INTAKE_MAILBOX'):
        warnings.append('IMAP_INTAKE_MAILBOX tidak diisi; command akan memakai default INBOX')
    if not missing and not any('IMAP_' in blocker for blocker in blockers):
        warnings.append('IMAP local configuration is ready; run email-check to verify live connectivity.')
    return blockers,warnings

def run_preflight(root: Path) -> tuple[list[str], list[str]]:
    """Mengembalikan blocker dan warning dari file/config/schema tanpa mengubah state."""
    blockers: list[str]=[]; warnings: list[str]=[]
    try:
        config=load_config(root/'resources'/'Config.xlsx')
        # Placeholder submission-safe sengaja dianggap blocker: reviewer wajib mengisi
        # konfigurasi miliknya sebelum menjalankan integrasi email atau ERP.
        if 'your-form-url' in config.forms_url: blockers.append('FormsUrl masih placeholder')
        if config.approver_email.endswith('@example.com'): blockers.append('ApproverEmail masih placeholder')
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
    # Kesiapan lokal dan konektivitas live dipisahkan; preflight tidak pernah membuka IMAP.
    imap_blockers,imap_warnings=_imap_local_readiness(root)
    blockers.extend(imap_blockers); warnings.extend(imap_warnings)
    warnings.append('POSTING_UNCERTAIN membutuhkan rekonsiliasi manual Google Forms.')
    return blockers,warnings
