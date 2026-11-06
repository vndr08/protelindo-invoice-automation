"""Database tests for atomic and idempotent PO validation persistence."""  # Menjelaskan cakupan test persistence PO.

from pathlib import Path  # Mengelola path database temporary.
import sqlite3  # Membuka SQLite untuk assertion state persistence.
from tempfile import TemporaryDirectory  # Membuat storage test yang otomatis dibersihkan.
import unittest  # Menggunakan framework test bawaan Python.

from src.database import Database, InvoiceRegistration  # Mengimpor persistence layer yang diuji.
from src.po_validation import ValidationResult  # Mengimpor hasil validator untuk disimpan.


class DatabasePoValidationTests(unittest.TestCase):  # Mengelompokkan skenario atomic persistence.
    def test_persists_only_one_result_for_same_invoice(self) -> None:  # Memastikan normal re-run tidak membuat transaksi/log duplikat.
        with TemporaryDirectory() as temporary_directory:  # Membuat folder test yang terisolasi.
            database_path = Path(temporary_directory) / "automation.db"  # Menentukan file SQLite sementara.
            database = Database(database_path)  # Membuat service database terhadap file temporary.
            database.initialise()  # Membuat schema dan migrasi terkini.
            registration = InvoiceRegistration("correlation-1", "/tmp/invoice.jpg", "hash-1")  # Menyiapkan invoice minimal yang unik.
            self.assertTrue(database.register_invoice(registration))  # Menyimpan invoice inbound.
            with sqlite3.connect(database_path) as connection:  # Membuka koneksi setup khusus test.
                connection.execute("UPDATE invoices SET status = 'EXTRACTED' WHERE correlation_id = ?", (registration.correlation_id,))  # Menyiapkan invoice pada tahap PO.
            result = ValidationResult("MATCHED", "Matched active PO: PO-001")  # Menyiapkan hasil validasi sukses.
            self.assertTrue(database.save_po_validation_result(registration.correlation_id, result))  # Menyimpan perubahan state pertama.
            self.assertFalse(database.save_po_validation_result(registration.correlation_id, result))  # Memastikan penyimpanan kedua di-skip.
            with sqlite3.connect(database_path) as connection:  # Membuka koneksi untuk assertion audit.
                log_count = connection.execute("SELECT COUNT(*) FROM process_logs WHERE step = 'PO_VALIDATION'").fetchone()[0]  # Menghitung log PO validation.
            self.assertEqual(log_count, 1)  # Memastikan hanya satu audit log yang dibuat.
