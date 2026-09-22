"""Narrow S3 adapter with immutable-write guarantees."""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

from botocore.exceptions import ClientError


class ObjectAlreadyExists(Exception):
    """A conditional create lost the race to an existing object."""


class ImmutableObjectConflict(Exception):
    """An existing object has different content."""


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    etag: str | None
    sha256: str | None
    size: int | None
    last_modified: datetime | None


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _is_precondition_failure(error: ClientError) -> bool:
    response = error.response or {}
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = str(response.get("Error", {}).get("Code", ""))
    return status == 412 or code in {"PreconditionFailed", "412"}


class S3Storage:
    def __init__(self, client: Any) -> None:
        self.client = client

    def get_bytes(self, bucket: str, key: str) -> bytes:
        response = self.client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        if isinstance(body, (bytes, bytearray)):
            return bytes(body)
        return body.read()

    def get_json(self, bucket: str, key: str) -> dict[str, Any]:
        try:
            value = json.loads(self.get_bytes(bucket, key).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Object {key} does not contain valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Object {key} must contain a JSON object")
        return value

    def get_json_optional(self, bucket: str, key: str) -> dict[str, Any] | None:
        try:
            return self.get_json(bucket, key)
        except ClientError as exc:
            response = exc.response or {}
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = str(response.get("Error", {}).get("Code", ""))
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

    def head_optional(self, bucket: str, key: str) -> ObjectInfo | None:
        try:
            response = self.client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            error_response = exc.response or {}
            status = error_response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = str(error_response.get("Error", {}).get("Code", ""))
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        metadata = {str(key).lower(): str(value) for key, value in response.get("Metadata", {}).items()}
        return ObjectInfo(
            key=key,
            etag=str(response.get("ETag", "")).strip('"') or None,
            sha256=metadata.get("sha256"),
            size=response.get("ContentLength"),
            last_modified=response.get("LastModified"),
        )

    def actual_sha256(self, bucket: str, key: str) -> str | None:
        info = self.head_optional(bucket, key)
        if info is None:
            return None
        if info.sha256:
            return info.sha256
        return sha256_hex(self.get_bytes(bucket, key))

    def put_immutable(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str,
        extra_metadata: dict[str, str] | None = None,
    ) -> ObjectInfo:
        expected_sha256 = sha256_hex(body)
        metadata = {"sha256": expected_sha256}
        metadata.update(extra_metadata or {})
        try:
            response = self.client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                Metadata=metadata,
                IfNoneMatch="*",
            )
            return ObjectInfo(
                key=key,
                etag=str(response.get("ETag", "")).strip('"') or None,
                sha256=expected_sha256,
                size=len(body),
                last_modified=None,
            )
        except ClientError as exc:
            if not _is_precondition_failure(exc):
                raise

        actual_sha256 = self.actual_sha256(bucket, key)
        if actual_sha256 != expected_sha256:
            raise ImmutableObjectConflict(
                f"Existing object {key} has sha256={actual_sha256}; expected {expected_sha256}"
            )
        info = self.head_optional(bucket, key)
        if info is None:
            raise RuntimeError(f"Object {key} disappeared after a conditional conflict")
        return ObjectInfo(
            key=key,
            etag=info.etag,
            sha256=expected_sha256,
            size=info.size,
            last_modified=info.last_modified,
        )

    def put_create_only(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> ObjectInfo:
        try:
            response = self.client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                Metadata=metadata or {},
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if _is_precondition_failure(exc):
                raise ObjectAlreadyExists(key) from exc
            raise
        return ObjectInfo(
            key=key,
            etag=str(response.get("ETag", "")).strip('"') or None,
            sha256=sha256_hex(body),
            size=len(body),
            last_modified=None,
        )

    def list_objects(self, bucket: str, prefix: str) -> Iterator[dict[str, Any]]:
        continuation_token = None
        while True:
            request: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if continuation_token:
                request["ContinuationToken"] = continuation_token
            response = self.client.list_objects_v2(**request)
            yield from response.get("Contents", [])
            if not response.get("IsTruncated"):
                return
            continuation_token = response.get("NextContinuationToken")
            if not continuation_token:
                raise RuntimeError("Truncated ListObjectsV2 response has no continuation token")


def json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
