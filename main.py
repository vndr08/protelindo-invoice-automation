"""Command-line entry point for the invoice automation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from src.config import ConfigError, load_config
from src.database import Database
from src.approval import ApprovalWorkflow, EmailConfigurationError, EmailSettings, ImapInbox, SmtpMailer
from src.intake import intake_directory
from src.logging_setup import configure_logging
from src.ocr import GoogleVisionClient, OcrError, extract_invoice_fields
from src.po_validation import PoDataError, load_purchase_orders, validate_invoice

# Menentukan folder root proyek dari lokasi file main.py ini.
ROOT = Path(__file__).resolve().parent


def initialise() -> None:
    """Create local runtime folders and initialise the SQLite schema."""
    # Mendaftarkan folder runtime yang wajib tersedia sebelum proses berjalan.
    for directory in ("data/incoming", "data/processed", "output", "logs"):
        # Membuat folder beserta induknya; tidak gagal bila sudah ada.
        (ROOT / directory).mkdir(parents=True, exist_ok=True)

    # Mengaktifkan pencatatan log ke terminal dan file.
    logger = configure_logging(ROOT)
    # Membaca nilai bisnis dari Config.xlsx agar tidak di-hardcode.
    config = load_config(ROOT / "resources" / "Config.xlsx")
    # Menentukan lokasi database SQLite aplikasi.
    database = Database(ROOT / "data" / "automation.db")
    # Membuat tabel serta indeks database jika belum ada.
    database.initialise()
    # Mencatat parameter penting setup ke log aplikasi.
    logger.info("Setup complete. threshold=%s, approver=%s", config.threshold_amount, config.approver_email)
    # Menampilkan konfirmasi setup ke pengguna terminal.
    print("Setup complete: database and runtime folders are ready.")
    # Menampilkan threshold yang benar-benar terbaca dari Excel.
    print(f"Approval threshold: {config.threshold_amount:,.0f}")
    # Menampilkan approver hasil konfigurasi untuk validasi awal.
    print(f"Approver: {config.approver_email}")


def status() -> None:
    """Show the local database location and invoice count."""
    # Membuka layanan database pada file SQLite lokal.
    database = Database(ROOT / "data" / "automation.db")
    # Menjamin command status tetap aman dijalankan pada proyek baru.
    database.initialise()
    # Menampilkan path file database yang sedang digunakan.
    print(f"Database: {ROOT / 'data' / 'automation.db'}")
    # Menampilkan jumlah invoice yang telah terdaftar.
    print(f"Invoices stored: {database.invoice_count()}")


def intake() -> None:
    """Register newly dropped invoice images from the local incoming folder."""
    # Mengaktifkan log agar setiap hasil intake mudah ditelusuri.
    logger = configure_logging(ROOT)
    # Membuka koneksi layanan database.
    database = Database(ROOT / "data" / "automation.db")
    # Memastikan tabel tersedia sebelum menyimpan invoice baru.
    database.initialise()
    # Memindai file invoice dari folder incoming dan menyimpan metadata yang valid.
    summary = intake_directory(ROOT / "data" / "incoming", database, logger)
    # Menampilkan ringkasan hasil satu batch intake.
    print(
        "Intake complete: "
        f"{summary.registered} registered, {summary.duplicates} duplicate(s), "
        f"{summary.unsupported} unsupported file(s)."
    )


def run_ocr(retry_failed: bool = False) -> None:
    """Recognise and extract data from all invoices waiting for OCR."""
    # Memuat credential lokal dari .env tanpa memasukkannya ke source code.
    load_dotenv(ROOT / ".env")
    # Menyalakan logging untuk semua hasil OCR dan error per invoice.
    logger = configure_logging(ROOT)
    # Membuka database invoice lokal.
    database = Database(ROOT / "data" / "automation.db")
    # Memastikan skema database tersedia sebelum pembaruan data.
    database.initialise()
    # Membuat klien Google Vision yang membaca API key dari environment.
    client = GoogleVisionClient.from_environment()
    # Mengambil invoice baru, dan opsional invoice gagal untuk dijalankan ulang setelah error diperbaiki.
    invoices = database.pending_ocr_invoices(include_failed=retry_failed)
    # Mengawali penghitung hasil batch OCR.
    succeeded = 0
    # Mengawali penghitung OCR yang gagal.
    failed = 0
    # Memproses satu invoice per iterasi supaya kegagalan satu file tidak menghentikan batch.
    for invoice in invoices:
        try:
            # Meminta teks mentah invoice kepada Google Cloud Vision.
            ocr_text = client.detect_text(Path(invoice["file_path"]))
            # Mengambil field bisnis wajib dari teks menggunakan regex, bukan LLM.
            extracted = extract_invoice_fields(ocr_text)
            # Menyimpan teks mentah serta hasil ekstraksi ke database.
            database.save_ocr_result(invoice["correlation_id"], ocr_text, extracted)
            # Mencatat sukses beserta invoice yang diproses.
            logger.info("OCR succeeded for %s", invoice["correlation_id"])
            # Menambah jumlah OCR yang berhasil.
            succeeded += 1
        except OcrError as error:
            # Menyimpan error sebagai status agar dapat ditinjau atau di-retry kemudian.
            database.mark_ocr_failed(invoice["correlation_id"], str(error))
            # Mencatat detail kegagalan teknis ke log.
            logger.exception("OCR failed for %s: %s", invoice["correlation_id"], error)
            # Menambah jumlah OCR yang gagal.
            failed += 1
    # Menampilkan ringkasan batch tanpa membocorkan credential atau isi invoice.
    print(f"OCR complete: {succeeded} succeeded, {failed} failed, {len(invoices)} processed.")


def reextract() -> None:
    """Re-run regex extraction from stored OCR text without calling Google Vision."""
    # Membuka database yang menyimpan teks hasil OCR sebelumnya.
    database = Database(ROOT / "data" / "automation.db")
    # Menjamin tabel tersedia sebelum pembaruan hasil regex.
    database.initialise()
    # Mengambil invoice yang sudah berhasil OCR dan memiliki teks mentah.
    invoices = database.extracted_invoices()
    # Memproses setiap teks OCR tanpa request ke Google Vision.
    for invoice in invoices:
        # Menjalankan regex terbaru pada teks OCR yang tersimpan.
        extracted = extract_invoice_fields(invoice["ocr_text"] or "")
        # Memperbarui field terstruktur tetapi mempertahankan teks OCR yang sama.
        database.save_ocr_result(invoice["correlation_id"], invoice["ocr_text"] or "", extracted)
    # Menampilkan jumlah record yang diperbarui kepada pengguna.
    print(f"Re-extraction complete: {len(invoices)} invoice(s) updated without OCR API calls.")


def validate_purchase_orders(po_file: Path) -> None:
    """Validate OCR-extracted invoices against the configured PO CSV source."""
    # Mengaktifkan logging terstruktur untuk setiap hasil validasi.
    logger = configure_logging(ROOT)
    # Membuka layanan database SQLite.
    database = Database(ROOT / "data" / "automation.db")
    # Menjamin kolom migrasi dan schema database tersedia.
    database.initialise()
    # Membaca serta memvalidasi master PO sebelum mengubah state invoice apa pun.
    purchase_orders = load_purchase_orders(po_file)
    # Mengambil hanya invoice OCR yang belum pernah divalidasi untuk menjaga idempotency.
    invoices = database.pending_po_validation_invoices()
    # Membuat penghitung hasil sukses untuk summary batch.
    matched = 0
    # Membuat penghitung hasil gagal untuk summary batch.
    failed = 0
    # Memproses setiap invoice secara independen agar hasil batch tetap tahan error data individual.
    for invoice in invoices:
        # Menjalankan comparison PO nomor, vendor, nominal, dan currency.
        result = validate_invoice(invoice, purchase_orders)
        # Menyimpan hasil hanya apabila status invoice masih EXTRACTED.
        saved = database.save_po_validation_result(invoice["correlation_id"], result)
        if not saved:
            # Mencatat race/idempotency skip dalam JSON agar mudah diproses sistem observability.
            logger.info("po_validation=%s", json.dumps({"correlation_id": invoice["correlation_id"], "event": "skipped"}))
            continue
        # Mencatat hasil validasi dengan key-value yang konsisten.
        logger.info(
            "po_validation=%s",
            json.dumps(
                {
                    "correlation_id": invoice["correlation_id"],
                    "match_status": result.match_status,
                    "result": "success" if result.is_valid else "failed",
                    "reason": result.reason,
                }
            ),
        )
        if result.is_valid:
            # Menambah counter match penuh.
            matched += 1
        else:
            # Menambah counter failure validasi.
            failed += 1
    # Menampilkan summary batch yang mudah digunakan untuk demo assessment.
    print(f"PO validation complete: {matched} matched, {failed} failed, {len(invoices)} processed.")


def approval_ttl_hours() -> int:
    """Read a bounded token lifetime from environment without hardcoding business data."""
    # Membaca TTL lokal dengan default 24 jam untuk token approval.
    value = int(os.getenv("APPROVAL_TOKEN_TTL_HOURS", "24"))
    # Menolak nilai yang tidak aman atau tidak masuk akal.
    if not 1 <= value <= 168:
        raise EmailConfigurationError("APPROVAL_TOKEN_TTL_HOURS must be between 1 and 168")
    # Mengembalikan jumlah jam yang telah tervalidasi.
    return value


def start_approval() -> None:
    """Queue approval decisions and send durable outbox records once."""
    # Memuat credential email dari .env tanpa memasukkannya ke source code.
    load_dotenv(ROOT / ".env")
    # Menyalakan logging operasional workflow approval.
    logger = configure_logging(ROOT)
    # Membaca threshold dan approver dinamis dari Config.xlsx.
    config = load_config(ROOT / "resources" / "Config.xlsx")
    # Membuka database dan memastikan schema approval tersedia.
    database = Database(ROOT / "data" / "automation.db")
    database.initialise()
    # Membuat workflow yang menggunakan SQLite sebagai persistent state machine.
    workflow = ApprovalWorkflow(database, logger)
    # Mencoba memuat SMTP; bila belum diset, outbox tetap aman berada pada READY.
    try:
        mailer = SmtpMailer(EmailSettings.from_environment())
    except EmailConfigurationError:
        mailer = None
        logger.warning("approval_smtp_not_configured; requests will remain safely queued in outbox")
    # Menjalankan auto approval, queue request, dan delivery sekali jalan.
    summary = workflow.initiate(config.threshold_amount, config.approver_email, approval_ttl_hours(), mailer)
    # Menampilkan summary non-sensitif untuk operator.
    print(f"Approval initiation complete: {summary['auto_approved']} auto-approved, {summary['pending']} pending, {summary['sent']} email(s) sent, {summary['failed']} email(s) failed.")


def retry_approval_delivery() -> None:
    """Explicitly retry bounded FAILED outbox records; SENDING records are never auto-resent."""
    # Memuat credential SMTP dari file environment lokal.
    load_dotenv(ROOT / ".env")
    # Menyalakan logging delivery retry.
    logger = configure_logging(ROOT)
    # Membuka database outbox.
    database = Database(ROOT / "data" / "automation.db")
    database.initialise()
    # Membuat mailer; tanpa setting valid, retry tidak boleh mengubah state outbox.
    mailer = SmtpMailer(EmailSettings.from_environment())
    # Menjalankan retry untuk FAILED, maksimal tiga attempt per record.
    summary = ApprovalWorkflow(database, logger).deliver(mailer, retry_failed=True)
    # Menampilkan hasil batch retry yang ringkas.
    print(f"Approval delivery retry complete: {summary['sent']} sent, {summary['failed']} failed.")


def read_approval_replies() -> None:
    """Read one finite IMAP batch and apply valid approval decisions."""
    # Memuat credential IMAP dari file environment lokal.
    load_dotenv(ROOT / ".env")
    # Menyalakan logging untuk hasil reply yang tervalidasi.
    logger = configure_logging(ROOT)
    # Membaca approver resmi dari Config.xlsx.
    config = load_config(ROOT / "resources" / "Config.xlsx")
    # Membuka database approval state.
    database = Database(ROOT / "data" / "automation.db")
    database.initialise()
    # Mengambil satu batch email UNSEEN tanpa polling tanpa batas.
    messages = ImapInbox(EmailSettings.from_environment()).unread_messages()
    # Memvalidasi sender, token, expiry, dan decision lalu menyimpan audit trail.
    summary = ApprovalWorkflow(database, logger).process_replies(messages, config.approver_email)
    # Menampilkan hasil agregat tanpa token, password, atau isi email.
    print(f"Approval reply processing complete: {json.dumps(summary, sort_keys=True)}")


def check_email_connectivity() -> None:
    """Check configured SMTP and IMAP connectivity without sending or reading approval mail."""
    load_dotenv(ROOT / ".env")
    settings = EmailSettings.from_environment()
    # SMTP hanya melakukan TLS/login/NOOP, sehingga tidak ada approval request terkirim.
    SmtpMailer(settings).check_connectivity()
    # IMAP hanya membuka INBOX read-only, sehingga tidak ada email diubah.
    ImapInbox(settings).check_connectivity()
    print("Email connectivity check passed: SMTP and IMAP authentication succeeded.")


def parse_args() -> argparse.Namespace:
    """Define and parse commands accepted by the application."""
    # Membuat parser command-line utama.
    parser = argparse.ArgumentParser(description="Protelindo invoice automation")
    # Membuat command turunan dan mewajibkan pengguna memilih salah satunya.
    subparsers = parser.add_subparsers(dest="command", required=True)
    # Mendaftarkan command setup awal.
    subparsers.add_parser("init", help="initialise folders, config validation, and database")
    # Mendaftarkan command pengecekan database.
    subparsers.add_parser("status", help="show local database status")
    # Mendaftarkan command pembacaan invoice lokal.
    subparsers.add_parser("intake", help="register invoice images from data/incoming")
    # Membuat parser khusus untuk command OCR.
    ocr_parser = subparsers.add_parser("ocr", help="run Google Vision OCR and regex extraction")
    # Mengizinkan retry eksplisit untuk invoice yang sebelumnya berstatus OCR_FAILED.
    ocr_parser.add_argument("--retry-failed", action="store_true", help="retry invoices previously marked OCR_FAILED")
    # Mendaftarkan command re-extraction untuk perbaikan regex tanpa biaya API.
    subparsers.add_parser("reextract", help="re-run regex extraction from stored OCR text")
    # Membuat parser khusus command PO validation.
    po_parser = subparsers.add_parser("po-validate", help="validate extracted invoices against PurchaseOrders.csv")
    # Mengizinkan file PO diganti saat reviewer menjalankan aplikasi dengan data mereka sendiri.
    po_parser.add_argument("--po-file", type=Path, default=ROOT / "resources" / "PurchaseOrders.csv", help="path to PurchaseOrders.csv")
    # Mendaftarkan command yang membuat auto approval dan approval request baru.
    subparsers.add_parser("approval-initiate", help="prepare approval decisions and send queued requests once")
    # Mendaftarkan retry eksplisit untuk outbox FAILED; tidak pernah retry SENDING otomatis.
    subparsers.add_parser("approval-retry-delivery", help="explicitly retry failed approval emails")
    # Mendaftarkan pembacaan satu batch reply IMAP.
    subparsers.add_parser("approval-read-replies", help="read and apply one IMAP batch of approval replies")
    subparsers.add_parser("email-check", help="check SMTP/IMAP connectivity without sending email")
    # Mengurai argument yang diketik pengguna dan mengembalikannya.
    return parser.parse_args()


def main() -> None:
    """Route the selected terminal command to its workflow."""
    # Mengambil command yang dipilih pengguna.
    args = parse_args()
    # Menangani error konfigurasi agar pesan terminal tetap mudah dipahami.
    try:
        # Menjalankan setup bila command adalah init.
        if args.command == "init":
            initialise()
        # Menjalankan diagnostik bila command adalah status.
        elif args.command == "status":
            status()
        # Menjalankan intake bila command adalah intake.
        elif args.command == "intake":
            intake()
        # Menjalankan OCR dan ekstraksi bila command adalah ocr.
        elif args.command == "ocr":
            # Mengirim pilihan retry yang diketik pengguna ke workflow OCR.
            run_ocr(retry_failed=args.retry_failed)
        # Menjalankan ulang regex jika command adalah reextract.
        elif args.command == "reextract":
            reextract()
        # Menjalankan validasi PO jika command adalah po-validate.
        elif args.command == "po-validate":
            validate_purchase_orders(args.po_file)
        # Memulai workflow approval setelah PO validation sukses.
        elif args.command == "approval-initiate":
            start_approval()
        # Menjalankan retry delivery secara eksplisit dan terbatas.
        elif args.command == "approval-retry-delivery":
            retry_approval_delivery()
        # Membaca satu batch reply approval melalui IMAP.
        elif args.command == "approval-read-replies":
            read_approval_replies()
        elif args.command == "email-check":
            check_email_connectivity()
    # Menangkap error konfigurasi dan credential dengan pesan yang mudah dipahami.
    except (ConfigError, OcrError, PoDataError, EmailConfigurationError, OSError, ValueError) as error:
        # Mengakhiri aplikasi dengan pesan ringkas, bukan traceback panjang.
        raise SystemExit(f"Configuration error: {error}") from error


if __name__ == "__main__":
    # Memulai program hanya ketika file ini dieksekusi langsung.
    main()
