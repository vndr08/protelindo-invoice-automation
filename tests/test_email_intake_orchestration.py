"""Mock integration test untuk orchestration IMAP invoice read-only."""
import sqlite3
import unittest
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.database import Database
from src.email_intake import _process_imap_message, run_email_intake
from src.imap_adapter import ImapAdapterError, ImapMessage
from src.intake import intake_directory


def raw_email(attachments: list[tuple[str, str, bytes]]) -> bytes:
    """Membuat raw MIME lokal agar test tidak menghubungi mailbox sungguhan."""
    message=EmailMessage(); message['From']='sender@example.com'; message['Subject']='Invoice'
    for filename,mime_type,payload in attachments:
        maintype,subtype=mime_type.split('/',1)
        message.add_attachment(payload,maintype=maintype,subtype=subtype,filename=filename)
    return message.as_bytes()


class FakeImapAdapter:
    """Kontrak adapter minimal untuk membuktikan orchestration tanpa IMAP network."""
    def __init__(self,unseen=None,refetched=None,error=None):
        self.unseen=unseen or []; self.refetched=refetched or []; self.error=error; self.refetch_uids=[]; self.unseen_calls=0

    def fetch_uids(self,uids):
        self.refetch_uids.append(list(uids))
        if self.error: raise self.error
        return self.refetched

    def fetch_unseen(self):
        self.unseen_calls+=1
        if self.error: raise self.error
        return self.unseen


class EmailIntakeOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.root=Path(self.temp.name)
        self.db=self.root/'test.db'; self.storage=self.root/'incoming'; self.identity='automation@example.com:INBOX'

    def tearDown(self):
        self.temp.cleanup()

    def execute_batch(self,adapter):
        return run_email_intake(adapter,self.identity,self.db,self.storage,1024)

    def attachment_rows(self):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute('SELECT id,processing_status,invoice_id,storage_path FROM imap_attachments ORDER BY id').fetchall()

    def invoice_rows(self):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute('SELECT id,correlation_id,status FROM invoices ORDER BY id').fetchall()

    def test_one_valid_message_becomes_received_invoice(self):
        adapter=FakeImapAdapter([ImapMessage('1','10',raw_email([('invoice.jpg','image/jpeg',b'\xff\xd8\xffvalid')]))])
        summary=self.execute_batch(adapter)
        self.assertEqual(summary['completed'],1)
        self.assertEqual(self.invoice_rows()[0][2],'RECEIVED')
        self.assertEqual(self.attachment_rows()[0][1],'COMPLETED')

    def test_multiple_messages_and_attachments_are_processed_independently(self):
        adapter=FakeImapAdapter([
            ImapMessage('1','11',raw_email([('a.jpg','image/jpeg',b'\xff\xd8\xffa'),('b.png','image/png',b'\x89PNG\r\n\x1a\nb')])),
            ImapMessage('1','12',raw_email([('c.jpg','image/jpeg',b'\xff\xd8\xffc')])),
        ])
        summary=self.execute_batch(adapter)
        self.assertEqual((summary['messages'],summary['completed']),(2,3))
        self.assertEqual(len(self.invoice_rows()),3)

    def test_duplicate_uid_and_rerun_do_not_create_duplicate_invoice(self):
        message=ImapMessage('1','13',raw_email([('invoice.jpg','image/jpeg',b'\xff\xd8\xffsame')]))
        self.execute_batch(FakeImapAdapter([message,message]))
        original=self.invoice_rows()[0]
        summary=self.execute_batch(FakeImapAdapter([message]))
        self.assertEqual(len(self.invoice_rows()),1)
        self.assertEqual(self.invoice_rows()[0],original)
        self.assertEqual(summary['completed'],1)

    def test_duplicate_hash_from_two_messages_reuses_invoice(self):
        payload=b'\xff\xd8\xffduplicate'
        self.execute_batch(FakeImapAdapter([
            ImapMessage('1','14',raw_email([('first.jpg','image/jpeg',payload)])),
            ImapMessage('1','15',raw_email([('second.jpg','image/jpeg',payload)])),
        ]))
        rows=self.attachment_rows()
        self.assertEqual(len(self.invoice_rows()),1)
        self.assertEqual(rows[0][2],rows[1][2])
        self.assertEqual([row[1] for row in rows],['COMPLETED','COMPLETED'])

    def test_needs_refetch_uses_original_uid_and_completes(self):
        message=ImapMessage('1','16',raw_email([('invoice.jpg','image/jpeg',b'\xff\xd8\xffrefetch')]))
        self.execute_batch(FakeImapAdapter([message]))
        attachment_id,_,_,path=self.attachment_rows()[0]
        Path(path).unlink()
        Database(self.db).record_imap_attachment_error(attachment_id,'NEEDS_REFETCH','missing local file','NEEDS_REFETCH')
        adapter=FakeImapAdapter(refetched=[message])
        summary=self.execute_batch(adapter)
        self.assertEqual(adapter.refetch_uids,[['16']])
        self.assertEqual(summary['completed'],1)
        self.assertEqual(self.attachment_rows()[0][1],'COMPLETED')
        self.assertEqual(len(self.invoice_rows()),1)

    def test_timeout_or_auth_failure_is_safe_and_does_not_create_invoice(self):
        for error in (OSError('timed out'),ImapAdapterError('authentication failed')):
            with self.subTest(error=type(error).__name__), self.assertLogs('src.email_intake',level='WARNING'):
                summary=self.execute_batch(FakeImapAdapter(error=error))
            self.assertEqual(summary['imap_errors'],1)
            self.assertEqual(summary['messages'],0)
            self.assertFalse(self.db.exists() and self.invoice_rows())

    def test_one_malformed_message_failure_does_not_stop_following_message(self):
        bad=ImapMessage('1','17',b'malformed fixture')
        good=ImapMessage('1','18',raw_email([('good.jpg','image/jpeg',b'\xff\xd8\xffgood')]))
        def fail_only_bad(message,*args,**kwargs):
            if message.uid=='17': raise ValueError('malformed MIME message')
            return _process_imap_message(message,*args,**kwargs)
        with patch('src.email_intake._process_imap_message',side_effect=fail_only_bad), self.assertLogs('src.email_intake',level='WARNING'):
            summary=self.execute_batch(FakeImapAdapter([bad,good]))
        self.assertEqual((summary['message_errors'],summary['completed']),(1,1))
        self.assertEqual(len(self.invoice_rows()),1)

    def test_local_folder_intake_remains_available(self):
        incoming=self.root/'local'; incoming.mkdir(); image=incoming/'invoice.jpg'; image.write_bytes(b'local image')
        database=Database(self.db); database.initialise()
        class Logger:
            def info(self,*args,**kwargs): pass
            def warning(self,*args,**kwargs): pass
        self.assertEqual(intake_directory(incoming,database,Logger()).registered,1)
        self.execute_batch(FakeImapAdapter())
        self.assertEqual(len(self.invoice_rows()),1)

    def test_selected_uid_uses_isolated_single_fetch_without_unseen_search(self):
        message=ImapMessage('7','99',raw_email([('invoice.jpg','image/jpeg',b'\xff\xd8\xffselected')]))
        adapter=FakeImapAdapter(refetched=[message],unseen=[ImapMessage('7','100',b'ignored')])
        summary=run_email_intake(adapter,self.identity,self.db,self.storage,1024,selected_uid='99',selected_uidvalidity='7')
        self.assertEqual(adapter.refetch_uids,[['99']])
        self.assertEqual(adapter.unseen_calls,0)
        self.assertEqual((summary['messages'],summary['completed']),(1,1))
        self.assertEqual(len(self.invoice_rows()),1)

    def test_selected_uidvalidity_mismatch_does_not_process_message(self):
        message=ImapMessage('new-validity','101',raw_email([('invoice.jpg','image/jpeg',b'\xff\xd8\xffchanged')]))
        with self.assertLogs('src.email_intake',level='WARNING'):
            summary=run_email_intake(FakeImapAdapter(refetched=[message]),self.identity,self.db,self.storage,1024,selected_uid='101',selected_uidvalidity='old-validity')
        self.assertEqual((summary['messages'],summary['uidvalidity_mismatch']),(0,1))
        self.assertEqual(len(self.invoice_rows()),0)


if __name__ == '__main__':
    unittest.main()
