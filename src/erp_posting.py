"""Google Forms ERP simulation with persistent posting state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PostingError(RuntimeError):
    """Kegagalan yang terbukti terjadi sebelum form disubmit."""


class PostingUncertain(RuntimeError):
    """Kegagalan setelah submit; hasil tidak boleh otomatis diulang."""


class FormPage(Protocol):
    """Kontrak kecil Playwright yang mudah di-mock saat test."""
    def goto(self, url: str, timeout: int) -> object: ...
    def get_by_label(self, label: str, exact: bool = True) -> object: ...
    def get_by_role(self, role: str, name: str) -> object: ...
    def get_by_text(self, text: str, exact: bool = False) -> object: ...


@dataclass(frozen=True)
class FormFields:
    """Label Google Form yang configurable melalui environment."""
    invoice_number: str = "InvoiceNumber"
    vendor_name: str = "VendorName"
    po_number: str = "PO_Number"
    amount: str = "Amount"
    currency: str = "Currency"
    correlation_id: str = "CorrelationId"
    posting_ref: str = "PostingRef"
    confirmation_text: str = "Respons Anda telah direkam"


class GoogleFormsPoster:
    """Adapter browser Playwright; keberhasilan hanya dari confirmation page."""
    def __init__(self, page: FormPage, fields: FormFields, timeout_ms: int = 30000) -> None:
        self.page, self.fields, self.timeout_ms = page, fields, timeout_ms

    def post(self, forms_url: str, invoice: dict[str, object]) -> None:
        """Isi form dan tunggu confirmation; error setelah klik menjadi uncertain."""
        try:
            # Membuka URL dari Config.xlsx dengan timeout yang dibatasi.
            self.page.goto(forms_url, timeout=self.timeout_ms)
            # Mengisi seluruh data yang diperlukan untuk ERP_Dummy dan audit form.
            values = ((self.fields.invoice_number, invoice["invoice_number"]), (self.fields.vendor_name, invoice["vendor_name"]), (self.fields.po_number, invoice["po_number"]), (self.fields.amount, str(invoice["amount"])), (self.fields.currency, invoice["currency"]), (self.fields.correlation_id, invoice["correlation_id"]), (self.fields.posting_ref, invoice["posting_ref"]))
            for label, value in values:
                try:
                    self.page.get_by_label(label, exact=True).fill(str(value))
                except Exception:
                    self.page.get_by_text(label, exact=True).locator("xpath=ancestor::div[@role='listitem']").locator("input").fill(str(value))
        except Exception as error:
            raise PostingError("Form could not be filled before submit") from error
        try:
            # Klik submit hanya sekali; setelah titik ini hasil bisa tidak pasti.
            self.page.get_by_role("button", name="Submit").click()
            # Google Forms harus menampilkan halaman konfirmasi untuk dianggap posted.
            self.page.get_by_text(self.fields.confirmation_text, exact=False).wait_for(timeout=self.timeout_ms)
        except Exception as error:
            raise PostingUncertain("Submission confirmation was not received") from error


def validate_form_fields(page: FormPage, forms_url: str, fields: FormFields, timeout_ms: int = 30000) -> list[str]:
    """Membuka form dan mengembalikan label wajib yang tidak tersedia tanpa mengisi data."""
    # Membuka responder URL saja; fungsi ini tidak mengetik atau menekan Submit.
    page.goto(forms_url, timeout=timeout_ms, wait_until="domcontentloaded")
    required_labels = (fields.invoice_number, fields.vendor_name, fields.po_number, fields.amount, fields.currency, fields.correlation_id, fields.posting_ref)
    missing: list[str] = []
    for label in required_labels:
        try:
            # Locator berbasis label membuat urutan pertanyaan Google Form tidak relevan.
            try:
                page.get_by_label(label, exact=True).wait_for(timeout=timeout_ms)
            except Exception:
                page.get_by_text(label, exact=True).locator("xpath=ancestor::div[@role='listitem']").locator("input").wait_for(timeout=timeout_ms)
        except Exception:
            missing.append(label)
    return missing
