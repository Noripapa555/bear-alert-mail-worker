import hashlib
import unittest
from unittest.mock import Mock

from mail_retention import MailIdentity, process_then_remove


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.raw = b'synthetic test mail'
        self.identity = MailIdentity('test@example.invalid/INBOX', '123', '7',
                                     hashlib.sha256(self.raw).hexdigest())
        self.client = Mock(capabilities=(b'IMAP4rev1', b'UIDPLUS'))
        self.client.response.return_value = ('UIDVALIDITY', [b'123'])
        self.client.uid.return_value = ('OK', [])
        self.save = Mock(return_value=self.identity)

    def process(self, **changes):
        args = dict(decision='bear', source_verified=True, persist_result=self.save)
        args.update(changes)
        return process_then_remove(self.client, self.identity, self.raw, **args)

    def test_only_target_uid_removed_after_save(self):
        events = []
        self.save.side_effect = lambda *_: (events.append('saved'), self.identity)[1]
        self.client.uid.side_effect = lambda *a: (events.append(a), ('OK', []))[1]
        self.assertEqual(self.process(), 'removed')
        self.assertEqual(events, ['saved', ('STORE', '7', '+FLAGS.SILENT', r'(\Deleted)'),
                                  ('EXPUNGE', '7')])
        self.client.expunge.assert_not_called()
        self.client.close.assert_not_called()

    def test_failed_save_prevents_deletion(self):
        self.save.side_effect = OSError('storage unavailable')
        with self.assertRaises(OSError):
            self.process()
        self.client.uid.assert_not_called()

    def test_missing_receipt_prevents_deletion(self):
        self.save.return_value = None
        with self.assertRaises(ValueError):
            self.process()
        self.client.uid.assert_not_called()

    def test_registration_unknown_failed_unverified_retained(self):
        for decision, verified in [('registration', True), ('unknown', True),
                                   ('failed', True), ('bear', False), ('unnecessary', False)]:
            with self.subTest(decision=decision, verified=verified):
                self.assertEqual(self.process(decision=decision, source_verified=verified), 'retained')
        self.save.assert_not_called()
        self.client.uid.assert_not_called()

    def test_verified_unnecessary_removed(self):
        self.assertEqual(self.process(decision='unnecessary'), 'removed')

    def test_changed_mailbox_or_content_prevents_deletion(self):
        self.client.response.return_value = ('UIDVALIDITY', [b'456'])
        with self.assertRaises(ValueError):
            self.process()
        self.client.uid.assert_not_called()
        self.client.response.return_value = ('UIDVALIDITY', [b'123'])
        self.raw = b'different message'
        with self.assertRaises(ValueError):
            self.process()
        self.client.uid.assert_not_called()

    def test_no_targeted_expunge_keeps_mail(self):
        self.client.capabilities = (b'IMAP4rev1',)
        self.assertEqual(self.process(), 'retained_no_uidplus')
        self.client.uid.assert_not_called()

    def test_failed_store_does_not_expunge(self):
        self.client.uid.return_value = ('NO', [])
        with self.assertRaises(RuntimeError):
            self.process()
        self.assertEqual(self.client.uid.call_count, 1)
