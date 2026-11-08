"""Command-line entry point for the invoice automation."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from src.config import ConfigError, load_config
from src.database import Database
from src.approval import ApprovalWorkflow, EmailConfigurationError, EmailSettings, ImapInbox, SmtpMailer
from src.intake import intake_directory
from src.logging_setup import configure_logging
from src.ocr import GoogleVisionClient, OcrError, extract_invoice_fields
from src.po_validation import PoDataError, load_purchase_orders, validate_invoice
from src.erp_posting import FormFields, GoogleFormsPoster, PostingError, PostingUncertain, diagnose_form_fields, diagnose_submit_button, validate_form_fields, validate_responder_form_url
from src.export import export_all
from src.archive import archive_final
from src.preflight import run_preflight
from src.imap_adapter import ReadOnlyImapAdapter
from src.email_intake import run_email_intake
from src.erp_test_fixture import prepare_erp_auto_test_fixture
from src.e2e_orchestrator import FinalE2EPaths, preflight_final_e2e, prepare_final_e2e_environment, run_stages, validate_isolated_paths

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
    # Jangan tulis alamat approver ke terminal/log; cukup catat status konfigurasi.
    approver_configured = not config.approver_email.lower().endswith("@example.com")
    logger.info("Setup complete. threshold=%s, approver_configured=%s", config.threshold_amount, approver_configured)
    # Menampilkan konfirmasi setup ke pengguna terminal.
    print("Setup complete: database and runtime folders are ready.")
    # Menampilkan threshold yang benar-benar terbaca dari Excel.
    print(f"Approval threshold: {config.threshold_amount:,.0f}")
    # Menampilkan status tanpa mengekspos alamat email pribadi.
    print(f"Approver: {'configured' if approver_configured else 'not configured'}")


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


def email_intake(args: argparse.Namespace) -> None:
    """Mengambil satu batch invoice IMAP secara read-only sampai siap OCR."""
    load_dotenv(ROOT / '.env')
    logger=configure_logging(ROOT)
    def required(name: str) -> str:
        value=os.getenv(name,'').strip()
        if not value: raise EmailConfigurationError(f'{name} is missing; add it to .env')
        return value
    host=required('IMAP_HOST'); username=required('IMAP_USERNAME'); password=required('IMAP_PASSWORD')
    try:
        port=int(os.getenv('IMAP_PORT','993'))
        timeout=int(os.getenv('IMAP_INTAKE_TIMEOUT_SECONDS','30'))
        max_bytes=int(os.getenv('IMAP_ATTACHMENT_MAX_BYTES','10485760'))
    except ValueError as error:
        raise EmailConfigurationError('IMAP port, timeout, and attachment size must be integers') from error
    if port < 1 or timeout < 1 or max_bytes < 1:
        raise EmailConfigurationError('IMAP port, timeout, and attachment size must be positive')
    mailbox=os.getenv('IMAP_INTAKE_MAILBOX','INBOX').strip() or 'INBOX'
    # Identity mencakup mailbox dan username untuk mencegah collision antar akun.
    mailbox_identity=f'{username}:{mailbox}'
    adapter=ReadOnlyImapAdapter(host,port,username,password,mailbox,timeout)
    if args.list_uids:
        # Hanya UID/UIDVALIDITY yang dicetak; body dan attachment tidak di-fetch.
        if not 1 <= args.limit <= 100: raise EmailConfigurationError('--limit must be between 1 and 100')
        for item in adapter.list_unseen_uids(args.limit): print(f'UID={item.uid} UIDVALIDITY={item.uidvalidity}')
        return
    if bool(args.uid) != bool(args.uidvalidity):
        raise EmailConfigurationError('--uid and --uidvalidity must be supplied together')
    summary=run_email_intake(adapter,mailbox_identity,args.db_path,args.storage_dir,max_bytes,args.uid,args.uidvalidity)
    logger.info('email_intake_complete=%s',json.dumps(summary,sort_keys=True))
    print(f"Email intake complete: {json.dumps(summary,sort_keys=True)}")


def run_ocr(retry_failed: bool = False, db_path: Path | None = None) -> None:
    """Recognise and extract data from all invoices waiting for OCR."""
    # Memuat credential lokal dari .env tanpa memasukkannya ke source code.
    load_dotenv(ROOT / ".env")
    # Menyalakan logging untuk semua hasil OCR dan error per invoice.
    logger = configure_logging(ROOT)
    # Membuka database invoice lokal.
    database = Database(db_path or ROOT / "data" / "automation.db")
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


def validate_purchase_orders(po_file: Path, db_path: Path | None = None) -> None:
    """Validate OCR-extracted invoices against the configured PO CSV source."""
    # Mengaktifkan logging terstruktur untuk setiap hasil validasi.
    logger = configure_logging(ROOT)
    # Membuka layanan database SQLite.
    database = Database(db_path or ROOT / "data" / "automation.db")
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


def start_approval(db_path: Path | None = None) -> None:
    """Queue approval decisions and send durable outbox records once."""
    # Memuat credential email dari .env tanpa memasukkannya ke source code.
    load_dotenv(ROOT / ".env")
    # Menyalakan logging operasional workflow approval.
    logger = configure_logging(ROOT)
    # Membaca threshold dan approver dinamis dari Config.xlsx.
    config = load_config(ROOT / "resources" / "Config.xlsx")
    # Membuka database dan memastikan schema approval tersedia.
    database = Database(db_path or ROOT / "data" / "automation.db")
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


def retry_approval_delivery(db_path: Path | None = None) -> None:
    """Explicitly retry bounded FAILED outbox records; SENDING records are never auto-resent."""
    # Memuat credential SMTP dari file environment lokal.
    load_dotenv(ROOT / ".env")
    # Menyalakan logging delivery retry.
    logger = configure_logging(ROOT)
    # Membuka database outbox.
    database = Database(db_path or ROOT / "data" / "automation.db")
    database.initialise()
    # Membuat mailer; tanpa setting valid, retry tidak boleh mengubah state outbox.
    mailer = SmtpMailer(EmailSettings.from_environment())
    # Menjalankan retry untuk FAILED, maksimal tiga attempt per record.
    summary = ApprovalWorkflow(database, logger).deliver(mailer, retry_failed=True)
    # Menampilkan hasil batch retry yang ringkas.
    print(f"Approval delivery retry complete: {summary['sent']} sent, {summary['failed']} failed.")


def read_approval_replies(db_path: Path | None = None, max_messages: int | None = None) -> None:
    """Read one finite IMAP batch and apply valid approval decisions."""
    # Memuat credential IMAP dari file environment lokal.
    load_dotenv(ROOT / ".env")
    # Menyalakan logging untuk hasil reply yang tervalidasi.
    logger = configure_logging(ROOT)
    # Membaca approver resmi dari Config.xlsx.
    config = load_config(ROOT / "resources" / "Config.xlsx")
    # Membuka database approval state.
    database = Database(db_path or ROOT / "data" / "automation.db")
    database.initialise()
    limit=max_messages if max_messages is not None else int(os.getenv('APPROVAL_IMAP_MAX_MESSAGES','10'))
    timeout=int(os.getenv('APPROVAL_IMAP_TIMEOUT_SECONDS','30'))
    if not 1 <= limit <= 100 or timeout < 1:
        raise EmailConfigurationError('approval IMAP max messages must be 1-100 and timeout must be positive')
    # Mengambil batch terbatas read-only; receipt existing dicegah sebelum BODY.PEEK dilakukan.
    messages = ImapInbox(EmailSettings.from_environment(),timeout).unread_messages(config.approver_email,limit,database.approval_receipt_uids())
    logger.info('approval_reply_batch_fetched count=%s limit=%s',len(messages),limit)
    # Memvalidasi sender, token, expiry, dan decision lalu menyimpan audit trail.
    summary = ApprovalWorkflow(database, logger).process_replies(messages, config.approver_email)
    # Menampilkan hasil agregat tanpa token, password, atau isi email.
    print(f"Approval reply processing complete: {json.dumps(summary, sort_keys=True)}")


def diagnose_approval_reply(uid: str) -> None:
    """Menampilkan metadata parsing aman untuk satu UID reply tanpa menerapkan keputusan approval."""
    load_dotenv(ROOT / '.env')
    timeout=int(os.getenv('APPROVAL_IMAP_TIMEOUT_SECONDS','30'))
    if timeout < 1: raise EmailConfigurationError('APPROVAL_IMAP_TIMEOUT_SECONDS must be positive')
    diagnostic=ImapInbox(EmailSettings.from_environment(),timeout).diagnose_uid(uid)
    print(json.dumps(asdict(diagnostic),sort_keys=True))


def check_email_connectivity() -> None:
    """Check configured SMTP and IMAP connectivity without sending or reading approval mail."""
    load_dotenv(ROOT / ".env")
    settings = EmailSettings.from_environment()
    # SMTP hanya melakukan TLS/login/NOOP, sehingga tidak ada approval request terkirim.
    SmtpMailer(settings).check_connectivity()
    # IMAP hanya membuka INBOX read-only, sehingga tidak ada email diubah.
    ImapInbox(settings).check_connectivity()
    print("Email connectivity check passed: SMTP and IMAP authentication succeeded.")


def post_to_erp(db_path: Path | None = None) -> None:
    """Post approval-final invoices once through visible Playwright browser."""
    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / "resources" / "Config.xlsx")
    if "your-form-url" in config.forms_url:
        raise ValueError("FormsUrl is still a placeholder; create Google Form first")
    validate_responder_form_url(config.forms_url)
    # Path opsional menjaga live-test terisolasi dari database operasional.
    database = Database(db_path or ROOT / "data" / "automation.db"); database.initialise()
    logger = configure_logging(ROOT)
    from playwright.sync_api import sync_playwright
    timeout = int(os.getenv("ERP_BROWSER_TIMEOUT_MS", "30000"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=os.getenv("HEADLESS", "false").lower() == "true")
        try:
            page = browser.new_page()
            poster = GoogleFormsPoster(page, FormFields.from_environment(), timeout, logger)
            for candidate in database.pending_erp_invoices():
                invoice = database.reserve_posting(candidate["correlation_id"])
                if not invoice: continue
                database.set_posting_state(invoice["correlation_id"], "POSTING_IN_PROGRESS")
                try:
                    poster.post(config.forms_url, invoice)
                    database.set_posting_state(invoice["correlation_id"], "POSTED")
                except PostingUncertain as error:
                    database.set_posting_state(invoice["correlation_id"], "POSTING_UNCERTAIN", str(error))
                except PostingError as error:
                    database.set_posting_state(invoice["correlation_id"], "POSTING_FAILED", str(error))
        finally:
            # Menutup Chromium walau posting gagal agar proses browser tidak tertinggal.
            browser.close()


def check_erp_form() -> None:
    """Memvalidasi akses dan tujuh field Google Form tanpa submission atau input data."""
    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / "resources" / "Config.xlsx")
    validate_responder_form_url(config.forms_url)
    from playwright.sync_api import sync_playwright
    timeout = int(os.getenv("ERP_BROWSER_TIMEOUT_MS", "30000"))
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                missing = validate_form_fields(page, config.forms_url, FormFields.from_environment(), timeout)
            finally:
                # Browser selalu ditutup juga saat navigasi/locator timeout agar tidak ada proses yatim.
                browser.close()
    except Exception as error:
        # Detail Playwright dapat panjang; jangan sertakan URL query atau data form pada CLI.
        raise ValueError("Google Form could not be reached within the configured browser timeout") from error
    if missing:
        raise ValueError(f"Google Form missing required field label(s): {', '.join(missing)}")
    print("ERP form connectivity check passed: all seven required labels are available.")


def diagnose_erp_form() -> None:
    """Mendiagnosis locator responden Google Forms tanpa data invoice atau Submit."""
    load_dotenv(ROOT / ".env")
    config = load_config(ROOT / "resources" / "Config.xlsx")
    validate_responder_form_url(config.forms_url)
    from playwright.sync_api import sync_playwright
    timeout = int(os.getenv("ERP_BROWSER_TIMEOUT_MS", "30000"))
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                # DOM cukup siap untuk diagnosis; networkidle tidak cocok untuk Google Forms.
                page.goto(config.forms_url, timeout=timeout, wait_until="domcontentloaded")
                page.wait_for_timeout(min(1500, timeout))
                fields = FormFields.from_environment()
                diagnostics = diagnose_form_fields(page, fields)
                submit_diagnostic = diagnose_submit_button(page, fields)
            finally:
                browser.close()
    except Exception as error:
        raise ValueError("Google Form locator diagnostic could not complete") from error
    print(json.dumps({"fields": [asdict(item) for item in diagnostics], "submit": asdict(submit_diagnostic)}, sort_keys=True))


def prepare_erp_retry(db_path: Path, invoice_number: str, confirmed_no_response: bool, dry_run: bool) -> None:
    """Menyiapkan retry ERP yang disengaja setelah operator merekonsiliasi Google Forms."""
    if not confirmed_no_response:
        raise ValueError("Recovery requires --confirmed-no-response after manual Google Forms reconciliation")
    database = Database(db_path)
    # Dry-run tidak memanggil initialise agar benar-benar tidak memodifikasi SQLite.
    if not dry_run:
        database.initialise()
    result = database.prepare_failed_posting_retry(invoice_number, dry_run=dry_run)
    print("ERP retry preparation: " + json.dumps(result, sort_keys=True))


def reconcile_erp_posting(db_path: Path, invoice_number: str, posting_ref: str, confirmed_in_responses: bool, dry_run: bool) -> None:
    """Merekonsiliasi hasil submission manual tanpa browser atau submit ulang."""
    if not confirmed_in_responses:
        raise ValueError("Reconciliation requires --confirmed-in-responses after verifying exactly one matching Google Forms response")
    database = Database(db_path)
    # Dry-run harus membaca saja; database produksi/testing tidak diinisialisasi ulang.
    if not dry_run:
        database.initialise()
    result = database.reconcile_uncertain_posting(invoice_number, posting_ref, dry_run=dry_run)
    print("ERP reconciliation: " + json.dumps(result, sort_keys=True))


def prepare_erp_auto_test(db_path: Path, storage_dir: Path, fixture_id: str) -> None:
    """Menyiapkan fixture lokal untuk live test ERP otomatis berikutnya, tanpa browser."""
    test_root = (ROOT / "data" / "erp-auto-test").resolve()
    if not db_path.resolve().is_relative_to(test_root) or not storage_dir.resolve().is_relative_to(test_root):
        raise ValueError("ERP auto-test fixture paths must remain under data/erp-auto-test")
    result = prepare_erp_auto_test_fixture(db_path, storage_dir, fixture_id)
    print("ERP auto-test fixture prepared: " + json.dumps(result, sort_keys=True))


def final_e2e_paths(args: argparse.Namespace) -> FinalE2EPaths:
    """Menyatukan seluruh path mutable agar satu run tidak mencampur environment."""
    return FinalE2EPaths(args.db_path, args.storage_dir, args.output_dir, args.archive_dir)


def prepare_final_e2e(args: argparse.Namespace) -> None:
    """Membuat SQLite/folder fresh end-to-end test tanpa memanggil integrasi eksternal."""
    paths = final_e2e_paths(args)
    prepare_final_e2e_environment(paths, ROOT)
    print("Final E2E environment prepared: " + json.dumps({"database": str(paths.database), "incoming": str(paths.incoming), "output": str(paths.output), "processed": str(paths.processed)}, sort_keys=True))


def run_once(args: argparse.Namespace) -> None:
    """Menjalankan satu siklus state machine terisolasi dengan UID intake eksplisit."""
    paths = final_e2e_paths(args)
    if args.dry_run:
        print("Final E2E preflight: " + json.dumps(preflight_final_e2e(paths, ROOT), sort_keys=True))
        return
    validate_isolated_paths(paths, ROOT)
    if not args.resume and (not args.uid or not args.uidvalidity):
        raise ValueError("Fresh run-once requires both --uid and --uidvalidity; use --resume only after prior intake")
    prepare_final_e2e_environment(paths, ROOT)
    # Semua stage memakai database dan direktori yang sama. Approval reply sengaja tidak dibaca
    # otomatis: approver harus memberi keputusan dahulu, lalu operator memakai --resume.
    intake_args = argparse.Namespace(list_uids=False, limit=1, uid=args.uid, uidvalidity=args.uidvalidity, db_path=paths.database, storage_dir=paths.incoming)
    stages = {
        "email_intake": lambda: email_intake(intake_args),
        "ocr": lambda: run_ocr(db_path=paths.database),
        "po_validation": lambda: validate_purchase_orders(ROOT / "resources" / "PurchaseOrders.csv", paths.database),
        "approval": lambda: start_approval(paths.database),
        "erp": lambda: post_to_erp(paths.database),
        "export": lambda: export_excel(paths.database, paths.output),
        "archive": lambda: archive_invoices(paths.database, paths.processed),
    }
    completed = run_stages(stages, resume=args.resume)
    print("Final E2E run complete: " + json.dumps({"completed_stages": completed, "resume": args.resume}, sort_keys=True))

def export_excel(db_path: Path | None = None, output_dir: Path | None = None) -> None:
    """Export SQLite source of truth ke tiga file Excel secara atomik."""
    destination = output_dir or ROOT / 'output'
    export_all(db_path or ROOT/'data'/'automation.db', ROOT/'resources', destination)
    print(f'Excel export complete: {destination}')

def archive_invoices(db_path: Path | None = None, archive_dir: Path | None = None) -> None:
    """Archive hanya invoice final tanpa mengubah state workflow."""
    destination = archive_dir or ROOT / 'data' / 'processed'
    print(f"Archive complete: {archive_final(db_path or ROOT/'data'/'automation.db', destination)}")

def preflight_check() -> None:
    """Menjalankan audit readiness read-only tanpa network atau mutasi database."""
    blockers, warnings = run_preflight(ROOT)
    print('PREFLIGHT BLOCKERS: ' + ('none' if not blockers else '; '.join(blockers)))
    print('PREFLIGHT WARNINGS: ' + ('none' if not warnings else '; '.join(warnings)))


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
    email_intake_parser=subparsers.add_parser("email-intake", help="read one IMAP invoice batch without changing mailbox flags")
    email_intake_parser.add_argument('--list-uids',action='store_true',help='list unread UID metadata only; never fetch message bodies')
    email_intake_parser.add_argument('--limit',type=int,default=20,help='latest UID metadata to show with --list-uids (1-100)')
    email_intake_parser.add_argument('--uid',help='process exactly one UID; requires --uidvalidity')
    email_intake_parser.add_argument('--uidvalidity',help='expected UIDVALIDITY for --uid')
    email_intake_parser.add_argument('--db-path',type=Path,default=ROOT/'data'/'automation.db',help='SQLite path; use data/live-test/automation.db for tests')
    email_intake_parser.add_argument('--storage-dir',type=Path,default=ROOT/'data'/'incoming',help='attachment storage; use data/live-test/incoming for tests')
    # Membuat parser khusus untuk command OCR.
    ocr_parser = subparsers.add_parser("ocr", help="run Google Vision OCR and regex extraction")
    # Mengizinkan retry eksplisit untuk invoice yang sebelumnya berstatus OCR_FAILED.
    ocr_parser.add_argument("--retry-failed", action="store_true", help="retry invoices previously marked OCR_FAILED")
    ocr_parser.add_argument("--db-path", type=Path, default=ROOT / "data" / "automation.db", help="SQLite path; use data/live-test/automation.db for isolated testing")
    # Mendaftarkan command re-extraction untuk perbaikan regex tanpa biaya API.
    subparsers.add_parser("reextract", help="re-run regex extraction from stored OCR text")
    # Membuat parser khusus command PO validation.
    po_parser = subparsers.add_parser("po-validate", help="validate extracted invoices against PurchaseOrders.csv")
    # Mengizinkan file PO diganti saat reviewer menjalankan aplikasi dengan data mereka sendiri.
    po_parser.add_argument("--po-file", type=Path, default=ROOT / "resources" / "PurchaseOrders.csv", help="path to PurchaseOrders.csv")
    po_parser.add_argument("--db-path", type=Path, default=ROOT / "data" / "automation.db", help="SQLite path; use data/live-test/automation.db for isolated testing")
    # Mendaftarkan command yang membuat auto approval dan approval request baru.
    approval_parser=subparsers.add_parser("approval-initiate", help="prepare approval decisions and send queued requests once")
    approval_parser.add_argument('--db-path',type=Path,default=ROOT/'data'/'automation.db',help='SQLite path; use data/live-test/automation.db for isolated testing')
    # Mendaftarkan retry eksplisit untuk outbox FAILED; tidak pernah retry SENDING otomatis.
    retry_parser=subparsers.add_parser("approval-retry-delivery", help="explicitly retry failed approval emails")
    retry_parser.add_argument('--db-path',type=Path,default=ROOT/'data'/'automation.db',help='SQLite path; use data/live-test/automation.db for isolated testing')
    # Mendaftarkan pembacaan satu batch reply IMAP.
    replies_parser=subparsers.add_parser("approval-read-replies", help="read and apply one IMAP batch of approval replies")
    replies_parser.add_argument('--db-path',type=Path,default=ROOT/'data'/'automation.db',help='SQLite path; use data/live-test/automation.db for isolated testing')
    replies_parser.add_argument('--max-messages',type=int,help='read at most 1-100 recent unprocessed candidate replies')
    diagnose_parser=subparsers.add_parser('approval-diagnose-reply',help='read-only MIME diagnostic for one approval reply UID')
    diagnose_parser.add_argument('--uid',required=True,help='numeric IMAP UID; this command fetches no other message')
    subparsers.add_parser("email-check", help="check SMTP/IMAP connectivity without sending email")
    erp_post_parser=subparsers.add_parser("erp-post", help="post approved invoices through Google Forms")
    erp_post_parser.add_argument('--db-path',type=Path,default=ROOT/'data'/'automation.db',help='SQLite path; use data/live-test/automation.db for isolated testing')
    erp_retry_parser=subparsers.add_parser("erp-retry-prepare", help="prepare a reconciled POSTING_FAILED invoice; never submits Google Forms")
    erp_retry_parser.add_argument('--db-path',type=Path,required=True,help='SQLite path for the explicitly reconciled invoice')
    erp_retry_parser.add_argument('--invoice-number',required=True,help='invoice number in POSTING_FAILED state')
    erp_retry_parser.add_argument('--confirmed-no-response',action='store_true',help='acknowledge manual Google Forms reconciliation found no submission')
    erp_retry_parser.add_argument('--dry-run',action='store_true',help='validate recovery guards without changing SQLite')
    erp_reconcile_parser=subparsers.add_parser("erp-reconcile", help="reconcile one POSTING_UNCERTAIN invoice from verified Google Forms Responses; never submits")
    erp_reconcile_parser.add_argument('--db-path',type=Path,required=True,help='SQLite path for the explicitly reconciled invoice')
    erp_reconcile_parser.add_argument('--invoice-number',required=True,help='invoice number in POSTING_UNCERTAIN state')
    erp_reconcile_parser.add_argument('--posting-ref',required=True,help='PostingRef verified in exactly one Google Forms response')
    erp_reconcile_parser.add_argument('--confirmed-in-responses',action='store_true',help='attest exactly one response matches PostingRef and invoice data')
    erp_reconcile_parser.add_argument('--dry-run',action='store_true',help='validate reconciliation guards without changing SQLite')
    erp_auto_fixture_parser=subparsers.add_parser("erp-auto-test-prepare", help="create only an isolated APPROVED ERP fixture; never opens a browser")
    erp_auto_fixture_parser.add_argument('--db-path',type=Path,default=ROOT/'data'/'erp-auto-test'/'automation.db')
    erp_auto_fixture_parser.add_argument('--storage-dir',type=Path,default=ROOT/'data'/'erp-auto-test'/'incoming')
    erp_auto_fixture_parser.add_argument('--fixture-id',default='component-test-v1',help='lowercase unique fixture identity, e.g. run-3')
    final_prepare_parser=subparsers.add_parser("final-e2e-prepare", help="create only fresh isolated SQLite/folders for end-to-end testing")
    final_run_parser=subparsers.add_parser("run-once", help="run one isolated end-to-end cycle; fresh runs require explicit IMAP UID")
    for item in (final_prepare_parser, final_run_parser):
        item.add_argument('--db-path',type=Path,default=ROOT/'data'/'final-e2e-test'/'automation.db')
        item.add_argument('--storage-dir',type=Path,default=ROOT/'data'/'final-e2e-test'/'incoming')
        item.add_argument('--output-dir',type=Path,default=ROOT/'data'/'final-e2e-test'/'output')
        item.add_argument('--archive-dir',type=Path,default=ROOT/'data'/'final-e2e-test'/'processed')
    final_run_parser.add_argument('--uid',help='single Gmail UID for a fresh run; never scans the entire inbox')
    final_run_parser.add_argument('--uidvalidity',help='UIDVALIDITY paired with --uid')
    final_run_parser.add_argument('--resume',action='store_true',help='skip Gmail intake and continue existing isolated states after approval')
    final_run_parser.add_argument('--dry-run',action='store_true',help='read-only local preflight; no DB or external integration')
    subparsers.add_parser("erp-form-check", help="check Google Form fields without filling or submitting")
    subparsers.add_parser("erp-form-diagnose", help="read-only locator diagnostic; never fills or submits Google Form")
    export_parser=subparsers.add_parser("export-excel", help="export database to official Excel templates")
    export_parser.add_argument('--db-path',type=Path,default=ROOT/'data'/'automation.db',help='SQLite path; use data/live-test/automation.db for isolated testing')
    export_parser.add_argument('--output-dir',type=Path,default=ROOT/'output',help='output directory; use data/live-test/output for isolated testing')
    archive_parser=subparsers.add_parser("archive-invoices", help="archive final invoice files")
    archive_parser.add_argument('--db-path',type=Path,default=ROOT/'data'/'automation.db',help='SQLite path; use data/live-test/automation.db for isolated testing')
    archive_parser.add_argument('--archive-dir',type=Path,default=ROOT/'data'/'processed',help='archive directory; use data/live-test/processed for isolated testing')
    subparsers.add_parser("preflight-check", help="read-only deployment readiness audit")
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
        elif args.command == "email-intake":
            email_intake(args)
        # Menjalankan OCR dan ekstraksi bila command adalah ocr.
        elif args.command == "ocr":
            # Mengirim pilihan retry yang diketik pengguna ke workflow OCR.
            run_ocr(retry_failed=args.retry_failed,db_path=args.db_path)
        # Menjalankan ulang regex jika command adalah reextract.
        elif args.command == "reextract":
            reextract()
        # Menjalankan validasi PO jika command adalah po-validate.
        elif args.command == "po-validate":
            validate_purchase_orders(args.po_file,args.db_path)
        # Memulai workflow approval setelah PO validation sukses.
        elif args.command == "approval-initiate":
            start_approval(args.db_path)
        # Menjalankan retry delivery secara eksplisit dan terbatas.
        elif args.command == "approval-retry-delivery":
            retry_approval_delivery(args.db_path)
        # Membaca satu batch reply approval melalui IMAP.
        elif args.command == "approval-read-replies":
            read_approval_replies(args.db_path,args.max_messages)
        elif args.command == 'approval-diagnose-reply':
            diagnose_approval_reply(args.uid)
        elif args.command == "email-check":
            check_email_connectivity()
        elif args.command == "erp-post":
            post_to_erp(args.db_path)
        elif args.command == "erp-retry-prepare":
            prepare_erp_retry(args.db_path, args.invoice_number, args.confirmed_no_response, args.dry_run)
        elif args.command == "erp-reconcile":
            reconcile_erp_posting(args.db_path, args.invoice_number, args.posting_ref, args.confirmed_in_responses, args.dry_run)
        elif args.command == "erp-auto-test-prepare":
            prepare_erp_auto_test(args.db_path, args.storage_dir, args.fixture_id)
        elif args.command == "final-e2e-prepare":
            prepare_final_e2e(args)
        elif args.command == "run-once":
            run_once(args)
        elif args.command == "erp-form-check":
            check_erp_form()
        elif args.command == "erp-form-diagnose":
            diagnose_erp_form()
        elif args.command == "export-excel": export_excel(args.db_path,args.output_dir)
        elif args.command == "archive-invoices": archive_invoices(args.db_path,args.archive_dir)
        elif args.command == "preflight-check": preflight_check()
    # Menangkap error konfigurasi dan credential dengan pesan yang mudah dipahami.
    except (ConfigError, OcrError, PoDataError, EmailConfigurationError, OSError, ValueError) as error:
        # Mengakhiri aplikasi dengan pesan ringkas, bukan traceback panjang.
        raise SystemExit(f"Configuration error: {error}") from error


if __name__ == "__main__":
    # Memulai program hanya ketika file ini dieksekusi langsung.
    main()
