"""Regression test pemilihan SQLite terisolasi untuk OCR dan PO validation."""
import csv
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import main
from src.database import Database, InvoiceRegistration


class IsolatedOcrPoTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.root=Path(self.temp.name)
        self.db_path=self.root/'live-test.db'; self.other_db_path=self.root/'main.db'
        self.file=self.root/'invoice.jpg'; self.file.write_bytes(b'fixture image')
        self.database=Database(self.db_path); self.database.initialise()
        self.database.register_invoice(InvoiceRegistration('isolated-correlation',str(self.file),hashlib.sha256(self.file.read_bytes()).hexdigest()))
        Database(self.other_db_path).initialise()

    def tearDown(self):
        self.temp.cleanup()

    def test_ocr_uses_selected_database_path(self):
        text='Vendor: PT Test\nInvoice No: INV-1\nPO Number: PO-1\nTotal: IDR 1000'
        with patch.object(main.GoogleVisionClient,'from_environment') as client, \
             patch.object(main,'configure_logging',return_value=Mock()):
            client.return_value.detect_text.return_value=text
            main.run_ocr(db_path=self.db_path)
        invoice=Database(self.db_path).invoices()[0]
        self.assertEqual((invoice['status'],invoice['invoice_number'],invoice['po_number']),('EXTRACTED','INV-1','PO-1'))
        self.assertEqual(Database(self.other_db_path).invoice_count(),0)

    def test_po_validation_uses_selected_database_path(self):
        self.database.save_ocr_result('isolated-correlation','ignored',type('Extracted',(),{
            'invoice_number':'INV-1','vendor_name':'PT Test','po_number':'PO-1','amount':'1000','currency':'IDR',
        })())
        csv_path=self.root/'PurchaseOrders.csv'
        with csv_path.open('w',newline='',encoding='utf-8') as file:
            writer=csv.DictWriter(file,fieldnames=['PO_Number','VendorName','TotalAmount','Currency','IsActive'])
            writer.writeheader(); writer.writerow({'PO_Number':'PO-1','VendorName':'PT Test','TotalAmount':'1000','Currency':'IDR','IsActive':'TRUE'})
        with patch.object(main,'configure_logging',return_value=Mock()):
            main.validate_purchase_orders(csv_path,self.db_path)
        invoice=Database(self.db_path).invoices()[0]
        self.assertEqual((invoice['status'],invoice['match_status']),('PO_VALIDATED','MATCHED'))
        self.assertEqual(Database(self.other_db_path).invoice_count(),0)


if __name__ == '__main__':
    unittest.main()
