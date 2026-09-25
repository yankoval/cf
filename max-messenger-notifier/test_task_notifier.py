import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch
import index
import requests


class TaskNotifierTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'MAX_BOT_TOKEN': 'test', 'MAX_CHAT_ID': '-1', 'MAX_SHORT_LINK_MODE': 'off'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.key = 'equipment-tasks/T-test.json'
        self.event = {'messages': [{'details': {'bucket_id': '1bf11148-3595-4a07-a089-d460153b7c7a', 'object_id': self.key}}]}

    @patch('index.requests.post')
    @patch('index.get_s3_client')
    def test_link_notification_reads_only_and_returns_message_id(self, s3, post):
        s3.return_value.get_object.return_value = {'Body': io.BytesIO(json.dumps({'id': 'T-test'}).encode())}
        s3.return_value.generate_presigned_url.return_value = 'https://storage.example/private-download'
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'message': {'body': {'mid': 'mid.123'}}}
        result = index.task_handler(self.event, None)
        self.assertEqual(result['deliveries'][0]['message_id'], 'mid.123')
        self.assertEqual(s3.return_value.generate_presigned_url.call_args.kwargs['ExpiresIn'], 86400)
        self.assertEqual([c[0] for c in s3.return_value.mock_calls], ['get_object', 'generate_presigned_url'])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['verify'], index.MAX_CA_FILE)
        self.assertNotIn('attachments', post.call_args.kwargs['json'])

    @patch('index.get_s3_client')
    def test_rejects_production_input_before_access(self, s3):
        self.event['messages'][0]['details']['object_id'] = 'Задания/T-test.json'
        with self.assertRaises(ValueError):
            index.task_handler(self.event, None)
        s3.assert_not_called()

    @patch('index.requests.post')
    @patch('index.get_s3_client')
    def test_http_200_error_is_not_success_or_retried(self, s3, post):
        s3.return_value.get_object.return_value = {'Body': io.BytesIO(b'{"id":"T-test"}')}
        s3.return_value.generate_presigned_url.return_value = 'https://storage.example/link'
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'code': 'internal.error'}
        with self.assertRaisesRegex(RuntimeError, 'reconcile'):
            index.task_handler(self.event, None)
        self.assertEqual(post.call_count, 1)

    @patch('index.get_s3_client')
    def test_empty_smoke_has_no_side_effects(self, s3):
        self.assertEqual(index.task_handler({'messages': []}, None), {'statusCode': 200, 'body': 'OK'})
        s3.assert_not_called()

    @patch('index.short_links.create_link')
    @patch('index.requests.post')
    @patch('index.get_s3_client')
    def test_short_link_is_sent_once_in_approved_compact_html_format(self, s3, post, create):
        source = b'{"id":"T-test"}\r\n'
        s3.return_value.get_object.return_value = {'Body': io.BytesIO(source)}
        create.return_value = {'url': 'https://download.example/?id=opaque', 'expires_at': 1790434800}
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'message': {'body': {'mid': 'mid.short'}}}
        with patch.dict(os.environ, {'MAX_SHORT_LINK_MODE': 'on'}):
            with self.assertLogs(index.logger, level='INFO') as logs:
                result = index.task_handler(self.event, None)
        create.assert_called_once_with('1bf11148-3595-4a07-a089-d460153b7c7a', self.key, source, None)
        s3.return_value.generate_presigned_url.assert_not_called()
        post.assert_called_once()
        self.assertEqual(result['deliveries'][0]['message_id'], 'mid.short')
        text = post.call_args.kwargs['json']['text']
        self.assertIn(create.return_value['url'], text)
        self.assertIn('📄 Скачать JSON</a>', text)
        self.assertNotIn('временно недоступна', text)
        self.assertEqual(post.call_args.kwargs['json']['format'], 'html')
        self.assertEqual(post.call_args.kwargs['params']['disable_link_preview'], 'true')
        self.assertNotIn('24 часа', text)
        self.assertNotIn('opaque', str(logs.output) + str(result))

    @patch('index.requests.post')
    @patch('index.get_s3_client')
    def test_long_link_fallback_preserves_signature_and_escapes_html(self, s3, post):
        import html
        s3.return_value.get_object.return_value = {'Body': io.BytesIO(b'{"id":"T-test"}')}
        url = 'https://storage.example/?a=1&signature=abc%2Bdef&filename="test"'
        s3.return_value.generate_presigned_url.return_value = url
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'message': {'body': {'mid': 'mid.123'}}}
        index.task_handler(self.event, None)
        text = post.call_args.kwargs['json']['text']
        self.assertIn('href="' + html.escape(url, quote=True) + '"', text)
        self.assertIn('📄 Скачать JSON</a>', text)
        self.assertEqual(post.call_args.kwargs['params']['disable_link_preview'], 'true')

    @patch('index.short_links.create_link', side_effect=index.short_links.LinkError('expired'))
    @patch('index.requests.post')
    @patch('index.get_s3_client')
    def test_registry_failure_stops_before_max_without_renewal(self, s3, post, create):
        s3.return_value.get_object.return_value = {'Body': io.BytesIO(b'{"id":"T-test"}')}
        with patch.dict(os.environ, {'MAX_SHORT_LINK_MODE': 'on'}):
            with self.assertRaises(index.short_links.LinkError):
                index.task_handler(self.event, None)
        post.assert_not_called()
        s3.return_value.generate_presigned_url.assert_not_called()

    @patch('index.short_links.create_link', return_value={'url': 'https://download.example/?id=opaque', 'expires_at': 1790434800})
    @patch('index.requests.post')
    @patch('index.get_s3_client')
    def test_short_mode_preserves_delivery_errors_and_no_internal_retry(self, s3, post, create):
        for status, payload, error in [(503, {}, None), (200, {'code': 'internal.error'}, None),
                                       (200, {'success': False}, None), (200, {}, requests.Timeout('ambiguous'))]:
            with self.subTest(status=status, error=type(error).__name__):
                post.reset_mock()
                post.side_effect = error
                post.return_value.status_code = status
                post.return_value.json.return_value = payload
                s3.return_value.get_object.return_value = {'Body': io.BytesIO(b'{"id":"T-test"}')}
                with patch.dict(os.environ, {'MAX_SHORT_LINK_MODE': 'on'}):
                    with self.assertRaises((RuntimeError, requests.Timeout)):
                        index.task_handler(self.event, None)
                self.assertEqual(post.call_count, 1)

    @patch('index.short_links.create_link')
    @patch('index.get_s3_client')
    def test_short_mode_empty_smoke_has_no_side_effects(self, s3, create):
        with patch.dict(os.environ, {'MAX_SHORT_LINK_MODE': 'on'}):
            self.assertEqual(index.task_handler({'messages': []}, None), {'statusCode': 200, 'body': 'OK'})
        s3.assert_not_called()
        create.assert_not_called()
