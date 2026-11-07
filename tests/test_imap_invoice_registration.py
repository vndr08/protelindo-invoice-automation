import hashlib, sqlite3, unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
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
 def test_corrupt_file_rejected(s):
  s.f.write_bytes(b'changed')
  with s.assertRaises(ValueError):s.db.register_stored_attachment_invoice(s.a)
  with closing(sqlite3.connect(s.db.path)) as c:s.assertEqual(c.execute('select count(*) from invoices').fetchone()[0],0)
 def test_duplicate_hash_local_intake(s):
  from src.database import InvoiceRegistration
  s.db.register_invoice(InvoiceRegistration('local',str(s.f),s.h))
  invoice=s.db.register_stored_attachment_invoice(s.a)
  s.assertEqual(invoice['correlation_id'],'local')
 def test_concurrent_registration(s):
  # Dua worker memakai file SQLite yang sama; UNIQUE file_hash mencegah invoice ganda.
  with ThreadPoolExecutor(max_workers=2) as pool:
   results=list(pool.map(lambda _:s.db.register_stored_attachment_invoice(s.a),range(2)))
  s.assertEqual(results[0]['id'],results[1]['id'])
  with closing(sqlite3.connect(s.db.path)) as c:s.assertEqual(c.execute('select count(*) from invoices').fetchone()[0],1)
 def test_finalize_idempotent(s):
  s.db.register_stored_attachment_invoice(s.a)
  s.assertTrue(s.db.finalize_imap_attachment(s.a));s.assertFalse(s.db.finalize_imap_attachment(s.a))
 def test_offline_recovery(s):
  from src.imap_recovery import recover
  s.assertEqual(recover(s.db)['completed'],1)
  with closing(sqlite3.connect(s.db.path)) as c:s.assertEqual(c.execute("select processing_status from imap_attachments where id=?",(s.a,)).fetchone()[0],'COMPLETED')
 def test_forced_rollback_after_invoice_insert(s):
  def fail(): raise RuntimeError('forced rollback')
  with s.assertRaises(RuntimeError):s.db.register_stored_attachment_invoice(s.a,fail)
  with closing(sqlite3.connect(s.db.path)) as c:
   s.assertEqual(c.execute('select count(*) from invoices').fetchone()[0],0)
   s.assertEqual(c.execute('select processing_status,invoice_id from imap_attachments where id=?',(s.a,)).fetchone(),('FILE_STORED',None))
  invoice=s.db.register_stored_attachment_invoice(s.a)
  s.assertEqual(invoice['status'],'RECEIVED')
