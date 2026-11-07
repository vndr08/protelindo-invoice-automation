"""Integration test offline untuk metadata attachment dan durable storage."""
import errno
import sqlite3
import unittest
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.durable_storage import store_attachment as durable_store_attachment
from src.email_intake import (
    register_parsed_attachments,
    register_parsed_email,
    store_registered_attachments,
)


def raw_email(attachments: list[tuple[str, str, bytes]]) -> bytes:
    """Membuat fixture MIME lokal tanpa koneksi email."""
    message=EmailMessage(); message['From']='invoice@example.com'; message['Subject']='Invoice'
    for filename,mime_type,payload in attachments:
        maintype,subtype=mime_type.split('/',1)
        message.add_attachment(payload,maintype=maintype,subtype=subtype,filename=filename)
    return message.as_bytes()


class EmailAttachmentStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.root=Path(self.temp.name)
        self.db=self.root/'test.db'; self.storage=self.root/'storage'

    def tearDown(self):
        self.temp.cleanup()

    def registered(self,uid: str,attachments: list[tuple[str,str,bytes]]) -> list[dict]:
        parsed=register_parsed_email(raw_email(attachments),'INBOX','1',uid,self.db,1024)
        return register_parsed_attachments(parsed,self.db)

    def attachment_row(self,attachment_id: int):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute(
                'SELECT processing_status,storage_path,retry_count,last_error_class '
                'FROM imap_attachments WHERE id=?',(attachment_id,),
            ).fetchone()

    def test_valid_attachment_becomes_file_stored(self):
        items=self.registered('1',[('invoice.jpg','image/jpeg',b'\xff\xd8\xffvalid')])
        result=store_registered_attachments(items,self.db,self.storage)[0]
        row=self.attachment_row(result['attachment_id'])
        self.assertEqual(result['status'],'FILE_STORED')
        self.assertEqual(row[0],'FILE_STORED')
        self.assertTrue(Path(row[1]).is_file())

    def test_rerun_does_not_duplicate_record_or_file(self):
        content=[('invoice.jpg','image/jpeg',b'\xff\xd8\xffsame')]
        first=store_registered_attachments(self.registered('2',content),self.db,self.storage)[0]
        second=store_registered_attachments(self.registered('2',content),self.db,self.storage)[0]
        with closing(sqlite3.connect(self.db)) as connection:
            count=connection.execute('SELECT COUNT(*) FROM imap_attachments').fetchone()[0]
        self.assertEqual(first['attachment_id'],second['attachment_id'])
        self.assertEqual(count,1)
        self.assertEqual(len([path for path in self.storage.rglob('*') if path.is_file()]),1)

    def test_invalid_attachment_is_not_stored(self):
        result=store_registered_attachments(
            self.registered('3',[('invoice.txt','text/plain',b'invalid')]),self.db,self.storage,
        )[0]
        self.assertEqual(result['status'],'FAILED')
        self.assertFalse(self.storage.exists())

    def test_multiple_attachments_continue_after_invalid_item(self):
        items=self.registered('4',[
            ('invalid.txt','text/plain',b'invalid'),
            ('valid.png','image/png',b'\x89PNG\r\n\x1a\nvalid'),
        ])
        result=store_registered_attachments(items,self.db,self.storage)
        self.assertEqual([item['status'] for item in result],['FAILED','FILE_STORED'])
        self.assertTrue(Path(result[1]['storage_path']).is_file())

    def test_existing_file_stored_is_verified_without_rewrite(self):
        content=[('invoice.jpg','image/jpeg',b'\xff\xd8\xffexisting')]
        first=store_registered_attachments(self.registered('5',content),self.db,self.storage)[0]
        path=Path(first['storage_path']); before=path.stat().st_mtime_ns
        # os.replace tidak boleh dipanggil ketika deterministic target sudah valid.
        with patch('src.durable_storage.os.replace',side_effect=AssertionError('unexpected rewrite')):
            second=store_registered_attachments(self.registered('5',content),self.db,self.storage)[0]
        self.assertEqual(second['storage_path'],path)
        self.assertEqual(path.stat().st_mtime_ns,before)

    def test_transient_storage_error_does_not_stop_batch(self):
        items=self.registered('6',[
            ('first.jpg','image/jpeg',b'\xff\xd8\xfffirst'),
            ('second.jpg','image/jpeg',b'\xff\xd8\xffsecond'),
        ])
        calls=0
        def store_with_first_failure(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1: raise OSError(errno.EAGAIN,'temporary filesystem failure')
            return durable_store_attachment(*args,**kwargs)
        with patch('src.email_intake.store_attachment',side_effect=store_with_first_failure), self.assertLogs('src.email_intake',level='WARNING'):
            result=store_registered_attachments(items,self.db,self.storage)
        first_row=self.attachment_row(items[0]['attachment_id'])
        self.assertEqual(result[0]['status'],'DISCOVERED')
        self.assertEqual(result[0]['error_class'],'TRANSIENT')
        self.assertEqual(first_row[0:4],('DISCOVERED',None,1,'TRANSIENT'))
        self.assertEqual(result[1]['status'],'FILE_STORED')


if __name__ == '__main__':
    unittest.main()
