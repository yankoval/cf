"""Environment configuration for the prnsrv Cloud Function adapter."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _prefix(value: str) -> str:
    return value.rstrip("/") + "/"


@dataclass(frozen=True)
class Settings:
    bucket_id: str | None
    input_prefix: str = "Задания/"
    output_prefix: str = "printer-tasks/"
    done_prefix: str = "_prnsrv/done/"
    templates_bucket: str | None = None
    templates_prefix: str = "printer-templates/"
    mapping_key: str | None = None
    column_name: str = "C1"
    windows_csv_dir: str = r"C:\tmp"
    sscc_url: str | None = None
    sscc_prefix: str = "460705179"
    sscc_extension: str | None = "0"
    sscc_timeout_seconds: float = 15.0
    reconcile_hours: int = 48
    stale_after_hours: int = 1
    s3_endpoint: str = "https://storage.yandexcloud.net"
    region: str = "ru-central1"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bucket_id=os.getenv("BUCKET_ID") or os.getenv("BUCKET"),
            input_prefix=_prefix(os.getenv("INPUT_PREFIX", "Задания/")),
            output_prefix=_prefix(os.getenv("OUTPUT_PREFIX", "printer-tasks/")),
            done_prefix=_prefix(os.getenv("DONE_PREFIX", "_prnsrv/done/")),
            templates_bucket=os.getenv("TEMPLATES_BUCKET"),
            templates_prefix=_prefix(os.getenv("TEMPLATES_PREFIX", "printer-templates/")),
            mapping_key=os.getenv("MAPPING_KEY") or None,
            column_name=os.getenv("COLUMN_NAME", "C1"),
            windows_csv_dir=os.getenv("WINDOWS_CSV_DIR", r"C:\tmp"),
            sscc_url=os.getenv("SSCC_URL"),
            sscc_prefix=os.getenv("SSCC_PREFIX", "460705179"),
            sscc_extension=os.getenv("SSCC_EXTENSION", "0"),
            sscc_timeout_seconds=float(os.getenv("SSCC_TIMEOUT_SECONDS", "15")),
            reconcile_hours=int(os.getenv("RECONCILE_HOURS", "48")),
            stale_after_hours=int(os.getenv("STALE_AFTER_HOURS", "1")),
            s3_endpoint=os.getenv("S3_ENDPOINT", "https://storage.yandexcloud.net"),
            region=os.getenv("AWS_REGION", "ru-central1"),
        )
