# -*- coding: utf-8 -*-
"""阶段 6 · 盘点(移动端现场录入)的测试。

覆盖开发计划 §阶段 6 的 **3 条完成判据** + 6 条任务:

  判据 1. 手机上能建单 / 录数 / 看进度;PC 上刷新能看到手机录的数(**同一张单**)
  判据 2. 手机录完提交 → 与阶段 5 的提交路径**完全一致**(同一套 API)
  判据 3. `/m/*` 免登录白名单已覆盖(`app.py._AUTH_PUBLIC_PREFIXES`)

  6.1 列表页(仓库 / 状态过滤)      6.2 详情页(按分类分组 / 搜索)
  6.3 快速录数(大按钮 / 失焦即存)  6.4 拍照辅助(OCR 只预填,不过账)
  6.5 复用 `static/css/mobile.css` 6.6 并发写同单(最后写入生效 + 冲突提示)

⚠️ 测试跑在 worklog_test.db(conftest 从生产库复制一份),不碰生产数据。
⚠️ 排查失败必须**串行**跑 pytest —— 并发跑会抢同一个 worklog_test.db。
⚠️ 造数要满足**全库级断言**:
   · `product_stock.quantity` 必须恒等于 `SUM(stock_ledger.quantity)`(A1)
   · `stock_ledger.balance_after` 必须逐笔链连续
   —— 所以下面的 `_insert_ledger` 默认取「该 (仓,SKU) 到本行为止的累计 + 本行数量」。
"""
import datetime
import os
import re
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TOL = 1e-6


# ── 工具 ──────────────────────────────────────────────────────────
def _conn():
    from models import get_db
    return get_db()


def _make_product(name, spec='', base_unit='码', category_id=1):
    """建一个 product 行,返回 product_id。

    ⚠️ 故意关掉外键检查再插 —— 生产库的 `product` 表带一条悬空外键
       (`weight_category_id → product_weight_categories(id)`),而那张表不存在。
    """
    from models import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys = OFF')
        conn.execute(
            'INSERT INTO product (product_code, product_name, category_id, specification, '
            'base_unit, aux_unit, status, created_at, updated_at) '
            "VALUES (?, ?, ?, ?, ?, NULL, 1, datetime('now','localtime'), "
            "datetime('now','localtime'))",
            (f'S6{abs(hash((name, spec, category_id))) % 10 ** 9}', name,
             int(category_id), spec, base_unit))
        pid = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
        conn.commit()
        return pid
    finally:
        conn.close()


def _insert_ledger(pid, qty, biz_type='inbound', occurred_at=None, created_at=None,
                   memo='', uniq_key=None, balance_after=None, warehouse_id=1,
                   operator_id=None):
    """直接写一条 stock_ledger(**可控 created_at**)。

    ⚠️ `balance_after` 默认取「该 (仓,SKU) 到本行为止的流水累计 + 本行数量」——
       必须链连续,否则会让**别的测试文件**的全库级断言挂掉。
    """
    import uuid
    conn = _conn()
    try:
        ts = created_at or datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        day = occurred_at or ts[:10]
        if balance_after is None:
            row = conn.execute(
                'SELECT COALESCE(SUM(quantity), 0) s FROM stock_ledger '
                'WHERE warehouse_id = ? AND product_id = ?',
                (int(warehouse_id), int(pid))).fetchone()
            balance_after = float(row['s'] or 0) + float(qty)
        conn.execute(
            'INSERT INTO stock_ledger (warehouse_id, biz_type, uniq_key, order_pk, record_id, '
            'product_id, quantity, unit, raw_quantity, raw_unit, factor, is_reversal, '
            'posting_id, operator_id, memo, occurred_at, balance_after, created_at) '
            "VALUES (?, ?, ?, 0, 0, ?, ?, '码', ?, '码', 1, 0, NULL, ?, ?, ?, ?, ?)",
            (int(warehouse_id), biz_type,
             uniq_key or f's6test:{biz_type}:{pid}:{uuid.uuid4().hex[:10]}',
             int(pid), float(qty), float(qty), operator_id, memo, day,
             float(balance_after), ts))
        conn.commit()
    finally:
        conn.close()


def _seed_stock(pid, qty, is_tracking=1, hours_ago=1):
    """造一个有**流水背书**的库存行(流水 + product_stock 成对写)。"""
    when = (datetime.datetime.now()
            - datetime.timedelta(hours=hours_ago)).strftime('%Y-%m-%d %H:%M:%S')
    _insert_ledger(pid, qty, biz_type='opening', created_at=when, memo='阶段6测试期初')
    conn = _conn()
    try:
        conn.execute(
            'INSERT INTO product_stock (warehouse_id, product_id, quantity, is_tracking, updated_at) '
            "VALUES (?, ?, ?, ?, datetime('now','localtime')) "
            'ON CONFLICT(warehouse_id, product_id) DO UPDATE SET '
            'quantity = excluded.quantity, is_tracking = excluded.is_tracking',
            (1, int(pid), float(qty), int(is_tracking)))
        conn.commit()
    finally:
        conn.close()


def _stock_qty(pid, warehouse_id=1):
    conn = _conn()
    try:
        row = conn.execute('SELECT quantity FROM product_stock WHERE warehouse_id = ? '
                           'AND product_id = ?', (int(warehouse_id), int(pid))).fetchone()
        return float(row['quantity']) if row else 0.0
    finally:
        conn.close()


def _count_row(count_id):
    conn = _conn()
    try:
        row = conn.execute('SELECT * FROM stock_counts WHERE id = ?',
                           (int(count_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _item_row(count_id, pid):
    conn = _conn()
    try:
        row = conn.execute(
            'SELECT * FROM stock_count_items WHERE count_id = ? AND product_id = ?',
            (int(count_id), int(pid))).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _adjust_rows(count_id):
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM stock_ledger WHERE biz_type = 'adjust' AND order_pk = ? ORDER BY id",
            (int(count_id),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _balance_consistent():
    """A1:product_stock.quantity 必须能由 stock_ledger 完全重算。"""
    conn = _conn()
    try:
        rows = conn.execute(
            'SELECT ps.warehouse_id, ps.product_id, ps.quantity, '
            'COALESCE((SELECT SUM(sl.quantity) FROM stock_ledger sl '
            '  WHERE sl.warehouse_id = ps.warehouse_id AND sl.product_id = ps.product_id), 0) AS s '
            'FROM product_stock ps').fetchall()
        return [dict(r) for r in rows if abs(float(r['quantity']) - float(r['s'])) > TOL]
    finally:
        conn.close()


@pytest.fixture
def operator(client):
    """造一个文员并给 client 挂上登录态(改账类 API 需要 current_operator)。"""
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO staff (name, role, is_active, created_at) "
            "VALUES (?, ?, 1, datetime('now','localtime'))",
            ('阶段6测试员', '文员'))
        sid = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    with client.session_transaction() as sess:
        sess['operator_id'] = sid
    return sid


def _create_count(client, title='阶段6盘点单', product_ids=None, **kwargs):
    """建盘点单(走**阶段 5 的同一套** API)。返回 (count_id, resp_json)。"""
    body = {'title': title, 'warehouse_id': 1, 'scope': 'selected',
            'product_ids': list(product_ids or [])}
    body.update(kwargs)
    r = client.post('/api/v1/stock/counts', json=body)
    assert r.status_code == 201, r.get_data(as_text=True)
    data = r.get_json()
    assert data['success'] is True
    return data['count_id'], data


# ── 判据 3 + 6.1 / 6.5:页面免登录 + 列表 ──────────────────────────
def test_criterion3_mobile_pages_are_public(client):
    """判据 3:`/m/*` 在免登录白名单里 —— 不登录也能打开盘点页。"""
    r = client.get('/m/stock-count')
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_data(as_text=True)
    assert '盘点' in body
    # 详情页同样免登录(用一个一定不存在的 id → 404 也说明路由通了)
    r2 = client.get('/m/stock-count/999999999')
    assert r2.status_code == 404


def test_criterion3_public_prefix_registered():
    """白名单里必须真的包含 `/m/`(防止以后有人改窄了)。"""
    src = open(os.path.join(ROOT, 'app.py'), encoding='utf-8').read()
    assert '"/m/"' in src


def test_task61_list_filters_by_status(client, operator):
    """6.1:列表页按状态过滤(进行中 / 已提交)。"""
    pid = _make_product('阶段6过滤品', 'A规格')
    cid, _ = _create_count(client, title='阶段6-进行中的单', product_ids=[pid])
    client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 5})
    client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'inventory'})

    r = client.get('/m/stock-count?status=closed')
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert '阶段6-进行中的单' in body

    r2 = client.get('/m/stock-count?status=open')
    body2 = r2.get_data(as_text=True)
    assert '阶段6-进行中的单' not in body2


def test_task65_reuses_mobile_css(client):
    """6.5:复用 `static/css/mobile.css`(不另起炉灶写一套样式)。"""
    r = client.get('/m/stock-count')
    body = r.get_data(as_text=True)
    assert 'css/mobile.css' in body


def test_mobile_index_has_count_entry(client):
    """移动端首页要有盘点入口。"""
    r = client.get('/m/')
    body = r.get_data(as_text=True)
    assert '/m/stock-count' in body
    assert '盘点' in body


# ── 判据 1 + 6.2:详情页按分类分组 ─────────────────────────────────
def test_task62_detail_groups_by_category(client, operator):
    """6.2:详情页按**分类**分组(两行不同分类 → 两个分组)。"""
    conn = _conn()
    try:
        cats = conn.execute(
            'SELECT id FROM product_categories WHERE parent_id IS NOT NULL '
            'ORDER BY level, sort_order LIMIT 2').fetchall()
    finally:
        conn.close()
    assert len(cats) >= 2, '测试库需要至少 2 个二级分类'
    cat_a, cat_b = int(cats[0]['id']), int(cats[1]['id'])

    pid_a = _make_product('阶段6分组品A', '规格A', category_id=cat_a)
    pid_b = _make_product('阶段6分组品B', '规格B', category_id=cat_b)
    cid, _ = _create_count(client, title='阶段6-分组单', product_ids=[pid_a, pid_b])

    r = client.get(f'/m/stock-count/{cid}')
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert '阶段6分组品A' in body and '阶段6分组品B' in body
    # 两个分类 → 两个分组标题
    assert body.count('class="m-cgroup"') == 2


def test_task62_detail_shows_book_and_pending(client, operator):
    """详情页要能看到账面数 + 「待录」标记。"""
    pid = _make_product('阶段6待录品', 'B规格')
    _seed_stock(pid, 12)
    cid, _ = _create_count(client, title='阶段6-待录单', product_ids=[pid])
    r = client.get(f'/m/stock-count/{cid}')
    body = r.get_data(as_text=True)
    assert '账面 12.0' in body
    assert '待录' in body


def test_task62_detail_404(client):
    r = client.get('/m/stock-count/999999999')
    assert r.status_code == 404


# ── 判据 1 + 6.3:手机录数 → PC 刷新能看到(同一张单)─────────────────
def test_criterion1_mobile_records_and_pc_sees_same_count(client, operator):
    """判据 1:手机录的数,PC 刷新就能看到(**同一张单**)。"""
    pid = _make_product('阶段6同单品', 'C规格')
    _seed_stock(pid, 10)
    cid, _ = _create_count(client, title='阶段6-同单', product_ids=[pid])

    # 手机走的是**同一套**阶段 5 API(URL 完全一致)
    r = client.post(f'/api/v1/stock/counts/{cid}/items/{pid}',
                    json={'quantity': 7.5})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['success'] is True

    # PC 端详情页(同一张单)能看到 7.5
    pc = client.get(f'/stock-count/{cid}')
    assert pc.status_code == 200
    assert '7.5' in pc.get_data(as_text=True)

    # 库里的那一行也确实是 7.5
    row = _item_row(cid, pid)
    assert abs(float(row['actual_quantity']) - 7.5) < TOL


def test_task63_quick_buttons_present(client, operator):
    """6.3:页面要有大按钮「+1 / +5 / +10 / 手动输入」+ 失焦即存。"""
    pid = _make_product('阶段6快按钮品', 'D规格')
    cid, _ = _create_count(client, title='阶段6-快按钮', product_ids=[pid])
    body = client.get(f'/m/stock-count/{cid}').get_data(as_text=True)
    assert '+1' in body and '+5' in body and '+10' in body
    assert 'onblur="onQtyBlur(this)"' in body
    assert 'bump(this,1)' in body


def test_task63_set_item_api_is_shared(client, operator):
    """6.3:录数端点就是阶段 5 的那个(**同一套 API**,不是新写一条)。"""
    pid = _make_product('阶段6共用端点品', 'E规格')
    cid, _ = _create_count(client, title='阶段6-共用端点', product_ids=[pid])
    r = client.post(f'/api/v1/stock/counts/{cid}/items/{pid}',
                    json={'quantity': 3})
    d = r.get_json()
    assert d['success'] is True
    assert 'item' in d and 'stats' in d
    assert abs(float(d['item']['actual_quantity']) - 3.0) < TOL


def test_mobile_detail_has_no_separate_write_path():
    """⭐ 移动端**不许**自己写一条提交路径(否则 A5 会有两个实现)。"""
    src = open(os.path.join(ROOT, 'blueprints', 'stock_count_mobile.py'),
               encoding='utf-8').read()
    assert '/submit' not in src
    assert "'/stock/counts/<int:count_id>/items/<int:product_id>'" not in src
    # 只允许这两个辅助端点
    assert src.count("@bp.route") == 4  # 2 页面 + identity + ocr-prefill


# ── 判据 2:提交路径完全一致 ──────────────────────────────────────
def test_criterion2_submit_uses_same_api_and_writes_ledger(client, operator):
    """判据 2:手机提交走同一套 API → 写 adjust 流水、余额 = 实盘数、关单。"""
    pid = _make_product('阶段6提交品', 'F规格')
    _seed_stock(pid, 20)
    cid, _ = _create_count(client, title='阶段6-提交单', product_ids=[pid])

    client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 18})

    # 预览 → 提交(和 PC 完全一样的两步)
    pv = client.post(f'/api/v1/stock/counts/{cid}/submit/preview',
                     json={'mode': 'inventory'})
    assert pv.status_code == 200
    pvj = pv.get_json()
    assert pvj['success'] is True
    assert pvj['summary']['counted'] == 1

    sb = client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'inventory'})
    assert sb.status_code == 200, sb.get_data(as_text=True)
    sj = sb.get_json()
    assert sj['success'] is True
    assert sj['adjusted'] == 1

    # 关单
    assert _count_row(cid)['status'] == 'closed'
    # 余额 = 实盘数
    assert abs(_stock_qty(pid) - 18.0) < TOL
    # adjust 流水在
    rows = _adjust_rows(cid)
    assert len(rows) == 1
    assert '实际' in (rows[0]['memo'] or '')
    # A1 仍然成立
    assert _balance_consistent() == []


def test_criterion2_repeat_submit_is_409(client, operator):
    """判据 2:重复提交 → 409(提交路径一致 → 幂等防线也一致)。"""
    pid = _make_product('阶段6重复提交品', 'G规格')
    _seed_stock(pid, 5)
    cid, _ = _create_count(client, title='阶段6-重复提交', product_ids=[pid])
    client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 4})
    assert client.post(f'/api/v1/stock/counts/{cid}/submit',
                       json={'mode': 'inventory'}).status_code == 200
    r = client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'inventory'})
    assert r.status_code == 409


# ── 6.6:并发写同单 ────────────────────────────────────────────────
def test_task66_conflict_detected_but_last_write_wins(client, operator):
    """6.6:别人改过这一行 → 提示冲突,但**仍然写入**(最后写入生效)。"""
    pid = _make_product('阶段6并发品', 'H规格')
    _seed_stock(pid, 8)
    cid, _ = _create_count(client, title='阶段6-并发单', product_ids=[pid])

    # 第一次录入
    r1 = client.post(f'/api/v1/stock/counts/{cid}/items/{pid}',
                     json={'quantity': 6, 'client_counted_at': ''})
    assert r1.status_code == 200
    assert not r1.get_json().get('conflict')

    # 模拟「客户端看到的旧值」→ 服务端发现已被别人改过
    r2 = client.post(f'/api/v1/stock/counts/{cid}/items/{pid}',
                     json={'quantity': 9, 'client_counted_at': '2000-01-01 00:00:00'})
    assert r2.status_code == 200
    d = r2.get_json()
    assert d['success'] is True
    assert d.get('conflict') is True
    assert d.get('conflict_at')
    # 最后写入生效:库里是 9
    assert abs(float(_item_row(cid, pid)['actual_quantity']) - 9.0) < TOL


def test_task66_no_conflict_when_token_matches(client, operator):
    """6.6:客户端看到的值与服务端一致 → 不报冲突。"""
    pid = _make_product('阶段6无冲突品', 'I规格')
    _seed_stock(pid, 8)
    cid, _ = _create_count(client, title='阶段6-无冲突', product_ids=[pid])
    r1 = client.post(f'/api/v1/stock/counts/{cid}/items/{pid}',
                     json={'quantity': 6, 'client_counted_at': ''})
    at = r1.get_json()['item']['counted_at']
    r2 = client.post(f'/api/v1/stock/counts/{cid}/items/{pid}',
                     json={'quantity': 7, 'client_counted_at': at})
    assert r2.get_json().get('conflict') is not True


def test_task66_legacy_client_without_token_has_no_conflict(client, operator):
    """PC 端(不传 token)→ 不做冲突检测,响应里也不该有 conflict 字段。"""
    pid = _make_product('阶段6老客户端品', 'J规格')
    cid, _ = _create_count(client, title='阶段6-老客户端', product_ids=[pid])
    r = client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 2})
    assert 'conflict' not in r.get_json()


# ── 6.4:拍照辅助(只预填,不过账)────────────────────────────────────
def test_task64_candidate_extraction():
    """6.4:从 OCR 文本里抽出数量候选(带单位的优先,规格数字要排除)。"""
    from blueprints.stock_count_mobile import _extract_count_candidates
    cands = _extract_count_candidates('白磅布 A1.5米 200g\n数量: 120支\n合计 350')
    vals = [c['value'] for c in cands]
    assert 120.0 in vals
    assert 350.0 in vals
    # 规格里的 200g / 1.5米 不该被当成数量
    assert 200.0 not in vals
    assert 1.5 not in vals


def test_task64_candidate_extraction_empty():
    from blueprints.stock_count_mobile import _extract_count_candidates
    assert _extract_count_candidates('') == []
    assert _extract_count_candidates('没有任何数字') == []


def test_task64_prefill_requires_image(client):
    """6.4:端点必须校验图片(没传文件 → 400)。"""
    r = client.post('/m/stock-count/ocr-prefill', data={})
    assert r.status_code == 400
    assert r.get_json()['success'] is False


def test_task64_prefill_rejects_non_image(client):
    """6.4:伪装文件(改后缀的 txt)→ 拒绝。"""
    import io as _io
    r = client.post('/m/stock-count/ocr-prefill', data={
        'file': (_io.BytesIO(b'not an image'), 'x.png')},
        content_type='multipart/form-data')
    assert r.status_code == 400


def test_task64_prefill_does_not_post(client, operator):
    """6.4:OCR 端点**只预填,不动库存**(不写任何 ledger / stock)。"""
    pid = _make_product('阶段6不自动过账品', 'K规格')
    _seed_stock(pid, 11)
    before = _stock_qty(pid)
    cid, _ = _create_count(client, title='阶段6-只预填', product_ids=[pid])
    # 即便 OCR 端点被调过(这里直接调一个非法图片让它早退),库存也不能变
    client.post('/m/stock-count/ocr-prefill', data={})
    assert abs(_stock_qty(pid) - before) < TOL


# ── 身份选择(A7:改账要留痕)────────────────────────────────────────
def test_identity_endpoint_sets_session(client):
    """选身份 → 写 session.operator_id(之后写操作自动带操作员)。"""
    conn = _conn()
    try:
        conn.execute("INSERT INTO staff (name, role, is_active, created_at) "
                     "VALUES ('阶段6手机员', '仓管', 1, datetime('now','localtime'))")
        sid = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
        conn.commit()
    finally:
        conn.close()

    r = client.post('/m/stock-count/identity', json={'staff_id': sid})
    assert r.status_code == 200
    d = r.get_json()
    assert d['success'] is True
    assert d['operator']['id'] == sid
    with client.session_transaction() as sess:
        assert sess['operator_id'] == sid


def test_identity_endpoint_rejects_inactive(client):
    conn = _conn()
    try:
        conn.execute("INSERT INTO staff (name, role, is_active, created_at) "
                     "VALUES ('阶段6停用员', '仓管', 0, datetime('now','localtime'))")
        sid = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    r = client.post('/m/stock-count/identity', json={'staff_id': sid})
    assert r.status_code == 400


def test_identity_endpoint_requires_staff_id(client):
    r = client.post('/m/stock-count/identity', json={})
    assert r.status_code == 400


# ── 静态扫描:别把写路径搬走 / 别用原生 confirm ─────────────────────
def test_mobile_templates_use_stage5_api_urls():
    """⭐ 静态扫描:移动端模板调的是**阶段 5 的 API 路径**(判据 2 的硬保证)。"""
    for name in ('stock-count.html', 'stock-count-detail.html'):
        src = open(os.path.join(ROOT, 'templates', 'mobile', name),
                   encoding='utf-8').read()
        assert '/api/v1/stock/counts' in src, name
        assert '/api/v1/shipping' not in src


def test_mobile_templates_avoid_native_confirm():
    """不用原生 `confirm()`(阶段 5 踩过的坑:会卡住渲染进程)。"""
    for name in ('stock-count.html', 'stock-count-detail.html'):
        src = open(os.path.join(ROOT, 'templates', 'mobile', name),
                   encoding='utf-8').read()
        assert not re.search(r'\bconfirm\s*\(', src), name


def test_mobile_templates_use_sheet_not_native_dialog():
    """提交确认走页内二次确认(两层:预览 → 再确认)。"""
    src = open(os.path.join(ROOT, 'templates', 'mobile', 'stock-count-detail.html'),
               encoding='utf-8').read()
    assert 'doPreview()' in src
    assert 'doSubmit()' in src
    assert 'submit/preview' in src
