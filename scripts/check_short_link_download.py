#!/usr/bin/env python3
"""Read-only real-S3 probe: local redirect, in-memory registry, no MAX calls.

Credentials are taken from the caller's environment, never printed or saved.
Only the source object is read from S3. All registry writes remain in RAM.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import sys
import threading
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "max-messenger-notifier"))
import short_links as links


def check(key):
    links.validate_target(links.BUCKET, key)
    real_s3 = links.get_client()
    obj = real_s3.get_object(Bucket=links.BUCKET, Key=key)
    try:
        source = obj["Body"].read()
    finally:
        obj["Body"].close()
    if json.loads(source).get("id") != key.rsplit("/", 1)[-1][:-5]:
        raise RuntimeError("Source identity mismatch")

    class Registry:
        def __init__(self):
            self.records = {}
        def get_object(self, *, Bucket, Key):
            if Bucket != links.BUCKET or not Key.startswith(links.REGISTRY_PREFIX):
                raise RuntimeError("Unexpected probe registry route")
            if Key not in self.records:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            return {"Body": io.BytesIO(self.records[Key])}
        def put_object(self, *, Bucket, Key, Body, IfNoneMatch, **kwargs):
            if Bucket != links.BUCKET or not Key.startswith(links.REGISTRY_PREFIX) or IfNoneMatch != "*":
                raise RuntimeError("Unexpected probe registry write")
            if Key in self.records:
                raise RuntimeError("Probe registry overwrite")
            self.records[Key] = Body
        def generate_presigned_url(self, *args, **kwargs):
            return real_s3.generate_presigned_url(*args, **kwargs)

    class HTTP(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            response = links.handler({"httpMethod": "GET", "queryStringParameters": {
                k: v[-1] for k, v in query.items()}, "multiValueQueryStringParameters": query}, None)
            self.send_response(response["statusCode"])
            for name, value in response["headers"].items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response["body"].encode())

    with patch.object(links, "get_client", return_value=Registry()), patch.dict(os.environ, {
        "MAX_SHORT_LINK_BASE_URL": "https://local-probe.invalid/get",
        "MAX_SHORT_LINK_HMAC_KEY": secrets.token_hex(32),
    }):
        link = links.create_link(links.BUCKET, key, source, obj.get("VersionId"))
        query = urlsplit(link["url"]).query
        server = ThreadingHTTPServer(("127.0.0.1", 0), HTTP)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            session = requests.Session()
            session.trust_env = False
            with session:
                response = session.get(f"http://127.0.0.1:{server.server_port}/?{query}", timeout=30)
                if response.status_code != 200 or [r.status_code for r in response.history] != [302]:
                    raise RuntimeError("Download probe failed")
                if response.content != source or response.headers.get("Content-Disposition") != "attachment":
                    raise RuntimeError("Download bytes or disposition mismatch")
            token = parse_qs(query)["id"][0]
            with patch.object(links.time, "time", return_value=link["expires_at"]):
                expired = links.handler({"httpMethod": "GET", "queryStringParameters": {"id": token}}, None)
            unknown = links.handler({"httpMethod": "GET", "queryStringParameters": {"id": "z" * 32}}, None)
            if expired["statusCode"] != 410 or unknown["statusCode"] != 404:
                raise RuntimeError("Expiry or unknown-link probe failed")
            return {"result": "passed", "bytes": len(source), "sha256": hashlib.sha256(source).hexdigest(),
                    "redirect_status": 302, "download_status": 200, "expired_status": 410,
                    "unknown_status": 404, "s3_writes": 0, "max_messages": 0,
                    "scope": "local HTTP resolver with real S3 download; registry in memory"}
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    try:
        result = check(args.key)
    except Exception:
        # Request exceptions may include presigned URLs. Do not print traceback.
        print('{"result":"failed","details":"redacted"}')
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
