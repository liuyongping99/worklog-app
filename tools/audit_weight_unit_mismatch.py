"""扫库生成「按重量单位越界」CSV 报告（2026-10-14 简化版）

用 product_units 表的 is_usingweightforcounting + allowed_units 字段作为配置源;
扫三张明细表,匹配命中后核对 unit 是否在 allowed_units 白名单内。

用法:
  python tools/audit_weight_unit_mismatch.py            # 默认: 越界明细 + CSV
  python tools/audit_weight_unit_mismatch.py --summary  # 只输出按商品聚合
  python tools/audit_weight_unit_mismatch.py --full     # 加全表(已绑商品的明细)
  python tools/audit_weight_unit_mismatch.py --help
"""
import os
import sys
import csv
import sqlite3
import argparse
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, 'worklog.db')
OUTPUT_DIR = r'D:\\BAK' if sys.platform == 'win32' else '/tmp'

# 三套订单明细(沿用既有写法)
TABLES = [
    ('shipping', 'shipping_orders', 'shipping_records',
     "r.id AS record_id, r.order_pk, o.date, o.customer AS customer",
     "r.order_pk = o.id"),
    ('inbound',  'inbound_orders',  'inbound_records',
     "r.id AS record_id, r.order_pk, o.date, o.supplier AS customer",
     "r.order_pk = o.id"),
    ('loading',  'loading_orders',  'loading_order_records',
     "r.id AS record_id, r.order_pk, o.date, o.customer AS customer",
     "r.order_pk = o.id"),
]


def _normalize_allowed(raw):
    """和 _helpers._parse_allowed_units 同款:JSON 数组 → list[str];逗号/顿号 → list[str]"""
    if not raw:
        return []
    s = str(raw).strip()
    if not s:
        return []
    if s.startswith('['):
        import json
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
    parts = [p.strip() for p in s.replace('、', ',').replace(' ', ',').split(',') if p.strip()]
    return parts


def collect(conn, scope_filter=None):
    """扫三张明细表,返所有「unit 越界」记录。

    scope_filter: {'no_filter': True} → 全表(已命中 is_usingweightforcounting=1 的明细)。
    """
    no_filter = scope_filter and scope_filter.get('no_filter')
    # 先抓所有 product_units 里启用按重量的行
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    cur.execute(
        'SELECT product_name, spec_keyword, allowed_units '
        'FROM product_units WHERE is_usingweightforcounting = 1'
    )
    cfg_rows = cur.fetchall()
    # 建 (pn, spec) → allowed_units 索引。spec_keyword 空 = 默认行(兜底)
    cfg_map = {}
    for r in cfg_rows:
        pn = r['product_name'] or ''
        skw = r['spec_keyword'] or ''
        au = _normalize_allowed(r['allowed_units'])
        if not au:
            continue
        cfg_map.setdefault((pn, skw), set()).update(au)
    if not cfg_map:
        return []

    rows = []
    for short, order_tbl, rec_tbl, select, join in TABLES:
        # 选 record.product_name / specification 与任一 cfg 匹配的行
        # 用 product_name IN 列表做粗筛(避免大表全扫);spec 精筛在 Python
        pns = sorted({k[0] for k in cfg_map.keys()})
        if not pns:
            continue
        placeholders = ','.join('?' * len(pns))
        unit_where = '' if no_filter else '''
                  AND r.unit IS NOT NULL AND r.unit != ''
                  AND NOT EXISTS (
                    SELECT 1 FROM product_units pu2
                    WHERE pu2.product_name = r.product_name
                      AND pu2.is_usingweightforcounting = 1
                      AND (pu2.allowed_units LIKE '%"' || r.unit || '"%'
                           OR pu2.allowed_units LIKE '%' || r.unit || ',%'
                           OR pu2.allowed_units LIKE '%,' || r.unit || '%')
                  )'''
        sql = f'''
            SELECT {select},
                   r.product_name, r.specification, r.quantity, r.unit, r.remark,
                   pu.allowed_units, pu.spec_keyword AS matched_spec_keyword
            FROM {rec_tbl} r
            JOIN {order_tbl} o ON {join}
            JOIN product_units pu
              ON pu.product_name = r.product_name
             AND pu.is_usingweightforcounting = 1
            WHERE pu.product_name IN ({placeholders}){unit_where}
        '''
        try:
            cur.execute(sql, pns)
            page_rows = [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError as e:
            print(f'  ⚠️  {short}: 跳过 ({e})', file=sys.stderr)
            continue
        for r in page_rows:
            r['__table'] = rec_tbl
            rows.append(r)
    return rows


def write_csv(path, rows):
    if not rows:
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            f.write('No records\n')
        return
    fields = ['__table', 'record_id', 'order_pk', 'date', 'customer',
              'product_name', 'specification', 'quantity', 'unit', 'remark',
              'matched_spec_keyword', 'allowed_units']
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f'  📄 已写:{path} ({len(rows)} 行)')


def csv_path(suffix=''):
    fname = f'audit_weight_unit_{__import__("datetime").datetime.now().strftime("%Y%m%d_%H%M")}'
    if suffix:
        fname += '_' + suffix
    return os.path.join(OUTPUT_DIR, fname + '.csv')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--full', action='store_true', help='输出全部已绑商品明细(不只越界)')
    ap.add_argument('--summary', action='store_true', help='只输出按商品聚合统计')
    args = ap.parse_args()

    print(f'数据库:{DB_PATH}  exists: {os.path.exists(DB_PATH)}')
    if not os.path.exists(DB_PATH):
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.text_factory = str

    rows = collect(conn)
    conn.close()

    print(f'\n按重量单位越界记录:{len(rows)} 条')
    if not rows:
        print('  ✓ 暂无')
        return

    by_table = defaultdict(int)
    for r in rows:
        by_table[r['__table']] += 1
    print('\n按表分布:')
    for t, n in sorted(by_table.items(), key=lambda x: -x[1]):
        print(f'  {t:<32} {n}')

    # 按商品聚合
    by_product = defaultdict(int)
    for r in rows:
        by_product[(r['product_name'], r.get('specification', ''),
                    r.get('allowed_units', ''))] += 1
    print('\n按商品聚合:')
    print(f"  {'商品名':<22} {'规格':<14} {'允许单位':<14} {'越界条数'}")
    print('  ' + '-' * 60)
    for (pn, sp, au), n in sorted(by_product.items(), key=lambda x: -x[1]):
        print(f'  {pn[:20]:<22} {sp[:12]:<14} {au:<14} {n}')

    if args.summary:
        return

    out = csv_path('mistakes')
    write_csv(out, rows)
    print(f'\n明细 CSV:{out}')

    if args.full:
        conn = sqlite3.connect(DB_PATH)
        conn.text_factory = str
        all_rows = collect(conn, scope_filter={'no_filter': True})
        conn.close()
        out_all = csv_path('full')
        write_csv(out_all, all_rows)
        print(f'\n全表 CSV:{out_all}')
        print(f'  全表 {len(all_rows)} 行,越界 {len(rows)} 行 → 越界比例 {len(rows) / max(1, len(all_rows)) * 100:.1f}%')


if __name__ == '__main__':
    main()
