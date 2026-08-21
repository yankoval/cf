"""Yandex Cloud Functions entrypoint: ``index.handler``."""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.config import Config

from app import PrnsrvFunction
from config import Settings
from storage import S3Storage


LOGGER = logging.getLogger("prnsrv_generator")
LOGGER.setLevel(logging.INFO)
EPHEMERAL_KEY_ENDPOINT = (
    "https://iam.api.cloud.yandex.net/iam/aws-compatibility/v1/ephemeralAccessKeys"
)


def _context_access_token(context: Any) -> str:
    token_data = getattr(context, "token", None)
    if token_data is None and isinstance(context, Mapping):
        token_data = context.get("token")
    if isinstance(token_data, Mapping):
        token = token_data.get("access_token")
    else:
        token = getattr(token_data, "access_token", None)
    if not token:
        raise RuntimeError("Cloud Functions context IAM token is required for S3 access")
    return str(token)


def _s3_access_policy(settings: Settings) -> dict[str, Any]:
    bucket = settings.bucket_id
    if not bucket:
        raise RuntimeError("BUCKET_ID is required for scoped S3 credentials")
    if settings.templates_bucket and settings.templates_bucket != bucket:
        raise RuntimeError("Ephemeral S3 credentials support one bucket per invocation")

    object_arn = f"arn:aws:s3:::{bucket}/"
    read_resources = [
        object_arn + settings.input_prefix + "*",
        object_arn + settings.templates_prefix + "*",
        object_arn + settings.output_prefix + "*",
        object_arn + settings.done_prefix + "*",
    ]
    if settings.mapping_key:
        read_resources.append(object_arn + settings.mapping_key)

    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{bucket}",
            },
            {
                "Effect": "Allow",
                "Action": "s3:GetObject",
                "Resource": sorted(set(read_resources)),
            },
            {
                "Effect": "Allow",
                "Action": "s3:PutObject",
                "Resource": [
                    object_arn + settings.output_prefix + "*",
                    object_arn + settings.done_prefix + "*",
                ],
            },
        ],
    }


def _issue_ephemeral_credentials(
    *, iam_token: str, settings: Settings, session_name: str
) -> dict[str, str]:
    policy = json.dumps(
        _s3_access_policy(settings),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    request = urllib.request.Request(
        EPHEMERAL_KEY_ENDPOINT,
        data=json.dumps(
            {
                "sessionName": session_name[:64],
                "policy": policy,
                "duration": "900s",
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {iam_token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.load(response)

    credentials = {
        "access_key_id": str(payload.get("accessKeyId") or ""),
        "secret_access_key": str(payload.get("secret") or ""),
        "session_token": str(payload.get("sessionToken") or ""),
    }
    if not all(credentials.values()):
        raise RuntimeError("IAM returned incomplete ephemeral S3 credentials")
    return credentials


def _create_app(context: Any) -> PrnsrvFunction:
    settings = Settings.from_env()
    request_id = str(getattr(context, "request_id", "") or "invocation")
    credentials = _issue_ephemeral_credentials(
        iam_token=_context_access_token(context),
        settings=settings,
        session_name=f"prnsrv-{request_id}",
    )
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
        "aws_access_key_id": credentials["access_key_id"],
        "aws_secret_access_key": credentials["secret_access_key"],
        "aws_session_token": credentials["session_token"],
    }
    s3_client = boto3.client(**client_options)
    return PrnsrvFunction(storage=S3Storage(s3_client), settings=settings)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        return _create_app(context).handle(event, context)
    except Exception:
        LOGGER.exception("prnsrv-generator invocation failed")
        raise
