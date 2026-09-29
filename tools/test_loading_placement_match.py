"""Verify the loading page's record-level placement_match (点数 button green class).

Flow:
  1. login
  2. upload one placement image to record 140
  3. set manual_count=32 (record 140 has quantity=32, unit=支, remark='-')
  4. load /loading-orders page and assert the 点数 button for record 140 has 'placement-ok' class
  5. set manual_count=99 (wrong count) and assert the class is removed
  6. cleanup: delete the placement image
"""
import urllib.request, urllib.error, http.cookiejar, json, base64, sys
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:5050'
PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAG0lEQVR4nGNgQAYi7LxwxKDBJwVHDDai6nAEADm8A8EwpfHDAAAAAElFTkSuQmCC"
png_bytes = base64.b64decode(PNG_B64)

cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def req(method, path, data=None, raw_json=False):
    h = {}
    body = None
    if data is not None:
        if raw_json:
            body = json.dumps(data).encode('utf-8')
            h['Content-Type'] = 'application/json'
        else:
            body = data.encode('utf-8')
            h['Content-Type'] = 'application/x-www-form-urlencoded'
    r = urllib.request.Request(BASE + path, data=body, method=method, headers=h)
    try:
        return op.open(r, timeout=30).status, op.open(r, timeout=30).read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'ignore')

# 1) login
s, _ = req('POST', '/login', 'staff_id=6221')
assert s == 200, 'login failed'

# 2) upload placement image (use a fresh request, not the function above, to capture body)
def post_raw(path, data, content_type='application/json'):
    body = json.dumps(data).encode('utf-8') if content_type == 'application/json' else data.encode('utf-8')
    r = urllib.request.Request(BASE + path, data=body, method='POST', headers={'Content-Type': content_type})
    try:
        resp = op.open(r, timeout=30)
        return resp.status, resp.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'ignore')

s, b = post_raw('/api/v1/loading-orders/records/140/placement-images',
                {'image': 'data:image/png;base64,' + PNG_B64})
assert s == 201, f'upload failed: {s} {b}'
iid = json.loads(b)['images'][0]['image_id']
print('uploaded image_id=', iid)

# 3) set manual_count=32 (match)
s, b = post_raw(f'/api/v1/loading-orders/placement-images/{iid}/manual-count', {'count': 32})
assert s == 200, f'manual-count failed: {s} {b}'
print('manual_count=32 set')

# 4) open the page and check the button class
with sync_playwright() as p:
    b2 = p.chromium.launch()
    ctx = b2.new_context()
    pg = ctx.new_page()
    # piggyback on the existing session cookie
    cookie_value = next((c.value for c in cj if c.name == 'session'), None)
    if cookie_value:
        ctx.add_cookies([{'name': 'session', 'value': cookie_value, 'url': BASE}])
    pg.goto(BASE + '/loading-orders?start_date=2026-06-01&end_date=2026-12-31', wait_until='networkidle')
    btn = pg.query_selector('.placement-add-btn[data-record-id="140"]')
    if not btn:
        print('FAIL: 点数 button not found for record 140')
        b2.close()
        sys.exit(1)
    cls = btn.get_attribute('class') or ''
    has_ok = 'placement-ok' in cls
    print('button class after manual_count=32:', cls, '-> has placement-ok?', has_ok)
    if not has_ok:
        print('FAIL: button should be green when manual_count matches quantity')
        b2.close()
        sys.exit(1)
    b2.close()

# 5) set manual_count=99 (no match) and re-check
s, b = post_raw(f'/api/v1/loading-orders/placement-images/{iid}/manual-count', {'count': 99})
assert s == 200, f'manual-count=99 failed: {s} {b}'

with sync_playwright() as p:
    b2 = p.chromium.launch()
    ctx = b2.new_context()
    pg = ctx.new_page()
    cookie_value = next((c.value for c in cj if c.name == 'session'), None)
    if cookie_value:
        ctx.add_cookies([{'name': 'session', 'value': cookie_value, 'url': BASE}])
    pg.goto(BASE + '/loading-orders?start_date=2026-06-01&end_date=2026-12-31', wait_until='networkidle')
    btn = pg.query_selector('.placement-add-btn[data-record-id="140"]')
    cls = btn.get_attribute('class') or ''
    has_ok = 'placement-ok' in cls
    print('button class after manual_count=99:', cls, '-> has placement-ok?', has_ok)
    if has_ok:
        print('FAIL: button should NOT be green when count does not match')
        b2.close()
        sys.exit(1)
    b2.close()

# 6) cleanup
s, b = req('DELETE', f'/api/v1/loading-orders/placement-images/{iid}')
print('cleanup delete:', s)
print('\nALL_OK')
