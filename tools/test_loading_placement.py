import urllib.request, urllib.error, http.cookiejar, json, sqlite3, base64

BASE = 'http://127.0.0.1:5050'
# minimal 1x1 png
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def req(method, path, data=None, raw_json=False):
    url = BASE + path
    h = {}
    body = None
    if data is not None:
        if raw_json:
            body = json.dumps(data).encode('utf-8')
            h['Content-Type'] = 'application/json'
        else:
            body = data.encode('utf-8')
            h['Content-Type'] = 'application/x-www-form-urlencoded'
    r = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        resp = op.open(r, timeout=30)
        return resp.status, resp.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'ignore')

# 1) login
s, b = req('POST', '/login', 'staff_id=6221')
print('login:', s)

# 2) pick a loading record (prefer 支-based)
db = sqlite3.connect('worklog.db')
db.row_factory = sqlite3.Row
cur = db.cursor()
cur.execute("SELECT id, order_pk, unit FROM loading_order_records ORDER BY id DESC LIMIT 10")
rows = cur.fetchall()
db.close()
rec = None
for r in rows:
    rec = dict(r)
    if '支' in (r['unit'] or ''):
        break
rid = rec['id']
print('target record:', rid, 'unit=', rec['unit'], 'order=', rec['order_pk'])

b64url = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAG0lEQVR4nGNgQAYi7LxwxKDBJwVHDDai6nAEADm8A8EwpfHDAAAAAElFTkSuQmCC'
ok = True
s, b = req('POST', f'/api/v1/loading-orders/records/{rid}/placement-images', {'image': b64url}, raw_json=True)
print('upload:', s, b[:160]);  ok = ok and s == 201
iid = json.loads(b)['images'][0]['image_id']
print('  image_id=', iid)

s, b = req('GET', f'/api/v1/loading-orders/records/{rid}/placement-images')
print('list:', s, 'count=', len(json.loads(b).get('images', [])));  ok = ok and s == 200

s, b = req('GET', f'/api/v1/loading-orders/placement-images/{iid}')
print('get:', s, 'has image?', 'image' in json.loads(b));  ok = ok and s == 200

s, b = req('POST', f'/api/v1/loading-orders/placement-images/{iid}/mark-scale', {'scale': 1.5}, raw_json=True)
print('mark-scale:', s, b[:80]);  ok = ok and s == 200

s, b = req('POST', f'/api/v1/loading-orders/placement-images/{iid}/loose-count', {'count': 3}, raw_json=True)
print('loose-count:', s, b[:80]);  ok = ok and s == 200

s, b = req('POST', f'/api/v1/loading-orders/placement-images/{iid}/manual-count', {'count': 12}, raw_json=True)
print('manual-count:', s, b[:80]);  ok = ok and s == 200

s, b = req('POST', f'/api/v1/loading-orders/placement-images/{iid}/marks', {'x_ratio': 0.5, 'y_ratio': 0.5, 'r': 0.1}, raw_json=True)
print('marks add:', s, 'n=', len(json.loads(b).get('marks', [])));  ok = ok and s == 200

s, b = req('POST', f'/api/v1/loading-orders/placement-images/{iid}/detect')
print('detect:', s, b[:80]);  ok = ok and s == 200

s, b = req('POST', f'/api/v1/loading-orders/placement-images/{iid}/unload', {'unload': True}, raw_json=True)
print('unload:', s, b[:120]);  ok = ok and s == 200

s, b = req('DELETE', f'/api/v1/loading-orders/placement-images/{iid}/marks/last')
print('marks/last del:', s, 'n=', len(json.loads(b).get('marks', [])));  ok = ok and s == 200

s, b = req('DELETE', f'/api/v1/loading-orders/placement-images/{iid}')
print('delete:', s, b[:80]);  ok = ok and s == 200

print('\nALL_OK' if ok else '\nSOME_FAILED')
