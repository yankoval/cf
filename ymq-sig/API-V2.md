# Unified SignJS / S3 API v2

Production release 2026-09-22: see RELEASE-20260922.md for active URLs, verification,
rollback and the test-function transition. The test-deployment notes below describe
the earlier isolated stage, not the current production release.

Transport: HTTPS POST, `Content-Type: application/json`, `X-Api-Key`.
Body: `{"action":"list-objects-v2","params":{"Prefix":"Задания/","MaxKeys":500}}`.
All operations require API_KEY, except CORS OPTIONS. Missing server key fails closed (503);
wrong client key returns 403. Never log request headers, raw events or signed URLs.

Existing SQS command names and ReceiveMessage.S3Links remain compatible with SignJS.
GET health now requires the API key. `ping` returns `{pong:true}`;
`capabilities` returns apiVersion=2 and the S3 action list.

## S3 commands

- `list-objects-v2` / `ls`: one page, AWS Contents/CommonPrefixes/NextContinuationToken.
  Bucket defaults from DEFAULT_BUCKET; MaxKeys 1..1000, default 500. No tag reads.
- `get-object-tagging`: Bucket, Key → TagSet.
- `put-object-tagging`: Bucket, Key, Tagging.TagSet → complete written TagSet.
- `delete-object-tagging`: Bucket, Key → empty TagSet.
- `set-object-tag` / `remove-object-tag`: Bucket, Key, TagKey, optional TagValue.
  Read-modify-write preserves unrelated tags, but is not atomic against external writers.
- `get-download-url`, `get-preview-url`: Bucket, Key → url, expiresIn.
  Preview only JSON/TXT (text/plain), BMP/PNG/JPG/JPEG; inline disposition.
- `get-upload-url`: Bucket, Key → url, expiresIn, required headers.
  If-None-Match:* prevents overwriting an existing object.

API errors use error + code; SDK metadata may appear in successful AWS responses.
Signed URLs are bearer credentials and remain valid until expiry after key rotation.

## Environment

Required: API_KEY. Credentials: ACCESS_KEY_ID + SECRET_ACCESS_KEY, S3_* aliases,
AWS_* aliases or standard SDK provider chain. AWS_SESSION_TOKEN supports temporary credentials.
STORAGE_ACCESS_KEY_ID / STORAGE_SECRET_ACCESS_KEY / STORAGE_SESSION_TOKEN / STORAGE_ENDPOINT
optionally configure a separate client for the new storage commands, preserving legacy SignJS
credentials. The test deployment uses the approved bukupl credentials for this client because
the SignJS credentials cannot read the actual tasks bucket 1bf11148-3595-4a07-a089-d460153b7c7a.
S3_ENDPOINT, YMQ_ENDPOINT, AWS_REGION, S3_FORCE_PATH_STYLE configure service compatibility.
YMQ_QUEUE_URL and UPLOAD_BUCKET retain their SignJS meaning. DEFAULT_BUCKET is the browser default.
URL_EXPIRATION: signed URL TTL (new S3 commands clamp to 60..3600 seconds).
CLIENT_TIMEOUT: connection/socket timeout; SDK maxAttempts=2.
ALLOWED_BUCKETS: optional comma-separated S3 bucket allowlist.
WRITE_PREFIX: optional prefix restriction for S3 mutations and upload links.
DISABLE_QUEUE=true: deny SQS operations in the storage test environment.

These restrictions apply to new S3 commands; SQS retains the legacy contract.
For the test deployment DISABLE_QUEUE must remain true when using production queue settings.
There is no Lockbox dependency. `node server.js` provides a portable HTTP adapter
(HOST default 127.0.0.1, PORT default 8080); deploy behind HTTPS in another environment.

## Test deployment

Function: sign-storage-api-test, d4emlmsp7hd8gr65scac.
URL: https://functions.yandexcloud.net/d4emlmsp7hd8gr65scac
Production sign and bukupl are unchanged. API/S3 credentials were copied with explicit approval;
IAM anonymous invocation was separately approved; the handler enforces X-Api-Key.
Writes are restricted to _api-tests/20260918/ in the approved bucket. Queue is disabled.

`npm test` checks contracts without cloud access. `smoke-test.py` checks the deployed endpoint,
reads all metadata pages of Задания and first 20 tag sets, and creates one unique test JSON.
The test object is deliberately retained for inspection. No production documents are signed.
`deploy-test.py` can publish ONLY the named test function; it never updates sign or bukupl.
Production rollout and re-enabling queue operations require the agreed post-test release decision.
