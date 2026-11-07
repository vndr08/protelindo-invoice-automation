"""Integration test offline untuk registrasi invoice dan finalisasi attachment IMAP."""
import sqlite3
import unittest
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory

from src.database import Database
from src.email_intake import (
    finalize_stored_attachments,
    register_parsed_attachments,
    register_parsed_email,
    store_registered_attachments,
)


def raw_email(attachments: list[tuple[str, str, bytes]]) -> bytes:
    """Membuat MIME fixture lokal sehingga test tidak membutuhkan mailbox."""
    message=EmailMessage(); message['From']='invoice@example.com'; message['Subject']='Invoice'
    for filename,mime_type,payload in attachments:
        maintype,subtype=mime_type.split('/',1)
        message.add_attachment(payload,maintype=maintype,subtype=subtype,filename=filename)
    return message.as_bytes()


class EmailAttachmentFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.root=Path(self.temp.name)
        self.db=self.root/'test.db'; self.storage=self.root/'storage'

    def tearDown(self):
        self.temp.cleanup()

    def stored(self,uid: str,attachments: list[tuple[str,str,bytes]]) -> list[dict]:
        parsed=register_parsed_email(raw_email(attachments),'INBOX','1',uid,self.db,1024)
        registered=register_parsed_attachments(parsed,self.db)
        return store_registered_attachments(registered,self.db,self.storage)

    def attachment_row(self,attachment_id: int):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute(
                'SELECT processing_status,invoice_id,storage_path FROM imap_attachments WHERE id=?',
                (attachment_id,),
            ).fetchone()

    def invoice_rows(self):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute('SELECT id,correlation_id,status FROM invoices ORDER BY id').fetchall()

    def test_file_stored_becomes_received_invoice_and_completed_attachment(self):
        result=finalize_stored_attachments(
            self.stored('1',[('invoice.jpg','image/jpeg',b'\xff\xd8\xffvalid')]),self.db,
        )[0]
        row=self.attachment_row(result['attachment_id']); invoice=self.invoice_rows()
        self.assertEqual(result['status'],'COMPLETED')
        self.assertEqual(row[0],'COMPLETED')
        self.assertEqual(len(invoice),1)
        self.assertEqual(invoice[0][2],'RECEIVED')
        self.assertEqual(row[1],invoice[0][0])

    def test_rerun_keeps_one_invoice_and_same_correlation_id(self):
        content=[('invoice.jpg','image/jpeg',b'\xff\xd8\xffsame')]
        first=finalize_stored_attachments(self.stored('2',content),self.db)[0]
        invoice_before=self.invoice_rows()[0]
        second=finalize_stored_attachments(self.stored('2',content),self.db)[0]
        self.assertEqual(first['status'],'COMPLETED')
        self.assertEqual(second['status'],'COMPLETED')
        self.assertEqual(self.invoice_rows(),[invoice_before])

    def test_multiple_unique_attachments_create_matching_invoices(self):
        result=finalize_stored_attachments(self.stored('3',[
            ('first.jpg','image/jpeg',b'\xff\xd8\xfffirst'),
            ('second.png','image/png',b'\x89PNG\r\n\x1a\nsecond'),
        ]),self.db)
        self.assertEqual([item['status'] for item in result],['COMPLETED','COMPLETED'])
        self.assertEqual(len(self.invoice_rows()),2)

    def test_duplicate_hash_from_different_messages_reuses_invoice(self):
        content=[('invoice.jpg','image/jpeg',b'\xff\xd8\xffduplicate')]
        first=finalize_stored_attachments(self.stored('4',content),self.db)[0]
        second=finalize_stored_attachments(self.stored('5',content),self.db)[0]
        first_row=self.attachment_row(first['attachment_id'])
        second_row=self.attachment_row(second['attachment_id'])
        self.assertEqual(len(self.invoice_rows()),1)
        self.assertEqual(first_row[1],second_row[1])
        self.assertEqual(first_row[0],second_row[0],'COMPLETED')

    def test_failed_and_needs_refetch_are_not_finalized(self):
        failed=store_registered_attachments(
            self.stored('6',[('invalid.txt','text/plain',b'bad')]),self.db,self.storage,
        )
        # Gunakan attachment valid kedua lalu tandai NEEDS_REFETCH untuk membuktikan skip.
        refetch=self.stored('7',[('retry.jpg','image/jpeg',b'\xff\xd8\xffretry')])[0]
        Database(self.db).record_imap_attachment_error(refetch['attachment_id'],'NEEDS_REFETCH','payload missing','NEEDS_REFETCH')
        refetch['status']='NEEDS_REFETCH'
        result=finalize_stored_attachments(failed+[refetch],self.db)
        self.assertEqual([item['status'] for item in result],['FAILED','NEEDS_REFETCH'])
        self.assertEqual(self.invoice_rows(),[])

    def test_invoice_registered_is_finalized_without_creating_new_invoice(self):
        stored=self.stored('8',[('invoice.jpg','image/jpeg',b'\xff\xd8\xffregistered')])
        database=Database(self.db); invoice=database.register_stored_attachment_invoice(stored[0]['attachment_id'])
        stored[0]['status']='INVOICE_REGISTERED'
        result=finalize_stored_attachments(stored,self.db)[0]
        self.assertEqual(result['status'],'COMPLETED')
        self.assertEqual(self.invoice_rows(),[(invoice['id'],invoice['correlation_id'],'RECEIVED')])

    def test_one_registration_failure_does_not_stop_other_attachment(self):
        stored=self.stored('9',[
            ('bad.jpg','image/jpeg',b'\xff\xd8\xffbad'),
            ('good.jpg','image/jpeg',b'\xff\xd8\xffgood'),
        ])
        # Mengubah file pertama setelah storage agar verifikasi hash repository gagal.
        Path(self.attachment_row(stored[0]['attachment_id'])[2]).write_bytes(b'corrupted')
        with self.assertLogs('src.email_intake',level='WARNING'):
            result=finalize_stored_attachments(stored,self.db)
        self.assertEqual(result[0]['status'],'FAILED')
        self.assertEqual(result[0]['error_class'],'PERMANENT')
        self.assertEqual(result[1]['status'],'COMPLETED')
        self.assertEqual(len(self.invoice_rows()),1)


if __name__ == '__main__':
    unittest.main()
