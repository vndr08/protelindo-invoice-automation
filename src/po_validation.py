"""Reusable Purchase Order validation against a CSV source."""  # Menjelaskan fokus modul validasi PO.

from __future__ import annotations  # Mengaktifkan type hint modern.

import csv  # Membaca sumber data PurchaseOrders.csv.
import re  # Menormalisasi vendor dan nomor PO secara konsisten.
from dataclasses import dataclass  # Membentuk objek domain yang aman dan eksplisit.
from decimal import Decimal, InvalidOperation  # Membandingkan nominal uang tanpa floating-point error.
from pathlib import Path  # Menangani path CSV lintas sistem operasi.


class PoDataError(ValueError):  # Menyediakan error khusus untuk sumber PO yang tidak valid.
    """Raised when the Purchase Order CSV cannot be safely used."""  # Menjelaskan kapan error dipakai.


@dataclass(frozen=True)  # Membuat data PO tidak berubah setelah dibaca dari CSV.
class PurchaseOrder:  # Merepresentasikan satu baris PurchaseOrders.csv.
    po_number: str  # Menyimpan nomor PO asli dari CSV.
    vendor_name: str  # Menyimpan nama vendor asli dari CSV.
    total_amount: Decimal  # Menyimpan nilai PO secara presisi.
    currency: str  # Menyimpan kode mata uang PO.
    is_active: bool  # Menyimpan apakah PO dapat dipakai untuk posting baru.


@dataclass(frozen=True)  # Membuat hasil validasi stabil saat disimpan ke database.
class ValidationResult:  # Merepresentasikan keputusan validasi satu invoice.
    match_status: str  # Menyimpan status utama yang konsisten untuk workflow berikutnya.
    reason: str  # Menyimpan alasan yang dapat diaudit ketika validasi gagal.

    @property  # Membuat indikator sukses yang mudah dipakai caller.
    def is_valid(self) -> bool:  # Menentukan apakah invoice cocok sepenuhnya dengan PO.
        return self.match_status == "MATCHED"  # Hanya match penuh yang dianggap valid.


def normalize_po_number(value: str | None) -> str | None:  # Menyiapkan nomor PO untuk comparison yang tahan format OCR.
    if not value:  # Menangani field kosong tanpa menghasilkan string palsu.
        return None  # Mengembalikan None untuk menandai missing field.
    normalized = re.sub(r"[^A-Z0-9]", "", value.upper())  # Menghapus spasi, dash, dan variasi tanda baca.
    return normalized or None  # Menghindari string kosong setelah normalisasi.


def normalize_vendor_name(value: str | None) -> str | None:  # Menyiapkan nama vendor untuk comparison yang tidak sensitif format.
    if not value:  # Menangani nama vendor yang tidak berhasil diekstrak OCR.
        return None  # Mengembalikan None sebagai sinyal required field hilang.
    normalized = re.sub(r"[^A-Z0-9]", "", value.upper())  # Menghapus spasi, titik, koma, dan variasi kapitalisasi.
    return normalized or None  # Menghindari vendor kosong setelah dibersihkan.


def normalize_currency(value: str | None) -> str | None:  # Menyusun kode currency ke bentuk ISO uppercase.
    if not value:  # Menangani currency yang hilang dari OCR.
        return None  # Mengembalikan None untuk required-field validation.
    normalized = value.strip().upper()  # Menghapus spasi dan menyamakan kapitalisasi.
    aliases = {"RP": "IDR", "RUPIAH": "IDR"}  # Menangani alias currency yang umum pada OCR invoice Indonesia.
    return aliases.get(normalized, normalized) or None  # Mengembalikan ISO code atau alias yang sudah dinormalisasi.


def normalize_amount(value: object) -> Decimal | None:  # Mengubah nilai nominal dari database/CSV menjadi Decimal.
    if value is None or str(value).strip() == "":  # Menangani nominal kosong secara eksplisit.
        return None  # Mengembalikan None agar workflow memberi status missing field.
    text = str(value).strip().replace(" ", "")  # Menghapus spasi yang dapat ditambahkan OCR.
    if text.count(",") == 1 and text.count(".") >= 1 and text.rfind(",") > text.rfind("."):  # Mengenali format 1.000,00.
        text = text.replace(".", "").replace(",", ".")  # Menormalisasi format Indonesia untuk Decimal.
    else:  # Menangani format 1,000.00 atau angka bulat.
        text = text.replace(",", "")  # Menghapus koma pemisah ribuan.
    try:  # Mencoba parsing Decimal setelah normalisasi.
        return Decimal(text)  # Mengembalikan nominal presisi.
    except InvalidOperation:  # Menangani teks nominal invalid dari OCR/CSV.
        return None  # Menyatakan nominal tidak bisa digunakan dalam comparison.


def load_purchase_orders(path: Path) -> dict[str, PurchaseOrder]:  # Membaca dan memvalidasi sumber Purchase Order CSV.
    if not path.exists():  # Memastikan file sumber benar-benar tersedia.
        raise PoDataError(f"Purchase Order CSV not found: {path}")  # Menghentikan validasi dengan pesan jelas.
    required_columns = {"PO_Number", "VendorName", "TotalAmount", "Currency", "IsActive"}  # Mendefinisikan kontrak CSV resmi.
    purchase_orders: dict[str, PurchaseOrder] = {}  # Menyiapkan index PO untuk lookup cepat.
    try:  # Membuka CSV dengan encoding UTF-8 yang eksplisit.
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)  # Membaca tiap baris berdasarkan nama header.
            if not reader.fieldnames or not required_columns.issubset(reader.fieldnames):  # Memeriksa seluruh header wajib.
                raise PoDataError("Purchase Order CSV is missing required columns")  # Menolak sumber data ambigu.
            for row_number, row in enumerate(reader, start=2):  # Membaca data mulai baris kedua setelah header.
                po_number = normalize_po_number(row.get("PO_Number"))  # Menormalisasi key PO untuk index.
                total_amount = normalize_amount(row.get("TotalAmount"))  # Menormalisasi nilai PO.
                currency = normalize_currency(row.get("Currency"))  # Menormalisasi currency PO.
                is_active_text = str(row.get("IsActive", "")).strip().upper()  # Membaca flag aktivitas PO.
                if not po_number or not normalize_vendor_name(row.get("VendorName")) or total_amount is None or not currency:  # Memastikan data penting lengkap.
                    raise PoDataError(f"Invalid required PO data at CSV row {row_number}")  # Menolak CSV dengan baris tidak dapat divalidasi.
                if is_active_text not in {"TRUE", "FALSE"}:  # Menegakkan boolean CSV yang eksplisit.
                    raise PoDataError(f"Invalid IsActive value at CSV row {row_number}")  # Menolak flag ambigu.
                if po_number in purchase_orders:  # Memeriksa duplikasi nomor PO yang bisa membuat keputusan tidak deterministik.
                    raise PoDataError(f"Duplicate PO_Number in CSV: {row.get('PO_Number')}")  # Menghentikan proses sebelum salah posting.
                purchase_orders[po_number] = PurchaseOrder(  # Menyimpan PO dalam index normalisasi.
                    po_number=row["PO_Number"].strip(),  # Mempertahankan nomor PO asli untuk audit.
                    vendor_name=row["VendorName"].strip(),  # Mempertahankan vendor asli untuk audit.
                    total_amount=total_amount,  # Menyimpan nominal Decimal tervalidasi.
                    currency=currency,  # Menyimpan currency yang telah dinormalisasi.
                    is_active=is_active_text == "TRUE",  # Mengubah flag CSV menjadi boolean Python.
                )  # Menutup pembuatan objek PO.
    except OSError as error:  # Menangani error akses file.
        raise PoDataError(f"Unable to read Purchase Order CSV: {path}") from error  # Menyediakan error domain yang aman.
    return purchase_orders  # Mengembalikan index PO yang reusable.


def validate_invoice(invoice: dict[str, object], purchase_orders: dict[str, PurchaseOrder]) -> ValidationResult:  # Memvalidasi data invoice terhadap index PO.
    required_fields = {  # Mendefinisikan field invoice yang wajib ada untuk comparison penuh.
        "PO_Number": invoice.get("po_number"),  # Memetakan nama field bisnis ke kolom database.
        "VendorName": invoice.get("vendor_name"),  # Memetakan vendor invoice.
        "Amount": invoice.get("amount"),  # Memetakan nominal invoice.
        "Currency": invoice.get("currency"),  # Memetakan mata uang invoice.
    }  # Menutup mapping required field.
    missing = [name for name, value in required_fields.items() if value is None or str(value).strip() == ""]  # Mencari field kosong.
    if missing:  # Memastikan comparison tidak dilakukan pada data parsial.
        return ValidationResult("MISSING_REQUIRED_FIELDS", f"Missing required fields: {', '.join(missing)}")  # Mengembalikan alasan audit yang jelas.
    po_key = normalize_po_number(str(invoice["po_number"]))  # Menormalisasi PO invoice untuk lookup.
    normalized_vendor = normalize_vendor_name(str(invoice["vendor_name"]))  # Menormalisasi vendor invoice sebelum comparison.
    normalized_amount = normalize_amount(invoice["amount"])  # Menormalisasi nominal invoice sebelum comparison.
    normalized_currency = normalize_currency(str(invoice["currency"]))  # Menormalisasi currency invoice sebelum comparison.
    invalid_fields = [  # Menyusun field yang ada tetapi tidak dapat dipakai secara semantik.
        name  # Mengembalikan nama field yang gagal normalisasi.
        for name, value in {"PO_Number": po_key, "VendorName": normalized_vendor, "Amount": normalized_amount, "Currency": normalized_currency}.items()  # Memeriksa hasil normalisasi seluruh field.
        if value is None  # Menandai field invalid sebagai missing required field untuk workflow.
    ]  # Menutup list invalid field.
    if invalid_fields:  # Menghindari comparison terhadap OCR value yang rusak.
        return ValidationResult("MISSING_REQUIRED_FIELDS", f"Invalid required fields: {', '.join(invalid_fields)}")  # Menyimpan alasan yang dapat diaudit.
    purchase_order = purchase_orders.get(po_key or "")  # Mengambil PO secara aman dari index.
    if purchase_order is None:  # Menangani nomor PO yang tidak terdapat pada master data.
        return ValidationResult("PO_NOT_FOUND", f"PO not found: {invoice['po_number']}")  # Mengembalikan status resmi PO tidak ditemukan.
    if not purchase_order.is_active:  # Mencegah invoice memakai PO yang sudah tidak aktif.
        return ValidationResult("PO_INACTIVE", f"PO is inactive: {purchase_order.po_number}")  # Menyimpan alasan PO tidak bisa dipakai.
    mismatches: list[str] = []  # Mengumpulkan seluruh mismatch agar audit tidak kehilangan informasi.
    if normalized_vendor != normalize_vendor_name(purchase_order.vendor_name):  # Membandingkan vendor yang sudah dinormalisasi.
        mismatches.append("VENDOR_MISMATCH")  # Menambahkan alasan vendor berbeda.
    if normalized_amount != purchase_order.total_amount:  # Membandingkan nominal sebagai Decimal.
        mismatches.append("AMOUNT_MISMATCH")  # Menambahkan alasan nominal berbeda.
    if normalized_currency != purchase_order.currency:  # Membandingkan currency yang telah dinormalisasi.
        mismatches.append("CURRENCY_MISMATCH")  # Menambahkan alasan currency berbeda.
    if mismatches:  # Menangani satu atau lebih field yang tidak cocok.
        return ValidationResult(mismatches[0], "; ".join(mismatches))  # Menyimpan kode utama serta seluruh alasan mismatch.
    return ValidationResult("MATCHED", f"Matched active PO: {purchase_order.po_number}")  # Mengembalikan hasil valid penuh.
