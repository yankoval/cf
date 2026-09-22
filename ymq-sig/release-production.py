"""Controlled production API release. Secrets stay in memory. Gateway uses a rollbackable tag."""
import argparse
import base64
import io
import json
from pathlib import Path
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'release-20260922'
FUNCTION = 'd4ellill0lftr0somfne'
OLD = 'd4ejvpekbv3p3fn7ep8h'
TESTED = 'd4ecrdatmhe1v6htpc67'
GATEWAY = 'd5d6kfk1rvhoeodarkrj'
URL = 'https://d5d6kfk1rvhoeodarkrj.p8361f8z.apigw.yandexcloud.net'
TAG = 'production-stable'

def yc(*args):
    return json.loads(subprocess.check_output(['yc', *args, '--format', 'json'], text=True, timeout=55))

def check(url, key, action, params=None, token=None):
    headers = {'Content-Type': 'application/json', 'X-Api-Key': key}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, data=json.dumps({'action': action, 'params': params or {}}).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=35) as response:
        result = json.load(response)
        assert response.status == 200
        return result

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('phase', choices=['prepare', 'deploy', 'activate', 'rollback'])
args = parser.parse_args()
old = yc('serverless', 'function', 'version', 'get', OLD)
key = old['environment']['API_KEY']

if args.phase == 'prepare':
    current = yc('serverless', 'function', 'version', 'get-by-tag', '--function-id', FUNCTION, '--tag', '$latest')
    assert current['id'] == OLD, 'Production changed: review before release'
    spec = subprocess.check_output(['yc', 'serverless', 'api-gateway', 'get-spec', '--id', GATEWAY], text=True, timeout=30)
    marker = '        function_id: ' + FUNCTION + '\n'
    assert spec.count(marker) == 1 and 'tag:' not in spec
    STATE.mkdir(exist_ok=True)
    with (STATE / 'gateway-original.yaml').open('x') as f:
        f.write(spec)
    with (STATE / 'gateway-stable.yaml').open('x') as f:
        f.write(spec.replace(marker, marker + '        tag: ' + TAG + '\n'))
    yc('serverless', 'function', 'version', 'set-tag', '--id', OLD, '--tag', 'rollback-20260922')
    yc('serverless', 'function', 'version', 'set-tag', '--id', OLD, '--tag', TAG)
    yc('serverless', 'api-gateway', 'update', '--id', GATEWAY, '--spec', str(STATE / 'gateway-stable.yaml'))
    assert check(URL, key, 'ping').get('pong') is True
    print(json.dumps({'prepared': True, 'rollback_version': OLD, 'gateway_tag': TAG}), flush=True)

elif args.phase == 'deploy':
    assert (STATE / 'gateway-original.yaml').exists()
    assert yc('serverless', 'function', 'version', 'get-by-tag', '--function-id', FUNCTION, '--tag', TAG)['id'] == OLD
    assert not (STATE / 'operation.json').exists(), 'Deployment already requested; inspect operation, do not duplicate'
    tested = yc('serverless', 'function', 'version', 'get', TESTED)
    env = dict(tested['environment'])
    for name in ['API_KEY', 'ACCESS_KEY_ID', 'SECRET_ACCESS_KEY', 'YMQ_QUEUE_URL']:
        assert env[name] == old['environment'][name], 'Legacy configuration differs'
    assert env['ALLOWED_BUCKETS'] == '1bf11148-3595-4a07-a089-d460153b7c7a'
    env.pop('WRITE_PREFIX', None)
    env['DISABLE_QUEUE'] = 'false'
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for name in ['index.js', 'storage-api.js', 'package.json', 'package-lock.json']:
            z.write(ROOT / name, name)
    token = subprocess.check_output(['yc', 'iam', 'create-token'], text=True, timeout=30).strip()
    payload = dict(functionId=FUNCTION, runtime='nodejs22', entrypoint='index.handler',
                   resources={'memory': '268435456'}, executionTimeout='30s', environment=env,
                   content=base64.b64encode(archive.getvalue()).decode(),
                   description='Unified SignJS/S3 API v2 production 2026-09-22')
    if old.get('service_account_id'):
        payload['serviceAccountId'] = old['service_account_id']
    # Do not silently drop specialized execution settings.
    assert not old.get('connectivity') and not old.get('named_service_accounts') and not old.get('secrets')
    req = urllib.request.Request('https://serverless-functions.api.cloud.yandex.net/functions/v1/versions',
        data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=50) as response:
        op = json.load(response)
    (STATE / 'operation.json').write_text(json.dumps({'id': op['id']}))
    print(json.dumps({'operation': op['id'], 'gateway_still_old': True}), flush=True)

elif args.phase == 'activate':
    operation = json.loads((STATE / 'operation.json').read_text())['id']
    op = yc('operation', 'get', operation)
    assert op.get('done') and not op.get('error'), 'Version not ready'
    version = op['response']['id']
    assert yc('serverless', 'function', 'version', 'get-by-tag', '--function-id', FUNCTION, '--tag', TAG)['id'] == OLD
    yc('serverless', 'function', 'version', 'set-tag', '--id', version, '--tag', 'release-20260922')
    token = subprocess.check_output(['yc', 'iam', 'create-token'], text=True, timeout=30).strip()
    candidate = 'https://functions.yandexcloud.net/' + FUNCTION + '?tag=release-20260922'
    assert check(candidate, key, 'capabilities', token=token)['apiVersion'] == 2
    check(candidate, key, 'list-objects-v2', {'Prefix': 'Задания/', 'MaxKeys': 1}, token=token)
    check(candidate, key, 'GetQueueAttributes', {'AttributeNames': ['ApproximateNumberOfMessages']}, token=token)
    try:
        yc('serverless', 'function', 'version', 'set-tag', '--id', version, '--tag', TAG)
        assert check(URL, key, 'capabilities')['apiVersion'] == 2
        check(URL, key, 'list-objects-v2', {'Prefix': 'Задания/', 'MaxKeys': 1})
        check(URL, key, 'GetQueueAttributes', {'AttributeNames': ['ApproximateNumberOfMessages']})
    except Exception:
        yc('serverless', 'function', 'version', 'set-tag', '--id', OLD, '--tag', TAG)
        raise
    (STATE / 'active.json').write_text(json.dumps({'version': version, 'previous': OLD, 'url': URL}))
    print(json.dumps({'active_version': version, 'gateway_verified': True}), flush=True)

elif args.phase == 'rollback':
    yc('serverless', 'function', 'version', 'set-tag', '--id', OLD, '--tag', TAG)
    assert check(URL, key, 'ping')['pong'] is True
    print(json.dumps({'rolled_back': OLD}), flush=True)
