"""Approval mock tests untuk SQLite terisolasi tanpa SMTP/IMAP nyata."""
import re
import sqlite3
import unittest
from contextlib import closing
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import main
from src.approval import ApprovalWorkflow
from src.config import AppConfig
from src.database import Database, InvoiceRegistration


class FakeLogger:
    def info(self,*args,**kwargs): pass
    def warning(self,*args,**kwargs): pass


class FakeMailer:
    def __init__(self): self.sent=[]
    def send(self,recipient,subject,body): self.sent.append((recipient,subject,body))


class ApprovalDatabaseIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); root=Path(self.temp.name)
        self.db_path=root/'live-test.db'; self.other_path=root/'main.db'
        self.db=Database(self.db_path); self.db.initialise(); Database(self.other_path).initialise()

    def tearDown(self): self.temp.cleanup()

    def seed(self,correlation: str,amount: str,status: str='PO_VALIDATED'):
        self.db.register_invoice(InvoiceRegistration(correlation,f'/tmp/{correlation}.jpg',f'hash-{correlation}'))
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("UPDATE invoices SET invoice_number='INV-1',vendor_name='PT Test',po_number='PO-1',amount=?,currency='IDR',status=? WHERE correlation_id=?",(amount,status,correlation))
            connection.commit()

    def invoice_state(self,correlation: str):
        with closing(sqlite3.connect(self.db_path)) as connection:
            return connection.execute('SELECT status,approval_status FROM invoices WHERE correlation_id=?',(correlation,)).fetchone()

    def test_initiation_uses_selected_database_and_configured_threshold(self):
        self.seed('high','1200000')
        fake_mailer=FakeMailer()
        config=AppConfig(Decimal('1000000'),'https://form.test','template.xlsx','approver@example.com')
        with patch.object(main,'load_config',return_value=config), patch.object(main.EmailSettings,'from_environment',return_value=object()), patch.object(main,'SmtpMailer',return_value=fake_mailer), patch.object(main,'configure_logging',return_value=FakeLogger()):
            main.start_approval(self.db_path)
        self.assertEqual(self.invoice_state('high'),('PENDING_APPROVAL','PENDING_APPROVAL'))
        self.assertEqual(len(fake_mailer.sent),1)
        self.assertEqual(Database(self.other_path).invoice_count(),0)

    def test_threshold_and_duplicate_initiation_are_idempotent(self):
        self.seed('at-threshold','1000000')
        workflow=ApprovalWorkflow(self.db,FakeLogger())
        first=workflow.initiate(Decimal('1000000'),'approver@example.com',24,None)
        second=workflow.initiate(Decimal('1000000'),'approver@example.com',24,None)
        self.assertEqual(first,{'auto_approved':1,'pending':0,'sent':0,'failed':0})
        self.assertEqual(second,{'auto_approved':0,'pending':0,'sent':0,'failed':0})
        self.assertEqual(self.invoice_state('at-threshold'),('APPROVED','AUTO_APPROVED'))

    def test_authorized_reply_is_applied_once_in_selected_database(self):
        self.seed('reply','1200000')
        workflow=ApprovalWorkflow(self.db,FakeLogger())
        workflow.initiate(Decimal('1000000'),'approver@example.com',24,None)
        with closing(sqlite3.connect(self.db_path)) as connection:
            body=connection.execute('SELECT body FROM approval_outbox').fetchone()[0]
        token=re.search(r'Approval Token: ([A-Za-z0-9_-]+)',body).group(1)
        message=EmailMessage(); message['From']='approver@example.com'; message.set_content(f'APPROVE\nApproval Token: {token}\n')
        self.assertEqual(workflow.process_replies([('test-uid',message.as_bytes())],'approver@example.com'),{'APPROVED':1})
        self.assertEqual(workflow.process_replies([('test-uid',message.as_bytes())],'approver@example.com'),{'DUPLICATE_EMAIL':1})
        self.assertEqual(self.invoice_state('reply'),('APPROVED','APPROVED'))
        self.assertEqual(Database(self.other_path).invoice_count(),0)


if __name__ == '__main__':
    unittest.main()
