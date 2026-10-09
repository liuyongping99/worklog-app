# -*- coding: utf-8 -*-
"""探针:unit='支' 且备注含「支」的行,在 YPP 码基商品里有多少 —— 只读,不改数据"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'worklog.db')
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

ypp_pn = set(r[0] for r in c.execute(
    'SELECT DISTINCT product_name FROM product_units WHERE is_usingyardforcounting=1'))
print('YPP 码基商品名数:', len(ypp_pn))

for tbl in ['shipping_records', 'inbound_records', 'loading_order_records']:
    tot = c.execute(f'SELECT COUNT(*) FROM {tbl}').fetchone()[0]
    rows = c.execute(
        f"SELECT product_name, specification, quantity, remark FROM {tbl} "
        f"WHERE unit='支' AND remark LIKE '%支%'").fetchall()
    in_ypp = [r for r in rows if r['product_name'] in ypp_pn]
    print(f'\n=== {tbl}: 总 {tot} 行 | unit=支 且备注含「支」 {len(rows)} 行 '
          f'| 其中商品在 YPP 码基表 {len(in_ypp)} 行 ===')
    for r in in_ypp[:8]:
        print('   ', dict(r))
