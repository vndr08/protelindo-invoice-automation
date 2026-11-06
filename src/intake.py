"""Local invoice-file intake with content-based duplicate detection."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from src.database import Database, InvoiceRegistration

# Menentukan tipe file yang boleh masuk ke pipeline invoice.
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf"}


@dataclass(frozen=True)
class IntakeSummary:
    registered: int = 0
    duplicates: int = 0
    unsupported: int = 0


def file_sha256(path: Path) -> str:
    """Return a streaming SHA-256 fingerprint for a file."""
    # Membuat objek hash SHA-256 kosong.
    digest = hashlib.sha256()
    # Membuka file sebagai biner agar berlaku untuk gambar maupun PDF.
    with path.open("rb") as file:
        # Membaca 1 MB per iterasi supaya file besar tidak memenuhi memori.
        for block in iter(lambda: file.read(1024 * 1024), b""):
            # Menambahkan isi blok saat ini ke perhitungan hash.
            digest.update(block)
    # Mengembalikan hash hexadecimal untuk duplicate detection.
    return digest.hexdigest()


def intake_directory(directory: Path, database: Database, logger: logging.Logger) -> IntakeSummary:
    """Register supported files in a flat incoming directory, preserving originals."""
    # Membuat folder incoming jika belum tersedia.
    directory.mkdir(parents=True, exist_ok=True)
    # Memulai penghitung hasil batch dengan nilai nol.
    summary = IntakeSummary()
    # Memproses file secara konsisten berdasarkan urutan nama.
    for path in sorted(directory.iterdir()):
        # Melewati folder dan file tersembunyi seperti .gitkeep.
        if not path.is_file() or path.name.startswith("."):
            continue
        # Menolak ekstensi yang bukan invoice image/PDF yang didukung.
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            # Menyimpan peringatan operasional di log.
            logger.warning("Skipping unsupported inbound file: %s", path.name)
            # Menyimpan catatan skip untuk audit process log.
            database.log_event(None, "LOCAL_INTAKE", "SKIPPED", f"Unsupported extension: {path.name}")
            # Memperbarui jumlah file tidak didukung.
            summary = IntakeSummary(summary.registered, summary.duplicates, summary.unsupported + 1)
            continue

        # Menyiapkan metadata unik yang akan disimpan untuk invoice baru.
        registration = InvoiceRegistration(
            # Memberi setiap proses invoice correlation ID yang unik.
            correlation_id=str(uuid4()),
            # Menyimpan absolute path agar file dapat ditemukan di langkah berikutnya.
            file_path=str(path.resolve()),
            # Menggunakan hash isi file agar perubahan nama tidak melewati duplicate check.
            file_hash=file_sha256(path),
        )
        # Mencoba menyimpan invoice baru ke database.
        if database.register_invoice(registration):
            # Mencatat bahwa invoice unik berhasil masuk database.
            logger.info("Registered inbound invoice %s (%s)", path.name, registration.correlation_id)
            # Menambah jumlah invoice yang berhasil didaftarkan.
            summary = IntakeSummary(summary.registered + 1, summary.duplicates, summary.unsupported)
        else:
            # Memberi peringatan jika hash invoice sudah pernah diproses.
            logger.warning("Duplicate inbound invoice skipped: %s", path.name)
            # Menulis audit log duplicate tanpa membuat invoice kedua.
            database.log_event(None, "LOCAL_INTAKE", "DUPLICATE", f"Duplicate content: {path.name}")
            # Menambah jumlah file duplicate pada ringkasan batch.
            summary = IntakeSummary(summary.registered, summary.duplicates + 1, summary.unsupported)
    # Mengembalikan hasil batch kepada command-line layer.
    return summary
