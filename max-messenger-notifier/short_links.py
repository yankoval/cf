"""Private, write-once S3 registry and public, read-only download redirects.

Never log events, registry keys, URLs, exception details or response headers.
The public function uses handler, not index.task_handler.
"""
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
import re
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

BUCKET = "1bf11148-3595-4a07-a089-d460153b7c7a"
ENDPOINT = "https://storage.yandexcloud.net"
REGISTRY_PREFIX = "_max-short-links/v1/"
TTL = 86400
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32}")
KEY_RE = re.compile(r"equipment-tasks/T-[A-Za-z0-9_-]+\.json")
logger = logging.getLogger(__name__)
_client = None


class LinkError(RuntimeError):
    """Safe to surface to Cloud Functions without credentials or URLs."""


def get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3", endpoint_url=ENDPOINT, region_name="ru-central1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          connect_timeout=5, read_timeout=10,
                          retries={"mode": "standard", "total_max_attempts": 2}),
        )
    return _client


def validate_target(bucket, key):
    if bucket != BUCKET or not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise LinkError("Unexpected short-link target")


def _settings():
    base = os.environ.get("MAX_SHORT_LINK_BASE_URL", "")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or parsed.port not in (None, 443)
            or re.search(r"[\s\\]", base)):
        raise LinkError("Invalid short-link base URL")
    # A pinned Cloud Function URL may contain ?tag=production-stable.
    if parsed.query and parse_qs(parsed.query, strict_parsing=True) != {"tag": ["production-stable"]}:
        raise LinkError("Invalid short-link base query")
    secret = os.environ.get("MAX_SHORT_LINK_HMAC_KEY", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", secret):
        raise LinkError("Short-link HMAC key must contain 32 random bytes in hex")
    return base, bytes.fromhex(secret)


def _record_key(token):
    return REGISTRY_PREFIX + token + ".json"


def _read(client, token):
    try:
        obj = client.get_object(Bucket=BUCKET, Key=_record_key(token))
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    body = obj["Body"]
    try:
        raw = body.read(16385)
    finally:
        body.close()
    if len(raw) > 16384:
        raise LinkError("Invalid link record")
    return json.loads(raw)


def _signed_times(url, key, version):
    """Accept only SDK-generated GET URLs to our exact object and endpoint."""
    if not isinstance(url, str) or re.search(r"[\s\\]", url):
        raise LinkError("Invalid link target URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "storage.yandexcloud.net"
            or parsed.fragment or parsed.path != "/" + BUCKET + "/" + quote(key, safe="/")):
        raise LinkError("Invalid link target URL")
    query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
    required = {"X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date", "X-Amz-Expires",
                "X-Amz-SignedHeaders", "X-Amz-Signature", "response-content-disposition",
                "response-cache-control"}
    allowed = required | {"X-Amz-Security-Token"}
    if version:
        allowed.add("versionId")
        required.add("versionId")
    if not required <= query.keys() or not query.keys() <= allowed or any(len(v) != 1 for v in query.values()):
        raise LinkError("Invalid link target query")
    q = {k: v[0] for k, v in query.items()}
    if (q["X-Amz-Algorithm"] != "AWS4-HMAC-SHA256" or q["X-Amz-SignedHeaders"] != "host"
            or q["X-Amz-Expires"] != str(TTL) or q["response-content-disposition"] != "attachment"
            or q["response-cache-control"] != "no-store"
            or not re.fullmatch(r"[0-9a-f]{64}", q["X-Amz-Signature"])
            or q.get("versionId") != version):
        raise LinkError("Invalid link signature parameters")
    issued = int(datetime.strptime(q["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp())
    return issued, issued + TTL


def _validate(record):
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise LinkError("Invalid link record")
    validate_target(record.get("bucket"), record.get("key"))
    if not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
        raise LinkError("Invalid link checksum")
    version = record.get("version_id")
    if version is not None and (not isinstance(version, str) or not version or version == "null"):
        raise LinkError("Invalid object version")
    issued, expires = _signed_times(record.get("url"), record["key"], version)
    if (type(record.get("issued_at")) is not int or type(record.get("expires_at")) is not int
            or record["issued_at"] != issued or record["expires_at"] != expires):
        raise LinkError("Invalid link lifetime")


def create_link(bucket, key, source_bytes, version_id=None):
    """Same object key -> same opaque ID, even after expiry or content changes.

    Keep records as tombstones: deleting records or rotating the HMAC key resets
    idempotency. A registry failure stops sending; the explicit off mode is the
    original long-link fallback, not an automatic renewal of expired links.
    """
    try:
        validate_target(bucket, key)
        base, secret = _settings()
        identity = ("max-task-link-v1\n" + bucket + "\n" + key).encode()
        token = base64.urlsafe_b64encode(hmac.digest(secret, identity, "sha256")[:24]).decode()
        digest = hashlib.sha256(source_bytes).hexdigest()
        client = get_client()
        record = _read(client, token)
        if record is None:
            version = version_id if version_id and version_id != "null" else None
            params = {"Bucket": bucket, "Key": key, "ResponseContentDisposition": "attachment",
                      "ResponseCacheControl": "no-store"}
            if version:
                params["VersionId"] = version
            url = client.generate_presigned_url("get_object", Params=params, ExpiresIn=TTL, HttpMethod="GET")
            issued, expires = _signed_times(url, key, version)
            record = {"schema": 1, "bucket": bucket, "key": key, "sha256": digest,
                      "version_id": version, "issued_at": issued, "expires_at": expires, "url": url}
            try:
                client.put_object(Bucket=BUCKET, Key=_record_key(token),
                                  Body=json.dumps(record, separators=(",", ":")).encode(),
                                  ContentType="application/json", CacheControl="no-store",
                                  IfNoneMatch="*")
            except Exception:
                # Includes concurrent writers and an ambiguous PUT timeout. Read
                # the winner; never overwrite or send an unpersisted URL.
                record = _read(client, token)
                if record is None:
                    raise LinkError("Short-link registry write was not confirmed") from None
        _validate(record)
        if record["bucket"] != bucket or record["key"] != key or record["sha256"] != digest:
            raise LinkError("Short-link source changed; reconcile before sending")
        now = time.time()
        if now < record["issued_at"] or now >= record["expires_at"]:
            raise LinkError("Short-link expired or clock invalid; reconcile before sending")
        separator = "&" if "?" in base else "?"
        return {"url": base + separator + urlencode({"id": token}), "expires_at": record["expires_at"]}
    except LinkError:
        raise
    except Exception:
        raise LinkError("Short-link creation unavailable; no message sent") from None


def _response(status, body="", location=None):
    headers = {"Cache-Control": "no-store, private, max-age=0", "Pragma": "no-cache",
               "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow, noarchive",
               "Content-Type": "text/plain; charset=utf-8"}
    if location:
        headers["Location"] = location
    if status == 405:
        headers["Allow"] = "GET, HEAD"
    if status == 503:
        headers["Retry-After"] = "30"
    return {"statusCode": status, "headers": headers, "body": body, "isBase64Encoded": False}


def handler(event, context):
    """Read-only HTTPS endpoint. It has no MAX credentials or signing logic."""
    head = isinstance(event, dict) and event.get("httpMethod") == "HEAD"
    try:
        if not isinstance(event, dict):
            return _response(400, "Invalid request")
        if event.get("httpMethod") not in ("GET", "HEAD"):
            return _response(405, "Method not allowed")
        query = event.get("queryStringParameters") or {}
        multi = event.get("multiValueQueryStringParameters") or {}
        if not isinstance(query, dict) or not isinstance(multi, dict):
            return _response(400, "" if head else "Invalid request")
        if query.keys() - {"id", "tag"} or ("tag" in query and query["tag"] != "production-stable"):
            return _response(400, "" if head else "Invalid request")
        if any(not isinstance(v, list) or len(v) != 1 for v in multi.values()):
            return _response(400, "" if head else "Invalid request")
        token = query.get("id")
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            return _response(404, "" if head else "Link not found")
        record = _read(get_client(), token)
        if record is None:
            return _response(404, "" if head else "Link not found")
        _validate(record)
        now = time.time()
        if now < record["issued_at"]:
            raise LinkError("Clock invalid")
        if now >= record["expires_at"]:
            return _response(410, "" if head else "Link expired")
        # GET URLs are signed for GET, so a redirected HEAD would fail at S3.
        return _response(200) if head else _response(302, location=record["url"])
    except Exception:
        logger.warning("Short-link resolution unavailable")
        return _response(503, "" if head else "Download temporarily unavailable")
