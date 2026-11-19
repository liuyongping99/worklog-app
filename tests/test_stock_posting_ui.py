# -*- coding: utf-8 -*-
"""阶段 4 · 入库过账(前端)的测试。

覆盖开发计划 §阶段 4 的 5 条任务 + 3 条完成判据:

  4.1 订单头「📥 过账」按钮 —— 锁定前 disabled
  4.2 「↩ 冲销」二次确认弹框 —— 文案写明冲销行数·数量 + 余额回到过账前
  4.3 「⚠ N 行未入账」徽章 —— 数字来自 /stock-status
  4.4 过账弹框 —— 仓库 / 汇总四格 / 跳过行明细表 / 未启用提示块
  4.5 幂等反馈 —— 重复点过账 → 提示「本单已过账」而不是报错

  判据① 建单 → 锁定 → 过账 → 弹窗汇总正确 → 库存页显示变化
  判据② 跳过行在**弹框和徽章**里都能看到原因
  判据③ 冲销有二次确认,且冲销后徽章状态正确

⚠️ 测试跑在 worklog_test.db(conftest 从生产库复制一份),不碰生产数据。
⚠️ 排查失败必须**串行**跑 pytest —— 并发跑会抢同一个 worklog_test.db。
"""
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
    """建 product 行(关外键 —— 生产库 product 表有指向空表的悬空 FK,见
    tools/fix_product_dangling_fk.py)。"""
    from models import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute('PRAGMA foreign_keys = OFF')
        conn.execute(
            'INSERT INTO product (product_code, product_name, category_id, specification, '
            'base_unit, status, created_at, updated_at) '
            "VALUES (?, ?, 1, ?, ?, 1, datetime('now','localtime'), "
            "datetime('now','localtime'))",
            (f'U4{abs(hash((name, spec))) % 10 ** 8}', name, spec, base_unit))
        pid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.commit()
        return int(pid)
    finally:
        conn.close()


def _make_order(client, supplier, lines):
    """建入库单 + 明细(明细一行一次调用,订单未锁定才能加)。"""
    r = client.post('/api/v1/inbound-orders',
                    json={'date': '2026-11-18', 'supplier': supplier})
    assert r.status_code == 201, r.get_data(as_text=True)[:200]
    oid = int(r.get_json()['id'])
    for pn, sp, qty in lines:
        rr = client.post(f'/api/v1/inbound-orders/{oid}/records', json={
            'product_name': pn, 'specification': sp, 'quantity': qty, 'unit': 'y',
        })
        assert rr.status_code == 201, rr.get_data(as_text=True)[:200]
    return oid


@pytest.fixture
def operator(client):
    """造一个文员并给 client 挂上登录态(改账类 API 需要 current_operator)。"""
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO staff (name, role, is_active, created_at) "
            "VALUES ('阶段4测试员', '文员', 1, datetime('now','localtime'))")
        conn.commit()
        sid = int(conn.execute('SELECT last_insert_rowid()').fetchone()[0])
    finally:
        conn.close()
    with client.session_transaction() as sess:
        sess['operator_id'] = sid
        sess['operator_name'] = '阶段4测试员'
    return sid


def _order_block(html, oid):
    """取某张单在页面 HTML 里的区块(用于断言徽章)。"""
    return html.split(f'data-order-id="{oid}"', 1)[-1][:6000]


# ── 4.1 / 4.3 · 页面渲染:按钮 + 徽章 ─────────────────────────────
def test_page_renders_post_button_and_badges(client, operator):
    """页面含「📥 过账」「↩ 冲销」按钮 + 徽章区 + 两个弹框。"""
    html = client.get('/inbound-records').get_data(as_text=True)
    assert '📥 过账' in html
    assert '↩ 冲销' in html
    assert 'stock-badge-area' in html
    assert 'id="stockPostModal"' in html
    assert 'id="stockReverseModal"' in html
    for fn in ('function openStockPostModal', 'function openStockReverseModal',
               'function confirmStockPost', 'function confirmStockReverse',
               'function refreshStockBadges'):
        assert fn in html, fn


def test_post_button_disabled_before_lock(client, operator):
    """锁定前「过账」按钮 disabled(按钮一直在,只是不可点)。"""
    oid = _make_order(client, '阶段4未锁定', [('阶段4未锁定布', 'A1', '5')])
    html = client.get('/inbound-records').get_data(as_text=True)
    block = _order_block(html, oid)
    assert 'stock-post-btn' in block
    m = re.search(r'<button[^>]*stock-post-btn[^>]*>', block)
    assert m, '找不到过账按钮'
    assert 'disabled' in m.group(0), '未锁定时过账按钮应当 disabled'


def test_badges_rendered_server_side(client, operator):
    """过账后服务端渲染「✓ 已过账」;有跳过行再渲染「⚠ N 行未入账」。"""
    _make_product('阶段4徽章布', 'A1')
    oid = _make_order(client, '阶段4徽章', [
        ('阶段4徽章布', 'A1', '6'),
        ('阶段4徽章查无此货', 'B9', '3'),
    ])
    client.patch(f'/api/v1/inbound-orders/{oid}', json={'is_locked': True})
    client.post(f'/api/v1/inbound-orders/{oid}/post')

    html = client.get('/inbound-records').get_data(as_text=True)
    block = _order_block(html, oid)
    assert '✓ 已过账' in block
    assert '⚠ 1 行未入账' in block


def test_reversed_order_shows_reversed_badge_not_posted(client, operator):
    """冲销后显示「↩ 已冲销」,且**不再**显示「✓ 已过账」(余额已回过账前)。"""
    _make_product('阶段4冲销徽章布', 'A1')
    oid = _make_order(client, '阶段4冲销徽章', [('阶段4冲销徽章布', 'A1', '8')])
    client.patch(f'/api/v1/inbound-orders/{oid}', json={'is_locked': True})
    client.post(f'/api/v1/inbound-orders/{oid}/post')
    client.post(f'/api/v1/inbound-orders/{oid}/reverse')

    html = client.get('/inbound-records').get_data(as_text=True)
    block = _order_block(html, oid)
    assert '↩ 已冲销' in block
    assert '✓ 已过账' not in block


# ── 4.4 · 弹框数据源:仓库 / 汇总 / 跳过原因 / 未启用 ──────────────
def test_stock_status_includes_warehouse_name(client, operator):
    """弹框要显示仓库名(阶段 4 后端增量:stock-status 补 warehouse)。"""
    oid = _make_order(client, '阶段4仓库名', [('阶段4仓库名布', 'A1', '1')])
    st = client.get(f'/api/v1/inbound-orders/{oid}/stock-status').get_json()
    assert st['success'] is True
    assert st['warehouse']['id'] == 1
    assert st['warehouse']['name'] == '芙蓉仓'
    assert st['warehouse']['code'] == 'FR'


def test_stock_status_items_carry_skip_reason_and_resolvable(client, operator):
    """跳过行在 items 里带 reason_text + resolvable(能否补过账)。"""
    _make_product('阶段4原因布', 'A1')
    oid = _make_order(client, '阶段4原因', [
        ('阶段4原因布', 'A1', '4'),
        ('阶段4原因查无此货', 'C7', '2'),
    ])
    client.patch(f'/api/v1/inbound-orders/{oid}', json={'is_locked': True})
    client.post(f'/api/v1/inbound-orders/{oid}/post')

    st = client.get(f'/api/v1/inbound-orders/{oid}/stock-status').get_json()
    skipped = [i for i in st['items'] if i['state'] == 'skipped']
    assert len(skipped) == 1
    assert skipped[0]['reason_code'] == 'no_product'
    assert skipped[0]['reason_text']
    assert skipped[0]['resolvable'] is True
    assert skipped[0]['product_name'] == '阶段4原因查无此货'
    assert skipped[0]['raw_quantity'] == '2'
    assert skipped[0]['raw_unit'] == 'y'
    # 已入账但未启用的清单
    assert any(n['product_name'] == '阶段4原因布' for n in st['not_tracking'])


def test_bulk_status_matches_single_status(client, operator):
    """批量查询(列表页徽章用)与单单查询口径一致。"""
    from blueprints.stock_posting import get_stock_status_bulk
    _make_product('阶段4批量布', 'A1')
    oid = _make_order(client, '阶段4批量', [
        ('阶段4批量布', 'A1', '9'),
        ('阶段4批量查无此货', 'D3', '1'),
    ])
    client.patch(f'/api/v1/inbound-orders/{oid}', json={'is_locked': True})
    client.post(f'/api/v1/inbound-orders/{oid}/post')

    single = client.get(f'/api/v1/inbound-orders/{oid}/stock-status').get_json()
    bulk = get_stock_status_bulk([oid])[oid]
    assert bulk['total'] == single['counts']['total']
    assert bulk['posted'] == single['counts']['posted']
    assert bulk['skipped'] == single['counts']['skipped']
    assert bulk['pending'] == single['counts']['pending']
    assert bulk['has_posting'] == single['has_posting']
    assert bulk['can_reverse'] == single['can_reverse']
    # 空输入不炸
    assert get_stock_status_bulk([]) == {}
    assert get_stock_status_bulk(None) == {}


# ── 4.5 · 幂等反馈 ───────────────────────────────────────────────
def test_repost_returns_ok_not_error(client, operator):
    """重复点过账 → 200 + already_posted=True + 「本单已过账」提示(不是报错)。"""
    _make_product('阶段4幂等布', 'A1')
    oid = _make_order(client, '阶段4幂等', [('阶段4幂等布', 'A1', '7')])
    client.patch(f'/api/v1/inbound-orders/{oid}', json={'is_locked': True})

    r1 = client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert r1.status_code == 200
    assert r1.get_json()['posted_count'] == 1

    r2 = client.post(f'/api/v1/inbound-orders/{oid}/post')
    assert r2.status_code == 200, '重复过账不应是 4xx/5xx'
    d = r2.get_json()
    assert d['already_posted'] is True
    assert d['posted_count'] == 0
    assert '已过账' in d['message']

    # 库存没变
    st = client.get(f'/api/v1/inbound-orders/{oid}/stock-status').get_json()
    assert st['counts']['posted'] == 1
    assert st['counts']['pending'] == 0


# ── 判据③ · 冲销 ─────────────────────────────────────────────────
def test_reverse_returns_summary_and_restores_balance(client, operator):
    """冲销 → 摘要写明行数/数量 + 余额回到过账前。"""
    _make_product('阶段4冲销布', 'A1')
    oid = _make_order(client, '阶段4冲销', [('阶段4冲销布', 'A1', '11')])
    client.patch(f'/api/v1/inbound-orders/{oid}', json={'is_locked': True})
    client.post(f'/api/v1/inbound-orders/{oid}/post')

    r = client.post(f'/api/v1/inbound-orders/{oid}/reverse')
    assert r.status_code == 200
    d = r.get_json()
    assert d['reversed_count'] == 1
    assert abs(float(d['reversed_qty']) + 11) < TOL
    assert '余额回到过账前' in d['message']

    conn = _conn()
    try:
        rows = conn.execute(
            'SELECT quantity, is_reversal FROM stock_ledger '
            'WHERE biz_type = ? AND order_pk = ? ORDER BY id', ('inbound', oid)).fetchall()
        assert len(rows) == 2
        assert float(rows[1]['quantity']) < 0
        assert int(rows[1]['is_reversal']) == 1
    finally:
        conn.close()

    st = client.get(f'/api/v1/inbound-orders/{oid}/stock-status').get_json()
    assert st['can_reverse'] is False
    assert st['is_reversed'] is True


def test_reverse_requires_login(client):
    """没登录 → 401(A7)。"""
    oid = _make_order(client, '阶段4未登录', [('阶段4未登录布', 'A1', '1')])
    r = client.post(f'/api/v1/inbound-orders/{oid}/reverse')
    assert r.status_code == 401
