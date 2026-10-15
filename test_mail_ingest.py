import datetime as dt
from email.message import EmailMessage
from email.utils import format_datetime
import hashlib
import base64
import subprocess
import unittest
from unittest.mock import Mock

from mail_ingest import classify, run, verify_signature
from mail_store import MailStore, alert_payload

NOW = dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc)
SOURCE = dict(id='synthetic', enabled=True, reuse_reviewed=True,
              name='TEST ONLY', official_url='https://example.invalid/', region='hokkaido',
              senders=['notice@example.invalid'], signing_domains=['example.invalid'],
              bear_subject_pattern=r'熊出没', public_body_pattern=r'(?P<alert>.+)\n---footer---',
              unnecessary_subject_patterns=[r'交通情報'])


def message(subject='熊出没', body='テストの熊目撃情報です。\n---footer---', issued=NOW):
    msg = EmailMessage()
    msg['From'] = 'notice@example.invalid'
    msg['Subject'] = subject
    msg['Date'] = format_datetime(issued)
    msg.set_content(body)
    return msg.as_bytes()


class ClassifierTests(unittest.TestCase):
    def classify(self, raw, **options):
        return classify(raw, [SOURCE], now=NOW, verifier=lambda *_: True, **options)

    def test_japanese_mime_deduplicates_by_content(self):
        a = self.classify(message())
        b = self.classify(message())
        self.assertEqual(a['decision'], 'bear')
        self.assertEqual(a['id'], b['id'])
        self.assertNotIn('footer', a['content']['text'])

    def test_registration_and_unmatched_content_kept(self):
        self.assertEqual(self.classify(message('熊出没 登録確認'))['decision'], 'registration')
        self.assertEqual(self.classify(message(body='判定できない形式'))['decision'], 'unknown')

    def test_private_links_never_published(self):
        for body in ['https://example.invalid/private?token=TEST\n---footer---',
                     '連絡先 test@example.invalid\n---footer---']:
            self.assertEqual(self.classify(message(body=body))['decision'], 'unknown')

    def test_unverified_or_unknown_sender_kept(self):
        self.assertEqual(classify(message(), [SOURCE], now=NOW, verifier=lambda *_: False)['decision'], 'unknown')
        raw = message().replace(b'notice@example.invalid', b'evil@example.invalid')
        self.assertEqual(self.classify(raw)['decision'], 'unknown')

    def test_old_mail_archived_and_future_kept(self):
        self.assertTrue(self.classify(message(issued=NOW-dt.timedelta(days=2)))['expired'])
        self.assertEqual(self.classify(message(issued=NOW+dt.timedelta(hours=1)))['decision'], 'unknown')

    def test_only_explicit_unnecessary_category_discarded(self):
        self.assertEqual(self.classify(message('交通情報', '通行止め'))['decision'], 'unnecessary')
        self.assertEqual(self.classify(message('その他', 'お知らせ'))['decision'], 'unknown')

    def test_corrected_text_gets_different_alert_identity(self):
        self.assertNotEqual(self.classify(message())['id'],
                            self.classify(message(body='訂正：熊の目撃場所を修正。\n---footer---'))['id'])


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(capabilities=(b'UIDPLUS',))
        self.client.select.return_value = ('OK', [])
        self.client.response.return_value = ('UIDVALIDITY', [b'123'])
        self.raw = message(issued=dt.datetime.now(dt.timezone.utc))
        self.calls = []
        def uid(command, *args):
            self.calls.append((command, args))
            if command == 'SEARCH':
                return 'OK', [b'7']
            if command == 'FETCH':
                if args[1] == '(RFC822.SIZE)':
                    return 'OK', [b'1 (UID 7 RFC822.SIZE 1000)']
                return 'OK', [(b'BODY[]', self.raw)]
            return 'OK', []
        self.client.uid.side_effect = uid
        self.store = Mock()
        self.store.cursor.return_value = 0
        self.store.receipt.side_effect = lambda i: i
        self.config = dict(username='test@example.invalid', folder='INBOX')

    def test_preview_does_not_save_or_delete_or_mark_read(self):
        result = run(self.client, self.config, [SOURCE], self.store, verifier=lambda *_: True)
        self.assertEqual(result['inspected'], 1)
        self.client.select.assert_called_once_with('INBOX', readonly=True)
        self.store.save.assert_not_called()
        self.assertFalse(any(c[0] in ('STORE', 'EXPUNGE') for c in self.calls))
        self.assertIn(('FETCH', ('7', '(BODY.PEEK[])')), self.calls)

    def test_apply_saves_before_targeted_removal(self):
        result = run(self.client, self.config, [SOURCE], self.store, apply=True, verifier=lambda *_: True)
        self.assertEqual(result['removed'], 1)
        self.store.save.assert_called_once()
        self.assertIn(('EXPUNGE', ('7',)), self.calls)

    def test_storage_failure_keeps_source(self):
        self.store.save.side_effect = RuntimeError('storage failure')
        result = run(self.client, self.config, [SOURCE], self.store, apply=True, verifier=lambda *_: True)
        self.assertEqual(result['errors'], 1)
        self.assertFalse(any(c[0] in ('STORE', 'EXPUNGE') for c in self.calls))


class StoreTests(unittest.TestCase):
    def test_translated_labels_do_not_claim_translated_original(self):
        content = classify(message(), [SOURCE], now=NOW, verifier=lambda *_: True)['content']
        payload = alert_payload(content)
        self.assertEqual(len(payload['bodies']), 6)
        self.assertIn('Japanese original', payload['bodies']['en'])
        self.assertIn(content['text'], payload['bodies']['fr'])

    def test_uncertain_delivery_is_not_retried(self):
        content = classify(message(issued=dt.datetime.now(dt.timezone.utc)), [SOURCE], verifier=lambda *_: True)['content']
        from mail_protocol import encode
        document = {'name': 'projects/test/databases/(default)/documents/mail_jobs/mail_test',
                    'updateTime': 'revision', 'fields': {'content': encode(content), 'state': encode('queued')}}
        session = Mock()
        query = Mock(status_code=200)
        query.json.return_value = [{'document': document}]
        published = Mock(status_code=200)
        failed = Mock(status_code=503)
        session.post.side_effect = [query, published, failed]
        session.patch.return_value = Mock(status_code=200)
        result = MailStore(session).dispatch()
        self.assertEqual(result['needs_review'], 1)
        self.assertEqual(session.post.call_count, 3)
        self.assertEqual(session.patch.call_args.kwargs['json']['fields']['state'], encode('needs_review'))


class RealSignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import dkim
        except ImportError:
            raise unittest.SkipTest('Install requirements.txt for real DKIM tests')
        cls.dkim = dkim
        # Ephemeral TEST ONLY key, never printed or stored in the repository.
        cls.key = subprocess.run(['openssl', 'genrsa', '1024'], capture_output=True,
                                 check=True).stdout
        pub = subprocess.run(['openssl', 'rsa', '-pubout', '-outform', 'DER'],
                             input=cls.key, capture_output=True, check=True).stdout
        cls.record = b'v=DKIM1; k=rsa; p=' + base64.b64encode(pub)

    def signed(self, headers=None):
        raw = message()
        signature = self.dkim.sign(raw, b'test', b'example.invalid', self.key,
                                   include_headers=headers or [b'from', b'subject', b'date'])
        return signature + raw

    def verified(self, raw, source=SOURCE):
        return verify_signature(raw, source, dnsfunc=lambda *_args, **_kw: self.record)

    def test_valid_signature_and_tampering(self):
        raw = self.signed()
        self.assertTrue(self.verified(raw))
        self.assertFalse(self.verified(raw.replace(b'footer', b'changed')))

    def test_subject_must_be_signed_and_domain_allowlisted(self):
        self.assertFalse(self.verified(self.signed([b'from', b'date'])))
        self.assertFalse(self.verified(self.signed(), dict(SOURCE, signing_domains=['other.invalid'])))


if __name__ == '__main__':
    unittest.main()
