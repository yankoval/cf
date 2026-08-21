"""Yandex Cloud Function adapter for the reusable prnsrv package."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from typing import Any, Callable, Mapping
from urllib.parse import unquote_plus

from prnsrv import (
    InputValidationError,
    SSCCClient,
    build_csv,
    build_vdf,
    canonical_source_hash,
    classify_count,
    resolve_route,
    resolve_template_name,
)

from config import Settings
from storage import (
    ImmutableObjectConflict,
    ObjectAlreadyExists,
    ObjectInfo,
    S3Storage,
    json_bytes,
)


LOGGER = logging.getLogger("prnsrv_generator")
LOGGER.setLevel(logging.INFO)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(level: int, event: str, **fields: Any) -> None:
    LOGGER.log(
        level,
        json.dumps(
            {"event": event, **fields},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


class PrnsrvFunction:
    def __init__(
        self,
        *,
        storage: S3Storage,
        settings: Settings,
        allocator: Any | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.storage = storage
        self.settings = settings
        self._allocator = allocator
        self.now = now

    def _allocator_client(self) -> Any:
        if self._allocator is None:
            if not self.settings.sscc_url:
                raise RuntimeError("SSCC_URL is required for jobs with a positive count")
            self._allocator = SSCCClient(
                self.settings.sscc_url,
                prefix=self.settings.sscc_prefix,
                extension=self.settings.sscc_extension,
                timeout_seconds=self.settings.sscc_timeout_seconds,
            )
        return self._allocator

    def _job_uuid_from_key(self, key: str) -> str:
        pattern = rf"^{re.escape(self.settings.input_prefix)}([^/]+)\.json$"
        match = re.fullmatch(pattern, key)
        if not match:
            raise InputValidationError("Source key does not match the configured input contract")
        try:
            return str(uuid.UUID(match.group(1)))
        except ValueError as exc:
            raise InputValidationError("Source filename must contain a UUID") from exc

    def _done_key(self, job_uuid: str) -> str:
        return f"{self.settings.done_prefix}{job_uuid}.done"

    def _output_keys(self, job_uuid: str, route: str) -> tuple[str, str]:
        return (
            f"{self.settings.output_prefix}{job_uuid}.csv",
            f"{self.settings.output_prefix}{route}_{job_uuid}.vdf",
        )

    def _read_source(self, bucket: str, key: str) -> dict[str, Any]:
        body = self.storage.get_bytes(bucket, key)
        try:
            value = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InputValidationError("Source object does not contain valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise InputValidationError("Source JSON must contain an object")
        return value

    def _load_mapping(self, source_bucket: str) -> list[Mapping[str, Any]]:
        if self.settings.mapping_key:
            mapping_bucket = self.settings.templates_bucket or source_bucket
            raw_mapping = self.storage.get_bytes(mapping_bucket, self.settings.mapping_key)
            try:
                mapping = json.loads(raw_mapping.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InputValidationError("Configured mapping object is not valid JSON") from exc
        else:
            mapping = json.loads(
                files("prnsrv").joinpath("assets/mapping.json").read_text(encoding="utf-8")
            )
        if not isinstance(mapping, list):
            raise InputValidationError("VDF mapping must contain a list")
        return mapping

    def _load_template(self, source_bucket: str, template_name: str) -> bytes:
        template_bucket = self.settings.templates_bucket or source_bucket
        template_key = f"{self.settings.templates_prefix}{template_name}.vdf"
        return self.storage.get_bytes(template_bucket, template_key)

    def _validate_existing_marker(
        self,
        *,
        bucket: str,
        marker: Mapping[str, Any],
        source_hash: str,
    ) -> str:
        if marker.get("source_hash") != source_hash:
            raise ImmutableObjectConflict("Done marker belongs to a different source hash")
        result = marker.get("result")
        if result == "NO_PRINT":
            return result
        if result != "GENERATED":
            raise ImmutableObjectConflict("Done marker contains an unknown result")

        outputs = marker.get("outputs")
        if not isinstance(outputs, Mapping):
            raise ImmutableObjectConflict("Generated done marker has no outputs")
        for output_name in ("csv", "vdf"):
            output = outputs.get(output_name)
            if not isinstance(output, Mapping):
                raise ImmutableObjectConflict(f"Done marker has no {output_name} output")
            key = str(output.get("key") or "")
            expected_sha256 = str(output.get("sha256") or "")
            if not key or not expected_sha256:
                raise ImmutableObjectConflict(f"Done marker has invalid {output_name} metadata")
            actual_sha256 = self.storage.actual_sha256(bucket, key)
            if actual_sha256 != expected_sha256:
                raise ImmutableObjectConflict(f"Done marker {output_name} does not match S3")
        return result

    @staticmethod
    def _markers_equivalent(existing: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
        if existing.get("source_hash") != expected.get("source_hash"):
            return False
        if existing.get("result") != expected.get("result"):
            return False
        if expected.get("result") == "NO_PRINT":
            return existing.get("reason_code") == expected.get("reason_code")

        existing_outputs = existing.get("outputs")
        expected_outputs = expected.get("outputs")
        if not isinstance(existing_outputs, Mapping) or not isinstance(expected_outputs, Mapping):
            return False
        for output_name in ("csv", "vdf"):
            existing_output = existing_outputs.get(output_name)
            expected_output = expected_outputs.get(output_name)
            if not isinstance(existing_output, Mapping) or not isinstance(expected_output, Mapping):
                return False
            if existing_output.get("key") != expected_output.get("key"):
                return False
            if existing_output.get("sha256") != expected_output.get("sha256"):
                return False
        return True

    def _create_done_marker(
        self,
        *,
        bucket: str,
        job_uuid: str,
        marker: dict[str, Any],
    ) -> None:
        marker_key = self._done_key(job_uuid)
        try:
            self.storage.put_create_only(
                bucket,
                marker_key,
                json_bytes(marker),
                content_type="application/json",
                metadata={
                    "source-hash": str(marker["source_hash"]),
                    "result": str(marker["result"]),
                },
            )
        except ObjectAlreadyExists:
            existing = self.storage.get_json(bucket, marker_key)
            if not self._markers_equivalent(existing, marker):
                raise ImmutableObjectConflict("Concurrent done marker has different content")

    def process_object(self, *, bucket: str, key: str, event_id: str | None = None) -> dict[str, Any]:
        if self.settings.bucket_id and bucket != self.settings.bucket_id:
            raise InputValidationError("Event bucket does not match BUCKET_ID")

        job_uuid = self._job_uuid_from_key(key)
        source = self._read_source(bucket, key)
        source_hash = canonical_source_hash(source)
        done_key = self._done_key(job_uuid)
        existing_marker = self.storage.get_json_optional(bucket, done_key)
        if existing_marker is not None:
            result = self._validate_existing_marker(
                bucket=bucket,
                marker=existing_marker,
                source_hash=source_hash,
            )
            _log(
                logging.INFO,
                "duplicate_done",
                uuid=job_uuid,
                event_id=event_id,
                result=result,
                source_hash=source_hash,
            )
            return {"uuid": job_uuid, "result": result, "duplicate": True}

        count_decision = classify_count(source)
        if not count_decision.should_print:
            marker = {
                "schema_version": 1,
                "uuid": job_uuid,
                "source_key": key,
                "source_hash": source_hash,
                "result": "NO_PRINT",
                "reason_code": count_decision.reason_code,
                "completed_at": _isoformat(self.now()),
            }
            self._create_done_marker(bucket=bucket, job_uuid=job_uuid, marker=marker)
            _log(
                logging.INFO,
                "job_completed",
                uuid=job_uuid,
                event_id=event_id,
                result="NO_PRINT",
                reason_code=count_decision.reason_code,
                count_field=count_decision.source_field,
                source_hash=source_hash,
            )
            return {"uuid": job_uuid, "result": "NO_PRINT", "duplicate": False}

        route = resolve_route(source)
        template_name = resolve_template_name(source)
        template_bytes = self._load_template(bucket, template_name)
        mapping_items = self._load_mapping(bucket)
        allocation = self._allocator_client().allocate(
            job_uuid=job_uuid,
            source_hash=source_hash,
            count=count_decision.count,
        )
        csv_bytes = build_csv(
            allocation.ssccs,
            count_decision.count,
            column_name=self.settings.column_name,
        )
        csv_key, vdf_key = self._output_keys(job_uuid, route)
        windows_csv_path = (
            self.settings.windows_csv_dir.rstrip("\\/") + "\\" + f"{job_uuid}.csv"
        )
        vdf_bytes = build_vdf(
            template_bytes,
            csv_bytes,
            source,
            mapping_items,
            source_path=windows_csv_path,
        )

        csv_info = self.storage.put_immutable(
            bucket,
            csv_key,
            csv_bytes,
            content_type="text/csv; charset=utf-8",
        )
        vdf_info = self.storage.put_immutable(
            bucket,
            vdf_key,
            vdf_bytes,
            content_type="application/xml; charset=utf-8",
        )
        if self.storage.actual_sha256(bucket, csv_key) != csv_info.sha256:
            raise ImmutableObjectConflict("Published CSV failed final checksum verification")
        if self.storage.actual_sha256(bucket, vdf_key) != vdf_info.sha256:
            raise ImmutableObjectConflict("Published VDF failed final checksum verification")

        marker = {
            "schema_version": 1,
            "uuid": job_uuid,
            "source_key": key,
            "source_hash": source_hash,
            "result": "GENERATED",
            "outputs": {
                "csv": self._marker_output(csv_info),
                "vdf": self._marker_output(vdf_info),
            },
            "completed_at": _isoformat(self.now()),
        }
        self._create_done_marker(bucket=bucket, job_uuid=job_uuid, marker=marker)
        _log(
            logging.INFO,
            "job_completed",
            uuid=job_uuid,
            event_id=event_id,
            result="GENERATED",
            count=count_decision.count,
            source_hash=source_hash,
            allocation_id=allocation.allocation_id,
            csv_key=csv_key,
            csv_etag=csv_info.etag,
            csv_sha256=csv_info.sha256,
            vdf_key=vdf_key,
            vdf_etag=vdf_info.etag,
            vdf_sha256=vdf_info.sha256,
        )
        return {"uuid": job_uuid, "result": "GENERATED", "duplicate": False}

    @staticmethod
    def _marker_output(info: ObjectInfo) -> dict[str, Any]:
        return {"key": info.key, "etag": info.etag, "sha256": info.sha256}

    def _outputs_consistent(self, bucket: str, csv_key: str, vdf_key: str) -> bool:
        csv_bytes = self.storage.get_bytes(bucket, csv_key)
        vdf_bytes = self.storage.get_bytes(bucket, vdf_key)
        try:
            root = ET.fromstring(vdf_bytes)
        except ET.ParseError:
            return False
        expected_md5 = hashlib.md5(csv_bytes).hexdigest().upper()
        if root.findtext(".//DataMd5") != expected_md5:
            return False
        source_path = root.findtext(".//SourcePath") or ""
        return source_path.replace("/", "\\").endswith("\\" + csv_key.rsplit("/", 1)[-1])

    def reconcile(self, *, bucket: str, run_id: str | None = None) -> dict[str, int]:
        if not bucket:
            raise RuntimeError("BUCKET_ID is required for timer reconciliation")
        now = self.now()
        recent_boundary = now - timedelta(hours=self.settings.reconcile_hours)
        stale_boundary = now - timedelta(hours=self.settings.stale_after_hours)

        recent_inputs: dict[str, str] = {}
        for item in self.storage.list_objects(bucket, self.settings.input_prefix):
            key = str(item.get("Key") or "")
            last_modified = item.get("LastModified")
            if not isinstance(last_modified, datetime):
                continue
            if last_modified.tzinfo is None:
                last_modified = last_modified.replace(tzinfo=timezone.utc)
            if not (recent_boundary <= last_modified <= stale_boundary):
                continue
            try:
                job_uuid = self._job_uuid_from_key(key)
            except InputValidationError:
                continue
            recent_inputs[job_uuid] = key

        done_uuids: set[str] = set()
        done_pattern = re.compile(rf"^{re.escape(self.settings.done_prefix)}([^/]+)\.done$")
        for item in self.storage.list_objects(bucket, self.settings.done_prefix):
            key = str(item.get("Key") or "")
            match = done_pattern.fullmatch(key)
            if not match:
                continue
            try:
                done_uuids.add(str(uuid.UUID(match.group(1))))
            except ValueError:
                continue

        candidates = sorted(set(recent_inputs) - done_uuids)
        counts: Counter[str] = Counter()
        counts["RECENT_INPUT"] = len(recent_inputs)
        counts["DONE"] = len(done_uuids)
        counts["CANDIDATE"] = len(candidates)

        for job_uuid in candidates:
            key = recent_inputs[job_uuid]
            try:
                source = self._read_source(bucket, key)
                count_decision = classify_count(source)
                if not count_decision.should_print:
                    classification = "NO_PRINT_MARKER_MISSING"
                    level = logging.INFO
                else:
                    route = resolve_route(source)
                    csv_key, vdf_key = self._output_keys(job_uuid, route)
                    csv_info = self.storage.head_optional(bucket, csv_key)
                    vdf_info = self.storage.head_optional(bucket, vdf_key)
                    if csv_info is not None and vdf_info is not None:
                        classification = (
                            "DONE_MARKER_MISSING"
                            if self._outputs_consistent(bucket, csv_key, vdf_key)
                            else "OUTPUT_INCONSISTENT"
                        )
                    elif csv_info is not None:
                        classification = "VDF_MISSING"
                    elif vdf_info is not None:
                        classification = "OUTPUT_INCONSISTENT"
                    else:
                        classification = "NOT_DONE"
                    level = logging.WARNING
            except Exception as exc:
                classification = "RECONCILIATION_CHECK_FAILED"
                level = logging.WARNING
                _log(
                    level,
                    "reconciliation_candidate",
                    uuid=job_uuid,
                    run_id=run_id,
                    classification=classification,
                    error_type=type(exc).__name__,
                )
                counts[classification] += 1
                continue

            counts[classification] += 1
            _log(
                level,
                "reconciliation_candidate",
                uuid=job_uuid,
                run_id=run_id,
                classification=classification,
            )

        result = dict(sorted(counts.items()))
        _log(logging.INFO, "reconciliation_completed", run_id=run_id, counts=result)
        return result

    def handle(self, event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
        request_id = getattr(context, "request_id", None) or getattr(context, "token", None)
        object_events: list[tuple[str, str, str | None]] = []
        for message in event.get("messages", []) if isinstance(event, Mapping) else []:
            if not isinstance(message, Mapping):
                continue
            details = message.get("details")
            if not isinstance(details, Mapping):
                continue
            bucket = details.get("bucket_id") or details.get("bucket")
            key = details.get("object_id") or details.get("key")
            if not bucket or not key:
                continue
            metadata = message.get("event_metadata")
            event_id = metadata.get("event_id") if isinstance(metadata, Mapping) else None
            object_events.append((str(bucket), unquote_plus(str(key)), str(event_id or request_id or "")))

        if object_events:
            results = [
                self.process_object(bucket=bucket, key=key, event_id=event_id or None)
                for bucket, key, event_id in object_events
            ]
            return {"statusCode": 200, "body": json.dumps({"results": results})}

        bucket = self.settings.bucket_id
        counts = self.reconcile(bucket=str(bucket or ""), run_id=str(request_id or ""))
        return {"statusCode": 200, "body": json.dumps({"counts": counts})}
