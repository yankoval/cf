from __future__ import annotations

import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import index
from config import Settings


BUCKET = "test-bucket"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class EphemeralS3CredentialsTests(unittest.TestCase):
    def test_policy_limits_object_access_to_generator_prefixes(self):
        settings = Settings(bucket_id=BUCKET, mapping_key="config/mapping.json")

        policy = index._s3_access_policy(settings)
        statements = {item["Action"]: item for item in policy["Statement"]}

        self.assertEqual(
            f"arn:aws:s3:::{BUCKET}", statements["s3:ListBucket"]["Resource"]
        )
        self.assertEqual(
            {
                f"arn:aws:s3:::{BUCKET}/Задания/*",
                f"arn:aws:s3:::{BUCKET}/config/templates/*",
                f"arn:aws:s3:::{BUCKET}/config/mapping.json",
                f"arn:aws:s3:::{BUCKET}/printer-tasks/*",
                f"arn:aws:s3:::{BUCKET}/_prnsrv/done/*",
            },
            set(statements["s3:GetObject"]["Resource"]),
        )
        self.assertEqual(
            {
                f"arn:aws:s3:::{BUCKET}/printer-tasks/*",
                f"arn:aws:s3:::{BUCKET}/_prnsrv/done/*",
            },
            set(statements["s3:PutObject"]["Resource"]),
        )
        self.assertNotIn("s3:DeleteObject", statements)
        encoded = json.dumps(policy, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(encoded), 2048)

    def test_ephemeral_credentials_are_issued_with_inline_policy(self):
        response = Response(
            json.dumps(
                {
                    "accessKeyId": "temporary-access-key",
                    "secret": "temporary-secret",
                    "sessionToken": "temporary-session-token",
                    "expiresAt": "2026-08-21T16:30:00Z",
                }
            ).encode()
        )
        with patch.object(index.urllib.request, "urlopen", return_value=response) as call:
            credentials = index._issue_ephemeral_credentials(
                iam_token="context-iam-token",
                settings=Settings(bucket_id=BUCKET),
                session_name="prnsrv-request",
            )

        self.assertEqual("temporary-access-key", credentials["access_key_id"])
        request = call.call_args.args[0]
        self.assertEqual("Bearer context-iam-token", request.get_header("Authorization"))
        body = json.loads(request.data)
        self.assertEqual("900s", body["duration"])
        policy = json.loads(body["policy"])
        self.assertEqual("2012-10-17", policy["Version"])

    def test_context_token_is_required(self):
        with self.assertRaisesRegex(RuntimeError, "context IAM token is required"):
            index._context_access_token(SimpleNamespace(token=None))

    def test_cross_bucket_templates_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "one bucket per invocation"):
            index._s3_access_policy(
                Settings(bucket_id=BUCKET, templates_bucket="different-bucket")
            )


if __name__ == "__main__":
    unittest.main()
