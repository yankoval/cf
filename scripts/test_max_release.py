import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import max_release as release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.commit = "a" * 40
        self.sources = {"index.py": b"pass\n", "requirements.txt": b"", "DejaVuSans.ttf": b"font"}

    def test_archive_is_reproducible_and_has_only_runtime_files(self):
        first, manifest = release.package_bytes(self.commit, self.sources)
        second, _ = release.package_bytes(self.commit, dict(reversed(list(self.sources.items()))))
        self.assertEqual(first, second)
        self.assertEqual(release.sha256(first), manifest["package_sha256"])
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            self.assertEqual(set(archive.namelist()), {*self.sources, "build-info.json"})
            self.assertEqual(json.loads(archive.read("build-info.json"))["commit"], self.commit)

    def test_modified_package_is_rejected_before_cloud_access(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            package, manifest = release.package_bytes(self.commit, self.sources)
            (output / "max-notifier.zip").write_bytes(package + b"tampered")
            (output / "manifest.json").write_bytes(release.json_bytes(manifest))
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                release.verify_package(output, self.commit)

    def test_artifact_for_another_commit_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            package, manifest = release.package_bytes(self.commit, self.sources)
            (output / "max-notifier.zip").write_bytes(package)
            (output / "manifest.json").write_bytes(release.json_bytes(manifest))
            with self.assertRaisesRegex(RuntimeError, "commit or package"):
                release.verify_package(output, "b" * 40)

    def test_changed_production_version_stops_promotion(self):
        class Cloud:
            def stable(self):
                return {"id": "unexpected", "functionId": release.FUNCTION_ID}
        with self.assertRaisesRegex(RuntimeError, "changed"):
            release.require_current(Cloud(), "expected")

    def test_pending_operation_uses_yandex_operation_service(self):
        finished = {"id": "operation-id", "done": True, "response": {"id": "version-id"}}
        with patch.object(release, "request_json", return_value=finished) as request, \
             patch.object(release.time, "sleep"):
            result = release.Cloud("fake-token").operation({"id": "operation-id", "done": False})
        self.assertEqual(result, {"id": "version-id"})
        request.assert_called_once_with("https://operation.api.cloud.yandex.net/operations/operation-id", token="fake-token")

    def test_pr_cannot_request_cloud_identity(self):
        with patch.dict(release.os.environ, {"GITHUB_REPOSITORY": "yankoval/cf", "GITHUB_EVENT_NAME": "pull_request"}):
            with self.assertRaisesRegex(RuntimeError, "workflow dispatch"):
                release.github_token()

    def test_other_branch_cannot_request_cloud_identity(self):
        with patch.dict(release.os.environ, {"GITHUB_REPOSITORY": "yankoval/cf", "GITHUB_EVENT_NAME": "workflow_dispatch",
                                           "GITHUB_REF": "refs/heads/untrusted", "DEFAULT_BRANCH": "main"}):
            with self.assertRaisesRegex(RuntimeError, "default branch"):
                release.github_token()

    def test_configuration_preserves_secrets_but_excludes_version_metadata(self):
        previous = {"id": "old", "description": "old release", "tags": ["production-stable"],
                    "runtime": "python314", "environment": {"TOKEN": "fake-secret"},
                    "secrets": [{"id": "fake-lockbox", "versionId": "version"}],
                    "resources": {"memory": "268435456"}, "concurrency": "1"}
        copied = release.config(previous)
        self.assertEqual(copied["environment"], previous["environment"])
        self.assertEqual(copied["secrets"], previous["secrets"])
        self.assertEqual(copied["resources"], previous["resources"])
        self.assertNotIn("id", copied)
        self.assertNotIn("description", copied)
        self.assertNotIn("tags", copied)

    def exercise_deploy(self, smoke_error=None, changed_config=False):
        previous = {"id": "d" * 20, "functionId": release.FUNCTION_ID,
                    "runtime": "python314", "entrypoint": "index.handler",
                    "resources": {"memory": "268435456"}, "environment": {"TOKEN": "fake-secret"}}
        candidate = dict(previous, id="e" * 20)
        if changed_config:
            candidate["resources"] = {"memory": "134217728"}
        cloud = MagicMock()
        cloud.stable.side_effect = [previous, previous, candidate]
        cloud.call.side_effect = [{"id": "operation-id"}, candidate]
        cloud.operation.return_value = candidate
        cloud.smoke.side_effect = smoke_error
        package, manifest = release.package_bytes(self.commit, self.sources)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.object(release, "git", side_effect=[self.commit, ""]), \
                 patch.object(release, "verify_package", return_value=(package, manifest)), \
                 patch.object(release, "github_token", return_value="fake-token"), \
                 patch.object(release, "Cloud", return_value=cloud), \
                 patch.dict(release.os.environ, {"GITHUB_SHA": self.commit, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}):
                if smoke_error or changed_config:
                    with self.assertRaises(RuntimeError):
                        release.deploy(output, previous["id"])
                    cloud.set_tag.assert_not_called()
                else:
                    release.deploy(output, previous["id"])
                    self.assertEqual(cloud.set_tag.call_args_list[-1].args, (candidate["id"], "production-stable"))
                record_text = (output / "deployment.json").read_text()
                self.assertNotIn("fake-secret", record_text)
                self.assertNotIn("fake-token", record_text)
                record = json.loads(record_text)
                return record

    def test_failed_smoke_keeps_production_unchanged(self):
        record = self.exercise_deploy(smoke_error=RuntimeError("failed smoke"))
        self.assertEqual(record["status"], "candidate")

    def test_changed_config_keeps_production_unchanged(self):
        record = self.exercise_deploy(changed_config=True)
        self.assertEqual(record["status"], "started")

    def test_successful_release_records_provenance_without_secrets(self):
        record = self.exercise_deploy()
        self.assertEqual(record["status"], "production")
        self.assertEqual(record["manifest"]["commit"], self.commit)


if __name__ == "__main__":
    unittest.main()
