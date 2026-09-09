"""拷贝纸/日本纸行级图片 (copy_paper_images 表) 模型层测试。

Task 1 of 2026-09-06-copy-paper-row-buttons:
- DDL: copy_paper_images
- Model: CopyPaperImage (create / list_by_record / get_by_id / update_count / delete)
- 不进 OCR pipeline,不进 match-col,仅供人工参考 + 行级 total 比对
"""
import io
import os
import pytest
from models._db import get_db, DB_PATH
from models.orders import CopyPaperImage
from blueprints._helpers import compute_copy_paper_expected_quantity


@pytest.fixture(scope="module", autouse=True)
def _ensure_table():
    """确保 copy_paper_images 表存在(conftest 只复制生产 db,
    新 DDL 需要 init_db() 主动跑一次)。module-scope 避免每个测试都跑全量迁移。"""
    from models import init_db
    init_db()
    yield


@pytest.fixture
def fresh_record(tmp_path):
    """Insert a minimal shipping_orders + shipping_records row, return record_pk.

    使用 uuid 后缀避免 UNIQUE(date,customer,order_num) 冲突 — 多次调用 fresh_record
    (跨测试) 不会撞键。
    """
    import uuid as _uuid
    cust = 'test-' + _uuid.uuid4().hex[:8]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO shipping_orders (date, customer, is_locked, created_at) "
        "VALUES (?, ?, 0, '2026-09-06 12:00:00')",
        ('2026-09-06', cust,))
    order_id = cur.lastrowid
    cur.execute(
        "INSERT INTO shipping_records (order_pk, product_name, specification, quantity, unit, created_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-09-06 12:00:00')",
        (order_id, '拷贝纸A', 'A4', 5, '令'))
    record_id = cur.lastrowid
    conn.commit()
    conn.close()
    return record_id


def test_ddl_table_exists():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='copy_paper_images'")
    assert cur.fetchone() is not None
    conn.close()


def test_create_and_list(fresh_record):
    rid = fresh_record
    img_id = CopyPaperImage.create(rid, 'upload/2026-09/x.jpg', 'x.jpg', 'label')
    assert isinstance(img_id, int) and img_id > 0

    rows = CopyPaperImage.list_by_record(rid)
    assert len(rows) == 1
    assert rows[0]['source'] == 'label'
    assert rows[0]['sheet_count'] is None
    assert rows[0]['file_path'] == 'upload/2026-09/x.jpg'


def test_update_count_int_and_none(fresh_record):
    rid = fresh_record
    iid = CopyPaperImage.create(rid, 'upload/2026-09/y.jpg', 'y.jpg', 'count')
    assert CopyPaperImage.update_count(iid, 100) is True
    assert CopyPaperImage.get_by_id(iid)['sheet_count'] == 100
    assert CopyPaperImage.update_count(iid, None) is True
    assert CopyPaperImage.get_by_id(iid)['sheet_count'] is None


def test_update_count_rejects_negative(fresh_record):
    rid = fresh_record
    iid = CopyPaperImage.create(rid, 'p', 'p', 'count')
    with pytest.raises(ValueError):
        CopyPaperImage.update_count(iid, -1)


def test_get_by_id_and_delete(fresh_record):
    rid = fresh_record
    iid = CopyPaperImage.create(rid, 'upload/2026-09/z.jpg', 'z.jpg', 'label')
    row = CopyPaperImage.get_by_id(iid)
    assert row is not None and row['id'] == iid

    assert CopyPaperImage.delete(iid) is True
    assert CopyPaperImage.get_by_id(iid) is None
    # 二次删应返回 False
    assert CopyPaperImage.delete(iid) is False


def test_helper_ling_positive():
    assert compute_copy_paper_expected_quantity(5, '令') == (5.0, True)
    assert compute_copy_paper_expected_quantity('3', '令') == (3.0, True)
    assert compute_copy_paper_expected_quantity('2.5', '令') == (2.5, True)


def test_helper_zhang_positive():
    assert compute_copy_paper_expected_quantity(500, '张') == (500.0, True)


def test_helper_unsupported_unit():
    assert compute_copy_paper_expected_quantity(5, '支') == (0.0, False)
    assert compute_copy_paper_expected_quantity(5, '码') == (0.0, False)
    assert compute_copy_paper_expected_quantity(5, None) == (0.0, False)


def test_helper_invalid_quantity():
    assert compute_copy_paper_expected_quantity(0, '令') == (0.0, False)
    assert compute_copy_paper_expected_quantity(None, '张') == (0.0, False)
    assert compute_copy_paper_expected_quantity('', '张') == (0.0, False)
    assert compute_copy_paper_expected_quantity('abc', '张') == (0.0, False)
    assert compute_copy_paper_expected_quantity(-3, '令') == (0.0, False)


# ─────────────────────────────────────────────────────────
# Task 3 (2026-09-06): 4 REST endpoints HTTP tests
# ─────────────────────────────────────────────────────────

def _png_bytes():
    """最小有效 PNG 字节(用 Pillow)。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (10, 10), 'white').save(buf, format='PNG')
    return buf.getvalue()


def test_http_upload_label(client, fresh_record):
    """POST label 图 → 200 + DB 落盘 + GET 能查到。"""
    rid = fresh_record
    data = {
        'source': 'label',
        'image': (io.BytesIO(_png_bytes()), 'test.png'),
    }
    resp = client.post(
        f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
        data=data,
        content_type='multipart/form-data',
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    j = resp.get_json()
    assert j['success'] is True
    assert j['image']['source'] == 'label'
    assert j['image']['sheet_count'] is None
    assert j['image']['original_name'] == 'test.png'
    assert 'file_path' in j['image'] and j['image']['file_path']
    assert 'relative_path' in j['image'] and j['image']['relative_path']

    # list 应能查到
    resp = client.get(f'/api/v1/shipping-orders/records/{rid}/copy-paper-images')
    j = resp.get_json()
    assert j['success'] is True and len(j['images']) == 1
    assert j['images'][0]['source'] == 'label'


def test_http_upload_count(client, fresh_record):
    """POST count 图 → 200,返 iid 给后续测试复用。"""
    rid = fresh_record
    data = {
        'source': 'count',
        'image': (io.BytesIO(_png_bytes()), 'c.png'),
    }
    resp = client.post(
        f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
        data=data,
        content_type='multipart/form-data',
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    j = resp.get_json()
    assert j['success'] is True
    assert j['image']['source'] == 'count'
    return j['image']['id']


def test_http_patch_sheet_count(client, fresh_record):
    """PATCH sheet_count: 正数 → 200, null 清空 → 200, 负数 → 400。"""
    iid = test_http_upload_count(client, fresh_record)

    # 正数
    resp = client.patch(
        f'/api/v1/shipping-orders/copy-paper-images/{iid}/sheet-count',
        json={'sheet_count': 250},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    j = resp.get_json()
    assert j['success'] is True
    assert j['sheet_count'] == 250
    assert CopyPaperImage.get_by_id(iid)['sheet_count'] == 250

    # null 清空
    resp = client.patch(
        f'/api/v1/shipping-orders/copy-paper-images/{iid}/sheet-count',
        json={'sheet_count': None},
    )
    assert resp.status_code == 200
    assert resp.get_json()['sheet_count'] is None
    assert CopyPaperImage.get_by_id(iid)['sheet_count'] is None

    # 负值 400
    resp = client.patch(
        f'/api/v1/shipping-orders/copy-paper-images/{iid}/sheet-count',
        json={'sheet_count': -1},
    )
    assert resp.status_code == 400


def test_http_delete(client, fresh_record):
    """DELETE: 200,二次删 404。"""
    rid = fresh_record
    data = {
        'source': 'label',
        'image': (io.BytesIO(_png_bytes()), 'd.png'),
    }
    resp = client.post(
        f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
        data=data,
        content_type='multipart/form-data',
    )
    iid = resp.get_json()['image']['id']

    # 第一次删 200
    resp = client.delete(f'/api/v1/shipping-orders/copy-paper-images/{iid}')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['success'] is True

    # DB 中已无此 id
    assert CopyPaperImage.get_by_id(iid) is None

    # 二次删 404
    resp = client.delete(f'/api/v1/shipping-orders/copy-paper-images/{iid}')
    assert resp.status_code == 404


def test_http_locked_blocked(client, fresh_record):
    """锁单后上传应 403。"""
    rid = fresh_record
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE shipping_orders SET is_locked = 1 "
        "WHERE id = (SELECT order_pk FROM shipping_records WHERE id = ?)",
        (rid,),
    )
    conn.commit()
    conn.close()

    data = {
        'source': 'label',
        'image': (io.BytesIO(_png_bytes()), 'l.png'),
    }
    resp = client.post(
        f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
        data=data,
        content_type='multipart/form-data',
    )
    assert resp.status_code == 403, resp.get_data(as_text=True)
    j = resp.get_json()
    assert j['success'] is False


# ─────────────────────────────────────────────────────────
# Task 4 (2026-09-06): record 级别富化函数测试
# ─────────────────────────────────────────────────────────

from blueprints.shipping import _enrich_copy_paper_for_item, _is_copy_paper_item


def _make_item(rid, qty, unit, name='拷贝纸A'):
    return {'id': rid, 'product_name': name, 'specification': '',
            'quantity': qty, 'unit': unit, 'remark': ''}


def test_enrich_green_ling(fresh_record):
    """拷贝纸 q=5令 + sheet_count=2+3 → match='green'."""
    rid = fresh_record
    iid1 = CopyPaperImage.create(rid, 'p1.jpg', 'p1.jpg', 'count')
    iid2 = CopyPaperImage.create(rid, 'p2.jpg', 'p2.jpg', 'count')
    CopyPaperImage.update_count(iid1, 2)
    CopyPaperImage.update_count(iid2, 3)
    item = _make_item(rid, 5, '令')
    _enrich_copy_paper_for_item(item)
    assert item['is_copy_paper'] is True
    assert item['copy_paper_total'] == 5
    assert item['copy_paper_match'] == 'green'


def test_enrich_yellow_ling(fresh_record):
    """q=5令 + sheet_count=4 → match='yellow'."""
    rid = fresh_record
    iid = CopyPaperImage.create(rid, 'p.jpg', 'p.jpg', 'count')
    CopyPaperImage.update_count(iid, 4)
    item = _make_item(rid, 5, '令')
    _enrich_copy_paper_for_item(item)
    assert item['copy_paper_match'] == 'yellow'


def test_enrich_partial(fresh_record):
    """q=5令 + 2 张图,1 张未录 → match='partial'."""
    rid = fresh_record
    CopyPaperImage.create(rid, 'p1.jpg', 'p1.jpg', 'count')
    CopyPaperImage.create(rid, 'p2.jpg', 'p2.jpg', 'count')
    iid = CopyPaperImage.list_by_record(rid)[0]['id']
    CopyPaperImage.update_count(iid, 2)
    item = _make_item(rid, 5, '令')
    _enrich_copy_paper_for_item(item)
    assert item['copy_paper_match'] == 'partial'
    assert item['copy_paper_total'] == 2


def test_enrich_no_match_when_no_quantity(fresh_record):
    """q=0 → match=None."""
    rid = fresh_record
    CopyPaperImage.create(rid, 'p.jpg', 'p.jpg', 'count')
    item = _make_item(rid, 0, '令')
    _enrich_copy_paper_for_item(item)
    assert item['copy_paper_match'] is None


def test_enrich_non_copy_paper_item(fresh_record):
    """杂胶袋 → is_copy_paper=False."""
    rid = fresh_record
    item = _make_item(rid, 5, '支', name='杂胶袋')
    _enrich_copy_paper_for_item(item)
    assert item['is_copy_paper'] is False
    assert item['copy_paper_match'] is None


def test_enrich_sets_expected_field(fresh_record):
    """Task 6: copy_paper_expected 字段写入(模板徽章文案依赖)。"""
    rid = fresh_record
    item = _make_item(rid, 5, '令')
    _enrich_copy_paper_for_item(item)
    assert item['copy_paper_expected'] == 5.0

    # 非法 unit 期望值算不出 → None
    item = _make_item(rid, 5, '支')
    _enrich_copy_paper_for_item(item)
    assert item['copy_paper_expected'] is None

    # 非 copy-paper 商品 → 仍写 None(模板安全)
    item = _make_item(rid, 5, '支', name='杂胶袋')
    _enrich_copy_paper_for_item(item)
    assert item['copy_paper_expected'] is None


# ─────────────────────────────────────────────────────────
# Task 9 (2026-09-06): 端到端 流程测试
# ─────────────────────────────────────────────────────────

def test_e2e_full_flow(client, fresh_record):
    """端到端: 上传 label + 上传 count + 录入张数 → match green。

    覆盖:
    - POST 上传 label 图 (multipart)
    - POST 上传 count 图 (multipart)
    - PATCH sheet_count=5
    - _enrich_copy_paper_for_item 计算 match='green' / total=5 / images=2
    """
    rid = fresh_record

    # 1. 上传 label 图
    data = {'source': 'label',
            'image': (io.BytesIO(_png_bytes()), 'lbl.png')}
    resp = client.post(f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
                       data=data, content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    j = resp.get_json()
    assert j['success'] is True
    assert j['image']['source'] == 'label'

    # 2. 上传 count 图
    data = {'source': 'count',
            'image': (io.BytesIO(_png_bytes()), 'cnt.png')}
    resp = client.post(f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
                       data=data, content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_data(as_text=True)
    j = resp.get_json()
    assert j['success'] is True
    iid = j['image']['id']
    assert iid and iid > 0

    # 3. 录入张数 5
    resp = client.patch(f'/api/v1/shipping-orders/copy-paper-images/{iid}/sheet-count',
                        json={'sheet_count': 5})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()['sheet_count'] == 5

    # 4. enrichment 应为 green (quantity=5 令)
    item = _make_item(rid, 5, '令')
    _enrich_copy_paper_for_item(item)
    assert item['is_copy_paper'] is True
    assert item['copy_paper_match'] == 'green'
    assert item['copy_paper_total'] == 5
    assert item['copy_paper_expected'] == 5.0
    assert len(item['copy_paper_images']) == 2


# ─────────────────────────────────────────────────────────
# Task 9 防回归 (2026-09-08): 标签/张数按钮必须带 onclick 属性
# 否则点击没反应,见 debug 笔记 "出货页面 TD-2026-09-08-003 日本纸 按钮无反应"
# ─────────────────────────────────────────────────────────

def test_template_buttons_have_onclick():
    """copy-paper-label-btn / copy-paper-count-btn 必须带 onclick=\"openCopyPaperUpload(...)\"。

    2026-09-08 bug: Task 5 implementer 漏了 onclick,导致点击无反应。
    改为正则强校验 onclick + 三个参数(recordPk, orderPk, source),不再 silently render inert buttons。
    """
    import re
    with open('templates/shipping-records.html', encoding='utf-8') as f:
        html = f.read()

    # 模板里 onclick 的参数是 Jinja 占位符 {{ item.id }} / {{ group.id }} (源码态),
    # 渲染后才是真实数字。两者都得接受。
    label_btn_re = re.compile(
        r'<button[^>]*class="[^"]*copy-paper-label-btn[^"]*"[^>]*'
        r'onclick="openCopyPaperUpload\(\'([\d{}a-zA-Z_\. ]+)\',\'([\d{}a-zA-Z_\. ]+)\',\'label\'\)"',
    )
    count_btn_re = re.compile(
        r'<button[^>]*class="[^"]*copy-paper-count-btn[^"]*"[^>]*'
        r'onclick="openCopyPaperUpload\(\'([\d{}a-zA-Z_\. ]+)\',\'([\d{}a-zA-Z_\. ]+)\',\'count\'\)"',
    )

    label_matches = label_btn_re.findall(html)
    count_matches = count_btn_re.findall(html)
    assert len(label_matches) >= 1, 'templates/shipping-records.html 缺 copy-paper-label-btn onclick'
    assert len(count_matches) >= 1, 'templates/shipping-records.html 缺 copy-paper-count-btn onclick'
    # 占位符 (item.id / group.id) 或真实数字都算合法
    for rid, oid in label_matches:
        ok = rid in ('{{ item.id }}',) or rid.isdigit()
        ok2 = oid in ('{{ group.id }}',) or oid.isdigit()
        assert ok and ok2, f'标签按钮参数错: ({rid!r}, {oid!r})'
    for rid, oid in count_matches:
        ok = rid in ('{{ item.id }}',) or rid.isdigit()
        ok2 = oid in ('{{ group.id }}',) or oid.isdigit()
        assert ok and ok2, f'张数按钮参数错: ({rid!r}, {oid!r})'


# ─────────────────────────────────────────────────────────
# Task 1 (2026-09-09): 拷贝纸标签图渲染归属测试
# ─────────────────────────────────────────────────────────

def test_label_image_renders_in_order_images_area(client, fresh_record):
    """拷贝纸 label 图 → 渲染在 order-images-area (.img-item-copy-paper-label)"""
    rid = fresh_record
    # 登录(必须,shipping-records 页有 before_request gate)
    _login_test_user(client)
    # 上传 1 张 label 图 + 1 张 count 图
    client.post(
        f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
        data={'source': 'label', 'image': (io.BytesIO(_png_bytes()), 'lbl.png')},
        content_type='multipart/form-data',
    )
    client.post(
        f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
        data={'source': 'count', 'image': (io.BytesIO(_png_bytes()), 'cnt.png')},
        content_type='multipart/form-data',
    )
    resp = client.get('/shipping-records?start_date=2026-09-06&end_date=2030-01-01')
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200

    # 作用域:本 record 所在的 date-group
    import re
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT order_pk FROM shipping_records WHERE id = ?', (rid,))
    order_id = cur.fetchone()['order_pk']
    conn.close()

    tr_re = re.compile(rf'<tr[^>]*data-record-id="{rid}"[^>]*>')
    tr_match = tr_re.search(html)
    assert tr_match, f'找不到 record {rid} 对应的 <tr>'
    grp_start = html.rfind('<div class="date-group"', 0, tr_match.end())
    grp_end = html.find('<div class="date-group"', tr_match.end())
    if grp_end == -1:
        grp_end = len(html)
    grp_html = html[grp_start:grp_end]

    assert 'img-item-copy-paper-label' in grp_html, \
        f'order {order_id} 的 label 图应渲染 .img-item-copy-paper-label class'
    assert 'copy-paper-label-del-btn' in grp_html, \
        f'order {order_id} 的 label 图应带 .copy-paper-label-del-btn 删除按钮'
    assert '拷贝纸标签' in grp_html, \
        f'order {order_id} 的 label 图应有「拷贝纸标签」水印标识'


def test_count_image_still_renders_in_copy_paper_area(client, fresh_record):
    """拷贝纸 count 图 → 仍渲染在 copy-paper-area,不出现在 order-images-area"""
    rid = fresh_record
    _login_test_user(client)
    # 只上传 1 张 count 图
    client.post(
        f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
        data={'source': 'count', 'image': (io.BytesIO(_png_bytes()), 'cnt.png')},
        content_type='multipart/form-data',
    )
    resp = client.get('/shipping-records?start_date=2026-09-06&end_date=2030-01-01')
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200

    # 找到本 record 所属的 date-group(整张订单)
    # date-group 的 data-order-id 与 record 的 order_id 对应
    import re
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT order_pk FROM shipping_records WHERE id = ?', (rid,))
    order_id = cur.fetchone()['order_pk']
    conn.close()

    # date-group 是 <div class="date-group" data-order-id="<order_id>" ...>...</div>
    # 在它里面找本 record 的 <tr data-record-id="<rid>">
    tr_re = re.compile(
        rf'<tr[^>]*data-record-id="{rid}"[^>]*>',
    )
    tr_match = tr_re.search(html)
    assert tr_match, f'找不到 record {rid} 对应的 <tr>'

    # date-group 边界(下一个 date-group 之前)
    grp_start = html.rfind('<div class="date-group"', 0, tr_match.end())
    grp_end = html.find('<div class="date-group"', tr_match.end())
    if grp_end == -1:
        grp_end = html.find('<!-- 空状态', tr_match.end())
    if grp_end == -1:
        grp_end = len(html)
    grp_html = html[grp_start:grp_end]

    # 本 record 所在 date-group 内:
    # - 应有 copy-paper-area (count 图渲染)
    # - 不应有 img-item-copy-paper-label (本 record 只传了 count)
    assert 'copy-paper-area' in grp_html, \
        f'order {order_id} 应有 copy-paper-area 渲染 count 图'
    assert 'img-item-copy-paper-label' not in grp_html, \
        f'order {order_id} 只上传了 count, 不应有 label 图'


def _login_test_user(client):
    """测试用登录 helper:拿到任意一个 active staff_id 用于登录。
    若没有 active staff 就跳过(让后续 GET 仍 302,测试失败信息更清楚)。"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT id FROM staff WHERE is_active = 1 LIMIT 1')
    row = cur.fetchone()
    conn.close()
    if row is None:
        return
    sid = row['id']
    # 走 GET /login 拿 cookie + 跳转 — 测试 client 跟会话
    client.get('/login')
    client.post('/login', data={'staff_id': sid})
