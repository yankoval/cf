from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from config import Settings


class SettingsTests(unittest.TestCase):
    def test_allocator_url_has_no_hidden_default(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertIsNone(settings.sscc_url)

    def test_allocator_url_comes_from_environment(self):
        configured_url = "https://functions.yandexcloud.net/configured-allocator"
        with patch.dict(os.environ, {"SSCC_URL": configured_url}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(configured_url, settings.sscc_url)

    def test_reconcile_cutover_timestamp_comes_from_environment(self):
        with patch.dict(
            os.environ,
            {"RECONCILE_NOT_BEFORE": "2026-08-21T15:58:00Z"},
            clear=True,
        ):
            settings = Settings.from_env()

        self.assertEqual(
            datetime(2026, 8, 21, 15, 58, tzinfo=timezone.utc),
            settings.reconcile_not_before,
        )

    def test_reconcile_cutover_timestamp_requires_timezone(self):
        with patch.dict(
            os.environ,
            {"RECONCILE_NOT_BEFORE": "2026-08-21T15:58:00"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "must include a timezone"):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
