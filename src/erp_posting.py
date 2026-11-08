"""Google Forms ERP simulation with persistent posting state."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import time
from typing import Protocol
from urllib.parse import urlparse


class PostingError(RuntimeError):
    """Kegagalan yang terbukti terjadi sebelum form disubmit."""


class PostingUncertain(RuntimeError):
    """Kegagalan setelah submit; hasil tidak boleh otomatis diulang."""


class ConfirmationTimeout(TimeoutError):
    """Timeout dengan metadata diagnostik aman untuk observasi pasca-click."""
    def __init__(self, last_url_path: str, navigation_observed: bool, elapsed_ms: int) -> None:
        super().__init__("Google Forms confirmation evidence was not found")
        self.last_url_path = last_url_path
        self.navigation_observed = navigation_observed
        self.elapsed_ms = elapsed_ms


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
    submit_button_text: str = "Submit"
    confirmation_text: str = "Respons Anda telah direkam"

    @classmethod
    def from_environment(cls) -> "FormFields":
        """Membaca teks konfirmasi tanpa menjadikannya aturan bisnis hardcode."""
        # Google Forms hanya dapat dinyatakan POSTED setelah teks ini terlihat.
        # Nilai default dipertahankan demi kompatibilitas instalasi sebelumnya.
        confirmation_text = os.getenv("ERP_CONFIRMATION_TEXT", cls.confirmation_text).strip()
        submit_button_text = os.getenv("ERP_SUBMIT_BUTTON_TEXT", cls.submit_button_text).strip()
        if not confirmation_text:
            raise PostingError("ERP_CONFIRMATION_TEXT must not be empty")
        if not submit_button_text:
            raise PostingError("ERP_SUBMIT_BUTTON_TEXT must not be empty")
        return cls(confirmation_text=confirmation_text, submit_button_text=submit_button_text)


@dataclass(frozen=True)
class FormFieldDiagnostic:
    """Hasil diagnosis aman per field tanpa membaca atau mengisi nilai form."""
    label: str
    found: bool
    locator_count: int
    visible: bool | None
    enabled: bool | None
    strategy: str
    error_category: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class SubmitButtonDiagnostic:
    """Status tombol submit tanpa click atau perubahan Google Forms."""
    configured_label: str
    matched_label: str | None
    locator_count: int
    visible: bool | None
    enabled: bool | None
    error_category: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class ConfirmationEvidence:
    """Bukti success page yang aman untuk log, tanpa isi form atau credential."""
    kind: str
    url_path: str
    navigation_observed: bool
    elapsed_ms: int


def validate_responder_form_url(forms_url: str) -> None:
    """Memastikan URL mengarah ke halaman responder, bukan editor Google Forms."""
    parsed = urlparse(forms_url)
    # Query seperti ``usp=publish-editor`` bukan endpoint editor; yang menentukan adalah path.
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise PostingError("FormsUrl must be an absolute HTTP(S) URL")
    path = parsed.path.rstrip("/")
    if not path.endswith("/viewform") or "/forms/" not in path or "/edit" in path:
        raise PostingError("FormsUrl must use the Google Forms responder /viewform URL")


class GoogleFormsPoster:
    """Adapter browser Playwright; keberhasilan hanya dari confirmation page."""
    def __init__(self, page: FormPage, fields: FormFields, timeout_ms: int = 30000, logger: logging.Logger | None = None) -> None:
        self.page, self.fields, self.timeout_ms = page, fields, timeout_ms
        self.logger = logger or logging.getLogger(__name__)

    def post(self, forms_url: str, invoice: dict[str, object]) -> None:
        """Isi form dan tunggu confirmation; error setelah klik menjadi uncertain."""
        try:
            # Membuka URL dari Config.xlsx dengan timeout yang dibatasi.
            self.page.goto(forms_url, timeout=self.timeout_ms)
            self.logger.info("erp_form_event=form_opened")
            # Mengisi seluruh data yang diperlukan untuk ERP_Dummy dan audit form.
            values = ((self.fields.invoice_number, invoice["invoice_number"]), (self.fields.vendor_name, invoice["vendor_name"]), (self.fields.po_number, invoice["po_number"]), (self.fields.amount, str(invoice["amount"])), (self.fields.currency, invoice["currency"]), (self.fields.correlation_id, invoice["correlation_id"]), (self.fields.posting_ref, invoice["posting_ref"]))
            for label, value in values:
                # Google Forms saat ini tidak mengasosiasikan teks pertanyaan dengan
                # elemen HTML label. Cek count dulu agar tidak menghabiskan timeout
                # penuh pada get_by_label() sebelum memakai locator pertanyaan.
                _field_locator(self.page, label).fill(str(value), timeout=self.timeout_ms)
            self.logger.info("erp_form_event=fields_filled")
        except Exception as error:
            raise PostingError("Form could not be filled before submit") from error
        try:
            # Klik submit hanya sekali; setelah titik ini hasil bisa tidak pasti.
            submit_button, submit_label = _submit_button_locator(self.page, self.fields)
            self.logger.info("erp_form_event=submit_located label=%s", submit_label)
            url_before = _safe_url_path(self.page)
            self.logger.info("erp_form_event=click_attempted")
            # Google Forms kadang mempertahankan koneksi halaman sesudah click. Hindari
            # timeout navigasi implicit; hasil click tetap diverifikasi secara eksplisit.
            submit_button.click(timeout=self.timeout_ms, no_wait_after=True)
            self.logger.info("erp_form_event=click_completed url_before=%s url_after=%s", url_before, _safe_url_path(self.page))
            evidence = _wait_for_confirmation(self.page, self.fields, self.timeout_ms)
            self.logger.info(
                "erp_form_event=confirmation_detected evidence=%s url_before=%s url_after=%s navigation_observed=%s elapsed_ms=%s",
                evidence.kind, url_before, evidence.url_path, evidence.navigation_observed, evidence.elapsed_ms,
            )
        except Exception as error:
            timeout_details = error if isinstance(error, ConfirmationTimeout) else None
            self.logger.warning(
                "erp_form_event=confirmation_not_detected url_before=%s url_after=%s navigation_observed=%s elapsed_ms=%s confirmation_url_detected=%s confirmation_text_detected=%s error_type=%s",
                locals().get("url_before", "unavailable"),
                timeout_details.last_url_path if timeout_details else _safe_url_path(self.page),
                timeout_details.navigation_observed if timeout_details else False,
                timeout_details.elapsed_ms if timeout_details else 0,
                False,
                False,
                type(error).__name__,
            )
            raise PostingUncertain("Submission confirmation was not received") from error


def validate_form_fields(page: FormPage, forms_url: str, fields: FormFields, timeout_ms: int = 30000) -> list[str]:
    """Membuka form dan mengembalikan label wajib yang tidak tersedia tanpa mengisi data."""
    # Membuka responder URL saja; fungsi ini tidak mengetik atau menekan Submit.
    # Batas default juga melindungi locator fallback yang dipanggil berulang per field.
    if hasattr(page, "set_default_timeout"):
        page.set_default_timeout(timeout_ms)
    page.goto(forms_url, timeout=timeout_ms, wait_until="domcontentloaded")
    required_labels = (fields.invoice_number, fields.vendor_name, fields.po_number, fields.amount, fields.currency, fields.correlation_id, fields.posting_ref)
    missing: list[str] = []
    for label in required_labels:
        try:
            # Locator berbasis label membuat urutan pertanyaan Google Form tidak relevan.
            try:
                labelled = page.get_by_label(label, exact=True)
                # count() tidak menunggu timeout per field, sehingga form-check tetap bounded
                # ketika salah satu label tidak tersedia.
                if hasattr(labelled, "count"):
                    if labelled.count() < 1:
                        raise LookupError(label)
                else:
                    labelled.wait_for(timeout=timeout_ms)
            except Exception:
                text = page.get_by_text(label, exact=True)
                if hasattr(text, "count"):
                    if text.count() < 1:
                        raise LookupError(label)
                else:
                    text.locator("xpath=ancestor::div[@role='listitem']").locator("input").wait_for(timeout=timeout_ms)
        except Exception:
            missing.append(label)
    return missing


def _field_locator(page: FormPage, label: str):
    """Memilih input Google Forms tanpa mengandalkan urutan pertanyaan."""
    labelled = page.get_by_label(label, exact=True)
    if hasattr(labelled, "count") and labelled.count() > 0:
        return labelled.first
    # Struktur responder Google Forms menempatkan teks pertanyaan dan input di listitem.
    question = page.get_by_text(label, exact=True)
    if hasattr(question, "count") and question.count() < 1:
        raise LookupError(f"Required Google Form field not found: {label}")
    item = question.first.locator("xpath=ancestor::div[@role='listitem']")
    inputs = item.locator("input, textarea")
    if hasattr(inputs, "count") and inputs.count() < 1:
        raise LookupError(f"Google Form field has no input: {label}")
    return inputs.first


def _submit_button_locator(page: FormPage, fields: FormFields):
    """Memilih tombol submit sesuai konfigurasi lalu fallback lokal yang eksplisit."""
    # Fallback dibatasi pada label Google Forms yang didukung; bukan pencarian tombol umum.
    for label in dict.fromkeys((fields.submit_button_text, "Submit", "Kirim")):
        candidate = page.get_by_role("button", name=label, exact=True)
        count = candidate.count() if hasattr(candidate, "count") else 1
        if count == 1:
            return candidate.first if hasattr(candidate, "first") else candidate, label
        if count > 1:
            raise LookupError(f"Ambiguous Google Forms submit button: {label}")
    raise LookupError("Google Forms submit button was not found")


def _confirmation_candidates(fields: FormFields) -> tuple[str, ...]:
    """Mengembalikan frasa sukses Google Forms yang didukung tanpa fuzzy match."""
    # Konfigurasi operator diutamakan; fallback hanya frasa standar yang spesifik.
    return tuple(dict.fromkeys((
        fields.confirmation_text,
        "Your response has been recorded",
        "Respons Anda telah direkam",
        "Tanggapan Anda telah direkam",
    )))


def _safe_url_path(surface: object) -> str:
    """Menyimpan path URL saja agar query parameter tidak masuk structured log."""
    try:
        return urlparse(str(getattr(surface, "url", ""))).path or "unavailable"
    except Exception:
        return "unavailable"


def _confirmation_surfaces(page: FormPage) -> list[object]:
    """Mengambil page utama, popup context, dan frame untuk diagnosis tanpa network assertion."""
    surfaces: list[object] = [page]
    try:
        context_pages = list(getattr(getattr(page, "context"), "pages"))
    except Exception:
        context_pages = []
    for candidate in context_pages:
        if candidate not in surfaces:
            surfaces.append(candidate)
    for candidate in list(surfaces):
        try:
            for frame in list(getattr(candidate, "frames")):
                if frame not in surfaces:
                    surfaces.append(frame)
        except Exception:
            continue
    return surfaces


def _surface_confirmation(surface: object, candidates: tuple[str, ...]) -> tuple[str, str] | None:
    """Mencari bukti URL atau DOM yang terlihat pada satu page/frame saat ini."""
    path = _safe_url_path(surface)
    if "/formResponse" in path:
        return "response_url", path
    try:
        text = str(surface.locator("body").inner_text(timeout=250)).casefold()
        if any(candidate.casefold() in text for candidate in candidates):
            return "confirmation_text", path
    except Exception:
        # Navigasi dapat menghancurkan context sementara; polling berikutnya akan memakai context baru.
        pass
    return None


def _wait_for_confirmation(page: FormPage, fields: FormFields, timeout_ms: int) -> ConfirmationEvidence:
    """Polling ulang page/frame sampai bukti kuat terlihat atau batas waktu habis."""
    candidates = _confirmation_candidates(fields)
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    initial_path = _safe_url_path(page)
    last_path = initial_path
    while time.monotonic() < deadline:
        for surface in _confirmation_surfaces(page):
            result = _surface_confirmation(surface, candidates)
            current_path = _safe_url_path(surface)
            if current_path != "unavailable":
                last_path = current_path
            if result:
                kind, path = result
                return ConfirmationEvidence(kind, path, path != initial_path, int((time.monotonic() - started) * 1000))
        # wait_for_timeout memungkinkan Playwright memproses navigation/popup; polling
        # tidak menganggap event network atau HTTP status sebagai bukti sukses.
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        if hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(min(200, remaining_ms))
        else:
            time.sleep(min(0.05, remaining_ms / 1000))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    raise ConfirmationTimeout(last_path, last_path != initial_path, elapsed_ms)


def diagnose_submit_button(page: FormPage, fields: FormFields) -> SubmitButtonDiagnostic:
    """Memeriksa kandidat tombol submit secara read-only tanpa click."""
    try:
        button, label = _submit_button_locator(page, fields)
        visible, enabled = button.is_visible(), button.is_enabled()
        return SubmitButtonDiagnostic(
            fields.submit_button_text, label, 1, visible, enabled,
            None if visible and enabled else ("NOT_VISIBLE" if not visible else "DISABLED"),
        )
    except Exception as error:
        category = "AMBIGUOUS" if "Ambiguous" in str(error) else "NOT_FOUND"
        return SubmitButtonDiagnostic(fields.submit_button_text, None, 0, None, None, category, type(error).__name__)


def diagnose_form_fields(page: FormPage, fields: FormFields) -> list[FormFieldDiagnostic]:
    """Memeriksa locator field tanpa fill atau Submit untuk investigasi aman."""
    labels = (fields.invoice_number, fields.vendor_name, fields.po_number, fields.amount,
              fields.currency, fields.correlation_id, fields.posting_ref)
    diagnostics: list[FormFieldDiagnostic] = []
    for label in labels:
        try:
            labelled = page.get_by_label(label, exact=True)
            label_count = labelled.count() if hasattr(labelled, "count") else 0
            if label_count:
                target, strategy, count = labelled.first, "label", label_count
            else:
                question = page.get_by_text(label, exact=True)
                text_count = question.count() if hasattr(question, "count") else 0
                if not text_count:
                    diagnostics.append(FormFieldDiagnostic(label, False, 0, None, None, "none", "NOT_FOUND"))
                    continue
                inputs = question.first.locator("xpath=ancestor::div[@role='listitem']").locator("input, textarea")
                count = inputs.count() if hasattr(inputs, "count") else 0
                if not count:
                    diagnostics.append(FormFieldDiagnostic(label, False, 0, None, None, "question_listitem_input", "INPUT_NOT_FOUND"))
                    continue
                target, strategy = inputs.first, "question_listitem_input"
            visible = target.is_visible()
            enabled = target.is_enabled()
            category = None if visible and enabled else ("NOT_VISIBLE" if not visible else "DISABLED")
            diagnostics.append(FormFieldDiagnostic(label, True, count, visible, enabled, strategy, category))
        except Exception as error:
            # Hanya tipe error ditampilkan; body form dan nilai field tidak pernah dibaca.
            diagnostics.append(FormFieldDiagnostic(label, False, 0, None, None, "error", "LOCATOR_ERROR", type(error).__name__))
    return diagnostics
