import hashlib, sqlite3, unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from src.database import Database
from src.durable_storage import store_attachment
class T(unittest.TestCase):
 def setUp(s):
  s.t=TemporaryDirectory();s.db=Path(s.t.name)/'x.db';Database(s.db).initialise()
  with closing(sqlite3.connect(s.db)) as c:
   c.execute("insert into imap_messages(mailbox_identity,uidvalidity,uid,processing_status) values('i','1','1','RECEIVED')");s.mid=c.execute('select id from imap_messages').fetchone()[0]
 def tearDown(s):s.t.cleanup()
 def test_store_and_retry(s):
  data=b'abc';h=hashlib.sha256(data).hexdigest()
  with closing(sqlite3.connect(s.db)) as c:c.execute("insert into imap_attachments(message_db_id,attachment_index,sha256,processing_status) values(?,?,?,'DISCOVERED')",(s.mid,0,h));c.commit();aid=c.execute('select id from imap_attachments').fetchone()[0]
  p=store_attachment(s.db,aid,data,'.jpg',Path(s.t.name)/'files');s.assertTrue(p.exists());s.assertEqual(store_attachment(s.db,aid,data,'.jpg',Path(s.t.name)/'files'),p)
 def test_hash_mismatch(s):
  with closing(sqlite3.connect(s.db)) as c:c.execute("insert into imap_attachments(message_db_id,attachment_index,sha256) values(?,?,?)",(s.mid,1,'bad'));c.commit();aid=c.execute('select id from imap_attachments where attachment_index=1').fetchone()[0]
  with s.assertRaises(ValueError):store_attachment(s.db,aid,b'abc','.jpg',Path(s.t.name))
