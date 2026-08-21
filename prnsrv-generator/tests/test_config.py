from __future__ import annotations

import os
import unittest
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


if __name__ == "__main__":
    unittest.main()
