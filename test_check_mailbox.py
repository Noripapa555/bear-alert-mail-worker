import unittest
import imaplib
import socket
import ssl
from unittest.mock import Mock

from check_mailbox import check, CheckError


class MailboxCheckTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(host='example.invalid', port=993,
                           username='test@example.invalid', folder='INBOX')
        self.client = Mock()
        self.client.login.return_value = ('OK', [])
        self.client.select.return_value = ('OK', [])
        self.factory = Mock(return_value=self.client)

    def test_verified_tls_read_only_and_no_fetch(self):
        check(self.config, 'test-only', self.factory)
        context = self.factory.call_args.kwargs['ssl_context']
        self.assertTrue(context.check_hostname)
        self.client.select.assert_called_once_with('INBOX', readonly=True)
        self.client.fetch.assert_not_called()
        self.client.store.assert_not_called()
        self.client.logout.assert_called_once()

    def test_failed_login_does_not_open_mailbox(self):
        self.client.login.return_value = ('NO', [])
        with self.assertRaises(ValueError):
            check(self.config, 'test-only', self.factory)
        self.client.select.assert_not_called()
        self.client.logout.assert_called_once()

    def test_failed_folder_still_logs_out(self):
        self.client.select.return_value = ('NO', [])
        with self.assertRaises(ValueError):
            check(self.config, 'test-only', self.factory)
        self.client.logout.assert_called_once()

    def test_auth_failure_does_not_disclose_server_response(self):
        self.client.login.side_effect = imaplib.IMAP4.error('secret server detail')
        with self.assertRaisesRegex(CheckError, 'E04') as caught:
            check(self.config, 'test-only', self.factory)
        self.assertNotIn('secret', str(caught.exception))
        self.client.logout.assert_called_once()

    def test_network_failures_have_distinct_codes(self):
        for error, code in [(socket.gaierror(), 'E01'),
                            (ssl.SSLError(), 'E02'), (TimeoutError(), 'E03')]:
            with self.subTest(code=code):
                self.factory.side_effect = error
                with self.assertRaisesRegex(CheckError, code):
                    check(self.config, 'test-only', self.factory)
