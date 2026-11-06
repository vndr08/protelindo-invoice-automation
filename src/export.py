"""Export database state to the official Excel assessment templates."""
from __future__ import annotations
import os, sqlite3, tempfile
from pathlib import Path
from openpyxl import load_workbook

class ExportError(RuntimeError): pass

def _atomic_workbook(template: Path, output: Path, rows: list[tuple]) -> None:
    """Menulis ulang data dari template ke temporary file lalu replace secara atomik."""
    try:
        workbook = load_workbook(template)
        sheet = workbook.active
        if sheet.max_row > 1: sheet.delete_rows(2, sheet.max_row - 1)
        for row in rows: sheet.append(row)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=output.parent, suffix='.xlsx', delete=False) as temp:
            temporary = Path(temp.name)
        workbook.save(temporary); workbook.close(); os.replace(temporary, output)
    except (OSError, ValueError) as error:
        raise ExportError(f"Excel export failed for {output.name}") from error

def export_all(database_path: Path, resources: Path, output: Path) -> None:
    """Export seluruh invoice, hanya ERP POSTED, dan seluruh process log dari SQLite."""
    try:
        with sqlite3.connect(database_path) as connection:
            invoices = connection.execute("SELECT correlation_id,file_path,invoice_number,vendor_name,po_number,amount,currency,match_status,needs_approval,approval_status,posting_ref,posted_at,status,created_at,updated_at FROM invoices ORDER BY id").fetchall()
            posted = connection.execute("SELECT invoice_number,vendor_name,po_number,amount,currency,posted_at,approval_status,posting_ref,posted_at FROM invoices WHERE posting_state='POSTED' ORDER BY id").fetchall()
            logs = connection.execute("SELECT created_at,correlation_id,step,result,message FROM process_logs ORDER BY id").fetchall()
    except sqlite3.Error as error: raise ExportError('Database read failed') from error
    _atomic_workbook(resources/'Invoices.template.xlsx', output/'Invoices.xlsx', invoices)
    _atomic_workbook(resources/'ERP_Dummy.template.xlsx', output/'ERP_Dummy.xlsx', posted)
    _atomic_workbook(resources/'ProcessLogs.template.xlsx', output/'ProcessLogs.xlsx', logs)
