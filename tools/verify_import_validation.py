# -*- coding: utf-8 -*-
"""§f「导入即校验」端到端口径验证 —— 2026-10-09

验证 _helpers.annotate_import_validation 用真实 DB 规则表算出的标志是否符合预期。
不写库、不改数据，纯内存调用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints._helpers import (  # noqa: E402
    annotate_import_validation,
    summarize_import_validation,
    check_qty_invalid,
)

CASES = [
    # ── 轨道①:YPP(按码卖)──
    # 7P环保三文治 ypp=4850/100=48.5,备注 3支*48.5y → 期望 145.5
    ('YPP 一致(乘法)', dict(product_name='7P环保三文治', specification='', quantity='145.5',
                           unit='y', remark='3支*48.5y'), '', ''),
    # 同样 3支 但数量写成 150 → 期望 145.5,差 4.5,3 支 → warn
    ('YPP 不符(多支→warn)', dict(product_name='7P环保三文治', specification='', quantity='150',
                                 unit='y', remark='3支*48.5y'), 'warn', ''),
    # 单支偏差 → info:1支*48.5y 期望 48.5,写 50 → info
    ('YPP 单支偏差(→info)', dict(product_name='7P环保三文治', specification='', quantity='50',
                                 unit='y', remark='1支*48.5y'), 'info', ''),
    # 散码加法:3支*48.5y+2y → 期望 147.5
    ('YPP 散码一致', dict(product_name='7P环保三文治', specification='', quantity='147.5',
                          unit='y', remark='3支*48.5y+2y'), '', ''),
    ('YPP 散码不符', dict(product_name='7P环保三文治', specification='', quantity='150',
                          unit='y', remark='3支*48.5y+2y'), 'warn', ''),
    # 不看 unit:只看「商品配了 YPP + 备注有支字」。
    # ⚠️ 探针发现 unit='支' 但数量实为码数的行真实存在(出货16行/入库26行,
    #    如 露华里 quantity=390码 备注'13支'),所以现有口径**不按 unit 分流**——
    #    对这些行做 YPP 校验恰恰是有意义的(13支×30码=390 ✓)。
    ('unit=支 但数量是码数(YPP仍校验)', dict(product_name='露华里', specification='足0.6面料黑',
                                          quantity='390', unit='支', remark='13支'), '', ''),
    # 无「支」字备注 → 跳过
    ('备注无支字→跳过', dict(product_name='7P环保三文治', specification='', quantity='100',
                             unit='y', remark='3y'), '', ''),

    # ── 轨道②:件数换算(纸类按件收)──
    # 日本纸616/516 spec 0.6 → 1500 张/件;2件 → 期望 3000
    ('件数一致', dict(product_name='日本纸616/516', specification='0.6', quantity='3000',
                      unit='张', remark='2件'), '', ''),
    ('件数不符(多件→warn)', dict(product_name='日本纸616/516', specification='0.6', quantity='5000',
                                 unit='张', remark='2件'), '', 'warn'),
    ('件数单件偏差(→info)', dict(product_name='日本纸616/516', specification='0.6', quantity='1600',
                                 unit='张', remark='1件'), '', 'info'),
    # 散装张数:2件+100张 → 期望 3100
    ('件数+散装一致', dict(product_name='日本纸616/516', specification='0.6', quantity='3100',
                           unit='张', remark='2件+100张'), '', ''),
    ('件数+散装不符', dict(product_name='日本纸616/516', specification='0.6', quantity='3000',
                           unit='张', remark='2件+100张'), '', 'warn'),

    # ── 数量格式异常(独立标志)──
    # 数量读不出来(float 抛)→ check_remark/check_piece_mismatch 内部直接 return ''
    # (算术比对无从谈起),所以 mismatch 为空,只有 qty_invalid 一个标志。
    ('数量非数字→只标 qty_invalid', dict(product_name='7P环保三文治', specification='',
                                       quantity='货-30', unit='y', remark='3支*48.5y'), '', ''),
    ('数量为空→只标 qty_invalid', dict(product_name='7P环保三文治', specification='',
                                      quantity='', unit='y', remark='3支*48.5y'), '', ''),
]

# 数量非数字 / 为空 的独立断言
QTY_CASES = [('', True), ('货-30', True), ('  ', True), ('145.5', False), ('0', False), (None, True)]

fail = 0
print('=' * 78)
print('§f 导入即校验 —— 口径验证(真实 product_units / piece_conversions)')
print('=' * 78)

for name, rec, exp_ypp, exp_piece in CASES:
    got = annotate_import_validation([dict(rec)])[0]
    ok = (got['mismatch'] == exp_ypp) and (got['piece_mismatch'] == exp_piece)
    flag = 'OK  ' if ok else 'FAIL'
    if not ok:
        fail += 1
    detail = ''
    if got.get('mismatch_detail'):
        d = got['mismatch_detail']
        detail = f"  [{d['label']} 期望{d['expected']} 实际{d['actual']} 差{d['diff']}]"
    print(f'{flag} {name:24s} mismatch={got["mismatch"] or "-":5s} piece={got["piece_mismatch"] or "-":5s}'
          f' qty_invalid={str(got["qty_invalid"]):5s}{detail}')
    if not ok:
        print(f'     期望 mismatch={exp_ypp!r} piece={exp_piece!r}')

print('-' * 78)
print('check_qty_invalid 独立断言:')
for q, exp in QTY_CASES:
    got = check_qty_invalid(q)
    ok = got == exp
    if not ok:
        fail += 1
    print(f'{"OK  " if ok else "FAIL"} quantity={q!r:10s} → {got}')

print('-' * 78)
# 汇总断言
batch = [dict(c[1]) for c in CASES]
annotate_import_validation(batch)
s = summarize_import_validation(batch)
print('summarize_import_validation →', s)
assert s['total'] == len(CASES), f'total 应为 {len(CASES)}'
assert s['flagged'] == s['warn'] + s['info'] + s['qty_invalid'], 'flagged 应等于三项之和'
assert s['flagged'] > 0, '样本里应至少有一行被标'

# 关键不变量:同一份输入,列表页渲染用的 check_remark 与导入校验必须同结论
print('-' * 78)
print('不变量:annotate 的 mismatch 必须 == check_remark 直接调用(同源):')
from blueprints._helpers import get_ypp, check_remark  # noqa: E402
drift = 0
for name, rec, _, _ in CASES:
    ypp = get_ypp(rec['product_name'], rec['specification'])
    direct = check_remark(rec['remark'], str(rec['quantity']), ypp)
    via = annotate_import_validation([dict(rec)])[0]['mismatch']
    if direct != via:
        drift += 1
        print(f'  FAIL 漂移 {name}: check_remark={direct!r} vs annotate={via!r}')
print(f'  {"OK   零漂移" if drift == 0 else f"FAIL {drift} 处漂移"}')

print('=' * 78)
if fail or drift:
    print(f'❌ 失败: {fail} 条用例不符, {drift} 处口径漂移')
    sys.exit(1)
print(f'✅ 全部通过: {len(CASES)} 条用例 + {len(QTY_CASES)} 条 qty 断言,零口径漂移')
