import copy
from datetime import datetime, timezone
import io
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import short_links as links

KEY = "equipment-tasks/T-test.json"
SOURCE = b'{ "id": "T-test", "original": [1, 2] }\r\n'
NOW = 1790348400


def missing():
    return ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")


class MemoryS3:
    """Atomic fake storage with real botocore SigV4 URL generation."""
    def __init__(self):
        self.records = {}
        self.writes = 0
        self.signs = 0
        self.lock = threading.Lock()
        self.signer = boto3.client("s3", endpoint_url=links.ENDPOINT,
                                   aws_access_key_id="fake-key", aws_secret_access_key="fake-secret",
                                   region_name="ru-central1",
                                   config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))

    def get_object(self, *, Bucket, Key):
        assert Bucket == links.BUCKET
        assert Key.startswith(links.REGISTRY_PREFIX)
        with self.lock:
            if Key not in self.records:
                raise missing()
            return {"Body": io.BytesIO(self.records[Key])}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, **kwargs):
        assert Bucket == links.BUCKET and Key.startswith(links.REGISTRY_PREFIX)
        assert IfNoneMatch == "*"
        with self.lock:
            if Key in self.records:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            self.records[Key] = Body
            self.writes += 1

    def generate_presigned_url(self, *args, **kwargs):
        self.signs += 1
        return self.signer.generate_presigned_url(*args, **kwargs)


class ShortLinkTests(unittest.TestCase):
    def setUp(self):
        self.s3 = MemoryS3()
        for p in (
            patch.dict(os.environ, {"MAX_SHORT_LINK_BASE_URL": "https://download.example/get",
                                    "MAX_SHORT_LINK_HMAC_KEY": "5a" * 32}),
            patch.object(links, "get_client", return_value=self.s3),
            patch.object(links.time, "time", return_value=NOW),
            patch("botocore.auth.get_current_datetime", return_value=datetime.fromtimestamp(NOW, timezone.utc)),
        ):
            p.start()
            self.addCleanup(p.stop)

    def create(self, source=SOURCE, key=KEY, version=None):
        return links.create_link(links.BUCKET, key, source, version)

    def event(self, link=None, method="GET"):
        link = link or self.create()
        token = parse_qs(urlsplit(link["url"]).query)["id"][0]
        return {"httpMethod": method, "queryStringParameters": {"id": token}}

    def record(self):
        return json.loads(next(iter(self.s3.records.values())))

    def replace_record(self, record):
        self.s3.records[next(iter(self.s3.records))] = json.dumps(record).encode()

    def test_repeat_is_same_id_and_fixed_expiry_without_resigning(self):
        first = self.create()
        with patch.object(links.time, "time", return_value=NOW + 5000):
            self.assertEqual(first, self.create())
        self.assertEqual(first["expires_at"], NOW + 86400)
        self.assertEqual((self.s3.writes, self.s3.signs), (1, 1))
        self.assertRegex(parse_qs(urlsplit(first["url"]).query)["id"][0], r"^[A-Za-z0-9_-]{32}$")
        self.assertNotIn("T-test", first["url"])

    def test_other_objects_have_different_opaque_ids(self):
        self.assertNotEqual(self.create()["url"], self.create(key="equipment-tasks/T-second.json")["url"])

    def test_concurrent_creation_returns_single_winner(self):
        original = self.s3.get_object
        barrier = threading.Barrier(8)
        def concurrent_read(**kwargs):
            try:
                return original(**kwargs)
            except ClientError:
                barrier.wait(timeout=5)
                raise
        with patch.object(self.s3, "get_object", side_effect=concurrent_read):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.create(), range(8)))
        self.assertEqual(len({r["url"] for r in results}), 1)
        self.assertEqual(self.s3.writes, 1)

    def test_expiry_boundary_never_renews(self):
        event = self.event()
        with patch.object(links.time, "time", return_value=NOW + 86399):
            self.assertEqual(links.handler(event, None)["statusCode"], 302)
        with patch.object(links.time, "time", return_value=NOW + 86400):
            self.assertEqual(links.handler(event, None)["statusCode"], 410)
            with self.assertRaisesRegex(links.LinkError, "expired"):
                self.create()
        self.assertEqual((self.s3.writes, self.s3.signs), (1, 1))

    def test_changed_source_does_not_replace_existing_record(self):
        self.create()
        before = copy.deepcopy(self.s3.records)
        with self.assertRaisesRegex(links.LinkError, "source changed"):
            self.create(source=SOURCE + b" ")
        self.assertEqual(before, self.s3.records)

    def test_lost_put_acknowledgement_reads_committed_record(self):
        original = self.s3.put_object
        def lost_ack(**kwargs):
            original(**kwargs)
            raise TimeoutError("sensitive URL must never surface")
        with patch.object(self.s3, "put_object", side_effect=lost_ack):
            self.assertEqual(self.create()["expires_at"], NOW + 86400)

    def test_unconfirmed_put_fails_without_a_link(self):
        with patch.object(self.s3, "put_object", side_effect=TimeoutError("sensitive")):
            with self.assertRaisesRegex(links.LinkError, "not confirmed") as error:
                self.create()
        self.assertNotIn("sensitive", str(error.exception))
        self.assertFalse(self.s3.records)

    def test_access_denied_is_not_treated_as_missing(self):
        with patch.object(self.s3, "get_object", side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")):
            with self.assertRaises(links.LinkError):
                self.create()
        self.assertEqual(self.s3.signs, 0)

    def test_unknown_and_malformed_ids_do_not_redirect(self):
        for token in ("a" * 32, "../secret", "", "a" * 1000):
            response = links.handler({"httpMethod": "GET", "queryStringParameters": {"id": token}}, None)
            self.assertEqual(response["statusCode"], 404)
            self.assertNotIn("Location", response["headers"])

    def test_public_endpoint_has_no_creation_or_open_redirect(self):
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(links.handler({"httpMethod": method}, None)["statusCode"], 405)
        event = self.event()
        event["queryStringParameters"]["url"] = "https://evil.example"
        self.assertEqual(links.handler(event, None)["statusCode"], 400)
        event = self.event()
        event["multiValueQueryStringParameters"] = {"id": ["a" * 32, "b" * 32]}
        self.assertEqual(links.handler(event, None)["statusCode"], 400)

    def test_redirect_and_head_do_not_sign_or_write(self):
        event = self.event()
        response = links.handler(event, None)
        self.assertEqual(response["statusCode"], 302)
        self.assertEqual(response["headers"]["Location"], self.record()["url"])
        self.assertIn("no-store", response["headers"]["Cache-Control"])
        self.assertEqual(response["headers"]["Referrer-Policy"], "no-referrer")
        event["httpMethod"] = "HEAD"
        head = links.handler(event, None)
        self.assertEqual(head["statusCode"], 200)
        self.assertEqual(head["body"], "")
        self.assertNotIn("Location", head["headers"])
        self.assertEqual((self.s3.writes, self.s3.signs), (1, 1))

    def test_tampered_record_is_never_redirected(self):
        event = self.event()
        good = self.record()
        bad_urls = [
            good["url"].replace("storage.yandexcloud.net", "evil.example"),
            good["url"].replace("https:", "http:"),
            good["url"] + "&url=https://evil.example",
            good["url"] + "&X-Amz-Expires=86400",
            good["url"].replace("X-Amz-Expires=86400", "X-Amz-Expires=172800"),
            good["url"].replace("T-test.json", "T-other.json"),
            good["url"] + "\r\nLocation: https://evil.example",
        ]
        for url in bad_urls:
            with self.subTest(url_type=bad_urls.index(url)):
                self.replace_record(dict(good, url=url))
                result = links.handler(event, None)
                self.assertEqual(result["statusCode"], 503)
                self.assertNotIn("Location", result["headers"])
        for changes in ({"expires_at": NOW + 172800}, {"bucket": "wrong"}, {"key": "Задания/T-test.json"}):
            self.replace_record(dict(good, **changes))
            self.assertEqual(links.handler(event, None)["statusCode"], 503)

    def test_future_record_is_unavailable(self):
        event = self.event()
        with patch.object(links.time, "time", return_value=NOW - 1):
            self.assertEqual(links.handler(event, None)["statusCode"], 503)

    def test_registry_errors_and_corrupt_json_are_redacted(self):
        event = self.event()
        token = event["queryStringParameters"]["id"]
        url = self.record()["url"]
        with patch.object(self.s3, "get_object", side_effect=RuntimeError(url + token)):
            with self.assertLogs(links.logger, level="WARNING") as logs:
                result = links.handler(event, None)
            self.assertEqual(result["statusCode"], 503)
            for value in (url, token, "fake-secret"):
                self.assertNotIn(value, str(result) + str(logs.output))
            with self.assertRaises(links.LinkError) as error:
                self.create()
            self.assertNotIn(token, str(error.exception))
            self.assertTrue(error.exception.__suppress_context__)
        self.s3.records[next(iter(self.s3.records))] = b"not json"
        self.assertEqual(links.handler(event, None)["statusCode"], 503)

    def test_target_allowlist_before_storage(self):
        for bucket, key in [("wrong", KEY), (links.BUCKET, "Задания/T-test.json"),
                            (links.BUCKET, "equipment-tasks/T-../private.json"),
                            (links.BUCKET, "equipment-tasks/T-a/other.json")]:
            with self.assertRaises(links.LinkError):
                links.create_link(bucket, key, SOURCE)
        self.assertEqual(self.s3.writes, 0)

    def test_config_must_be_secure(self):
        for base in ("http://example.org", "https://user:pass@example.org", "https://example.org/#fragment",
                     "https://example.org/?id=other", "https://example.org/?tag=$latest"):
            with patch.dict(os.environ, {"MAX_SHORT_LINK_BASE_URL": base}):
                with self.assertRaises(links.LinkError):
                    self.create()
        with patch.dict(os.environ, {"MAX_SHORT_LINK_HMAC_KEY": "weak"}):
            with self.assertRaises(links.LinkError):
                self.create()

    def test_pinned_function_base_and_version_id(self):
        with patch.dict(os.environ, {"MAX_SHORT_LINK_BASE_URL": "https://functions.yandexcloud.net/example?tag=production-stable"}):
            link = self.create(version="version-1")
        self.assertIn("?tag=production-stable&id=", link["url"])
        record = self.record()
        self.assertEqual(parse_qs(urlsplit(record["url"]).query)["versionId"], ["version-1"])
        self.assertEqual(links.handler(self.event(link), None)["statusCode"], 302)

    def test_loopback_http_download_preserves_exact_bytes(self):
        # Local transport adapter only rewrites the validated S3 Location to a
        # fixture server. Production handler never accepts this endpoint.
        event = self.event()
        expected_location = self.record()["url"]
        class HTTP(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path.startswith("/short?"):
                    response = links.handler(event, None)
                    self.send_response(response["statusCode"])
                    assert response["headers"]["Location"] == expected_location
                    self.send_header("Location", "/source")
                    self.end_headers()
                elif self.path == "/source":
                    self.send_response(200)
                    self.send_header("Content-Disposition", "attachment")
                    self.end_headers()
                    self.wfile.write(SOURCE)
                else:
                    self.send_error(404)
        server = ThreadingHTTPServer(("127.0.0.1", 0), HTTP)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/short?" + urlencode(event["queryStringParameters"])) as response:
                self.assertEqual(response.read(), SOURCE)
                self.assertEqual(response.headers["Content-Disposition"], "attachment")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
