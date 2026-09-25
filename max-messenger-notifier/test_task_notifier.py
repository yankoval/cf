import io
import json
import os
import unittest
from unittest.mock import MagicMock, patch
import index


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
