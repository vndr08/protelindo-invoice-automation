import errno, sqlite3, unittest
from src.imap_recovery import classify_recovery_error
class T(unittest.TestCase):
 def test_classification(self):
  self.assertEqual(classify_recovery_error(sqlite3.OperationalError('database is locked')),'TRANSIENT')
  self.assertEqual(classify_recovery_error(OSError(errno.EAGAIN,'again')),'TRANSIENT')
  self.assertEqual(classify_recovery_error(FileNotFoundError()),'NEEDS_REFETCH')
  self.assertEqual(classify_recovery_error(ValueError('hash mismatch')),'PERMANENT')
  self.assertEqual(classify_recovery_error(RuntimeError()),'UNKNOWN')
