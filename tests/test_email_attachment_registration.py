"""Regression metadata attachment dari raw MIME tanpa filesystem atau network."""
import hashlib, sqlite3, unittest
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from src.email_intake import register_parsed_email, register_parsed_attachments

def raw(filename, mime, payload):
    message=EmailMessage(); message['From']='a@example.com'; message['Subject']='test'
    message.add_attachment(payload,maintype=mime.split('/')[0],subtype=mime.split('/')[1],filename=filename)
    return message.as_bytes()

class AttachmentRegistrationTests(unittest.TestCase):
    def setUp(self): self.temp=TemporaryDirectory(); self.db=Path(self.temp.name)/'test.db'
    def tearDown(self): self.temp.cleanup()
    def parsed(self, uid, content): return register_parsed_email(content,'INBOX','1',uid,self.db,100)
    def test_valid_jpeg_metadata(self):
        data=b'\xff\xd8\xffx'; item=register_parsed_attachments(self.parsed('1',raw('a.jpg','image/jpeg',data)),self.db)[0]
        self.assertEqual(item['status'],'DISCOVERED')
        with closing(sqlite3.connect(self.db)) as c:self.assertEqual(c.execute('select sha256,mime_type from imap_attachments').fetchone(),(hashlib.sha256(data).hexdigest(),'image/jpeg'))
    def test_duplicate_uid_no_duplicate_attachment(self):
        content=raw('a.jpg','image/jpeg',b'\xff\xd8\xffx'); register_parsed_attachments(self.parsed('2',content),self.db); register_parsed_attachments(self.parsed('2',content),self.db)
        with closing(sqlite3.connect(self.db)) as c:self.assertEqual(c.execute('select count(*) from imap_attachments').fetchone()[0],1)
    def test_invalid_is_failed(self):
        item=register_parsed_attachments(self.parsed('3',raw('x.txt','text/plain',b'x')),self.db)[0]
        self.assertEqual(item['status'],'FAILED'); self.assertIsNotNone(item['validation_error'])
    def test_completed_not_reset(self):
        content=raw('a.jpg','image/jpeg',b'\xff\xd8\xffx'); parsed=self.parsed('4',content); item=register_parsed_attachments(parsed,self.db)[0]
        with closing(sqlite3.connect(self.db)) as c:c.execute("update imap_attachments set processing_status='COMPLETED'");c.commit()
        self.assertEqual(register_parsed_attachments(parsed,self.db)[0]['status'],'COMPLETED')
