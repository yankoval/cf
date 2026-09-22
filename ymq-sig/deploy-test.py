"""Deploy only the isolated test function; secrets stay in process memory."""
import base64
import argparse
import io
import json
from pathlib import Path
import subprocess
import urllib.request
import zipfile

NAME = 'sign-storage-api-test'
FOLDER = 'b1g66di24cjhduu1tdoc'
BUCKET = '1bf11148-3595-4a07-a089-d460153b7c7a'

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--enable-production-queue', action='store_true',
                    help='Explicitly approved live signing trial; only the isolated test function changes')
args = parser.parse_args()

def yc(*args):
    return json.loads(subprocess.check_output(['yc', *args, '--format', 'json'], text=True))

functions = yc('serverless', 'function', 'list', '--folder-id', FOLDER)
target = next((f for f in functions if f['name'] == NAME), None)
if target is None:
    target = yc('serverless', 'function', 'create', '--name', NAME, '--folder-id', FOLDER,
                '--description', 'Isolated unified SignJS/S3 API test; writes restricted; production queue disabled')
assert target['name'] == NAME
source = yc('serverless', 'function', 'version', 'get', 'd4ejvpekbv3p3fn7ep8h')
env = dict(source['environment'])
storage_source = yc('serverless', 'function', 'version', 'get', 'd4e52os4km745hfjjk2n')['environment']
assert storage_source['BUCKET_NAME'] == BUCKET
env.update(STORAGE_ACCESS_KEY_ID=storage_source['AWS_ACCESS_KEY_ID'],
           STORAGE_SECRET_ACCESS_KEY=storage_source['AWS_SECRET_ACCESS_KEY'])
assert env.get('API_KEY') and env.get('ACCESS_KEY_ID') and env.get('SECRET_ACCESS_KEY')
env.update(DEFAULT_BUCKET=BUCKET, ALLOWED_BUCKETS=BUCKET, WRITE_PREFIX='_api-tests/20260918/',
           DISABLE_QUEUE='false' if args.enable_production_queue else 'true',
           S3_FORCE_PATH_STYLE='true', URL_EXPIRATION='900', CLIENT_TIMEOUT='3000')
archive = io.BytesIO()
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
    for name in ['index.js', 'storage-api.js', 'package.json', 'package-lock.json']:
        z.write(Path(__file__).parent / name, name)
token = subprocess.check_output(['yc', 'iam', 'create-token'], text=True).strip()
payload = dict(functionId=target['id'], runtime='nodejs22', entrypoint='index.handler',
               resources={'memory': '268435456'}, executionTimeout='30s', environment=env,
               content=base64.b64encode(archive.getvalue()).decode(),
               description=('Unified API v2 live signing trial; production queue enabled; storage writes restricted'
                            if args.enable_production_queue else 'Unified API v2 test; no production queue operations'))
request = urllib.request.Request('https://serverless-functions.api.cloud.yandex.net/functions/v1/versions',
    data=json.dumps(payload).encode(), headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
with urllib.request.urlopen(request, timeout=60) as response:
    operation = json.load(response)
print(json.dumps({'function_id': target['id'], 'operation_id': operation['id'],
                  'url': 'https://functions.yandexcloud.net/' + target['id']}))
