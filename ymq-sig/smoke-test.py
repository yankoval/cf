"""Exercise the isolated API. Writes only a uniquely named test JSON object."""
import json
import subprocess
import time
import urllib.request
import urllib.error
import uuid
import socket

# This workstation's IPv6 route to the cloud endpoint is intermittent.
original_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda host, port, family=0, type=0, proto=0, flags=0: original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

URL = 'https://functions.yandexcloud.net/d4emlmsp7hd8gr65scac'
BUCKET = '1bf11148-3595-4a07-a089-d460153b7c7a'
source = json.loads(subprocess.check_output(['yc', 'serverless', 'function', 'version', 'get',
    'd4ejvpekbv3p3fn7ep8h', '--format', 'json'], text=True))
api_key = source['environment']['API_KEY']

def call(action, params=None, key=api_key):
    request = urllib.request.Request(URL, data=json.dumps({'action': action, 'params': params or {}}).encode(),
        headers={'Content-Type': 'application/json', 'X-Api-Key': key})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=35) as r:
            return r.status, json.load(r), round(time.monotonic() - start, 3)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e), round(time.monotonic() - start, 3)

assert call('ping', key='')[0] == 403
assert call('ping', key='wrong')[0] == 403
assert call('capabilities')[1]['apiVersion'] == 2
assert call('GetQueueAttributes', {'AttributeNames': ['ApproximateNumberOfMessages']})[0] == 403
cursor = None
count = 0
pages = []
first_keys = []
while True:
    params = {'Bucket': BUCKET, 'Prefix': 'Задания/', 'Delimiter': '/', 'MaxKeys': 500}
    if cursor:
        params['ContinuationToken'] = cursor
    status, data, elapsed = call('list-objects-v2', params)
    assert status == 200, (status, data)
    objects = data.get('Contents', [])
    count += len(objects)
    pages.append(elapsed)
    print(json.dumps({'page': len(pages), 'objects_so_far': count, 'seconds': elapsed}), flush=True)
    first_keys.extend(o['Key'] for o in objects[:max(0, 20-len(first_keys))])
    if not data.get('IsTruncated'):
        break
    cursor = data['NextContinuationToken']
for key in first_keys:
    status, data, _ = call('get-object-tagging', {'Bucket': BUCKET, 'Key': key})
    assert status == 200 and isinstance(data['TagSet'], list)
assert call('set-object-tag', {'Key': 'Задания/do-not-write', 'TagKey': 'test', 'TagValue': 'true'})[0] == 403
key = '_api-tests/20260918/probe-' + str(uuid.uuid4()) + '.json'
status, upload, _ = call('get-upload-url', {'Key': key})
assert status == 200
content = b'{"test":"unified-api-v2","production":false}'
request = urllib.request.Request(upload['url'], data=content, method='PUT', headers=upload['headers'])
with urllib.request.urlopen(request, timeout=25) as r:
    assert r.status == 200
try:
    urllib.request.urlopen(request, timeout=25)
    raise AssertionError('Overwrite unexpectedly succeeded')
except urllib.error.HTTPError as e:
    assert e.code == 412
for name, value in [('keep', 'yes'), ('check', 'finished')]:
    assert call('set-object-tag', {'Key': key, 'TagKey': name, 'TagValue': value})[0] == 200
status, changed, _ = call('remove-object-tag', {'Key': key, 'TagKey': 'check'})
assert status == 200 and changed['TagSet'] == [{'Key': 'keep', 'Value': 'yes'}]
status, preview, _ = call('get-preview-url', {'Key': key})
assert status == 200
with urllib.request.urlopen(preview['url'], timeout=25) as r:
    assert r.read() == content
    assert r.headers.get_content_type() == 'text/plain'
    assert r.headers['Content-Disposition'] == 'inline'
print(json.dumps({'passed': True, 'folder': 'Задания/', 'objects': count, 'pages': len(pages),
    'max_page_seconds': max(pages), 'tags_checked': len(first_keys), 'test_object': key,
    'checks': ['authentication', 'pagination', 'tags', 'write restriction', 'queue restriction',
               'upload', 'overwrite rejected', 'set/remove tag', 'preview']}, ensure_ascii=False))
