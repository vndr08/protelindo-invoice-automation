# Protelindo Invoice Automation

Python automation for the Junior Automation Developer technical assessment.

## Current setup

This initial foundation provides:

- Excel-driven application configuration;
- SQLite database schema for invoices and process logs;
- local runtime folders and application logging;
- a CLI for initialisation and status checks.
- local image/PDF intake with SHA-256 duplicate detection.
- Google Cloud Vision OCR via API key in `.env` and regex-only invoice extraction.

## Quick start

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
python main.py init
python main.py status
python main.py intake
python main.py ocr
```

`resources/Config.xlsx` and the Excel templates are supplied with the assessment and must be present before `init`. Do not commit `.env`, credentials, or generated database/output files.

Place `.jpg`, `.jpeg`, `.png`, `.tif`, `.tiff`, or `.pdf` invoice files in `data/incoming/`, then run `python main.py intake`. Each unique file is registered in SQLite with a correlation ID. Re-running intake (or adding a copy of the same file under another name) is safely logged as a duplicate.

Add `GOOGLE_VISION_API_KEY` to your local `.env`, then run `python main.py ocr`. This processes invoices in `RECEIVED` status using Google Vision `TEXT_DETECTION`, then stores raw OCR text and regex-extracted invoice fields in SQLite. To retry known OCR failures after a network/configuration fix, run `python main.py ocr --retry-failed`. The current OCR command supports image files; PDF support will be added separately because it uses a different Google Vision flow.

If only a regex pattern changes, run `python main.py reextract`. It reuses stored OCR text and therefore makes no Google Vision API calls.

## Purchase Order validation

Validate all OCR-complete invoices against the supplied CSV:

```bash
python main.py po-validate
```

By default the command reads `resources/PurchaseOrders.csv`. A reviewer can point it to their own source without code changes:

```bash
python main.py po-validate --po-file /path/to/PurchaseOrders.csv
```

The validator normalizes PO number, vendor name, amount, and currency before comparison. It saves `MATCHED`, `PO_NOT_FOUND`, `VENDOR_MISMATCH`, `AMOUNT_MISMATCH`, `CURRENCY_MISMATCH`, or `MISSING_REQUIRED_FIELDS` plus an audit reason in SQLite. Each invoice is processed only from `EXTRACTED` status, so a normal re-run is idempotent and produces no duplicate validation transaction or process-log row.

## Email approval workflow

Configure the same test mailbox for SMTP and IMAP in `.env`; use an app password when your provider requires one. `ThresholdAmount` and `ApproverEmail` are always read from `resources/Config.xlsx`.

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=automation@example.com
SMTP_PASSWORD=app-password
IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_USERNAME=automation@example.com
IMAP_PASSWORD=app-password
APPROVAL_TOKEN_TTL_HOURS=24
```

For Gmail, enable two-step verification on the test mailbox, create a **Google App Password** for Mail, and use that 16-character app password as both `SMTP_PASSWORD` and `IMAP_PASSWORD`. Do not use the normal Gmail password. Use `smtp.gmail.com:587` and `imap.gmail.com:993`, then verify credentials without sending any email:

```bash
python main.py email-check
```

This command only performs SMTP `STARTTLS` + login + `NOOP`, plus IMAP TLS login and read-only `INBOX` access. It does not send, fetch, mark, or alter any email.

Run one approval batch after PO validation:

```bash
python main.py approval-initiate
```

Amounts at or below the configured threshold become `AUTO_APPROVED`. Larger amounts become `PENDING_APPROVAL`; the request is first saved to the SQLite outbox and then sent through SMTP. The approver must reply in this exact body format:

```text
APPROVE
Approval Token: <token-from-request>
```

Use `REJECT` instead of `APPROVE` to reject. Read one finite IMAP batch manually or from a scheduler:

```bash
python main.py approval-read-replies
```

If SMTP fails, the outbox is marked `FAILED` and can be retried explicitly (maximum three attempts):

```bash
python main.py approval-retry-delivery
```

Messages left in `SENDING` are deliberately never resent automatically: SMTP cannot guarantee exactly-once delivery if a network failure occurs after the server accepts a message but before SQLite records `SENT`. Reconcile such messages manually before retrying to avoid duplicate email. Passwords and approval tokens are never logged; the outbox body temporarily holds the token until delivery, so protect the local SQLite file with normal device/storage controls.

## Google Forms ERP simulation

Create a Google Form with required short-answer fields: `PostingRef`, `CorrelationId`, `InvoiceNumber`, `VendorName`, `PO_Number`, `Amount`, and `Currency`. Put its responder URL into `FormsUrl` in `resources/Config.xlsx`, install `playwright`, then run `playwright install chromium` once and execute `python main.py erp-post`. A POSTED state requires Google Forms confirmation text; timeout after Submit becomes `POSTING_UNCERTAIN` and is never retried automatically. `POSTING_UNCERTAIN` must be reconciled against Form responses using the Posting Reference before any manual retry.

Before any real submission, validate connectivity and labels only with `python main.py erp-form-check`. This command never fills a field or clicks Submit.

## Export and archive

Run `python main.py export-excel` to atomically rebuild `output/Invoices.xlsx`, `output/ERP_Dummy.xlsx`, and `output/ProcessLogs.xlsx` from SQLite. Run `python main.py archive-invoices` to move only final states: POSTED→success, REJECTED→rejected, PO_VALIDATION_FAILED→validation_failed, OCR_FAILED→review. Pending approval/posting records remain untouched. If an archive hash mismatches or file is missing, it remains unarchived and receives an audit log.

## Next milestones

1. Local invoice intake and duplicate detection.
2. Google Vision OCR and regex extraction.
3. PO validation and approval workflow.
4. Browser ERP submission and Excel export.
