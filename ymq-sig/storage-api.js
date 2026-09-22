const S3 = require('@aws-sdk/client-s3');

const ACTIONS = ['list-objects-v2', 'ls', 'get-object-tagging', 'put-object-tagging',
  'delete-object-tagging', 'set-object-tag', 'remove-object-tag', 'get-download-url',
  'get-preview-url', 'get-upload-url'];
function fail(message, status = 400) { throw Object.assign(new Error(message), { status }); }
function createStorageAPI({ client, sign, env }) {
  const buckets = (env.ALLOWED_BUCKETS || '').split(',').filter(Boolean);
  return async function storage(action, params) {
    const p = { ...params, Bucket: params.Bucket || env.DEFAULT_BUCKET || env.UPLOAD_BUCKET };
    if (!p.Bucket || typeof p.Bucket !== 'string') fail('Bucket is required');
    if (buckets.length && !buckets.includes(p.Bucket)) fail('Bucket is not allowed', 403);
    const send = (Command, input) => client.send(new Command(input));
    if (action === 'ls' || action === 'list-objects-v2') {
      const limit = p.MaxKeys ?? 500;
      if (!Number.isInteger(limit) || limit < 1 || limit > 1000) fail('MaxKeys must be 1..1000');
      if (p.Prefix !== undefined && typeof p.Prefix !== 'string') fail('Invalid Prefix');
      if (p.ContinuationToken !== undefined && typeof p.ContinuationToken !== 'string') fail('Invalid ContinuationToken');
      return send(S3.ListObjectsV2Command, { Bucket: p.Bucket, Prefix: p.Prefix || '',
        Delimiter: p.Delimiter ?? '/', MaxKeys: limit, ContinuationToken: p.ContinuationToken });
    }
    if (!p.Key || typeof p.Key !== 'string') fail('Key is required');
    const target = { Bucket: p.Bucket, Key: p.Key };
    const writes = ['put-object-tagging', 'delete-object-tagging', 'set-object-tag', 'remove-object-tag', 'get-upload-url'];
    if (writes.includes(action) && env.WRITE_PREFIX && !p.Key.startsWith(env.WRITE_PREFIX)) fail('Writes outside test prefix are disabled', 403);
    if (action === 'get-object-tagging') return send(S3.GetObjectTaggingCommand, target);
    if (action === 'delete-object-tagging') {
      await send(S3.DeleteObjectTaggingCommand, target);
      return { ...target, TagSet: [], success: true };
    }
    if (['put-object-tagging', 'set-object-tag', 'remove-object-tag'].includes(action)) {
      let TagSet;
      if (action === 'put-object-tagging') TagSet = p.Tagging?.TagSet;
      else {
        if (typeof p.TagKey !== 'string' || !p.TagKey) fail('TagKey is required');
        if (action === 'set-object-tag' && typeof p.TagValue !== 'string') fail('TagValue must be a string');
        const old = await send(S3.GetObjectTaggingCommand, target);
        TagSet = (old.TagSet || []).filter(t => t.Key !== p.TagKey);
        if (action === 'set-object-tag') TagSet.push({ Key: p.TagKey, Value: p.TagValue });
      }
      if (!Array.isArray(TagSet) || TagSet.length > 10 || TagSet.some(t => !t || typeof t.Key !== 'string' || !t.Key || typeof t.Value !== 'string') || new Set(TagSet.map(t => t.Key)).size !== TagSet.length) fail('Invalid TagSet');
      await send(S3.PutObjectTaggingCommand, { ...target, Tagging: { TagSet } });
      return { ...target, TagSet, success: true };
    }
    const expiresIn = Math.min(3600, Math.max(60, Number(env.URL_EXPIRATION) || 900));
    if (action === 'get-upload-url') {
      const url = await sign(client, new S3.PutObjectCommand({ ...target, IfNoneMatch: '*' }), { expiresIn });
      return { url, expiresIn, headers: { 'If-None-Match': '*' }, key: p.Key };
    }
    const preview = action === 'get-preview-url';
    const ext = p.Key.split('.').pop().toLowerCase();
    const types = { json: 'text/plain; charset=utf-8', txt: 'text/plain; charset=utf-8',
      bmp: 'image/bmp', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg' };
    if (preview && !types[ext]) fail('Unsupported preview format');
    const filename = encodeURIComponent(p.Key.split('/').pop()).replace(/'/g, '%27');
    const command = new S3.GetObjectCommand({ ...target,
      ResponseContentDisposition: preview ? 'inline' : `attachment; filename*=UTF-8''${filename}`,
      ...(preview ? { ResponseContentType: types[ext] } : {}) });
    return { url: await sign(client, command, { expiresIn }), expiresIn };
  };
}
module.exports = { createStorageAPI, ACTIONS };
