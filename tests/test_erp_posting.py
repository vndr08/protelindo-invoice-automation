"""Test Google Forms adapter memakai page mock tanpa browser sungguhan."""
from unittest.mock import Mock, patch
import unittest
from src.erp_posting import FormFields, GoogleFormsPoster, PostingError, PostingUncertain, diagnose_form_fields, diagnose_submit_button, validate_form_fields, validate_responder_form_url

class Element:
    @property
    def first(self): return self
    def fill(self, value, **kwargs): self.value = value
    def click(self, **kwargs): pass
    def wait_for(self, timeout): pass
    def count(self): return 1
    def is_visible(self): return True
    def is_enabled(self): return True
    def locator(self, selector): return self
    def inner_text(self, **kwargs): return "Respons Anda telah direkam"
class Page:
    url = "https://docs.google.com/forms/d/e/id/viewform"
    def goto(self, url, timeout, **kwargs): pass
    def set_default_timeout(self, timeout): pass
    def get_by_label(self, label, exact=True): return Element()
    def get_by_role(self, role, name, exact=True): return Element()
    def get_by_text(self, text, exact=False): return Element()
    def locator(self, selector): return Element()

class ErpPostingTests(unittest.TestCase):
    def test_posts_after_confirmation(self):
        invoice={"invoice_number":"I","vendor_name":"V","po_number":"P","amount":"1","currency":"IDR","correlation_id":"c","posting_ref":"POST-1"}
        GoogleFormsPoster(Page(), FormFields(), 100).post("https://example.test/form", invoice)
    def test_timeout_after_submit_is_uncertain(self):
        class TimeoutPage(Page):
            def get_by_text(self, text, exact=False):
                class E:
                    def wait_for(self, timeout): raise TimeoutError()
                return E()
            def locator(self, selector):
                class EmptyBody:
                    def inner_text(self, **kwargs): return ""
                return EmptyBody()
        invoice={"invoice_number":"I","vendor_name":"V","po_number":"P","amount":"1","currency":"IDR","correlation_id":"c","posting_ref":"POST-1"}
        with self.assertRaises(PostingUncertain): GoogleFormsPoster(TimeoutPage(), FormFields(), 100).post("https://example.test/form", invoice)

    def test_response_url_confirmation_marks_post_successfully(self):
        class ResponsePage(Page):
            url = "https://docs.google.com/forms/d/e/id/formResponse"
        invoice={"invoice_number":"I","vendor_name":"V","po_number":"P","amount":"1","currency":"IDR","correlation_id":"c","posting_ref":"POST-1"}
        GoogleFormsPoster(ResponsePage(), FormFields(), 100).post("https://example.test/form", invoice)

    def test_confirmation_timeout_after_click_is_uncertain(self):
        class TimeoutHandlePage(Page):
            def wait_for_timeout(self, timeout): pass
            def locator(self, selector):
                class EmptyBody:
                    def inner_text(self, **kwargs): return ""
                return EmptyBody()
        invoice={"invoice_number":"I","vendor_name":"V","po_number":"P","amount":"1","currency":"IDR","correlation_id":"c","posting_ref":"POST-1"}
        with self.assertRaises(PostingUncertain):
            GoogleFormsPoster(TimeoutHandlePage(), FormFields(), 100).post("https://example.test/form", invoice)

    def test_timeout_log_contains_safe_navigation_diagnostics(self):
        class TimeoutPage(Page):
            def locator(self, selector):
                class EmptyBody:
                    def inner_text(self, **kwargs): return ""
                return EmptyBody()
            def wait_for_timeout(self, timeout): pass
        logger = Mock()
        invoice={"invoice_number":"I","vendor_name":"V","po_number":"P","amount":"1","currency":"IDR","correlation_id":"c","posting_ref":"POST-1"}
        with self.assertRaises(PostingUncertain):
            GoogleFormsPoster(TimeoutPage(), FormFields(), 20, logger).post("https://example.test/form", invoice)
        self.assertIn("confirmation_not_detected", logger.warning.call_args.args[0])

    def test_delayed_navigation_is_polled_until_response_url(self):
        class DelayedNavigationPage(Page):
            url = "https://docs.google.com/forms/d/e/id/viewform"
            ticks = 0
            def locator(self, selector):
                class EmptyBody:
                    def inner_text(self, **kwargs): return ""
                return EmptyBody()
            def wait_for_timeout(self, timeout):
                self.ticks += 1
                if self.ticks == 1:
                    self.url = "https://docs.google.com/forms/d/e/id/formResponse"
        invoice={"invoice_number":"I","vendor_name":"V","po_number":"P","amount":"1","currency":"IDR","correlation_id":"c","posting_ref":"POST-1"}
        GoogleFormsPoster(DelayedNavigationPage(), FormFields(), 100).post("https://example.test/form", invoice)

    def test_delayed_confirmation_dom_is_polled(self):
        class Body:
            def __init__(self, page): self.page = page
            def inner_text(self, timeout): return "Respons Anda telah direkam" if self.page.ticks else ""
        class DelayedDomPage(Page):
            url = "https://docs.google.com/forms/d/e/id/viewform"
            ticks = 0
            def locator(self, selector): return Body(self)
            def wait_for_timeout(self, timeout): self.ticks += 1
        invoice={"invoice_number":"I","vendor_name":"V","po_number":"P","amount":"1","currency":"IDR","correlation_id":"c","posting_ref":"POST-1"}
        GoogleFormsPoster(DelayedDomPage(), FormFields(), 100).post("https://example.test/form", invoice)

    def test_reports_missing_label(self):
        class MissingPage(Page):
            def get_by_label(self, label, exact=True):
                if label == "Currency": raise LookupError()
                return Element()
            def get_by_text(self, text, exact=False):
                if text == "Currency": raise LookupError()
                return Element()
        self.assertEqual(validate_form_fields(MissingPage(), "https://example.test/form", FormFields(), 100), ["Currency"])

    def test_accepts_responder_url_even_with_publish_editor_query(self):
        validate_responder_form_url("https://docs.google.com/forms/d/e/form-id/viewform?usp=publish-editor")

    def test_rejects_google_form_editor_url(self):
        with self.assertRaises(PostingError):
            validate_responder_form_url("https://docs.google.com/forms/d/form-id/edit")

    def test_confirmation_text_can_be_configured_from_environment(self):
        with patch.dict("os.environ", {"ERP_CONFIRMATION_TEXT": "Recorded"}):
            self.assertEqual(FormFields.from_environment().confirmation_text, "Recorded")

    def test_google_forms_fallback_skips_missing_html_label_without_timeout(self):
        class NoHtmlLabelPage(Page):
            def get_by_label(self, label, exact=True):
                class Empty(Element):
                    def count(self): return 0
                return Empty()
        invoice={"invoice_number":"I","vendor_name":"V","po_number":"P","amount":"1","currency":"IDR","correlation_id":"c","posting_ref":"POST-1"}
        GoogleFormsPoster(NoHtmlLabelPage(), FormFields(), 100).post("https://example.test/form", invoice)

    def test_diagnostic_reports_fallback_and_enabled_field(self):
        class NoHtmlLabelPage(Page):
            def get_by_label(self, label, exact=True):
                class Empty(Element):
                    def count(self): return 0
                return Empty()
        result=diagnose_form_fields(NoHtmlLabelPage(), FormFields())
        self.assertEqual(len(result), 7)
        self.assertTrue(all(item.strategy == "question_listitem_input" and item.enabled for item in result))

    def test_submit_locator_uses_kirim_when_configured_submit_is_absent(self):
        class IndonesianPage(Page):
            def get_by_role(self, role, name, exact=True):
                class Empty(Element):
                    def count(self): return 0
                return Element() if name == "Kirim" else Empty()
        diagnostic = diagnose_submit_button(IndonesianPage(), FormFields())
        self.assertEqual(diagnostic.matched_label, "Kirim")
        self.assertTrue(diagnostic.enabled)

    def test_submit_locator_uses_exact_english_submit_when_available(self):
        diagnostic = diagnose_submit_button(Page(), FormFields())
        self.assertEqual(diagnostic.matched_label, "Submit")

    def test_ambiguous_submit_locator_is_not_clicked(self):
        class Ambiguous(Element):
            def __init__(self): self.click_calls = 0
            def count(self): return 2
            def click(self, **kwargs): self.click_calls += 1
        button = Ambiguous()
        class AmbiguousPage(Page):
            def get_by_role(self, role, name, exact=True): return button
        diagnostic = diagnose_submit_button(AmbiguousPage(), FormFields())
        self.assertEqual(diagnostic.error_category, "AMBIGUOUS")
        self.assertEqual(button.click_calls, 0)

    def test_submit_not_found_becomes_uncertain_without_second_click(self):
        class NoSubmitPage(Page):
            def get_by_role(self, role, name, exact=True):
                class Empty(Element):
                    def count(self): return 0
                return Empty()
        invoice={"invoice_number":"I","vendor_name":"V","po_number":"P","amount":"1","currency":"IDR","correlation_id":"c","posting_ref":"POST-1"}
        with self.assertRaises(PostingUncertain):
            GoogleFormsPoster(NoSubmitPage(), FormFields(), 100).post("https://example.test/form", invoice)

    def test_post_logs_each_submission_stage(self):
        logger = Mock()
        invoice={"invoice_number":"I","vendor_name":"V","po_number":"P","amount":"1","currency":"IDR","correlation_id":"c","posting_ref":"POST-1"}
        GoogleFormsPoster(Page(), FormFields(), 100, logger).post("https://example.test/form", invoice)
        messages = [call.args[0] for call in logger.info.call_args_list]
        self.assertEqual(messages, ["erp_form_event=form_opened", "erp_form_event=fields_filled", "erp_form_event=submit_located label=%s", "erp_form_event=click_attempted", "erp_form_event=click_completed url_before=%s url_after=%s", "erp_form_event=confirmation_detected evidence=%s url_before=%s url_after=%s navigation_observed=%s elapsed_ms=%s"])
