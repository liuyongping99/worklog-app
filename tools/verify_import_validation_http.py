# -*- coding: utf-8 -*-
"""§f「导入即校验」HTTP 端到端验证 —— 打真实 batch 端点,验响应体带标志

三页各测一遍。流程:
  1. 直接建临时订单(绕开各蓝图建单端点的字段名差异)
  2. POST /records/batch 造 3 行(YP warn / 件数 warn / 完全一致)
  3. 验响应体:逐行标志 + mismatch_detail + validation 汇总
  4. 确认 DB 表无 mismatch 字段(不落库)
  5. 清理:先 SELECT id 再按 id IN (...) 删(硬规则禁 LIKE 模糊删)

⚠️ 只碰自己建的 __ZZ_TEMP_f_* 临时订单,不碰任何业务订单。
"""
import datetime
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:5050'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'worklog.db')
MARK = '__ZZ_TEMP_f_test__'

fail = 0


def api(path, payload=None, cookie=None, method=None):
    url = BASE + path
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ('POST' if data else 'GET'))
    req.add_header('Content-Type', 'application/json')
    if cookie:
        req.add_header('Cookie', cookie)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8')
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {'_raw': raw[:300]}
    except urllib.error.URLError as e:
        return 0, {'error': f'连接失败: {e}'}


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def flags_of(rec):
    return {k: rec.get(k) for k in ('mismatch', 'piece_mismatch', 'qty_invalid') if rec.get(k)}


def check(label, cond, extra=''):
    global fail
    if not cond:
        fail += 1
    print(f'  {"OK  " if cond else "FAIL"} {label}' + (f'  → {extra}' if extra else ''))


# 三页配置:(蓝图前缀, 订单表, 明细表, 日期列, 客户列)
PAGES = [
    ('shipping', '/api/v1/shipping-orders', 'shipping_orders', 'shipping_records', 'customer'),
    ('inbound', '/api/v1/inbound-orders', 'inbound_orders', 'inbound_records', 'supplier'),
    ('loading', '/api/v1/loading-orders', 'loading_orders', 'loading_order_records', 'customer'),
]

c = db()
staff = c.execute('SELECT id, name FROM staff WHERE is_active=1 ORDER BY id LIMIT 1').fetchone()
if not staff:
    print('❌ 没有 is_active=1 的 staff')
    sys.exit(1)
cookie = f'operator_id={staff["id"]}'
print(f'登录身份: {staff["name"]} (id={staff["id"]})\n')

today = datetime.date.today().isoformat()
created = []   # (订单表, 明细表, order_pk)

try:
    for name, api_prefix, otbl, rtbl, custcol in PAGES:
        print('=' * 72)
        print(f'【{name}】{api_prefix}/<id>/records/batch')
        print('=' * 72)

        # ── 1. 建临时订单 ──
        c.execute(f'INSERT INTO {otbl} (date, {custcol}, order_num, created_at, is_locked) '
                  f'VALUES (?,?,?,?,0)', (today, MARK, MARK, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        order_pk = c.execute('SELECT last_insert_rowid()').fetchone()[0]
        c.commit()
        created.append((otbl, rtbl, order_pk))
        print(f'  临时订单 {otbl}#{order_pk}')

        # ── 2. batch 造 3 行 ──
        rows = [
            # a) YPP 不符(多支 → warn):7P环保三文治 ypp=48.5,3支 → 期望 145.5,写 150
            {'product_name': '7P环保三文治', 'specification': '', 'quantity': '150',
             'unit': 'y', 'remark': '3支*48.5y'},
            # b) 件数换算不符(多件 → warn):日本纸0.6 → 1500张/件,2件 → 期望 3000,写 5000
            {'product_name': '日本纸616/516', 'specification': '0.6', 'quantity': '5000',
             'unit': '张', 'remark': '2件'},
            # c) 完全一致:3支*48.5y → 145.5
            {'product_name': '7P环保三文治', 'specification': '', 'quantity': '145.5',
             'unit': 'y', 'remark': '3支*48.5y'},
        ]
        payload = {'items': rows, 'records': rows}   # 两键都给:出货读 items,入库/装柜读 records
        st, r = api(f'{api_prefix}/{order_pk}/records/batch', payload, cookie=cookie)
        print(f'  POST → HTTP {st}')
        if st != 201:
            print('  响应:', json.dumps(r, ensure_ascii=False)[:400])
            check(f'{name} batch 返回 201', False, f'got {st}')
            print()
            continue

        check(f'{name} batch 201 + success', r.get('success') is True)
        recs = r.get('records') or []
        check(f'{name} 返回 3 条明细', len(recs) == 3, f'got {len(recs)}')

        if len(recs) == 3:
            r0, r1, r2 = recs
            # 行1:YPP warn
            check('行1 mismatch=warn', r0.get('mismatch') == 'warn', repr(r0.get('mismatch')))
            d = r0.get('mismatch_detail') or {}
            check('行1 mismatch_detail.expected=145.5', abs((d.get('expected') or 0) - 145.5) < 0.01,
                  repr(d.get('expected')))
            check('行1 diff=+4.5', abs((d.get('diff') or 0) - 4.5) < 0.01, repr(d.get('diff')))
            check('行1 unit_hint 有支数提示', '支' in (r0.get('unit_hint') or ''), repr(r0.get('unit_hint')))
            # 行2:件数换算 warn
            check('行2 piece_mismatch=warn', r1.get('piece_mismatch') == 'warn', repr(r1.get('piece_mismatch')))
            check('行2 piece_hint 含 张/件', '张/件' in (r1.get('piece_hint') or ''), repr(r1.get('piece_hint')))
            # 行3:一致 → 无标志
            check('行3 无任何标志', not flags_of(r2), json.dumps(flags_of(r2), ensure_ascii=False))

        v = r.get('validation')
        check(f'{name} 响应带 validation', isinstance(v, dict), repr(v))
        if v:
            check('validation.total=3', v.get('total') == 3, repr(v.get('total')))
            check('validation.warn=2', v.get('warn') == 2, repr(v.get('warn')))
            check('validation.flagged=2', v.get('flagged') == 2, repr(v.get('flagged')))

        # ── 3. 不落库确认 ──
        cols = [x[1] for x in c.execute(f'PRAGMA table_info({rtbl})')]
        check(f'{rtbl} 无 mismatch 字段(不落库)', 'mismatch' not in cols, f'列数={len(cols)}')
        print()

finally:
    # ── 4. 清理 ──
    print('=' * 72)
    print('清理临时数据')
    for otbl, rtbl, order_pk in created:
        ids = [x[0] for x in c.execute(f'SELECT id FROM {rtbl} WHERE order_pk = ?', (order_pk,))]
        print(f'  {otbl}#{order_pk}: 明细 id={ids}')
        if ids:
            q = ','.join(str(i) for i in ids)
            c.execute(f'DELETE FROM {rtbl} WHERE id IN ({q})')
        c.execute(f'DELETE FROM {otbl} WHERE id = ?', (order_pk,))
    c.commit()
    for otbl, rtbl, order_pk in created:
        left = c.execute(f'SELECT COUNT(*) FROM {rtbl} WHERE order_pk = ?', (order_pk,)).fetchone()[0]
        oleft = c.execute(f'SELECT COUNT(*) FROM {otbl} WHERE id = ?', (order_pk,)).fetchone()[0]
        print(f'  {otbl}#{order_pk} 残留: 订单 {oleft} / 明细 {left}')
    # 全库确认没有 __ZZ_TEMP_f_test__ 残留
    for otbl, _, _ in created:
        n = c.execute(f'SELECT COUNT(*) FROM {otbl} WHERE order_num = ?', (MARK,)).fetchone()[0]
        print(f'  {otbl} 中 {MARK} 残留: {n}')
    c.close()

print('\n' + '=' * 72)
print(f'❌ 失败 {fail} 项' if fail else '✅ 三页 HTTP 端到端全部通过，临时数据已清理')
sys.exit(1 if fail else 0)
