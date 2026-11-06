import hashlib, sqlite3, unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from src.database import Database
class T(unittest.TestCase):
 def setUp(s):
  s.t=TemporaryDirectory();s.p=Path(s.t.name);s.db=Database(s.p/'x.db');s.db.initialise();s.f=s.p/'a.jpg';s.f.write_bytes(b'abc');s.h=hashlib.sha256(b'abc').hexdigest()
  with closing(sqlite3.connect(s.db.path)) as c:
   c.execute("insert into imap_messages(mailbox_identity,uidvalidity,uid,processing_status) values('i','1','1','RECEIVED')");m=c.execute('select id from imap_messages').fetchone()[0];c.execute("insert into imap_attachments(message_db_id,attachment_index,sha256,storage_path,processing_status) values(?,?,?,?, 'FILE_STORED')",(m,0,s.h,str(s.f)));s.a=c.execute('select id from imap_attachments').fetchone()[0]
   c.commit()
 def tearDown(s):s.t.cleanup()
 def test_idempotent(s):
  one=s.db.register_stored_attachment_invoice(s.a);two=s.db.register_stored_attachment_invoice(s.a);s.assertEqual(one['id'],two['id']);s.assertEqual(one['status'],'RECEIVED')
 def test_missing(s):
  s.f.unlink()
  with s.assertRaises(ValueError):s.db.register_stored_attachment_invoice(s.a)
