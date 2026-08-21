from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from config import SSCC_GENERATOR_CI_URL, Settings


class SettingsTests(unittest.TestCase):
    def test_production_default_uses_designated_ci_allocator(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(SSCC_GENERATOR_CI_URL, settings.sscc_url)
        self.assertIn("d4et2pvmtgp0oo5pk0bh", settings.sscc_url)

    def test_isolated_ct_can_override_allocator_url(self):
        ct_url = "https://functions.yandexcloud.net/test-allocator"
        with patch.dict(os.environ, {"SSCC_URL": ct_url}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(ct_url, settings.sscc_url)


if __name__ == "__main__":
    unittest.main()
