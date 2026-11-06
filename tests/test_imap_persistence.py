"""Test persistence IMAP tanpa network memakai SQLite temporary."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from src.database import Database

class ImapPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.db=Database(Path(self.temp.name)/'test.db'); self.db.initialise()
    def tearDown(self): self.temp.cleanup()
    def test_message_identity_is_unique(self):
        first=self.db.register_imap_message('INBOX','7','10','<x>','a@b','subject',None)
        self.assertIsNotNone(first); self.assertIsNone(self.db.register_imap_message('INBOX','7','10','<x>','a@b','subject',None))
    def test_attachment_identity_and_recovery(self):
        message=self.db.register_imap_message('INBOX','7','11',None,None,None,None)
        self.assertTrue(self.db.register_imap_attachment(message,0,'x.jpg','abc','/safe/x.jpg'))
        self.assertFalse(self.db.register_imap_attachment(message,0,'x.jpg','abc','/safe/x.jpg'))
        self.db.update_imap_attachment(message,0,'FAILED','corrupt')
        self.assertEqual(self.db.pending_imap_attachments()[0]['processing_status'],'FAILED')
