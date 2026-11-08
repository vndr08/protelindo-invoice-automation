# Protelindo Invoice Automation

Python automation for the Protelindo Junior Automation Developer technical assessment. It processes invoice images from a mailbox through Google Cloud Vision OCR, regex extraction, SQLite, purchase-order validation, email approval, Google Forms ERP simulation, Excel export, and archive.

The reviewer supplies their own mailbox, Google Cloud Vision API key, Google Forms responder URL, approver address, and invoice images. No LLM is used for extraction.

## What is implemented

| Assessment area | Implementation |
| --- | --- |
| Email intake | Read-only IMAP intake using UID/UIDVALIDITY and `BODY.PEEK[]`; a local-folder intake alternative is also available. |
| OCR and extraction | Google Vision `TEXT_DETECTION` plus deterministic regex for InvoiceNumber, VendorName, PO_Number, Amount, and Currency. |
| Persistence | SQLite is the source of truth for invoice, validation, approval, posting, archive, and process-log state. |
| PO validation | Normalized comparison with `resources/PurchaseOrders.csv`. |
| Approval | Threshold and approver are read from `resources/Config.xlsx`; SMTP delivery uses a persistent outbox and IMAP replies are validated. |
| ERP simulation | Playwright fills a Google Forms responder form with a persistent PostingRef. |
| Output | `Invoices.xlsx`, `ERP_Dummy.xlsx`, `ProcessLogs.xlsx`, plus hash-verified invoice archiving. |

## Workflow

```text
IMAP invoice image / local image
  -> Google Vision OCR
  -> regex extraction
  -> SQLite
  -> PO validation
  -> auto approval OR email approval
  -> Google Forms via Playwright
  -> archive
  -> Excel exports
```

The workflow is state-based and idempotent. Re-running normal stages does not deliberately create duplicate invoice records, approval requests, attachments, or ERP submissions. A Google Forms submission without strong confirmation becomes `POSTING_UNCERTAIN` and is never retried automatically.

## Prerequisites

- Python 3.11 or newer
- A mailbox you control with IMAP and SMTP enabled
- Google Cloud Vision API key
- Google Forms responder URL
- Chromium for Playwright

Only JPG, JPEG, and PNG are supported end-to-end for email attachment OCR. PDF intake is not part of the current end-to-end OCR flow.

## Windows PowerShell setup

Run the following from a directory where you keep projects. Replace `<repository-url>` with the repository URL supplied for review.

```powershell
git clone <repository-url>
Set-Location PROTELINDO

py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium

Copy-Item .env.example .env
```

If PowerShell blocks activation for the current terminal session, use this process-scoped setting, then activate again. It does not change the machine-wide policy.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Next, fill `.env` and `resources/Config.xlsx` as described below. Then initialise SQLite and run local preflight checks in this order:

```powershell
python main.py init
python main.py preflight-check
python main.py email-check
```

`init` creates runtime folders and the default SQLite database. `preflight-check` expects this database to exist, so it must run after `init`. Configure `.env` and `resources/Config.xlsx` before expecting preflight to pass. `email-check` performs SMTP authentication/NOOP and IMAP read-only connectivity only; it does not send or fetch invoice email.

## macOS and Linux setup

```bash
git clone <repository-url>
cd PROTELINDO

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env

python main.py init
python main.py preflight-check
```

## Configuration

### `.env`

Copy `.env.example` to `.env`. It contains local credentials and must never be committed. The public repository deliberately contains empty/reviewer-safe values.

| Variable | Purpose |
| --- | --- |
| `GOOGLE_VISION_API_KEY` | API key used by Google Vision OCR. |
| `IMAP_HOST`, `IMAP_PORT` | Invoice mailbox IMAP endpoint; Gmail normally uses port `993`. |
| `IMAP_USERNAME`, `IMAP_PASSWORD` | Invoice mailbox login. For Gmail, use an App Password rather than the normal account password. |
| `IMAP_INTAKE_MAILBOX` | Mailbox for invoice intake; defaults to `INBOX`. |
| `IMAP_INTAKE_TIMEOUT_SECONDS` | IMAP intake connection timeout. |
| `IMAP_ATTACHMENT_MAX_BYTES` | Maximum allowed invoice attachment size. |
| `APPROVAL_IMAP_MAX_MESSAGES` | Bounded number of candidate approval replies processed in one run. |
| `APPROVAL_IMAP_TIMEOUT_SECONDS` | Approval-reply IMAP timeout. |
| `SMTP_HOST`, `SMTP_PORT` | SMTP endpoint for approval requests; Gmail normally uses port `587`. |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | SMTP login for the robot mailbox. |
| `APPROVAL_TOKEN_TTL_HOURS` | Approval-token lifetime, from 1 to 168 hours. |
| `HEADLESS` | Set `false` to show the Playwright browser during ERP posting. |
| `ERP_BROWSER_TIMEOUT_MS` | Browser timeout for Google Forms operations. |
| `ERP_CONFIRMATION_TEXT` | Exact success text shown by the Google Forms confirmation page. |
| `ERP_SUBMIT_BUTTON_TEXT` | Preferred submit label. The ERP locator has controlled `Submit` and `Kirim` fallbacks. |

### `resources/Config.xlsx`

Keep the `Config_T` sheet and its existing structure. Reviewers must replace the public placeholders with their own operational values.

| Key | Reviewer action |
| --- | --- |
| `ThresholdAmount` | Amount at or below this value is auto-approved. |
| `FormsUrl` | Use the Google Forms **responder** URL ending in `/viewform`, never the editing URL. |
| `ExcelERPPath` | Retained from the assessment template. |
| `ApproverEmail` | Mailbox permitted to reply `APPROVE` or `REJECT`. |

`preflight-check` reports placeholder FormsUrl or ApproverEmail values as blockers. It is local and read-only; it does not test credentials or contact external services.

## Google Forms setup

The assessment permits Microsoft Forms or Google Forms. This implementation supports **Google Forms only**, through Playwright.

Create seven short-answer fields with labels that match exactly:

1. `PostingRef`
2. `CorrelationId`
3. `InvoiceNumber`
4. `VendorName`
5. `PO_Number`
6. `Amount`
7. `Currency`

Before live posting, validate the responder page without filling or submitting it:

```powershell
python main.py erp-form-check
python main.py erp-form-diagnose
```

## Individual CLI workflow

The following commands use the default database, `data/automation.db`.

### 1. Locate and intake one invoice email

List a bounded set of unread UID/UIDVALIDITY pairs. This does not fetch message bodies or attachments:

```powershell
python main.py email-intake --list-uids --limit 10
```

Process only a deliberately selected message:

```powershell
python main.py email-intake --uid <UID> --uidvalidity <UIDVALIDITY>
```

IMAP runs read-only and uses `BODY.PEEK[]`; it does not mark, move, or delete messages. For a local image instead, place it in `data/incoming/` and run:

```powershell
python main.py intake
```

### 2. OCR, extraction, and PO validation

```powershell
python main.py ocr
python main.py po-validate
```

OCR uses Google Vision and then regex extraction. Missing or invalid extracted fields result in an auditable PO validation failure; the system does not infer values from the PO file. To apply updated regex to already stored OCR text without a Vision API call:

```powershell
python main.py reextract
```

### 3. Approval

```powershell
python main.py approval-initiate
```

- Amount at or below `ThresholdAmount`: `AUTO_APPROVED`.
- Amount above `ThresholdAmount`: `PENDING_APPROVAL` and one durable approval request is queued/sent.

The configured approver replies with one of the following bodies, using the token received in the approval request:

```text
APPROVE
Approval Token: <approval token>
```

```text
REJECT
Approval Token: <approval token>
```

Process a finite, read-only reply batch:

```powershell
python main.py approval-read-replies --max-messages 10
```

Sender, token, expiry, and duplicate receipt are validated before a decision is applied. Rejected invoices cannot proceed to ERP posting.

### 4. ERP posting, archive, and Excel export

Only invoices with valid `APPROVED` or `AUTO_APPROVED` approval state are eligible for ERP posting.

```powershell
python main.py erp-post
python main.py archive-invoices
python main.py export-excel
```

Verify a successful post by checking that the same `PostingRef` appears in:

- the Google Forms Responses sheet;
- `Invoices.xlsx`;
- `ERP_Dummy.xlsx`.

If browser submission was clicked but confirmation is not detected, the invoice becomes `POSTING_UNCERTAIN`. Do **not** run `erp-post` again. First verify that exactly one Google Forms response matches both invoice data and PostingRef, then use guarded reconciliation:

```powershell
python main.py erp-reconcile --db-path <database-path> --invoice-number <invoice-number> --posting-ref <posting-ref> --confirmed-in-responses --dry-run
```

Remove `--dry-run` only after manual response verification. This command never opens a browser or submits a form.

## Isolated `run-once` workflow

`run-once` is an orchestration option for an isolated environment under `data/final-e2e-test/`. A fresh run requires one explicit UID and UIDVALIDITY; it does not scan the entire mailbox.

```powershell
python main.py final-e2e-prepare
python main.py run-once --dry-run
python main.py run-once --uid <UID> --uidvalidity <UIDVALIDITY>
```

The fresh run executes intake, OCR, PO validation, approval initiation, eligible ERP posting, archive, and export. A first run can complete with an invoice still in `PENDING_APPROVAL`.

After the approver replies, process that reply against the same isolated database, then resume:

```powershell
python main.py approval-read-replies --db-path data/final-e2e-test/automation.db --max-messages 10
python main.py run-once --resume
```

`run-once --resume` deliberately does not read email replies itself. It resumes OCR/PO/approval state checks, ERP, archive, and export after the explicit approval-reader step.

For an alternate isolated directory, provide matching values to `--db-path`, `--storage-dir`, `--output-dir`, and `--archive-dir` on `final-e2e-prepare` and `run-once`. All must remain under `data/final-e2e-test/`.

## Outputs

| Location | Content |
| --- | --- |
| `data/automation.db` | Default SQLite source of truth. |
| `data/incoming/` | Local intake files and durable email attachments. |
| `data/processed/` | Archived files in `success`, `rejected`, `validation_failed`, or `review`. |
| `output/Invoices.xlsx` | All invoice records. |
| `output/ERP_Dummy.xlsx` | Only invoices with `posting_state = POSTED`. |
| `output/ProcessLogs.xlsx` | Process steps and errors. |
| `logs/automation.log` | Local application log. |

New timestamps are stored in SQLite as UTC ISO-8601 with an explicit offset. Terminal logs and exported Excel datetime cells display Asia/Jakarta time as:

```text
YYYY-MM-DD HH:mm:ss WIB
```

Excel uses native datetime cells plus a `WIB` number format, so time columns remain sortable and filterable.

## Reliability and recovery

- SHA-256 hashes, IMAP UID/UIDVALIDITY metadata, unique database constraints, and state guards prevent duplicate processing.
- A failure for one attachment or invoice does not stop the entire batch.
- SMTP uses a persistent outbox. SMTP cannot guarantee exactly-once delivery after an ambiguous network failure, so `SENDING` messages are not automatically resent.
- Excel files are written through a temporary file and atomic replacement.
- Archive verifies the invoice hash before moving it and records archive results in ProcessLogs.
- `POSTING_UNCERTAIN` requires manual Google Forms reconciliation; it is never automatically retried.

## Troubleshooting

| Symptom | Supported action |
| --- | --- |
| Preflight reports database not initialized | Run `python main.py init`, then rerun `python main.py preflight-check`. |
| Preflight reports missing or placeholder configuration | Fill `.env` and replace reviewer-safe `FormsUrl` / `ApproverEmail` placeholders in `resources/Config.xlsx`. |
| `email-check` fails | Verify IMAP/SMTP host, port, username, and App Password in `.env`. It does not send email. |
| OCR reports missing API key | Add `GOOGLE_VISION_API_KEY` to `.env`. |
| ERP form check reports missing labels | Correct the Google Forms responder form so all seven labels match exactly. |
| Playwright cannot launch Chromium | Run `python -m playwright install chromium` in the active virtual environment. |
| Invoice remains `PENDING_APPROVAL` | Send a correctly formatted APPROVE/REJECT reply from the configured approver, then run `approval-read-replies`. |
| Invoice is `POSTING_UNCERTAIN` | Do not retry ERP posting. Verify exactly one matching response and use `erp-reconcile` with its required flags. |

## Automated tests

Run the strict local suite:

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -v
```

The test suite uses mocks and temporary SQLite/filesystem resources. It does not access Gmail, call Google Vision, send email, or submit Google Forms.

## Verification evidence

Two isolated end-to-end scenarios were completed during development:

| Scenario | Verified outcome |
| --- | --- |
| `INV-E2E-001` | PO matched, auto-approved, ERP posted, archived, and exported. |
| `INV-E2E-003` | PO matched, approval reply validated, ERP posted, archived, and exported. |

Artifacts are intentionally Git-ignored. Reviewers should run the documented flow with their own inbox, form, API key, and invoices.

## Submission notes

- Do not commit `.env`, API keys, mailbox passwords, approval tokens, databases, generated Excel files, logs, attachments, or local configuration backups.
- Share a responder-form URL and access to its responses with the reviewer through an appropriate channel.
- Provide the repository link, a form link, and a short screen recording of one full run as required by the assessment.
