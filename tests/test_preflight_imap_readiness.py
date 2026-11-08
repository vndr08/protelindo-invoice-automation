"""Regression test preflight IMAP lokal tanpa koneksi Gmail atau email nyata."""
import unittest
import os
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.config import AppConfig
from src.preflight import run_preflight


class PreflightImapReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'src').mkdir()
        (self.root/'src'/'imap_adapter.py').write_text('# adapter',encoding='utf-8')
        (self.root/'src'/'email_intake.py').write_text('# intake',encoding='utf-8')
        (self.root/'main.py').write_text('command = "email-intake"',encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def write_env(self,text: str):
        (self.root/'.env').write_text(text,encoding='utf-8')

    def test_complete_imap_configuration_is_locally_ready_without_live_claim(self):
        self.write_env(
            'IMAP_HOST=imap.example.test\nIMAP_PORT=993\nIMAP_USERNAME=robot@example.test\n'
            'IMAP_PASSWORD=test-password\nIMAP_INTAKE_MAILBOX=INBOX\n'
            'IMAP_INTAKE_TIMEOUT_SECONDS=30\nIMAP_ATTACHMENT_MAX_BYTES=1024\n'
        )
        blockers,warnings=run_preflight(self.root)
        self.assertFalse(any('IMAP configuration missing' in item for item in blockers))
        self.assertFalse(any('IMAP invoice intake belum' in item for item in blockers))
        self.assertIn('IMAP local configuration is ready; run email-check to verify live connectivity.',warnings)
        self.assertFalse(any('test-password' in item for item in blockers+warnings))

    def test_incomplete_imap_configuration_is_a_local_blocker(self):
        self.write_env('IMAP_HOST=imap.example.test\nIMAP_PORT=not-a-port\n')
        # Isolasi environment process karena test lain dapat memuat .env proyek utama.
        with patch.dict(os.environ,{'IMAP_USERNAME':'','IMAP_PASSWORD':''},clear=False):
            blockers,warnings=run_preflight(self.root)
        missing=next(item for item in blockers if item.startswith('IMAP configuration missing:'))
        self.assertIn('IMAP_USERNAME',missing)
        self.assertIn('IMAP_PASSWORD',missing)
        self.assertNotIn('imap.example.test',missing)
        self.assertFalse(any('live connectivity' in item for item in warnings))

    def test_submission_safe_config_placeholders_are_explicit_blockers(self):
        """Placeholder Config tidak boleh membuat integrasi reviewer memakai fallback diam-diam."""
        config=AppConfig(
            threshold_amount=Decimal('1000000'),
            forms_url='https://your-form-url.example',
            excel_erp_path='/ERP_Dummy.xlsx',
            approver_email='approver@example.com',
        )
        with patch('src.preflight.load_config',return_value=config):
            blockers,_=run_preflight(self.root)
        self.assertIn('FormsUrl masih placeholder',blockers)
        self.assertIn('ApproverEmail masih placeholder',blockers)


if __name__ == '__main__':
    unittest.main()
