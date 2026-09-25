import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from html.parser import HTMLParser
import index
import requests


class TaskNotifierTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'MAX_BOT_TOKEN': 'test', 'MAX_CHAT_ID': '-1'})
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
        self.assertEqual(s3.return_value.generate_presigned_url.call_args.kwargs['ExpiresIn'], 172800)
        self.assertEqual([c[0] for c in s3.return_value.mock_calls], ['get_object', 'generate_presigned_url'])
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['verify'], index.MAX_CA_FILE)
        self.assertNotIn('attachments', post.call_args.kwargs['json'])
        self.assertEqual(post.call_args.kwargs['params'], {'chat_id': '-1', 'disable_link_preview': 'true'})
        self.assertEqual(post.call_args.kwargs['json'], {
            'text': '<b>Задание оборудования</b>\nT-test\n\n<a href="https://storage.example/private-download">📄 Скачать JSON</a>',
            'notify': True, 'format': 'html',
        })

    @patch('index.requests.post')
    @patch('index.get_s3_client')
    def test_html_escapes_id_and_preserves_signed_url(self, s3, post):
        task_id = 'T-<b>"&test'
        self.event['messages'][0]['details']['object_id'] = f'equipment-tasks/{task_id}.json'
        url = 'https://storage.example/T-test.json?signature=a%2Bb%3D&name="task"&other=1'
        s3.return_value.get_object.return_value = {'Body': io.BytesIO(json.dumps({'id': task_id}).encode())}
        s3.return_value.generate_presigned_url.return_value = url
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'message': {'body': {'mid': 'mid.escaped'}}}
        index.task_handler(self.event, None)
        text = post.call_args.kwargs['json']['text']
        self.assertIn('T-&lt;b&gt;&quot;&amp;test', text)
        self.assertIn('&amp;name=&quot;task&quot;', text)
        links = []
        class Links(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag == 'a':
                    links.append(dict(attrs)['href'])
        Links().feed(text)
        self.assertEqual(links, [url])
        self.assertNotIn('Загрузка файлов', text)
        self.assertNotIn('Скачать исходный', text)

    @patch('index.requests.post', side_effect=requests.Timeout('ambiguous'))
    @patch('index.get_s3_client')
    def test_timeout_is_not_retried(self, s3, post):
        s3.return_value.get_object.return_value = {'Body': io.BytesIO(b'{"id":"T-test"}')}
        s3.return_value.generate_presigned_url.return_value = 'https://storage.example/link'
        with self.assertRaises(requests.Timeout):
            index.task_handler(self.event, None)
        post.assert_called_once()

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
