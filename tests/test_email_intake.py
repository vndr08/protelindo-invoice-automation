from email.message import EmailMessage
import unittest
from src.email_intake import parse_email_attachments
def raw(parts):
 m=EmailMessage();m['From']='Nama <a@example.com>';m['Subject']='Héllo';m['Message-ID']='<x>'
 for name,mime,data in parts: m.add_attachment(data,maintype=mime.split('/')[0],subtype=mime.split('/')[1],filename=name)
 return m.as_bytes()
class T(unittest.TestCase):
 def test_valid_and_multiple(self):
  meta,a=parse_email_attachments(raw([('a.jpg','image/jpeg',b'\xff\xd8\xffx'),('b.png','image/png',b'\x89PNG\r\n\x1a\nx')]),100)
  self.assertEqual(meta.subject,'Héllo');self.assertEqual(len(a),2);self.assertTrue(all(x.error is None for x in a))
 def test_invalid_does_not_stop(self):
  _,a=parse_email_attachments(raw([('bad.txt','text/plain',b'x'),('a.jpg','image/jpeg',b'\xff\xd8\xffx')]),100);self.assertIsNotNone(a[0].error);self.assertIsNone(a[1].error)
 def test_empty(self): self.assertIsNotNone(parse_email_attachments(raw([('a.jpg','image/jpeg',b'')]),100)[1][0].error)
