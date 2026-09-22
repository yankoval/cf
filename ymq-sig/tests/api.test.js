const test = require('node:test');
const assert = require('node:assert/strict');
const S3 = require('@aws-sdk/client-s3');
const SQS = require('@aws-sdk/client-sqs');
const { createStorageAPI } = require('../storage-api');
const calls = [];
let tagSet = [{ Key: 'keep', Value: 'yes' }];
const api = createStorageAPI({ env: { DEFAULT_BUCKET: 'test', ALLOWED_BUCKETS: 'test', WRITE_PREFIX: 'test/' },
  client: { send: async c => { calls.push(c); return c instanceof S3.GetObjectTaggingCommand ? { TagSet: tagSet } : { Contents: [], IsTruncated: true, NextContinuationToken: 'next' }; } },
  sign: async (_, c) => { calls.push(c); return 'https://example.test/signed'; } });
test('listing executes one S3 call and passes cursor without collecting all pages', async () => {
  calls.length = 0;
  const result = await api('ls', { Prefix: 'test/', MaxKeys: 20, ContinuationToken: 'cursor' });
  assert.equal(calls.length, 1); assert.equal(calls[0].input.ContinuationToken, 'cursor');
  assert.equal(result.NextContinuationToken, 'next');
  await assert.rejects(api('ls', { MaxKeys: 1001 }), /MaxKeys/);
  await assert.rejects(api('ls', { Bucket: 'other' }), /not allowed/);
});
test('single-tag mutation retains unrelated tags and returns full written set', async () => {
  const result = await api('set-object-tag', { Key: 'test/a', TagKey: 'check', TagValue: 'done' });
  assert.deepEqual(result.TagSet, [{ Key: 'keep', Value: 'yes' }, { Key: 'check', Value: 'done' }]);
  await assert.rejects(api('set-object-tag', { Key: 'production/a', TagKey: 'check', TagValue: 'done' }), /disabled/);
});
test('preview forces safe text MIME and upload preserves no-overwrite condition', async () => {
  await api('get-preview-url', { Key: 'test/a.json' });
  assert.equal(calls.at(-1).input.ResponseContentType, 'text/plain; charset=utf-8');
  await assert.rejects(api('get-preview-url', { Key: 'test/a.html' }), /Unsupported/);
  await api('get-upload-url', { Key: 'test/a.json' });
  assert.equal(calls.at(-1).input.IfNoneMatch, '*');
});
process.env.API_KEY = 'test-secret';
process.env.ACCESS_KEY_ID = 'mock'; process.env.SECRET_ACCESS_KEY = 'mock-secret';
process.env.YMQ_QUEUE_URL = 'https://example.test/queue';
SQS.SQSClient.prototype.send = async command => {
  calls.push(command);
  return command instanceof SQS.ReceiveMessageCommand ? { Messages: [{ Body: JSON.stringify({ bucket_id: 'test', object_id: 'a.json' }) }] } : {};
};
const { handler } = require('../index');
const invoke = (action, headers = { 'x-api-key': 'test-secret' }) => handler({ httpMethod: 'POST', headers, body: JSON.stringify({ action, params: {} }) });
test('auth rejects missing and invalid keys, accepts case-insensitive headers', async () => {
  assert.equal((await invoke('ping', {})).statusCode, 403);
  assert.equal((await invoke('ping', { 'X-Api-Key': 'bad' })).statusCode, 403);
  assert.equal((await invoke('ping', { 'X-API-KEY': 'test-secret' })).statusCode, 200);
  assert.equal((await handler({ httpMethod: 'OPTIONS' })).statusCode, 204);
});
test('SignJS ReceiveMessage preserves S3Links; DeleteMessage remains supported', async () => {
  const result = await invoke('ReceiveMessage');
  assert.equal(result.statusCode, 200);
  const links = JSON.parse(result.body).Messages[0].S3Links;
  assert.equal(links.sigKey, 'a.json.sig'); assert.equal(links.originalBucket, 'test');
  assert.ok(links.downloadUrl); assert.ok(links.uploadUrl);
  assert.equal((await invoke('DeleteMessage')).statusCode, 200);
});
