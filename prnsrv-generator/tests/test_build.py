from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build.py"


def load_build_module():
    spec = importlib.util.spec_from_file_location("prnsrv_generator_build", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildArchiveTests(unittest.TestCase):
    def test_cloud_build_archive_contains_requirements_and_pinned_prnsrv(self):
        module = load_build_module()

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "function.zip"
            args = argparse.Namespace(
                output=output,
                prnsrv_source=None,
                prnsrv_requirement=None,
                skip_third_party=True,
                python_version="314",
                target_platform="manylinux2014_x86_64",
            )

            def fake_install(command: list[str]) -> None:
                target = Path(command[command.index("--target") + 1])
                package = target / "prnsrv"
                package.mkdir()
                (package / "__init__.py").write_text("", encoding="utf-8")

            with mock.patch.object(module, "run", side_effect=fake_install):
                module.build(args)

            self.assertLess(output.stat().st_size, 3_500_000)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                self.assertIn("requirements.txt", names)
                self.assertIn("prnsrv/__init__.py", names)
                self.assertNotIn("boto3/__init__.py", names)
                manifest = json.loads(archive.read("build-manifest.json"))

            self.assertEqual(manifest["dependency_mode"], "cloud-build")
            self.assertEqual(
                manifest["prnsrv"]["requirement"],
                module.DEFAULT_PRNSRV_REQUIREMENT,
            )


if __name__ == "__main__":
    unittest.main()
