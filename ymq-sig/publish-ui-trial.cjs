// Publish an isolated static UI trial. Never overwrite the working site or existing objects.
const { S3Client, PutObjectCommand } = require('@aws-sdk/client-s3');
const { execFileSync } = require('node:child_process');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { createHash } = require('node:crypto');
const bucket = '20ab2a0c-2726-4ba1-9c7c-7deae82941ff';
const prefix = '_ui-tests/20260921-v1/';
const base = `https://storage.yandexcloud.net/${bucket}/`;
const hash = b => createHash('sha256').update(b).digest('hex');
async function readPublic(name) {
    const response = await fetch(base + name, { signal: AbortSignal.timeout(20000) });
    if (!response.ok) throw new Error(`Public asset ${name}: HTTP ${response.status}`);
    return Buffer.from(await response.arrayBuffer());
}
async function main() {
    const root = resolve(__dirname, '../..');
    const assets = [];
    for (const name of ['cloud-sign.html', 'CloudSignApp.js', 'style.css', 'cadesplugin_api.js']) {
        const bytes = await readPublic(name);
        if (name !== 'cadesplugin_api.js' && hash(bytes) !== hash(readFileSync(resolve(root, 'SignJS', name)))) {
            throw new Error(`Published SignJS differs from tested local file: ${name}`);
        }
        assets.push({ name, bytes });
    }
    assets.push({ name: 'refresh-controller.js', bytes: readFileSync(resolve(root, 'buketsUpload/refresh-controller.js')) });
    // Entry point last: do not expose a page before its dependencies are uploaded.
    assets.push({ name: 'index.html', bytes: readFileSync(resolve(root, 'buketsUpload/index.html')) });
    const original = await readPublic('index.html');
    const version = JSON.parse(execFileSync('yc', ['serverless', 'function', 'version', 'get',
        'd4ejvpekbv3p3fn7ep8h', '--format', 'json'], { encoding: 'utf8', timeout: 30000 }));
    const env = version.environment;
    const client = new S3Client({ endpoint: 'https://storage.yandexcloud.net', region: 'ru-central1',
        forcePathStyle: true, maxAttempts: 2,
        credentials: { accessKeyId: env.ACCESS_KEY_ID, secretAccessKey: env.SECRET_ACCESS_KEY } });
    for (const { name, bytes } of assets) {
        const contentType = name.endsWith('.html') ? 'text/html; charset=utf-8'
            : name.endsWith('.css') ? 'text/css; charset=utf-8' : 'application/javascript; charset=utf-8';
        await client.send(new PutObjectCommand({ Bucket: bucket, Key: prefix + name,
            Body: bytes, ContentType: contentType, CacheControl: 'no-store', IfNoneMatch: '*', ACL: 'public-read' }));
        if (hash(await readPublic(prefix + name)) !== hash(bytes)) throw new Error(`Verification failed: ${name}`);
        console.log(JSON.stringify({ file: prefix + name, verified: true, sha256: hash(bytes) }));
    }
    if (hash(await readPublic('index.html')) !== hash(original)) throw new Error('Working index changed during publishing');
    console.log(JSON.stringify({ url: base + prefix + 'index.html', workingIndexUnchanged: true }));
    client.destroy();
}
main().catch(error => { console.error(error.name, error.message); process.exitCode = 1; });
