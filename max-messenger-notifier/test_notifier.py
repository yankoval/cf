import io
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch

from botocore.exceptions import ClientError


sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import index


class TestNotifier(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "MAX_BOT_TOKEN": "test_token",
                "MAX_CHAT_ID": "test_chat",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

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
        self.v1_task = {
            "id": "T-V1-REPORT",
            "reportSchemaVersion": 1,
            "palletNumbers": ["046070517921585754"],
        }

    @staticmethod
    def s3_body(data):
        body = MagicMock()
        body.read.return_value = json.dumps(data).encode("utf-8")
        return {"Body": body}

    @staticmethod
    def missing_s3_object():
        return ClientError(
            {
                "Error": {"Code": "NoSuchKey", "Message": "Not Found"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "GetObject",
        )

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
        self.assertEqual(
            info["pallet_details"],
            [
                {
                    "pallet_number": "046070517921585754",
                    "boxes": 2,
                    "products": 3,
                },
                {
                    "pallet_number": "046070517921585761",
                    "boxes": 1,
                    "products": 3,
                },
            ],
        )
        self.assertEqual(info["boxes"], 3)
        self.assertEqual(info["products"], 6)

    def test_extract_v2_requires_closed_boxes(self):
        report = json.loads(json.dumps(self.v2_report))
        report["readyPallet"][0]["readyBox"][0]["boxAgregate"] = False

        with self.assertRaisesRegex(
            ValueError,
            "boxAgregate должен быть true",
        ):
            index.extract_report_info(report)

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
            operator="operator-v2",
            pallet_number="046070517921585754",
            pallet_index=0,
            pallet_count=2,
            boxes_count=2,
            products_count=3,
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

    def test_task_assignment_rejects_task_id_mismatch(self):
        s3 = MagicMock()
        s3.get_object.return_value = self.s3_body(
            {
                **self.v2_task,
                "id": "OTHER-REPORT",
            }
        )
        info = index.extract_report_info(self.v2_report)

        with self.assertRaisesRegex(
            ValueError,
            "ID задания 'OTHER-REPORT' не совпадает",
        ):
            index.validate_task_pallet_assignment(
                s3,
                "bucket",
                info,
            )

    def test_validate_report_identity_rejects_v2_file_name_mismatch(self):
        info = index.extract_report_info(self.v2_report)

        with self.assertRaisesRegex(
            ValueError,
            "не совпадает с именем объекта",
        ):
            index.validate_report_identity(
                "equipment-reports/OTHER-REPORT.json",
                info,
            )

    @patch("index.send_file_message", return_value=True)
    @patch("index.get_s3_client")
    def test_handler_legacy_v1_task_keeps_one_summary_message(
        self,
        mock_get_s3,
        mock_send,
    ):
        s3 = MagicMock()
        s3.get_object.side_effect = [
            self.s3_body(self.v1_report),
            self.s3_body({"id": "T-V1-REPORT"}),
        ]
        mock_get_s3.return_value = s3

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
        self.assertEqual(
            mock_send.call_args.kwargs["text"],
            "\n".join(
                [
                    "Отчёт оборудования",
                    "ID: T-V1-REPORT",
                    "Оператор: operator-v1",
                    "Количество коробок: 2",
                    "Количество продуктов: 6",
                    "Номера коробов: 1513538, 1513559",
                ]
            ),
        )
        self.assertEqual(
            mock_send.call_args.kwargs["file_name"],
            "report_T-V1-REPORT.png",
        )
        self.assertTrue(
            mock_send.call_args.kwargs["file_bytes"].startswith(
                b"\x89PNG\r\n\x1a\n"
            )
        )
        s3.get_object.assert_has_calls(
            [
                call(
                    Bucket="bucket",
                    Key="equipment-reports/T-V1-REPORT.json",
                ),
                call(
                    Bucket="bucket",
                    Key="equipment-tasks/T-V1-REPORT.json",
                ),
            ]
        )

    @patch("index.create_pallet_label_image", return_value=b"pallet-png")
    @patch("index.create_info_image", return_value=b"summary-png")
    @patch("index.send_file_message", return_value=True)
    @patch("index.get_s3_client")
    def test_handler_v1_new_task_sends_single_pallet_label(
        self,
        mock_get_s3,
        mock_send,
        mock_create_info,
        mock_create_label,
    ):
        s3 = MagicMock()
        s3.get_object.side_effect = [
            self.s3_body(self.v1_report),
            self.s3_body(self.v1_task),
        ]
        mock_get_s3.return_value = s3

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
        self.assertEqual(mock_send.call_count, 2)
        self.assertIn(
            "Количество паллетов: 1",
            mock_send.call_args_list[0].kwargs["text"],
        )
        self.assertIn(
            "SSCC: 046070517921585754",
            mock_send.call_args_list[1].kwargs["text"],
        )
        mock_create_label.assert_called_once_with(
            report_id="T-V1-REPORT",
            operator="operator-v1",
            pallet_number="046070517921585754",
            pallet_index=0,
            pallet_count=1,
            boxes_count=2,
            products_count=6,
        )

    @patch("index.send_file_message", return_value=True)
    @patch("index.get_s3_client")
    def test_handler_v1_without_task_keeps_legacy_summary(
        self,
        mock_get_s3,
        mock_send,
    ):
        s3 = MagicMock()
        s3.get_object.side_effect = [
            self.s3_body(self.v1_report),
            self.missing_s3_object(),
        ]
        mock_get_s3.return_value = s3

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
        self.assertNotIn(
            "Количество паллетов",
            mock_send.call_args.kwargs["text"],
        )

    def test_task_assignment_rejects_multiple_pallets_for_v1(self):
        s3 = MagicMock()
        s3.get_object.return_value = self.s3_body(
            {
                **self.v1_task,
                "palletNumbers": [
                    "046070517921585754",
                    "046070517921585761",
                ],
            }
        )

        with self.assertRaisesRegex(ValueError, "ровно один"):
            index.validate_task_pallet_assignment(
                s3,
                "bucket",
                index.extract_report_info(self.v1_report),
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
            "Оператор: operator-v2",
            mock_send.call_args_list[1].kwargs["text"],
        )
        self.assertIn(
            "Коробов: 2",
            mock_send.call_args_list[1].kwargs["text"],
        )
        self.assertIn(
            "Штук: 3",
            mock_send.call_args_list[1].kwargs["text"],
        )
        self.assertIn(
            "SSCC: 046070517921585761",
            mock_send.call_args_list[2].kwargs["text"],
        )
        mock_create_label.assert_has_calls(
            [
                call(
                    report_id="T-V2-REPORT",
                    operator="operator-v2",
                    pallet_number="046070517921585754",
                    pallet_index=0,
                    pallet_count=2,
                    boxes_count=2,
                    products_count=3,
                ),
                call(
                    report_id="T-V2-REPORT",
                    operator="operator-v2",
                    pallet_number="046070517921585761",
                    pallet_index=1,
                    pallet_count=2,
                    boxes_count=1,
                    products_count=3,
                ),
            ]
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

    @patch("index.send_file_message")
    @patch("index.get_s3_client")
    def test_handler_rejects_v2_pallet_not_assigned_by_task(
        self,
        mock_get_s3,
        mock_send,
    ):
        s3 = MagicMock()
        s3.get_object.side_effect = [
            self.s3_body(self.v2_report),
            self.s3_body(
                {
                    **self.v2_task,
                    "palletNumbers": ["046070517921585778"],
                }
            ),
        ]
        mock_get_s3.return_value = s3

        result = index.handler(
            {
                "messages": [
                    {
                        "details": {
                            "bucket_id": "bucket",
                            "object_id": (
                                "equipment-reports/T-V2-REPORT.json"
                            ),
                        }
                    }
                ]
            },
            None,
        )

        self.assertEqual(result["statusCode"], 200)
        mock_send.assert_not_called()

    @patch("index.create_pallet_label_image")
    @patch("index.send_file_message", return_value=False)
    @patch("index.get_s3_client")
    def test_handler_does_not_send_labels_when_summary_failed(
        self,
        mock_get_s3,
        mock_send,
        mock_create_label,
    ):
        s3 = MagicMock()
        s3.get_object.side_effect = [
            self.s3_body(self.v2_report),
            self.s3_body(self.v2_task),
        ]
        mock_get_s3.return_value = s3

        result = index.handler(
            {
                "messages": [
                    {
                        "details": {
                            "bucket_id": "bucket",
                            "object_id": (
                                "equipment-reports/T-V2-REPORT.json"
                            ),
                        }
                    }
                ]
            },
            None,
        )

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(mock_send.call_count, 1)
        mock_create_label.assert_not_called()

    @patch("index.create_pallet_label_image", return_value=b"pallet-png")
    @patch("index.create_info_image", return_value=b"summary-png")
    @patch(
        "index.send_file_message",
        side_effect=[True, RuntimeError("upload failed"), True],
    )
    @patch("index.get_s3_client")
    def test_handler_continues_labels_after_one_label_exception(
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

        result = index.handler(
            {
                "messages": [
                    {
                        "details": {
                            "bucket_id": "bucket",
                            "object_id": (
                                "equipment-reports/T-V2-REPORT.json"
                            ),
                        }
                    }
                ]
            },
            None,
        )

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(mock_send.call_count, 3)
        self.assertEqual(mock_create_info.call_count, 1)
        self.assertEqual(mock_create_label.call_count, 2)
        self.assertIn(
            "SSCC: 046070517921585754",
            mock_send.call_args_list[1].kwargs["text"],
        )
        self.assertIn(
            "SSCC: 046070517921585761",
            mock_send.call_args_list[2].kwargs["text"],
        )

    @patch("index.send_file_message", return_value=True)
    @patch("index.get_s3_client")
    def test_handler_continues_after_invalid_report_in_same_event(
        self,
        mock_get_s3,
        mock_send,
    ):
        invalid_report = {
            "id": "INVALID",
            "readyBox": [
                {
                    "boxNumber": "046070517915135385",
                    "productNumbersFull": "not-an-array",
                }
            ],
        }
        s3 = MagicMock()
        s3.get_object.side_effect = [
            self.s3_body(invalid_report),
            self.s3_body(self.v1_report),
            self.s3_body({"id": "T-V1-REPORT"}),
        ]
        mock_get_s3.return_value = s3

        result = index.handler(
            {
                "messages": [
                    {
                        "details": {
                            "bucket_id": "bucket",
                            "object_id": (
                                "equipment-reports/INVALID.json"
                            ),
                        }
                    },
                    {
                        "details": {
                            "bucket_id": "bucket",
                            "object_id": (
                                "equipment-reports/T-V1-REPORT.json"
                            ),
                        }
                    },
                ]
            },
            None,
        )

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(mock_send.call_count, 1)
        self.assertIn(
            "ID: T-V1-REPORT",
            mock_send.call_args.kwargs["text"],
        )

    @patch.dict(os.environ, {}, clear=True)
    @patch("index.get_s3_client")
    def test_handler_returns_configuration_error_without_max_settings(
        self,
        mock_get_s3,
    ):
        result = index.handler({"messages": []}, None)

        self.assertEqual(
            result,
            {"statusCode": 500, "body": "Configuration error"},
        )
        mock_get_s3.assert_not_called()

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

    @patch("index.time.sleep")
    @patch("index.requests.post")
    def test_send_file_message_retries_until_attachment_ready(
        self,
        mock_post,
        mock_sleep,
    ):
        init_response = MagicMock()
        init_response.json.return_value = {"url": "https://upload.example"}
        upload_response = MagicMock()
        upload_response.json.return_value = {"token": "file-token"}
        pending_response = MagicMock()
        pending_response.status_code = 409
        pending_response.json.return_value = {
            "code": "attachment.not.ready",
        }
        final_response = MagicMock()
        final_response.status_code = 200
        mock_post.side_effect = [
            init_response,
            upload_response,
            pending_response,
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
        self.assertEqual(mock_post.call_count, 4)
        mock_sleep.assert_called_once_with(2)

    @patch("index.time.sleep")
    @patch("index.requests.post")
    def test_send_file_message_stops_on_terminal_max_error(
        self,
        mock_post,
        mock_sleep,
    ):
        init_response = MagicMock()
        init_response.json.return_value = {"url": "https://upload.example"}
        upload_response = MagicMock()
        upload_response.json.return_value = {"token": "file-token"}
        error_response = MagicMock()
        error_response.status_code = 400
        error_response.json.return_value = {"code": "invalid.request"}
        error_response.text = "invalid request"
        mock_post.side_effect = [
            init_response,
            upload_response,
            error_response,
        ]

        result = index.send_file_message(
            token="token",
            chat_id="chat",
            text="message",
            file_name="file.png",
            file_bytes=b"png",
        )

        self.assertFalse(result)
        self.assertEqual(mock_post.call_count, 3)
        mock_sleep.assert_not_called()

    @patch("index.requests.post")
    def test_send_file_message_rejects_upload_without_token(
        self,
        mock_post,
    ):
        init_response = MagicMock()
        init_response.json.return_value = {"url": "https://upload.example"}
        upload_response = MagicMock()
        upload_response.json.return_value = {}
        mock_post.side_effect = [init_response, upload_response]

        with self.assertRaisesRegex(
            ValueError,
            "MAX не вернул token",
        ):
            index.send_file_message(
                token="token",
                chat_id="chat",
                text="message",
                file_name="file.png",
                file_bytes=b"png",
            )


if __name__ == "__main__":
    unittest.main()
