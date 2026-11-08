# Protelindo Invoice Automation

Python automation for the Protelindo Junior Automation Developer technical assessment.

The system processes an invoice from an IMAP mailbox through Google Cloud Vision OCR, regex extraction, SQLite, PO validation, email approval, browser-based ERP simulation, Excel export, and archival. A reviewer can use their own mailbox, API key, web form, and invoice files.

## Requirement coverage

| Assessment requirement | Implementation |
| --- | --- |
| Email intake and save invoice image | Read-only IMAP intake with a local-folder alternative, attachment validation, and SHA-256 deduplication. |
| Google Vision + regex extraction | Google Vision `TEXT_DETECTION`; regex extracts InvoiceNumber, VendorName, PO_Number, Amount, and Currency. No LLM is used. |
| Database as source of truth | SQLite stores invoices, OCR output, validation, approval, posting, archive state, and process logs. |
| PO validation | Dynamic `PurchaseOrders.csv` lookup with normalized PO/vendor/amount/currency comparison. |
| Threshold approval | `ThresholdAmount` and `ApproverEmail` are read from `Config.xlsx`; above-threshold invoices require a valid email reply. |
| Browser ERP form | Playwright populates a responder form and uses a persistent PostingRef. |
| Excel and archive | Generates `Invoices.xlsx`, `ERP_Dummy.xlsx`, and `ProcessLogs.xlsx` from SQLite, then archives final invoice images. |

## Architecture

```text
IMAP/local image
  -> Google Vision OCR
  -> regex extraction
  -> SQLite
  -> PO validation
  -> auto approval OR email approval
  -> Playwright ERP form
  -> archive
  -> Excel exports
```

The workflow is state-based and idempotent. Re-running a normal batch does not create a duplicate invoice, approval request, attachment, or ERP submission. A Google Forms submission without strong confirmation is recorded as `POSTING_UNCERTAIN` and is never auto-retried.

## Prerequisites

- Python 3.11 or newer
- A mailbox you control with IMAP and SMTP enabled (Gmail App Password is supported)
- A Google Cloud Vision API key
- A Google Forms responder URL
- Chromium for Playwright

Only JPG, JPEG, and PNG are supported end-to-end for email attachment OCR. PDF OCR is intentionally not supported by the current Google Vision image flow.

## Installation

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

## Configuration

### `.env`

Copy `.env.example` to `.env` and provide your own values. The file is ignored by Git and must never be committed.

Required for the complete workflow:

- `GOOGLE_VISION_API_KEY`
- `IMAP_HOST`, `IMAP_PORT`, `IMAP_USERNAME`, `IMAP_PASSWORD`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`
- `ERP_CONFIRMATION_TEXT` — exact success message shown by the responder form
- `ERP_SUBMIT_BUTTON_TEXT` — preferred button label; `Submit` and `Kirim` are controlled fallbacks

Optional operational settings include `HEADLESS`, IMAP timeout/batch limits, approval token TTL, attachment size limit, and ERP browser timeout. See `.env.example` for every supported setting.

For Gmail, enable two-step verification and generate a Mail App Password. Use the App Password in the local IMAP and SMTP password variables; do not use or commit the normal account password.

### `resources/Config.xlsx`

The public workbook preserves the official `Config_T` sheet and keys:

| Key | Reviewer action |
| --- | --- |
| `ThresholdAmount` | Set the approval threshold required for the test. |
| `FormsUrl` | Replace the placeholder with the responder URL, ending in `/viewform`; never use an editor URL. |
| `ExcelERPPath` | Retained from the assessment template. |
| `ApproverEmail` | Replace the placeholder with the mailbox allowed to approve/reject. |

The public repository deliberately contains placeholders for `FormsUrl` and `ApproverEmail`. `python main.py preflight-check` reports either placeholder as a blocker rather than using an unsafe fallback.

## Google Forms setup

The assessment permits either Microsoft Forms or Google Forms. This implementation deliberately supports **Google Forms only** through Playwright; use a Google Forms responder URL for `FormsUrl`.

Create a responder form with these seven short-answer fields. Labels must match exactly.

1. `PostingRef`
2. `CorrelationId`
3. `InvoiceNumber`
4. `VendorName`
5. `PO_Number`
6. `Amount`
7. `Currency`

Before a live post, use the read-only locator check:

```bash
python main.py erp-form-check
python main.py erp-form-diagnose
```

Both commands open the form but do not populate fields or click Submit/Kirim.

## Quick start: step-by-step workflow

First configure `.env` and `resources/Config.xlsx`, then initialise local folders and SQLite:

```bash
python main.py preflight-check
python main.py init
```

`email-check` verifies SMTP and IMAP authentication without sending, fetching, or changing messages:

```bash
python main.py email-check
```

### 1. Select and intake one Gmail invoice

List a bounded set of unread UID metadata first. This does not fetch message bodies or attachments:

```bash
python main.py email-intake --list-uids --limit 10
```

Then process one deliberately selected UID/UIDVALIDITY pair:

```bash
python main.py email-intake --uid <UID> --uidvalidity <UIDVALIDITY>
```

The IMAP connection is read-only and uses `BODY.PEEK[]`; it does not mark, move, or delete email. For local files instead, place an invoice image in `data/incoming/` and run:

```bash
python main.py intake
```

### 2. OCR, extraction, and PO validation

```bash
python main.py ocr
python main.py po-validate
```

OCR errors are recorded as `OCR_FAILED`. Missing or invalid extracted fields become an auditable PO validation failure rather than an approval/ERP candidate. To re-run regex only, without a Vision request:

```bash
python main.py reextract
```

### 3. Approval

```bash
python main.py approval-initiate
```

Invoices at or below `ThresholdAmount` become `AUTO_APPROVED`. Higher values become `PENDING_APPROVAL` and receive one durable SMTP approval request.

The approver replies using this body format:

```text
APPROVE
Approval Token: <token from approval request>
```

Use `REJECT` instead of `APPROVE` to reject. After the reply arrives, process one bounded read-only batch:

```bash
python main.py approval-read-replies --max-messages 10
```

Sender, token, expiry, and duplicate receipt are validated before a decision is applied. A rejected invoice is never eligible for ERP posting.

### 4. ERP posting, archive, and export

Only `APPROVED` or `AUTO_APPROVED` invoices are eligible:

```bash
python main.py erp-post
python main.py archive-invoices
python main.py export-excel
```

The browser is visible by default (`HEADLESS=false`). If confirmation is not observed after the form button is clicked, the invoice remains `POSTING_UNCERTAIN`; do not run `erp-post` again. Verify exactly one matching Form response manually and use the guarded reconciliation command instead:

```bash
python main.py erp-reconcile --db-path <database-path> --invoice-number <invoice-number> --posting-ref <posting-ref> --confirmed-in-responses --dry-run
```

Remove `--dry-run` only after confirming the matching response.

## Isolated end-to-end run

The project includes an isolated test environment under `data/final-e2e-test/`, which is ignored by Git.

```bash
python main.py final-e2e-prepare
python main.py run-once --dry-run
python main.py run-once --uid <UID> --uidvalidity <UIDVALIDITY>
```

A fresh run processes the explicit UID only. It can pause at `PENDING_APPROVAL`. After the approver reply has been processed with `approval-read-replies`, continue the same isolated environment:

```bash
python main.py run-once --resume
```

`--resume` does not read approval replies itself; that explicit operator step is intentional.

For other isolated databases, commands that support `--db-path` include `email-intake`, `ocr`, `po-validate`, `approval-initiate`, `approval-read-replies`, `erp-post`, `export-excel`, and `archive-invoices`.

## Outputs and folders

| Location | Purpose |
| --- | --- |
| `data/automation.db` | Default SQLite source of truth; ignored by Git. |
| `data/incoming/` | Local intake files / durable email attachments. |
| `data/processed/` | Archive destination: `success`, `rejected`, `validation_failed`, or `review`. |
| `output/Invoices.xlsx` | All invoice records, exported from SQLite. |
| `output/ERP_Dummy.xlsx` | Only invoices with `posting_state = POSTED`. |
| `output/ProcessLogs.xlsx` | Process steps and errors, exported from SQLite. |
| `logs/automation.log` | Local operational log; ignored by Git. |

`PostingRef` is persisted before browser submission and is carried into SQLite, the Form, `Invoices.xlsx`, and `ERP_Dummy.xlsx`.

## Reliability, logging, and recovery

- Unique hashes, IMAP UID/UIDVALIDITY metadata, database constraints, and persistent state prevent duplicate processing.
- Each invoice/attachment is processed independently so a failure does not stop the batch.
- Email delivery uses a persistent outbox. SMTP cannot guarantee exactly-once delivery after an ambiguous network failure, so `SENDING` messages are not resent automatically.
- Excel output is generated from SQLite with temporary-file replacement.
- Archive verifies SHA-256 before moving an image and logs failures.
- Approval and ERP operations retain audit records in `ProcessLogs.xlsx`.
- New database timestamps are stored as UTC ISO-8601 with an explicit `+00:00` offset. Excel and application logs display Asia/Jakarta time as `YYYY-MM-DD HH:mm WIB`; Excel uses native local datetime cells with a `WIB` number-format suffix so sorting remains chronological. Legacy SQLite `CURRENT_TIMESTAMP` values (`YYYY-MM-DD HH:MM:SS`) are interpreted as UTC because SQLite defines that built-in value in UTC. Naive timestamps imported from an unknown external system require manual review.

## Automated tests

Run the strict local suite:

```bash
python -W error::ResourceWarning -m unittest discover -s tests -v
```

Tests use mocks and temporary SQLite/filesystem resources; they do not send email, access Gmail, call Google Vision, or submit a form.

## Verification evidence

Two isolated end-to-end scenarios were completed during development:

| Scenario | Verified outcome |
| --- | --- |
| `INV-E2E-001` | PO matched, auto-approved, ERP posted, archived, and exported. |
| `INV-E2E-003` | PO matched, approval reply validated, ERP posted, archived, and exported. |

These test artifacts are intentionally Git-ignored. Reviewers should execute the documented flow with their own mailbox, form, and API key.

## Submission notes

Before submission:

1. Keep `.env`, databases, generated Excel files, logs, attachment data, local config backups, credentials, and personal mailbox details out of Git.
2. Share a responder-form URL and access to its responses with the reviewer through an appropriate channel.
3. Provide the repository link and a short recording of one full run, as required by the assessment brief.
4. On Windows, run the installation commands above and configure credentials locally before testing with new invoices.
