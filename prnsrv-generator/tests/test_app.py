from __future__ import annotations

import hashlib
import json
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import PrnsrvFunction
from config import Settings
from storage import ImmutableObjectConflict, S3Storage
from tests.fakes import FakeAllocator, FakeS3Client


BUCKET = "test-bucket"
FIXED_NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
TEMPLATE = b"""<?xml version="1.0" encoding="utf-8"?>
<Root>
  <DataSourceSet><DataSource><DataPathInfo><SourcePath>old.csv</SourcePath></DataPathInfo><DataMd5>OLD</DataMd5></DataSource></DataSourceSet>
  <RipParam><EndNo>0</EndNo><OutputRecords>0-0</OutputRecords></RipParam>
</Root>"""


class PrnsrvFunctionTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeS3Client()
        self.storage = S3Storage(self.client)
        self.allocator = FakeAllocator()
        self.settings = Settings(
            bucket_id=BUCKET,
            templates_prefix="templates/",
            mapping_key="config/mapping.json",
        )
        self.client.seed(BUCKET, "templates/test.vdf", TEMPLATE)
        self.client.seed(BUCKET, "config/mapping.json", b"[]")
        self.app = PrnsrvFunction(
            storage=self.storage,
            settings=self.settings,
            allocator=self.allocator,
            now=lambda: FIXED_NOW,
        )

    def seed_source(self, data, *, job_uuid=None, age_hours=2):
        job_uuid = job_uuid or str(uuid.uuid4())
        key = f"Задания/{job_uuid}.json"
        self.client.seed(
            BUCKET,
            key,
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
            last_modified=FIXED_NOW - timedelta(hours=age_hours),
        )
        return job_uuid, key

    @staticmethod
    def printable(count=2):
        return {
            "count": count,
            "PasportData": {
                "Manufacturer_inn": "7733154124",
                "Format": "test",
            },
        }

    def test_no_print_values_create_done_without_allocator_or_outputs(self):
        cases = [0, "0", -1, "", "   ", None, "abc", [], {}, float("inf")]
        for raw_count in cases:
            with self.subTest(raw_count=raw_count):
                job_uuid, key = self.seed_source({"count": raw_count})
                before_calls = len(self.allocator.calls)
                with self.assertLogs("prnsrv_generator", level="INFO") as logs:
                    result = self.app.process_object(bucket=BUCKET, key=key)
                self.assertEqual("NO_PRINT", result["result"])
                self.assertEqual(before_calls, len(self.allocator.calls))
                marker = self.client.json(BUCKET, f"_prnsrv/done/{job_uuid}.done")
                self.assertEqual("NO_PRINT", marker["result"])
                self.assertNotIn("outputs", marker)
                self.assertIn('"result":"NO_PRINT"', "\n".join(logs.output))
                self.assertTrue(all(record.startswith("INFO:") for record in logs.output))
                self.assertFalse(
                    any(
                        object_key.startswith("printer-tasks/")
                        for bucket, object_key in self.client.objects
                        if bucket == BUCKET and job_uuid in object_key
                    )
                )

    def test_legacy_quantity_no_print_is_supported(self):
        job_uuid, key = self.seed_source({"Quantity": ""})
        result = self.app.process_object(bucket=BUCKET, key=key)
        self.assertEqual("NO_PRINT", result["result"])
        self.assertEqual(
            "NO_PRINT",
            self.client.json(BUCKET, f"_prnsrv/done/{job_uuid}.done")["result"],
        )

    def test_generated_job_writes_csv_before_vdf_and_done(self):
        job_uuid, key = self.seed_source(self.printable())
        result = self.app.process_object(bucket=BUCKET, key=key)
        self.assertEqual("GENERATED", result["result"])

        put_keys = [call[2] for call in self.client.calls if call[0] == "put_object"]
        csv_key = f"printer-tasks/{job_uuid}.csv"
        vdf_key = f"printer-tasks/7733154124_{job_uuid}.vdf"
        done_key = f"_prnsrv/done/{job_uuid}.done"
        self.assertLess(put_keys.index(csv_key), put_keys.index(vdf_key))
        self.assertLess(put_keys.index(vdf_key), put_keys.index(done_key))

        marker = self.client.json(BUCKET, done_key)
        self.assertEqual("GENERATED", marker["result"])
        self.assertEqual(
            hashlib.sha256(self.client.objects[(BUCKET, csv_key)]["body"]).hexdigest(),
            marker["outputs"]["csv"]["sha256"],
        )
        self.assertEqual(1, len(self.allocator.calls))

    def test_duplicate_done_does_not_call_allocator_twice(self):
        job_uuid, key = self.seed_source(self.printable(1))
        self.app.process_object(bucket=BUCKET, key=key)
        result = self.app.process_object(bucket=BUCKET, key=key)
        self.assertTrue(result["duplicate"])
        self.assertEqual(1, len(self.allocator.calls))
        self.assertEqual(job_uuid, result["uuid"])

    def test_parallel_invocations_converge_on_one_pair(self):
        job_uuid, key = self.seed_source(self.printable(2))
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: self.app.process_object(bucket=BUCKET, key=key),
                    range(2),
                )
            )
        self.assertEqual(["GENERATED", "GENERATED"], [item["result"] for item in results])
        self.assertIn((BUCKET, f"printer-tasks/{job_uuid}.csv"), self.client.objects)
        self.assertIn((BUCKET, f"printer-tasks/7733154124_{job_uuid}.vdf"), self.client.objects)
        self.assertIn((BUCKET, f"_prnsrv/done/{job_uuid}.done"), self.client.objects)
        self.assertEqual(1, len(self.allocator.allocations))

    def test_parallel_no_print_invocations_converge_without_allocator(self):
        job_uuid, key = self.seed_source({"count": "invalid"})
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: self.app.process_object(bucket=BUCKET, key=key),
                    range(2),
                )
            )
        self.assertEqual(["NO_PRINT", "NO_PRINT"], [item["result"] for item in results])
        self.assertEqual([], self.allocator.calls)
        self.assertEqual(
            "NO_PRINT",
            self.client.json(BUCKET, f"_prnsrv/done/{job_uuid}.done")["result"],
        )

    def test_retry_after_vdf_recovers_missing_done_marker(self):
        job_uuid, key = self.seed_source(self.printable(1))
        self.app.process_object(bucket=BUCKET, key=key)
        del self.client.objects[(BUCKET, f"_prnsrv/done/{job_uuid}.done")]

        result = self.app.process_object(bucket=BUCKET, key=key)

        self.assertEqual("GENERATED", result["result"])
        self.assertIn((BUCKET, f"_prnsrv/done/{job_uuid}.done"), self.client.objects)
        self.assertEqual(1, len(self.allocator.allocations))

    def test_immutable_output_conflict_is_terminal(self):
        job_uuid, key = self.seed_source(self.printable(1))
        self.client.seed(BUCKET, f"printer-tasks/{job_uuid}.csv", b"different")
        with self.assertRaises(ImmutableObjectConflict):
            self.app.process_object(bucket=BUCKET, key=key)

    def test_object_storage_event_is_dispatched(self):
        job_uuid, key = self.seed_source({"count": ""})
        response = self.app.handle(
            {
                "messages": [
                    {
                        "event_metadata": {"event_id": "event-1"},
                        "details": {"bucket_id": BUCKET, "object_id": key},
                    }
                ]
            }
        )
        self.assertEqual(200, response["statusCode"])
        self.assertIn((BUCKET, f"_prnsrv/done/{job_uuid}.done"), self.client.objects)

    def test_context_iam_token_is_forwarded_only_to_allocator(self):
        job_uuid, key = self.seed_source(self.printable(1))
        with self.assertLogs("prnsrv_generator", level="INFO") as logs:
            response = self.app.handle(
                {
                    "messages": [
                        {
                            "event_metadata": {"event_id": "event-iam"},
                            "details": {"bucket_id": BUCKET, "object_id": key},
                        }
                    ]
                },
                SimpleNamespace(
                    request_id="request-iam",
                    token={"access_token": "short-lived-iam-token"},
                ),
            )

        self.assertEqual(200, response["statusCode"])
        self.assertEqual("short-lived-iam-token", self.allocator.calls[0][3])
        self.assertNotIn("short-lived-iam-token", "\n".join(logs.output))

    def test_reconciliation_classifies_no_print_and_missing_vdf(self):
        no_print_uuid, _ = self.seed_source({"count": ""})
        print_uuid, _ = self.seed_source(self.printable(1))
        self.client.seed(
            BUCKET,
            f"printer-tasks/{print_uuid}.csv",
            b"C1\r\n00123456789012345678\r\n",
            metadata={"sha256": "placeholder"},
        )

        counts = self.app.reconcile(bucket=BUCKET, run_id="timer-1")

        self.assertEqual(2, counts["CANDIDATE"])
        self.assertEqual(1, counts["NO_PRINT_MARKER_MISSING"])
        self.assertEqual(1, counts["VDF_MISSING"])
        self.assertNotIn((BUCKET, f"_prnsrv/done/{no_print_uuid}.done"), self.client.objects)

    def test_reconciliation_ignores_inputs_before_cutover_boundary(self):
        old_uuid, _ = self.seed_source({"count": ""}, age_hours=2)
        new_uuid, _ = self.seed_source({"count": ""}, age_hours=1.25)
        settings = Settings(
            bucket_id=BUCKET,
            templates_prefix="templates/",
            mapping_key="config/mapping.json",
            reconcile_not_before=FIXED_NOW - timedelta(hours=1.5),
        )
        app = PrnsrvFunction(
            storage=self.storage,
            settings=settings,
            allocator=self.allocator,
            now=lambda: FIXED_NOW,
        )

        counts = app.reconcile(bucket=BUCKET, run_id="timer-cutover")

        self.assertEqual(1, counts["RECENT_INPUT"])
        self.assertEqual(1, counts["CANDIDATE"])
        self.assertEqual(1, counts["NO_PRINT_MARKER_MISSING"])
        self.assertNotEqual(old_uuid, new_uuid)


if __name__ == "__main__":
    unittest.main()
