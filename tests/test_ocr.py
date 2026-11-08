"""Unit tests for regex extraction without calling Google Vision."""  # Menjelaskan bahwa test tidak membutuhkan API key.

from decimal import Decimal  # Membandingkan nominal uang dengan presisi.
import unittest  # Menggunakan framework test bawaan Python.

from src.ocr import extract_invoice_fields  # Menguji fungsi regex extractor secara langsung.


class ExtractInvoiceFieldsTests(unittest.TestCase):  # Mengelompokkan skenario uji extractor.
    def test_extracts_indonesian_invoice(self) -> None:  # Memastikan format umum Indonesia dapat diekstrak.
        text = """PT Maju Jaya
Invoice No: INV-2026-001
PO Number: PO-001
Grand Total: Rp 1.200.000,00
"""  # Menyediakan teks yang secara realistis dapat keluar dari OCR.
        result = extract_invoice_fields(text)  # Menjalankan regex extraction.
        self.assertEqual(result.invoice_number, "INV-2026-001")  # Memastikan nomor invoice benar.
        self.assertEqual(result.vendor_name, "PT Maju Jaya")  # Memastikan vendor fallback benar.
        self.assertEqual(result.po_number, "PO-001")  # Memastikan nomor PO benar.
        self.assertEqual(result.amount, Decimal("1200000.00"))  # Memastikan nominal Indonesia dikonversi benar.
        self.assertEqual(result.currency, "IDR")  # Memastikan simbol Rupiah dinormalisasi ke IDR.

    def test_extracts_amount_and_vendor_name_labels(self) -> None:  # Memastikan format sample invoice assessment terbaca.
        text = """INVOICE INV-2025-001
Vendor Name: PT Maju Jaya
PO Number: PO-001
Amount: 1200000 IDR
"""  # Menyediakan teks OCR asli yang diperoleh dari sample invoice.
        result = extract_invoice_fields(text)  # Menjalankan extractor pada format sample.
        self.assertEqual(result.vendor_name, "PT Maju Jaya")  # Memastikan prefix Name tidak ikut tersimpan.
        self.assertEqual(result.amount, Decimal("1200000"))  # Memastikan Amount tanpa label Total terbaca.

    def test_extracts_label_value_column_layout(self) -> None:
        """Mendukung urutan label penuh lalu nilai penuh dari Google Vision."""
        text = """INVOICE
Invoice Number
Vendor Name
PO Number
Total Amount
Currency
INV-SCREEN-001
PT Maju Jaya
PO-001
IDR 1,200,000
IDR
"""
        result = extract_invoice_fields(text)
        self.assertEqual(result.invoice_number, "INV-SCREEN-001")
        self.assertEqual(result.vendor_name, "PT Maju Jaya")
        self.assertEqual(result.po_number, "PO-001")
        self.assertEqual(result.amount, Decimal("1200000"))
        self.assertEqual(result.currency, "IDR")

    def test_rejects_ambiguous_label_column_layout(self) -> None:
        """Label wajib yang ganda tidak boleh dipetakan berdasarkan posisi secara spekulatif."""
        text = """Invoice Number
Vendor Name
PO Number
Total Amount
Currency
Invoice Number
INV-SCREEN-001
PT Maju Jaya
PO-001
IDR 1,200,000
IDR
"""
        result = extract_invoice_fields(text)
        self.assertEqual(result.amount, None)
        self.assertEqual(result.invoice_number, None)

    def test_rejects_label_column_layout_with_missing_value(self) -> None:
        """Nilai kurang tidak boleh diisi dari sumber lain atau dari label berikutnya."""
        text = """Invoice Number
Vendor Name
PO Number
Total Amount
Currency
INV-SCREEN-001
PT Maju Jaya
PO-001
IDR 1,200,000
"""
        result = extract_invoice_fields(text)
        self.assertEqual(result.amount, None)
        self.assertEqual(result.currency, None)

    def test_rejects_label_column_layout_with_inconsistent_order(self) -> None:
        """Urutan label yang berubah membuat mapping posisi tidak aman."""
        text = """Invoice Number
Vendor Name
Total Amount
PO Number
Currency
INV-SCREEN-001
PT Maju Jaya
IDR 1,200,000
PO-001
IDR
"""
        result = extract_invoice_fields(text)
        self.assertEqual(result.amount, None)
        self.assertEqual(result.po_number, None)

    def test_extracts_exact_live_inline_ocr_text_after_invoice_header(self) -> None:
        """Header INVOICE di baris sendiri tidak boleh membuat regex mengambil kata Invoice sebagai nilai."""
        text = """INVOICE
Invoice Number: INV-SCREEN-002
Vendor Name: PT Maju Jaya
PO Number: PO-001
Total Amount: 1200000 IDR
Currency: IDR
Invoice Status: Unpaid
Description: Invoice for goods and services according to Purchase Order PO-001.
Thank you for your business.
"""
        result = extract_invoice_fields(text)
        self.assertEqual(result.invoice_number, "INV-SCREEN-002")
        self.assertEqual(result.vendor_name, "PT Maju Jaya")
        self.assertEqual(result.po_number, "PO-001")
        self.assertEqual(result.amount, Decimal("1200000"))
        self.assertEqual(result.currency, "IDR")

    def test_rejects_generic_invoice_and_po_identifiers(self) -> None:
        """Kata label seperti Invoice atau Number tidak boleh menjadi identifier bisnis."""
        invoice_label = extract_invoice_fields("Invoice Number: Invoice\nPO Number: PO-001")
        po_label = extract_invoice_fields("Invoice Number: INV-1\nPO Number: Number")
        self.assertIsNone(invoice_label.invoice_number)
        self.assertIsNone(po_label.po_number)


if __name__ == "__main__":  # Memungkinkan test dijalankan langsung sebagai script.
    unittest.main()  # Menjalankan seluruh unit test pada modul ini.
