import unittest
import json
import os
import sys
from unittest.mock import MagicMock, patch

# Добавляем текущую директорию в path, чтобы импортировать index
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import index

class TestNotifier(unittest.TestCase):
    def setUp(self):
        self.sample_data = {
            "id": "T-7333-77-002-2956-C-13-1102120d-9412-43dc-9b10-117dceed298a",
            "operator": "44170d5f34ccf8ef",
            "readyBox": [
                {
                    "Number": 0,
                    "boxNumber": "046070517915135385",
                    "productNumbersFull": ["p1", "p2", "p3", "p4", "p5", "p6"]
                },
                {
                    "Number": 1,
                    "boxNumber": "046070517915135590",
                    "productNumbersFull": ["p7", "p8", "p9", "p10", "p11", "p12"]
                }
            ]
        }

    def test_data_extraction(self):
        ready_boxes = self.sample_data.get('readyBox', [])
        boxes_count = len(ready_boxes)
        products_count = sum(len(box.get('productNumbersFull', [])) for box in ready_boxes)

        self.assertEqual(boxes_count, 2)
        self.assertEqual(products_count, 12)
        self.assertEqual(self.sample_data['id'], "T-7333-77-002-2956-C-13-1102120d-9412-43dc-9b10-117dceed298a")
        self.assertEqual(self.sample_data['operator'], "44170d5f34ccf8ef")

    def test_compact_ssccs(self):
        ssccs = [
            "046070517917346241",
            "046070517917346258",
            "046070517917346265",
            "046070517917346302",
            "046070517917346326",
            "00046070517917346340", # AI (00)
        ]
        result = index.compact_ssccs(ssccs)
        self.assertEqual(result, "1734624-1734626, 1734630, 1734632, 1734634")

    def test_compact_report_boxes_invalid(self):
        report = {
            "readyBox": [
                {"boxNumber": "123"} # Invalid length
            ]
        }
        result = index.compact_report_boxes(report)
        self.assertEqual(result, "—")

    def test_image_generation(self):
        report_info = {
            'id': self.sample_data['id'],
            'operator': self.sample_data['operator'],
            'boxes': 2,
            'products': 12,
            'ssccs': "1513538, 1513559"
        }
        png_bytes = index.create_info_image(report_info)
        self.assertIsInstance(png_bytes, bytes)
        self.assertTrue(len(png_bytes) > 0)
        # Проверка заголовка PNG
        self.assertEqual(png_bytes[:8], b'\x89PNG\r\n\x1a\n')

    @patch('index.get_s3_client')
    @patch('requests.post')
    def test_handler_success(self, mock_post, mock_get_s3):
        # Mock S3
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(self.sample_data).encode('utf-8')
        mock_s3.get_object.return_value = {'Body': mock_body}

        # Mock Requests
        mock_res_init = MagicMock()
        mock_res_init.status_code = 200
        mock_res_init.json.return_value = {'url': 'http://upload.url'}

        mock_res_upload = MagicMock()
        mock_res_upload.status_code = 200
        mock_res_upload.json.return_value = {'token': 'file_token_123'}

        mock_res_final = MagicMock()
        mock_res_final.status_code = 200

        mock_post.side_effect = [mock_res_init, mock_res_upload, mock_res_final]

        os.environ["MAX_BOT_TOKEN"] = "test_token"
        os.environ["MAX_CHAT_ID"] = "test_chat"

        event = {
            'messages': [
                {
                    'details': {
                        'bucket_id': 'test-bucket',
                        'object_id': 'test-object.json'
                    }
                }
            ]
        }

        result = index.handler(event, None)
        self.assertEqual(result['statusCode'], 200)
        self.assertEqual(mock_post.call_count, 3)

if __name__ == '__main__':
    unittest.main()
