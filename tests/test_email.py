import hashlib
import hmac
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import monitor


ENDPOINT = 'https://script.google.com/macros/s/test-deployment/exec'
SECRET = 'test-unsubscribe-secret-' + 'x' * 32
API_KEY = 'test-api-key-' + 'y' * 32
EVENTS = [{'company_name': '測試公司', 'stage': '送件', 'title': '測試事件'}]


def payload(emails):
    return json.dumps({
        'ok': True, 'schema_version': 1, 'emails': emails,
        'signing_key_check': hmac.new(SECRET.encode(), b'unsubscribe-config:v1', hashlib.sha256).hexdigest(),
    })


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.config = patch.multiple(monitor,
            EMAIL_SERVICE_SENDER='service@example.com', EMAIL_SERVICE_APP_PASSWORD='test-password',
            EMAIL_FROM_NAME='興櫃轉板監測通知', EMAIL_SUBSCRIBERS_URL=ENDPOINT,
            EMAIL_SUBSCRIBERS_API_KEY=API_KEY, EMAIL_UNSUBSCRIBE_SECRET=SECRET, MAIL_TEST_MODE=False)
        self.config.start()
        self.addCleanup(self.config.stop)

    def test_each_recipient_has_separate_message_and_token(self):
        with patch.object(monitor, 'fetch_subscriber_emails', return_value=['alice@example.com', 'bob@example.com']), \
                patch.object(monitor.smtplib, 'SMTP_SSL') as smtp_class:
            smtp = smtp_class.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            self.assertEqual(monitor.send_email_notification(EVENTS), 2)
        smtp.login.assert_called_once_with('service@example.com', 'test-password')
        calls = smtp.send_message.call_args_list
        self.assertEqual(len(calls), 2)
        tokens = []
        for call, recipient, other in zip(calls, ['alice@example.com', 'bob@example.com'], ['bob@example.com', 'alice@example.com']):
            message = call.args[0]
            self.assertEqual(message['To'], recipient)
            self.assertIn('service@example.com', str(message['From']))
            self.assertIn('service@example.com', str(message['Reply-To']))
            self.assertIsNone(message['Cc'])
            self.assertIsNone(message['Bcc'])
            self.assertNotIn(other, message.as_string())
            self.assertEqual(call.kwargs, {'from_addr': 'service@example.com', 'to_addrs': [recipient]})
            url = str(message['List-Unsubscribe']).strip('<>')
            self.assertIn(url, message.get_content())
            self.assertNotIn(API_KEY, message.as_string())
            self.assertNotIn(SECRET, message.as_string())
            self.assertIsNone(message['List-Unsubscribe-Post'])
            tokens.append(parse_qs(urlsplit(url).query)['token'][0])
        self.assertNotEqual(tokens[0], tokens[1])

    def test_smtp_refusal_does_not_expose_recipient_in_error(self):
        with patch.object(monitor, 'fetch_subscriber_emails', return_value=['alice@example.com']), \
                patch.object(monitor.smtplib, 'SMTP_SSL') as smtp_class:
            smtp = smtp_class.return_value.__enter__.return_value
            smtp.send_message.side_effect = monitor.smtplib.SMTPRecipientsRefused({'alice@example.com': (550, b'no')})
            with self.assertRaises(RuntimeError) as caught:
                monitor.send_email_notification(EVENTS)
            self.assertNotIn('alice', str(caught.exception))

    def test_no_service_identity_never_uses_legacy_private_account(self):
        with patch.object(monitor, 'EMAIL_SERVICE_SENDER', ''), \
                patch.dict('os.environ', {'EMAIL_SENDER': 'private@example.com'}), \
                patch.object(monitor.smtplib, 'SMTP_SSL') as smtp_class:
            with self.assertRaisesRegex(RuntimeError, '獨立服務信箱'):
                monitor.send_email_notification(EVENTS)
            smtp_class.assert_not_called()

    def test_test_mode_uses_only_explicit_recipients(self):
        with patch.object(monitor, 'MAIL_TEST_MODE', True), \
                patch.object(monitor, 'fetch_subscriber_emails') as fetch:
            self.assertEqual(monitor.get_email_recipients('Test@Example.com'), ['test@example.com'])
            fetch.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, '必須填寫'):
                monitor.get_email_recipients('')

    def test_empty_active_list_sends_nothing(self):
        with patch.object(monitor, 'fetch_subscriber_emails', return_value=[]), \
                patch.object(monitor.smtplib, 'SMTP_SSL') as smtp_class:
            self.assertEqual(monitor.send_email_notification(EVENTS), 0)
            smtp_class.assert_not_called()

    def test_no_events_does_not_query_or_send(self):
        with patch.object(monitor, 'fetch_subscriber_emails') as fetch, \
                patch.object(monitor.smtplib, 'SMTP_SSL') as smtp_class:
            self.assertEqual(monitor.send_email_notification([]), 0)
            fetch.assert_not_called()
            smtp_class.assert_not_called()

    def test_subscriber_api_uses_authenticated_post(self):
        response = Mock(text=payload(['alice@example.com', 'alice@example.com']))
        with patch.object(monitor.requests, 'post', return_value=response) as post:
            self.assertEqual(monitor.fetch_subscriber_emails(), ['alice@example.com'])
            post.assert_called_once_with(ENDPOINT, json={'action': 'list', 'api_key': API_KEY}, timeout=40)
            response.raise_for_status.assert_called_once()

    def test_api_errors_and_old_formats_fail_closed(self):
        for raw in ['not json', '[]', '{"ok":false}', '{"emails":["old@example.com"]}',
                    payload(['bad@example.com\r\nBcc: stolen@example.com'])]:
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                monitor.parse_subscriber_emails_from_payload(raw)
        with patch.object(monitor.requests, 'post', side_effect=monitor.requests.Timeout('sensitive-detail')):
            with self.assertRaises(RuntimeError) as caught:
                monitor.fetch_subscriber_emails()
            self.assertNotIn('sensitive-detail', str(caught.exception))

    def test_mismatched_signing_secret_stops_before_sending(self):
        with patch.object(monitor.requests, 'post', return_value=Mock(text=payload(['alice@example.com']))), \
                patch.object(monitor, 'EMAIL_UNSUBSCRIBE_SECRET', 'another-secret-' + 'z' * 32), \
                patch.object(monitor.smtplib, 'SMTP_SSL') as smtp_class:
            with self.assertRaisesRegex(RuntimeError, '不一致'):
                monitor.send_email_notification(EVENTS)
            smtp_class.assert_not_called()

    def test_secret_in_url_or_wrong_endpoint_is_rejected(self):
        for endpoint in [ENDPOINT + '?key=secret', ENDPOINT.replace('/exec', '/dev'),
                         ENDPOINT.replace('https:', 'http:'), 'https://example.com/exec']:
            with self.subTest(endpoint=endpoint), patch.object(monitor, 'EMAIL_SUBSCRIBERS_URL', endpoint), \
                    patch.object(monitor.requests, 'post') as post:
                with self.assertRaises(RuntimeError):
                    monitor.fetch_subscriber_emails()
                post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
