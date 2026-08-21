from __future__ import annotations

import hashlib
import io
import json
import threading
from datetime import datetime, timezone

from botocore.exceptions import ClientError
from prnsrv import SSCCAllocation, SSCCError


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self._lock = threading.Lock()

    def seed(self, bucket, key, body, *, metadata=None, last_modified=None, content_type=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        with self._lock:
            self.objects[(bucket, key)] = {
                "body": bytes(body),
                "metadata": dict(metadata or {}),
                "last_modified": last_modified or datetime.now(timezone.utc),
                "content_type": content_type,
                "etag": hashlib.md5(body).hexdigest(),
            }

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get_object", Bucket, Key))
        with self._lock:
            value = self.objects.get((Bucket, Key))
            if value is None:
                self._not_found("GetObject")
            return {"Body": io.BytesIO(value["body"]), "Metadata": dict(value["metadata"])}

    def head_object(self, *, Bucket, Key):
        self.calls.append(("head_object", Bucket, Key))
        with self._lock:
            value = self.objects.get((Bucket, Key))
            if value is None:
                self._not_found("HeadObject")
            return {
                "ETag": f'"{value["etag"]}"',
                "Metadata": dict(value["metadata"]),
                "ContentLength": len(value["body"]),
                "LastModified": value["last_modified"],
            }

    def put_object(self, *, Bucket, Key, Body, ContentType, Metadata, IfNoneMatch):
        self.calls.append(("put_object", Bucket, Key))
        if hasattr(Body, "read"):
            Body = Body.read()
        body = bytes(Body)
        with self._lock:
            if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed", "Message": "exists"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            etag = hashlib.md5(body).hexdigest()
            self.objects[(Bucket, Key)] = {
                "body": body,
                "metadata": dict(Metadata),
                "last_modified": datetime.now(timezone.utc),
                "content_type": ContentType,
                "etag": etag,
            }
            return {"ETag": f'"{etag}"'}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        self.calls.append(("list_objects_v2", Bucket, Prefix))
        with self._lock:
            contents = [
                {
                    "Key": key,
                    "Size": len(value["body"]),
                    "ETag": f'"{value["etag"]}"',
                    "LastModified": value["last_modified"],
                }
                for (bucket, key), value in sorted(self.objects.items())
                if bucket == Bucket and key.startswith(Prefix)
            ]
        return {"Contents": contents, "IsTruncated": False}

    @staticmethod
    def _not_found(operation):
        raise ClientError(
            {
                "Error": {"Code": "NoSuchKey", "Message": "missing"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            operation,
        )

    def json(self, bucket, key):
        return json.loads(self.objects[(bucket, key)]["body"].decode("utf-8"))


class FakeAllocator:
    def __init__(self):
        self.calls = []
        self.allocations = {}
        self._lock = threading.Lock()

    def allocate(self, *, job_uuid, source_hash, count, auth_token=None):
        with self._lock:
            self.calls.append((job_uuid, source_hash, count, auth_token))
            existing = self.allocations.get(job_uuid)
            if existing is not None:
                if existing[0] != source_hash or len(existing[1].ssccs) != count:
                    raise SSCCError("idempotency conflict")
                return existing[1]
            values = tuple(f"{index + 1:018d}" for index in range(count))
            allocation = SSCCAllocation(values, f"allocation-{job_uuid}")
            self.allocations[job_uuid] = (source_hash, allocation)
            return allocation
