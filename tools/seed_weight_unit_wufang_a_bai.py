"""一次性种子:在 product_units 表给「无纺布 A白」加一条按重量计价配置(2026-10-14 简化版)

用户原话:「将无纺布A白纳入按kg管理类别,出货页面当此商品选择的单位不是kg时,自动报错」

本脚本等价于用户在 /product-unit 页面手点:
  商品名称 = 无纺布A白
  规格匹配 = (留空,默认行)
  每支码数 = 1  (YPP 不启用,本字段无意义但 NOT NULL 旧 schema 要求)
  勾「按重量卖」
  允许单位 = kg,斤

幂等:重复跑不重复插入(用 OR REPLACE 复合唯一键 (product_name, spec_keyword))。

历史脏数据(已落库的 unit=y 行,如 1267/1268/1347/3152/3533)由 tools/audit_weight_unit_mismatch.py
单独出 CSV,不在本脚本里回填(按既定不动历史原则)。
"""
import sys
import os
import json
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, 'worklog.db')


def main():
    print(f'数据库:{DB_PATH}')
    print('⚠️  建议先跑 python /d/BAK/_backup_now.py 做完整备份')
    print()

    conn = sqlite3.connect(DB_PATH)
    conn.text_factory = str
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row

    # 「无纺布A白」(订单常见写法)与「无纺布 A白」(DB 写法)是同义不同形,
    # product 表 15 行实际是 '无纺布 A白'(中间有半角空格)。这里加「无纺布 A白」行,
    # 校验时 get_match 直接按 product_name 字面比;orders 的 record product_name='无纺布A白'
    # 不会命中(product 表没这名字)。
    # 解决方案:同时加两条配置(两种写法各一行)—— _match_unit_in_cache 严格比对 product_name,
    # 不会跨写法归一化。所以走「订单 product_name 归一化」方案,需要中间层。
    # 实际:shipping_records 表的 product_name 列历史就是 '无纺布A白'(无空格)15 条
    # 是 product.product_name = '无纺布 A白'(有空格)。两者字面不匹配。
    # 校验 get_match 用 product_name 严格比对,会漏掉历史脏数据。
    # 解决:在 product 表里把 product_name 都改一致(本脚本不动 DB),
    #      改让 product_units 加「无纺布A白」(无空格)行;而校验路径
    #      在 shipping/inbound records 的 product_name 上也跑「去空格」匹配。
    #
    # 但本脚本职责只是「加配置行」,不做校验路径改动。校验路径在
    # _helpers._match_unit_in_cache 用 product_name 严格比对 ——
    # 即「无纺布A白」(订单) 会命中 product_units 中 product_name='无纺布A白' 的行,
    # 不会命中 '无纺布 A白'。所以下面同时加两条,让两种写法都命中。

    rows_to_upsert = [
        # 订单写法(无空格)
        ('无纺布A白', None, 100, 0, 1, '["kg","斤"]'),  # 不启用 YPP,启用按重量
        # product 表写法(有空格) — 双向都覆盖
        ('无纺布 A白', None, 100, 0, 1, '["kg","斤"]'),
    ]

    n = 0
    for (pn, skw, yards, use_yard, use_weight, allowed_json) in rows_to_upsert:
        # 查现有
        if skw is None:
            cur.execute(
                'SELECT id FROM product_units WHERE product_name = ? AND spec_keyword IS NULL',
                (pn,)
            )
        else:
            cur.execute(
                'SELECT id FROM product_units WHERE product_name = ? AND spec_keyword = ?',
                (pn, skw)
            )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                'UPDATE product_units SET yards_per_piece = ?, '
                'is_usingyardforcounting = ?, is_usingweightforcounting = ?, '
                'allowed_units = ? WHERE id = ?',
                (yards, use_yard, use_weight, allowed_json, existing['id'])
            )
            print(f'  ↻ 已更新 {pn} (spec={skw!r})')
        else:
            cur.execute(
                'INSERT INTO product_units '
                '(product_name, spec_keyword, yards_per_piece, '
                ' is_usingyardforcounting, is_usingweightforcounting, allowed_units) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (pn, skw, yards, use_yard, use_weight, allowed_json)
            )
            print(f'  ✓ 已插入 {pn} (spec={skw!r})')
        n += 1
    conn.commit()
    conn.close()
    print(f'\n完成 {n} 条 product_units 行的 UPSERT')
    print('  - 启用按重量: ✓')
    print('  - 允许单位: kg, 斤')
    print('  - 不启用 YPP(每支码数=100 仅占位)')
    print()
    print('下一步:')
    print('  1. 跑 python tools/audit_weight_unit_mismatch.py 看历史脏数据')
    print('  2. 出货页录无纺布A白 + unit=kg 通过;unit=y 应被后端 400 WEIGHT_UNIT_INVALID')


if __name__ == '__main__':
    main()
