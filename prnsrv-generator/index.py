"""Yandex Cloud Functions entrypoint: ``index.handler``."""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from botocore.config import Config

from app import PrnsrvFunction
from config import Settings
from storage import S3Storage


LOGGER = logging.getLogger("prnsrv_generator")
LOGGER.setLevel(logging.INFO)
_APP: PrnsrvFunction | None = None


def _create_app() -> PrnsrvFunction:
    settings = Settings.from_env()
    client_options: dict[str, Any] = {
        "service_name": "s3",
        "endpoint_url": settings.s3_endpoint,
        "region_name": settings.region,
        "config": Config(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=30,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    }
    access_key = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
    if access_key and secret_key:
        client_options["aws_access_key_id"] = access_key
        client_options["aws_secret_access_key"] = secret_key
    s3_client = boto3.client(**client_options)
    return PrnsrvFunction(storage=S3Storage(s3_client), settings=settings)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    global _APP
    if _APP is None:
        _APP = _create_app()
    try:
        return _APP.handle(event, context)
    except Exception:
        LOGGER.exception("prnsrv-generator invocation failed")
        raise
