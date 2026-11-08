"""Regression policy untuk UTC storage dan tampilan WIB."""

from datetime import datetime, timezone
import unittest

from src.time_utils import excel_wib_datetime, format_wib, parse_utc_timestamp, to_wib, utc_now, utc_now_iso
from src.email_intake import parse_email_attachments


class TimestampPolicyTests(unittest.TestCase):
    def test_new_timestamp_is_aware_utc_with_explicit_offset(self) -> None:
        self.assertEqual(utc_now().utcoffset().total_seconds(), 0)
        self.assertTrue(utc_now_iso().endswith('+00:00'))

    def test_utc_to_wib_crosses_day_boundary(self) -> None:
        source = '2026-01-01T20:30:00+00:00'
        self.assertEqual(format_wib(source), '2026-01-02 03:30 WIB')
        self.assertEqual(excel_wib_datetime(source), datetime(2026, 1, 2, 3, 30))

    def test_legacy_sqlite_naive_timestamp_is_interpreted_as_utc(self) -> None:
        parsed = parse_utc_timestamp('2026-01-01 23:00:00')
        self.assertEqual(parsed, datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc))
        self.assertEqual(to_wib(parsed).hour, 6)

    def test_blank_timestamp_stays_blank(self) -> None:
        self.assertIsNone(format_wib(None))
        self.assertIsNone(excel_wib_datetime(''))

    def test_imap_message_date_is_canonical_utc(self) -> None:
        """Metadata Gmail RFC 2822 disimpan canonical agar tidak bergantung timezone laptop."""
        raw = b"From: sender@example.test\r\nSubject: timestamp\r\nDate: Thu, 01 Jan 2026 20:30:00 +0000\r\n\r\nbody"
        metadata, attachments = parse_email_attachments(raw, 1024)
        self.assertEqual(metadata.date, '2026-01-01T20:30:00+00:00')
        self.assertEqual(attachments, [])
