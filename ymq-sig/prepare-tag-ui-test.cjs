const { execFileSync } = require('node:child_process');
const { randomUUID } = require('node:crypto');
async function main() {
    const source = JSON.parse(execFileSync('yc', ['serverless', 'function', 'version', 'get',
        'd4ejvpekbv3p3fn7ep8h', '--format', 'json'], { encoding: 'utf8', timeout: 30000 }));
    const key = `_api-tests/20260918/ui-tag-check-${randomUUID()}.txt`;
    async function api(action, params) {
        const r = await fetch('https://functions.yandexcloud.net/d4emlmsp7hd8gr65scac', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Api-Key': source.environment.API_KEY },
            body: JSON.stringify({ action, params }), signal: AbortSignal.timeout(30000) });
        if (!r.ok) throw new Error(`${action}: HTTP ${r.status}`);
        return r.json();
    }
    const upload = await api('get-upload-url', { Key: key });
    const r = await fetch(upload.url, { method: 'PUT', headers: upload.headers,
        body: 'UI tag update test only. Not a signing task. No production document.\n',
        signal: AbortSignal.timeout(30000) });
    if (!r.ok) throw new Error(`Upload: HTTP ${r.status}`);
    await api('set-object-tag', { Key: key, TagKey: 'keep', TagValue: 'yes' });
    const tags = await api('get-object-tagging', { Key: key });
    console.log(JSON.stringify({ key, tags: tags.TagSet }));
}
main().catch(e => { console.error(e.name, e.message); process.exitCode = 1; });
