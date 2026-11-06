"""Test Google Forms adapter memakai page mock tanpa browser sungguhan."""
import unittest
from src.erp_posting import FormFields, GoogleFormsPoster, PostingUncertain, validate_form_fields

class Element:
    def fill(self, value): self.value = value
    def click(self): pass
    def wait_for(self, timeout): pass
class Page:
    def goto(self, url, timeout, **kwargs): pass
    def get_by_label(self, label, exact=True): return Element()
    def get_by_role(self, role, name): return Element()
    def get_by_text(self, text, exact=False): return Element()

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
        invoice={"invoice_number":"I","vendor_name":"V","po_number":"P","amount":"1","currency":"IDR","correlation_id":"c","posting_ref":"POST-1"}
        with self.assertRaises(PostingUncertain): GoogleFormsPoster(TimeoutPage(), FormFields(), 100).post("https://example.test/form", invoice)

    def test_reports_missing_label(self):
        class MissingPage(Page):
            def get_by_label(self, label, exact=True):
                if label == "Currency": raise LookupError()
                return Element()
        self.assertEqual(validate_form_fields(MissingPage(), "https://example.test/form", FormFields(), 100), ["Currency"])
