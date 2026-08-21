import unittest

from storage import ImmutableObjectConflict, S3Storage
from tests.fakes import FakeS3Client


class ImmutableStorageTests(unittest.TestCase):
    def test_same_content_is_an_idempotent_success(self):
        client = FakeS3Client()
        storage = S3Storage(client)
        first = storage.put_immutable("bucket", "key", b"same", content_type="text/plain")
        second = storage.put_immutable("bucket", "key", b"same", content_type="text/plain")
        self.assertEqual(first.sha256, second.sha256)

    def test_different_content_is_a_conflict(self):
        client = FakeS3Client()
        storage = S3Storage(client)
        storage.put_immutable("bucket", "key", b"first", content_type="text/plain")
        with self.assertRaises(ImmutableObjectConflict):
            storage.put_immutable("bucket", "key", b"second", content_type="text/plain")


if __name__ == "__main__":
    unittest.main()
