"""Mock regression untuk batching IMAP approval tanpa mailbox nyata."""
import re
import unittest
from contextlib import closing
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.approval import ApprovalWorkflow, EmailSettings, ImapInbox
from src.database import Database, InvoiceRegistration


class FakeLogger:
    def info(self,*args,**kwargs): pass
    def warning(self,*args,**kwargs): pass


class LargeMailbox:
    """Fake IMAP besar yang mencatat operasi tanpa menyentuh network."""
    def __init__(self,timeout=False): self.calls=[]; self.readonly=None; self.timeout=timeout
    def __enter__(self): return self
    def __exit__(self,*args): return None
    def login(self,*args): return 'OK',[]
    def select(self,*args,**kwargs): self.readonly=kwargs.get('readonly'); return 'OK',[]
    def uid(self,command,*args):
        self.calls.append((command,args))
        if command=='search': return 'OK',[b' '.join(str(value).encode() for value in range(1,51))]
        if command=='fetch':
            if self.timeout: raise TimeoutError('simulated timeout')
            part=args[1]
            if 'HEADER' in part: return 'OK',[(b'header',b'From: approver@example.com\r\n')]
            return 'OK',[(b'text',b'APPROVE\r\nApproval Token: abcdefghijklmnopqrstuvwxyz\r\n')]
        raise AssertionError('unexpected IMAP operation')


class ApprovalReplyBatchingTests(unittest.TestCase):
    def settings(self): return EmailSettings('smtp',587,'user','password','imap',993,'user','password')

    def test_large_mailbox_fetches_only_recent_bounded_unprocessed_replies(self):
        fake=LargeMailbox()
        with patch('src.approval.imaplib.IMAP4_SSL',return_value=fake):
            messages=ImapInbox(self.settings(),timeout_seconds=7).unread_messages('approver@example.com',3,{'50'})
        self.assertEqual([uid for uid,_ in messages],['49','48','47'])
        self.assertTrue(fake.readonly)
        search=next(args for command,args in fake.calls if command=='search')
        self.assertEqual(search, (None,'UNSEEN','FROM','"approver@example.com"','TEXT','"Approval Token:"'))
        fetches=[args[1] for command,args in fake.calls if command=='fetch']
        self.assertEqual(len(fetches),6)
        self.assertTrue(all('BODY.PEEK' in part and 'RFC822' not in part for part in fetches))
        self.assertFalse(any(command=='store' for command,_ in fake.calls))

    def test_fetch_timeout_propagates_without_flag_mutation(self):
        fake=LargeMailbox(timeout=True)
        with patch('src.approval.imaplib.IMAP4_SSL',return_value=fake), self.assertRaises(TimeoutError):
            ImapInbox(self.settings()).unread_messages('approver@example.com',1)
        self.assertTrue(fake.readonly)
        self.assertFalse(any(command=='store' for command,_ in fake.calls))

    def test_gmail_bad_search_is_diagnosable_without_sensitive_arguments(self):
        class BadSearchMailbox(LargeMailbox):
            def uid(self,command,*args):
                self.calls.append((command,args))
                if command=='search': return 'BAD',[b'Could not parse command']
                raise AssertionError('FETCH must not run after BAD SEARCH')
        fake=BadSearchMailbox()
        with patch('src.approval.imaplib.IMAP4_SSL',return_value=fake), self.assertRaisesRegex(OSError,'server status BAD') as error:
            ImapInbox(self.settings()).unread_messages('approver@example.com',1)
        self.assertNotIn('approver@example.com',str(error.exception))
        self.assertNotIn('Approval Token',str(error.exception))

    def test_invalid_sender_and_duplicate_reply_remain_database_guarded(self):
        with TemporaryDirectory() as temporary:
            database=Database(Path(temporary)/'test.db'); database.initialise()
            database.register_invoice(InvoiceRegistration('c','/tmp/invoice.jpg','hash-c'))
            with closing(__import__('sqlite3').connect(database.path)) as connection:
                connection.execute("UPDATE invoices SET amount='1200000',currency='IDR',status='PO_VALIDATED' WHERE correlation_id='c'")
                connection.commit()
            workflow=ApprovalWorkflow(database,FakeLogger())
            workflow.initiate(Decimal('1000000'),'approver@example.com',24,None)
            with closing(__import__('sqlite3').connect(database.path)) as connection:
                body=connection.execute('SELECT body FROM approval_outbox').fetchone()[0]
            token=re.search(r'Approval Token: ([A-Za-z0-9_-]+)',body).group(1)
            invalid=EmailMessage(); invalid['From']='intruder@example.com'; invalid.set_content(f'APPROVE\nApproval Token: {token}')
            self.assertEqual(workflow.process_replies([('invalid',invalid.as_bytes())],'approver@example.com'),{'UNAUTHORIZED_APPROVER':1})
            valid=EmailMessage(); valid['From']='approver@example.com'; valid.set_content(f'APPROVE\nApproval Token: {token}')
            self.assertEqual(workflow.process_replies([('valid',valid.as_bytes())],'approver@example.com'),{'APPROVED':1})
            self.assertEqual(workflow.process_replies([('valid',valid.as_bytes())],'approver@example.com'),{'DUPLICATE_EMAIL':1})


if __name__ == '__main__':
    unittest.main()
