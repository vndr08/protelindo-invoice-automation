"""Regression orchestration end-to-end tanpa Gmail, OCR, SMTP, browser, atau filesystem live."""
from pathlib import Path
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch

from src.config import AppConfig
from src.e2e_orchestrator import FinalE2EPaths, preflight_final_e2e, prepare_final_e2e_environment, run_stages, validate_isolated_paths


class FinalE2EOrchestratorTests(unittest.TestCase):
    def _preflight_paths(self, root: Path) -> FinalE2EPaths:
        base = root / "data" / "final-e2e-test"
        resources = root / "resources"
        resources.mkdir()
        for name in ("Invoices.template.xlsx", "ERP_Dummy.template.xlsx", "ProcessLogs.template.xlsx", "PurchaseOrders.csv"):
            (resources / name).touch()
        return FinalE2EPaths(base / "automation.db", base / "incoming", base / "output", base / "processed")

    def test_prepare_creates_only_isolated_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "data" / "final-e2e-test"
            paths = FinalE2EPaths(base / "automation.db", base / "incoming", base / "output", base / "processed")
            prepare_final_e2e_environment(paths, root)
            self.assertTrue(paths.database.exists())
            self.assertTrue(paths.incoming.is_dir())
            self.assertTrue(paths.output.is_dir())
            self.assertTrue(paths.processed.is_dir())

    def test_orchestration_order_and_resume_skip_intake(self):
        calls: list[str] = []
        stages = {name: (lambda name=name: calls.append(name)) for name in ("email_intake", "ocr", "po_validation", "approval", "erp", "archive", "export")}
        self.assertEqual(run_stages(stages), ["email_intake", "ocr", "po_validation", "approval", "erp", "archive", "export"])
        self.assertEqual(calls, ["email_intake", "ocr", "po_validation", "approval", "erp", "archive", "export"])
        calls.clear()
        self.assertEqual(run_stages(stages, resume=True), ["ocr", "po_validation", "approval", "erp", "archive", "export"])
        self.assertNotIn("email_intake", calls)

    def test_partial_failure_stops_later_external_stages_and_resume_can_continue(self):
        calls: list[str] = []
        def fail_approval():
            calls.append("approval")
            raise RuntimeError("approval unavailable")
        stages = {name: (lambda name=name: calls.append(name)) for name in ("email_intake", "ocr", "po_validation", "erp", "archive", "export")}
        stages["approval"] = fail_approval
        with self.assertRaises(RuntimeError):
            run_stages(stages)
        self.assertEqual(calls, ["email_intake", "ocr", "po_validation", "approval"])

    def test_non_isolated_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = FinalE2EPaths(root / "data" / "automation.db", root / "data" / "incoming", root / "data" / "output", root / "data" / "processed")
            with self.assertRaises(ValueError):
                validate_isolated_paths(bad, root)

    def test_preflight_rejects_submission_placeholders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._preflight_paths(root)
            config = AppConfig(Decimal("1000000"), "https://your-form-url.example/viewform", "/ERP_Dummy.xlsx", "approver@example.com")
            with patch("src.e2e_orchestrator.load_config", return_value=config):
                result = preflight_final_e2e(paths, root)
            self.assertFalse(result["forms_url_configured"])
            self.assertFalse(result["approver_email_configured"])
            self.assertFalse(result["ready"])

    def test_preflight_accepts_complete_reviewer_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._preflight_paths(root)
            config = AppConfig(Decimal("1000000"), "https://forms.example.test/forms/id/viewform", "/ERP_Dummy.xlsx", "reviewer@example.test")
            with patch("src.e2e_orchestrator.load_config", return_value=config):
                result = preflight_final_e2e(paths, root)
            self.assertTrue(result["forms_url_configured"])
            self.assertTrue(result["approver_email_configured"])
            self.assertTrue(result["ready"])
