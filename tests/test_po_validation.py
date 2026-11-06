"""Unit tests for PO normalisation and validation outcomes."""  # Menjelaskan cakupan test modul PO.

from decimal import Decimal  # Membuat nominal test secara presisi.
import unittest  # Menggunakan framework test bawaan Python.

from src.po_validation import PurchaseOrder, validate_invoice  # Mengimpor unit domain yang diuji.


class PurchaseOrderValidationTests(unittest.TestCase):  # Mengelompokkan test skenario PO validation.
    def setUp(self) -> None:  # Menyiapkan data PO reusable untuk setiap test.
        self.purchase_orders = {  # Membuat index PO dengan key yang telah dinormalisasi.
            "PO001": PurchaseOrder("PO-001", "PT Maju Jaya", Decimal("1200000"), "IDR", True)  # Membuat PO aktif yang valid.
        }  # Menutup index PO.
        self.valid_invoice = {  # Menyiapkan invoice yang seharusnya lolos seluruh comparison.
            "po_number": "po 001",  # Menguji normalisasi spasi dan kapitalisasi PO.
            "vendor_name": "PT. MAJU  JAYA",  # Menguji normalisasi tanda baca dan spasi vendor.
            "amount": "1,200,000",  # Menguji normalisasi angka dengan pemisah ribuan.
            "currency": "idr",  # Menguji normalisasi kapitalisasi currency.
        }  # Menutup data invoice valid.

    def test_matches_normalized_invoice(self) -> None:  # Memastikan invoice valid tetap match walaupun formatnya bervariasi.
        result = validate_invoice(self.valid_invoice, self.purchase_orders)  # Menjalankan validator terhadap data valid.
        self.assertTrue(result.is_valid)  # Memastikan keputusan validator adalah sukses.
        self.assertEqual(result.match_status, "MATCHED")  # Memastikan status workflow benar.

    def test_returns_po_not_found(self) -> None:  # Memastikan PO tidak terdaftar ditolak secara eksplisit.
        invoice = {**self.valid_invoice, "po_number": "PO-999"}  # Membuat invoice dengan PO yang tidak ada.
        self.assertEqual(validate_invoice(invoice, self.purchase_orders).match_status, "PO_NOT_FOUND")  # Memeriksa kode hasil.

    def test_returns_vendor_mismatch(self) -> None:  # Memastikan vendor berbeda terdeteksi.
        invoice = {**self.valid_invoice, "vendor_name": "PT Vendor Lain"}  # Mengganti vendor dengan nilai salah.
        self.assertEqual(validate_invoice(invoice, self.purchase_orders).match_status, "VENDOR_MISMATCH")  # Memeriksa kode mismatch.

    def test_returns_amount_mismatch(self) -> None:  # Memastikan nominal berbeda terdeteksi.
        invoice = {**self.valid_invoice, "amount": "1200001"}  # Mengganti nominal dengan nilai salah.
        self.assertEqual(validate_invoice(invoice, self.purchase_orders).match_status, "AMOUNT_MISMATCH")  # Memeriksa kode mismatch.

    def test_returns_currency_mismatch(self) -> None:  # Memastikan mata uang berbeda terdeteksi.
        invoice = {**self.valid_invoice, "currency": "USD"}  # Mengganti currency dengan nilai salah.
        self.assertEqual(validate_invoice(invoice, self.purchase_orders).match_status, "CURRENCY_MISMATCH")  # Memeriksa kode mismatch.

    def test_returns_missing_required_fields(self) -> None:  # Memastikan field wajib kosong tidak diproses sebagai match.
        invoice = {**self.valid_invoice, "amount": None}  # Mengosongkan amount invoice.
        self.assertEqual(validate_invoice(invoice, self.purchase_orders).match_status, "MISSING_REQUIRED_FIELDS")  # Memeriksa kode missing field.

    def test_returns_missing_fields_for_invalid_amount(self) -> None:  # Memastikan nominal OCR rusak tidak dibandingkan sebagai angka valid.
        invoice = {**self.valid_invoice, "amount": "not-a-number"}  # Membuat nominal yang tidak dapat dinormalisasi.
        self.assertEqual(validate_invoice(invoice, self.purchase_orders).match_status, "MISSING_REQUIRED_FIELDS")  # Memeriksa status aman untuk data rusak.
