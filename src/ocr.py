"""Google Cloud Vision OCR and regex-based invoice-field extraction."""  # Menjelaskan tanggung jawab modul ini.

from __future__ import annotations  # Mengaktifkan type hint modern.

import base64  # Mengubah bytes gambar menjadi format aman untuk request JSON.
import certifi  # Menyediakan certificate authority bundle yang konsisten pada macOS dan Windows.
import json  # Membuat dan membaca payload JSON Google Vision.
import os  # Membaca API key dari environment variable.
import re  # Menjalankan ekstraksi field dengan regular expression.
import ssl  # Membuat HTTPS context dengan certificate bundle yang tervalidasi.
from dataclasses import dataclass  # Membuat objek hasil yang ringkas dan bertipe.
from decimal import Decimal, InvalidOperation  # Menyimpan nilai uang secara presisi.
from pathlib import Path  # Menangani file invoice secara lintas platform.
from urllib.error import HTTPError, URLError  # Menangani kegagalan jaringan HTTP secara eksplisit.
from urllib.request import Request, urlopen  # Mengirim request REST tanpa dependency tambahan.


class OcrError(RuntimeError):  # Membuat tipe error khusus agar kegagalan OCR mudah dibedakan.
    """Raised when OCR cannot be configured or completed."""  # Menjelaskan kapan error ini digunakan.


@dataclass(frozen=True)  # Membuat hasil ekstraksi immutable setelah OCR selesai.
class ExtractedInvoice:  # Mendefinisikan lima field wajib assessment.
    invoice_number: str | None  # Menyimpan nomor invoice jika regex menemukannya.
    vendor_name: str | None  # Menyimpan nama vendor jika regex menemukannya.
    po_number: str | None  # Menyimpan nomor purchase order jika regex menemukannya.
    amount: Decimal | None  # Menyimpan nominal invoice tanpa floating-point error.
    currency: str | None  # Menyimpan mata uang invoice yang dinormalisasi.


class GoogleVisionClient:  # Mengenkapsulasi komunikasi REST ke Google Cloud Vision.
    """Small REST client for Google Vision TEXT_DETECTION."""  # Menjelaskan fungsi klien.

    endpoint = "https://vision.googleapis.com/v1/images:annotate"  # Menentukan endpoint OCR synchronous Google Vision.

    def __init__(self, api_key: str) -> None:  # Menerima API key yang telah disiapkan di environment.
        self.api_key = api_key  # Menyimpan key di memori proses saja.

    @classmethod  # Menandai factory method yang tidak memerlukan instance lebih dulu.
    def from_environment(cls) -> GoogleVisionClient:  # Membuat klien dari .env atau environment sistem.
        api_key = os.getenv("GOOGLE_VISION_API_KEY", "").strip()  # Membaca key lalu menghapus spasi tidak sengaja.
        if not api_key:  # Memastikan kredensial tersedia sebelum request jaringan dibuat.
            raise OcrError("GOOGLE_VISION_API_KEY is missing; add it to .env")  # Memberi instruksi perbaikan yang aman.
        return cls(api_key)  # Mengembalikan klien siap pakai.

    def detect_text(self, path: Path) -> str:  # Meminta dan mengembalikan teks lengkap satu gambar invoice.
        if path.suffix.lower() == ".pdf":  # Membatasi sementara karena endpoint gambar synchronous tidak menangani PDF lokal.
            raise OcrError("PDF OCR is not enabled yet; provide an image invoice (.jpg or .png)")  # Menjelaskan cara melanjutkan.
        try:  # Membuka file invoice dalam mode biner.
            image_content = path.read_bytes()  # Membaca seluruh gambar untuk payload Vision.
        except OSError as error:  # Menangani file hilang atau hak akses yang tidak cukup.
            raise OcrError(f"Cannot read invoice file: {path.name}") from error  # Mengubah error teknis menjadi error domain.
        payload = {  # Membuat format request yang diwajibkan API Vision.
            "requests": [  # Vision dapat menerima banyak request, tetapi batch ini mengirim satu invoice.
                {  # Membuka konfigurasi request invoice.
                    "image": {"content": base64.b64encode(image_content).decode("ascii")},  # Menyertakan gambar sebagai base64.
                    "features": [{"type": "TEXT_DETECTION"}],  # Meminta OCR teks umum pada invoice.
                }  # Menutup konfigurasi satu invoice.
            ]  # Menutup daftar request Vision.
        }  # Menutup payload JSON.
        request = Request(  # Membuat HTTP POST yang akan dikirim ke Google.
            f"{self.endpoint}?key={self.api_key}",  # Menyertakan API key melalui query string HTTPS.
            data=json.dumps(payload).encode("utf-8"),  # Mengubah payload dictionary menjadi bytes JSON.
            headers={"Content-Type": "application/json"},  # Menandai tipe isi request sebagai JSON.
            method="POST",  # Menetapkan method POST sesuai spesifikasi Vision.
        )  # Menutup pembuatan HTTP request.
        try:  # Memulai request jaringan yang mungkin gagal.
            # Membuat HTTPS context yang mempercayai CA bundle certifi, bukan certificate store Python yang bermasalah.
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            # Mengirim request dengan context HTTPS tervalidasi dan batas waktu 60 detik.
            with urlopen(request, timeout=60, context=ssl_context) as response:
                body = json.loads(response.read().decode("utf-8"))  # Membaca respons JSON Google.
        except HTTPError as error:  # Menangani API key tidak valid, quota, atau respons 4xx/5xx.
            raise OcrError(f"Google Vision request failed with HTTP {error.code}") from error  # Menyimpan status tanpa menulis key.
        except URLError as error:  # Menangani masalah jaringan atau DNS.
            raise OcrError("Google Vision is unreachable") from error  # Memberi error jaringan yang ringkas.
        result = body.get("responses", [{}])[0]  # Mengambil respons invoice pertama secara defensif.
        if "error" in result:  # Memeriksa error yang dikirim Vision di body sukses HTTP.
            raise OcrError(f"Google Vision error: {result['error'].get('message', 'unknown error')}")  # Mengangkat error API.
        annotations = result.get("textAnnotations", [])  # Mengambil kandidat teks dari respons OCR.
        if not annotations:  # Memastikan gambar menghasilkan teks.
            raise OcrError("No text detected in invoice")  # Menghentikan proses untuk invoice yang tidak terbaca.
        return annotations[0].get("description", "").strip()  # Mengembalikan teks penuh dari anotasi pertama.


def extract_invoice_fields(text: str) -> ExtractedInvoice:  # Mengubah teks OCR menjadi lima field invoice terstruktur.
    """Extract assessment fields using regex only, never an LLM."""  # Menegaskan kepatuhan terhadap brief.
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())  # Membersihkan spasi dan baris kosong OCR.
    invoice_number = _first_group(r"(?:invoice\s*(?:no\.?|number|#)?\s*[:#-]?\s*)([A-Z0-9][A-Z0-9/-]+)", normalized)  # Menangkap nomor invoice.
    po_number = _first_group(r"(?:p\.?o\.?\s*(?:no\.?|number|#)?\s*[:#-]?\s*)([A-Z0-9][A-Z0-9/-]+)", normalized)  # Menangkap nomor PO.
    currency = _find_currency(normalized)  # Menentukan mata uang dari simbol atau kode ISO.
    amount = _find_amount(normalized)  # Menentukan nominal total invoice.
    vendor_name = _find_vendor(normalized)  # Menentukan nama vendor dari label atau baris awal.
    return ExtractedInvoice(invoice_number, vendor_name, po_number, amount, currency)  # Mengembalikan hasil terstruktur.


def _first_group(pattern: str, text: str) -> str | None:  # Mengambil capture group pertama dari pola regex.
    match = re.search(pattern, text, flags=re.IGNORECASE)  # Menjalankan pencarian tanpa membedakan kapitalisasi.
    return match.group(1).strip() if match else None  # Mengembalikan nilai bersih atau None bila tidak ditemukan.


def _find_currency(text: str) -> str | None:  # Mendeteksi mata uang yang umum pada invoice assessment.
    if re.search(r"\bIDR\b|(?:^|\s)Rp\.?", text, flags=re.IGNORECASE):  # Mendeteksi kode atau simbol Rupiah.
        return "IDR"  # Menormalisasi Rupiah ke kode ISO IDR.
    if re.search(r"\bUSD\b|\$", text, flags=re.IGNORECASE):  # Mendeteksi Dollar AS.
        return "USD"  # Menormalisasi Dollar ke USD.
    if re.search(r"\bEUR\b|€", text, flags=re.IGNORECASE):  # Mendeteksi Euro.
        return "EUR"  # Menormalisasi Euro ke EUR.
    return None  # Mengembalikan None apabila mata uang tidak dapat ditentukan.


def _find_amount(text: str) -> Decimal | None:  # Mengambil nilai total paling relevan dari teks invoice.
    pattern = r"(?:grand\s+total|total\s+amount|amount\s+due|amount|total)\s*[:#-]?\s*(?:IDR|USD|EUR|Rp\.?|\$|€)?\s*([0-9][0-9.,]*)"  # Mendahulukan label total atau Amount pada sample assessment.
    value = _first_group(pattern, text)  # Mencari angka setelah label total.
    if value is None:  # Menangani format invoice yang tidak memiliki label total standar.
        return None  # Mengembalikan None untuk direview, bukan menebak nilai.
    return _parse_money(value)  # Mengubah representasi OCR menjadi Decimal.


def _parse_money(value: str) -> Decimal | None:  # Menormalkan format 1.000.000,00 atau 1,000,000.00.
    compact = value.replace(" ", "")  # Menghapus spasi yang mungkin dibuat OCR.
    if compact.count(",") == 1 and compact.count(".") >= 1 and compact.rfind(",") > compact.rfind("."):  # Mengenali format Indonesia.
        compact = compact.replace(".", "").replace(",", ".")  # Mengubah pemisah Indonesia menjadi format Decimal.
    else:  # Menangani format Inggris atau angka bulat dengan pemisah ribuan.
        compact = compact.replace(",", "")  # Menghapus pemisah ribuan koma.
    try:  # Mencoba mengubah angka bersih menjadi Decimal.
        return Decimal(compact)  # Mengembalikan nominal presisi.
    except InvalidOperation:  # Menangani OCR yang menghasilkan angka rusak.
        return None  # Memaksa data direview di tahap validasi berikutnya.


def _find_vendor(text: str) -> str | None:  # Mengambil nama vendor dari label eksplisit atau baris pertama yang cocok.
    labeled_vendor = _first_group(r"(?:vendor(?:\s+name)?|supplier|bill\s+from)\s*[:#-]?\s*([^\n]+)", text)  # Mencari label Vendor Name atau label vendor umum.
    if labeled_vendor:  # Memprioritaskan hasil dengan label jelas.
        return labeled_vendor[:120]  # Membatasi panjang untuk menjaga penyimpanan tetap wajar.
    for line in text.splitlines()[:5]:  # Memeriksa lima baris pertama sebagai fallback konservatif.
        if len(line) > 2 and not re.search(r"invoice|tax|address|date", line, flags=re.IGNORECASE):  # Menolak header yang bukan nama vendor.
            return line[:120]  # Mengembalikan kandidat vendor pertama.
    return None  # Mengembalikan None bila vendor tidak dapat diidentifikasi aman.
