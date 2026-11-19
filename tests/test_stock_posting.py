# -*- coding: utf-8 -*-
"""阶段 3 · 入库过账(后端)的测试。

覆盖开发计划 §阶段 3 的 **11 条完成判据**(逐条对应,别删):

  1. 建单 → 加明细 → 锁定 → 过账 → 流水写入、内部累计增加、对外显示 0(未启用)
  2. 重复点过账 → 库存不变(幂等)
  3. 部分可过账的单 → 跳过行都有原因
  4. 冲销 → 余额回到过账前,流水里正负两条都在,过账记录不消失
  5. 补完 product 数据后再点过账 → 只补入跳过行,已入账的不重复
  6. y → 码;令/块/卷 → 1:1;支 → 走 aux_unit
  7. 并发双击过账 → 不产生双份流水
  8. ⭐ A2:事务中途人为抛异常 → 4 张表全回滚
  9. ⭐ A1:随机 N 笔 入/冲销/调整 后重算余额断言一致(容差 1e-6)
 10. ⭐ A4/A6/A7:occurred_at 取订单日期;已过账单解锁 403;改账后 audit_log.user_id 非 NULL
 11. ⭐ A3:过账代码全库无 INSERT OR REPLACE/IGNORE / ON CONFLICT 写 stock_ledger;
            触发器只做守卫

⚠️ 测试跑在 worklog_test.db(conftest 从生产库复制一份),不碰生产数据。
⚠️ 排查失败必须**串行**跑 pytest —— 并发跑会抢同一个 worklog_test.db。
"""
import datetime
import os
import re
import sqlite3
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TOL = 1e-6


# ── 工具 ──────────────────────────────────────────────────────────
def _conn():
    """拿一个测试库连接(conftest 已把 models._db 重载到 worklog_test.db)。"""
    from models import get_db
    return get_db()


def _make_product(name, spec='', base_unit='码', aux_unit=None):
    """建一个 product 行,返回 product_id。

    ⚠️ 这里**故意关掉外键检查**再插:
       生产库的 `product` 表带一条悬空外键
       (`weight_category_id → product_weight_categories(id)`),
       而 `product_weight_categories` 这张表**在整个代码库里根本不存在**
       (当前 models/_init.py 的 DDL 也没有这条 FK)——
       于是 `PRAGMA foreign_keys=ON` 下 INSERT INTO product 直接报
       "no such table: main.product_weight_categories"。

       这是**存量库与当前 DDL 漂移**的既有缺陷(阶段 1.6 遗留,同类于"全新库缺列",
       方向相反:这次是老库多了一条指向空表的 FK),
       修它的脚本见 tools/fix_product_dangling_fk.py(默认演练,--apply 才落库)。
       测试只在自己的**副本库**里造数据,关掉 FK 检查不影响生产。
    """
    from models import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys = OFF')
        conn.execute(
            'INSERT INTO product (product_code, product_name, category_id, specification, '
            'base_unit, aux_unit, status, created_at, updated_at) '
            "VALUES (?, ?, 1, ?, ?, ?, 1, datetime('now','localtime'), "
            "datetime('now','localtime'))",
            (f'T3{abs(hash((name, spec))) % 10 ** 9}', name, spec, base_unit, aux_unit))
        pid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit()
        return int(pid)
    finally:
        conn.close()


def _make_order(date_str, supplier, lines, locked=False, warehouse_id=1):
    """建入库单 + 明细行。lines = [(品名, 规格, 数量, 单位), ...]"""
    conn = _conn()
    try:
        conn.execute(
            'INSERT INTO inbound_orders (date, supplier, order_num, created_at, is_locked, '
            'warehouse_id) VALUES (?, ?, 1, datetime(\'now\',\'localtime\'), ?, ?)',
            (date_str, supplier, 1 if locked else 0, warehouse_id))
        oid = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
        rids = []
        for i, (name, spec, qty, unit) in enumerate(lines, start=1):
            conn.execute(
                'INSERT INTO inbound_records (order_pk, product_name, specification, '
                'quantity, unit, remark, created_at, sort_order) '
                "VALUES (?, ?, ?, ?, ?, '', datetime('now','localtime'), ?)",
                (oid, name, spec, str(qty), unit, i))
            rids.append(int(conn.execute('SELECT last_insert_rowid()').fetchone()[0]))
        conn.commit()
        return oid, rids
    finally:
        conn.close()


def _lock(order_id, locked=1):
    conn = _conn()
    try:
        conn.execute('UPDATE inbound_orders SET is_locked = ? WHERE id = ?',
                     (locked, int(order_id)))
        conn.commit()
    finally:
        conn.close()


def _ledger(order_id):
    conn = _conn()
    try:
        rows = conn.execute(
            'SELECT * FROM stock_ledger WHERE biz_type = ? AND order_pk = ? ORDER BY id ASC',
            ('inbound', int(order_id))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _postings(order_id):
    conn = _conn()
    try:
        rows = conn.execute(
            'SELECT * FROM stock_postings WHERE biz_type = ? AND order_pk = ? ORDER BY id ASC',
            ('inbound', int(order_id))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _skips(order_id):
    conn = _conn()
    try:
        rows = conn.execute(
            'SELECT * FROM stock_posting_skips WHERE order_pk = ? ORDER BY id ASC',
            (int(order_id),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _stock_qty(product_id, warehouse_id=1):
    """product_stock 缓存里的内部累计。"""
    conn = _conn()
    try:
        row = conn.execute(
            'SELECT quantity FROM product_stock WHERE warehouse_id = ? AND product_id = ?',
            (warehouse_id, int(product_id))).fetchone()
        return float(row['quantity']) if row else 0.0
    finally:
        conn.close()


def _is_tracking(product_id, warehouse_id=1):
    conn = _conn()
    try:
        row = conn.execute(
            'SELECT is_tracking FROM product_stock WHERE warehouse_id = ? AND product_id = ?',
            (warehouse_id, int(product_id))).fetchone()
        return int(row['is_tracking']) if row else 0
    finally:
        conn.close()


def _display_row(product_id, warehouse_id=1):
    """走库存页那套查询,拿「对外显示值」(未启用 → 0)。"""
    from models.inventory import ProductStock
    rows = ProductStock.list_stock_view(warehouse_id, limit=2000)
    for r in rows:
        if int(r['product_id']) == int(product_id):
            return r
    return None


def _audit_rows(action, entity_id):
    conn = _conn()
    try:
        rows = conn.execute(
            'SELECT * FROM audit_log WHERE action = ? AND entity_type = ? AND entity_id = ? '
            'ORDER BY id ASC',
            (action, 'inbound_order', int(entity_id))).fetchall()
        return [dict(r) for r in rows]
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
            ('阶段3测试员', '文员'))
        sid = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    with client.session_transaction() as sess:
        sess['operator_id'] = sid
    return sid


# ── 判据 1 · 过账写入流水 + 内部累计 + 对外显示 0 ─────────────────
def test_criterion1_post_writes_ledger_and_stock(client, operator):
    pid = _make_product('判据1测试布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '判据1供应商',
                         [('判据1测试布', 'A1', 10, 'y')])
    _lock(oid)

    r = client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data['success'] is True
    assert data['posted_count'] == 1
    assert data['posted_qty'] == 10

    # 流水写了
    led = _ledger(oid)
    assert len(led) == 1
    assert led[0]['quantity'] == 10
    assert led[0]['unit'] == '码'
    assert led[0]['uniq_key'] == f'inbound:{led[0]["record_id"]}'
    assert led[0]['is_reversal'] == 0
    # A4:occurred_at = 订单日期; A1:balance_after = 本笔后余额
    assert led[0]['occurred_at'] == '2026-11-10'
    assert led[0]['balance_after'] == 10

    # 内部累计加了
    assert _stock_qty(pid) == 10
    # 但对外显示仍是 0(未启用)
    row = _display_row(pid)
    assert row is not None
    assert row['quantity'] == 10
    assert row['is_tracking'] == 0
    assert row['display_quantity'] == 0


# ── 判据 2 · 幂等(重复点过账 → 库存不变)──────────────────────────
def test_criterion2_repost_is_idempotent(client, operator):
    pid = _make_product('判据2测试布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '判据2供应商',
                         [('判据2测试布', 'A1', 10, 'y')])
    _lock(oid)

    r1 = client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert r1.status_code == 200
    assert r1.get_json()['posted_count'] == 1

    r2 = client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert r2.status_code == 200, r2.get_data(as_text=True)
    d2 = r2.get_json()
    # 幂等命中:**不报错**,而是提示「本单已过账」
    assert d2['success'] is True
    assert d2['already_posted'] is True
    assert d2['posted_count'] == 0
    assert d2['already_posted_count'] == 1

    # 库存不变 + 流水不重复
    assert _stock_qty(pid) == 10
    assert len(_ledger(oid)) == 1


# ── 判据 3 · 部分过账 → 跳过行都有原因 ───────────────────────────
def test_criterion3_partial_post_lists_reasons(client, operator):
    _make_product('判据3能过账布', 'A1', base_unit='码')
    _make_product('判据3缺换算布', 'A1', base_unit='码')
    _make_product('判据3基本单位非法', 'A1', base_unit='元')
    oid, _ = _make_order('2026-11-10', '判据3供应商', [
        ('判据3能过账布', 'A1', 10, 'y'),          # OK
        ('判据3不存在的商品XYZ', 'B1', 5, 'y'),      # no_product
        ('判据3缺换算布', 'A1', 5, '令'),            # no_conversion(码←令 无规则)
        ('判据3基本单位非法', 'A1', 5, 'y'),         # bad_base_unit
        ('判据3能过账布', 'A1', 0, 'y'),            # bad_qty
    ])
    _lock(oid)

    r = client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data['posted_count'] == 1
    assert data['skipped_count'] == 4

    codes = {s['reason_code'] for s in data['skipped']}
    assert codes == {'no_product', 'no_conversion', 'bad_base_unit', 'bad_qty'}
    # 每条都有原因文案
    for s in data['skipped']:
        assert s['reason_text']
        assert s['resolvable'] is True     # 这 4 类修好后都能补过账

    # 跳过清单也落库了(待处理)
    skips = _skips(oid)
    assert len(skips) == 4
    assert {s['reason_code'] for s in skips} == codes
    assert all(s['resolved'] == 0 for s in skips)


# ── 判据 4 · 红字冲销 ────────────────────────────────────────────
def test_criterion4_reverse_restores_balance(client, operator):
    pid = _make_product('判据4测试布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '判据4供应商',
                         [('判据4测试布', 'A1', 10, 'y')])
    _lock(oid)
    client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert _stock_qty(pid) == 10

    r = client.post(f'/api/v1/inbound-orders/{oid}/reverse')
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data['reversed_count'] == 1
    assert data['reversed_qty'] == -10

    # 余额回到过账前
    assert _stock_qty(pid) == 0
    # 流水里正负两条都在,原记录没被改
    led = _ledger(oid)
    assert len(led) == 2
    assert led[0]['quantity'] == 10
    assert led[0]['is_reversal'] == 0
    assert led[1]['quantity'] == -10
    assert led[1]['is_reversal'] == 1
    assert led[1]['uniq_key'] == led[0]['uniq_key'] + ':rev'
    assert led[1]['balance_after'] == 0
    # 过账记录不消失
    postings = _postings(oid)
    assert len(postings) == 1
    assert postings[0]['status'] == 'reversed'
    assert postings[0]['reversed_at']

    # 不能重复冲销
    r2 = client.post(f'/api/v1/inbound-orders/{oid}/reverse')
    assert r2.status_code == 409


# ── 判据 5 · 补过账(只补跳过行,已入账的不重复)───────────────────
def test_criterion5_repost_only_fills_skipped_rows(client, operator):
    pid_ok = _make_product('判据5已存在布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '判据5供应商', [
        ('判据5已存在布', 'A1', 10, 'y'),
        ('判据5后补布', 'B1', 5, 'y'),
    ])
    _lock(oid)

    r1 = client.post(f'/api/v1/inbound-orders/{oid}/post')
    d1 = r1.get_json()
    assert d1['posted_count'] == 1
    assert d1['skipped_count'] == 1
    assert d1['skipped'][0]['reason_code'] == 'no_product'

    # 补完 product 数据 → 再点过账
    pid_new = _make_product('判据5后补布', 'B1', base_unit='码')
    r2 = client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert r2.status_code == 200, r2.get_data(as_text=True)
    d2 = r2.get_json()
    # 只补了 1 行(跳过行),已入账的那行没重复
    assert d2['posted_count'] == 1
    assert d2['skipped_count'] == 0
    assert d2['already_posted_count'] == 1

    assert _stock_qty(pid_ok) == 10
    assert _stock_qty(pid_new) == 5
    assert len(_ledger(oid)) == 2

    # 老的那条跳过记录被标成「已解决」,不再留在待处理清单里
    # (第 2 次过账时该行已入账 → 不再新增跳过记录,老的那条标 resolved=1)
    skips = _skips(oid)
    assert len(skips) == 1
    assert skips[0]['resolved'] == 1
    assert sum(1 for s in skips if s['resolved'] == 0) == 0


# ── 判据 6 · 单位折算 ────────────────────────────────────────────
def test_criterion6_unit_conversion(client, operator):
    pid_y = _make_product('判据6-y单位布', 'A', base_unit='码')
    pid_ling = _make_product('判据6-令单位布', 'A', base_unit='令')
    pid_kuai = _make_product('判据6-块单位布', 'A', base_unit='块')
    pid_juan = _make_product('判据6-卷单位布', 'A', base_unit='卷')
    pid_zhi = _make_product('判据6-支单位布', 'A', base_unit='码', aux_unit='1支=46码')
    oid, _ = _make_order('2026-11-10', '判据6供应商', [
        ('判据6-y单位布', 'A', 10, 'y'),
        ('判据6-令单位布', 'A', 5, '令'),
        ('判据6-块单位布', 'A', 3, '块'),
        ('判据6-卷单位布', 'A', 7, '卷'),
        ('判据6-支单位布', 'A', 2, '支'),
    ])
    _lock(oid)

    r = client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data['posted_count'] == 5, data['skipped']
    assert data['skipped_count'] == 0

    assert _stock_qty(pid_y) == 10          # y → 码,1:1
    assert _stock_qty(pid_ling) == 5        # 令 → 令,1:1
    assert _stock_qty(pid_kuai) == 3        # 块 → 块,1:1
    assert _stock_qty(pid_juan) == 7        # 卷 → 卷,1:1
    assert _stock_qty(pid_zhi) == 92        # 支 → 码,aux_unit '1支=46码' → 2×46

    # 流水里记着折算系数与原始单位
    led = {l['record_id']: l for l in _ledger(oid)}
    zhi = [l for l in led.values() if l['raw_unit'] == '支'][0]
    assert zhi['factor'] == 46
    assert zhi['raw_quantity'] == 2
    assert zhi['quantity'] == 92
    assert zhi['unit'] == '码'


# ── 判据 7 · 并发双击过账 ────────────────────────────────────────
def test_criterion7_concurrent_post_no_double_ledger():
    """两个线程同时过账同一张单 → 只入账一次。"""
    from blueprints.stock_posting import post_inbound_order

    pid = _make_product('判据7并发布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '判据7供应商',
                         [('判据7并发布', 'A1', 10, 'y')])
    _lock(oid)

    results, errors = [], []
    barrier = threading.Barrier(2)

    def run():
        try:
            barrier.wait(timeout=10)
            results.append(post_inbound_order(oid, operator_id=1, operator_name='并发'))
        except Exception as e:      # noqa: BLE001 —— 测试里要看到所有异常
            errors.append(repr(e))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    # 两个线程都返回了结果(一个真入账,一个幂等跳过)
    assert len(results) == 2
    posted = sum(r['posted_count'] for r in results)
    assert posted == 1
    # 关键:流水只有一条,余额只有一份
    assert len(_ledger(oid)) == 1
    assert _stock_qty(pid) == 10


def test_criterion7_duplicate_uniq_key_raises_not_silently_replaces():
    """A3:重复 uniq_key 必须**报错**,不能静默把数量改掉。

    ⚠️ 这里手工插的那条流水必须**与 A1 不变量自洽**
       (balance_after 非空 + product_stock 缓存同步)——
       否则它会污染后面 criterion9 的「余额可由流水完全重算」断言。
       流水表是只追加的(禁 DELETE 触发器),插进去就擦不掉,只能一开始就写对。
    """
    pid = _make_product('判据7唯一键布', 'A1', base_unit='码')
    oid, rids = _make_order('2026-11-10', '判据7供应商',
                            [('判据7唯一键布', 'A1', 10, 'y')])
    _lock(oid)
    conn = _conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            'INSERT INTO stock_ledger (warehouse_id, biz_type, uniq_key, order_pk, '
            'record_id, product_id, quantity, unit, raw_quantity, raw_unit, factor, '
            'is_reversal, memo, occurred_at, balance_after, created_at) '
            "VALUES (1, 'inbound', ?, ?, ?, ?, 10, '码', 10, 'y', 1, 0, '', "
            "'2026-11-10', 10.0, datetime('now','localtime'))",
            (f'inbound:{rids[0]}', oid, rids[0], pid))
        conn.execute(
            'INSERT INTO product_stock (warehouse_id, product_id, quantity, is_tracking, '
            "updated_at) VALUES (1, ?, 10.0, 0, datetime('now','localtime'))", (pid,))
        conn.execute('COMMIT')
    finally:
        conn.close()

    # 再插同键 → 必须 IntegrityError(触发器 / UNIQUE 拦下)
    conn = _conn()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO stock_ledger (warehouse_id, biz_type, uniq_key, order_pk, '
                'record_id, product_id, quantity, unit, raw_quantity, raw_unit, factor, '
                'is_reversal, memo, occurred_at, balance_after, created_at) '
                "VALUES (1, 'inbound', ?, ?, ?, ?, 999, '码', 10, 'y', 1, 0, '', "
                "'2026-11-10', 999.0, datetime('now','localtime'))",
                (f'inbound:{rids[0]}', oid, rids[0], pid))
    finally:
        conn.close()

    # 原行的数量没被改成 999
    led = _ledger(oid)
    assert len(led) == 1
    assert led[0]['quantity'] == 10


# ── 判据 8 · ⭐A2 事务回滚 ───────────────────────────────────────
def test_criterion8_transaction_rolls_back_all_four_tables(client, operator, monkeypatch):
    """过账中途人为抛异常 → stock_postings / stock_ledger / product_stock /
    stock_posting_skips **四表全回滚**,不留孤儿流水。"""
    import blueprints.stock_posting as sp

    pid = _make_product('判据8回滚布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '判据8供应商',
                         [('判据8回滚布', 'A1', 10, 'y')])
    _lock(oid)

    real_stock = sp.ProductStock

    class _BoomStock:
        """替身:其它方法正常转发,add_quantity 抛异常(模拟写到一半失败)。"""

        def __getattr__(self, name):
            return getattr(real_stock, name)

        def add_quantity(self, *a, **kw):
            raise RuntimeError('人为注入的异常:模拟过账中途失败')

    monkeypatch.setattr(sp, 'ProductStock', _BoomStock())

    with pytest.raises(RuntimeError):
        sp.post_inbound_order(oid, operator_id=1, operator_name='测试')

    # 四表全回滚
    assert _ledger(oid) == []                 # stock_ledger 无孤儿
    assert _postings(oid) == []               # stock_postings 无批次
    assert _skips(oid) == []                  # stock_posting_skips 无记录
    assert _stock_qty(pid) == 0.0             # product_stock 缓存没动


# ── 判据 9 · ⭐A1 余额不变量 ─────────────────────────────────────
def _write_adjust(warehouse_id, product_id, qty, occurred_at, seq):
    """模拟盘点调整(阶段 5 的写入口,这里先手写一条 adjust 流水做不变量验证)。"""
    from models.inventory import ProductStock, StockLedger
    conn = _conn()
    conn.isolation_level = None
    try:
        conn.execute('BEGIN IMMEDIATE')
        cur = ProductStock.get(warehouse_id, product_id, conn=conn)
        base = float(cur['quantity']) if cur else 0.0
        ProductStock.ensure(warehouse_id, product_id, conn=conn)
        balance = base + qty
        StockLedger.create(warehouse_id, 'adjust', f'adjust:{seq}', product_id, qty,
                           '码', occurred_at=occurred_at, balance_after=balance,
                           conn=conn)
        ProductStock.add_quantity(warehouse_id, product_id, qty, conn=conn)
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()


def _assert_a1_consistent():
    """A1:① 缓存 == 流水重算 ② balance_after 逐笔链连续 ③ occurred_at 非空。"""
    conn = _conn()
    try:
        # ① 缓存 vs 流水(逐 (仓, SKU))
        for r in conn.execute('''
                SELECT ps.warehouse_id, ps.product_id, ps.quantity AS cache_qty,
                       COALESCE((SELECT SUM(l.quantity) FROM stock_ledger l
                                 WHERE l.warehouse_id = ps.warehouse_id
                                   AND l.product_id = ps.product_id), 0) AS ledger_qty
                FROM product_stock ps''').fetchall():
            assert abs(float(r['cache_qty']) - float(r['ledger_qty'])) < TOL, dict(r)
        # ② balance_after 链 + ③ occurred_at 非空
        prev = {}
        for r in conn.execute('''SELECT warehouse_id, product_id, quantity,
                                        balance_after, occurred_at
                                 FROM stock_ledger ORDER BY warehouse_id, product_id, id''').fetchall():
            assert r['occurred_at'], f"occurred_at 为空: {dict(r)}"
            ba = r['balance_after']
            assert ba is not None, f"balance_after 为空: {dict(r)}"
            key = (r['warehouse_id'], r['product_id'])
            if key in prev:
                assert abs(prev[key] + float(r['quantity']) - float(ba)) < TOL, dict(r)
            else:
                # 该 (仓,SKU) 的第一笔:余额从 0 起
                assert abs(float(r['quantity']) - float(ba)) < TOL, dict(r)
            prev[key] = float(ba)
    finally:
        conn.close()


def test_criterion9_random_operations_keep_balance_consistent():
    """随机 N 笔 入 / 冲销 / 调整 后,余额必须仍能由流水完全重算。"""
    import random
    from blueprints.stock_posting import post_inbound_order, reverse_inbound_order

    rnd = random.Random(20261117)
    pids = []
    for i in range(5):
        pids.append(_make_product(f'判据9随机布{i}', 'A1', base_unit='码'))

    order_ids = []
    for i in range(6):
        lines = []
        for _ in range(rnd.randint(1, 4)):
            lines.append((f'判据9随机布{rnd.randint(0, 4)}', 'A1',
                          rnd.randint(1, 50), 'y'))
        oid, _ = _make_order('2026-11-10', f'判据9供应商{i}', lines)
        _lock(oid)
        order_ids.append(oid)
        post_inbound_order(oid, operator_id=1, operator_name='随机')

    # 冲销其中 3 单
    for oid in order_ids[:3]:
        reverse_inbound_order(oid, operator_id=1, operator_name='随机')

    # 调整 10 笔
    for i in range(10):
        _write_adjust(1, pids[rnd.randint(0, 4)], rnd.uniform(-20, 20),
                      '2026-11-11', 900000 + i)

    _assert_a1_consistent()


# ── 判据 10 · ⭐A4 / A6 / A7 ─────────────────────────────────────
def test_criterion10_a4_occurred_at_is_order_date(client, operator):
    """补录上周的单 → occurred_at 是上周日期,不是今天。"""
    last_week = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    pid = _make_product('判据10补录布', 'A1', base_unit='码')
    oid, _ = _make_order(last_week, '判据10供应商',
                         [('判据10补录布', 'A1', 10, 'y')])
    _lock(oid)

    client.post(f'/api/v1/inbound-orders/{oid}/post')
    led = _ledger(oid)
    assert led[0]['occurred_at'] == last_week
    assert led[0]['occurred_at'] != datetime.date.today().isoformat()


def test_criterion10_a6_unlock_after_post_returns_403(client, operator):
    """已过账的订单解锁 → 403,要求先红字冲销。"""
    _make_product('判据10解锁布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '判据10供应商',
                         [('判据10解锁布', 'A1', 10, 'y')])
    _lock(oid)
    client.post(f'/api/v1/inbound-orders/{oid}/post')

    r = client.patch(f'/api/v1/inbound-orders/{oid}', json={'is_locked': False})
    assert r.status_code == 403, r.get_data(as_text=True)
    body = r.get_json()
    assert body['success'] is False
    assert '冲销' in body['error']

    # 冲销之后就能解锁了
    rr = client.post(f'/api/v1/inbound-orders/{oid}/reverse')
    assert rr.status_code == 200
    r2 = client.patch(f'/api/v1/inbound-orders/{oid}', json={'is_locked': False})
    assert r2.status_code == 200


def test_a6_guard_survives_idempotent_repost(client, operator):
    """⭐ 回归:重复点过账(幂等空转)**不能**留下挡住解锁的「僵尸批次」。

    2026-11-17 浏览器实测踩到的 bug:
        「重复点过账」曾留下一条 posted_count=0 的空批次,status 仍是 'posted' →
        A6 守卫若拿**批次状态**当判据,就会在「冲销之后」仍然 403 解不了锁。
    → 现在纯幂等空转**不建批次**,且守卫判据改成「还有没被冲销的**流水**」。
    """
    _make_product('判据A6空转布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '判据A6供应商',
                         [('判据A6空转布', 'A1', 10, 'y')])
    _lock(oid)

    client.post(f'/api/v1/inbound-orders/{oid}/post')
    # 重复点两次(模拟用户手抖 / 网络重试)
    client.post(f'/api/v1/inbound-orders/{oid}/post')
    client.post(f'/api/v1/inbound-orders/{oid}/post')

    # 纯幂等空转不应新增批次
    assert len(_postings(oid)) == 1

    # 已过账 → 解锁被拒
    r = client.patch(f'/api/v1/inbound-orders/{oid}', json={'is_locked': False})
    assert r.status_code == 403

    # 冲销之后 → 必须能解锁(这条就是原来挂住的地方)
    rr = client.post(f'/api/v1/inbound-orders/{oid}/reverse')
    assert rr.status_code == 200, rr.get_data(as_text=True)
    r2 = client.patch(f'/api/v1/inbound-orders/{oid}', json={'is_locked': False})
    assert r2.status_code == 200, r2.get_data(as_text=True)


def test_criterion10_a7_audit_user_id_not_null(client, operator):
    """改账后 audit_log.user_id 非 NULL(A7:操作员自动注入)。"""
    _make_product('判据10审计布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '判据10供应商',
                         [('判据10审计布', 'A1', 10, 'y')])
    _lock(oid)
    client.post(f'/api/v1/inbound-orders/{oid}/post')
    client.post(f'/api/v1/inbound-orders/{oid}/reverse')

    posts = _audit_rows('stock_post', oid)
    assert posts, '过账没有留下审计记录'
    assert posts[-1]['user_id'] == operator
    assert posts[-1]['user_id'] is not None

    revs = _audit_rows('stock_reverse', oid)
    assert revs, '冲销没有留下审计记录'
    assert revs[-1]['user_id'] == operator


def test_criterion10_a7_post_requires_login(client):
    """未登录不许改账(改账类 API 至少校验 current_operator 存在)。"""
    pid = _make_product('判据10未登录布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '判据10供应商',
                         [('判据10未登录布', 'A1', 10, 'y')])
    _lock(oid)
    r = client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert r.status_code == 401
    assert _stock_qty(pid) == 0.0
    assert _ledger(oid) == []


# ── 判据 11 · ⭐A3 幂等写入契约 ──────────────────────────────────
def test_criterion11_no_upsert_into_stock_ledger():
    """全库扫描:写 stock_ledger 的地方不许用 OR REPLACE / OR IGNORE / ON CONFLICT。

    ⚠️ 扫描范围**排除 tests/**:
       `tests/test_inventory_models.py` 里故意写了 `INSERT OR REPLACE INTO stock_ledger`,
       那是在**验证触发器能不能拦住它**(A3 第 3 条触发器的存在意义),
       不是生产代码里的幂等写法。判据盯的是「过账代码」。
    """
    pattern = re.compile(r'INSERT\s+(?:OR\s+\w+\s+)?INTO\s+stock_ledger', re.I)
    offenders = []
    skip_dirs = {'.git', '__pycache__', 'upload', 'log', '.claude', 'node_modules', 'tests'}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if not fn.endswith('.py'):
                continue
            path = os.path.join(dirpath, fn)
            try:
                src = open(path, encoding='utf-8', errors='ignore').read()
            except OSError:
                continue
            for m in pattern.finditer(src):
                seg = src[m.start():m.start() + 400].split('VALUES')[0]
                if re.search(r'OR\s+(REPLACE|IGNORE)', seg, re.I) or \
                        'ON CONFLICT' in seg.upper():
                    offenders.append((path, seg[:120]))
    assert not offenders, offenders


def test_criterion11_ledger_triggers_are_guard_only():
    """A3:3 条守卫触发器存在,且体内只有 SELECT + RAISE(不承载业务计算)。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='stock_ledger'"
        ).fetchall()
        assert len(rows) == 3, [r['name'] for r in rows]
        names = {r['name'] for r in rows}
        assert names == {'trg_stock_ledger_no_update', 'trg_stock_ledger_no_delete',
                         'trg_stock_ledger_no_reuse'}
        for r in rows:
            sql = r['sql'] or ''
            assert 'RAISE' in sql.upper() and 'ABORT' in sql.upper(), r['name']
            # 触发器体(BEGIN 之后)不得出现写表的关键字
            body = sql.upper().split('BEGIN', 1)[-1]
            for kw in ('INSERT INTO', 'UPDATE ', 'DELETE FROM', 'REPLACE INTO'):
                assert kw not in body, f'{r["name"]} 体内出现 {kw}'
    finally:
        conn.close()


# ── 附:闸门与状态查询 ────────────────────────────────────────────
def test_post_requires_locked_order(client, operator):
    """闸门①:没锁定不许过账 → 409。"""
    _make_product('闸门未锁布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '闸门供应商',
                         [('闸门未锁布', 'A1', 10, 'y')])
    r = client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert r.status_code == 409
    assert '锁定' in r.get_json()['error']


def test_post_rejects_unknown_warehouse(client, operator):
    """闸门②:仓库不存在 → 400(FK 只当兜底,应用层给中文提示)。"""
    _make_product('闸门坏仓布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '闸门供应商',
                         [('闸门坏仓布', 'A1', 10, 'y')], warehouse_id=9999)
    _lock(oid)
    r = client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert r.status_code == 400
    assert '仓库' in r.get_json()['error']


def test_post_missing_order_returns_404(client, operator):
    r = client.post('/api/v1/inbound-orders/99999999/post')
    assert r.status_code == 404


def test_stock_status_reports_counts(client, operator):
    """GET stock-status:过账状态 + 跳过清单 + 未启用提示。"""
    _make_product('状态能过账布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '状态供应商', [
        ('状态能过账布', 'A1', 10, 'y'),
        ('状态不存在的商品XYZ', 'B1', 5, 'y'),
    ])
    _lock(oid)

    s0 = client.get(f'/api/v1/inbound-orders/{oid}/stock-status').get_json()
    assert s0['counts']['total'] == 2
    assert s0['counts']['posted'] == 0
    assert s0['can_post'] is True
    assert s0['can_reverse'] is False

    client.post(f'/api/v1/inbound-orders/{oid}/post')

    s1 = client.get(f'/api/v1/inbound-orders/{oid}/stock-status').get_json()
    assert s1['counts']['posted'] == 1
    assert s1['counts']['skipped'] == 1
    assert s1['has_posting'] is True
    assert s1['can_reverse'] is True
    assert len(s1['skipped']) == 1
    assert s1['skipped'][0]['reason_code'] == 'no_product'
    # 未启用提示清单(已入账但 is_tracking=0)
    assert len(s1['not_tracking']) == 1
    assert s1['not_tracking'][0]['product_name'] == '状态能过账布'
    # 每行状态
    states = {i['record_id']: i['state'] for i in s1['items']}
    assert sorted(states.values()) == ['posted', 'skipped']


def test_posted_order_cannot_be_reposted_after_reverse(client, operator):
    """冲销过的行不能再正向过账(正向 uniq_key 仍被原流水占着)。"""
    _make_product('冲销后重过账布', 'A1', base_unit='码')
    oid, _ = _make_order('2026-11-10', '冲销供应商',
                         [('冲销后重过账布', 'A1', 10, 'y')])
    _lock(oid)
    client.post(f'/api/v1/inbound-orders/{oid}/post')
    client.post(f'/api/v1/inbound-orders/{oid}/reverse')

    r = client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert r.status_code == 200
    d = r.get_json()
    assert d['posted_count'] == 0
    assert d['order_reversed'] is True
    # 余额没被加回去
    led = _ledger(oid)
    assert len(led) == 2


# ── 附:init_db 事务边界回归(阶段 3 引入 warehouses 种子 INSERT 后暴露)────
def test_init_db_closes_connection_when_it_raises(monkeypatch):
    """init_db 中途抛异常 → 连接必须被 close,写锁必须还回去。

    回归背景:阶段 3 在 init_db() 里加了 `INSERT OR IGNORE INTO warehouses`,
    这是**稳定库上 init_db() 的第一条 DML** → Python sqlite3 会给它开隐式写事务。
    而 init_db 原本把 `conn.close()` 写在函数最后一行(不在 try/finally 里)
    → 一旦中途抛异常,连接连同未提交的事务一起泄漏
    → SQLite 写锁被永久持有 → 之后所有写库都报 "database is locked"。
    """
    import models._init as init_mod

    real = init_mod.get_db()

    class _BoomConn:
        """任何一条 execute 都炸,用来模拟 init_db 中途失败。"""

        def __init__(self, inner):
            self._inner = inner

        def cursor(self):
            return self

        def execute(self, *a, **kw):
            raise RuntimeError('人为注入的异常:模拟 init_db 中途失败')

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            self._inner.close()

    proxy = _BoomConn(real)
    monkeypatch.setattr(init_mod, 'get_db', lambda: proxy)

    with pytest.raises(RuntimeError):
        init_mod.init_db()

    # 连接必须已关闭(sqlite3 对已关闭连接抛 ProgrammingError)
    with pytest.raises(sqlite3.ProgrammingError):
        real.execute('SELECT 1')


def test_init_db_does_not_hold_write_lock_after_return():
    """init_db() 返回后,别的连接必须能**立刻**拿到写锁。

    回归背景:init_db() 里那条种子 INSERT 会开隐式写事务,
    如果不提前 commit,剩下几百行 DDL 全跑在这个写事务里
    → 整个启动过程都占着 SQLite 写锁
    → 并发写库时 busy_timeout 等满 30s 后报 "database is locked"。
    """
    from models._init import init_db
    from models._db import DB_PATH

    init_db()

    conn = sqlite3.connect(DB_PATH, timeout=2)
    try:
        conn.execute('PRAGMA busy_timeout = 2000')
        conn.execute('BEGIN IMMEDIATE')    # 立刻拿到 → 说明没被 init_db 占着
        conn.execute('ROLLBACK')
    finally:
        conn.close()
