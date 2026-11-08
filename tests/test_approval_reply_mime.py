"""Regression MIME parser approval reply tanpa token/body dicetak ke output test."""
import unittest
from email.message import EmailMessage
from unittest.mock import patch

from src.approval import EmailSettings, ImapInbox, diagnose_reply, parse_reply


TOKEN='abcdefghijklmnopqrstuvwxyz'
TEXT=f'APPROVE\nApproval Token: {TOKEN}\n'


def message_bytes(kind: str) -> bytes:
    message=EmailMessage(); message['From']='approver@example.com'
    if kind=='multipart':
        message.set_content(TEXT); message.add_alternative('<p>Approve</p>',subtype='html')
    else:
        message.set_content(TEXT,cte=kind if kind in {'quoted-printable','base64'} else None)
    return message.as_bytes()


class ReplyMimeParsingTests(unittest.TestCase):
    def test_plain_multipart_quoted_printable_and_base64_are_valid(self):
        for kind in ('plain','multipart','quoted-printable','base64'):
            with self.subTest(kind=kind):
                raw=message_bytes(kind)
                parsed=parse_reply(raw); diagnostic=diagnose_reply(raw,'77')
                self.assertEqual(parsed.decision,'APPROVE')
                self.assertIsNotNone(parsed.token)
                self.assertTrue(diagnostic.reply_format_valid)
                self.assertIsNone(diagnostic.failure_category)

    def test_quoted_original_decision_is_not_applied(self):
        message=EmailMessage(); message['From']='approver@example.com'
        message.set_content(f'Looks good\n\nOn yesterday wrote:\n> APPROVE\n> Approval Token: {TOKEN}\n')
        diagnostic=diagnose_reply(message.as_bytes(),'78')
        self.assertFalse(diagnostic.decision_detected)
        self.assertFalse(diagnostic.reply_format_valid)
        self.assertEqual(diagnostic.failure_category,'DECISION_ONLY_IN_QUOTED_CONTENT')

    def test_single_uid_diagnostic_fetches_only_requested_uid_read_only(self):
        raw=message_bytes('multipart'); header,body=raw.split(b'\n\n',1)
        class FakeImap:
            def __init__(self): self.calls=[]; self.readonly=False
            def __enter__(self): return self
            def __exit__(self,*args): return None
            def login(self,*args): return 'OK',[]
            def select(self,*args,**kwargs): self.readonly=kwargs.get('readonly'); return 'OK',[]
            def uid(self,command,*args):
                self.calls.append((command,args))
                self_test.assertEqual(command,'fetch')
                self_test.assertEqual(args[0],b'18620')
                return 'OK',[(b'header',header+b'\n')] if 'HEADER' in args[1] else [(b'text',body)]
        self_test=self; fake=FakeImap()
        settings=EmailSettings('smtp',587,'u','p','imap',993,'u','p')
        with patch('src.approval.imaplib.IMAP4_SSL',return_value=fake):
            diagnostic=ImapInbox(settings).diagnose_uid('18620')
        self.assertTrue(fake.readonly)
        self.assertTrue(diagnostic.reply_format_valid)
        self.assertEqual(len(fake.calls),2)


if __name__ == '__main__':
    unittest.main()
