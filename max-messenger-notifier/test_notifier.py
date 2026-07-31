import io
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch


sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import index


class TestNotifier(unittest.TestCase):
    def setUp(self):
        self.v1_report = {
            "id": "T-V1-REPORT",
            "operator": "operator-v1",
            "readyBox": [
                {
                    "Number": 0,
                    "boxNumber": "046070517915135385",
                    "productNumbersFull": ["p1", "p2", "p3"],
                },
                {
                    "Number": 1,
                    "boxNumber": "046070517915135590",
                    "productNumbersFull": ["p4", "p5", "p6"],
                },
            ],
        }
        self.v2_report = {
            "schemaVersion": 2,
            "id": "T-V2-REPORT",
            "operator": "operator-v2",
            "readyPallet": [
                {
                    "Number": 0,
                    "palletNumber": "046070517921585754",
                    "palletAggregate": True,
                    "readyBox": [
                        {
                            "boxNumber": "046070517915135385",
                            "boxAgregate": True,
                            "productNumbersFull": ["p1", "p2"],
                        },
                        {
                            "boxNumber": "046070517915135590",
                            "boxAgregate": True,
                            "productNumbersFull": ["p3"],
                        },
                    ],
                },
                {
                    "Number": 1,
                    "palletNumber": "046070517921585761",
                    "palletAggregate": True,
                    "readyBox": [
                        {
                            "boxNumber": "046070517915135606",
                            "boxAgregate": True,
                            "productNumbersFull": ["p4", "p5", "p6"],
                        }
                    ],
                },
            ],
        }
        self.v2_task = {
            "id": "T-V2-REPORT",
            "reportSchemaVersion": 2,
            "numBoxesInPallet": 2,
            "plannedPalletCount": 2,
            "palletSsccReserve": 1,
            "palletNumbers": [
                "046070517921585754",
                "046070517921585761",
                "046070517921585778",
            ],
        }

    @staticmethod
    def s3_body(data):
        body = MagicMock()
        body.read.return_value = json.dumps(data).encode("utf-8")
        return {"Body": body}

    def test_extract_v1_report_info(self):
        info = index.extract_report_info(self.v1_report)

        self.assertEqual(info["schema_version"], 1)
        self.assertEqual(info["pallets"], 0)
        self.assertEqual(info["pallet_numbers"], [])
        self.assertEqual(info["boxes"], 2)
        self.assertEqual(info["products"], 6)

    def test_extract_v2_report_info_flattens_all_pallets(self):
        info = index.extract_report_info(self.v2_report)

        self.assertEqual(info["schema_version"], 2)
        self.assertEqual(info["pallets"], 2)
        self.assertEqual(
            info["pallet_numbers"],
            [
                "046070517921585754",
                "046070517921585761",
            ],
        )
        self.assertEqual(info["boxes"], 3)
        self.assertEqual(info["products"], 6)

    def test_compact_ssccs(self):
        ssccs = [
            "046070517917346246",
            "046070517917346253",
            "046070517917346260",
            "046070517917346307",
            "046070517917346321",
            "00046070517917346345",
        ]
        self.assertEqual(
            index.compact_ssccs(ssccs),
            "1734624-1734626, 1734630, 1734632, 1734634",
        )

    def test_compact_report_boxes_supports_v2(self):
        result = index.compact_report_boxes(self.v2_report)
        self.assertEqual(result, "1513538, 1513559-1513560")

    def test_compact_report_boxes_invalid(self):
        report = {"readyBox": [{"boxNumber": "123"}]}
        self.assertEqual(index.compact_report_boxes(report), "—")

    def test_normalize_sscc_rejects_wrong_check_digit(self):
        with self.assertRaisesRegex(ValueError, "контрольная цифра"):
            index.normalize_sscc("046070517921585755")

    def test_pallet_gs1_value_adds_ai_00(self):
        self.assertEqual(
            index.pallet_gs1_value("046070517921585754"),
            "00046070517921585754",
        )

    def test_info_image_generation(self):
        png_bytes = index.create_info_image(
            index.extract_report_info(self.v2_report)
        )
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_pallet_label_generation(self):
        png_bytes = index.create_pallet_label_image(
            report_id="T-V2-REPORT",
            pallet_number="046070517921585754",
            pallet_index=0,
            pallet_count=2,
        )
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))

        image = index.Image.open(io.BytesIO(png_bytes))
        self.assertGreaterEqual(image.width, 1200)
        self.assertGreater(image.height, 400)

    def test_task_assignment_rejects_foreign_pallet(self):
        s3 = MagicMock()
        s3.get_object.return_value = self.s3_body(
            {
                **self.v2_task,
                "palletNumbers": ["046070517921585778"],
            }
        )
        info = index.extract_report_info(self.v2_report)

        with self.assertRaisesRegex(ValueError, "не назначен"):
            index.validate_task_pallet_assignment(
                s3,
                "bucket",
                info,
            )

    @patch("index.send_file_message", return_value=True)
    @patch("index.get_s3_client")
    def test_handler_v1_keeps_one_summary_message(
        self,
        mock_get_s3,
        mock_send,
    ):
        s3 = MagicMock()
        s3.get_object.return_value = self.s3_body(self.v1_report)
        mock_get_s3.return_value = s3
        os.environ["MAX_BOT_TOKEN"] = "test_token"
        os.environ["MAX_CHAT_ID"] = "test_chat"

        result = index.handler(
            {
                "messages": [
                    {
                        "details": {
                            "bucket_id": "bucket",
                            "object_id": "equipment-reports/T-V1-REPORT.json",
                        }
                    }
                ]
            },
            None,
        )

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(mock_send.call_count, 1)
        self.assertIn(
            "Количество коробок: 2",
            mock_send.call_args.kwargs["text"],
        )

    @patch("index.create_pallet_label_image", return_value=b"pallet-png")
    @patch("index.create_info_image", return_value=b"summary-png")
    @patch("index.send_file_message", return_value=True)
    @patch("index.get_s3_client")
    def test_handler_v2_sends_summary_and_one_label_per_pallet(
        self,
        mock_get_s3,
        mock_send,
        mock_create_info,
        mock_create_label,
    ):
        s3 = MagicMock()
        s3.get_object.side_effect = [
            self.s3_body(self.v2_report),
            self.s3_body(self.v2_task),
        ]
        mock_get_s3.return_value = s3
        os.environ["MAX_BOT_TOKEN"] = "test_token"
        os.environ["MAX_CHAT_ID"] = "test_chat"

        result = index.handler(
            {
                "messages": [
                    {
                        "details": {
                            "bucket_id": "bucket",
                            "object_id": "equipment-reports/T-V2-REPORT.json",
                        }
                    }
                ]
            },
            None,
        )

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(mock_send.call_count, 3)
        self.assertEqual(mock_create_label.call_count, 2)
        self.assertIn(
            "Количество паллетов: 2",
            mock_send.call_args_list[0].kwargs["text"],
        )
        self.assertIn(
            "SSCC: 046070517921585754",
            mock_send.call_args_list[1].kwargs["text"],
        )
        self.assertIn(
            "SSCC: 046070517921585761",
            mock_send.call_args_list[2].kwargs["text"],
        )
        s3.get_object.assert_has_calls(
            [
                call(
                    Bucket="bucket",
                    Key="equipment-reports/T-V2-REPORT.json",
                ),
                call(
                    Bucket="bucket",
                    Key="equipment-tasks/T-V2-REPORT.json",
                ),
            ]
        )

    @patch("index.requests.post")
    def test_send_file_message_uses_existing_max_protocol(self, mock_post):
        init_response = MagicMock()
        init_response.json.return_value = {"url": "https://upload.example"}
        upload_response = MagicMock()
        upload_response.json.return_value = {"token": "file-token"}
        final_response = MagicMock()
        final_response.status_code = 200
        mock_post.side_effect = [
            init_response,
            upload_response,
            final_response,
        ]

        result = index.send_file_message(
            token="token",
            chat_id="chat",
            text="message",
            file_name="file.png",
            file_bytes=b"png",
        )

        self.assertTrue(result)
        self.assertEqual(mock_post.call_count, 3)


if __name__ == "__main__":
    unittest.main()
