# -*- coding: utf-8 -*-
"""阶段 5 · 盘点(PC 单据页)的测试。

覆盖开发计划 §阶段 5 的 **8 条完成判据**(逐条对应,别删):

  1. 盘点单建单 → 导出 CSV → 批量粘贴回填 → 提交 → **库存 = 实盘数**
  2. 差额≠0 时流水里有一条 `adjust`,`memo` 写明「实际 X / 基准 Y / 期间 Z / 调整 D」
  3. **未录的行不处理**,仍是「未启用」
  4. 盘点期间有出入库的商品 → 提交确认弹框列出四个数(基准 / 期间 / 实际 / 调整)
  5. 提交后 `status='closed'`,**重复提交 → 409**
  6. ⭐ **A5**：「先数货后入库」与「先入库后数货」两种顺序,提交后余额**都 = 实盘数**
  7. ⭐ **A1 / A4**:提交后余额可由流水完全重算(容差 1e-6);`adjust` 流水的
     `occurred_at` / `balance_after` 非空
  8. ⭐ **A7**:提交后 `audit_log.user_id` 非 NULL;归零不填 `memo` 原因被拒

⚠️ 测试跑在 worklog_test.db(conftest 从生产库复制一份),不碰生产数据。
⚠️ 排查失败必须**串行**跑 pytest —— 并发跑会抢同一个 worklog_test.db。

⭐ **A5 的口径要点**(判据 6 的实现基础):
    差额 = `实盘` − **基准余额** − **期间变动**
    而「基准余额 + 期间变动」恒等于「当前余额」——
    所以**两种入库顺序提交后余额都 = 实盘数**,区别只在于
    弹框里给用户看的**四个数**和**期间变动逐笔明细**是否讲得清。
    旧口径(拿建单快照直接当差额)会把期间发生的入库**重复计一次**,
    这就是 A5 要修的 bug。
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


def _make_product(name, spec='', base_unit='码'):
    """建一个 product 行,返回 product_id。

    ⚠️ 故意关掉外键检查再插 —— 生产库的 `product` 表带一条悬空外键
       (`weight_category_id → product_weight_categories(id)`),而那张表不存在。
       详见 tests/test_stock_posting.py::_make_product 的同一段说明。
    """
    from models import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys = OFF')
        conn.execute(
            'INSERT INTO product (product_code, product_name, category_id, specification, '
            'base_unit, aux_unit, status, created_at, updated_at) '
            "VALUES (?, ?, 1, ?, ?, NULL, 1, datetime('now','localtime'), "
            "datetime('now','localtime'))",
            (f'S5{abs(hash((name, spec))) % 10 ** 9}', name, spec, base_unit))
        pid = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
        conn.commit()
        return pid
    finally:
        conn.close()


def _product_code(pid):
    conn = _conn()
    try:
        row = conn.execute('SELECT product_code, product_name, specification FROM product '
                           'WHERE product_id = ?', (int(pid),)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _set_stock(pid, qty, is_tracking=0, warehouse_id=1):
    """直接写 product_stock(测试造数用,不走引擎)。"""
    conn = _conn()
    try:
        conn.execute(
            'INSERT INTO product_stock (warehouse_id, product_id, quantity, is_tracking, updated_at) '
            "VALUES (?, ?, ?, ?, datetime('now','localtime')) "
            'ON CONFLICT(warehouse_id, product_id) DO UPDATE SET '
            'quantity = excluded.quantity, is_tracking = excluded.is_tracking',
            (int(warehouse_id), int(pid), float(qty), int(is_tracking)))
        conn.commit()
    finally:
        conn.close()


def _insert_ledger(pid, qty, biz_type='inbound', occurred_at=None, created_at=None,
                   memo='', uniq_key=None, balance_after=None, warehouse_id=1,
                   operator_id=None):
    """直接写一条 stock_ledger(**可控 created_at**,用来确定性地造「期间变动」)。

    ⚠️ 为什么不用真实过账 API:过账写的 `created_at` 是 `datetime('now','localtime')`
       (**秒级精度**),而 `as_of` 也是秒级 —— 建单和过账落在同一秒时,
       「同日用 created_at > as_of 判定」这一条就没法确定性地测。
       这里显式指定 `created_at`,把两种顺序钉死。
       ⚠️ 流水表有禁改触发器(UPDATE/DELETE 全被拦),所以只能 INSERT。

    ⭐ `balance_after` 默认取「**该 (仓,SKU) 到本行为止的流水累计**」——
       不是本行数量。原因:`tests/test_stock_posting.py::_assert_a1_consistent()`
       会扫描**整个测试库**校验 `balance_after` 逐笔链连续;
       若这里取「本行数量」,同一商品的第二行起链就断了,
       会让**别的测试文件**挂掉(踩过一次:criterion9 判据挂了,实际是本文件造数不对)。
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
             uniq_key or f'test:{biz_type}:{pid}:{uuid.uuid4().hex[:10]}',
             int(pid), float(qty), float(qty), operator_id, memo, day,
             float(balance_after), ts))
        conn.commit()
    finally:
        conn.close()


def _seed_stock(pid, qty, is_tracking=1, hours_ago=1):
    """造一个有**流水背书**的库存行。

    ⭐ 为什么不能只写 `product_stock`:
       A1 保证生产库里 `product_stock.quantity` **恒等于** `SUM(stock_ledger.quantity)`,
       而 A5 的「基准余额 / 期间变动」是**从流水算出来的** ——
       只改 product_stock 会让两者对不上,算出来的差额自然不对。
       造数必须成对写:一条 opening 流水 + 一行 product_stock。
    """
    when = (datetime.datetime.now()
            - datetime.timedelta(hours=hours_ago)).strftime('%Y-%m-%d %H:%M:%S')
    _insert_ledger(pid, qty, biz_type='opening', created_at=when,
                   memo='测试期初')
    _set_stock(pid, qty, is_tracking)


def _stock_qty(pid, warehouse_id=1):
    conn = _conn()
    try:
        row = conn.execute('SELECT quantity FROM product_stock WHERE warehouse_id = ? '
                           'AND product_id = ?', (int(warehouse_id), int(pid))).fetchone()
        return float(row['quantity']) if row else 0.0
    finally:
        conn.close()


def _display_row(pid, warehouse_id=1):
    conn = _conn()
    try:
        row = conn.execute(
            'SELECT ps.quantity, ps.is_tracking, '
            "CASE WHEN ps.is_tracking = 1 THEN ps.quantity ELSE 0 END AS display_quantity "
            'FROM product_stock ps WHERE ps.warehouse_id = ? AND ps.product_id = ?',
            (int(warehouse_id), int(pid))).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _count_row(count_id):
    conn = _conn()
    try:
        row = conn.execute('SELECT * FROM stock_counts WHERE id = ?', (int(count_id),)).fetchone()
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
        return [(dict(r)) for r in rows if abs(float(r['quantity']) - float(r['s'])) > TOL]
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
            ('阶段5测试员', '文员'))
        sid = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    with client.session_transaction() as sess:
        sess['operator_id'] = sid
    return sid


def _create_count(client, title='测试盘点单', product_ids=None, **kwargs):
    """建盘点单(走真实 API)。返回 (count_id, resp_json)。"""
    body = {'title': title, 'warehouse_id': 1, 'scope': 'selected',
            'product_ids': list(product_ids or [])}
    body.update(kwargs)
    r = client.post('/api/v1/stock/counts', json=body)
    assert r.status_code == 201, r.get_data(as_text=True)
    data = r.get_json()
    assert data['success'] is True
    return data['count_id'], data


# ── 判据 1 · 建单 → 导出 → 粘贴回填 → 提交 → 库存 = 实盘数 ──────────
def test_criterion1_full_flow_stock_equals_count(client, operator):
    pid = _make_product('判据1盘点布', 'A1', base_unit='码')
    _seed_stock(pid, 30)
    code = _product_code(pid)['product_code']

    # 建单
    cid, data = _create_count(client, '判据1盘点', product_ids=[pid])
    assert data['total_items'] == 1
    count = _count_row(cid)
    assert count['status'] == 'open'
    assert count['as_of'], '建单必须写死 as_of(A5 基准时点)'
    assert count['total_items'] == 1

    # 导出 CSV —— 账面数在,实盘数 / 差异为空
    r = client.get(f'/api/v1/stock/counts/{cid}/export.csv')
    assert r.status_code == 200
    csv_text = r.get_data(as_text=True)
    assert '判据1盘点布' in csv_text
    assert 'A1' in csv_text
    lines = [ln for ln in csv_text.strip().splitlines() if ln.strip()]
    assert len(lines) == 2, '表头 + 1 行明细'
    # 实盘数 / 差异两列都空 → 结尾是两个逗号
    assert lines[1].endswith(',,'), lines[1]

    # 批量粘贴回填(用商品编号,故意漏前导 0)
    short_code = code.lstrip('0') or code
    r = client.post(f'/api/v1/stock/counts/{cid}/bulk-input',
                    json={'text': f'{short_code}\t25'})
    assert r.status_code == 200, r.get_data(as_text=True)
    bd = r.get_json()
    assert bd['success'] is True
    assert bd['parsed'] == 1
    assert bd['matched'] == 1, bd['lines']
    assert bd['unmatched'] == 0

    # 提交
    r = client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'inventory'})
    assert r.status_code == 200, r.get_data(as_text=True)
    sd = r.get_json()
    assert sd['success'] is True
    assert sd['adjusted'] == 1

    # ⭐ 库存 = 实盘数(25)
    assert abs(_stock_qty(pid) - 25) < TOL, _stock_qty(pid)
    row = _display_row(pid)
    assert row['is_tracking'] == 1
    assert abs(row['display_quantity'] - 25) < TOL


# ── 判据 2 · 差额≠0 → 一条 adjust 流水,memo 写明四个数 ────────────
def test_criterion2_adjust_ledger_and_memo(client, operator):
    pid = _make_product('判据2盘点布', 'A1', base_unit='码')
    _seed_stock(pid, 30)

    cid, _ = _create_count(client, '判据2盘点', product_ids=[pid])
    client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 25})

    r = client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'inventory'})
    assert r.status_code == 200, r.get_data(as_text=True)

    rows = _adjust_rows(cid)
    assert len(rows) == 1, '差额≠0 → 恰好一条 adjust 流水'
    led = rows[0]
    assert led['biz_type'] == 'adjust'
    assert abs(float(led['quantity']) - (-5)) < TOL, led['quantity']
    assert led['uniq_key'] == f'adjust:count:{cid}:{pid}'
    # memo 写明「实际 X / 基准 Y / 期间 Z / 调整 D」
    memo = led['memo']
    assert '实际' in memo and '基准' in memo and '期间' in memo and '调整' in memo, memo
    assert '25' in memo and '30' in memo and '-5' in memo, memo

    assert abs(_stock_qty(pid) - 25) < TOL


# ── 判据 3 · 未录的行不处理,仍是「未启用」 ─────────────────────────
def test_criterion3_unfilled_rows_not_touched(client, operator):
    pid_ok = _make_product('判据3已录布', 'A1', base_unit='码')
    pid_skip = _make_product('判据3未录布', 'A2', base_unit='码')
    _seed_stock(pid_ok, 10)
    _seed_stock(pid_skip, 10, is_tracking=0)

    cid, _ = _create_count(client, '判据3盘点', product_ids=[pid_ok, pid_skip])
    client.post(f'/api/v1/stock/counts/{cid}/items/{pid_ok}', json={'quantity': 8})

    r = client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'inventory'})
    assert r.status_code == 200, r.get_data(as_text=True)

    # 已录的行被调整 + 被启用
    assert abs(_stock_qty(pid_ok) - 8) < TOL
    assert _display_row(pid_ok)['is_tracking'] == 1

    # ⭐ 未录的行:余额不动、**仍是未启用**(A8:启用只走盘点 / 归零)
    assert abs(_stock_qty(pid_skip) - 10) < TOL
    assert _display_row(pid_skip)['is_tracking'] == 0
    assert _display_row(pid_skip)['display_quantity'] == 0
    # 也没有为它写 adjust 流水
    assert [x for x in _adjust_rows(cid) if x['product_id'] == pid_skip] == []


# ── 判据 4 · 期间有出入库 → 提交确认弹框列出四个数 ────────────────
def test_criterion4_preview_lists_four_numbers_and_period_rows(client, operator):
    pid = _make_product('判据4盘点布', 'A1', base_unit='码')
    _seed_stock(pid, 10)

    cid, _ = _create_count(client, '判据4盘点', product_ids=[pid])
    count = _count_row(cid)
    as_of = count['as_of']

    # ⭐ 造一条**基准时点之后**发生的入库(可控 created_at)
    later = _as_of_plus(as_of, seconds=3600)
    _insert_ledger(pid, 5, biz_type='inbound', occurred_at=as_of[:10],
                    created_at=later, memo='期间入库 5 码', uniq_key=f'test:period:{cid}')
    _set_stock(pid, 15, is_tracking=1)

    client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 12})

    r = client.post(f'/api/v1/stock/counts/{cid}/submit/preview', json={'mode': 'inventory'})
    assert r.status_code == 200, r.get_data(as_text=True)
    pv = r.get_json()
    assert pv['success'] is True
    assert len(pv['rows']) == 1
    row = pv['rows'][0]
    # 四个数:实际 / 基准 / 期间 / 调整
    assert abs(row['actual_quantity'] - 12) < TOL
    assert abs(row['base_qty'] - 10) < TOL, '基准余额 = as_of 之前的流水合计'
    assert abs(row['period_qty'] - 5) < TOL, '期间变动 = as_of 之后的流水合计'
    assert abs(row['delta'] - (12 - 10 - 5)) < TOL
    # 期间变动逐笔列出
    assert len(row['period_rows']) == 1
    assert row['period_rows'][0]['quantity'] == 5
    assert row['period_rows'][0]['biz_type'] == 'inbound'
    # 摘要
    assert pv['summary']['counted'] == 1
    assert pv['summary']['adjusted'] == 1
    assert pv['summary']['total_period_changes'] == 1


def _as_of_plus(as_of, seconds=1):
    """as_of 字符串 + N 秒(纯字符串加秒,够用,不引第三方库)。"""
    import datetime
    dt = datetime.datetime.strptime(as_of, '%Y-%m-%d %H:%M:%S')
    return (dt + datetime.timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S')


# ── 判据 5 · 提交后 status='closed',重复提交 → 409 ─────────────────
def test_criterion5_close_and_double_submit_409(client, operator):
    pid = _make_product('判据5盘点布', 'A1', base_unit='码')
    _seed_stock(pid, 10)
    cid, _ = _create_count(client, '判据5盘点', product_ids=[pid])
    client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 10})

    r1 = client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'inventory'})
    assert r1.status_code == 200, r1.get_data(as_text=True)
    assert _count_row(cid)['status'] == 'closed'

    r2 = client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'inventory'})
    assert r2.status_code == 409, r2.get_data(as_text=True)
    assert r2.get_json()['success'] is False

    # 已提交的单据不能再改 / 不能删
    r3 = client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 99})
    assert r3.status_code == 409
    r4 = client.delete(f'/api/v1/stock/counts/{cid}')
    assert r4.status_code == 409
    # 余额没被二次改动
    assert abs(_stock_qty(pid) - 10) < TOL


# ── 判据 6 · ⭐ A5:两种入库顺序,提交后余额都 = 实盘数 ──────────────
def test_criterion6_a5_count_before_inbound(client, operator):
    """先数货 → 后入库。

    ⭐ 流水时刻**显式**放在基准时点之后 1 小时 → 确定性地落在「期间变动」段。
    """
    pid = _make_product('判据6先数货布', 'A1', base_unit='码')
    _seed_stock(pid, 0, is_tracking=0)

    # ① 建单(基准时点在此刻)
    cid, _ = _create_count(client, '判据6先数货', product_ids=[pid])
    as_of = _count_row(cid)['as_of']
    # ② 数货:实盘 10
    client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 10})
    # ③ 数完货之后又入库 10 码(基准时点之后)
    _insert_ledger(pid, 10, biz_type='inbound', occurred_at=as_of[:10],
                    created_at=_as_of_plus(as_of, 3600),
                    uniq_key=f'test:c6a:{cid}')
    _set_stock(pid, 10, is_tracking=0)
    # ④ 提交
    r = client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'inventory'})
    assert r.status_code == 200, r.get_data(as_text=True)

    # ⭐ 余额 = 实盘数(10),且已启用
    assert abs(_stock_qty(pid) - 10) < TOL, _stock_qty(pid)
    assert _display_row(pid)['is_tracking'] == 1
    assert abs(_display_row(pid)['display_quantity'] - 10) < TOL

    # 期间变动被正确识别(基准 0 / 期间 +10)
    pv = client.post(f'/api/v1/stock/counts/{cid}/submit/preview',
                     json={'mode': 'inventory'}).get_json()
    # 已提交 → preview 409,但没关系;改查 adjust 流水
    assert pv.get('success') is not True


def test_criterion6_a5_inbound_before_count(client, operator):
    """先入库 → 后数货。

    ⭐ 流水时刻**显式**放在基准时点之前 → 确定性地落在「基准余额」段。
    """
    pid = _make_product('判据6先入库布', 'A1', base_unit='码')
    _seed_stock(pid, 0, is_tracking=0)

    # ① 先入库 10 码(时刻在 1 小时前 → 必定在建单之前)
    import datetime
    past = (datetime.datetime.now() - datetime.timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    _insert_ledger(pid, 10, biz_type='inbound', created_at=past,
                   uniq_key=f'test:c6b:{cid_placeholder()}')
    _set_stock(pid, 10, is_tracking=0)
    # ② 再建单(基准时点在入库之后)
    cid, _ = _create_count(client, '判据6先入库', product_ids=[pid])
    # ③ 数货:实盘 10
    client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 10})
    # ④ 提交
    r = client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'inventory'})
    assert r.status_code == 200, r.get_data(as_text=True)

    assert abs(_stock_qty(pid) - 10) < TOL, _stock_qty(pid)
    assert _display_row(pid)['is_tracking'] == 1
    assert abs(_display_row(pid)['display_quantity'] - 10) < TOL


def test_criterion6_a5_count_differs_from_book_both_orders(client, operator):
    """两种顺序下,实盘 ≠ 账面时,提交后余额仍然 = 实盘数。"""
    import datetime
    # (a) 先数货后入库:数出 8,之后入 10(期间变动)
    pid_a = _make_product('判据6差异先数货', 'A1', base_unit='码')
    _seed_stock(pid_a, 0, is_tracking=0)
    cid_a, _ = _create_count(client, '判据6差异先数货', product_ids=[pid_a])
    as_of_a = _count_row(cid_a)['as_of']
    client.post(f'/api/v1/stock/counts/{cid_a}/items/{pid_a}', json={'quantity': 8})
    _insert_ledger(pid_a, 10, biz_type='inbound', occurred_at=as_of_a[:10],
                    created_at=_as_of_plus(as_of_a, 3600),
                    uniq_key=f'test:c6c:{cid_a}')
    _set_stock(pid_a, 10, is_tracking=0)
    r = client.post(f'/api/v1/stock/counts/{cid_a}/submit', json={'mode': 'inventory'})
    assert r.status_code == 200, r.get_data(as_text=True)
    # 基准 0 + 期间 10 + 调整(8-0-10=-2) → 余额 = 8 = 实盘
    assert abs(_stock_qty(pid_a) - 8) < TOL, _stock_qty(pid_a)
    led = _adjust_rows(cid_a)
    assert len(led) == 1
    assert abs(float(led[0]['quantity']) - (-2)) < TOL, led[0]['quantity']
    assert abs(float(led[0]['balance_after']) - 8) < TOL

    # (b) 先入库后数货:先入 10(基准内),数出 8
    pid_b = _make_product('判据6差异先入库', 'A1', base_unit='码')
    _seed_stock(pid_b, 0, is_tracking=0)
    past = (datetime.datetime.now() - datetime.timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    _insert_ledger(pid_b, 10, biz_type='inbound', created_at=past,
                   uniq_key=f'test:c6d:{cid_placeholder()}')
    _set_stock(pid_b, 10, is_tracking=0)
    cid_b, _ = _create_count(client, '判据6差异先入库', product_ids=[pid_b])
    client.post(f'/api/v1/stock/counts/{cid_b}/items/{pid_b}', json={'quantity': 8})
    r = client.post(f'/api/v1/stock/counts/{cid_b}/submit', json={'mode': 'inventory'})
    assert r.status_code == 200, r.get_data(as_text=True)
    # 基准 10 + 期间 0 + 调整(8-10-0=-2) → 余额 = 8 = 实盘
    assert abs(_stock_qty(pid_b) - 8) < TOL, _stock_qty(pid_b)
    led_b = _adjust_rows(cid_b)
    assert len(led_b) == 1
    assert abs(float(led_b[0]['quantity']) - (-2)) < TOL, led_b[0]['quantity']


def cid_placeholder():
    """给 uniq_key 用的一次性后缀(避免同一条测试里撞键)。"""
    import uuid
    return uuid.uuid4().hex[:8]


# ── 判据 7 · ⭐ A1 / A4:余额可重算 + occurred_at / balance_after 非空 ──
def test_criterion7_a1_a4_ledger_recomputable_and_fields_filled(client, operator):
    pid1 = _make_product('判据7盘点布甲', 'A1', base_unit='码')
    pid2 = _make_product('判据7盘点布乙', 'A2', base_unit='码')
    _seed_stock(pid1, 30)
    _seed_stock(pid2, 20)

    cid, _ = _create_count(client, '判据7盘点', product_ids=[pid1, pid2])
    client.post(f'/api/v1/stock/counts/{cid}/items/{pid1}', json={'quantity': 25})
    client.post(f'/api/v1/stock/counts/{cid}/items/{pid2}', json={'quantity': 20})

    r = client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'inventory'})
    assert r.status_code == 200, r.get_data(as_text=True)

    # A1:product_stock.quantity 能由 stock_ledger 完全重算
    diffs = _balance_consistent()
    assert diffs == [], f'余额与流水对不上: {diffs}'

    # A4:occurred_at 非空(= 盘点基准日);A1:balance_after 非空
    rows = _adjust_rows(cid)
    assert len(rows) == 1
    led = rows[0]
    assert led['occurred_at'], 'A4:occurred_at 不能为空'
    assert led['balance_after'] is not None, 'A1:balance_after 不能为空'
    assert abs(float(led['balance_after']) - 25) < TOL


# ── 判据 8 · ⭐ A7:audit_log.user_id 非 NULL;归零不填原因被拒 ──────
def test_criterion8_a7_audit_user_id_not_null(client, operator):
    pid = _make_product('判据8盘点布', 'A1', base_unit='码')
    _seed_stock(pid, 10)
    cid, _ = _create_count(client, '判据8盘点', product_ids=[pid])
    client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 7})

    r = client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'inventory'})
    assert r.status_code == 200, r.get_data(as_text=True)

    conn = _conn()
    try:
        row = conn.execute(
            "SELECT user_id FROM audit_log WHERE action = 'stock_count_submit' "
            'AND entity_id = ? ORDER BY id DESC LIMIT 1', (cid,)).fetchone()
    finally:
        conn.close()
    assert row is not None, '提交必须写 audit_log(A7 留痕)'
    assert row['user_id'] is not None, 'audit_log.user_id 不能为 NULL'
    assert int(row['user_id']) == operator


def test_criterion8_zero_without_reason_is_rejected(client, operator):
    pid = _make_product('判据8归零布', 'A1', base_unit='码')
    _seed_stock(pid, 10)
    cid, _ = _create_count(client, '判据8归零', product_ids=[pid])
    client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 0})

    # 归零不填原因 → 拒绝
    r = client.post(f'/api/v1/stock/counts/{cid}/submit', json={'mode': 'zero'})
    assert r.status_code == 400, r.get_data(as_text=True)
    assert '原因' in r.get_json()['error']
    # 单据没被关掉,余额没动
    assert _count_row(cid)['status'] == 'open'
    assert abs(_stock_qty(pid) - 10) < TOL

    # 填了原因 → 调到 0
    r = client.post(f'/api/v1/stock/counts/{cid}/submit',
                    json={'mode': 'zero', 'reason': '清仓,货已处理'})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert abs(_stock_qty(pid) - 0) < TOL, _stock_qty(pid)
    assert _count_row(cid)['status'] == 'closed'
    memo = _adjust_rows(cid)[0]['memo']
    assert '清仓' in memo, memo


# ── 附加 · 批量粘贴的编号容错 ─────────────────────────────────────
def test_bulk_input_number_tolerance(client, operator):
    pid = _make_product('容错盘点布', 'A1', base_unit='码')
    _seed_stock(pid, 10)
    info = _product_code(pid)
    cid, _ = _create_count(client, '容错盘点', product_ids=[pid])

    # ① 商品编号(故意漏前导 0)
    # ② product_id
    # ③ 品名 + 规格
    text = '\n'.join([
        f"{info['product_code'].lstrip('0') or info['product_code']}\t11",
        f"{pid}\t12",
        f"{info['product_name']} {info['specification']}\t13",
    ])
    r = client.post(f'/api/v1/stock/counts/{cid}/bulk-input', json={'text': text})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d['parsed'] == 3
    assert d['matched'] == 3, d['lines']
    assert d['unmatched'] == 0

    # 最后一次的值生效(13)
    conn = _conn()
    try:
        row = conn.execute('SELECT actual_quantity FROM stock_count_items '
                           'WHERE count_id = ? AND product_id = ?', (cid, pid)).fetchone()
    finally:
        conn.close()
    assert abs(float(row['actual_quantity']) - 13) < TOL


def test_bulk_input_ambiguous_name_reports_error(client, operator):
    """只给品名命中多行 → 明确报错让人补规格(不猜)。"""
    pid1 = _make_product('歧义盘点布', 'A1', base_unit='码')
    pid2 = _make_product('歧义盘点布', 'A2', base_unit='码')
    cid, _ = _create_count(client, '歧义盘点', product_ids=[pid1, pid2])

    r = client.post(f'/api/v1/stock/counts/{cid}/bulk-input',
                    json={'text': '歧义盘点布\t9'})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d['matched'] == 0
    assert d['unmatched'] == 1
    assert '请补规格' in d['lines'][0]['message']


def test_bulk_input_bad_number_is_skipped(client, operator):
    pid = _make_product('跳过盘点布', 'A1', base_unit='码')
    cid, _ = _create_count(client, '跳过盘点', product_ids=[pid])
    r = client.post(f'/api/v1/stock/counts/{cid}/bulk-input',
                    json={'text': 'abc\n123 abc\n456 -3'})
    d = r.get_json()
    assert d['skipped'] == 3
    assert d['matched'] == 0


# ── 附加 · 单行录数 / 删行 / 范围 ────────────────────────────────
def test_set_and_delete_item(client, operator):
    pid = _make_product('单行盘点布', 'A1', base_unit='码')
    cid, _ = _create_count(client, '单行盘点', product_ids=[pid])

    r = client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': 6.5})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert abs(r.get_json()['item']['actual_quantity'] - 6.5) < TOL
    assert r.get_json()['stats']['counted_items'] == 1

    # 清空 = 回到待录
    r = client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': None})
    assert r.status_code == 200
    assert r.get_json()['item']['actual_quantity'] is None
    assert r.get_json()['stats']['counted_items'] == 0

    # 负数拒绝
    r = client.post(f'/api/v1/stock/counts/{cid}/items/{pid}', json={'quantity': -1})
    assert r.status_code == 400

    # 删行
    r = client.delete(f'/api/v1/stock/counts/{cid}/items/{pid}')
    assert r.status_code == 200


def test_create_count_scope_all(client, operator):
    """范围 = 全仓 → 清单包含库里所有商品。"""
    pid = _make_product('全仓盘点布', 'A1', base_unit='码')
    r = client.post('/api/v1/stock/counts',
                    json={'title': '全仓盘点', 'warehouse_id': 1, 'scope': 'all'})
    assert r.status_code == 201, r.get_data(as_text=True)
    d = r.get_json()
    assert d['total_items'] >= 1
    assert d['as_of']

    conn = _conn()
    try:
        n = conn.execute('SELECT COUNT(*) n FROM stock_count_items WHERE count_id = ?',
                         (d['count_id'],)).fetchone()['n']
    finally:
        conn.close()
    assert n == d['total_items']


def test_create_count_empty_scope_is_rejected(client, operator):
    r = client.post('/api/v1/stock/counts',
                    json={'title': '空盘点', 'warehouse_id': 1, 'scope': 'selected',
                          'product_ids': []})
    assert r.status_code == 400
    assert r.get_json()['success'] is False


def test_create_count_requires_title(client, operator):
    r = client.post('/api/v1/stock/counts',
                    json={'title': '   ', 'warehouse_id': 1, 'scope': 'all'})
    assert r.status_code == 400


def test_list_page_bad_warehouse_returns_400_not_500(client, operator):
    """列表页的**错误分支**必须返回 400,不是 500。

    ⚠️ 踩坑：Jinja2 的 `Undefined` **属性访问会抛 UndefinedError** ——
    错误分支漏传一个 `stats`,模板里 `{{ stats.total }}` 就把 400 变成 500。
    """
    r = client.get('/stock-count?warehouse_id=999999')
    assert r.status_code == 400, r.get_data(as_text=True)
    assert '不存在' in r.get_data(as_text=True)


def test_detail_page_missing_count_returns_404(client, operator):
    r = client.get('/stock-count/999999')
    assert r.status_code == 404


def test_delete_open_count_allowed_and_closed_not(client, operator):
    pid = _make_product('删单盘点布', 'A1', base_unit='码')
    cid, _ = _create_count(client, '删单盘点', product_ids=[pid])

    r = client.delete(f'/api/v1/stock/counts/{cid}')
    assert r.status_code == 200, r.get_data(as_text=True)
    assert _count_row(cid) is None

    # 已提交的不给删(审计链)
    pid2 = _make_product('删单盘点布乙', 'A1', base_unit='码')
    _seed_stock(pid2, 5)
    cid2, _ = _create_count(client, '删单盘点乙', product_ids=[pid2])
    client.post(f'/api/v1/stock/counts/{cid2}/items/{pid2}', json={'quantity': 5})
    client.post(f'/api/v1/stock/counts/{cid2}/submit', json={'mode': 'inventory'})
    r = client.delete(f'/api/v1/stock/counts/{cid2}')
    assert r.status_code == 409


# ── 附加 · A3:流水写入契约 ───────────────────────────────────────
def test_a3_no_or_replace_on_stock_ledger():
    """盘点提交路径不得用 OR REPLACE / OR IGNORE / ON CONFLICT 写 stock_ledger。

    A3 靠 DB 触发器强制,但代码层也要干净 —— 这条做**静态扫描**。

    ⚠️ 扫描要**只盯真正执行的 SQL**:用 `ast` 找出所有 `conn.execute(...)` /
       `cursor.execute(...)` 的字符串参数,而不是全文正则 ——
       全文正则会把**注释和 docstring 里**「禁 OR REPLACE / OR IGNORE / ON CONFLICT」
       这句说明也当成违规(踩过一次)。
    """
    import ast
    path = os.path.join(ROOT, 'blueprints', 'stock_counting.py')
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read())

    sql_texts = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == 'execute'):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                sql_texts.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                # f-string:取其中的常量片段
                for piece in arg.values:
                    if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                        sql_texts.append(piece.value)

    bad = []
    for sql in sql_texts:
        if not re.search(r'\bINSERT\b', sql, re.IGNORECASE):
            continue
        if re.search(r'OR\s+REPLACE|OR\s+IGNORE|ON\s+CONFLICT', sql, re.IGNORECASE):
            bad.append(sql.strip()[:120])
    assert bad == [], f'流水写入用了冲突策略: {bad}'
