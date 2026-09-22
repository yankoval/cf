// Backup before any overwrite; publish only the new entry point and a versioned dependency.
const { S3Client, GetObjectCommand, PutObjectCommand } = require('@aws-sdk/client-s3');
const { execFileSync } = require('node:child_process');
const { readFileSync, writeFileSync, existsSync, mkdirSync } = require('node:fs');
const { resolve } = require('node:path');
const { createHash } = require('node:crypto');
const state = resolve(__dirname, 'release-20260922');
const bucket = '20ab2a0c-2726-4ba1-9c7c-7deae82941ff';
const base = `https://storage.yandexcloud.net/${bucket}/`;
const hash = b => createHash('sha256').update(b).digest('hex');
const mime = 'text/html; charset=utf-8';
async function main() {
    const phase = process.argv[2];
    if (!['publish', 'rollback'].includes(phase)) throw new Error('Use publish or rollback');
    if (!existsSync(resolve(state, 'active.json'))) throw new Error('API must be activated first');
    const env = JSON.parse(execFileSync('yc', ['serverless', 'function', 'version', 'get',
        'd4ejvpekbv3p3fn7ep8h', '--format', 'json'], { encoding: 'utf8', timeout: 30000 })).environment;
    const client = new S3Client({ endpoint: 'https://storage.yandexcloud.net', region: 'ru-central1',
        forcePathStyle: true, maxAttempts: 2,
        credentials: { accessKeyId: env.ACCESS_KEY_ID, secretAccessKey: env.SECRET_ACCESS_KEY } });
    async function get(key) {
        const result = await client.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
        return { bytes: Buffer.from(await result.Body.transformToByteArray()), etag: result.ETag,
            contentType: result.ContentType, cacheControl: result.CacheControl };
    }
    async function verify(key, bytes) {
        const r = await fetch(base + key + '?release-check=20260922', { signal: AbortSignal.timeout(20000) });
        if (!r.ok || hash(Buffer.from(await r.arrayBuffer())) !== hash(bytes)) throw new Error(`Verification failed: ${key}`);
    }
    mkdirSync(state, { recursive: true });
    const recordPath = resolve(state, 'ui.json');
    if (phase === 'rollback') {
        const record = JSON.parse(readFileSync(recordPath));
        const current = await get('index.html');
        if (hash(current.bytes) !== record.newHash) throw new Error('Index changed after release; do not overwrite');
        const bytes = readFileSync(resolve(state, 'index-before.html'));
        await client.send(new PutObjectCommand({ Bucket: bucket, Key: 'index.html', Body: bytes,
            ContentType: record.contentType || mime, CacheControl: record.cacheControl || 'no-cache',
            IfMatch: current.etag, ACL: 'public-read' }));
        await verify('index.html', bytes);
        console.log('Working index restored; versioned dependency retained');
        client.destroy(); return;
    }
    if (existsSync(recordPath)) throw new Error('Release state exists; inspect before retrying');
    // Check production API before publishing a frontend that requires it.
    const r = await fetch('https://d5d6kfk1rvhoeodarkrj.p8361f8z.apigw.yandexcloud.net', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Api-Key': env.API_KEY },
        body: JSON.stringify({ action: 'capabilities' }), signal: AbortSignal.timeout(30000) });
    if (!r.ok || (await r.json()).apiVersion !== 2) throw new Error('Production API v2 is unavailable');
    for (const name of ['cloud-sign.html', 'CloudSignApp.js', 'style.css', 'cadesplugin_api.js']) {
        const live = await get(name), tested = await get('_ui-tests/20260921-v1/' + name);
        if (hash(live.bytes) !== hash(tested.bytes)) throw new Error(`SignJS changed since trial: ${name}`);
    }
    const previous = await get('index.html');
    writeFileSync(resolve(state, 'index-before.html'), previous.bytes, { flag: 'wx' });
    const backupKey = '_release-backups/20260922/index.html';
    await client.send(new PutObjectCommand({ Bucket: bucket, Key: backupKey, Body: previous.bytes,
        ContentType: previous.contentType || mime, IfNoneMatch: '*', ACL: 'private' }));
    if (hash((await get(backupKey)).bytes) !== hash(previous.bytes)) throw new Error('Backup verification failed');
    const root = resolve(__dirname, '../..', 'buketsUpload');
    const dependency = 'refresh-controller.v1.3.1.js';
    const js = readFileSync(resolve(root, 'refresh-controller.js'));
    const original = readFileSync(resolve(root, 'index.html'), 'utf8');
    if (!original.includes('refresh-controller.js?v=1.3.0')) throw new Error('Unexpected dependency reference');
    const html = Buffer.from(original.replace('refresh-controller.js?v=1.3.0', dependency));
    const record = { previousHash: hash(previous.bytes), newHash: hash(html), backupKey,
        contentType: previous.contentType, cacheControl: previous.cacheControl, dependency };
    writeFileSync(recordPath, JSON.stringify(record, null, 2), { flag: 'wx' });
    await client.send(new PutObjectCommand({ Bucket: bucket, Key: dependency, Body: js,
        ContentType: 'application/javascript; charset=utf-8', CacheControl: 'public, max-age=31536000, immutable',
        IfNoneMatch: '*', ACL: 'public-read' }));
    await verify(dependency, js);
    await client.send(new PutObjectCommand({ Bucket: bucket, Key: 'index.html', Body: html,
        ContentType: mime, CacheControl: 'no-store', IfMatch: previous.etag, ACL: 'public-read' }));
    try { await verify('index.html', html); }
    catch (error) {
        const current = await get('index.html');
        if (hash(current.bytes) === hash(html)) {
            await client.send(new PutObjectCommand({ Bucket: bucket, Key: 'index.html', Body: previous.bytes,
                ContentType: previous.contentType || mime, CacheControl: previous.cacheControl || 'no-cache',
                IfMatch: current.etag, ACL: 'public-read' }));
        }
        throw error;
    }
    console.log(JSON.stringify({ published: base + 'index.html', backupKey, sha256: hash(html), signingAssetsUnchanged: true }));
    client.destroy();
}
main().catch(e => { console.error(e.name, e.message); process.exitCode = 1; });
